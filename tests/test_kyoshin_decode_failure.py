"""不正な強震モニタ画像の検査（describe_image_problem）と集計ウィンドウ（_record_decode_result）のテスト。

配信元(NIED)は HTTP 200 のまま0バイトや途中で切れた GIF を散発的に返す（実測約1%、再取得でも直らない）。
- 原因が分かるメッセージで記録する（従来は "cannot identify image file <_io.BytesIO ...>" のみ）
- 散発（低割合）ではWARNINGを出さず、連続的な異常のときだけWARNINGを出す
"""
import asyncio
import io
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest  # noqa: E402

import cogs.kyoshin_monitor as km  # noqa: E402
from core.kyoshin_shared import describe_image_problem  # noqa: E402

PIL = pytest.importorskip("PIL.Image")


def _gif() -> bytes:
    buf = io.BytesIO()
    PIL.new("P", (32, 32), 3).save(buf, "GIF")
    return buf.getvalue()


# ── A: デコード前の検査 ──
def test_describe_problem_cases():
    assert "0バイト" in describe_image_problem(b"")
    assert "GIF形式ではありません" in describe_image_problem(b"<html>error</html>")
    assert describe_image_problem(_gif()) is None
    # 末尾欠けは事前検査では弾かず Pillow に任せる（有効な GIF の誤検出を避ける）
    assert describe_image_problem(_gif()[:-30]) is None


# ── B: 集計ウィンドウ ──
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def cog(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(km.time, "monotonic", clock)
    monkeypatch.setattr(km, "KYOSHIN_DECODE_FAILURE_LOG_INTERVAL_SEC", 300)
    monkeypatch.setattr(km, "KYOSHIN_DECODE_FAILURE_WARN_RATIO", 0.2)
    monkeypatch.setattr(km, "KYOSHIN_DECODE_FAILURE_WARN_MIN_COUNT", 5)
    c = km.KyoshinMonitorCog.__new__(km.KyoshinMonitorCog)
    c._decode_window_start_mono = None
    c._decode_window_attempts = 0
    c._decode_window_failures = 0
    c._decode_window_last_reason = ""
    c.clock = clock
    return c


def _feed(c, ok: int, ng: int):
    """1秒間隔で ok 枚の成功と ng 枚の失敗を混ぜて記録し、ウィンドウを閉じる。"""
    total = ok + ng
    for i in range(total):
        c.clock.t += 1.0
        c._record_decode_result("0バイトの応答" if i < ng else None)
    c.clock.t += 300      # ウィンドウ終端を越える
    c._record_decode_result(None)


def _levels(caplog):
    return [r.levelno for r in caplog.records]


def test_sporadic_failures_do_not_warn(cog, caplog):
    with caplog.at_level(logging.DEBUG, logger="QTLBot"):
        _feed(cog, ok=290, ng=10)            # 約3%（実測の通常範囲）
    assert logging.WARNING not in _levels(caplog)
    assert any("不正でした" in r.getMessage() for r in caplog.records)   # DEBUG集計は残る


def test_sustained_failures_warn(cog, caplog):
    with caplog.at_level(logging.DEBUG, logger="QTLBot"):
        _feed(cog, ok=100, ng=200)           # 約67%（配信障害相当）
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    assert "配信元の障害" in warns[0].getMessage() and "200枚" in warns[0].getMessage()


def test_min_count_prevents_false_alarm(cog, caplog):
    with caplog.at_level(logging.DEBUG, logger="QTLBot"):
        _feed(cog, ok=1, ng=2)               # 割合は高いが件数が少ない（<5）
    assert logging.WARNING not in _levels(caplog)


def test_window_resets(cog, caplog):
    with caplog.at_level(logging.DEBUG, logger="QTLBot"):
        _feed(cog, ok=100, ng=200)
        caplog.clear()
        _feed(cog, ok=300, ng=0)             # 回復後は警告なし・集計もなし
    assert logging.WARNING not in _levels(caplog)
    assert not any("不正でした" in r.getMessage() for r in caplog.records)


# ── 取得経路: 理由付きで記録され、検知には空dictを返す ──
def _fetch_cog(monkeypatch, body: bytes):
    c = km.KyoshinMonitorCog.__new__(km.KyoshinMonitorCog)
    c._decode_window_start_mono = None
    c._decode_window_attempts = 0
    c._decode_window_failures = 0
    c._decode_window_last_reason = ""
    c._stations_initialized = True
    c._station_by_code = {}
    c._last_image_url = None
    c._last_image_at = None
    c._latest_max_shindo = None

    async def fake_download():
        return body, "https://example.invalid/20261027055534.jma_s.gif"

    c._download_latest_image = fake_download
    monkeypatch.setattr(km, "KYOSHIN_RECV_JMA_S_ENABLE", True)
    monkeypatch.setattr(km, "_PIL_AVAILABLE", True)
    return c


@pytest.mark.parametrize("body,expect", [
    (b"", "0バイト"),
    (_gif()[:4096 if len(_gif()) > 4096 else -30], "GIF終端なし"),
])
def test_fetch_records_reason(monkeypatch, caplog, body, expect):
    c = _fetch_cog(monkeypatch, body)
    with caplog.at_level(logging.DEBUG, logger="QTLBot"):
        assert asyncio.run(c._fetch_current_shindo_map()) == {}
    assert c._decode_window_failures == 1
    assert expect in c._decode_window_last_reason
    assert "url=https://example.invalid" in c._decode_window_last_reason
    assert "BytesIO" not in c._decode_window_last_reason     # 不明瞭なメモリアドレスは出さない


def test_fetch_success_counts_attempt(monkeypatch):
    c = _fetch_cog(monkeypatch, _gif())
    assert asyncio.run(c._fetch_current_shindo_map()) == {}      # 観測点0件なので空だが成功扱い
    assert (c._decode_window_attempts, c._decode_window_failures) == (1, 0)
