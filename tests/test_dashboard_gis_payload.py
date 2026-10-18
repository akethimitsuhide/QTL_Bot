"""ダッシュボードの地図データ配信（cogs.system の _build_gis_payload / _gis_payload_response）のテスト。"""
import gzip
import json
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

from aiohttp.test_utils import make_mocked_request

from cogs.system import _build_gis_payload, _gis_payload_response

_FEATURES = [
    {"type": "Feature", "properties": {"name": "a"},
     "geometry": {"type": "Polygon", "coordinates": [[[139.123456789, 35.987654321], [140.0, 36.0], [139.5, 37.0], [139.123456789, 35.987654321]]]}},
    {"type": "Feature", "properties": {"name": "empty"}, "geometry": {"type": "Polygon", "coordinates": []}},
    {"type": "Feature", "properties": {"name": "none"}, "geometry": None},
]


def test_payload_keeps_geometry_only_rounds_and_drops_empty():
    raw, gz = _build_gis_payload(_FEATURES)
    data = json.loads(raw)
    assert len(data) == 1                                  # 描画できない2件は除く
    assert data[0]["properties"] == {}                     # 属性は配信しない
    assert data[0]["geometry"]["coordinates"][0][0] == [139.1235, 35.9877]   # 4桁に丸め
    assert json.loads(gzip.decompress(gz)) == data         # gzip版は同じ内容


def test_response_negotiates_gzip():
    payload = _build_gis_payload(_FEATURES)
    resp = _gis_payload_response(make_mocked_request("GET", "/x", headers={"Accept-Encoding": "gzip, deflate"}), payload)
    assert resp.headers["Content-Encoding"] == "gzip"
    assert resp.body == payload[1]
    assert "max-age" in resp.headers["Cache-Control"]


def test_response_plain_without_gzip_support():
    payload = _build_gis_payload(_FEATURES)
    resp = _gis_payload_response(make_mocked_request("GET", "/x"), payload)
    assert "Content-Encoding" not in resp.headers
    assert resp.body == payload[0]
