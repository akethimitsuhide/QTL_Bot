"""core.gis_render._finalize（PNG化・縮小）のテスト。"""
import io
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

from PIL import Image

from core import gis_render


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


def test_finalize_supersampled_reduces_to_target_size():
    canvas = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    img = _open(gis_render._finalize(canvas, (200, 150)))
    assert img.format == "PNG" and img.size == (200, 150) and img.mode == "RGB"


def test_finalize_non_integer_ratio_falls_back_to_resize():
    canvas = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    assert _open(gis_render._finalize(canvas, (130, 100))).size == (130, 100)


def test_finalize_without_target_keeps_size():
    canvas = Image.new("RGBA", (64, 48), (0, 0, 0, 0))
    assert _open(gis_render._finalize(canvas)).size == (64, 48)


def test_finalize_transparent_becomes_sea_color_and_opaque_is_kept():
    canvas = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    canvas.paste((200, 10, 10, 255), (0, 0, 20, 40))          # 左半分だけ不透明な赤
    img = _open(gis_render._finalize(canvas, (20, 20)))
    sea = tuple(gis_render._theme_colors()[1])[:3]
    assert img.getpixel((15, 10)) == sea                       # 透明部分は海色
    assert img.getpixel((2, 10)) == (200, 10, 10)              # 不透明部分はそのまま


def test_project_ring_matches_project():
    bbox = gis_render._BBox(130.0, 140.0, 30.0, 40.0)
    proj = gis_render._Projector(bbox, 500, 400)
    ring = [(131.5, 31.0), (139.0, 39.5), (130.0, 40.0)]
    assert proj.project_ring(ring) == [proj.project(lon, lat) for lon, lat in ring]
