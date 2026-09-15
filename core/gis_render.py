"""
core/gis_render.py
===================
気象庁シェープファイル由来のGeoJSON（core/gis_data.py が管理）を用いて、
地震情報・EEW・津波情報向けの簡易地図画像（PNG）をPillowで描画するモジュール
（GIS地図描画機能、試験導入。2026-09-13〜、津波対応・自動ズームは2026-09-14〜）。

【設計方針】
- 依存を増やさない：本プロジェクトは「軽量・低依存」方針（requirements.txt
  は discord.py/aiohttp/websockets/pygame/psutil のみ）のため、地図描画は
  Pillowのみを新規追加し、shapely/cartopy/matplotlib等の重量級ライブラリは
  使わない。緯度経度→ピクセル変換も自前の簡易な正距円筒図法（表示範囲の
  中心緯度で cos(緯度) 補正）で行う。穴（内側リング）を持つポリゴンは
  実データ確認済みで0件だったため、外側リングのみ描画する（実装を単純化
  するための意図的な割り切り）。
- 起動のたびの重い処理を避ける：GeoJSONのパース結果（区域名→生の緯度経度
  リング＋bbox）はプロセス内で一度だけ行いメモリ上にキャッシュする
  （_AreaSet.get() が遅延初期化・使い回しを行う）。ピクセルへの投影自体は
  表示範囲（後述の自動ズーム）ごとに変わるため、描画のたびに行う
  （_Projector）。ディスクへの中間キャッシュ（ベース画像等）は持たない。

【自動ズーム（2026-09-14追加）】
描画対象（塗りつぶし区域・観測点・震源）の緯度経度から表示範囲を動的に
計算し、対象が日本の一部にとどまる場合はその周辺だけを拡大して描画する
（余白は表示範囲の35%、最小表示範囲は2度四方を確保）。対象が広範囲、
または何も無い場合は日本全体にフォールバックする（_compute_viewport）。
表示範囲外の区域境界線は描画自体をスキップし、負荷を抑える。

【対応する描画内容】
1. render_eew_warn_map()  : 緊急地震速報（警報）の発表地域（強い揺れが
                             予想される地域）を塗りつぶし、震源にバツ印
                             （PLUM法の場合はドーナツ、いずれも視認性の
                             ため白フチ付き）
2. render_shindo_map()    : 緊急地震速報（予報）・地震情報向け。地域
                             ごとの震度色分け塗りつぶし、または観測点
                             ごとの震度マーカー（どちらか一方、または
                             両方同時に指定可）＋震源のバツ印/ドーナツ
3. render_tsunami_map()   : 津波情報向け。津波予報区の沿岸線
                             （ポリゴンではなく線データ）を警報種別の
                             色で塗り分け（2026-09-14追加）

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

from core.config import (
    GIS_MAP_ENABLE, GIS_MAP_WARNING_COLOR,
    TSUNAMI_COLOR_MAJOR_WARNING, TSUNAMI_COLOR_WARNING,
    TSUNAMI_COLOR_WATCH, TSUNAMI_COLOR_UNKNOWN,
)
from core.constants import SHINDO_COLORS
from core.helpers import shindo_short_label
from core import gis_data

logger = logging.getLogger("QTLBot")

# ===============================
# 表示範囲（日本全体、フォールバック用）
# ===============================
# 実データのbbox（地震系: 経度122.94〜148.89 / 津波系: 経度122.94〜153.99
# 〈小笠原諸島含む〉、緯度24.05〜45.56）に少し余白を持たせた範囲。
_JAPAN_LON_MIN, _JAPAN_LON_MAX = 122.0, 155.0
_JAPAN_LAT_MIN, _JAPAN_LAT_MAX = 23.0, 46.0

# ===============================
# 自動ズーム設定
# ===============================
_VIEWPORT_PADDING_RATIO = 0.35   # 対象のbboxに対して、上下左右に足す余白の比率
_VIEWPORT_MIN_SPAN_DEG = 2.0     # 最小表示範囲（これより小さくズームしない）

_BASE_DIM = 900     # 基準の画像サイズ（px）。表示範囲が横長・縦長どちらでも
_MAX_DIM = 1400     # 長辺がこのpxを超えないよう、短辺を基準にして縮尺を決める

# 背景・境界線・図形の見た目
_BG_COLOR = (245, 245, 245, 255)          # 地図全体の背景（薄いグレー）
_BOUNDARY_COLOR = (110, 110, 110, 255)     # 区域境界線（2026-09-15: 見づらいとの指摘で濃くした。旧: (170,170,170,255)）
_BOUNDARY_WIDTH = 1
_HIGHLIGHT_BORDER_WIDTH = 2               # 塗りつぶし区域の輪郭線の太さ
_FILL_ALPHA = 150                         # 塗りつぶしの不透明度（0-255）。
                                           # 隣接区域との境界が塗りつぶし後も
                                           # 見えるよう、輪郭線は別途不透明で
                                           # 描く。
_HALO_COLOR = (255, 255, 255, 255)        # 震源マークの視認性向上用の白フチ
_HALO_OUTLINE_COLOR = (90, 90, 90, 255)   # 白フチ自体の輪郭（薄い背景に対する視認性確保）
_HYPO_MARK_SIZE = 10                      # 震源バツ印の半径（px）
_HYPO_MARK_WIDTH = 3
_HYPO_HALO_PAD = 5                        # バツ印の白フチの余白（px）
_PLUM_OUTER_R = 14                        # PLUM法ドーナツの外半径（px）
_PLUM_INNER_R = 7                         # PLUM法ドーナツの内半径（px）
_PLUM_HALO_PAD = 4                        # ドーナツの白フチの余白（px）
_STATION_MARKER_SIZE = 11                 # 観測点マーカー（正方形）の一辺（px）
_TSUNAMI_LINE_WIDTH = 6                   # 津波予報区の色付き沿岸線の太さ（px）


def _rgb_to_rgba(color: int, alpha: int = 255) -> tuple[int, int, int, int]:
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF, alpha)


@dataclass
class _BBox:
    lon_min: float
    lon_max: float
    lat_min: float
    lat_max: float

    def intersects(self, other: "_BBox") -> bool:
        return not (
            self.lon_max < other.lon_min or self.lon_min > other.lon_max or
            self.lat_max < other.lat_min or self.lat_min > other.lat_max
        )


def _japan_bbox() -> _BBox:
    return _BBox(_JAPAN_LON_MIN, _JAPAN_LON_MAX, _JAPAN_LAT_MIN, _JAPAN_LAT_MAX)


def is_outside_japan_bbox(lon: float, lat: float) -> bool:
    """
    緯度経度が本モジュールの「日本全体」表示範囲の外にあるかを返す。
    core.gis_tile_render（国土地理院タイルとの重ね合わせ、2026-09-15
    追加）が、震源が海外かどうかの判定＝表示モード切り替えのトリガーに
    使う。
    """
    return not (
        _JAPAN_LON_MIN <= lon <= _JAPAN_LON_MAX and
        _JAPAN_LAT_MIN <= lat <= _JAPAN_LAT_MAX
    )


def get_local_area_shapes() -> dict:
    """
    細分区域（AreaForecastLocalE_GIS、194件）の生の緯度経度リングデータを
    返す公開アクセサ。core.gis_tile_render が、国土地理院タイルの上に
    日本の区域境界線を重ね描きする際に、GeoJSONの再パースを避けて
    このモジュールのキャッシュ済みデータを再利用するために使う。
    """
    return _local_areas.get()


class _Projector:
    """特定の表示範囲（_BBox）・画像サイズに対する緯度経度→ピクセル変換。"""

    def __init__(self, bbox: _BBox, width: int, height: int):
        self.bbox = bbox
        self.width = width
        self.height = height

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        b = self.bbox
        x = (lon - b.lon_min) / (b.lon_max - b.lon_min) * self.width
        y = (b.lat_max - lat) / (b.lat_max - b.lat_min) * self.height
        return x, y

    def project_ring(self, ring: list) -> list:
        return [self.project(lon, lat) for lon, lat in ring]


def _dimensions_for_bbox(bbox: _BBox) -> tuple[int, int]:
    """
    表示範囲（bbox）に対して、東西南北の縮尺が揃う（distortionが出ない）
    画像サイズを決める。長辺が _MAX_DIM を超えないよう、範囲の形状（横長/
    縦長）に応じて基準となる辺を切り替える。
    """
    lat0 = (bbox.lat_min + bbox.lat_max) / 2
    coslat0 = max(math.cos(math.radians(lat0)), 0.1)
    lon_span = max(bbox.lon_max - bbox.lon_min, 0.01)
    lat_span = max(bbox.lat_max - bbox.lat_min, 0.01)

    height_for_base_width = _BASE_DIM * lat_span / (lon_span * coslat0)
    if height_for_base_width <= _MAX_DIM:
        width = _BASE_DIM
        height = height_for_base_width
    else:
        height = _MAX_DIM
        width = height * lon_span * coslat0 / lat_span

    width = max(round(min(width, _MAX_DIM)), 200)
    height = max(round(min(height, _MAX_DIM)), 200)
    return width, height


def _compute_viewport(points: list) -> _BBox:
    """
    描画対象（区域ポリゴンの全頂点・観測点・震源等）の緯度経度リストから、
    自動ズーム後の表示範囲を計算する。対象が無い場合、範囲が異常な場合、
    計算結果が日本全体の範囲を超える場合は日本全体にフォールバックする。
    """
    if not points:
        return _japan_bbox()

    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    lon_min, lon_max = min(lons), max(lons)
    lat_min, lat_max = min(lats), max(lats)

    lon_span = lon_max - lon_min
    lat_span = lat_max - lat_min
    pad_lon = max(lon_span * _VIEWPORT_PADDING_RATIO, _VIEWPORT_MIN_SPAN_DEG * 0.3)
    pad_lat = max(lat_span * _VIEWPORT_PADDING_RATIO, _VIEWPORT_MIN_SPAN_DEG * 0.3)
    lon_min -= pad_lon
    lon_max += pad_lon
    lat_min -= pad_lat
    lat_max += pad_lat

    if lon_max - lon_min < _VIEWPORT_MIN_SPAN_DEG:
        c = (lon_min + lon_max) / 2
        lon_min, lon_max = c - _VIEWPORT_MIN_SPAN_DEG / 2, c + _VIEWPORT_MIN_SPAN_DEG / 2
    if lat_max - lat_min < _VIEWPORT_MIN_SPAN_DEG:
        c = (lat_min + lat_max) / 2
        lat_min, lat_max = c - _VIEWPORT_MIN_SPAN_DEG / 2, c + _VIEWPORT_MIN_SPAN_DEG / 2

    # 日本全体の範囲でクランプする（対象が広範囲すぎる場合は自然に
    # 日本全体表示になる。対象が範囲外＝データ異常な場合の暴走も防ぐ）。
    lon_min = max(lon_min, _JAPAN_LON_MIN)
    lon_max = min(lon_max, _JAPAN_LON_MAX)
    lat_min = max(lat_min, _JAPAN_LAT_MIN)
    lat_max = min(lat_max, _JAPAN_LAT_MAX)

    if lon_min >= lon_max or lat_min >= lat_max:
        return _japan_bbox()
    return _BBox(lon_min, lon_max, lat_min, lat_max)


def _extract_rings(geometry: dict, kind: str) -> list:
    """
    GeoJSON の geometry から、描画に使う生の緯度経度リングを取り出す。
    kind="polygon" : Polygon / MultiPolygon の外側リングのみ（穴は無視）。
    kind="line"    : LineString / MultiLineString の各パス。
    """
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return []

    if kind == "polygon":
        if gtype == "Polygon":
            polygons = [coords]
        elif gtype == "MultiPolygon":
            polygons = coords
        else:
            return []
        rings = []
        for poly in polygons:
            if poly:
                rings.append([(lon, lat) for lon, lat in poly[0]])  # poly[1:]＝穴は無視
        return rings

    if kind == "line":
        if gtype == "LineString":
            paths = [coords]
        elif gtype == "MultiLineString":
            paths = coords
        else:
            return []
        return [[(lon, lat) for lon, lat in path] for path in paths if path]

    return []


def _rings_bbox(rings: list) -> Optional[_BBox]:
    lon_min = lat_min = math.inf
    lon_max = lat_max = -math.inf
    for ring in rings:
        for lon, lat in ring:
            lon_min = min(lon_min, lon)
            lon_max = max(lon_max, lon)
            lat_min = min(lat_min, lat)
            lat_max = max(lat_max, lat)
    if lon_min > lon_max:
        return None
    return _BBox(lon_min, lon_max, lat_min, lat_max)


@dataclass
class _AreaShape:
    code: str
    name: str
    rings: list  # list[list[(lon,lat)]]（生の緯度経度。投影は描画時に行う）
    bbox: _BBox


class _AreaSet:
    """
    1種類のGeoJSON（府県予報区・細分区域・津波予報区のいずれか）を保持する。
    プロセス内で一度だけ読み込みを行い、以降は使い回す（スレッドセーフの
    ため簡易ロック付き。asyncio単一イベントループ内からの呼び出しが前提
    だが、念のため）。
    """

    def __init__(self, loader, kind: str):
        self._loader = loader
        self._kind = kind
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
                rings = _extract_rings(feature.get("geometry"), self._kind)
                bbox = _rings_bbox(rings) if rings else None
                if not rings or bbox is None:
                    skipped += 1
                    continue
                shapes[name] = _AreaShape(code=props.get("code", ""), name=name, rings=rings, bbox=bbox)
            if skipped:
                logger.debug(f"GIS地図: geometryが空のため{skipped}件の区域を読み込みスキップしました")
            self._shapes = shapes
            return shapes


_eew_areas = _AreaSet(gis_data.load_eew_areas, kind="polygon")
_local_areas = _AreaSet(gis_data.load_local_areas, kind="polygon")
_tsunami_areas = _AreaSet(gis_data.load_tsunami_areas, kind="line")
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


def _draw_polygon_boundaries(draw: ImageDraw.ImageDraw, area_set: _AreaSet,
                              projector: _Projector, viewport: _BBox) -> None:
    """区域セット全体の輪郭線を薄く描画し、地図の下地とする（ポリゴン用、閉路）。
    表示範囲と交差しない区域は描画自体をスキップする（負荷軽減）。"""
    for shape in area_set.get().values():
        if not shape.bbox.intersects(viewport):
            continue
        for ring in shape.rings:
            if len(ring) < 2:
                continue
            pts = projector.project_ring(ring)
            draw.line(pts + [pts[0]], fill=_BOUNDARY_COLOR, width=_BOUNDARY_WIDTH)


def _draw_line_boundaries(draw: ImageDraw.ImageDraw, area_set: _AreaSet,
                           projector: _Projector, viewport: _BBox) -> None:
    """区域セット全体の輪郭線を薄く描画する（線データ用、開いたパス。津波予報区）。"""
    for shape in area_set.get().values():
        if not shape.bbox.intersects(viewport):
            continue
        for ring in shape.rings:
            if len(ring) < 2:
                continue
            pts = projector.project_ring(ring)
            draw.line(pts, fill=_BOUNDARY_COLOR, width=_BOUNDARY_WIDTH)


def _fill_area(draw: ImageDraw.ImageDraw, shape: _AreaShape, projector: _Projector,
               rgba_fill: tuple, rgb_border: tuple) -> None:
    """
    1つの区域を塗りつぶす（半透明フィル＋不透明の輪郭線）。
    隣接区域が同系色で塗られても境界が判別できるよう、輪郭線は
    フィルとは別に不透明で重ね描きする。
    """
    for ring in shape.rings:
        if len(ring) < 3:
            continue
        draw.polygon(projector.project_ring(ring), fill=rgba_fill)
    for ring in shape.rings:
        if len(ring) < 2:
            continue
        pts = projector.project_ring(ring)
        draw.line(pts + [pts[0]], fill=rgb_border, width=_HIGHLIGHT_BORDER_WIDTH)


def _draw_line_highlight(draw: ImageDraw.ImageDraw, shape: _AreaShape, projector: _Projector,
                          rgb_color: tuple) -> None:
    """1つの津波予報区の沿岸線を太い色付き線で強調表示する。"""
    for ring in shape.rings:
        if len(ring) < 2:
            continue
        pts = projector.project_ring(ring)
        draw.line(pts, fill=rgb_color, width=_TSUNAMI_LINE_WIDTH, joint="curve")


def _draw_x_mark(draw: ImageDraw.ImageDraw, xy: tuple[float, float], rgb_color: tuple) -> None:
    """
    震源のバツ印を描く。地図の塗り色（赤系）に紛れて見づらくなるのを
    防ぐため、白い丸フチ（グレーの輪郭付き）を下地に敷いてから描画する
    （2026-09-14追加）。
    """
    x, y = xy
    s = _HYPO_MARK_SIZE
    halo_r = s + _HYPO_HALO_PAD
    draw.ellipse([x - halo_r, y - halo_r, x + halo_r, y + halo_r],
                 fill=_HALO_COLOR, outline=_HALO_OUTLINE_COLOR, width=1)
    draw.line([(x - s, y - s), (x + s, y + s)], fill=rgb_color, width=_HYPO_MARK_WIDTH)
    draw.line([(x - s, y + s), (x + s, y - s)], fill=rgb_color, width=_HYPO_MARK_WIDTH)


def _draw_plum_donut(draw: ImageDraw.ImageDraw, xy: tuple[float, float], rgb_color: tuple) -> None:
    """
    PLUM法（震源未確定）の震源マークとして、二重丸（ドーナツ）を描く。
    バツ印と同様、白フチを下地に敷いてから描画する（2026-09-14追加）。
    """
    x, y = xy
    outer = _PLUM_OUTER_R
    inner = _PLUM_INNER_R
    halo_r = outer + _PLUM_HALO_PAD
    draw.ellipse([x - halo_r, y - halo_r, x + halo_r, y + halo_r],
                 fill=_HALO_COLOR, outline=_HALO_OUTLINE_COLOR, width=1)
    draw.ellipse([x - outer, y - outer, x + outer, y + outer], outline=rgb_color, width=3)
    draw.ellipse([x - inner, y - inner, x + inner, y + inner], outline=rgb_color, width=2)


def _draw_hypocenter(draw: ImageDraw.ImageDraw, lon: Optional[float], lat: Optional[float],
                      is_plum: bool, rgb_color: tuple, projector: _Projector) -> None:
    if lon is None or lat is None:
        return
    if lon <= -180 or lat <= -90:  # -200等のダミー値（震源不明）を弾く
        return
    xy = projector.project(lon, lat)
    if is_plum:
        _draw_plum_donut(draw, xy, rgb_color)
    else:
        _draw_x_mark(draw, xy, rgb_color)


def _draw_station_markers(draw: ImageDraw.ImageDraw, station_shindo: dict, projector: _Projector) -> None:
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
        x, y = projector.project(lon, lat)
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
        logger.warning(
            "GIS地図: GIS_MAP_ENABLE=true ですが外部データ（GeoJSON/観測点一覧）が"
            "未取得のため、地図の描画・添付をスキップします。"
            "Cogの起動ログでGISデータの取得に失敗していないか確認するか、"
            "`python3 bot.py --refresh_gis_data` を試してください。"
        )
        return False
    return True


def _hypocenter_point(hypocenter_lonlat: Optional[tuple]) -> list:
    if not hypocenter_lonlat:
        return []
    lon, lat = hypocenter_lonlat
    if lon is None or lat is None or lon <= -180 or lat <= -90:
        return []
    return [(lon, lat)]


def render_eew_warn_map(
    warn_region_names: set,
    hypocenter_lonlat: Optional[tuple[float, float]] = None,
    is_plum: bool = False,
) -> Optional[bytes]:
    """
    緊急地震速報（警報）向け：発表地域（強い揺れが予想される地域。
    REGION_MAP変換後の府県予報区名の集合）を警戒色で塗りつぶし、震源に
    バツ印（PLUM法の場合はドーナツ）を描画したPNG画像を返す。表示範囲は
    発表地域・震源の位置に応じて自動的にズームする（モジュールdocstring
    「自動ズーム」参照）。

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

        matched_shapes = [shapes[n] for n in warn_region_names if n in shapes]
        for n in warn_region_names:
            if n not in shapes:
                logger.debug(f"GIS地図(EEW警報): 区域名 {n!r} がGeoJSON上に見つかりません")

        hypo_points = _hypocenter_point(hypocenter_lonlat)
        if not matched_shapes and not hypo_points:
            return None  # 描画すべき内容が何もなければ画像自体を作らない

        viewport_points = list(hypo_points)
        for shape in matched_shapes:
            for ring in shape.rings:
                viewport_points.extend(ring)
        viewport = _compute_viewport(viewport_points)
        width, height = _dimensions_for_bbox(viewport)
        projector = _Projector(viewport, width, height)

        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        _draw_polygon_boundaries(draw, _eew_areas, projector, viewport)

        fill_rgba = _rgb_to_rgba(GIS_MAP_WARNING_COLOR, _FILL_ALPHA)
        border_rgb = _rgb_to_rgba(GIS_MAP_WARNING_COLOR)[:3]
        for shape in matched_shapes:
            _fill_area(draw, shape, projector, fill_rgba, border_rgb)

        if hypo_points:
            lon, lat = hypo_points[0]
            _draw_hypocenter(draw, lon, lat, is_plum, border_rgb, projector)

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
    （どちらか一方、または両方同時に指定可）。表示範囲は塗りつぶし区域・
    観測点・震源の位置に応じて自動的にズームする。

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

        matched_regions = []
        if region_shindo:
            for region_name, code in region_shindo.items():
                shape = shapes.get(region_name)
                if shape is None:
                    logger.debug(f"GIS地図(震度分布): 区域名 {region_name!r} がGeoJSON上に見つかりません")
                    continue
                matched_regions.append((shape, code))

        stations = _get_stations()
        matched_stations = {}
        if station_shindo:
            for name, code in station_shindo.items():
                if name in stations:
                    matched_stations[name] = code

        hypo_points = _hypocenter_point(hypocenter_lonlat)

        if not matched_regions and not matched_stations and not hypo_points:
            return None

        viewport_points = list(hypo_points)
        for shape, _code in matched_regions:
            for ring in shape.rings:
                viewport_points.extend(ring)
        for name in matched_stations:
            lat, lon = stations[name]
            viewport_points.append((lon, lat))

        viewport = _compute_viewport(viewport_points)
        width, height = _dimensions_for_bbox(viewport)
        projector = _Projector(viewport, width, height)

        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        _draw_polygon_boundaries(draw, _local_areas, projector, viewport)

        for shape, code in matched_regions:
            color = SHINDO_COLORS.get(code, SHINDO_COLORS[-1])
            fill_rgba = _rgb_to_rgba(color, _FILL_ALPHA)
            border_rgb = _rgb_to_rgba(color)[:3]
            _fill_area(draw, shape, projector, fill_rgba, border_rgb)

        if matched_stations:
            _draw_station_markers(draw, matched_stations, projector)

        if hypo_points:
            lon, lat = hypo_points[0]
            warn_rgb = _rgb_to_rgba(GIS_MAP_WARNING_COLOR)[:3]
            _draw_hypocenter(draw, lon, lat, is_plum, warn_rgb, projector)

        return _finalize(canvas)
    except Exception:
        logger.error("GIS地図(震度分布)描画エラー", exc_info=True)
        return None


