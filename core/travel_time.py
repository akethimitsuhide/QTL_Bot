"""
core/travel_time.py
====================
気象庁の走時表（JMA2001）を読み込み、「震源の深さ」と「地震発生からの経過秒数」から
P波・S波の到達距離（震央距離 km）を求める純関数モジュール。EEW地図の予報円
（core/eew_wave.py → core/gis_render.py）が使う。

【使う走時表】
  JMA2001走時表（観測点標高100m毎のファイル群）のうち、標高0mのファイル
  （既定: data/tjma/tjma2001h/tjma2001h.+00000）だけを読む。
  配布元: https://ds.data.jma.go.jp/eqev/data/bulletin/catalog/appendix/trtime/trt_j.html
  ※ JMA2020 系（A/B/C）は利用条件の確認が済むまで使わない（このモジュールは対象外）。

【ファイル形式（実ファイルの先頭行からの推定。python3 -m core.travel_time で検証できる）】
  1行 = "P  <P波の走時秒> S  <S波の走時秒>  <震源の深さkm>  <震央距離km>"
  例: "P    0.416 S    0.703   0      2"
  メッシュ間隔は気象庁の公開ページによると、震央距離・深さとも 0〜50km=2km、
  50〜200km=5km、200km〜=10km（震央距離は2000km、深さは700kmまで）。
  行の並び順には依存しない（各行の深さ・震央距離の列から表を組み立てる）。
  形式に合わない行は読み飛ばす。

【求め方】
  深さ h の走時曲線 T(Δ) は、h を挟む2つの深さの行を線形補間して作る。経過秒数 t から
  T(Δ)=t となる震央距離 Δ を、曲線上の線形補間で逆引きする。
    - t が T(Δ=0)（直下の地表への到達時間）より小さい: まだ地表に到達していない → None
    - t が表の最大走時を超える（震央距離 2000km 超）: 表の範囲外 → None

標準ライブラリのみ。表は初回利用時に1回だけ読み込む（get_table）。
"""
import bisect
import logging
import math
import os
import threading
from typing import Optional

logger = logging.getLogger("QTLBot")

WAVE_P = "P"
WAVE_S = "S"

# 半径がこれ未満の円は描かない（直下の浅い地震で発生直後など。画像上で点になるだけのため）
MIN_RADIUS_KM = 0.5


def _parse_line(line: str) -> Optional[tuple]:
    """1行を (深さ, 震央距離, P走時, S走時) にする。形式に合わない行・負値・非有限値は None。"""
    parts = line.split()
    if len(parts) < 6 or parts[0] != "P" or parts[2] != "S":
        return None
    try:
        tp = float(parts[1])
        ts = float(parts[3])
        depth = float(parts[4])
        dist = float(parts[5])
    except ValueError:
        return None
    if not all(math.isfinite(v) for v in (tp, ts, depth, dist)):
        return None
    if tp < 0 or ts < 0 or depth < 0 or dist < 0:
        return None
    return depth, dist, tp, ts


def _interp(xs: list, ys: list, x: float) -> float:
    """昇順の xs に対する線形補間。範囲外は端の値（呼び出し側で範囲を確認する前提）。"""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, x)
    x0, x1 = xs[i - 1], xs[i]
    if x1 == x0:
        return ys[i - 1]
    return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - x0) / (x1 - x0)


