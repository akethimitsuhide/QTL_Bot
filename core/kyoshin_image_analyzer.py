"""
core/kyoshin_image_analyzer.py
================================
強震モニタのリアルタイム震度画像を解析し、
「疑似観測点（グリッドセル）ごとのリアルタイム震度」を抽出するモジュール。

【設計方針: なぜグリッド分割方式なのか】
本来、観測点ごとの震度を得るには「全国の観測点の座標・ピクセル位置対応表」
が必要だが、そのデータは持っていない。

代わりに、画像を N×N ピクセルのグリッドセルに分割し、
各セル内で最も彩度・明度の高い非背景色ピクセルを代表値として抽出、
その色相(Hue)を震度に変換する。グリッドセルの座標そのものを
「疑似観測点ID」として扱い、隣接する8セルを「近隣観測点」とする。

この方式には以下の利点がある:
- 外部の観測点データ（座標・近隣リスト）が一切不要
- core.kyoshin_detector.EventManager にそのまま統合できる
  （register_station() をグリッド全セル分、機械的に生成できる）

一方で以下の制約がある:
- 「地名」との対応付けはできない（グリッド座標のみ）
- グリッドサイズが粗いと、隣接する強い揺れと弱い揺れが
  同一セルに混在し、代表値が不正確になる可能性がある
- 南西諸島（奄美大島〜石垣島）は画像左上に本土から離れて配置されている
  （近隣グリッドが本土クラスタとは自然に分離される）

【色相→震度変換テーブルについて】
HUE_TO_SHINDO_ANCHORS は、防災科研(NIED)公式のリアルタイム震度
カラースケール画像（震度7[赤]〜震度-3[青]の凡例）を実際に
ピクセル解析して作成した「実データ較正済み」のテーブル。
震度7(H=0°)から震度-2.95(H≈239°)まで、公式配色の全域をカバーしている。

ただし以下の点に注意:
- H=0°(純粋な赤)は震度6.14〜7.00の範囲で色が変化しないため、
  この極域だけは色相のみでの精密な区別ができない
  （テーブルでは代表値として震度6.54を割り当てている）。
  正確な最大震度が必要な用途では、この点を踏まえること。
- カラースケール画像には彩度・明度の情報も含まれるが、
  本テーブルは色相(Hue)のみを使った近似である。
"""
from __future__ import annotations

import colorsys
import logging
from dataclasses import dataclass

logger = logging.getLogger("QTLBot")

# ===============================
# 色相 → 震度(実数値) 変換テーブル
# ===============================
# 防災科研(NIED)公式カラースケール画像を実際に解析して作成した較正済みテーブル。
# (色相[度], リアルタイム震度) のペアを、震度7→震度-3の順（色相0°→239°の順）に格納。
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

# 背景色（黒）とみなす明度・彩度のしきい値
BACKGROUND_VALUE_THRESHOLD = 0.05   # これ未満のV(明度)は背景として無視


def hue_to_realtime_shindo(hue_deg: float) -> float:
    """
    色相（度、0〜360）をリアルタイム震度（実数値。例: 4.5=震度5弱相当）に変換する。
    HUE_TO_SHINDO_ANCHORS（NIED公式カラースケール実測値）を線形補間して求める。
    """
    h = hue_deg
    if h > 239.37:
        h = 239.37  # 最も青い(=最も弱い)色相でクリップ
    if h < 0:
        h = 0

    anchors = HUE_TO_SHINDO_ANCHORS
    for i in range(len(anchors) - 1):
        h1, s1 = anchors[i]
        h2, s2 = anchors[i + 1]
        if h1 <= h <= h2:
            ratio = (h - h1) / (h2 - h1) if h2 != h1 else 0
            return s1 + (s2 - s1) * ratio
    return anchors[-1][1]


@dataclass
class GridCellReading:
    cell_id: str
    shindo: float
    x: int
    y: int


class KyoshinImageAnalyzer:
    """
    強震モニタ画像をグリッド分割し、セルごとの疑似震度を抽出するアナライザ。
    """

    def __init__(self, grid_size: int = 10):
        """
        Parameters
        ----------
        grid_size : グリッドセルの一辺のピクセル数（大きいほど粗いが処理は軽い）
        """
        self.grid_size = grid_size

    def analyze(self, image) -> list[GridCellReading]:
        """
        PIL.Image を受け取り、グリッドセルごとの GridCellReading のリストを返す。
        画像は RGB モードに変換済みであることを想定（呼び出し側で convert("RGB") 済みでも良い）。
        """
        if image.mode != "RGB":
            image = image.convert("RGB")

        w, h = image.size
        pixels = image.load()
        readings: list[GridCellReading] = []

        for gy in range(0, h, self.grid_size):
            for gx in range(0, w, self.grid_size):
                best_shindo = None
                best_score = -1.0  # 彩度×明度が最大のピクセルを代表値とする

                for y in range(gy, min(gy + self.grid_size, h)):
                    for x in range(gx, min(gx + self.grid_size, w)):
                        r, g, b = pixels[x, y]
                        hh, ss, vv = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                        if vv < BACKGROUND_VALUE_THRESHOLD:
                            continue  # 背景（黒）はスキップ
                        score = ss * vv
                        if score > best_score:
                            best_score = score
                            best_shindo = hue_to_realtime_shindo(hh * 360)

                if best_shindo is not None:
                    cell_id = f"g{gx // self.grid_size}_{gy // self.grid_size}"
                    readings.append(GridCellReading(
                        cell_id=cell_id, shindo=best_shindo, x=gx, y=gy,
                    ))

        return readings

    def build_station_grid(self, image_width: int, image_height: int) -> dict[str, list[str]]:
        """
        画像サイズから、全グリッドセルの station_id と
        8近傍セルの近隣リストを機械的に生成する。
        core.kyoshin_detector.EventManager.register_station() への
        一括登録に使う。

        Returns
        -------
        dict[cell_id, list[neighbor_cell_id]]
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