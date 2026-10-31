"""
tests/test_travel_time.py
==========================
core/travel_time.py（走時表の読み込みと「経過秒数 → 震央距離」の逆引き）のテスト。
走時表は tests/_wave_table.py の合成ファイルを使う（実ファイルに依存しない）。
"""
import logging
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _wave_table import VP, VS, write_table  # noqa: E402
from core import travel_time as tt  # noqa: E402


@pytest.fixture()
def table(tmp_path):
    return tt.TravelTimeTable.from_file(write_table(tmp_path / "tjma2001h.+00000"))


def test_parse_line_accepts_real_file_layout():
    assert tt._parse_line("P    0.416 S    0.703   0      2") == (0.0, 2.0, 0.416, 0.703)


@pytest.mark.parametrize("line", [
    "", "   ", "garbage", "P 1.0 S", "P x S 2.0 0 2", "Q 1.0 S 2.0 0 2",
    "P -1.0 S 2.0 0 2", "P 1.0 S 2.0 -5 2", "P nan S 2.0 0 2", "P inf S 2.0 0 2",
])
def test_parse_line_skips_malformed(line):
    assert tt._parse_line(line) is None


def test_from_file_ranges_and_malformed_lines_skipped(tmp_path):
    path = write_table(tmp_path / "t")
    with open(path, "a", encoding="utf-8") as f:
        f.write("broken line\n\nP 1 S\n")
    t = tt.TravelTimeTable.from_file(path)
    assert t.depth_range == (0.0, 100.0)
    assert t.distance_range() == (0.0, 2000.0)
    assert t.depth_count == 5


def test_from_file_without_valid_rows_raises(tmp_path):
    p = tmp_path / "empty"
    p.write_text("nothing here\n", encoding="utf-8")
    with pytest.raises(ValueError):
        tt.TravelTimeTable.from_file(str(p))


def test_duplicate_rows_later_wins(tmp_path):
    p = tmp_path / "dup"
    p.write_text("P 1.0 S 2.0 0 10\nP 3.0 S 4.0 0 10\nP 0.0 S 0.0 0 0\nP 9.0 S 9.0 0 20\n", encoding="utf-8")
    t = tt.TravelTimeTable.from_file(str(p))
    assert t.travel_time(0, 10, "P") == 3.0


@pytest.mark.parametrize("depth,dist", [(10, 100), (10, 500), (0, 30), (50, 1000), (100, 1900)])
def test_radius_matches_travel_time_on_grid_depths(table, depth, dist):
    t = math.hypot(depth, dist) / VP
    assert table.radius_km(depth, t, "P") == pytest.approx(dist, abs=0.6)
    ts = math.hypot(depth, dist) / VS
    assert table.radius_km(depth, ts, "S") == pytest.approx(dist, abs=0.6)


def test_s_wave_is_always_behind_p_wave(table):
    for sec in (5, 10, 20, 40, 80, 160):
        p, s = table.radius_km(10, sec, "P"), table.radius_km(10, sec, "S")
        assert p is not None and s is not None and s < p


def test_depth_is_interpolated_between_rows(table):
    depth, dist = 15, 120
    t = math.hypot(depth, dist) / VP
    assert table.radius_km(depth, t, "P") == pytest.approx(dist, abs=1.0)


def test_none_before_wave_reaches_surface(table):
    assert table.radius_km(50, 5.0, "P") is None          # 直下の地表まで 50/6=8.3秒
    assert table.radius_km(50, 8.0, "P") is None
    assert table.radius_km(50, 9.0, "P") is not None


def test_none_when_radius_too_small(table):
    assert table.radius_km(0, 0.01, "P") is None          # 半径 0.06km < MIN_RADIUS_KM


def test_just_inside_table_end_is_still_drawn(table):
    t = math.hypot(100, 2000) / VP - 0.5               # 表の最大走時の直前
    r = table.radius_km(100, t, "P")
    assert r is not None and r == pytest.approx(2000, abs=5.0)


def test_none_beyond_table_range(table):
    assert table.radius_km(10, 10_000.0, "P") is None     # 震央距離 2000km 超
    assert table.radius_km(150, 30.0, "P") is None        # 深さが表の範囲外
    assert table.radius_km(-1, 30.0, "P") is None


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf")])
def test_none_for_invalid_inputs(table, bad):
    assert table.radius_km(10, bad, "P") is None
    assert table.radius_km(bad, 10.0, "P") is None


def test_travel_time_function(table):
    assert table.travel_time(10, 100, "P") == pytest.approx(math.hypot(10, 100) / VP, abs=0.01)
    assert table.travel_time(10, 5000, "P") is None
    assert table.travel_time(500, 10, "P") is None


def test_rows_with_different_distance_grids_do_not_break(tmp_path):
    p = tmp_path / "irregular"
    p.write_text(
        "P 0.0 S 0.0 0 0\nP 2.0 S 3.0 0 10\nP 4.0 S 6.0 0 20\n"
        "P 1.0 S 1.5 10 0\nP 4.5 S 6.5 10 20\n", encoding="utf-8")
    t = tt.TravelTimeTable.from_file(str(p))
    r = t.radius_km(5, 3.0, "P")           # 深さ5km（2行の中間）
    assert r is not None and 0 < r < 20


def test_non_monotonic_curve_uses_outermost_crossing(tmp_path):
    p = tmp_path / "nonmono"
    p.write_text("P 0.0 S 0.0 0 0\nP 5.0 S 5.0 0 10\nP 4.0 S 4.0 0 20\nP 9.0 S 9.0 0 30\n", encoding="utf-8")
    t = tt.TravelTimeTable.from_file(str(p))
    assert t.radius_km(0, 4.5, "P") == pytest.approx(20 + (4.5 - 4.0) / (9.0 - 4.0) * 10, abs=1e-6)


def test_get_table_missing_file_warns_once(tmp_path, caplog):
    tt.reset_for_tests()
    missing = str(tmp_path / "nope")
    with caplog.at_level(logging.WARNING, logger="QTLBot"):
        assert tt.get_table(missing) is None
        assert tt.get_table(missing) is None
    assert sum("走時表を読み込めない" in r.message for r in caplog.records) == 1
    tt.reset_for_tests()


def test_get_table_loads_once_and_caches(tmp_path):
    tt.reset_for_tests()
    path = write_table(tmp_path / "t")
    first = tt.get_table(path)
    assert first is not None and tt.get_table(path) is first
    tt.reset_for_tests()
