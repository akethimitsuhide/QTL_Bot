"""
tests/test_estimated_intensity.py
==================================
推計震度分布図（core/estimated_intensity.py と render_estimated_intensity_map、2026-11-14追加）のテスト。
一覧JSONの形は、気象庁HPの実データ（2026-11-08 静岡県西部 M5.7）を元にした合成データ。
"""
import asyncio
import io
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from PIL import Image  # noqa: E402

from core import estimated_intensity as ei  # noqa: E402
from core import gis_render as g  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

RAW = {
    "hypo": {"it": "2026-11-08T01:13:42", "at": "2026-11-08T01:04:00", "lat": 34.93, "lon": 137.66,
             "dep": 40, "mag": 5.7, "epi": "静岡県西部", "kun": 0, "maxi": 5.0},
    "comment": "震度５弱のところでは、物が倒れたり、ガラスが割れるなどの被害が発生している可能性があります。",
    "url": "202611080104_443",
    "rank_cnt": {"i9": 0, "i8": 0, "i7": 0, "i6": 85, "i5": 1920, "i4": 107281, "i3": 221968},
    "bounds": [[34.6, 136.57], [35.9, 138.24]],
    "mesh_num": ["5137", "5138", "5236"],
    "datum": 2,
}


def _png(color=(255, 230, 0, 255), size=(80, 80)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


# ---------- 解析 ----------
def test_parse_entries_reads_real_shape():
    (e,) = ei.parse_entries([RAW])
    assert e.id == "202611080104_443" and e.epicenter == "静岡県西部"
    assert (e.lat, e.lon, e.depth, e.magnitude) == (34.93, 137.66, 40.0, "5.7")
    assert e.meshes == ["5137", "5138", "5236"] and e.datum == 2
    assert e.bounds == ((34.6, 136.57), (35.9, 138.24))


def test_parse_entries_skips_malformed_and_unsafe():
    bad_id = dict(RAW, url="../../etc/passwd")
    bad_mesh = dict(RAW, mesh_num=["5137", "../x", "12"])
    out = ei.parse_entries([bad_id, "x", None, bad_mesh])
    assert [e.id for e in out] == [RAW["url"]] and out[0].meshes == ["5137"]
    assert ei.parse_entries({"a": 1}) == []


def test_rank_summary_orders_large_first_and_drops_zero():
    (e,) = ei.parse_entries([RAW])
    assert ei.rank_summary(e) == [("5強", 85), ("5弱", 1920), ("4", 107281)]


# ---------- メッシュの範囲 ----------
def test_mesh_bounds_modern_datum_has_no_correction():
    (lat0, lon0), (lat1, lon1) = ei.mesh_bounds("5137", 2)
    assert (lat0, lon0) == pytest.approx((34.0, 137.0))
    assert (lat1, lon1) == pytest.approx((34.0 + 40 / 60, 138.0))


def test_mesh_bounds_old_datum_is_shifted_slightly():
    new = ei.mesh_bounds("5337", 2)
    old = ei.mesh_bounds("5337", None)
    assert old[0][0] != new[0][0]
    # 日本測地系→世界測地系の補正は緯度で約0.005度、経度で約0.01度
    assert abs(old[0][0] - new[0][0]) < 0.01 and abs(old[0][1] - new[0][1]) < 0.02


def test_tile_url_is_fixed_host():
    (e,) = ei.parse_entries([RAW])
    assert ei.tile_url(e, "5236") == (
        "https://www.jma.go.jp/bosai/estimated_intensity_map/data/202611080104_443/5236.png")


# ---------- 取得（条件付きGET・サイズ上限） ----------
class _Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status, self._body, self.headers = status, body, headers or {}
        self.content = SimpleNamespace(iter_chunked=self._iter)

    async def _iter(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, resps):
        self.resps, self.calls = list(resps), []

    def get(self, url, headers=None):
        self.calls.append((url, headers or {}))
        return self.resps.pop(0)


def test_fetch_entries_uses_etag_and_handles_304():
    import json
    body = json.dumps([RAW]).encode()
    sess = _Session([_Resp(200, body, {"ETag": 'W/"abc"', "Last-Modified": "x"}), _Resp(304)])
    state = {}
    first = asyncio.run(ei.fetch_entries(sess, state))
    assert [e.id for e in first] == [RAW["url"]] and state["etag"] == 'W/"abc"'
    assert asyncio.run(ei.fetch_entries(sess, state)) is None
    assert sess.calls[1][1]["If-None-Match"] == 'W/"abc"'


def test_fetch_entries_rejects_oversize_and_http_error(monkeypatch):
    monkeypatch.setattr(ei, "MAX_LIST_BYTES", 10)
    with pytest.raises(Exception):
        asyncio.run(ei.fetch_entries(_Session([_Resp(200, b"x" * 100)]), {}))
    with pytest.raises(Exception):
        asyncio.run(ei.fetch_entries(_Session([_Resp(503)]), {}))


def test_fetch_tiles_returns_only_successful_ones():
    (e,) = ei.parse_entries([RAW])
    ok = _png()
    sess = _Session([_Resp(200, ok), _Resp(404), _Resp(200, ok)])
    tiles = asyncio.run(ei.fetch_tiles(sess, e))
    assert len(tiles) == 2 and set(tiles) <= {"5137", "5138", "5236"}


# ---------- 描画 ----------
class _FakeAreaSet:
    def get(self):
        ring = [(136.0, 34.0), (139.0, 34.0), (139.0, 36.0), (136.0, 36.0)]
        shape = SimpleNamespace(code="", name="", rings=[ring], bbox=g._rings_bbox([ring]))
        return {"区域": shape}


def _setup(monkeypatch):
    monkeypatch.setattr(g, "_local_areas", _FakeAreaSet())
    monkeypatch.setattr(g, "_ready", lambda: True)


def test_render_returns_png(monkeypatch):
    _setup(monkeypatch)
    tiles = [(ei.mesh_bounds("5137", 2), _png()), (ei.mesh_bounds("5236", 2), _png((255, 40, 0, 255)))]
    out = g.render_estimated_intensity_map(tiles, ((34.6, 136.57), (35.9, 138.24)), (137.66, 34.93), ei.LEGEND)
    assert out is not None and out.startswith(PNG_MAGIC)


def test_render_skips_broken_tiles_and_returns_none_if_nothing_pasted(monkeypatch):
    _setup(monkeypatch)
    good = (ei.mesh_bounds("5137", 2), _png())
    bad = (ei.mesh_bounds("5138", 2), b"not a png")
    assert g.render_estimated_intensity_map([bad, good], None, None) is not None
    assert g.render_estimated_intensity_map([bad], None, None) is None


def test_render_none_when_not_ready_or_no_tiles(monkeypatch):
    _setup(monkeypatch)
    assert g.render_estimated_intensity_map([], None, None) is None
    monkeypatch.setattr(g, "_ready", lambda: False)
    assert g.render_estimated_intensity_map([(ei.mesh_bounds("5137", 2), _png())], None, None) is None
