"""
core/gis_render.py
===================
気象庁シェープファイル由来のGeoJSON（core/gis_data.py が管理）を用いて、
地震情報・EEW通知向けの簡易地図画像（PNG）をPillowで描画するモジュール
（GIS地図描画機能、試験導入。2026-09-13〜）。

【設計方針】
- 依存を増やさない：本プロジェクトは「軽量・低依存」方針（requirements.txt
  は discord.py/aiohttp/websockets/pygame/psutil のみ）のため、地図描画は
  Pillowのみを新規追加し、shapely/cartopy/matplotlib等の重量級ライブラリは
  使わない。緯度経度→ピクセル変換も自前の簡易な正距円筒図法（日本付近の
  東西縮尺を cos(緯度) で補正）で行う。穴（内側リング）を持つポリゴンは
  実データ確認済みで0件だったため、外側リングのみ描画する（実装を単純化
  するための意図的な割り切り。もし将来データが更新され穴付きポリゴンが
  混入した場合、穴の塗り漏れではなく「本来くり抜くべき部分も塗られる」形
  で表面化する）。
- 起動のたびの重い処理を避ける：194地域・56地域それぞれのポリゴンを
  ピクセル座標へ変換する処理はやや重いため、プロセス内で一度だけ実行して
  メモリ上にキャッシュする（_AreaSet.load() が遅延初期化・使い回しを行う）。
  ディスクへの中間キャッシュ（ベース画像等）は持たない設計とした
  （試験導入段階のため、まずはシンプルさを優先。Raspberry Pi 5であれば
  1回あたりの描画は数百ミリ秒程度に収まる想定）。

【対応する描画内容】
1. render_eew_warn_map()      : 緊急地震速報（警報）の発表地域（強い
                                 揺れが予想される地域）を塗りつぶし、
                                 震源にバツ印（PLUM法の場合はドーナツ）
2. render_shindo_map()        : 緊急地震速報（予報）・地震情報向け。
                                 地域ごとの震度色分け塗りつぶし、または
                                 観測点ごとの震度マーカー（どちらか一方、
                                 または両方同時に指定可）＋震源のバツ印
                                 （PLUM法の場合はドーナツ）

いずれも GIS_MAP_ENABLE=false、または必要な外部データが未取得の場合は
None を返す（呼び出し側＝各Cogは、Noneの場合は地図添付をスキップする
だけでよい。例外は投げない設計とし、地図描画の失敗が通知本体の送信を
妨げないようにする）。
"""
import io
import logging
import math
import threading
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from core.config import GIS_MAP_ENABLE, GIS_MAP_WARNING_COLOR
from core.constants import SHINDO_COLORS
from core.helpers import shindo_short_label
from core import gis_data

logger = logging.getLogger("QTLBot")

# ===============================
# 投影設定（簡易正距円筒図法）
# ===============================
# 実データのbbox（経度122.94〜148.89、緯度24.05〜45.56。沖縄〜北海道）に
# 少し余白を持たせた範囲。
_LON_MIN, _LON_MAX = 122.0, 150.0
_LAT_MIN, _LAT_MAX = 23.0, 46.0
_LAT0 = 35.0  # 東西方向の縮尺補正に使う基準緯度（本州中部付近）

_IMG_WIDTH = 900
# 東西1度あたりの実距離は cos(緯度) に比例して縮むため、南北のピクセル
# サイズをその分だけ小さくすることで、東西南北の縮尺を揃える
# （楕円体等は考慮しない簡易補正。試験導入機能として十分な精度とする）。
_IMG_HEIGHT = round(
    _IMG_WIDTH * (_LAT_MAX - _LAT_MIN) / ((_LON_MAX - _LON_MIN) * math.cos(math.radians(_LAT0)))
)

# 背景・境界線・図形の見た目
_BG_COLOR = (245, 245, 245, 255)          # 地図全体の背景（薄いグレー）
_BOUNDARY_COLOR = (170, 170, 170, 255)    # 区域境界線（細い）
_BOUNDARY_WIDTH = 1
_HIGHLIGHT_BORDER_WIDTH = 2               # 塗りつぶし区域の輪郭線の太さ
_FILL_ALPHA = 150                         # 塗りつぶしの不透明度（0-255）。
                                           # 隣接区域との境界が塗りつぶし後も
                                           # 見えるよう、輪郭線は別途不透明で
                                           # 描く（境界を見えるようにしたい、
                                           # という要望への対応）。
