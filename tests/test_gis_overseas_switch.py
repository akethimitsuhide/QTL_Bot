"""
tests/test_gis_overseas_switch.py
==================================
EEW・地震情報の地図で、震源が表示範囲の外、または表示範囲に陸地が無い場合に
海外向け地図へ自動で切り替える処理（core/gis_render.py の _needs_overseas_map、
render_eew_warn_map / render_shindo_map / render_overseas_map）のテスト
（2026-10-03追加。台湾付近の地震のEEWで海だけの画像が添付された不具合の対策）。

純粋な判定ロジックのテストはGISデータ無しで動く。実際に描画するテストは
data/gis/ のGeoJSONが取得済み（python bot.py --refresh_gis_data）の環境でのみ実行し、
無い環境ではスキップする。
"""
import io
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core import gis_render as g  # noqa: E402


class FakeAreaSet:
    def __init__(self, shapes):
        self._shapes = shapes

    def get(self):
        return self._shapes


def _shape(rings):
    return SimpleNamespace(rings=rings, bbox=g._rings_bbox(rings))


VIEW = g._BBox(130.0, 135.0, 33.0, 38.0)


def test_no_hypocenter_never_switches():
    assert g._needs_overseas_map([], VIEW, (FakeAreaSet({}),)) is False


def test_hypocenter_outside_viewport_switches():
    land = FakeAreaSet({"a": _shape([[(131, 34), (132, 34), (132, 35), (131, 35)]])})
    assert g._needs_overseas_map([(121.7, 23.8)], VIEW, (land,)) is True      # 台湾付近
    assert g._needs_overseas_map([(145.0, 47.5)], VIEW, (land,)) is True      # 枠の外（緯度>46）


def test_hypocenter_inside_with_land_in_view_does_not_switch():
    land = FakeAreaSet({"a": _shape([[(131, 34), (132, 34), (132, 35), (131, 35)]])})
    assert g._needs_overseas_map([(133.0, 36.0)], VIEW, (land,)) is False


def test_hypocenter_inside_but_no_land_switches():
    far = FakeAreaSet({"a": _shape([[(140, 40), (141, 40), (141, 41), (140, 41)]])})       # bboxが重ならない
    assert g._needs_overseas_map([(133.0, 36.0)], VIEW, (far,)) is True
    # 島しょ部のように区域のbboxだけが広く重なり、頂点は表示範囲に1つも無い場合
    sparse = FakeAreaSet({"islands": _shape([[(120, 20), (150, 20), (150, 25), (120, 25), (120, 36)]])})
    assert sparse._shapes["islands"].bbox.intersects(VIEW)
    assert g._needs_overseas_map([(133.0, 36.0)], VIEW, (sparse,)) is True


def test_any_area_set_with_land_prevents_switch():
    far = FakeAreaSet({"a": _shape([[(140, 40), (141, 40), (141, 41), (140, 41)]])})
    near = FakeAreaSet({"b": _shape([[(131, 34), (132, 34), (132, 35), (131, 35)]])})
    assert g._needs_overseas_map([(133.0, 36.0)], VIEW, (far, near)) is False


# ---------- 実際の描画（GISデータがある環境のみ） ----------
needs_gis = pytest.mark.skipif(not g._ready() or not g._local_areas.get(),
                               reason="data/gis のGeoJSONが未取得のためスキップ")


def _is_sea_only(png: bytes) -> bool:
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    colors = im.getcolors(maxcolors=1 << 20) or []
    # 海色（_SEA_COLOR）のみ、またはほぼ海色だけ（震源の×印の分を除く）
    sea = next((n for n, c in colors if c[:3] == tuple(g._SEA_COLOR[:3])), 0)
    return sea / (im.width * im.height) > 0.995


@needs_gis
@pytest.mark.parametrize("name,hypo", [
    ("台湾付近", (121.7, 23.8)),
    ("オホーツク海北部（緯度46超）", (145.0, 47.5)),
    ("日本海中部", (135.0, 40.5)),
    ("小笠原諸島西方沖", (138.5, 27.5)),
])
def test_render_shindo_map_never_returns_sea_only_image(name, hypo):
    png = g.render_shindo_map(hypocenter_lonlat=hypo, enlarge_hypocenter_mark=True)
    assert png is not None and not _is_sea_only(png), name


@needs_gis
def test_taiwan_eew_warn_map_keeps_okinawa_fill_on_wide_map():
    from PIL import Image
    with_warn = g.render_eew_warn_map({"沖縄本島", "宮古島", "八重山"}, (121.7, 23.8))
    no_warn = g.render_eew_warn_map(set(), (121.7, 23.8))
    assert with_warn is not None and no_warn is not None
    im = Image.open(io.BytesIO(with_warn))
    assert im.width >= 800 and not _is_sea_only(with_warn)
    assert with_warn != no_warn                      # 警報地域（沖縄）の塗りつぶしが広域図に残っている


@needs_gis
def test_normal_domestic_map_is_unchanged_in_mode():
    """日本国内で陸地が写る通常の地図は、海外向け（広域）には切り替わらない（縦横比・サイズで確認）。"""
    from PIL import Image
    png = g.render_shindo_map(region_shindo={"茨城県南部": 20, "千葉県北東部": 10}, hypocenter_lonlat=(139.9, 36.1))
    im = Image.open(io.BytesIO(png))
    assert im.size != (900, 754)                       # 台湾付近の広域図とは別のサイズ（国内向けの拡大図）


@needs_gis
def test_overseas_map_region_fill_is_optional_and_safe_for_unknown_names():
    assert g.render_overseas_map((121.7, 23.8)) is not None
    assert g.render_overseas_map((121.7, 23.8), region_shindo={"存在しない区域": 20}, warn_region_names={"存在しない"}) is not None
