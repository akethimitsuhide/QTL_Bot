"""
core/gis_tile_render.py
========================
遠地地震（震源が日本国外）向けに、国土地理院（GSI）の淡色地図タイルを
背景に、世界の国境データ（countries.geojson、日本を除く）・日本の
細分区域境界線（core.gis_render が保持する AreaForecastLocalE_GIS
データを再利用）・震源のバツ印を重ね描きしたPNG画像を生成するモジュール
（2026-09-15追加、countries.geojsonの重ね合わせは2026-09-17追加）。

【「震源が日本国外」の判定方法】
core.gis_render.is_outside_japan_bbox(lon, lat) が、震源の緯度経度が
core.gis_render の「日本全体」表示範囲（_JAPAN_LON_MIN/MAX,
_JAPAN_LAT_MIN/MAX＝およそ経度122〜155度・緯度23〜46度の固定の
バウンディングボックス）の外にあるかどうかで判定する（単純な範囲外
判定であり、実際の国境線やJMAの発表種別コード等は見ていない）。
呼び出し元（cogs/quake.py の _render_quake_gis_map、
cogs/other.py の _render_hypocenter_gis_map）が、通知に含まれる震源の
緯度経度に対してこの関数を呼び、Trueなら本モジュールの
render_overseas_map() に、Falseなら core.gis_render 側の通常の
ベクター地図に処理を振り分ける。

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
ため、日本の区域境界線・国境データ・震源マークの重ね描きも必ずこの
モジュール内のWeb Mercator投影で行う（core.gis_render側の投影関数
自体は使わないが、_LAND_COLOR/_SEA_COLOR/_BOUNDARY_COLOR等の配色
定数、および_extract_rings/_rings_bbox/_BBoxといったGeoJSONパース用の
汎用ヘルパーはcore.gis_renderから再利用している）。

【レイヤー構成（render_overseas_map()のdocstring参照）】
国土地理院タイルは日本国内データが中心で、海外（震源が遠地の場合の
相手国）の陸地についてはタイルが取得できない／内容が薄いことがある。
そこで、海色の背景→世界の国境データ（countries.geojson、日本を除く。
陸地色で塗りつぶし）→国土地理院タイル（取得できた範囲のみ。空白は
透明）→日本の区域境界線（線のみ）→震源マーク、の順に重ねることで、
国土地理院タイルが無い／薄い海外地域でも陸地か海かが常に判別できる
ようにしている。

【タイル取得とキャッシュ】
- 淡色地図: https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png
- 表示範囲（日本全体 ∪ 震源）を収める最も詳細なズームレベルを、
  最大タイル数（_MAX_TILES）の範囲内で選ぶ（画像サイズ・リクエスト数を
  抑えるため）。
- タイル画像はズーム/x/y ごとに GIS_MAP_DATA_DIR/tiles/ 配下へ永続
  キャッシュする（地図タイルは内容が変化しない静的データのため）。
  国境データ（countries.geojson）は core/gis_data.py が管理する他の
  GeoJSONと同様、GIS_MAP_DATA_DIR 直下にキャッシュされる。
- 取得失敗（404・タイムアウト等）のタイルは1枚ずつ透明のまま（背後の
  国境データ・海色が透けて見える）とし、処理全体は継続する。
- 国土地理院コンテンツ利用規約に基づき、画像内に出典表示を必ず入れる。
  本モジュールは地理院タイルに国境データ・区域境界線・震源マークを重ねて
  加工しているため、「加工して作成」の旨も併記する（2026-09-22追加）。

GIS_MAP_ENABLE=false、または外部データ（細分区域・国境データ等）
未取得の場合は None を返す。タイル取得に全面的に失敗した場合でも、
国境データ・区域線・震源マークのみの画像は返す（国土地理院タイルは
「あれば表示する」付加情報という位置づけ）。
"""
import asyncio
import io
import logging
import math
import os
import threading
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from core.config import GIS_MAP_DATA_DIR, GIS_MAP_WARNING_COLOR
from core.gis_render import (
    _ready, _rgb_to_rgba, _draw_x_mark,
    get_local_area_shapes,
    _JAPAN_LON_MIN, _JAPAN_LON_MAX, _JAPAN_LAT_MIN, _JAPAN_LAT_MAX,
    _BOUNDARY_COLOR, _BOUNDARY_WIDTH, _LAND_COLOR, _SEA_COLOR,
    _extract_rings, _rings_bbox, _BBox,
    _HYPO_MARK_SIZE, _HYPO_MARK_OUTLINE_PAD,
)
from core import gis_data