_HYPO_MARK_SIZE = 10                      # 震源バツ印の半径（px）
_HYPO_MARK_WIDTH = 3
_PLUM_OUTER_R = 14                        # PLUM法ドーナツの外半径（px）
_PLUM_INNER_R = 7                         # PLUM法ドーナツの内半径（px）
_STATION_MARKER_SIZE = 11                 # 観測点マーカー（正方形）の一辺（px）


def _project(lon: float, lat: float) -> tuple[float, float]:
    """経度緯度をキャンバス上のピクセル座標に変換する。"""
    x = (lon - _LON_MIN) / (_LON_MAX - _LON_MIN) * _IMG_WIDTH
    y = (_LAT_MAX - lat) / (_LAT_MAX - _LAT_MIN) * _IMG_HEIGHT
    return x, y


def _rgb_to_rgba(color: int, alpha: int = 255) -> tuple[int, int, int, int]:
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF, alpha)


def _polygon_rings(geometry: dict) -> list[list[tuple[float, float]]]:
    """
    GeoJSON の geometry（Polygon / MultiPolygon）から、描画に使う
    「外側リングのみ」のピクセル座標リストを取り出す（穴は無視。
    モジュールdocstring参照）。
    """
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return []

    if gtype == "Polygon":
        polygons = [coords]
    elif gtype == "MultiPolygon":
        polygons = coords
    else:
        return []

    rings = []
    for poly in polygons:
        if not poly:
            continue
        exterior = poly[0]  # 穴（poly[1:]）は無視
        rings.append([_project(lon, lat) for lon, lat in exterior])
    return rings


@dataclass
class _AreaShape:
    code: str
    name: str
    rings: list  # list[list[tuple[float,float]]]


class _AreaSet:
    """
    1種類のGeoJSON（府県予報区セット、または細分区域セット）を保持する。
    プロセス内で一度だけ読み込み・投影を行い、以降は使い回す
    （スレッドセーフのため簡易ロック付き。asyncio単一イベントループ内
    からの呼び出しが前提だが、念のため）。
    """

    def __init__(self, loader):
        self._loader = loader
        self._shapes: Optional[dict[str, _AreaShape]] = None
        self._lock = threading.Lock()

    def get(self) -> dict[str, _AreaShape]:
        if self._shapes is not None:
            return self._shapes
        with self._lock:
            if self._shapes is not None:
                return self._shapes
            shapes: dict[str, _AreaShape] = {}
            skipped = 0
            for feature in self._loader():
                props = feature.get("properties", {}) or {}
                name = props.get("name")
                if not name:
                    continue
                rings = _polygon_rings(feature.get("geometry"))
                if not rings:
                    skipped += 1
                    continue
                shapes[name] = _AreaShape(code=props.get("code", ""), name=name, rings=rings)
            if skipped:
                logger.debug(f"GIS地図: geometryが空のため{skipped}件の区域を読み込みスキップしました")
            self._shapes = shapes
            return shapes


_eew_areas = _AreaSet(gis_data.load_eew_areas)
_local_areas = _AreaSet(gis_data.load_local_areas)
_stations: Optional[dict[str, tuple[float, float]]] = None
_stations_lock = threading.Lock()


def _get_stations() -> dict[str, tuple[float, float]]:
    global _stations
    if _stations is not None:
        return _stations
    with _stations_lock:
        if _stations is None:
            _stations = gis_data.load_stations()
        return _stations


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Pillow<10.1系では load_default() に size 引数が無い
        return ImageFont.load_default()


def _draw_all_boundaries(draw: ImageDraw.ImageDraw, area_set: _AreaSet) -> None:
    """区域セット全体の輪郭線を薄く描画し、地図の下地とする。"""
    for shape in area_set.get().values():
        for ring in shape.rings:
            if len(ring) < 2:
                continue
            draw.line(ring + [ring[0]], fill=_BOUNDARY_COLOR, width=_BOUNDARY_WIDTH)


