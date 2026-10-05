"""
tests/test_eew_region_resolve.py
=================================
EEWの地域名の解決（core/eew_convert.py の resolve_chiiki / extract_alert_regions /
build_region_shindo_map）と、GIS地図の区域名の読み替え（core/gis_render.py）の
テスト（2026-10-03追加）。

不具合: eew_sample5.json を --test_auto で実行すると、REGION_MAP に無い地域名
「秋田県南部」が「その他」と読み上げ・表示され、地図でも塗られなかった。また
GeoJSON側の区域名と REGION_MAP の不一致（「奄美(群島)」と「奄美群島」、「釧路地方中南」と
「釧路地方中南部」）により、該当地域が警報・震度の対象でも地図で塗られなかった。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core.constants import INT_MAP, REGION_MAP  # noqa: E402
from core.eew_convert import (  # noqa: E402
    build_region_shindo_map, extract_alert_regions, resolve_chiiki,
)

KNOWN = {"秋田県沿岸北部": "秋田", "秋田県沿岸南部": "秋田", "秋田県内陸北部": "秋田", "秋田県内陸南部": "秋田",
         "岩手県沿岸南部": "岩手", "岩手県内陸南部": "岩手", "北海道道央": "x", "千葉県北東部": "千葉"}


def test_resolve_exact_match_is_returned_as_is():
    assert resolve_chiiki("千葉県北東部", KNOWN) == ["千葉県北東部"]


def test_resolve_akita_south_to_both_south_areas():
    assert resolve_chiiki("秋田県南部", KNOWN) == ["秋田県内陸南部", "秋田県沿岸南部"]


def test_resolve_is_conservative():
    assert resolve_chiiki("", KNOWN) == []
    assert resolve_chiiki("存在しない県南部", KNOWN) == []          # 同じ都道府県の既知の区域が無い
    assert resolve_chiiki("秋田県", KNOWN) == []                    # 都道府県名を除いた部分が短すぎる
    assert resolve_chiiki("秋田県西部", KNOWN) == []                # 末尾が一致する区域が無い
    many = {f"秋田県A{i}部": "秋田" for i in range(6)}
    assert resolve_chiiki("秋田県部", {**many}) == []              # 候補が多すぎる（曖昧）は解決しない
    assert resolve_chiiki("秋田県南部", ["秋田県沿岸南部"]) == ["秋田県沿岸南部"]   # list/tupleのknown_namesも可


def _eew(*names, typ="警報"):
    return {"WarnArea": [{"Chiiki": n, "Type": typ, "Shindo1": "4", "Shindo2": "4"} for n in names]}


def test_extract_alert_regions_resolves_unknown_name_to_its_region():
    rm = {"秋田県沿岸南部": "秋田", "秋田県内陸南部": "秋田", "千葉県北東部": "千葉"}
    assert extract_alert_regions(_eew("秋田県南部"), rm) == {"秋田"}        # 「その他」にならない
    assert extract_alert_regions(_eew("千葉県北東部", "秋田県南部"), rm) == {"千葉", "秋田"}


def test_extract_alert_regions_falls_back_to_other_when_ambiguous_across_regions():
    rm = {"X県北部": "A", "X県中部": "B", "X県南部": "C"}
    assert extract_alert_regions(_eew("X県部"), rm) == {"その他"}           # 複数の府県予報区に跨る→曖昧
    assert extract_alert_regions(_eew("まったく未知"), rm) == {"その他"}


def test_extract_alert_regions_ignores_non_alert_types():
    rm = {"千葉県北東部": "千葉"}
    assert extract_alert_regions(_eew("千葉県北東部", typ="予報"), rm) == set()


def test_build_region_shindo_map_expands_unknown_names():
    wa = [{"Chiiki": "秋田県南部", "Shindo1": "4", "Shindo2": "4"},
          {"Chiiki": "秋田県内陸南部", "Shindo1": "5強", "Shindo2": "5強"},      # 同じ区域に別の震度 → 高い方
          {"Chiiki": "未知の地域", "Shindo1": "3", "Shindo2": "3"}]
    r = build_region_shindo_map(wa, INT_MAP, known_names=KNOWN)
    assert r["秋田県沿岸南部"] == 40 and r["秋田県内陸南部"] == 50
    assert r["未知の地域"] == 30                                             # 解決できない名称は従来どおり残る
    assert "秋田県南部" not in r
    # known_names を渡さなければ従来どおり（後方互換）
    assert build_region_shindo_map(wa[:1], INT_MAP) == {"秋田県南部": 40}


# ---------- 実データ（eew_sample5相当）での回帰 ----------
def test_region_map_keys_cover_polygon_names_and_values_cover_eew_areas():
    """GISデータがある環境で、REGION_MAP側の名称とGeoJSON側の区域名が一致していること。"""
    os.environ.setdefault("GIS_MAP_ENABLE", "true")
    from core import gis_render as g
    if not g._ready() or not g._local_areas.get():
        pytest.skip("data/gis のGeoJSONが未取得のためスキップ")
    loc, eew = g._local_areas.get(), g._eew_areas.get()
    assert "釧路地方中南部" in loc and "釧路地方中南" not in loc
    assert "奄美群島" in eew and "奄美(群島)" not in eew
    assert [v for v in set(REGION_MAP.values()) if v not in eew] == []          # 府県予報区名はすべて地図に描ける
    assert [k for k in REGION_MAP if k not in loc] == []                        # 細分区域名はすべて地図に描ける


def test_sample_with_unknown_area_has_no_other_and_everything_is_paintable():
    os.environ.setdefault("GIS_MAP_ENABLE", "true")
    from core import gis_render as g
    if not g._ready() or not g._local_areas.get():
        pytest.skip("data/gis のGeoJSONが未取得のためスキップ")
    names = list(REGION_MAP)[:60] + ["秋田県南部"]
    data = _eew(*names)
    assert "その他" not in extract_alert_regions(data, REGION_MAP)
    painted = build_region_shindo_map(data["WarnArea"], INT_MAP, known_names=REGION_MAP)
    assert all(n in g._local_areas.get() for n in painted)
