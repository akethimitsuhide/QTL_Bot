"""
core/gis_tile_render.py
========================
遠地地震（震源が日本国外）向けに、国土地理院（GSI）の淡色地図タイルを
背景に、日本の細分区域境界線（core.gis_render が保持する
AreaForecastLocalE_GIS データを再利用）と震源のバツ印を重ね描きした
PNG画像を生成するモジュール（2026-09-15追加）。

【なぜ別モジュールにしたか】
core/gis_render.py は「外部データはGeoJSONのみ・プロセス内メモリキャッシュ
のみ・ネットワークI/Oなし」という設計で統一していたが、本モジュールは
国土地理院タイルサーバーへのHTTPフェッチ（I/O）が必須のため、責務を
分離した。震源が日本国内の場合は従来通り core.gis_render のみで完結し、
海外の場合のみ本モジュールに切り替わる。

【投影方式の違いに注意】
core.gis_render は簡易正距円筒図法（cos補正）だが、国土地理院タイルは
Web Mercator（EPSG:3857）で配信されているため、本モジュールは独自に
Web Mercator用の投影（緯度経度⇔グローバルピクセル座標）を実装している
（_lonlat_to_mercator_px）。2つの図法を混在させると重ね合わせがずれる
ため、日本の区域境界線・震源マークの重ね描きも必ずこのモジュール内の
Web Mercator投影で行う（core.gis_render側の投影関数は流用しない）。

【タイル取得とキャッシュ】
- 淡色地図: https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png
- 表示範囲（日本全体 ∪ 震源）を収める最も詳細なズームレベルを、
  最大タイル数（_MAX_TILES）の範囲内で選ぶ（画像サイズ・リクエスト数を
  抑えるため）。
- タイル画像はズーム/x/y ごとに GIS_MAP_DATA_DIR/tiles/ 配下へ永続
  キャッシュする（地図タイルは内容が変化しない静的データのため）。
- 取得失敗（404・タイムアウト等）のタイルは1枚ずつ空白（背景色）として
  扱い、処理全体は継続する（GSI側が日本国外のデータを持たない可能性が
  あるため。震源位置の相対関係を示す区域線・バツ印は国土地理院タイルの
  有無に関わらず正しく重ね描きされる）。
- 国土地理院コンテンツ利用規約に基づき、画像内に「出典：国土地理院」の
  出典表示を必ず入れる。

GIS_MAP_ENABLE=false、または細分区域データ未取得の場合は None を返す。
タイル取得に全面的に失敗した場合でも、区域線・震源マークのみの画像は
返す（国土地理院タイルは「あれば表示する」付加情報という位置づけ）。
"""
import asyncio
import io
import logging
import math
import os
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from core.config import GIS_MAP_DATA_DIR, GIS_MAP_WARNING_COLOR
from core.gis_render import (
    _ready, _rgb_to_rgba, _draw_x_mark,
    get_local_area_shapes,
    _JAPAN_LON_MIN, _JAPAN_LON_MAX, _JAPAN_LAT_MIN, _JAPAN_LAT_MAX,
    _BOUNDARY_COLOR, _BOUNDARY_WIDTH,
)

logger = logging.getLogger("QTLBot")

GSI_PALE_TILE_URL = "https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png"
_TILE_SIZE = 256
_MAX_TILES = 64            # 1回の描画で取得するタイル数の上限（サーバー負荷・処理時間対策）
_MIN_ZOOM, _MAX_ZOOM = 1, 9
_TILE_FETCH_TIMEOUT = 8
_TILE_FETCH_CONCURRENCY = 6
_VIEWPORT_PADDING_RATIO = 0.15   # 日本+震源のbboxに対する余白比率

_ATTRIBUTION_TEXT_JA = "出典：国土地理院"
_ATTRIBUTION_TEXT_FALLBACK = "Source: GSI, Japan"  # 日本語グリフを持つフォントが無い環境向け

# Raspberry Pi OS（Debian系）で見つかる可能性のある日本語フォントの
# 候補パス。PILの組み込みデフォルトフォントは日本語グリフを持たない
# ため、出典表示（国土地理院コンテンツ利用規約で必須）がそのままでは
# 文字化け（トーフ表示）してしまう。見つかったものを優先的に使い、
# どれも無い環境では英語表記にフォールバックする（新規フォント
# ファイルの同梱・フォントパッケージへの依存は追加しない方針のため）。
_JP_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/truetype/takao-gothic/TakaoGothic.ttf",
    "/usr/share/fonts/truetype/vlgothic/VL-Gothic-Regular.ttf",
    "/usr/share/fonts/truetype/ipafont-gothic/ipag.ttf",
)


def _tile_cache_path(z: int, x: int, y: int) -> str:
    return os.path.join(GIS_MAP_DATA_DIR, "tiles", str(z), str(x), f"{y}.png")