def _fill_area(draw: ImageDraw.ImageDraw, shape: _AreaShape, rgba_fill: tuple, rgb_border: tuple) -> None:
    """
    1つの区域を塗りつぶす（半透明フィル＋不透明の輪郭線）。
    隣接区域が同系色で塗られても境界が判別できるよう、輪郭線は
    フィルとは別に不透明で重ね描きする。
    """
    for ring in shape.rings:
        if len(ring) < 3:
            continue
        draw.polygon(ring, fill=rgba_fill)
    for ring in shape.rings:
        if len(ring) < 2:
            continue
        draw.line(ring + [ring[0]], fill=rgb_border, width=_HIGHLIGHT_BORDER_WIDTH)


def _draw_x_mark(draw: ImageDraw.ImageDraw, xy: tuple[float, float], rgb_color: tuple) -> None:
    x, y = xy
    s = _HYPO_MARK_SIZE
    draw.line([(x - s, y - s), (x + s, y + s)], fill=rgb_color, width=_HYPO_MARK_WIDTH)
    draw.line([(x - s, y + s), (x + s, y - s)], fill=rgb_color, width=_HYPO_MARK_WIDTH)


def _draw_plum_donut(draw: ImageDraw.ImageDraw, xy: tuple[float, float], rgb_color: tuple) -> None:
    """PLUM法（震源未確定）の震源マークとして、二重丸（ドーナツ）を描く。"""
    x, y = xy
    outer = _PLUM_OUTER_R
    inner = _PLUM_INNER_R
    draw.ellipse([x - outer, y - outer, x + outer, y + outer], outline=rgb_color, width=3)
    draw.ellipse([x - inner, y - inner, x + inner, y + inner], outline=rgb_color, width=2)


def _draw_hypocenter(draw: ImageDraw.ImageDraw, lon: Optional[float], lat: Optional[float],
                      is_plum: bool, rgb_color: tuple) -> None:
    if lon is None or lat is None:
        return
    if lon <= -180 or lat <= -90:  # -200等のダミー値（震源不明）を弾く
        return
    xy = _project(lon, lat)
    if is_plum:
        _draw_plum_donut(draw, xy, rgb_color)
    else:
        _draw_x_mark(draw, xy, rgb_color)


def _draw_station_markers(draw: ImageDraw.ImageDraw, station_shindo: dict) -> None:
    """
    観測点ごとの震度マーカー（色付き正方形＋短い震度ラベル）を描画する。
    station_shindo: {観測点名(stations.jsonのname): 震度コード(INT_MAPのキー)}
    stations.json 上に見つからない観測点名は描画をスキップする（ログのみ）。
    """
    stations = _get_stations()
    font = _font(11)
    half = _STATION_MARKER_SIZE / 2
    missing = 0
    for name, code in station_shindo.items():
        latlon = stations.get(name)
        if latlon is None:
            missing += 1
            continue
        lat, lon = latlon
        x, y = _project(lon, lat)
        rgb = _rgb_to_rgba(SHINDO_COLORS.get(code, SHINDO_COLORS[-1]))[:3]
        draw.rectangle([x - half, y - half, x + half, y + half], fill=rgb, outline=(40, 40, 40, 255))

        label = shindo_short_label(code)
        # テキストは黒固定（マーカー色が薄い場合でも視認性を確保するため）
        draw.text((x, y), label, fill=(20, 20, 20, 255), font=font, anchor="mm")

    if missing:
        logger.debug(f"GIS地図: stations.jsonに見つからない観測点を{missing}件スキップしました")


def _finalize(canvas: Image.Image) -> bytes:
    """RGBAキャンバスを背景と合成し、PNGバイト列にして返す。"""
    bg = Image.new("RGBA", canvas.size, _BG_COLOR)
    composed = Image.alpha_composite(bg, canvas)
    buf = io.BytesIO()
    composed.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _ready() -> bool:
    if not GIS_MAP_ENABLE:
        return False
    if not gis_data.gis_data_ready():
        logger.debug("GIS地図: 外部データが未取得のため描画をスキップします")
        return False
    return True


