"""
core/gis_render.py
===================
気象庁シェープファイル由来のGeoJSON（core/gis_data.py が管理）を用いて、
地震情報・EEW・津波情報向けの簡易地図画像（PNG）をPillowで描画するモジュール
（GIS地図描画機能、試験導入。2026-09-13〜、津波対応・自動ズームは2026-09-14〜、
陸地／海の色分け・アンチエイリアスは2026-09-17〜）。

【陸地／海の色分け・アンチエイリアス（2026-09-17追加）】
「塗られていない区域＝背景と同じ薄灰色」だと日本列島の形そのものが
把握しにくいとの指摘を受け、区域データを陸地色（_LAND_COLOR）で塗り、
背景を海色（_SEA_COLOR）にした。加えて、実際のサイズの2倍
（_SUPERSAMPLE）でキャンバスに描画してから最後にLANCZOSで縮小する
ことで、県境・海岸線のギザつきを滑らかにしている（_supersampled()）。
render_eew_warn_map / render_shindo_map / render_tsunami_map /
render_overseas_map の全ての描画関数がこの2つ（塗り・アンチエイリアス）
を共通で使っている（2026-09-26追記：render_overseas_mapは地理院タイル
廃止に伴い独自ベクター地図化されたため、以前のような「投影方式が
Web Mercatorで異なるため適用できない」という制約は無くなった）。

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
4. render_overseas_map()  : 震源が日本国外の場合向け。世界の国境データ
                             （簡略化版、countries.geojson由来）を陸地色
                             で塗りつぶし、日本の細分区域境界線・震源の
                             バツ印を重ね描き（2026-09-26追加。後述の
                             「遠地地震向けの描画」参照）

【遠地地震（震源が日本国外）向けの描画（2026-09-26、地理院タイル廃止）】
2026-09-25までは、震源が日本国外の場合（is_outside_japan_bbox()で判定）
は別モジュール core/gis_tile_render.py が国土地理院の淡色地図タイルとの
重ね合わせで描画していたが、地理院タイルサーバー側の負荷・大規模地震時
のアクセス集中への配慮から、地理院タイルの使用を廃止した。render_shindo_map
等と同じ「GeoJSONのみ・ネットワークI/Oなし」の独自ベクター地図に統一し、
world countries.geojson（datasets/geo-countries、258カ国）を陸地色で
塗りつぶした背景の上に、日本部分は従来通り自前のGeoJSON
（AreaForecastLocalE_GIS）を重ね描きする。countries.geojson自体は
約14MB・頂点数約55万と非常に大きいため、そのまま毎回描画に使うと処理
負荷が高くなる。そこで core/gis_data.py が Ramer-Douglas-Peucker
アルゴリズムで間引いた軽量版（load_countries_simplified()、約4万頂点・
約0.6〜0.7MB）を使う（Webダッシュボードの/status/gis_countries
エンドポイントとも共用）。旧core/gis_tile_render.pyは削除済み。

いずれも GIS_MAP_ENABLE=false、または必要な外部データが未取得の場合は
None を返す（呼び出し側＝各Cogは、Noneの場合は地図添付をスキップする
だけでよい。例外は投げない設計とし、地図描画の失敗が通知本体の送信を
妨げないようにする）。
"""
import io
import logging
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from core.config import (
    GIS_MAP_ENABLE, GIS_MAP_WARNING_COLOR,
    TSUNAMI_COLOR_MAJOR_WARNING, TSUNAMI_COLOR_WARNING,
    TSUNAMI_COLOR_WATCH, TSUNAMI_COLOR_UNKNOWN,
)
from core.constants import SHINDO_COLORS, LG_COLORS
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
_STATION_ZOOM_MIN_SHINDO = 30    # 観測点マーカーの表示範囲計算で「拡大対象」とする震度コードの下限（30=震度3。2026-09-16追加）

_BASE_DIM = 900     # 基準の画像サイズ（px）。表示範囲が横長・縦長どちらでも
_MAX_DIM = 1400     # 長辺がこのpxを超えないよう、短辺を基準にして縮尺を決める

# ===============================
# アンチエイリアス（2026-09-17追加）
# ===============================
# 実際の画像サイズの _SUPERSAMPLE 倍でキャンバスに描画してから、最後に
# LANCZOSで縮小することでアンチエイリアスをかける（県境・海岸線の
# ギザつきを滑らかにする）。_scale は現在の描画倍率で、_supersampled()
# コンテキスト内でのみ _SUPERSAMPLE になり、それ以外は1（無効）。
# 本モジュールの描画は常に単一スレッド・同期的に1回の呼び出しで完結する
# ため、グローバル変数での管理でも競合の心配はない。
_SUPERSAMPLE = 2
_scale = 1


def _px(value: float) -> int:
    """現在の描画倍率（_scale）を適用したピクセル値を返す。"""
    return round(value * _scale)


@contextmanager
def _supersampled():
    """このwithブロック内でのみ _px() が _SUPERSAMPLE 倍の値を返すようにする。"""
    global _scale
    _scale = _SUPERSAMPLE
    try:
        yield _scale
    finally:
        _scale = 1