_TSUNAMI_GRADE_COLORS = {
    "MajorWarning": TSUNAMI_COLOR_MAJOR_WARNING,
    "Warning":      TSUNAMI_COLOR_WARNING,
    "Watch":        TSUNAMI_COLOR_WATCH,
}


def render_tsunami_map(area_grades: dict) -> Optional[bytes]:
    """
    津波情報向け：津波予報区の沿岸線（ポリゴンではなく海岸線に沿った
    線データ）を警報種別の色で塗り分けたPNG画像を返す（2026-09-14追加）。
    表示範囲は該当する沿岸線の位置に応じて自動的にズームする。

    area_grades : {区域名(津波情報APIのareas[].name): 警報種別}
        警報種別は "MajorWarning"（大津波警報）/ "Warning"（津波警報）/
        "Watch"（津波注意報）を想定。それ以外の値（"Unknown"等）は
        TSUNAMI_COLOR_UNKNOWN で描画する。

    GIS_MAP_ENABLE=false、または外部データ未取得の場合は None を返す。
    """
    if not _ready():
        return None
    try:
        shapes = _tsunami_areas.get()
        if not shapes:
            return None

        matched = []
        for name, grade in (area_grades or {}).items():
            shape = shapes.get(name)
            if shape is None:
                logger.debug(f"GIS地図(津波): 区域名 {name!r} がGeoJSON上に見つかりません")
                continue
            color = _TSUNAMI_GRADE_COLORS.get(grade, TSUNAMI_COLOR_UNKNOWN)
            matched.append((shape, color))

        if not matched:
            return None

        viewport_points = []
        for shape, _color in matched:
            for ring in shape.rings:
                viewport_points.extend(ring)
        viewport = _compute_viewport(viewport_points)
        width, height = _dimensions_for_bbox(viewport)
        projector = _Projector(viewport, width, height)

        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        _draw_line_boundaries(draw, _tsunami_areas, projector, viewport)

        for shape, color in matched:
            _draw_line_highlight(draw, shape, projector, _rgb_to_rgba(color)[:3])

        return _finalize(canvas)
    except Exception:
        logger.error("GIS地図(津波)描画エラー", exc_info=True)
        return None