def render_eew_warn_map(
    warn_region_names: set,
    hypocenter_lonlat: Optional[tuple[float, float]] = None,
    is_plum: bool = False,
) -> Optional[bytes]:
    """
    緊急地震速報（警報）向け：発表地域（強い揺れが予想される地域。
    REGION_MAP変換後の府県予報区名の集合）を警戒色で塗りつぶし、震源に
    バツ印（PLUM法の場合はドーナツ）を描画したPNG画像を返す。

    warn_region_names : REGION_MAP変換後の名称の集合
        （core.eew_convert.extract_alert_regions() 等の出力をそのまま渡せる）
    hypocenter_lonlat  : (経度, 緯度)。震源不明の場合は None
    is_plum            : PLUM法（仮定震源要素）の場合 True

    GIS_MAP_ENABLE=false、または外部データ未取得の場合は None を返す。
    """
    if not _ready():
        return None
    try:
        shapes = _eew_areas.get()
        if not shapes:
            return None

        canvas = Image.new("RGBA", (_IMG_WIDTH, _IMG_HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        _draw_all_boundaries(draw, _eew_areas)

        fill_rgba = _rgb_to_rgba(GIS_MAP_WARNING_COLOR, _FILL_ALPHA)
        border_rgb = _rgb_to_rgba(GIS_MAP_WARNING_COLOR)[:3]
        matched = 0
        for region_name in warn_region_names:
            shape = shapes.get(region_name)
            if shape is None:
                logger.debug(f"GIS地図(EEW警報): 区域名 {region_name!r} がGeoJSON上に見つかりません")
                continue
            _fill_area(draw, shape, fill_rgba, border_rgb)
            matched += 1

        if hypocenter_lonlat:
            lon, lat = hypocenter_lonlat
            _draw_hypocenter(draw, lon, lat, is_plum, border_rgb)

        if matched == 0 and not hypocenter_lonlat:
            return None  # 描画すべき内容が何もなければ画像自体を作らない

        return _finalize(canvas)
    except Exception:
        logger.error("GIS地図(EEW警報)描画エラー", exc_info=True)
        return None


def render_shindo_map(
    region_shindo: Optional[dict] = None,
    station_shindo: Optional[dict] = None,
    hypocenter_lonlat: Optional[tuple[float, float]] = None,
    is_plum: bool = False,
) -> Optional[bytes]:
    """
    緊急地震速報（予報）・地震情報向け：地域ごとの震度色分け塗りつぶし、
    または観測点ごとの震度マーカーを描画したPNG画像を返す
    （どちらか一方、または両方同時に指定可）。

    region_shindo      : {区域名(REGION_MAP変換前の生の名称): 震度コード}
        区域名は core.constants.INT_MAP のキー（10,20,...,70,-1）に対応する
        震度コードで指定する。EEWのWarnAreaならShindo1/Shindo2文字列を
        core.helpers.shindo_code_from_label() で変換して渡すこと。
    station_shindo      : {観測点名(stations.jsonのname): 震度コード}
        地震情報の points[] 等、観測点ごとの震度が得られる場合に使う。
    hypocenter_lonlat   : (経度, 緯度)。震源不明の場合は None
    is_plum             : PLUM法の場合 True

    GIS_MAP_ENABLE=false、または外部データ未取得の場合は None を返す。
    """
    if not _ready():
        return None
    try:
        shapes = _local_areas.get()
        if not shapes:
            return None

        canvas = Image.new("RGBA", (_IMG_WIDTH, _IMG_HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        _draw_all_boundaries(draw, _local_areas)

        drawn = 0
        if region_shindo:
            for region_name, code in region_shindo.items():
                shape = shapes.get(region_name)
                if shape is None:
                    logger.debug(f"GIS地図(震度分布): 区域名 {region_name!r} がGeoJSON上に見つかりません")
                    continue
                color = SHINDO_COLORS.get(code, SHINDO_COLORS[-1])
                fill_rgba = _rgb_to_rgba(color, _FILL_ALPHA)
                border_rgb = _rgb_to_rgba(color)[:3]
                _fill_area(draw, shape, fill_rgba, border_rgb)
                drawn += 1

        if station_shindo:
            _draw_station_markers(draw, station_shindo)
            drawn += len(station_shindo)

        if hypocenter_lonlat:
            lon, lat = hypocenter_lonlat
            warn_rgb = _rgb_to_rgba(GIS_MAP_WARNING_COLOR)[:3]
            _draw_hypocenter(draw, lon, lat, is_plum, warn_rgb)
            drawn += 1

        if drawn == 0:
            return None

        return _finalize(canvas)
    except Exception:
        logger.error("GIS地図(震度分布)描画エラー", exc_info=True)
        return None