logger = logging.getLogger("QTLBot")

GSI_PALE_TILE_URL = "https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png"
_TILE_SIZE = 256
_MAX_TILES = 64            # 1回の描画で取得するタイル数の上限（サーバー負荷・処理時間対策）
_MIN_ZOOM, _MAX_ZOOM = 1, 9
_TILE_FETCH_TIMEOUT = 8
_TILE_FETCH_CONCURRENCY = 6
_VIEWPORT_PADDING_RATIO = 0.15   # 日本+震源のbboxに対する余白比率

# 【2026-09-22 変更】地理院タイルを加工（他データの重ね描き）して作成した
# 画像であることを出典表示に明記する。画像幅が足りない場合は
# _ATTRIBUTION_TEXT_JA_SHORT へ、日本語フォントが無い環境では
# _ATTRIBUTION_TEXT_FALLBACK へフォールバックする。
_ATTRIBUTION_TEXT_JA = "出典：国土地理院（地理院タイル〈淡色地図〉を加工して作成）"
_ATTRIBUTION_TEXT_JA_SHORT = "出典：国土地理院（加工して作成）"
_ATTRIBUTION_TEXT_FALLBACK = "Source: GSI, Japan (pale tiles, modified)"  # 日本語グリフを持つフォントが無い環境向け

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


# ===============================
# 世界の国境データ（countries.geojson、2026-09-17追加）
# ===============================
# 国土地理院タイルは日本国内データが中心のため、海外（震源が遠地の場合の
# 相手国）の陸地についてはタイルが取得できない／内容が薄いことがある。
# その場合に陸地か海か判別すらできず見づらくなるのを防ぐため、世界の
# 国境データ（datasets/geo-countries）を背景の下地として使う。日本自体は
# 自前のGeoJSON（AreaForecastLocalE_GIS）の方が精密なため、この国境
# データからは除外する（プロパティ name が "Japan" のfeatureをスキップ）。
_countries_cache: Optional[list] = None
_countries_lock = threading.Lock()


