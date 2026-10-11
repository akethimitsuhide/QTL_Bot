"""core/status_history_view.py（/qtl_status 履歴オプションの表示整形）のテスト（2026-10-12追加）。"""
from core.constants import INT_MAP
from core.status_history_view import (
    format_eew_line, format_quake_line, join_lines,
)


def test_quake_line_normal():
    rec = {"time_display": "2026年10月1日8時20分頃", "hypocenter_name": "石川県能登地方",
           "max_scale": 45, "magnitude": 4.5, "depth": 10, "title": "震度・震源に関する情報"}
    line = format_quake_line(rec, INT_MAP)
    assert "石川県能登地方" in line and "最大震度5弱" in line
    assert "M4.5" in line and "約10km" in line and "震度・震源に関する情報" in line


def test_quake_line_unknown_values():
    rec = {"time_display": "不明", "hypocenter_name": "調査中",
           "max_scale": None, "magnitude": -1, "depth": -1}
    line = format_quake_line(rec, INT_MAP)
    assert "最大震度不明" in line and "M調査中" in line and "深さ調査中" in line


def test_eew_line_normal_and_final():
    rec = {"time_display": "t", "hypocenter_name": "三陸沖", "max_intensity_label": "4",
           "magnitude": 6.1, "depth": 20, "serial": 3, "is_final": True}
    line = format_eew_line(rec)
    assert "最終報" in line and "予想最大震度4" in line and "M6.1" in line


def test_eew_line_plum_has_no_hypocenter_elements():
    rec = {"time_display": "t", "hypocenter_name": "不明", "max_intensity_label": "5弱",
           "is_assumption": True, "serial": 1}
    line = format_eew_line(rec)
    assert "PLUM法" in line and "第1報" in line and "M" not in line.replace("PLUM", "")


def test_join_lines_empty_and_truncate():
    assert join_lines([]) == "履歴がありません。"
    long = ["x" * 500] * 20
    out = join_lines(long)
    assert len(out) <= 4000 and out.endswith("（長いため一部省略）")