# ===============================
# 陸地／海の色分け（2026-09-17追加）
# ===============================
# 「塗られていない区域＝背景と同じ薄灰色」だと日本列島の輪郭そのものが
# 把握しにくいとの指摘を受け、区域データ（_eew_areas / _local_areas、
# 2026-09-26からは海外向けの国境データ_get_countries()も同様）を陸地色で
# 塗りつぶし、背景（_finalizeの合成先）を海色にすることで陸地の形を
# 分かりやすくした。_draw_land_fill()（区域データ用）と
# _draw_country_fill()（国境データ用）はどちらもこの2色を使う。
_LAND_COLOR = (238, 232, 220, 255)   # 陸地（薄いクリーム色）
# 海（水色）。_finalize() の合成背景に使う。
# 【2026-09-22変更】旧色(200,222,238)は陸地色との明度差が小さく（陸地
# 平均輝度≒230に対し海は≒220）、縮小表示（Discordのサムネイル表示等）
# では遠目に判別しづらいとの指摘のため、明度差を広げつつ彩度を少し
# 上げて「水色」とわかりやすい色に変更した（平均輝度≒203。陸地との
# 差は約10→約27に拡大）。
_SEA_COLOR = (178, 205, 227, 255)

# 背景・境界線・図形の見た目
# 区域境界線（2026-09-15: 見づらいとの指摘で(170,170,170,255)→濃くした。
# 【2026-09-22変更】(110,110,110,255)→さらに濃くした。縮小表示時、細い
# 境界線が背景色に埋もれて判別しづらいとの指摘のため）
_BOUNDARY_COLOR = (80, 80, 80, 255)
_BOUNDARY_WIDTH = 1
_HIGHLIGHT_BORDER_WIDTH = 2               # 塗りつぶし区域の輪郭線の太さ
# 【2026-09-22追加】震度速報（ScalePrompt）・緊急地震速報の予想震度など、
# SHINDO_COLORS を地図に塗る際の明度係数。SHINDO_COLORSはDiscord Embed の
# 色（cogs/eew.py・cogs/quake.py等）と共通の定数だが、Embedの帯のように
# 小さい面積で見る色と、地図全体を塗る面積とでは同じ色でも「薄い」印象の
# 出方が異なる。地図の色だけが薄く感じるとの指摘を受け、地図描画時のみ
# （Embed色自体は変更せず）_darken_rgbで明度を90%に落として塗る。
_SHINDO_FILL_DARKEN_FACTOR = 0.9
_FILL_ALPHA = 255                         # 塗りつぶしの不透明度（0-255）。
                                           # 2026-09-16: 「震度の色が薄い」との
                                           # 指摘を受け、半透明(150)から不透明
                                           # (255=設定色そのまま)に変更した。
                                           # 隣接区域との境界は、フィルと同じ
                                           # 色に頼らず「フィルより暗い色」の
                                           # 輪郭線（_darken_rgb）で確保する
                                           # （同じ震度＝同じ色の区域が隣接
                                           # していても境界が見えるようにする
                                           # ため）。
_BORDER_DARKEN_FACTOR = 0.55              # 輪郭線の明度（フィル色に掛ける係数。小さいほど暗い）
_HALO_COLOR = (255, 255, 255, 255)        # 震源マークの視認性向上用の白フチ
_HALO_OUTLINE_COLOR = (90, 90, 90, 255)   # 白フチ自体の輪郭（薄い背景に対する視認性確保）
_HYPO_MARK_SIZE = 10                      # 震源バツ印（赤）の半径（px）
_HYPO_MARK_WIDTH = 3                       # 震源バツ印（赤）の線の太さ（px）
_HYPO_MARK_OUTLINE_PAD = 3                 # 白フチのバツ印を赤バツ印より一回り大きくする分（px）
_HYPO_MARK_OUTLINE_EXTRA_WIDTH = 4         # 白フチのバツ印の線の太さを赤バツ印より太くする分（px）
# EEW地図の震源バツ印の拡大率（2026-09-22追加。「バツ印が見づらい」との
# 指摘を受け、現行の1.5〜2倍程度に拡大。表示範囲の広さに応じて連続的に
# 変え、日本全体表示で _EEW_MARK_SCALE_MIN、最大ズーム
# （_VIEWPORT_MIN_SPAN_DEG四方）で _EEW_MARK_SCALE_MAX になる）。
_EEW_MARK_SCALE_MIN = 1.5
_EEW_MARK_SCALE_MAX = 2.0
_PLUM_OUTER_R = 14                        # PLUM法ドーナツの外半径（px）
_PLUM_INNER_R = 7                         # PLUM法ドーナツの内半径（px）
_PLUM_HALO_PAD = 4                        # ドーナツの白フチの余白（px）
_STATION_MARKER_SIZE = 24                 # 観測点マーカー（正方形）の一辺（px）。
                                           # 2026-09-16: 「小さくて見づらい」との
                                           # 指摘を受け、11→16→24に拡大
                                           # （S/M/Lの比較サンプルを提示し、
                                           # Lの24pxを採用）。
_REGION_ICON_SIZE = 28                    # 震度速報時、区域中央に置くアイコンの一辺（px）。
                                           # 観測点マーカーより一回り大きくする（2026-09-16追加）
_TSUNAMI_LINE_WIDTH = 6                   # 津波予報区の色付き沿岸線の太さ（px）


def _rgb_to_rgba(color: int, alpha: int = 255) -> tuple[int, int, int, int]:
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF, alpha)


