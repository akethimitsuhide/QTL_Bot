"""
tests/test_gis_wave_overlay.py
===============================
EEW地図のP波・S波の円（core/gis_render.py の wave_overlay）のテスト。
GISデータ（data/gis/）が無い環境でも動くよう、陸地・国境はダミーに差し替える。
"""
import io
import os
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core import gis_render as g  # noqa: E402
from core.eew_wave import WaveOverlay, circle_ring  # noqa: E402

P_RGB = ((g.EEW_WAVE_P_COLOR >> 16) & 255, (g.EEW_WAVE_P_COLOR >> 8) & 255, g.EEW_WAVE_P_COLOR & 255)
S_RGB = ((g.EEW_WAVE_S_COLOR >> 16) & 255, (g.EEW_WAVE_S_COLOR >> 8) & 255, g.EEW_WAVE_S_COLOR & 255)
HYPO = (140.5, 35.5)


class _FakeAreaSet:
    def __init__(self, shapes):
        self._shapes = shapes

    def get(self):
        return self._shapes


@pytest.fixture()
def fake_gis(monkeypatch):
    rings = [[(140.0, 35.0), (141.0, 35.0), (141.0, 36.0), (140.0, 36.0)]]
    shape = SimpleNamespace(rings=rings, bbox=g._rings_bbox(rings))
    monkeypatch.setattr(g, "_eew_areas", _FakeAreaSet({"テスト県": shape}))
    monkeypatch.setattr(g, "_local_areas", _FakeAreaSet({"テスト地方": shape}))
    monkeypatch.setattr(g, "_ready", lambda: True)
    monkeypatch.setattr(g, "_draw_country_fill", lambda *a, **k: None)
    return shape


def _count_color(png, rgb, tol=40):
    img = Image.open(io.BytesIO(png)).convert("RGB")
    raw = img.tobytes()
    n = 0
    for k in range(0, len(raw), 3):
        if (abs(raw[k] - rgb[0]) <= tol and abs(raw[k + 1] - rgb[1]) <= tol
                and abs(raw[k + 2] - rgb[2]) <= tol):
            n += 1
    return n


def _overlay(p=150.0, s=90.0):
    return WaveOverlay(circles=(("P", p), ("S", s)), caption="12:00:20 JST  +20s")


# ---------- 表示範囲 ----------
def test_wave_ring_points_empty_without_overlay_or_center():
    assert g._wave_ring_points(HYPO, None) == []
    assert g._wave_ring_points(None, _overlay()) == []
    assert g._wave_ring_points(HYPO, WaveOverlay(circles=())) == []


def test_expand_viewport_returns_same_object_without_circles():
    vp = g._BBox(140.0, 141.0, 35.0, 36.0)
    assert g._expand_viewport_for_circles(vp, []) is vp


def test_expand_viewport_contains_all_circle_points_and_the_original():
    vp = g._BBox(140.0, 141.0, 35.0, 36.0)
    pts = g._wave_ring_points(HYPO, _overlay(p=300.0, s=150.0))
    out = g._expand_viewport_for_circles(vp, pts)
    assert out.lon_min <= min(p[0] for p in pts) and out.lon_max >= max(p[0] for p in pts)
    assert out.lat_min <= min(p[1] for p in pts) and out.lat_max >= max(p[1] for p in pts)
    assert out.lon_min <= vp.lon_min and out.lon_max >= vp.lon_max


def test_expand_viewport_can_exceed_the_japan_frame():
    vp = g._japan_bbox()
    pts = g._wave_ring_points((142.0, 24.0), _overlay(p=1500.0, s=800.0))
    out = g._expand_viewport_for_circles(vp, pts)
    assert out.lat_min < g._JAPAN_LAT_MIN or out.lon_max > g._JAPAN_LON_MAX
    assert -85.0 <= out.lat_min and out.lat_max <= 85.0


