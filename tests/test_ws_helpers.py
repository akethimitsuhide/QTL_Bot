"""
tests/test_ws_helpers.py
========================
core/ws_helpers.py の ws_connect_loop（WebSocket 自動再接続ループ）のログ表記のテスト（2026-11追加）。

変更点: 接続を確立したあとの切断を「再接続失敗」と表記していたのをやめ、接続時間を出す。
接続できなかった場合は従来どおり「再接続失敗 #N」。再接続の待ち時間（指数バックオフ）と
on_connect / on_disconnect の呼び出しは変えない。
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import core.ws_helpers as ws_helpers  # noqa: E402


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _run(script, monkeypatch, caplog, init_delay=5, max_delay=60):
    """script: 接続ごとの振る舞いのリスト。
      ("fail", exc)               接続自体に失敗する
      ("drop", seconds, exc)      接続後 seconds 秒で例外により切断される
      ("closed", seconds)         接続後 seconds 秒でサーバーが正常に閉じる
    戻り値: (待機秒数のリスト, on_connect 回数, on_disconnect 回数)
    """
    clock = _Clock()
    monkeypatch.setattr(ws_helpers.time, "monotonic", clock)
    pending = list(script)
    delays, counts = [], {"connect": 0, "disconnect": 0}

    class FakeConnect:
        def __init__(self, url, **kw):
            self.entry = pending.pop(0)

        async def __aenter__(self):
            if self.entry[0] == "fail":
                raise self.entry[1]
            return self

        async def __aexit__(self, *a):
            return False

    async def handler(ws):
        kind = ws.entry[0]
        clock.now += ws.entry[1]
        if kind == "drop":
            raise ws.entry[2]

    async def fake_sleep(sec):
        delays.append(sec)

    monkeypatch.setattr(ws_helpers.websockets, "connect", FakeConnect)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def run():
        await ws_helpers.ws_connect_loop(
            lambda: not pending, "ws://x", "TestWS", handler,
            on_disconnect=lambda: counts.__setitem__("disconnect", counts["disconnect"] + 1),
            init_delay=init_delay, max_delay=max_delay,
            on_connect=lambda: counts.__setitem__("connect", counts["connect"] + 1),
        )

    with caplog.at_level(logging.DEBUG, logger="QTLBot"):
        asyncio.run(run())
    return delays, counts["connect"], counts["disconnect"]


def _messages(caplog, level):
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def test_drop_after_connect_logs_uptime_not_reconnect_failure(monkeypatch, caplog):
    delays, connects, disconnects = _run(
        [("drop", 432, OSError("no close frame received or sent")), ("closed", 1)], monkeypatch, caplog)
    warns = _messages(caplog, logging.WARNING)
    assert len(warns) == 1
    assert "接続時間: 7分12秒" in warns[0] and "no close frame" in warns[0]
    assert "再接続失敗" not in warns[0]
    assert (connects, disconnects) == (2, 1)       # on_disconnect は従来どおり切断ごとに呼ばれる
    assert delays == [5]                           # 再接続の待ち時間は従来どおり（初回5秒）
    assert any("0 failures" in m for m in _messages(caplog, logging.INFO))   # 切断は「失敗」に数えない


def test_connection_failures_keep_reconnect_failure_wording_and_backoff(monkeypatch, caplog):
    delays, connects, disconnects = _run(
        [("fail", OSError("HTTP 503")), ("fail", OSError("HTTP 503")), ("fail", OSError("HTTP 503")),
         ("closed", 1)], monkeypatch, caplog, init_delay=5, max_delay=15)
    warns = _messages(caplog, logging.WARNING)
    assert [("再接続失敗 #%d" % n) in w for n, w in enumerate(warns, 1)] == [True, True, True]
    assert all("接続失敗" in w for w in warns)
    assert delays == [5, 10, 15]                   # 指数バックオフは従来どおり（上限あり）
    assert (connects, disconnects) == (1, 3)


def test_failure_count_resets_after_successful_connect(monkeypatch, caplog):
    _run([("fail", OSError("x")), ("drop", 60, OSError("x")), ("fail", OSError("x")), ("closed", 1)],
         monkeypatch, caplog)
    warns = _messages(caplog, logging.WARNING)
    assert "再接続失敗 #1" in warns[0]
    assert "接続時間: 1分0秒" in warns[1]
    assert "再接続失敗 #1" in warns[2]             # 接続成功を挟んだので #2 ではなく #1 から数え直し


def test_graceful_close_is_info_and_not_a_disconnect(monkeypatch, caplog):
    delays, connects, disconnects = _run([("closed", 45), ("closed", 1)], monkeypatch, caplog)
    assert not _messages(caplog, logging.WARNING)
    assert any("WebSocket が閉じられました（接続時間: 45秒）" in m for m in _messages(caplog, logging.INFO))
    assert disconnects == 0 and delays == [5]


def test_cancelled_error_propagates(monkeypatch, caplog):
    with pytest.raises(asyncio.CancelledError):
        _run([("drop", 1, asyncio.CancelledError())], monkeypatch, caplog)


@pytest.mark.parametrize("seconds, expected", [
    (0, "0秒"), (45, "45秒"), (59.9, "59秒"), (60, "1分0秒"), (432, "7分12秒"),
    (3600, "1時間0分"), (3725, "1時間2分"), (-5, "0秒"),
])
def test_format_duration(seconds, expected):
    assert ws_helpers.format_duration(seconds) == expected