def _darken_rgb(rgb: tuple, factor: float = _BORDER_DARKEN_FACTOR) -> tuple:
    """
    輪郭線用に、フィルと同じ色相のまま明度だけ落とした色を作る。
    フィルを不透明にした（2026-09-16）ことで、フィルと同じ色の輪郭線では
    隣接区域との境界が見えなくなるため、常にフィルより暗い色を使う。
    """
    r, g, b = rgb[0], rgb[1], rgb[2]
    return (round(r * factor), round(g * factor), round(b * factor))


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
    震源が海外かどうかの判定＝render_shindo_map/render_overseas_mapの
    表示モード切り替えのトリガーとして、各Cogが使う（2026-09-15追加）。
    """
    return not (
        _JAPAN_LON_MIN <= lon <= _JAPAN_LON_MAX and
        _JAPAN_LAT_MIN <= lat <= _JAPAN_LAT_MAX
    )


def get_local_area_shapes() -> dict:
    """
    細分区域（AreaForecastLocalE_GIS、194件）の生の緯度経度リングデータを
    返す公開アクセサ。render_overseas_map自身は_local_areasを直接参照
    できるため使っていないが、他モジュールが同じキャッシュ済みデータを
    再利用したい場合向けに公開のまま残している。
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


def _polygon_centroid(ring: list) -> Optional[tuple]:
    """
    1つの閉じたリング（多角形の外側の頂点列）の重心（面積重心）を、
    測量分野で使われる標準的なshoelace公式で計算する。単純な頂点の
    平均（各点を等しく重み付けして足すだけ）だと、海岸線のようにギザ
    ギザした部分の頂点密度に引きずられて実際の図形の中心からずれる
    ことがあるため、面積に基づく重心を使う。
    """
    if len(ring) < 3:
        return None
    area2 = 0.0
    cx = 0.0
    cy = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area2) < 1e-12:
        # 退化した（面積がほぼ0の）リング。単純平均にフォールバックする
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    area = area2 / 2.0
    cx /= (6.0 * area)
    cy /= (6.0 * area)
    return (cx, cy)


def _shape_main_centroid(shape: "_AreaShape") -> Optional[tuple]:
    """
    区域（複数リング＝本土＋離島等を持ちうる）の「主要な部分」の重心を
    返す。単純に全リングの頂点を平均すると、本土から離れた小さな離島に
    引っ張られてアイコンが海上に配置されてしまうことがあるため、
    最も面積（bboxの面積で近似）が大きいリングを選び、その重心のみを
    使う（2026-09-16追加、震度速報時の区域アイコン配置用）。
    """
    if not shape.rings:
        return None
    best_ring = None
    best_area = -1.0
    for ring in shape.rings:
        bbox = _rings_bbox([ring])
        if bbox is None:
            continue
        area = (bbox.lon_max - bbox.lon_min) * (bbox.lat_max - bbox.lat_min)
        if area > best_area:
            best_area = area
            best_ring = ring
    if best_ring is None:
        return None
    return _polygon_centroid(best_ring)


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
        return ImageFont.load_default(size=_px(size))
    except TypeError:
        # Pillow<10.1系では load_default() に size 引数が無い
        return ImageFont.load_default()


def _draw_land_fill(draw: ImageDraw.ImageDraw, area_set: "_AreaSet",
                     projector: _Projector, viewport: _BBox) -> None:
    """
    区域セット全体を陸地色（_LAND_COLOR）で塗りつぶし、海（_SEA_COLOR。
    _finalize()の背景）との区別をつきやすくする（2026-09-17追加）。
    表示範囲と交差しない区域はスキップする。境界線は別途
    _draw_polygon_boundaries が描くため、ここでは塗りのみ行う。
    """
    for shape in area_set.get().values():
        if not shape.bbox.intersects(viewport):
            continue
        for ring in shape.rings:
            if len(ring) < 3:
                continue
            draw.polygon(projector.project_ring(ring), fill=_LAND_COLOR)


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
            draw.line(pts + [pts[0]], fill=_BOUNDARY_COLOR, width=_px(_BOUNDARY_WIDTH))


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
            draw.line(pts, fill=_BOUNDARY_COLOR, width=_px(_BOUNDARY_WIDTH))


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
        draw.line(pts + [pts[0]], fill=rgb_border, width=_px(_HIGHLIGHT_BORDER_WIDTH))


def _draw_line_highlight(draw: ImageDraw.ImageDraw, shape: _AreaShape, projector: _Projector,
                          rgb_color: tuple) -> None:
    """1つの津波予報区の沿岸線を太い色付き線で強調表示する。"""
    for ring in shape.rings:
        if len(ring) < 2:
            continue
        pts = projector.project_ring(ring)
        draw.line(pts, fill=rgb_color, width=_px(_TSUNAMI_LINE_WIDTH), joint="curve")