# ---------- 描画 ----------
def test_eew_warn_map_draws_p_and_s_circles(fake_gis):
    base = g.render_eew_warn_map({"テスト県"}, HYPO)
    with_ov = g.render_eew_warn_map({"テスト県"}, HYPO, wave_overlay=_overlay())
    assert base is not None and with_ov is not None
    assert _count_color(base, P_RGB) < 30 < _count_color(with_ov, P_RGB)
    assert _count_color(with_ov, S_RGB) > 30


def test_no_p_s_letters_are_drawn_on_the_circles(fake_gis, monkeypatch):
    """円の上に「P」「S」の文字は描かない（色で区別する）。左下の注記だけが文字として描かれる。"""
    texts = []
    original = g.ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        texts.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(g.ImageDraw.ImageDraw, "text", spy)
    for render in (
        lambda: g.render_eew_warn_map({"テスト県"}, HYPO, wave_overlay=_overlay()),
        lambda: g.render_shindo_map(region_shindo={"テスト地方": 50}, hypocenter_lonlat=HYPO, wave_overlay=_overlay()),
        lambda: g.render_overseas_map((121.5, 24.0), wave_overlay=_overlay(p=400.0, s=250.0)),
    ):
        texts.clear()
        assert render() is not None
        assert "P" not in texts and "S" not in texts
        assert "12:00:20 JST  +20s" in texts


def test_shindo_map_draws_circles(fake_gis):
    png = g.render_shindo_map(region_shindo={"テスト地方": 50}, hypocenter_lonlat=HYPO,
                              enlarge_hypocenter_mark=True, wave_overlay=_overlay())
    assert png is not None and _count_color(png, P_RGB) > 30


def test_no_overlay_output_is_identical_to_before(fake_gis):
    a = g.render_shindo_map(region_shindo={"テスト地方": 50}, hypocenter_lonlat=HYPO)
    b = g.render_shindo_map(region_shindo={"テスト地方": 50}, hypocenter_lonlat=HYPO, wave_overlay=None)
    c = g.render_shindo_map(region_shindo={"テスト地方": 50}, hypocenter_lonlat=HYPO,
                            wave_overlay=WaveOverlay(circles=()))
    assert a == b == c


def test_overseas_map_draws_circles(fake_gis):
    taiwan = (121.5, 24.0)
    png = g.render_overseas_map(taiwan, wave_overlay=_overlay(p=400.0, s=250.0))
    assert png is not None and _count_color(png, P_RGB) > 30
    base = g.render_overseas_map(taiwan)
    assert base is not None and _count_color(base, P_RGB) < 30


def test_overseas_switch_still_decided_before_circles_expand_the_viewport(fake_gis, monkeypatch):
    """震源が日本の枠の外なら、円の有無にかかわらず海外向け地図に切り替わる。"""
    called = {}

    def fake_overseas(hypo, **kw):
        called["wave_overlay"] = kw.get("wave_overlay")
        return b"overseas"

    monkeypatch.setattr(g, "render_overseas_map", fake_overseas)
    ov = _overlay()
    out = g.render_eew_warn_map({"テスト県"}, (121.5, 24.0), wave_overlay=ov)
    assert out == b"overseas" and called["wave_overlay"] is ov
    called.clear()
    out = g.render_shindo_map(region_shindo={"テスト地方": 50}, hypocenter_lonlat=(121.5, 24.0), wave_overlay=ov)
    assert out == b"overseas" and called["wave_overlay"] is ov


def test_circle_larger_than_japan_frame_renders_without_error(fake_gis):
    png = g.render_eew_warn_map({"テスト県"}, HYPO, wave_overlay=_overlay(p=1800.0, s=1000.0))
    assert png is not None and Image.open(io.BytesIO(png)).size[0] > 0


def test_circle_ring_matches_the_overlay_geometry():
    ring = circle_ring(*HYPO, 150.0)
    assert len(ring) == 72
