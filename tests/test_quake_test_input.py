"""
tests/test_quake_test_input.py
==============================
2026-10-17追加: --test_quake / --test_auto が気象庁HP JSON・Quake.One(info.json) を
受け付けること、観測点名の「＊」の有無を無視した照合、代替取得の既定の取得元のテスト。
サンプルは 2026-10-16 の「カムチャツカ半島付近の地震（EEW過大予測の翌日確認用）」の電文。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core import config, test_runner as tr  # noqa: E402
from core.gis_render import _station_latlon  # noqa: E402

JMA_JSON = {
    "Control": {"Title": "震源・震度に関する情報", "PublishingOffice": "気象庁"},
    "Head": {"Title": "震源・震度情報", "ReportDateTime": "2026-10-16T12:52:00+09:00",
             "EventID": "20261016123810", "InfoType": "発表", "Serial": "1"},
    "Body": {
        "Earthquake": {
            "OriginTime": "2026-10-16T12:35:00+09:00",
            "Hypocenter": {"Area": {"Name": "カムチャツカ半島付近", "Code": "941",
                                    "Coordinate": "+50.0+158.1-160000/"}},
            "Magnitude": "6.9"},
        "Intensity": {"Observation": {"MaxInt": "1", "Pref": [{
            "Name": "北海道", "Code": "01", "MaxInt": "1", "Area": [{
                "Name": "渡島地方東部", "Code": "106", "MaxInt": "1", "City": [{
                    "Name": "函館市", "Code": "0120200", "MaxInt": "1", "IntensityStation": [{
                        "Name": "函館市新浜町＊", "Code": "0120222", "Int": "1"}]}]}]}]}},
        "Comments": {"ForecastComment": {"Text": "この地震による津波の心配はありません。", "Code": "0215"}},
    },
}
QUAKE_ONE_INFO = {
    "EventID": "20261016123810", "ReportDateTime": "2026-10-16T12:52:00+09:00",
    "OriginDateTime": "2026-10-16T12:35:00+09:00", "MaxInt": "1", "Magnitude": "6.9",
    "Hypocenter": {"Name": "カムチャツカ半島付近", "Coordinate": [158.1, 50], "Depth": -160000},
    "Comments": "この地震による津波の心配はありません。",
}
QUAKE_ONE_SMALL = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"class": "epicenter", "area": "カムチャツカ半島付近"},
     "geometry": {"type": "Point", "coordinates": [158.1, 50]}},
    {"type": "Feature", "properties": {"class": "1", "name": "渡島地方東部"},
     "geometry": {"type": "Point", "coordinates": [140.73, 41.92]}},
]}


def test_sniff_recognizes_jma_json_and_quake_one():
    assert tr.sniff_test_target(JMA_JSON) == ["quake"]
    assert tr.sniff_test_target(QUAKE_ONE_INFO) == ["quake"]


def test_sniff_does_not_confuse_long_period_or_hypocenter_update():
    lg = {"Control": {"Title": "長周期地震動に関する観測情報"}, "Head": {"Title": "長周期地震動に関する観測情報"},
          "Body": {"Earthquake": {}, "Intensity": {"Observation": {}}}}
    assert "quake" not in tr.sniff_test_target(lg)


def test_convert_jma_json():
    d = tr.convert_quake_test_data(JMA_JSON, "x.json")
    assert d["issue"]["source"] == "気象庁"
    assert d["earthquake"]["hypocenter"]["name"] == "カムチャツカ半島付近"
    assert d["points"][0]["addr"] == "函館市新浜町＊"


def test_convert_quake_one_with_sibling_small_points(tmp_path):
    (tmp_path / "info.json").write_text(json.dumps(QUAKE_ONE_INFO), encoding="utf-8")
    (tmp_path / "smallScalePoints.json").write_text(json.dumps(QUAKE_ONE_SMALL), encoding="utf-8")
    d = tr.convert_quake_test_data(QUAKE_ONE_INFO, str(tmp_path / "info.json"))
    assert d["issue"]["source"] == "Quake.One"
    assert d["points"] == [{"pref": "北海道", "addr": "渡島地方東部", "isArea": True, "scale": 10}]


def test_convert_quake_one_without_small_points(tmp_path):
    d = tr.convert_quake_test_data(QUAKE_ONE_INFO, str(tmp_path / "info.json"))
    assert d["points"] == []


def test_p2p_format_passes_through():
    p2p = {"issue": {"type": "DetailScale"}, "earthquake": {}, "points": []}
    assert tr.convert_quake_test_data(p2p, "x.json") is p2p


def test_station_lookup_ignores_asterisk_mark():
    st = {"函館市新浜町": (41.8, 141.1), "別の観測点＊": (35.0, 135.0)}
    assert _station_latlon(st, "函館市新浜町＊") == (41.8, 141.1)   # JSON側に＊、stations側に無し
    assert _station_latlon(st, "別の観測点") == (35.0, 135.0)       # P2P側に無し、stations側に＊
    assert _station_latlon(st, "函館市新浜町") == (41.8, 141.1)
    assert _station_latlon(st, "存在しない") is None


def test_failover_default_prefers_quake_one_and_excludes_xml():
    if "QUAKE_FAILOVER_SOURCES" in os.environ:
        return  # 環境変数で上書きされている場合は既定値を検証できない
    assert config.QUAKE_FAILOVER_SOURCES == ["quake_one", "jma_json"]
