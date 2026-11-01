"""
tests/test_eew_wave.py
=======================
core/eew_wave.py（EEW地図のP波・S波の円の材料づくり）のテスト。
走時表は tests/_wave_table.py の合成ファイルを使う（実ファイルに依存しない）。
"""
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _wave_table import write_table  # noqa: E402
from core import eew_wave as ew  # noqa: E402
from core.travel_time import TravelTimeTable  # noqa: E402

JST = ew.JST
ORIGIN = datetime(2026, 10, 30, 12, 0, 0, tzinfo=JST)


@pytest.fixture()
def table(tmp_path):
    return TravelTimeTable.from_file(write_table(tmp_path / "t"))


def _eew(**over):
    data = {
        "OriginTime": "2026/10/30 12:00:00", "AnnouncedTime": "2026/10/30 12:00:10",
        "Latitude": 35.5, "Longitude": 140.0, "Depth": 10, "isAssumption": False,
    }
    data.update(over)
    return data


def _haversine_km(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * ew.EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# ---------- 時刻・深さの解釈 ----------
@pytest.mark.parametrize("text", [
    "2026-10-30T12:00:00+09:00", "2026/10/30 12:00:00", "2026-10-30 12:00:00",
    "2026-10-30 12:00:00.000", "2026-10-30T03:00:00+00:00", "2026-10-30T03:00:00Z",
])
def test_parse_eew_time_formats_all_mean_the_same_instant(text):
    assert ew.parse_eew_time(text) == ORIGIN


def test_parse_eew_time_naive_is_jst():
    assert ew.parse_eew_time("2026/10/30 12:00:00").utcoffset() == timedelta(hours=9)


@pytest.mark.parametrize("bad", [None, "", "  ", "不明", "abc", 123, "2026-13-45 99:99:99"])
def test_parse_eew_time_invalid(bad):
    assert ew.parse_eew_time(bad) is None


@pytest.mark.parametrize("value,expected", [
    (10, 10.0), (0, 0.0), ("10km", 10.0), (" 30 km ", 30.0), (12.5, 12.5),
    (-1, None), ("-1", None), (None, None), ("ごく浅い", None), (True, None), (float("nan"), None),
])
def test_parse_depth_km(value, expected):
    assert ew.parse_depth_km(value) == expected


# ---------- 円周の頂点 ----------
@pytest.mark.parametrize("lon,lat,r", [(140, 35, 100), (140, 35, 1500), (150, 60, 800), (130, -30, 500), (142, 10, 2000)])
def test_circle_ring_points_are_radius_km_away(lon, lat, r):
    ring = ew.circle_ring(lon, lat, r, steps=72)
    assert len(ring) == 72
    for plon, plat in ring:
        assert _haversine_km(lon, lat, plon, plat) == pytest.approx(r, rel=0.002)


def test_circle_ring_first_point_is_due_north():
    lon, lat = ew.circle_ring(140.0, 35.0, 200.0)[0]
    assert lon == pytest.approx(140.0, abs=1e-9) and lat > 35.0


def test_circle_ring_is_continuous_across_the_dateline():
    ring = ew.circle_ring(179.5, 20.0, 500.0, steps=72)
    lons = [p[0] for p in ring]
    assert max(lons) - min(lons) < 20             # 360度飛びがない
    assert max(lons) > 180                        # 東側は 180 を超える連続値
    ring_w = ew.circle_ring(-179.5, 20.0, 500.0, steps=72)
    assert min(p[0] for p in ring_w) < -180


def test_circle_ring_latitude_is_clamped():
    assert all(abs(p[1]) <= 85.0 for p in ew.circle_ring(0, 84.9, 2000.0))


# ---------- 経過秒数 ----------
def test_elapsed_render_basis_uses_now():
    now = ORIGIN + timedelta(seconds=25)
    assert ew.elapsed_seconds(_eew(), basis="render", now=now) == 25.0


def test_elapsed_announce_basis_uses_announced_time():
    now = ORIGIN + timedelta(seconds=90)
    assert ew.elapsed_seconds(_eew(), basis="announce", now=now) == 10.0


def test_elapsed_announce_falls_back_to_render_when_missing_or_garbled():
    now = ORIGIN + timedelta(seconds=40)
    for announced in (None, "", "不明", "xx"):
        assert ew.elapsed_seconds(_eew(AnnouncedTime=announced), basis="announce", now=now) == 40.0


def test_elapsed_none_without_origin_time():
    assert ew.elapsed_seconds(_eew(OriginTime="不明")) is None
    assert ew.elapsed_seconds({}) is None


# ---------- build_wave_overlay ----------
def test_overlay_has_p_and_s_with_caption(table):
    ov = ew.build_wave_overlay(_eew(), table, now=ORIGIN + timedelta(seconds=20))
    kinds = {k: r for k, r in ov.circles}
    assert set(kinds) == {"P", "S"} and kinds["P"] > kinds["S"] > 0
    assert kinds["P"] == pytest.approx(math.sqrt((6.0 * 20) ** 2 - 10 ** 2), abs=1.0)
    assert ov.caption == "12:00:20 JST  +20s"


def test_overlay_announce_basis_caption_and_radius(table):
    ov = ew.build_wave_overlay(_eew(), table, basis="announce", now=ORIGIN + timedelta(seconds=90))
    assert ov.caption == "12:00:10 JST  +10s"
    assert dict(ov.circles)["P"] == pytest.approx(math.sqrt(60 ** 2 - 10 ** 2), abs=1.0)


def test_overlay_only_p_when_s_not_reached_yet_is_still_drawn_as_p_only(table):
    # 深さ50km: S波は 50/3.5=14.3秒で直下に到達、P波は 8.3秒。12秒時点で P のみ
    ov = ew.build_wave_overlay(_eew(Depth=50), table, now=ORIGIN + timedelta(seconds=12))
    assert [k for k, _ in ov.circles] == ["P"]


@pytest.mark.parametrize("override", [
    {"isAssumption": True},                               # PLUM法
    {"Latitude": -200, "Longitude": -200},                # 震源不明
    {"Latitude": None}, {"Longitude": "abc"},
    {"Depth": -1}, {"Depth": "ごく浅い"}, {"Depth": None},   # 深さ不明
    {"OriginTime": "不明"}, {"OriginTime": ""},
])
def test_overlay_none_for_undrawable_events(table, override):
    assert ew.build_wave_overlay(_eew(**override), table, now=ORIGIN + timedelta(seconds=20)) is None


def test_overlay_none_without_table(table):
    assert ew.build_wave_overlay(_eew(), None, now=ORIGIN + timedelta(seconds=20)) is None


def test_overlay_none_when_elapsed_exceeds_max(table):
    assert ew.build_wave_overlay(_eew(), table, max_elapsed=60, now=ORIGIN + timedelta(seconds=61)) is None
    assert ew.build_wave_overlay(_eew(), table, max_elapsed=60, now=ORIGIN + timedelta(seconds=60)) is not None


def test_overlay_clock_skew_tolerance(table):
    # OriginTime が数秒だけ「未来」→ 経過0秒扱い（直下にもまだ届かず円なし）。大きい未来は None
    assert ew.build_wave_overlay(_eew(), table, now=ORIGIN - timedelta(seconds=3)) is None
    assert ew.build_wave_overlay(_eew(Depth=0), table, now=ORIGIN - timedelta(seconds=30)) is None


def test_overlay_deep_event_has_no_circle_until_wave_reaches_surface(table):
    assert ew.build_wave_overlay(_eew(Depth=100), table, now=ORIGIN + timedelta(seconds=10)) is None
    assert ew.build_wave_overlay(_eew(Depth=100), table, now=ORIGIN + timedelta(seconds=30)) is not None


def test_overlay_test_mode_ignores_old_origin_time(table):
    old = _eew(OriginTime="2020/01/01 00:00:00", AnnouncedTime="2020/01/01 00:00:15")
    assert ew.build_wave_overlay(old, table) is None                      # 通常は古すぎて None
    ov = ew.build_wave_overlay(old, table, test_mode=True)               # テスト通知は 発表−発生 = 15秒
    assert ov is not None and ov.caption.endswith("+15s")
    no_announce = _eew(OriginTime="2020/01/01 00:00:00", AnnouncedTime="")
    ov2 = ew.build_wave_overlay(no_announce, table, test_mode=True)
    assert ov2 is not None and ov2.caption.endswith(f"+{ew.TEST_DEFAULT_ELAPSED_SEC:.0f}s")


def test_overlay_wolfx_numeric_depth_and_string_depth_equivalent(table):
    now = ORIGIN + timedelta(seconds=20)
    a = ew.build_wave_overlay(_eew(Depth=10), table, now=now)
    b = ew.build_wave_overlay(_eew(Depth="10km"), table, now=now)
    assert a == b


# ---------- 日付をまたぐ（0時前後の更新報） ----------
@pytest.mark.parametrize("origin,announced,now,expected_elapsed", [
    ("2026/10/31 23:59:50", "2026/11/01 00:00:03", datetime(2026, 11, 1, 0, 0, 10, tzinfo=JST), 20.0),   # 日付をまたぐ
    ("2026/12/31 23:59:55", "2027/01/01 00:00:02", datetime(2027, 1, 1, 0, 0, 5, tzinfo=JST), 10.0),     # 年をまたぐ
    ("2028/02/28 23:59:58", "2028/02/29 00:00:05", datetime(2028, 2, 29, 0, 0, 8, tzinfo=JST), 10.0),    # 閏日
    ("2026/10/31 23:59:50", "2026/11/01 00:00:03",
     datetime(2026, 10, 31, 15, 0, 10, tzinfo=timezone.utc), 20.0),                                      # now が UTC でも同じ
])
def test_elapsed_is_correct_across_midnight(table, origin, announced, now, expected_elapsed):
    data = _eew(OriginTime=origin, AnnouncedTime=announced)
    assert ew.elapsed_seconds(data, basis="render", now=now) == expected_elapsed
    ov = ew.build_wave_overlay(data, table, now=now)
    assert ov is not None and {k for k, _ in ov.circles} == {"P", "S"}
    assert ov.caption.endswith(f"+{expected_elapsed:.0f}s")


def test_midnight_caption_uses_jst_wall_clock_and_announce_basis(table):
    data = _eew(OriginTime="2026/10/31 23:59:50", AnnouncedTime="2026/11/01 00:00:03")
    now = datetime(2026, 10, 31, 15, 0, 10, tzinfo=timezone.utc)      # = 2026-11-01 00:00:10 JST
    assert ew.build_wave_overlay(data, table, now=now).caption == "00:00:10 JST  +20s"
    assert ew.build_wave_overlay(data, table, basis="announce", now=now).caption == "00:00:03 JST  +13s"


def test_serial_updates_straddling_midnight_each_get_their_own_elapsed(table):
    """23:59台に発生し、0時をまたいで届く複数の更新報。報ごとに OriginTime が変わっても経過秒数は連続している。"""
    elapsed = []
    for serial, (origin, sec_after_midnight) in enumerate(
            [("2026/10/31 23:59:48", -5), ("2026/10/31 23:59:48", 4), ("2026/10/31 23:59:46", 12)], start=1):
        now = datetime(2026, 11, 1, 0, 0, 0, tzinfo=JST) + timedelta(seconds=sec_after_midnight)
        ov = ew.build_wave_overlay(_eew(OriginTime=origin, Serial=serial), table, now=now)
        assert ov is not None
        elapsed.append(float(ov.caption.rsplit("+", 1)[1].rstrip("s")))
    assert elapsed == [7.0, 16.0, 26.0]