def _draw_x_mark(draw: ImageDraw.ImageDraw, xy: tuple[float, float], rgb_color: tuple,
                 scale: float = 1.0) -> None:
    """
    震源のバツ印を描く。2026-09-17: 「警察署の地図記号のよう」との
    指摘を受け、白い丸フチ（_HALO_COLORの円）を敷く方式から、
    「一回り大きい白いバツ印（背面）＋赤バツ印（最前面）」という、
    バツ印そのものに白い縁取りをする方式に変更した。同じ形（X）を
    白フチ側は少し大きく・太く、赤側は元のサイズで重ね描きすることで、
    赤いバツ印の輪郭に沿った白い縁取りとして見えるようにしている。
    """
    x, y = xy
    # scale: バツ印全体（大きさ・線幅・白フチ）の拡大率。既定1.0は従来と同じ
    # （2026-09-22追加。EEW地図が表示範囲に応じた拡大率を渡す）。
    s = _px(_HYPO_MARK_SIZE * scale)
    s_outline = _px((_HYPO_MARK_SIZE + _HYPO_MARK_OUTLINE_PAD) * scale)
    outline_width = _px((_HYPO_MARK_WIDTH + _HYPO_MARK_OUTLINE_EXTRA_WIDTH) * scale)
    red_width = _px(_HYPO_MARK_WIDTH * scale)

    # 白フチのバツ印（一回り大きく・太め、背面）
    draw.line([(x - s_outline, y - s_outline), (x + s_outline, y + s_outline)],
              fill=_HALO_COLOR, width=outline_width)
    draw.line([(x - s_outline, y + s_outline), (x + s_outline, y - s_outline)],
              fill=_HALO_COLOR, width=outline_width)

    # 赤バツ印（最前面）
    draw.line([(x - s, y - s), (x + s, y + s)], fill=rgb_color, width=red_width)
    draw.line([(x - s, y + s), (x + s, y - s)], fill=rgb_color, width=red_width)


def _draw_plum_donut(draw: ImageDraw.ImageDraw, xy: tuple[float, float], rgb_color: tuple) -> None:
    """
    PLUM法（震源未確定）の震源マークとして、二重丸（ドーナツ）を描く。
    バツ印と同様、白フチを下地に敷いてから描画する（2026-09-14追加）。
    """
    x, y = xy
    outer = _px(_PLUM_OUTER_R)
    inner = _px(_PLUM_INNER_R)
    halo_r = outer + _px(_PLUM_HALO_PAD)
    draw.ellipse([x - halo_r, y - halo_r, x + halo_r, y + halo_r],
                 fill=_HALO_COLOR, outline=_HALO_OUTLINE_COLOR, width=_px(1))
    draw.ellipse([x - outer, y - outer, x + outer, y + outer], outline=rgb_color, width=_px(3))
    draw.ellipse([x - inner, y - inner, x + inner, y + inner], outline=rgb_color, width=_px(2))


def _draw_hypocenter(draw: ImageDraw.ImageDraw, lon: Optional[float], lat: Optional[float],
                      is_plum: bool, rgb_color: tuple, projector: _Projector,
                      mark_scale: float = 1.0) -> None:
    if lon is None or lat is None:
        return
    if lon <= -180 or lat <= -90:  # -200等のダミー値（震源不明）を弾く
        return
    xy = projector.project(lon, lat)
    if is_plum:
        _draw_plum_donut(draw, xy, rgb_color)
    else:
        _draw_x_mark(draw, xy, rgb_color, scale=mark_scale)


def _zoom_mark_scale(viewport: _BBox) -> float:
    """
    表示範囲（自動ズーム後）の広さに応じた震源バツ印の拡大率を返す
    （2026-09-22追加。EEW地図用）。日本全体を表示しているときは
    _EEW_MARK_SCALE_MIN、_VIEWPORT_MIN_SPAN_DEG 四方まで拡大した
    ときは _EEW_MARK_SCALE_MAX、その間は表示範囲の対数に比例して
    連続的に変化する。
    """
    full_lon = _JAPAN_LON_MAX - _JAPAN_LON_MIN
    full_lat = _JAPAN_LAT_MAX - _JAPAN_LAT_MIN
    frac = max(
        (viewport.lon_max - viewport.lon_min) / full_lon,
        (viewport.lat_max - viewport.lat_min) / full_lat,
    )
    frac = min(max(frac, 1e-6), 1.0)
    frac_min = _VIEWPORT_MIN_SPAN_DEG / full_lon
    t = math.log(1.0 / frac) / math.log(1.0 / frac_min)
    t = min(max(t, 0.0), 1.0)
    return _EEW_MARK_SCALE_MIN + (_EEW_MARK_SCALE_MAX - _EEW_MARK_SCALE_MIN) * t


def _draw_shindo_square(draw: ImageDraw.ImageDraw, xy: tuple, code: int, size: int,
                         outline: tuple = (40, 40, 40, 255)) -> None:
    """
    震度を表す色付き正方形＋短いラベル（"1"〜"7"等）を1つ描く共通処理。
    観測点マーカー（_draw_station_markers）と、震度速報時の区域中央
    アイコン（_draw_region_icons）の両方から使う（2026-09-16、区域
    アイコン追加時に共通化）。
    """
    x, y = xy
    half = _px(size) / 2
    rgb = _darken_rgb(_rgb_to_rgba(SHINDO_COLORS.get(code, SHINDO_COLORS[-1]))[:3],
                       _SHINDO_FILL_DARKEN_FACTOR)
    draw.rectangle([x - half, y - half, x + half, y + half], fill=rgb, outline=outline, width=_px(1))
    font = _font(max(round(size), 11))
    label = shindo_short_label(code)
    # テキストは黒固定（マーカー色が薄い場合でも視認性を確保するため）
    draw.text((x, y), label, fill=(20, 20, 20, 255), font=font, anchor="mm")


