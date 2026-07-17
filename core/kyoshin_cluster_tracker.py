"""
core/kyoshin_cluster_tracker.py
=================================
KyoshinImageAnalyzer が抽出した「アクティブなグリッドセル」に対して、
1. 連結成分解析（グリッド隣接ベースのクラスタリング。DBSCANの離散版）
2. 複数フレーム連続での持続確認（時系列検証）
を行い、「本物の地震である可能性が高いクラスタ」のみを
confirmed（確定）として返すモジュール。

【設計方針の背景】
参考: ユーザー提供の詳細仕様書（HSVマスク・DBSCANクラスタリング・
複数フレーム検証を組み合わせた揺れ検知アルゴリズム）

KyoshinImageAnalyzer が「震度1相当以上の色を持つピクセルが
一定数集中しているセル」だけを返すようになったことで、
背景ノイズの大部分は除去された。しかし以下のリスクはまだ残る:
- 単一セルだけが偶然アクティブになる孤立ノイズ
  （GIF圧縮アーティファクト、極端に小さい着色領域等）
- 1フレームだけ偶然アクティブになった後、次のフレームで消える瞬間的な揺らぎ

これらを排除するため、
1. アクティブセルを「8近傍で連結しているセルの塊」ごとにクラスタ化し、
   最小サイズ（MIN_CLUSTER_SIZE）未満のクラスタは孤立ノイズとして捨てる。
2. 同じクラスタ（≒重なりのある領域）が複数フレーム連続で
   存在し続けているかを追跡し、REQUIRED_CONSECUTIVE_FRAMES 回
   連続して確認できたクラスタのみを「confirmed」として返す。

【EventManagerとの役割分担】
本トラッカーは「候補クラスタが本物の地震らしいか」を判定する層。
一方 core.kyoshin_detector.EventManager は、confirmed 済みの
観測点（セル）を受け取ってイベントのライフサイクル
（マージ・動的タイマー・フェーズ管理）を管理する層として使う。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.kyoshin_image_analyzer import GridCellReading

logger = logging.getLogger("QTLBot")

# クラスタとして認めるための最小メンバー数（これ未満は孤立ノイズとして無視）
MIN_CLUSTER_SIZE = 3

# 「本物の地震」として確定させるために必要な連続フレーム数
REQUIRED_CONSECUTIVE_FRAMES = 2

# 前フレームのクラスタと「同一」とみなすための最小共有セル数
MIN_OVERLAP_CELLS = 1


def _parse_cell_coords(cell_id: str) -> tuple[int, int]:
    """'g12_34' 形式の cell_id から (12, 34) を取り出す。"""
    body = cell_id[1:]
    gx_str, gy_str = body.split("_")
    return int(gx_str), int(gy_str)


def _cluster_cells_by_adjacency(readings: list[GridCellReading]) -> list[set[str]]:
    """
    アクティブセル群を8近傍の隣接関係で連結成分に分割する
    （DBSCANの、グリッド隣接を距離指標とした離散版に相当）。
    """
    coord_to_id = {}
    for r in readings:
        gx, gy = _parse_cell_coords(r.cell_id)
        coord_to_id[(gx, gy)] = r.cell_id

    visited: set[str] = set()
    clusters: list[set[str]] = []

    for r in readings:
        if r.cell_id in visited:
            continue
        # BFSで連結成分を1つ探索
        stack = [r.cell_id]
        component: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur in component:
                continue
            component.add(cur)
            visited.add(cur)
            gx, gy = _parse_cell_coords(cur)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    neighbor_coord = (gx + dx, gy + dy)
                    neighbor_id = coord_to_id.get(neighbor_coord)
                    if neighbor_id and neighbor_id not in component:
                        stack.append(neighbor_id)
        clusters.append(component)

    return clusters


@dataclass
class _TrackedCluster:
    member_cells: set[str]
    streak: int = 1              # 連続して確認できたフレーム数
    confirmed: bool = False


@dataclass
class ClusterTrackerResult:
    confirmed_cell_ids: set[str] = field(default_factory=set)
    """今フレームで confirmed 判定された全セルID（EventManagerへ供給する対象）"""


class ClusterTracker:
    """
    複数フレームにわたってクラスタを追跡し、
    「一定サイズ以上のクラスタが、一定フレーム数以上連続して
    存在し続けている」場合のみ confirmed とするトラッカー。
    """

    def __init__(self,
                 min_cluster_size: int = MIN_CLUSTER_SIZE,
                 required_consecutive_frames: int = REQUIRED_CONSECUTIVE_FRAMES,
                 min_overlap_cells: int = MIN_OVERLAP_CELLS):
        self.min_cluster_size = min_cluster_size
        self.required_consecutive_frames = required_consecutive_frames
        self.min_overlap_cells = min_overlap_cells
        self._tracked: list[_TrackedCluster] = []

    def update(self, readings: list[GridCellReading]) -> ClusterTrackerResult:
        """
        今フレームのアクティブセル群を受け取り、
        クラスタリング→前フレームとの継続性追跡→確定判定を行う。
        """
        raw_clusters = _cluster_cells_by_adjacency(readings)

        # 最小サイズ未満のクラスタは孤立ノイズとして除外
        candidate_clusters = [c for c in raw_clusters if len(c) >= self.min_cluster_size]

        new_tracked: list[_TrackedCluster] = []
        matched_candidate_indices: set[int] = set()

        # 既存の追跡中クラスタと、今フレームの候補クラスタを重なりでマッチング
        for prev in self._tracked:
            best_match_idx = None
            best_overlap = 0
            for i, cand in enumerate(candidate_clusters):
                if i in matched_candidate_indices:
                    continue
                overlap = len(prev.member_cells & cand)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_match_idx = i

            if best_match_idx is not None and best_overlap >= self.min_overlap_cells:
                matched_candidate_indices.add(best_match_idx)
                cand = candidate_clusters[best_match_idx]
                streak = prev.streak + 1
                confirmed = prev.confirmed or streak >= self.required_consecutive_frames
                new_tracked.append(_TrackedCluster(
                    member_cells=cand, streak=streak, confirmed=confirmed,
                ))
                if confirmed and not prev.confirmed:
                    logger.info(
                        f"KyoshinClusterTracker: クラスタを確定(confirmed)しました "
                        f"(サイズ={len(cand)}, 継続フレーム数={streak})"
                    )
            # マッチしなかった追跡中クラスタは今フレームで消滅 → 破棄（streakリセット）

        # マッチしなかった新規候補クラスタは、streak=1の新規追跡対象として登録
        for i, cand in enumerate(candidate_clusters):
            if i not in matched_candidate_indices:
                new_tracked.append(_TrackedCluster(member_cells=cand, streak=1, confirmed=False))

        self._tracked = new_tracked

        result = ClusterTrackerResult()
        for tc in self._tracked:
            if tc.confirmed:
                result.confirmed_cell_ids |= tc.member_cells
        return result
