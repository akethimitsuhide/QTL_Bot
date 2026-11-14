"""
tests/test_gis_long_period_area.py
===================================
長周期地震動に関する観測情報の「細分区域ごとの塗りつぶし地図」
（core.gis_render.render_long_period_area_map、2026-11-14追加）のテスト。

GISデータ（data/gis/）が無い環境でも動くよう、陸地（細分区域）はダミーに差し替える。
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core import gis_render as g  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class _FakeAreaSet:
    def __init__(self, shapes):
        self._shapes = shapes

    def get(self):
        return self._shapes


def _shape(lon0, lat0):
    rings = [[(lon0, lat0), (lon0 + 1.0, lat0), (lon0 + 1.0, lat0 + 1.0), (lon0, lat0 + 1.0)]]
    return SimpleNamespace(code="", name="", rings=rings, bbox=g._rings_bbox(rings))


def _setup(monkeypatch):
    shapes = {"愛知県西部": _shape(136.5, 34.5), "愛知県東部": _shape(137.5, 34.5)}
    monkeypatch.setattr(g, "_local_areas", _FakeAreaSet(shapes))
    monkeypatch.setattr(g, "_ready", lambda: True)


def test_renders_png_for_known_regions(monkeypatch):
    _setup(monkeypatch)
    png = g.render_long_period_area_map(
        region_lg={"愛知県西部": "1", "愛知県東部": "2"},
        hypocenter_lonlat=(137.7, 34.9),
    )
    assert png is not None and png.startswith(PNG_MAGIC)


def test_unknown_region_names_are_skipped(monkeypatch):
    _setup(monkeypatch)
    png = g.render_long_period_area_map(
        region_lg={"愛知県西部": "1", "存在しない区域": "3"},
        hypocenter_lonlat=None,
    )
    assert png is not None and png.startswith(PNG_MAGIC)


def test_returns_none_when_no_region_matches(monkeypatch):
    """震源だけの地図は1枚目と重複するため作らない（区域が1件も照合できなければ None）。"""
    _setup(monkeypatch)
    assert g.render_long_period_area_map(
        region_lg={"存在しない区域": "1"}, hypocenter_lonlat=(137.7, 34.9)) is None


def test_unknown_or_empty_level_is_ignored(monkeypatch):
    _setup(monkeypatch)
    assert g.render_long_period_area_map(
        region_lg={"愛知県西部": "不明", "愛知県東部": ""}, hypocenter_lonlat=None) is None


def test_none_and_empty_input(monkeypatch):
    _setup(monkeypatch)
    assert g.render_long_period_area_map(region_lg=None) is None
    assert g.render_long_period_area_map(region_lg={}) is None


def test_returns_none_when_gis_not_ready(monkeypatch):
    monkeypatch.setattr(g, "_ready", lambda: False)
    assert g.render_long_period_area_map(region_lg={"愛知県西部": "1"}) is None
