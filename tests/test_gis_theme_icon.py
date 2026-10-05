"""
tests/test_gis_theme_icon.py
=============================
地図の配色テーマ（GIS_MAP_THEME）と震度アイコンの装飾（角丸・外側の縁・文字色）の
テスト（2026-10-05追加）。GISデータ無しで動くよう、PILの画像へ直接アイコンを描く。
"""
import os
import subprocess
import sys

from PIL import Image, ImageDraw

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core import config  # noqa: E402
from core import gis_render as g  # noqa: E402


def _run_env(env_extra: dict, code: str) -> str:
    env = {**os.environ, "BOT_TOKEN": "x", "CHANNEL_ID": "1", **env_extra}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                          text=True, check=True).stdout.strip()


def test_theme_selection_and_invalid_fallback():
    code = "from core import gis_render as g; print(g._LAND_COLOR, g._SEA_COLOR, g._BOUNDARY_COLOR)"
    for theme in ("light", "dark", "highlight"):
        out = _run_env({"GIS_MAP_THEME": theme}, code)
        assert out == str(g._THEMES[theme][0]) + " " + str(g._THEMES[theme][1]) + " " + str(g._THEMES[theme][2])
    assert _run_env({"GIS_MAP_THEME": "nonsense"}, code) == _run_env({"GIS_MAP_THEME": "light"}, code)
    assert g._THEMES["light"] == ((238, 232, 220, 255), (178, 205, 227, 255), (80, 80, 80, 255))  # 従来の配色


def test_icon_text_color_parser():
    p = config._parse_icon_text_colors
    assert p("1:FFFFFF,5-:#000000, 6+:0xFF0000") == {"1": 0xFFFFFF, "5-": 0, "6+": 0xFF0000}
    assert p("") == {} and p("x") == {} and p("1:GGGGGG") == {} and p("1:FFF") == {}


def _icon(monkeypatch, **cfg):
    for k, v in cfg.items():
        monkeypatch.setattr(g, k, v)
    img = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
    g._draw_shindo_square(ImageDraw.Draw(img), (50, 50), 45, 40)   # 一辺40px、震度5弱
    return img


def test_icon_has_outer_border_with_lower_saturation_and_rounded_corners(monkeypatch):
    img = _icon(monkeypatch, GIS_ICON_CORNER_RADIUS_RATIO=0.25, GIS_ICON_BORDER_RATIO=0.1,
                GIS_ICON_BORDER_SATURATION=0.5, GIS_ICON_TEXT_COLORS={})
    # 一辺40の外側に4px（1/10）の縁 → x=28〜29（中心50-20-4=26〜30の範囲）が縁
    fill = img.getpixel((50, 35))        # アイコン内（文字を避ける）
    border = img.getpixel((50, 28))      # 上辺の外側4pxの縁
    outside = img.getpixel((50, 24))     # 縁の外
    assert outside == (255, 255, 255, 255)
    assert border != fill and border[:3] != (255, 255, 255)
    import colorsys
    sat = lambda c: colorsys.rgb_to_hsv(*(v / 255 for v in c[:3]))[1]
    assert sat(border) < sat(fill)
    # 角が丸い: 縁を含む外形の左上の角（30-4,30-4 付近）は塗られていない
    assert img.getpixel((26, 26)) == (255, 255, 255, 255)
    assert img.getpixel((50, 26)) != (255, 255, 255, 255)           # 辺の中央は縁がある


def test_icon_without_border_or_rounding_is_plain_square(monkeypatch):
    img = _icon(monkeypatch, GIS_ICON_CORNER_RADIUS_RATIO=0.0, GIS_ICON_BORDER_RATIO=0.0,
                GIS_ICON_BORDER_SATURATION=0.5, GIS_ICON_TEXT_COLORS={})
    assert img.getpixel((30, 30)) == img.getpixel((50, 35))          # 角まで塗られる
    assert img.getpixel((29, 50)) == (255, 255, 255, 255)            # 縁なし


def test_icon_text_color_default_and_override(monkeypatch):
    def has_color(img, rgb):
        return any(img.getpixel((x, y))[:3] == rgb for x in range(30, 70) for y in range(30, 70))
    base = _icon(monkeypatch, GIS_ICON_TEXT_COLORS={})
    assert has_color(base, (20, 20, 20))                              # 既定は従来の文字色
    custom = _icon(monkeypatch, GIS_ICON_TEXT_COLORS={"5-": 0xFFFFFF})
    assert not has_color(custom, (20, 20, 20))
    assert has_color(custom, (255, 255, 255))