def _get_countries() -> list:
    """
    (rings_bbox, rings) のリストを返す（日本を除く）。プロセス内で
    一度だけパースし、以降はキャッシュを再利用する
    （core.gis_render._AreaSet と同じ考え方）。
    """
    global _countries_cache
    if _countries_cache is not None:
        return _countries_cache
    with _countries_lock:
        if _countries_cache is not None:
            return _countries_cache
        countries = []
        skipped = 0
        for feature in gis_data.load_countries():
            props = feature.get("properties", {}) or {}
            if props.get("name") == "Japan":
                continue
            rings = _extract_rings(feature.get("geometry"), kind="polygon")
            bbox = _rings_bbox(rings) if rings else None
            if not rings or bbox is None:
                skipped += 1
                continue
            countries.append((bbox, rings))
        if skipped:
            logger.debug(f"GIS地図(海外): geometryが空のため{skipped}件の国をスキップしました")
        logger.debug(f"GIS地図(海外): 国境データを{len(countries)}件読み込みました（日本を除く）")
        _countries_cache = countries
        return _countries_cache


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
    # 2026-09-17: 取得できなかったタイルは不透明の灰色ではなく透明にし、
    # 下に敷く国境データ（陸地）・海色のレイヤーが透けて見えるようにする
    # （国土地理院タイルが海外のデータを持たない場合の見やすさ対策）。
    tile_canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

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
    日本語グリフを持つフォントが見つかればそれを使い「出典：国土地理院
    （…加工して作成）」を、見つからなければPILの組み込みデフォルト
    フォントで英語表記を返す。(font, text) のタプル。
    """
    for path in _JP_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size), _ATTRIBUTION_TEXT_JA
            except Exception:
                continue
    from core.gis_render import _font
    return _font(size), _ATTRIBUTION_TEXT_FALLBACK


# 出典帯を置く候補位置（優先順）。右下がデフォルト（従来通り）で、
# 震源のバツ印と重なる場合のみ他の角へずらす（2026-09-22追加）。
_ATTRIBUTION_CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")


def _attribution_rect(corner: str, w: int, h: int, tw: float, th: float, margin: int) -> tuple:
    """出典帯の矩形 [x0, y0, x1, y1] を角の指定から算出する。"""
    box_w, box_h = tw + margin * 3, th + margin * 3
    if corner == "bottom-right":
        return (w - box_w, h - box_h, w, h)
    if corner == "bottom-left":
        return (0, h - box_h, box_w, h)
    if corner == "top-right":
        return (w - box_w, 0, w, box_h)
    return (0, 0, box_w, box_h)  # top-left


def _rect_intersects_circle(rect: tuple, cx: float, cy: float, radius: float) -> bool:
    """矩形と円（震源バツ印を囲む簡易的な当たり判定用の円）が重なるか。"""
    x0, y0, x1, y1 = rect
    nearest_x = min(max(cx, x0), x1)
    nearest_y = min(max(cy, y0), y1)
    return (cx - nearest_x) ** 2 + (cy - nearest_y) ** 2 <= radius ** 2


def _draw_attribution(draw: ImageDraw.ImageDraw, size: tuple,
                       avoid_xy: Optional[tuple] = None) -> None:
    """
    国土地理院コンテンツ利用規約に基づく出典表示を描く（既定は右下）。

    avoid_xy : 震源のバツ印の描画位置（px）。指定された場合、出典帯が
        バツ印と重なって読めなくなる・バツ印を隠してしまうのを避けるため、
        重なりが生じる角を避けて別の角へずらす（2026-09-22追加。日本の
        遠地――特にUSGS由来の海外地震情報で、震源が右下付近になり
        出典表示と重なって両方とも見えづらくなる報告があったための対応）。
        すべての角で重なる場合（画像が極端に小さい等）は、従来通り
        右下に描画する（出典表示自体を省略すると規約違反になるため）。
    """
    w, h = size
    font, text = _attribution_font_and_text()
    margin = 6
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # 画像幅に収まらない場合（表示範囲が狭くタイル枚数が少ない等）は、
    # 「加工して作成」は残したまま短い文言に切り替える。
    if text == _ATTRIBUTION_TEXT_JA and tw + margin * 3 > w:
        text = _ATTRIBUTION_TEXT_JA_SHORT
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    corner = _ATTRIBUTION_CORNERS[0]
    if avoid_xy is not None:
        cx, cy = avoid_xy
        # バツ印本体（白フチ込み）に、文字が隣接していても読める程度の
        # 余白を足した半径を当たり判定に使う。
        radius = _HYPO_MARK_SIZE + _HYPO_MARK_OUTLINE_PAD + margin * 2
        for candidate in _ATTRIBUTION_CORNERS:
            rect = _attribution_rect(candidate, w, h, tw, th, margin)
            if not _rect_intersects_circle(rect, cx, cy, radius):
                corner = candidate
                break

    x0, y0, x1, y1 = _attribution_rect(corner, w, h, tw, th, margin)
    draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 255, 180))
    draw.text((x1 - tw - margin, y1 - th - margin), text, fill=(30, 30, 30, 255), font=font)


async def render_overseas_map(session: aiohttp.ClientSession,
                               hypocenter_lonlat: tuple) -> Optional[bytes]:
    """
    震源が日本国外の場合向け：国土地理院の淡色地図タイルを背景に、
    世界の国境データ（日本を除く。countries.geojson）・日本の細分区域
    境界線・震源のバツ印を重ね描きしたPNG画像を返す。

    レイヤー構成（背面から前面）:
      1. 海色の背景（_SEA_COLOR。core.gis_renderの独自ベクター地図と
         共通の配色）
      2. 世界の国境データ（日本を除く）を陸地色（_LAND_COLOR）で塗り
         つぶした下地（2026-09-17追加。国土地理院タイルが海外のデータを
         十分に持たない場合でも、陸地か海かが常に判別できるようにする
         ための保険。国土地理院タイルが実際に取得できた範囲は3の
         レイヤーがこの上から完全に覆うため、両方のデータが「二重に」
         見えることはない）
      3. 国土地理院タイル（取得できた範囲のみ。失敗したタイルは透明の
         ままなので、2のレイヤーがそのまま透けて見える）
      4. 日本の細分区域境界線（塗りつぶしはせず線のみ。日本については
         3の実際の地図タイルをそのまま見せ、行政区分の参考として線だけ
         重ねる）
      5. 震源のバツ印
      6. 出典表示

    hypocenter_lonlat : (経度, 緯度)。震源不明の場合は呼び出し側で判定し、
        このプロパティ自体を呼ばないこと（本関数は必須パラメータとして扱う）。

    GIS_MAP_ENABLE=false、または外部データ（細分区域・国境データ等）
    未取得の場合は None。国土地理院タイルの取得に失敗した場合でも、
    国境データ・区域線・震源マークだけの画像は返す。
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

        # 【2026-09-25修正】_compute_overseas_viewportは、日付変更線をまたぐ
        # 表示範囲（南北アメリカ等、震源の経度が西経で、太平洋を挟んだ東回り
        # の方が表示範囲が短くなるケース）で、震源の経度を+360（または-360）
        # シフトした値をbboxの算出に使うことがある（例: コロンビア
        # lon=-74.1 → 実際にはlon_min=97.4〜lon_max=310.5という、+360した
        # 286.9相当の範囲が選ばれる）。しかし後段でバツ印・出典表示の位置を
        # 求めるproject(lon, lat)には、このシフトを反映していない元の生の
        # 経度（-74.1）をそのまま渡していたため、算出されるピクセル座標が
        # キャンバス範囲外（西へ丸々1周分ずれた位置）になり、バツ印が
        # 描画されない（または端にわずかに掛かるだけで小さく見える）
        # 不具合があった。viewportの範囲[lon_min, lon_max]に収まるよう、
        # 震源の経度を同じ±360シフトで正規化してから使う。
        draw_lon = lon
        for candidate in (lon, lon + 360, lon - 360):
            if lon_min <= candidate <= lon_max:
                draw_lon = candidate
                break

        tile_layer, (origin_x, origin_y) = await _build_basemap(session, bbox, zoom)
        canvas_size = tile_layer.size

        def project(plon: float, plat: float) -> tuple:
            px, py = _lonlat_to_mercator_px(plon, plat, zoom)
            return px - origin_x, py - origin_y

        # 1. 海色の背景
        composed = Image.new("RGBA", canvas_size, _SEA_COLOR)

        # 2. 世界の国境データ（日本を除く）
        viewport_bbox = _BBox(lon_min, lon_max, lat_min, lat_max)
        country_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        cdraw = ImageDraw.Draw(country_layer)
        for country_bbox, rings in _get_countries():
            if not country_bbox.intersects(viewport_bbox):
                continue
            for ring in rings:
                if len(ring) < 3:
                    continue
                pts = [project(plon, plat) for plon, plat in ring]
                cdraw.polygon(pts, fill=_LAND_COLOR)
        composed = Image.alpha_composite(composed, country_layer)

        # 3. 国土地理院タイル（取得できた範囲のみ。空白は透明なので2が透ける）
        composed = Image.alpha_composite(composed, tile_layer)

        # 4・5. 日本の区域境界線＋震源マーク
        overlay = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
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
        _draw_x_mark(draw, project(draw_lon, lat), warn_rgb)

        composed = Image.alpha_composite(composed, overlay)

        # 6. 出典表示（震源のバツ印と重なる場合は別の角へずらす）
        _draw_attribution(ImageDraw.Draw(composed), composed.size, avoid_xy=project(draw_lon, lat))

        buf = io.BytesIO()
        composed.convert("RGB").save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.error("GIS地図(海外/国土地理院タイル)描画エラー", exc_info=True)
        return None
