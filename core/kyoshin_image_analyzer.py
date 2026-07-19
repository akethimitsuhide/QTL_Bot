"""
core/kyoshin_image_analyzer.py
================================
強震モニタのリアルタイム震度画像を解析し、
「実際に揺れを示す色（黄緑色以上）のピクセルが集中しているグリッドセル」
のみを抽出するモジュール。

【設計方針の転換について（重要）】
初版では「グリッド内で最も彩度・明度の高いピクセルを常に代表値として
採用する」方式だったが、これは背景の青色域（平常時、震度0未満）内の
GIF圧縮・アンチエイリアシングによる微細な色相ゆらぎまで
「震度上昇」として拾ってしまい、実際には地震が起きていない瞬間にも
複数グリッドセルが偶然同時に「上昇」判定され、誤検知（False Positive）
を引き起こしていた。

参考:
- https://qiita.com/ingen084/items/82985e8d3227c97c608d
  （強震モニタの画像から揺れていることを検知する／ingen084氏）
- ユーザー提供の詳細仕様書（HSVマスク・DBSCANクラスタリング・
  複数フレーム検証を組み合わせた揺れ検知アルゴリズム）

この反省を踏まえ、以下の方針に転換した:
1. HSVマスク処理を最初に行い、「震度1相当（黄緑）以上」の色を持つ
   ピクセルのみを"揺れ候補ピクセル"として抽出する。
   背景の青色域（震度0未満）は最初から解析対象に含めない
   （=常時ノイズの発生源そのものを除去する）。
2. グリッドセル単位で「揺れ候補ピクセルの個数」を数え、
   一定数以上（MIN_ACTIVE_PIXELS）存在するセルのみを
   "アクティブセル"として扱う。単一の孤立ピクセル
   （アンチエイリアシングの縁など）では反応しない。
3. クラスタリング・複数フレーム検証は
   core/kyoshin_cluster_tracker.py が担当する
   （このモジュールは単一フレームの解析のみを行う）。

【設計方針: なぜグリッド分割方式なのか】
本来、観測点ごとの震度を得るには「全国の観測点の座標・ピクセル位置対応表」
が必要だが、そのデータは持っていない。代わりに画像を N×N ピクセルの
グリッドセルに分割し、セルの座標を疑似観測点として扱う。

【色相→震度変換テーブルについて】
HUE_TO_SHINDO_ANCHORS は、防災科研(NIED)公式のリアルタイム震度
カラースケール画像（震度7[赤]〜震度-3[青]の凡例）を実際に
ピクセル解析して作成した実データ較正済みのテーブル。
"""
from __future__ import annotations

import colorsys
import logging
from dataclasses import dataclass

logger = logging.getLogger("QTLBot")

# ===============================
# 色相 → 震度(実数値) 変換テーブル
# ===============================
HUE_TO_SHINDO_ANCHORS: list[tuple[float, float]] = [
    (0.00, 6.54), (2.19, 5.92), (4.82, 5.76), (7.44, 5.60), (10.00, 5.44),
    (12.57, 5.28), (15.12, 5.12), (17.65, 4.96), (20.71, 4.80), (23.53, 4.63),
    (26.35, 4.47), (29.18, 4.31), (32.24, 4.15), (34.82, 3.99), (37.88, 3.83),
    (40.71, 3.67), (43.76, 3.51), (46.59, 3.34), (49.65, 3.18), (52.30, 3.02),
    (53.41, 2.86), (54.82, 2.70), (56.00, 2.54), (57.18, 2.38), (58.59, 2.22),
    (59.68, 2.05), (62.37, 1.89), (64.78, 1.73), (67.47, 1.57), (69.92, 1.41),
    (72.68, 1.25), (75.25, 1.09), (80.00, 0.92), (85.45, 0.76), (91.75, 0.60),
    (97.78, 0.44), (105.22, 0.28), (112.50, 0.12), (120.32, -0.04), (127.50, -0.20),
    (135.00, -0.37), (141.52, -0.53), (148.51, -0.69), (154.95, -0.85), (162.06, -1.01),
    (172.46, -1.17), (186.07, -1.33), (197.23, -1.49), (207.48, -1.66), (215.06, -1.82),
    (222.62, -1.98), (224.94, -2.14), (227.54, -2.30), (230.04, -2.46), (232.94, -2.62),
    (235.81, -2.78), (239.37, -2.95),
]

BACKGROUND_VALUE_THRESHOLD = 0.05   # これ未満のV(明度)は背景(黒)として無視

# ===============================
# 揺れ色抽出のしきい値（HSVマスク処理）
# ===============================
# 「震度1相当」以上（テーブル上では概ね hue<=76°程度）を"揺れ候補色"とする。
# 参考資料の「黄色以上を抽出する」という考え方に基づく。
# これより弱い色（背景の青〜水色〜緑域）は、GIFノイズの温床であるため
# 最初から解析対象に含めない。
ACTIVE_SHINDO_FLOOR = 1.0

# 1グリッドセル内で、この個数以上の「揺れ候補ピクセル」が
# 存在しない限り、そのセルはアクティブとみなさない
# （アンチエイリアシングによる単一の縁ピクセルでの誤反応を防ぐ）。
MIN_ACTIVE_PIXELS = 3


