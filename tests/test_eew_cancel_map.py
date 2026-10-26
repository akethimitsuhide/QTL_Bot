"""EEWキャンセル報の地図描画（直前の報の震源要素・塗りつぶしなし）のテスト（2026-10-27追加）。"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest  # noqa: E402

import cogs.eew as eew_mod  # noqa: E402

PNG = b"\x89PNG-fake"


def _report(event="E1", serial=1, lat=38.0, lon=142.0, plum=False, **kw):
    d = {
        "EventID": event, "Serial": serial, "isCancel": False, "isFinal": False, "isWarn": False,
        "isAssumption": plum, "Hypocenter": "三陸沖", "Latitude": lat, "Longitude": lon,
        "Magnitude": 5.0, "Depth": 10, "MaxIntensity": "3", "OriginTime": "2026/10/27 10:00:00",
        "WarnArea": [{"Chiiki": "宮城県北部", "Shindo1": "3", "Shindo2": "3", "Type": "予報"}],
    }
    d.update(kw)
    return d


def _cancel(event="E1", serial=2):
    # P2P のキャンセル報は震源などを含まない最小形
    return {"EventID": event, "Serial": serial, "isCancel": True, "OriginTime": "不明"}


@pytest.fixture
def cog(monkeypatch):
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

    def fake_shindo_map(**kw):
        c.render_calls.append(("shindo", kw))
        return PNG

    def fake_warn_map(**kw):
        c.render_calls.append(("warn", kw))
        return PNG

    monkeypatch.setattr(eew_mod, "send_with_retry", fake_send)
    monkeypatch.setattr(eew_mod, "render_shindo_map", fake_shindo_map)
    monkeypatch.setattr(eew_mod, "render_eew_warn_map", fake_warn_map)
    monkeypatch.setattr(eew_mod, "record_delivery", lambda *a, **k: None)
    monkeypatch.setattr(eew_mod, "record_notification", lambda *a, **k: None)
    monkeypatch.setattr(eew_mod, "ENABLE_KYOSHIN", False)
    monkeypatch.setattr(eew_mod.rs, "get", lambda key, *a, **k: True, raising=False)
    return c


def _run(c, data):
    c.render_calls.clear()
    c.sent.clear()
    asyncio.run(c.notify_eew(data))


def test_cancel_draws_hypocenter_only_map_from_last_report(cog):
    _run(cog, _report(serial=1, lat=38.0, lon=142.0))
    _run(cog, _report(serial=2, lat=38.5, lon=142.5, plum=True))     # 直前（最後）の報
    _run(cog, _cancel())

    assert [k for k, _ in cog.render_calls] == ["shindo"]            # 警報地域図は描かない
    kw = cog.render_calls[0][1]
    assert kw["region_shindo"] is None                               # 地域ごとの震度の塗りつぶしなし
    assert kw["hypocenter_lonlat"] == (142.5, 38.5)                  # 最後の報の震源
    assert kw["is_plum"] is True
    assert "files" in cog.sent[0]                                    # 画像が添付される


def test_cancel_after_warning_report_has_no_area_fill(cog):
    _run(cog, _report(isWarn=True))
    _run(cog, _cancel())
    assert [k for k, _ in cog.render_calls] == ["shindo"]            # 警報の発表地域図(warn)は描かない
    assert cog.render_calls[0][1]["region_shindo"] is None


def test_cancel_without_previous_report_sends_without_map(cog):
    _run(cog, _cancel(event="UNKNOWN"))
    assert cog.render_calls == []
    assert "files" not in cog.sent[0] and "キャンセルされました" in cog.sent[0]["embed"].description


def test_cancel_when_last_report_has_no_coordinates(cog):
    _run(cog, _report(lat=-200, lon=-200))
    _run(cog, _cancel())
    assert cog.render_calls == []
    assert "files" not in cog.sent[0]


def test_other_event_is_not_used(cog):
    _run(cog, _report(event="E1", lat=38.0, lon=142.0))
    _run(cog, _report(event="E2", lat=35.0, lon=139.0))
    _run(cog, _cancel(event="E1"))
    assert cog.render_calls[0][1]["hypocenter_lonlat"] == (142.0, 38.0)


def test_survives_beyond_recent_eews_ttl(cog):
    """recent_eews（300秒）が切れた後にキャンセル報が届いても震源を描ける。"""
    _run(cog, _report(lat=38.0, lon=142.0))
    t = time.time() - 600
    cog.recent_eews = {k: (d, t) for k, (d, _) in cog.recent_eews.items()}
    cog._last_eew_report["E1"]["at"] = time.time() - 600
    _run(cog, _cancel())
    assert cog.render_calls and cog.render_calls[0][1]["hypocenter_lonlat"] == (142.0, 38.0)


def test_render_failure_still_sends_cancel_notice(cog, monkeypatch):
    monkeypatch.setattr(eew_mod, "render_shindo_map", lambda **kw: None)   # GIS無効・データ未取得
    _run(cog, _report())
    _run(cog, _cancel())
    assert "files" not in cog.sent[0] and "キャンセルされました" in cog.sent[0]["embed"].description


def test_out_of_order_serial_does_not_overwrite(cog):
    now = time.time()
    cog._remember_last_eew_report("E1", _report(serial=3, lat=38.0, lon=142.0), 3, now)
    cog._remember_last_eew_report("E1", _report(serial=2, lat=10.0, lon=100.0), 2, now + 1)
    assert cog._last_eew_report["E1"]["lonlat"] == (142.0, 38.0)


def test_store_is_bounded_and_expires(cog):
    now = time.time()
    for i in range(80):
        cog._remember_last_eew_report(f"E{i}", _report(event=f"E{i}"), 1, now + i)
    assert len(cog._last_eew_report) <= cog.recent_eews_max_size
    cog._remember_last_eew_report("NEW", _report(event="NEW"), 1, now + 100 + cog._LAST_REPORT_TTL_SEC)
    assert list(cog._last_eew_report) == ["NEW"]                     # 期限切れは破棄