def _draw_station_markers(draw: ImageDraw.ImageDraw, station_shindo: dict, projector: _Projector) -> None:
    """
    観測点ごとの震度マーカー（色付き正方形＋短い震度ラベル）を描画する。
    station_shindo: {観測点名(stations.jsonのname): 震度コード(INT_MAPのキー)}
    stations.json 上に見つからない観測点名は描画をスキップする（ログのみ）。

    観測点が密集していて互いに重なる場合、震度が大きい観測点ほど
    手前（後から描画＝上に重なる）に来るよう、震度コードの昇順で
    描画する（2026-09-16追加。従来は station_shindo の辞書順＝データ
    ソース側の並び順のままだったため、震度の小さい観測点が大きい
    観測点を覆い隠してしまうことがあった）。
    """
    stations = _get_stations()
    missing = 0
    for name, code in sorted(station_shindo.items(), key=lambda item: item[1]):
        latlon = stations.get(name)
        if latlon is None:
            missing += 1
            continue
        lat, lon = latlon
        xy = projector.project(lon, lat)
        _draw_shindo_square(draw, xy, code, _STATION_MARKER_SIZE)

    if missing:
        logger.debug(f"GIS地図: stations.jsonに見つからない観測点を{missing}件スキップしました")


def _draw_region_icons(draw: ImageDraw.ImageDraw, matched_regions: list, projector: _Projector) -> None:
    """
    震度速報（ScalePrompt）向け：塗りつぶしだけでは震度が分かりにくいとの
    指摘を受け、区域ごとに震度ラベル付きの大きめアイコンをその区域の
    重心（_shape_main_centroid）へ追加で配置する（2026-09-16追加）。
    観測点マーカーと同様、震度が大きい区域のアイコンほど前面に来る
    よう震度コード昇順で描画する。
    """
    for shape, code in sorted(matched_regions, key=lambda item: item[1]):
        centroid = _shape_main_centroid(shape)
        if centroid is None:
            continue
        lon, lat = centroid
        xy = projector.project(lon, lat)
        _draw_shindo_square(draw, xy, code, _REGION_ICON_SIZE, outline=(20, 20, 20, 255))