def hue_to_realtime_shindo(hue_deg: float) -> float:
    """
    色相（度、0〜360）をリアルタイム震度（実数値）に変換する。

    HUE_TO_SHINDO_ANCHORS は 0°(赤)〜239.37°(青)の範囲のみで構成された
    非循環テーブルである。しかし colorsys.rgb_to_hsv が返す実際の hue は
    0〜360°の循環値であり、240°〜360°（紫・マゼンタ域）は凡例に
    一切存在しない配色（文字・アイコン等のUI要素の色である可能性が高い）。
    この範囲を239.37°側へ丸めてしまうと、無関係な色が最大震度(6.54)相当
    として誤検出される（240°付近と358°付近が同一視されてしまう）ため、
    239.37°〜300°未満は最弱値へフォールバックさせ、300°以上は
    テーブル上の「赤(0°)」に近いとみなして丸める。
    """
    h = hue_deg % 360
    if h > 239.37:
        if h < 300:
            return HUE_TO_SHINDO_ANCHORS[-1][1]  # 凡例外の色 → 最弱値扱い
        h = 0.0  # 300°以上は赤(0°)に近い色相とみなす

    anchors = HUE_TO_SHINDO_ANCHORS
    for i in range(len(anchors) - 1):
        h1, s1 = anchors[i]
        h2, s2 = anchors[i + 1]
        if h1 <= h <= h2:
            ratio = (h - h1) / (h2 - h1) if h2 != h1 else 0
            return s1 + (s2 - s1) * ratio
    return anchors[-1][1]


def _shindo_to_hue_ceiling(shindo_floor: float) -> float:
    """
    「この震度以上」に対応する色相の上限値(度)を、
    HUE_TO_SHINDO_ANCHORS を逆引きして求める。
    （テーブルは hue が増えるほど shindo が減る単調減少なので、
    shindo_floor 以上となる hue の範囲は [0, ceiling] になる）
    """
    anchors = HUE_TO_SHINDO_ANCHORS
    for i in range(len(anchors) - 1):
        h1, s1 = anchors[i]
        h2, s2 = anchors[i + 1]
        if s1 >= shindo_floor >= s2:
            if s1 == s2:
                return h2
            ratio = (s1 - shindo_floor) / (s1 - s2)
            return h1 + (h2 - h1) * ratio
    return anchors[-1][0]


@dataclass
class GridCellReading:
    cell_id: str
    shindo: float
    x: int
    y: int
    active_pixel_count: int = 0


class KyoshinImageAnalyzer:
    """
    強震モニタ画像から、揺れ色（震度1相当以上）のピクセルが
    一定数以上集中しているグリッドセルのみを抽出するアナライザ。

    背景の青色域は最初から解析対象に含めないため、
    平常時（地震が起きていない瞬間）は原則として
    アクティブセルが0件になる（＝ノイズ源そのものの除去）。
    """

    def __init__(self, grid_size: int = 10,
                 active_shindo_floor: float = ACTIVE_SHINDO_FLOOR,
                 min_active_pixels: int = MIN_ACTIVE_PIXELS):
        self.grid_size = grid_size
        self.active_shindo_floor = active_shindo_floor
        self.min_active_pixels = min_active_pixels
        self._hue_ceiling = _shindo_to_hue_ceiling(active_shindo_floor)

    def analyze(self, image) -> list[GridCellReading]:
        """
        PIL.Image を受け取り、アクティブなグリッドセルのみの
        GridCellReading リストを返す（非アクティブなセルは含まれない）。
        """
        if image.mode != "RGB":
            image = image.convert("RGB")

        w, h = image.size
        pixels = image.load()
        readings: list[GridCellReading] = []

        for gy in range(0, h, self.grid_size):
            for gx in range(0, w, self.grid_size):
                active_pixels: list[float] = []  # このセル内の揺れ候補ピクセルの震度値

                for y in range(gy, min(gy + self.grid_size, h)):
                    for x in range(gx, min(gx + self.grid_size, w)):
                        r, g, b = pixels[x, y]
                        hh, ss, vv = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                        if vv < BACKGROUND_VALUE_THRESHOLD:
                            continue  # 背景（黒）
                        hue_deg = hh * 360
                        if hue_deg > self._hue_ceiling:
                            continue  # 揺れ色（黄緑以上）ではない → 背景の青色域なので無視
                        active_pixels.append(hue_to_realtime_shindo(hue_deg))

                if len(active_pixels) >= self.min_active_pixels:
                    cell_id = f"g{gx // self.grid_size}_{gy // self.grid_size}"
                    readings.append(GridCellReading(
                        cell_id=cell_id,
                        shindo=max(active_pixels),  # セル内の最大震度を代表値とする
                        x=gx, y=gy,
                        active_pixel_count=len(active_pixels),
                    ))

        return readings

    def build_station_grid(self, image_width: int, image_height: int) -> dict[str, list[str]]:
        """
        画像サイズから、全グリッドセルの station_id と
        8近傍セルの近隣リストを機械的に生成する。
        """
        cols = (image_width + self.grid_size - 1) // self.grid_size
        rows = (image_height + self.grid_size - 1) // self.grid_size

        result: dict[str, list[str]] = {}
        for gy in range(rows):
            for gx in range(cols):
                cell_id = f"g{gx}_{gy}"
                neighbors = []
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        nx, ny = gx + dx, gy + dy
                        if 0 <= nx < cols and 0 <= ny < rows:
                            neighbors.append(f"g{nx}_{ny}")
                result[cell_id] = neighbors
        return result