class TravelTimeTable:
    """深さごとの (震央距離, P走時, S走時) の表。"""

    def __init__(self, rows_by_depth: dict):
        # {深さ: (dists, p_times, s_times)}（dists は昇順・重複なし）
        self._rows = rows_by_depth
        self._depths = sorted(rows_by_depth)

    @classmethod
    def from_file(cls, path: str) -> "TravelTimeTable":
        raw: dict = {}
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parsed = _parse_line(line)
                if parsed is None:
                    continue
                depth, dist, tp, ts = parsed
                raw.setdefault(depth, {})[dist] = (tp, ts)   # 同じ (深さ, 距離) は後の行が優先
        if not raw:
            raise ValueError("走時表として読める行がありません")
        rows = {}
        for depth, by_dist in raw.items():
            dists = sorted(by_dist)
            rows[depth] = (
                dists,
                [by_dist[d][0] for d in dists],
                [by_dist[d][1] for d in dists],
            )
        return cls(rows)

    @property
    def depth_range(self) -> tuple:
        return self._depths[0], self._depths[-1]

    @property
    def depth_count(self) -> int:
        return len(self._depths)

    def distance_range(self) -> tuple:
        lo = min(r[0][0] for r in self._rows.values())
        hi = max(r[0][-1] for r in self._rows.values())
        return lo, hi

    def _curve(self, depth_km: float, wave: str) -> Optional[tuple]:
        """深さ depth_km の走時曲線 (dists, times)。表の深さの範囲外は None。"""
        lo, hi = self.depth_range
        if depth_km < lo or depth_km > hi:
            return None
        idx = 2 if wave == WAVE_S else 1   # 行は (dists, P走時, S走時) の順
        i = bisect.bisect_left(self._depths, depth_km)
        d1 = self._depths[i]
        if d1 == depth_km or i == 0:
            row = self._rows[d1]
            return row[0], row[idx]
        d0 = self._depths[i - 1]
        w = (depth_km - d0) / (d1 - d0)
        row0, row1 = self._rows[d0], self._rows[d1]
        xs = row0[0]
        ts = [
            t0 + (_interp(row1[0], row1[idx], x) - t0) * w
            for x, t0 in zip(xs, row0[idx])
        ]
        return xs, ts

    def travel_time(self, depth_km: float, distance_km: float, wave: str) -> Optional[float]:
        """深さ・震央距離に対する走時（秒）。表の範囲外は None。"""
        curve = self._curve(depth_km, wave)
        if curve is None:
            return None
        xs, ts = curve
        if distance_km < xs[0] or distance_km > xs[-1]:
            return None
        return _interp(xs, ts, distance_km)

    def radius_km(self, depth_km: float, elapsed_sec: float, wave: str) -> Optional[float]:
        """
        地震発生から elapsed_sec 秒後の、wave（"P"/"S"）の到達距離（震央距離 km）。
        まだ地表に届いていない・表の範囲外・半径が MIN_RADIUS_KM 未満の場合は None。
        """
        if depth_km is None or elapsed_sec is None:
            return None
        if not (math.isfinite(depth_km) and math.isfinite(elapsed_sec)):
            return None
        curve = self._curve(depth_km, wave)
        if curve is None:
            return None
        xs, ts = curve
        n = len(xs)
        if elapsed_sec < ts[0]:
            return None   # 直下の地表にもまだ届いていない
        # 後ろから見て、走時が elapsed_sec 以下になる最初の点（最も遠い交点）を探す
        i = n - 1
        while i >= 0 and ts[i] > elapsed_sec:
            i -= 1
        if i < 0:
            return None
        if i == n - 1:
            return xs[-1] if elapsed_sec == ts[-1] else None   # 表の最大走時を超えた
        t0, t1 = ts[i], ts[i + 1]
        x0, x1 = xs[i], xs[i + 1]
        radius = x0 if t1 <= t0 else x0 + (elapsed_sec - t0) / (t1 - t0) * (x1 - x0)
        return radius if radius >= MIN_RADIUS_KM else None


# ---------------------------------------------------------------
# 表の遅延読み込み（プロセス内で1回）
# ---------------------------------------------------------------
_table: Optional[TravelTimeTable] = None
_table_path: Optional[str] = None
_load_failed = False
_lock = threading.Lock()


def get_table(path: str) -> Optional[TravelTimeTable]:
    """
    走時表を返す（初回のみ読み込む）。ファイルが無い・読めない場合は警告を1回だけ出して
    None を返す（以後は再読み込みしない。ファイルを置いた後は Bot の再起動が必要）。
    """
    global _table, _table_path, _load_failed
    if _table is not None and _table_path == path:
        return _table
    with _lock:
        if _table is not None and _table_path == path:
            return _table
        if _load_failed and _table_path == path:
            return None
        _table_path = path
        try:
            _table = TravelTimeTable.from_file(path)
            _load_failed = False
            lo, hi = _table.depth_range
            logger.info(f"走時表を読み込みました (path={path}, 深さ={lo:g}〜{hi:g}km, {_table.depth_count}段)")
        except (OSError, ValueError) as e:
            _table = None
            _load_failed = True
            logger.warning(f"走時表を読み込めないため、EEW地図のP波・S波の円は描画しません (path={path}): {e}")
        return _table


def reset_for_tests() -> None:
    global _table, _table_path, _load_failed
    with _lock:
        _table, _table_path, _load_failed = None, None, False


if __name__ == "__main__":
    # 実ファイルの形式確認用: python3 -m core.travel_time [走時表ファイル]
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "tjma", "tjma2001h", "tjma2001h.+00000",
    )
    try:
        tbl = TravelTimeTable.from_file(target)
    except (OSError, ValueError) as exc:
        print(f"NG: {target} を読み込めません: {exc}")
        raise SystemExit(1)
    d_lo, d_hi = tbl.depth_range
    x_lo, x_hi = tbl.distance_range()
    print(f"OK: {target}")
    print(f"  深さ     : {d_lo:g}〜{d_hi:g} km（{tbl.depth_count}段）")
    print(f"  震央距離 : {x_lo:g}〜{x_hi:g} km")
    print("  深さ10km の到達距離の目安（経過秒数 → P波 / S波の震央距離 km）")
    for sec in (5, 10, 20, 40, 80):
        p = tbl.radius_km(10, sec, WAVE_P)
        s = tbl.radius_km(10, sec, WAVE_S)
        fmt = lambda v: "範囲外/未到達" if v is None else f"{v:7.1f}"
        print(f"    {sec:>3d}秒 → P {fmt(p)} / S {fmt(s)}")