def _finalize(canvas: Image.Image, target_size: Optional[tuple] = None) -> bytes:
    """
    RGBAキャンバスを背景（海色）と合成し、PNGバイト列にして返す。
    target_size が指定され、かつcanvasのサイズと異なる場合（＝
    _supersampled()で拡大して描画した場合）、LANCZOSで指定サイズへ
    縮小することでアンチエイリアスをかける（2026-09-17追加）。
    """
    bg = Image.new("RGBA", canvas.size, _SEA_COLOR)
    composed = Image.alpha_composite(bg, canvas)
    if target_size and target_size != composed.size:
        composed = composed.resize(target_size, Image.LANCZOS)
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

        with _supersampled():
            render_w, render_h = _px(width), _px(height)
            projector = _Projector(viewport, render_w, render_h)

            canvas = Image.new("RGBA", (render_w, render_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            _draw_land_fill(draw, _eew_areas, projector, viewport)
            _draw_polygon_boundaries(draw, _eew_areas, projector, viewport)

            fill_rgba = _rgb_to_rgba(GIS_MAP_WARNING_COLOR, _FILL_ALPHA)
            border_rgb = _darken_rgb(_rgb_to_rgba(GIS_MAP_WARNING_COLOR)[:3])
            for shape in matched_shapes:
                _fill_area(draw, shape, projector, fill_rgba, border_rgb)

            if hypo_points:
                lon, lat = hypo_points[0]
                _draw_hypocenter(draw, lon, lat, is_plum, border_rgb, projector,
                                 mark_scale=_zoom_mark_scale(viewport))

            return _finalize(canvas, (width, height))
    except Exception:
        logger.error("GIS地図(EEW警報)描画エラー", exc_info=True)
        return None


def render_shindo_map(
    region_shindo: Optional[dict] = None,
    station_shindo: Optional[dict] = None,
    hypocenter_lonlat: Optional[tuple[float, float]] = None,
    is_plum: bool = False,
    show_region_icons: bool = False,
    station_zoom_priority: bool = True,
    enlarge_hypocenter_mark: bool = False,
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
    show_region_icons   : True の場合、region_shindo で塗りつぶした各区域の
        中央（重心）に、震度ラベル付きの大きめアイコンを追加で配置する
        （2026-09-16追加。震度速報＝ScalePromptは塗りつぶしだけでは
        震度が分かりにくいとの指摘のため。QuakeInfoCogがScalePrompt
        判定時のみTrueを渡す想定。EEW予報側は今のところFalseのまま）。
    station_zoom_priority : True（デフォルト）の場合、震度3以上の観測点が
        あればその周辺だけに表示範囲を絞る（_STATION_ZOOM_MIN_SHINDO
        参照）。False を渡すと、この優先ズームを行わず常に station_shindo
        の全観測点を表示範囲の計算対象にする（2026-09-18追加。「各地の
        震度に関する情報」で、拡大表示に加えて全観測点を見渡せる通常
        表示の画像も併せて見たいとの要望のため。呼び出し側で
        station_zoom_priority=True/False の2回呼び出し、2枚を添付する
        想定。region_shindo・PLUM法には影響しない）。
    enlarge_hypocenter_mark : True の場合、震源のバツ印を表示範囲に応じて
        1.5〜2.0倍に拡大する（_zoom_mark_scale。2026-09-22追加。EEWの
        地図用。地震情報の地図は従来の大きさのままとするため既定は
        False）。

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

        # 観測点は、震度3以上（_STATION_ZOOM_MIN_SHINDO）の地点があれば
        # そちらだけを表示範囲の計算対象にし、その周辺を拡大表示する
        # （2026-09-16追加。全観測点を含めると、震度1程度の遠方の観測点
        # 1件のせいで震度の大きい地域がズームアウトされ見づらくなる
        # ため）。震度3以上の観測点が1件もない場合（震度1〜2程度の
        # 小規模な地震等）は、従来通り全観測点を対象に含める。
        station_points_all = []
        station_points_strong = []
        for name, code in matched_stations.items():
            lat, lon = stations[name]
            station_points_all.append((lon, lat))
            if code >= _STATION_ZOOM_MIN_SHINDO:
                station_points_strong.append((lon, lat))
        if station_zoom_priority:
            viewport_points.extend(station_points_strong or station_points_all)
        else:
            viewport_points.extend(station_points_all)

        viewport = _compute_viewport(viewport_points)
        width, height = _dimensions_for_bbox(viewport)

        with _supersampled():
            render_w, render_h = _px(width), _px(height)
            projector = _Projector(viewport, render_w, render_h)

            canvas = Image.new("RGBA", (render_w, render_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            _draw_land_fill(draw, _local_areas, projector, viewport)
            _draw_polygon_boundaries(draw, _local_areas, projector, viewport)

            for shape, code in matched_regions:
                color = SHINDO_COLORS.get(code, SHINDO_COLORS[-1])
                fill_rgb = _darken_rgb(_rgb_to_rgba(color)[:3], _SHINDO_FILL_DARKEN_FACTOR)
                fill_rgba = (*fill_rgb, _FILL_ALPHA)
                border_rgb = _darken_rgb(fill_rgb)
                _fill_area(draw, shape, projector, fill_rgba, border_rgb)

            if show_region_icons and matched_regions:
                _draw_region_icons(draw, matched_regions, projector)

            if matched_stations:
                _draw_station_markers(draw, matched_stations, projector)

            if hypo_points:
                lon, lat = hypo_points[0]
                warn_rgb = _rgb_to_rgba(GIS_MAP_WARNING_COLOR)[:3]
                _draw_hypocenter(
                    draw, lon, lat, is_plum, warn_rgb, projector,
                    mark_scale=_zoom_mark_scale(viewport) if enlarge_hypocenter_mark else 1.0,
                )

            return _finalize(canvas, (width, height))
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

        with _supersampled():
            render_w, render_h = _px(width), _px(height)
            projector = _Projector(viewport, render_w, render_h)

            canvas = Image.new("RGBA", (render_w, render_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            # 津波予報区（_tsunami_areas）は海岸線のみの線データで陸地の
            # 塗りつぶしはできないため、陸地の下地には _local_areas
            # （細分区域ポリゴン）を代わりに使う（2026-09-17追加）。
            _draw_land_fill(draw, _local_areas, projector, viewport)
            _draw_line_boundaries(draw, _tsunami_areas, projector, viewport)

            for shape, color in matched:
                _draw_line_highlight(draw, shape, projector, _rgb_to_rgba(color)[:3])

            return _finalize(canvas, (width, height))
    except Exception:
        logger.error("GIS地図(津波)描画エラー", exc_info=True)
        return None


def render_long_period_map(
    lg_stations: Optional[list] = None,
    hypocenter_lonlat: Optional[tuple[float, float]] = None,
) -> Optional[bytes]:
    """
    長周期地震動に関する観測情報向け：観測点ごとの長周期地震動階級
    （"1"〜"4"）を色分けしたマーカー地図を返す（2026-09-18追加）。

    長周期地震動階級は震度スケール（core.constants.SHINDO_COLORS、
    10,20,...,70）とは別の尺度・配色（core.constants.LG_COLORS、
    "1"〜"4"の文字列キー）のため、render_shindo_map とは別関数にして
    いる（マーカーの見た目は同じだが、色とラベルの意味が異なるため
    共通化はしない）。

    lg_stations       : [{"lat": 緯度, "lon": 経度, "level": 階級文字列("1"〜"4")}, ...]
        cogs/other.py の notify_long_period が、観測情報JSONの
        Body.Intensity.Observation...IntensityStation[]（Name/LgInt/latlon）
        から組み立てて渡す。

        【2026-09-22 修正】以前は {観測点名: 階級} を受け取り、座標を
        stations.json（地震情報の観測点一覧）の名前引きで得ていたが、
        呼び出し側が渡していたのは観測点名ではなく「地域名」
        （埼玉県南部 等）だったため1件も一致せず、観測点が描画されない
        （震源のバツ印だけの、1枚目と同一の画像になる）不具合があった。
        観測情報JSONの各観測点には latlon（座標）が含まれているため、
        名前引きをやめて座標を直接受け取る方式に変更した
        （stations.json の観測点名と表記が一致しない場合の取りこぼしも
        無くなる）。
    hypocenter_lonlat  : (経度, 緯度)。震源不明の場合は None

    描画できる観測点が1件も無い場合は None を返す（震源だけの地図は
    呼び出し側が別途描画する1枚目と重複するため作らない）。
    GIS_MAP_ENABLE=false、または外部データ未取得の場合も None を返す。
    """
    if not _ready():
        return None
    try:
        valid = []
        for st in lg_stations or []:
            try:
                lat, lon = float(st["lat"]), float(st["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            level = str(st.get("level", "")).strip()
            if not level or level == "不明":
                continue
            valid.append((lon, lat, level))
        if not valid:
            return None

        hypo_points = _hypocenter_point(hypocenter_lonlat)
        viewport_points = list(hypo_points) + [(lon, lat) for lon, lat, _ in valid]
        viewport = _compute_viewport(viewport_points)
        width, height = _dimensions_for_bbox(viewport)

        with _supersampled():
            render_w, render_h = _px(width), _px(height)
            projector = _Projector(viewport, render_w, render_h)

            canvas = Image.new("RGBA", (render_w, render_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            _draw_land_fill(draw, _local_areas, projector, viewport)
            _draw_polygon_boundaries(draw, _local_areas, projector, viewport)

            half = _px(_STATION_MARKER_SIZE) / 2
            font = _font(max(_STATION_MARKER_SIZE, 11))
            # 階級が大きい観測点ほど前面に描画する（観測点マーカーと同じ考え方）
            for lon, lat, level in sorted(valid, key=lambda item: item[2]):
                x, y = projector.project(lon, lat)
                color_hex = LG_COLORS.get(level, LG_COLORS["不明"])
                rgb = _rgb_to_rgba(color_hex)[:3]
                draw.rectangle([x - half, y - half, x + half, y + half],
                                fill=rgb, outline=(40, 40, 40, 255), width=_px(1))
                draw.text((x, y), level, fill=(20, 20, 20, 255), font=font, anchor="mm")

            if hypo_points:
                lon, lat = hypo_points[0]
                warn_rgb = _rgb_to_rgba(GIS_MAP_WARNING_COLOR)[:3]
                _draw_hypocenter(draw, lon, lat, False, warn_rgb, projector)

            return _finalize(canvas, (width, height))
    except Exception:
        logger.error("GIS地図(長周期地震動)描画エラー", exc_info=True)
        return None


# ===============================
# 遠地地震（震源が日本国外）向け描画（2026-09-26追加、地理院タイル廃止）
# ===============================
# モジュールdocstring「遠地地震（震源が日本国外）向けの描画」参照。
_OVERSEAS_VIEWPORT_PADDING_RATIO = 0.15  # 日本全体+震源のbboxに対する余白比率（旧gis_tile_renderと同じ値）

_countries_cache: Optional[list] = None
_countries_lock = threading.Lock()


def _get_countries() -> list:
    """
    (bbox, rings) のリストを返す（間引き済み・日本を除く）。プロセス内で
    一度だけパースし、以降はキャッシュを再利用する（_AreaSetと同じ
    考え方）。countriesは国名等のプロパティを描画に使わないため、
    _AreaShapeではなく軽量な (bbox, rings) タプルのリストにしている
    （旧core/gis_tile_render.py._get_countries() から移植。データ元を
    gis_data.load_countries()〈間引き前・約14MB〉から
    gis_data.load_countries_simplified()〈間引き済み・約0.6〜0.7MB〉に
    変更した点のみ異なる）。
    """
    global _countries_cache
    if _countries_cache is not None:
        return _countries_cache
    with _countries_lock:
        if _countries_cache is not None:
            return _countries_cache
        countries = []
        skipped = 0
        for feature in gis_data.load_countries_simplified():
            rings = _extract_rings(feature.get("geometry"), kind="polygon")
            bbox = _rings_bbox(rings) if rings else None
            if not rings or bbox is None:
                skipped += 1
                continue
            countries.append((bbox, rings))
        if skipped:
            logger.debug(f"GIS地図(海外): geometryが空のため{skipped}件の国をスキップしました")
        logger.debug(f"GIS地図(海外): 国境データを{len(countries)}件読み込みました（日本を除く・間引き済み）")
        _countries_cache = countries
        return _countries_cache


def _pick_lon_shift(bbox: _BBox, viewport: _BBox) -> float:
    """
    国境ポリゴンのbboxを表示範囲（viewport）の経度座標系に揃えるための
    シフト量（0／+360／-360度）を選ぶ（2026-09-26追加）。

    render_overseas_map の表示範囲は、日付変更線をまたぐ場合（南北
    アメリカ等）、震源の経度を±360シフトした連続値（例: 97〜310度）に
    なることがある（_compute_overseas_viewport参照）。一方、
    countries.geojson由来の国境データは通常の-180〜180度の範囲の
    ままなので、そのままではシフトされた表示範囲と重ならず、対象の
    国が描画されない（震源のバツ印だけが海上に浮いて見える）不具合が
    あったため、3候補（シフト無し／+360／-360）のうち表示範囲との
    重なりが最大になるものを選び、描画前に国境データ側へ適用する。
    """
    best_shift, best_overlap = 0.0, -1.0
    for shift in (0.0, 360.0, -360.0):
        lo, hi = bbox.lon_min + shift, bbox.lon_max + shift
        overlap = min(hi, viewport.lon_max) - max(lo, viewport.lon_min)
        if overlap > best_overlap:
            best_overlap, best_shift = overlap, shift
    return best_shift


def _draw_country_fill(draw: ImageDraw.ImageDraw, projector: _Projector, viewport: _BBox) -> None:
    """世界の国境データ（間引き済み・日本を除く）を陸地色で塗りつぶす。"""
    for bbox, rings in _get_countries():
        shift = _pick_lon_shift(bbox, viewport)
        shifted_bbox = _BBox(bbox.lon_min + shift, bbox.lon_max + shift, bbox.lat_min, bbox.lat_max)
        if not shifted_bbox.intersects(viewport):
            continue
        for ring in rings:
            if len(ring) < 3:
                continue
            draw_ring = [(lon + shift, lat) for lon, lat in ring] if shift else ring
            draw.polygon(projector.project_ring(draw_ring), fill=_LAND_COLOR)


def _compute_overseas_viewport(hypo_lonlat: tuple) -> _BBox:
    """
    震源が日本国外の場合向け：日本全体＋震源を収める表示範囲を返す
    （render_overseas_map専用）。_compute_viewport() は結果を日本全体の
    範囲へ強制的にクランプするため、震源が日本国外にあるケースには
    使えない（旧core/gis_tile_render.py._compute_overseas_viewport()の
    移植。Web Mercator用のズームレベル計算部分のみ削除している）。

    経度方向は日付変更線をまたぐ可能性がある（南米・北米西岸等、太平洋を
    挟んで日本の反対側にある震源の場合）。単純にmin/maxを取ると、太平洋
    を挟んだ「近い側」ではなく地球を逆回りした「遠い側」（ヨーロッパ・
    アフリカ・大西洋経由）の範囲を選んでしまうことがあるため、震源の
    経度を±360度シフトした場合も含めて候補を作り、経度方向の幅が最小に
    なる＝太平洋側の短い経路を選ぶ（lonの値自体は±180度の範囲に収めず、
    連続した値のまま返す）。
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

    pad_lon = (lon_max - lon_min) * _OVERSEAS_VIEWPORT_PADDING_RATIO
    pad_lat = (lat_max - lat_min) * _OVERSEAS_VIEWPORT_PADDING_RATIO
    lon_min -= pad_lon
    lon_max += pad_lon
    lat_min = max(lat_min - pad_lat, -85.0)
    lat_max = min(lat_max + pad_lat, 85.0)
    return _BBox(lon_min, lon_max, lat_min, lat_max)


def render_overseas_map(hypocenter_lonlat: tuple[float, float]) -> Optional[bytes]:
    """
    震源が日本国外の場合向け：世界の国境データ（間引き済み・日本を除く。
    countries.geojson由来）を陸地色で塗りつぶした背景に、日本の細分区域
    境界線・震源のバツ印を重ね描きしたPNG画像を返す（2026-09-26追加）。

    【地理院タイルの使用を廃止】2026-09-25までは国土地理院の淡色地図
    タイルとの重ね合わせ（旧core/gis_tile_render.py）で描画していたが、
    地理院タイルサーバー側の負荷・大規模地震時のアクセス集中への配慮
    から使用を取りやめ、本モジュールの他の地図（render_shindo_map等）
    と同じ「GeoJSONのみ・ネットワークI/Oなし」の独自ベクター地図に
    統一した。そのため以前必要だった国土地理院コンテンツ利用規約に
    基づく出典表示（「出典：国土地理院…」）も不要になった。実際の
    地形の精密さ・詳細さではタイル画像に劣るが、震源のおおよその位置
    （どの国・地域か、日本との位置関係）を伝える目的には十分と判断した。

    【同期関数への変更】旧render_overseas_map()はタイル取得のHTTPフェッチ
    が必要なためasync関数（aiohttp.ClientSessionを引数に取る）だったが、
    ネットワークI/Oが不要になったため、render_shindo_map等と同じ同期
    関数に変更した（呼び出し側はaiohttp.ClientSessionを渡す必要が無く
    なり、awaitも不要になった）。

    hypocenter_lonlat : (経度, 緯度)。震源不明の場合は呼び出し側で判定し、
        このプロパティ自体を呼ばないこと（本関数は必須パラメータとして扱う）。

    GIS_MAP_ENABLE=false、または外部データ（細分区域・国境データ等）
    未取得の場合は None を返す。
    """
    if not _ready():
        return None

    lon, lat = hypocenter_lonlat
    if lon is None or lat is None or lon <= -180 or lat <= -90:
        return None

    if not _local_areas.get():
        return None

    try:
        viewport = _compute_overseas_viewport((lon, lat))
        width, height = _dimensions_for_bbox(viewport)

        # 日付変更線をまたぐ表示範囲では、震源の経度をviewportと同じ
        # ±360シフトで正規化してから投影する（正規化を怠ると震源が表示
        # 範囲外の座標に投影され、バツ印が描画されない／端にわずかに
        # 掛かるだけになる。旧gis_tile_render.pyの2026-09-25修正と同じ
        # 理由）。
        draw_lon = lon
        for candidate in (lon, lon + 360, lon - 360):
            if viewport.lon_min <= candidate <= viewport.lon_max:
                draw_lon = candidate
                break

        with _supersampled():
            render_w, render_h = _px(width), _px(height)
            projector = _Projector(viewport, render_w, render_h)

            canvas = Image.new("RGBA", (render_w, render_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)

            # 1. 世界の国境データ（日本を除く）
            _draw_country_fill(draw, projector, viewport)

            # 2. 日本の細分区域（陸地色の塗り＋境界線。日本自体は自前の
            #    GeoJSONの方が精密なため、国境データの上から重ね描きする）
            _draw_land_fill(draw, _local_areas, projector, viewport)
            _draw_polygon_boundaries(draw, _local_areas, projector, viewport)

            # 3. 震源のバツ印
            warn_rgb = _rgb_to_rgba(GIS_MAP_WARNING_COLOR)[:3]
            _draw_x_mark(draw, projector.project(draw_lon, lat), warn_rgb)

            return _finalize(canvas, (width, height))
    except Exception:
        logger.error("GIS地図(海外)描画エラー", exc_info=True)
        return None
