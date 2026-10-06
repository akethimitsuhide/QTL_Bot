"""
tests/test_eew_forecast_label.py
=================================
EEW「地域ごとの予想震度」の見出し（core/eew_convert.py）のテスト（2026-10-05追加）。

Wolfx API仕様: WarnArea[].Shindo1＝地域の最大震度、Shindo2＝地域の最小震度。
PLUM法では Shindo2 は null/None。見出しは「震度{Shindo1}〜{Shindo2}程度」、
同値または Shindo2 が未設定なら「震度{Shindo1}程度」。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core.constants import INT_MAP  # noqa: E402
from core.eew_convert import (  # noqa: E402
    build_forecast_groups, build_region_shindo_map, convert_p2p_eew_to_wolfx,
    format_forecast_section,
)


def _a(name, s1, s2):
    return {"Chiiki": name, "Shindo1": s1, "Shindo2": s2}


def test_same_value_or_unset_shindo2_is_single_label():
    for s2 in ("4", None, "null", "None", "", "不明"):
        assert build_forecast_groups([_a("千葉県北東部", "4", s2)], INT_MAP) == {"震度4程度": ["千葉県北東部"]}


def test_different_values_follow_wolfx_order_shindo1_to_shindo2():
    g = build_forecast_groups([_a("A", "5強", "4"), _a("B", "6弱", "5弱")], INT_MAP)
    assert g == {"震度5強〜4程度": ["A"], "震度6弱〜5弱程度": ["B"]}


def test_plum_uses_shindo1_only():
    g = build_forecast_groups([_a("A", "5弱", None), _a("B", "4", "3")], INT_MAP, is_assumption=True)
    assert g == {"震度5弱程度": ["A"], "震度4程度": ["B"]}


def test_open_upper_in_shindo1_shows_lower_bound():
    g = build_forecast_groups([_a("A", "以上", "5弱"), _a("B", "over", "4"), _a("C", "以上", None)], INT_MAP)
    assert g == {"震度5弱以上": ["A"], "震度4以上": ["B"]}


def test_section_text_and_order():
    text = format_forecast_section(
        build_forecast_groups([_a("千葉県北東部", "4", None), _a("茨城県南部", "5弱", "4")], INT_MAP), INT_MAP)
    assert text.index("震度5弱〜4程度") < text.index("震度4程度")
    assert "■ 震度4程度\n　千葉県北東部" in text


def test_region_shindo_map_uses_max_and_handles_none():
    m = build_region_shindo_map([_a("A", "5強", "4"), _a("B", "3", None), _a("C", "以上", "5弱"),
                                 _a("D", None, None)], INT_MAP)
    assert m == {"A": 50, "B": 30, "C": 45}


def test_p2p_conversion_outputs_wolfx_semantics():
    base = {"issue": {"eventId": "1", "serial": "1"},
            "earthquake": {"hypocenter": {"name": "x", "depth": 10, "magnitude": 5.0}, "originTime": ""}}
    areas = [{"name": "A", "scaleFrom": 40, "scaleTo": 45, "kindCode": "10"},
             {"name": "B", "scaleFrom": 45, "scaleTo": 99, "kindCode": "10"},
             {"name": "C", "scaleFrom": 30, "scaleTo": 30, "kindCode": "10"}]
    out = convert_p2p_eew_to_wolfx({**base, "areas": areas})
    wa = {w["Chiiki"]: (w["Shindo1"], w["Shindo2"]) for w in out["WarnArea"]}
    assert wa == {"A": ("5弱", "4"), "B": ("以上", "5弱"), "C": ("3", "3")}
    plum = convert_p2p_eew_to_wolfx({**base, "earthquake": {**base["earthquake"], "condition": "仮定震源要素"},
                                     "areas": areas})
    # PLUM法: Shindo2 は常に None。Shindo1 は scaleTo（最大震度。上限なし99・不明は scaleFrom）
    assert {w["Chiiki"]: (w["Shindo1"], w["Shindo2"]) for w in plum["WarnArea"]} == {
        "A": ("5弱", None), "B": ("5弱", None), "C": ("3", None)}


def test_plum_area_value_matches_overall_max_intensity():
    base = {"issue": {"eventId": "1", "serial": "1"},
            "earthquake": {"hypocenter": {"name": "x", "depth": 10, "magnitude": 1}, "originTime": "",
                           "condition": "仮定震源要素"}}
    for sf, st, expect in ((45, 50, "5強"), (50, 99, "5強"), (55, 99, "6弱"), (45, -1, "5弱"), (-1, 50, "5強")):
        out = convert_p2p_eew_to_wolfx({**base, "areas": [{"name": "A", "scaleFrom": sf, "scaleTo": st,
                                                           "kindCode": "19"}]})
        assert out["WarnArea"][0]["Shindo1"] == expect and out["WarnArea"][0]["Shindo2"] is None
        assert out["MaxIntensity"] == expect                      # 全体の最大震度と地域別が一致する
        g = build_forecast_groups(out["WarnArea"], INT_MAP, is_assumption=True)
        assert g == {f"震度{expect}程度": ["A"]}
