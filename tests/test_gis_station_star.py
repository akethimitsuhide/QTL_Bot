"""
tests/test_gis_station_star.py
===============================
気象庁HP JSON（代替取得）由来の観測点名「＊」付き（例: "横芝光町宮川＊"）で
render_shindo_map が KeyError になっていた不具合の回帰テスト（2026-10-29）。

GISデータ（data/gis/）が無い環境でも動くよう、陸地・観測点はダミーに差し替える。
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core import gis_render as g  # noqa: E402


class _FakeAreaSet:
    def __init__(self, shapes):
        self._shapes = shapes

    def get(self):
        return self._shapes


def _setup(monkeypatch, stations):
    rings = [[(140.0, 35.0), (141.0, 35.0), (141.0, 36.0), (140.0, 36.0)]]
    shape = SimpleNamespace(rings=rings, bbox=g._rings_bbox(rings))
    monkeypatch.setattr(g, "_local_areas", _FakeAreaSet({"千葉県北東部": shape}))
    monkeypatch.setattr(g, "_ready", lambda: True)
    monkeypatch.setattr(g, "_stations", stations)


def test_star_suffixed_name_does_not_raise(monkeypatch):
    """stations に「＊」無しで登録されている観測点を、「＊」付きの名前で渡しても描画できる。"""
    _setup(monkeypatch, {"横芝光町宮川": (35.66, 140.50)})
    png = g.render_shindo_map(
        station_shindo={"横芝光町宮川＊": 20},
        hypocenter_lonlat=(140.7, 35.7),
    )
    assert png is not None


def test_star_in_stations_but_not_in_name(monkeypatch):
    """逆向き（stations に「＊」付き、渡す名前は「＊」無し。P2P由来）も描画できる。"""
    _setup(monkeypatch, {"横芝光町宮川＊": (35.66, 140.50)})
    png = g.render_shindo_map(
        station_shindo={"横芝光町宮川": 20},
        hypocenter_lonlat=(140.7, 35.7),
    )
    assert png is not None


def test_exact_match_still_works(monkeypatch):
    _setup(monkeypatch, {"横芝光町宮川": (35.66, 140.50)})
    png = g.render_shindo_map(
        station_shindo={"横芝光町宮川": 20},
        hypocenter_lonlat=(140.7, 35.7),
    )
    assert png is not None
