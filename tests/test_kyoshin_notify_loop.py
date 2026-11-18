"""
tests/test_kyoshin_notify_loop.py
=================================
強震モニタ通知ループ（cogs/kyoshin_monitor.py の _notify_loop）のエラー処理のテスト（2026-11追加）。

不具合: Discord への送信中に一時的な切断（aiohttp の ServerDisconnectedError）が起きると、汎用の
except に落ちて ERROR（全文トレース）となり、管理者チャンネルにも誤エラー通知が出ていた。
通知ループは次の周期で最新のフレームを送り直すため、想定内のネットワークエラーは WARNING に留め、
ループを止めない。想定外の例外は従来どおり ERROR。
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import aiohttp  # noqa: E402
import pytest  # noqa: E402

from cogs.kyoshin_monitor import KyoshinMonitorCog  # noqa: E402

ERRORS = [
    aiohttp.ServerDisconnectedError("Server disconnected"),
    aiohttp.ClientConnectionError("Cannot connect"),
    asyncio.TimeoutError(),
    ConnectionResetError("reset"),
]


async def _no_sleep(*a, **kw):
    return None


def _run_loop(exc, monkeypatch, caplog, ticks=2):
    """_notify_tick が1回目に exc を投げ、2回目以降は成功する状況で、ループを ticks 周回させる。"""
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    cog = KyoshinMonitorCog.__new__(KyoshinMonitorCog)
    state = {"calls": 0}
    cog.bot = SimpleNamespace(is_closed=lambda: state["calls"] >= ticks)
    cog._notify_interval_sec = 1.0

    async def fake_tick():
        state["calls"] += 1
        if state["calls"] == 1:
            raise exc
    cog._notify_tick = fake_tick
    with caplog.at_level(logging.DEBUG, logger="QTLBot"):
        asyncio.run(cog._notify_loop())
    return state["calls"]


@pytest.mark.parametrize("exc", ERRORS, ids=lambda e: type(e).__name__)
def test_transient_network_error_is_warning_and_loop_continues(exc, monkeypatch, caplog):
    calls = _run_loop(exc, monkeypatch, caplog)
    assert calls == 2                                    # 例外のあとも次の周期へ進む
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1 and "次の周期で再試行します" in warns[0].getMessage()
    assert warns[0].exc_info is None                     # トレースバックは出さない


def test_unexpected_error_is_still_error(monkeypatch, caplog):
    calls = _run_loop(ValueError("想定外"), monkeypatch, caplog)
    assert calls == 2                                    # 想定外でもループは止めない（従来どおり）
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and errors[0].exc_info is not None
