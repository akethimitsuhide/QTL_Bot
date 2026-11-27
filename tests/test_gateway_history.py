"""
tests/test_gateway_history.py
==============================
Discord ゲートウェイの切断・復旧の履歴（core/gateway_history.py）と、
SystemCog の on_disconnect / on_resumed / on_ready での記録を確認する。
"""
import asyncio
import os
from datetime import datetime, timedelta

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

from core.gateway_history import GATEWAY_HISTORY_MAX, GatewayHistory

T0 = datetime(2026, 11, 27, 5, 0, 0)


def _at(sec):
    return T0 + timedelta(seconds=sec)


def test_initial_state_is_connected_with_no_events():
    s = GatewayHistory(T0).snapshot(_at(60))
    assert s["connected"] is True and s["events"] == []
    assert s["total_disconnects"] == 0 and s["state_duration_sec"] == 60.0


def test_disconnect_then_recover_records_duration():
    g = GatewayHistory(T0)
    g.on_disconnect(_at(100))
    ongoing = g.snapshot(_at(160))
    assert ongoing["connected"] is False
    assert ongoing["events"][0] == {"start": _at(100).isoformat(), "end": None, "duration_sec": 60.0}
    assert ongoing["total_downtime_sec"] == 60.0 and ongoing["longest_sec"] == 60.0

    g.on_connected(_at(400))
    s = g.snapshot(_at(460))
    assert s["connected"] is True and s["state_duration_sec"] == 60.0
    assert s["events"] == [{"start": _at(100).isoformat(), "end": _at(400).isoformat(), "duration_sec": 300.0}]
    assert (s["total_disconnects"], s["total_downtime_sec"], s["longest_sec"]) == (1, 300.0, 300.0)


def test_repeated_disconnect_events_count_once():
    g = GatewayHistory(T0)
    g.on_disconnect(_at(10))
    g.on_disconnect(_at(20))           # 切断中の重複通知
    g.on_disconnect(_at(30))
    g.on_connected(_at(70))
    s = g.snapshot(_at(80))
    assert s["total_disconnects"] == 1 and s["events"][0]["duration_sec"] == 60.0   # 最初の切断時刻から


def test_connected_without_disconnect_is_ignored():
    g = GatewayHistory(T0)
    g.on_connected(_at(5))             # 起動時の on_ready など
    s = g.snapshot(_at(10))
    assert s["events"] == [] and s["state_duration_sec"] == 10.0


def test_events_are_newest_first_and_bounded():
    g = GatewayHistory(T0, max_events=3)
    for i in range(5):
        g.on_disconnect(_at(i * 100))
        g.on_connected(_at(i * 100 + 10 * (i + 1)))
    s = g.snapshot(_at(1000))
    assert [e["duration_sec"] for e in s["events"]] == [50.0, 40.0, 30.0]      # 新しい順・直近3件
    assert s["total_disconnects"] == 5 and s["longest_sec"] == 50.0           # 累計は上限の影響を受けない
    assert GATEWAY_HISTORY_MAX >= 10


def test_snapshot_limit():
    g = GatewayHistory(T0)
    for i in range(6):
        g.on_disconnect(_at(i * 100))
        g.on_connected(_at(i * 100 + 5))
    assert len(g.snapshot(_at(1000), limit=4)["events"]) == 4


# ---------- SystemCog のリスナー ----------
from cogs.system import SystemCog  # noqa: E402


def _cog():
    cog = object.__new__(SystemCog)
    cog._gateway_history = GatewayHistory(datetime.now())
    cog._pending_errors = __import__("core.error_notify_queue", fromlist=["x"]).PendingErrorQueue()
    cog._pending_flush_lock = asyncio.Lock()
    cog.admin_channel = None
    return cog


def test_cog_listeners_record_disconnect_and_resume():
    cog = _cog()
    asyncio.run(cog.on_disconnect())
    assert cog._gateway_history.snapshot(datetime.now())["connected"] is False
    asyncio.run(cog.on_resumed())
    s = cog._gateway_history.snapshot(datetime.now())
    assert s["connected"] is True and s["total_disconnects"] == 1 and len(s["events"]) == 1


def test_on_resumed_stays_small_and_on_ready_keeps_startup_work():
    """回帰: on_resumed が on_ready の本体（起動処理）を巻き込まないこと。"""
    import inspect
    resumed = inspect.getsource(SystemCog.on_resumed)
    ready = inspect.getsource(SystemCog.on_ready)
    for marker in ("_startup_tasks_done", "tree.sync", "_notify_startup", "status_history_recorder"):
        assert marker in ready and marker not in resumed
    assert "_flush_pending_errors" in ready and "_flush_pending_errors" in resumed