def _lonlat_to_mercator_px(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """緯度経度→Web Mercatorの「グローバルピクセル座標」（原点は地図全体の左上）。"""
    lat = max(min(lat, 85.05112878), -85.05112878)  # Web Mercatorの定義域外を弾く
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n * _TILE_SIZE
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n * _TILE_SIZE
    return x, y


def _choose_zoom(lon_min: float, lon_max: float, lat_min: float, lat_max: float) -> int:
    """表示範囲全体が _MAX_TILES 枚以内に収まる最大のズームレベルを選ぶ。"""
    for zoom in range(_MAX_ZOOM, _MIN_ZOOM - 1, -1):
        x0, y0 = _lonlat_to_mercator_px(lon_min, lat_max, zoom)  # 北西
        x1, y1 = _lonlat_to_mercator_px(lon_max, lat_min, zoom)  # 南東
        tiles_x = (int(x1 // _TILE_SIZE) - int(x0 // _TILE_SIZE)) + 1
        tiles_y = (int(y1 // _TILE_SIZE) - int(y0 // _TILE_SIZE)) + 1
        if tiles_x * tiles_y <= _MAX_TILES:
            return zoom
    return _MIN_ZOOM


async def _fetch_tile(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                       z: int, x: int, y: int) -> Optional[Image.Image]:
    n = 2 ** z
    # 日付変更線をまたぐ表示範囲では x が [0, n) の範囲外になりうるため、
    # 実際のタイル取得時に周回方向へ正規化する（_compute_overseas_viewport
    # 側は連続した値のまま扱っているため、ここで初めて実座標に変換する）。
    x = x % n
    if y < 0 or y >= n:
        return None  # 南北方向はタイルピラミッドの範囲外（極付近。通常発生しない）

    cache_path = _tile_cache_path(z, x, y)
    try:
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            return Image.open(cache_path).convert("RGBA")
    except Exception:
        pass  # 壊れたキャッシュは無視して再取得する

    url = GSI_PALE_TILE_URL.format(z=z, x=x, y=y)
    async with sem:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=_TILE_FETCH_TIMEOUT)) as resp:
                if resp.status != 200:
                    return None
                body = await resp.read()
        except Exception:
            return None

    try:
        img = Image.open(io.BytesIO(body)).convert("RGBA")
    except Exception:
        return None

    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(body)
        os.replace(tmp_path, cache_path)
    except Exception:
        logger.debug(f"GIS地図(海外): タイルキャッシュの書き込みに失敗しました z={z} x={x} y={y}")

    return img


async def _build_basemap(session: aiohttp.ClientSession, bbox: tuple, zoom: int) -> tuple:
    """
    指定範囲のタイルを取得・合成し、(PILImage, ピクセル原点のグローバル座標) を返す。
    取得に失敗したタイルは空白のまま（背景色）とする。
    """
    lon_min, lon_max, lat_min, lat_max = bbox
    px0, py0 = _lonlat_to_mercator_px(lon_min, lat_max, zoom)  # 北西（キャンバス左上）
    px1, py1 = _lonlat_to_mercator_px(lon_max, lat_min, zoom)  # 南東（キャンバス右下）

    tile_x_min = int(px0 // _TILE_SIZE)
    tile_x_max = int(px1 // _TILE_SIZE)
    tile_y_min = int(py0 // _TILE_SIZE)
    tile_y_max = int(py1 // _TILE_SIZE)

    canvas_w = (tile_x_max - tile_x_min + 1) * _TILE_SIZE
    canvas_h = (tile_y_max - tile_y_min + 1) * _TILE_SIZE
    tile_canvas = Image.new("RGBA", (canvas_w, canvas_h), (235, 235, 235, 255))

    sem = asyncio.Semaphore(_TILE_FETCH_CONCURRENCY)
    tasks = []
    coords = []
    for tx in range(tile_x_min, tile_x_max + 1):
        for ty in range(tile_y_min, tile_y_max + 1):
            tasks.append(_fetch_tile(session, sem, zoom, tx, ty))
            coords.append((tx, ty))

    tiles = await asyncio.gather(*tasks, return_exceptions=False)
    fetched = 0
    for (tx, ty), tile_img in zip(coords, tiles):
        if tile_img is None:
            continue
        fetched += 1
        ox = (tx - tile_x_min) * _TILE_SIZE
        oy = (ty - tile_y_min) * _TILE_SIZE
        tile_canvas.paste(tile_img, (ox, oy))

    logger.debug(f"GIS地図(海外): タイル取得 {fetched}/{len(coords)}枚 (zoom={zoom})")

    # キャンバス原点（左上）のグローバルピクセル座標
    origin_x = tile_x_min * _TILE_SIZE
    origin_y = tile_y_min * _TILE_SIZE

    # 表示範囲ぴったりにクロップ
    crop_box = (
        round(px0 - origin_x), round(py0 - origin_y),
        round(px1 - origin_x), round(py1 - origin_y),
    )
    cropped = tile_canvas.crop(crop_box)
    return cropped, (px0, py0)


def _compute_overseas_viewport(hypo_lonlat: tuple) -> tuple:
    """
    日本全体＋震源を収める表示範囲（緯度経度bbox）を返す。

    経度方向は日付変更線をまたぐ可能性がある（南米・北米西岸等、太平洋を
    挟んで日本の反対側にある震源の場合）。単純に min/max を取ると、
    太平洋を挟んだ「近い側」ではなく地球を逆回りした「遠い側」（ヨーロッパ
    ・アフリカ・大西洋経由）の範囲を選んでしまうことがあるため、震源の
    経度を ±360度シフトした場合も含めて候補を作り、経度方向の幅が最小
    になる＝太平洋側の短い経路を選ぶ（2026-09-15、実装時に発覚したため
    修正。lon の値自体は ±180度の範囲に収めず、連続した値のまま返す。
    実際のタイル座標への変換〈mod 2^zoom〉は _fetch_tile 側で行う）。
    """
    hypo_lon, hypo_lat = hypo_lonlat

    candidates = []
    for shifted_lon in (hypo_lon, hypo_lon + 360, hypo_lon - 360):
        lon_min = min(_JAPAN_LON_MIN, shifted_lon)
        lon_max = max(_JAPAN_LON_MAX, shifted_lon)
        candidates.append((lon_max - lon_min, lon_min, lon_max))
    _, lon_min, lon_max = min(candidates, key=lambda c: c[0])

    lat_min = min(_JAPAN_LAT_MIN, hypo_lat)
    lat_max = max(_JAPAN_LAT_MAX, hypo_lat)

    pad_lon = (lon_max - lon_min) * _VIEWPORT_PADDING_RATIO
    pad_lat = (lat_max - lat_min) * _VIEWPORT_PADDING_RATIO
    lon_min -= pad_lon
    lon_max += pad_lon
    lat_min = max(lat_min - pad_lat, -85.0)
    lat_max = min(lat_max + pad_lat, 85.0)
    return lon_min, lon_max, lat_min, lat_max


def _attribution_font_and_text(size: int = 15):
    """
    日本語グリフを持つフォントが見つかればそれを使い「出典：国土地理院」を、
    見つからなければPILの組み込みデフォルトフォントで英語表記
    "Source: GSI, Japan" を返す。(font, text) のタプル。
    """
    for path in _JP_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size), _ATTRIBUTION_TEXT_JA
            except Exception:
                continue
    from core.gis_render import _font
    return _font(size), _ATTRIBUTION_TEXT_FALLBACK


def _draw_attribution(draw: ImageDraw.ImageDraw, size: tuple) -> None:
    """国土地理院コンテンツ利用規約に基づく出典表示を右下に描く。"""
    w, h = size
    font, text = _attribution_font_and_text()
    margin = 6
    # 文字の視認性確保のため、半透明の背景帯を敷く
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.rectangle(
        [w - tw - margin * 3, h - th - margin * 3, w, h],
        fill=(255, 255, 255, 180),
    )
    draw.text((w - tw - margin * 2, h - th - margin * 2), text, fill=(30, 30, 30, 255), font=font)


async def render_overseas_map(session: aiohttp.ClientSession,
                               hypocenter_lonlat: tuple) -> Optional[bytes]:
    """
    震源が日本国外の場合向け：国土地理院の淡色地図タイルを背景に、日本の
    細分区域境界線と震源のバツ印を重ね描きしたPNG画像を返す。

    hypocenter_lonlat : (経度, 緯度)。震源不明の場合は呼び出し側で判定し、
        このプロパティ自体を呼ばないこと（本関数は必須パラメータとして扱う）。

    GIS_MAP_ENABLE=false、または細分区域データ未取得の場合は None。
    国土地理院タイルの取得に失敗した場合でも、区域線・震源マークのみの
    画像を返す（タイルは付加情報という位置づけ）。
    """
    if not _ready():
        return None

    lon, lat = hypocenter_lonlat
    if lon is None or lat is None or lon <= -180 or lat <= -90:
        return None

    shapes = get_local_area_shapes()
    if not shapes:
        return None

    try:
        bbox = _compute_overseas_viewport((lon, lat))
        lon_min, lon_max, lat_min, lat_max = bbox
        zoom = _choose_zoom(lon_min, lon_max, lat_min, lat_max)

        basemap, (origin_x, origin_y) = await _build_basemap(session, bbox, zoom)

        def project(plon: float, plat: float) -> tuple:
            px, py = _lonlat_to_mercator_px(plon, plat, zoom)
            return px - origin_x, py - origin_y

        overlay = Image.new("RGBA", basemap.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # 日本の細分区域境界線（Web Mercator投影で重ね描き。
        # core.gis_render側の等距円筒図法の投影関数は使わないこと）
        for shape in shapes.values():
            for ring in shape.rings:
                if len(ring) < 2:
                    continue
                pts = [project(plon, plat) for plon, plat in ring]
                draw.line(pts + [pts[0]], fill=_BOUNDARY_COLOR, width=_BOUNDARY_WIDTH)

        warn_rgb = _rgb_to_rgba(GIS_MAP_WARNING_COLOR)[:3]
        _draw_x_mark(draw, project(lon, lat), warn_rgb)

        composed = Image.alpha_composite(basemap.convert("RGBA"), overlay)
        _draw_attribution(ImageDraw.Draw(composed), composed.size)

        buf = io.BytesIO()
        composed.convert("RGB").save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.error("GIS地図(海外/国土地理院タイル)描画エラー", exc_info=True)
        return None
