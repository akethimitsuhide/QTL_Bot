"""
tests/test_eew_wave_integration.py
===================================
EewCog.notify_eew が、EEW_WAVE_CIRCLE_ENABLE=true のとき全EEW画像の描画に wave_overlay を渡し、
描けない場合（無効・PLUM法・走時表なし）は None を渡すことの確認。描画関数・送信はダミー。
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest  # noqa: E402

import cogs.eew as eew_mod  # noqa: E402
from _wave_table import write_table  # noqa: E402
from core import travel_time  # noqa: E402
from core.eew_wave import JST  # noqa: E402

PNG = b"\x89PNG-fake"


def _fmt(dt):
    return dt.strftime("%Y/%m/%d %H:%M:%S")


def _report(event="E1", serial=1, plum=False, origin=None, **kw):
    origin = origin or (datetime.now(JST) - timedelta(seconds=20))
    d = {
        "EventID": event, "Serial": serial, "isCancel": False, "isFinal": False, "isWarn": True,
        "isAssumption": plum, "Hypocenter": "三陸沖", "Latitude": 38.0, "Longitude": 142.0,
        "Magnitude": 6.0, "Depth": 10, "MaxIntensity": "5弱", "OriginTime": _fmt(origin),
        "AnnouncedTime": _fmt(origin + timedelta(seconds=8)),
        "WarnArea": [{"Chiiki": "宮城県北部", "Shindo1": "5弱", "Shindo2": "4", "Type": "警報"}],
    }
    d.update(kw)
    return d


@pytest.fixture
def cog(monkeypatch, tmp_path):
    travel_time.reset_for_tests()
    c = object.__new__(eew_mod.EewCog)
    c.sent, c.render_calls = [], []
    c.eew_channel = c.p2p_eew_channel = c.channel = object()
    c.recent_eews, c.recent_eews_max_size = {}, 50
    c.eew_state, c._eew_state_max_size = {}, 50
    c._last_eew_report = {}
    c.monitored_event_id = None
    c.eew_monitor_timeout_task = None
    c.last_eew_data = None

    async def noop(*a, **k):
        return None

    c.speak_local = noop
    c.play_eew_sound = noop
    c.generate_and_speak_eew = noop

    async def fake_send(channel, **kw):
        c.sent.append(kw)

    monkeypatch.setattr(eew_mod, "send_with_retry", fake_send)
    monkeypatch.setattr(eew_mod, "render_shindo_map", lambda **kw: c.render_calls.append(("shindo", kw)) or PNG)
    monkeypatch.setattr(eew_mod, "render_eew_warn_map", lambda **kw: c.render_calls.append(("warn", kw)) or PNG)
    monkeypatch.setattr(eew_mod, "record_delivery", lambda *a, **k: None)
    monkeypatch.setattr(eew_mod, "record_notification", lambda *a, **k: None)
    monkeypatch.setattr(eew_mod, "ENABLE_KYOSHIN", False)
    monkeypatch.setattr(eew_mod.rs, "get", lambda key, *a, **k: True, raising=False)
    monkeypatch.setattr(eew_mod, "EEW_WAVE_CIRCLE_ENABLE", True)
    # .env の値（EEW_WAVE_CIRCLE_TIME_BASE=announce など）に左右されないよう、既定値に固定する
    monkeypatch.setattr(eew_mod, "EEW_WAVE_CIRCLE_TIME_BASE", "render")
    monkeypatch.setattr(eew_mod, "EEW_WAVE_CIRCLE_MAX_ELAPSED_SEC", 180.0)
    monkeypatch.setattr(eew_mod, "EEW_WAVE_TABLE_FILE", write_table(tmp_path / "table"))
    yield c
    travel_time.reset_for_tests()


def _run(c, data, **kw):
    c.render_calls.clear()
    c.sent.clear()
    asyncio.run(c.notify_eew(data, **kw))


def test_overlay_is_passed_to_every_eew_map(cog):
    _run(cog, _report())
    assert cog.render_calls, "地図が描画されていない"
    for _kind, kw in cog.render_calls:
        ov = kw["wave_overlay"]
        assert ov is not None and {k for k, _ in ov.circles} == {"P", "S"}


def test_disabled_passes_none(cog, monkeypatch):
    monkeypatch.setattr(eew_mod, "EEW_WAVE_CIRCLE_ENABLE", False)
    _run(cog, _report())
    assert cog.render_calls and all(kw["wave_overlay"] is None for _k, kw in cog.render_calls)


def test_plum_passes_none(cog):
    _run(cog, _report(plum=True))
    assert cog.render_calls and all(kw["wave_overlay"] is None for _k, kw in cog.render_calls)


def test_missing_table_passes_none_and_still_notifies(cog, monkeypatch, tmp_path):
    monkeypatch.setattr(eew_mod, "EEW_WAVE_TABLE_FILE", str(tmp_path / "missing"))
    _run(cog, _report())
    assert cog.sent and cog.render_calls
    assert all(kw["wave_overlay"] is None for _k, kw in cog.render_calls)


def test_old_origin_time_passes_none_but_test_notification_draws(cog):
    old = _report(origin=datetime.now(JST) - timedelta(days=30))
    _run(cog, old)
    assert all(kw["wave_overlay"] is None for _k, kw in cog.render_calls)
    _run(cog, old, is_test=True)
    assert cog.render_calls and all(kw["wave_overlay"] is not None for _k, kw in cog.render_calls)


def test_computation_error_does_not_stop_the_notification(cog, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(eew_mod.EewCog, "_build_wave_overlay", staticmethod(boom))
    _run(cog, _report())
    assert cog.sent and all(kw["wave_overlay"] is None for _k, kw in cog.render_calls)


def test_announce_basis_fresh_draws_and_stale_does_not(cog, monkeypatch):
    monkeypatch.setattr(eew_mod, "EEW_WAVE_CIRCLE_TIME_BASE", "announce")
    _run(cog, _report())
    assert cog.render_calls and all(kw["wave_overlay"] is not None for _k, kw in cog.render_calls)
    assert all(kw["wave_overlay"].caption.endswith("+8s") for _k, kw in cog.render_calls)
    _run(cog, _report(origin=datetime.now(JST) - timedelta(days=30)))   # 再送・再生された古いEEW
    assert cog.render_calls and all(kw["wave_overlay"] is None for _k, kw in cog.render_calls)
