"""
tests/test_error_notify_queue.py
=================================
ネットワーク断時の管理者エラー通知（保留 → 復旧後にまとめて送信）のテスト。
core/error_notify_queue.py と SystemCog.notify_error / _flush_pending_errors を確認する。
"""
import asyncio
import os
import socket
from datetime import datetime, timedelta

import aiohttp
import discord

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest

from core.error_notify_queue import (
    FLUSH_MAX_LINES, PENDING_MAX_ITEMS, PendingErrorQueue, build_flush_text, is_network_error,
)

NOW = datetime(2026, 11, 27, 5, 37, 0)


# ---------- is_network_error ----------
class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "x"


@pytest.mark.parametrize("exc", [
    aiohttp.ClientConnectionError("x"),
    aiohttp.ServerTimeoutError("x"),
    asyncio.TimeoutError(),
    ConnectionResetError(),
    socket.gaierror(-3, "dns"),
    discord.GatewayNotFound(),
    discord.HTTPException(_Resp(503), "unavailable"),
])
def test_network_errors_are_detected(exc):
    assert is_network_error(exc) is True


@pytest.mark.parametrize("exc", [
    ValueError("x"),
    PermissionError("x"),
    discord.HTTPException(_Resp(400), "bad request"),
    discord.HTTPException(_Resp(403), "forbidden"),
    None,
])
def test_non_network_errors_are_not_detected(exc):
    assert is_network_error(exc) is False


def test_network_error_found_through_cause_chain():
    # 実ログと同じ形: OSError ← DNSError の連鎖を aiohttp が包む
    try:
        try:
            raise socket.gaierror(-3, "Timeout while contacting DNS servers")
        except OSError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as e:
        assert is_network_error(e) is True


# ---------- PendingErrorQueue ----------
def test_queue_dedupes_same_key_and_counts():
    q = PendingErrorQueue()
    q.add(1, "a.b", "msg", NOW)
    q.add(1, "a.b", "msg", NOW)
    q.add(2, "c.d", "other", NOW)
    items, dropped = q.drain()
    assert [(i.err_type, i.count) for i in items] == [("a.b", 2), ("c.d", 1)] and dropped == 0
    assert len(q) == 0


def test_queue_drops_oldest_over_limit():
    q = PendingErrorQueue(max_items=3)
    for i in range(5):
        q.add(i, "t", f"m{i}", NOW)
    items, dropped = q.drain()
    assert [i.message for i in items] == ["m2", "m3", "m4"] and dropped == 2


def test_restore_puts_items_back_before_newer_ones():
    q = PendingErrorQueue()
    q.add(1, "t", "old1", NOW)
    q.add(2, "t", "old2", NOW)
    items, dropped = q.drain()
    q.add(3, "t", "new", NOW)                     # 送信中に追加された分
    q.restore(items, dropped)
    again, _ = q.drain()
    assert [i.message for i in again] == ["old1", "old2", "new"]


def test_build_flush_text_limits_lines_and_reports_dropped():
    items = [__import__("core.error_notify_queue", fromlist=["PendingError"]).PendingError(
        "mod.func", "x" * 300, NOW + timedelta(seconds=i)) for i in range(FLUSH_MAX_LINES + 3)]
    text = build_flush_text(items, dropped=4)
    assert text.count("\n") == FLUSH_MAX_LINES + 1          # 10行 + 「ほか」+ 破棄の注記
    assert "ほか 3 件" in text and "古い 4 件" in text
    assert max(len(line) for line in text.splitlines()) < 220  # 長いメッセージは切り詰め


# ---------- SystemCog.notify_error ----------
from cogs.system import SystemCog  # noqa: E402


class _Channel:
    def __init__(self, exc=None):
        self.exc = exc
        self.sent = []

    async def send(self, *, embed):
        if self.exc is not None:
            raise self.exc
        self.sent.append(embed)


class _Loop:
    def create_task(self, coro):
        coro.close()                  # ワーカー本体は動かさない（起動されたことだけ記録する）
        _Loop.tasks += 1

        class _T:
            def done(self):
                return False

            def cancel(self):
                pass
        return _T()
    tasks = 0


class _Bot:
    loop = _Loop()

    def is_closed(self):
        return False


def _cog(channel):
    cog = object.__new__(SystemCog)
    cog.bot = _Bot()
    cog.admin_channel = channel
    cog.error_notification_cache = {}
    cog.error_count_today = 0
    cog.daily_error_summary = {}
    cog._pending_errors = PendingErrorQueue()
    cog._pending_flush_lock = asyncio.Lock()
    cog._pending_flush_task = None
    return cog


def test_notify_error_queues_on_network_failure_without_traceback(caplog):
    ch = _Channel(aiohttp.ClientConnectionError("Cannot connect to host discord.com:443"))
    cog = _cog(ch)
    _Loop.tasks = 0
    with caplog.at_level("DEBUG", logger="QTLBot"):
        asyncio.run(cog.notify_error("boom", "mod.func"))
    assert len(cog._pending_errors) == 1
    assert _Loop.tasks == 1                                    # 再送ワーカーを起動
    warns = [r for r in caplog.records if r.name == "QTLBot" and r.levelname == "WARNING"]
    assert len(warns) == 1 and warns[0].exc_info is None       # 1行の WARNING（トレースバック無し）
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_notify_error_non_network_failure_still_logs_error(caplog):
    cog = _cog(_Channel(discord.HTTPException(_Resp(403), "Missing Permissions")))
    with caplog.at_level("DEBUG", logger="QTLBot"):
        asyncio.run(cog.notify_error("boom", "mod.func"))
    assert len(cog._pending_errors) == 0                       # 権限エラーは保留しない
    assert [r for r in caplog.records if r.levelname == "ERROR"]


def test_flush_sends_one_summary_after_recovery_and_keeps_on_failure():
    ch = _Channel(aiohttp.ClientConnectionError("down"))
    cog = _cog(ch)
    for i in range(3):
        cog._pending_errors.add(i, f"mod.f{i}", f"error {i}", NOW)

    assert asyncio.run(cog._flush_pending_errors()) is False   # まだ繋がらない → 保留へ戻す
    assert len(cog._pending_errors) == 3

    ch.exc = None                                              # 復旧
    assert asyncio.run(cog._flush_pending_errors()) is True
    assert len(ch.sent) == 1                                   # 3件が1通にまとまる
    assert all(f"error {i}" in ch.sent[0].description for i in range(3))
    assert len(cog._pending_errors) == 0
    assert asyncio.run(cog._flush_pending_errors()) is True    # 空なら何もしない
    assert len(ch.sent) == 1


def test_flush_discards_on_non_network_failure():
    ch = _Channel(discord.HTTPException(_Resp(403), "Missing Permissions"))
    cog = _cog(ch)
    cog._pending_errors.add(1, "t", "m", NOW)
    assert asyncio.run(cog._flush_pending_errors()) is False
    assert len(cog._pending_errors) == 0                       # 無限リトライしない


def test_pending_limit_constant_is_reasonable():
    assert 10 <= PENDING_MAX_ITEMS <= 200
