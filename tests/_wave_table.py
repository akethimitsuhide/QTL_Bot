"""
tests/_wave_table.py
=====================
予報円（core/travel_time.py など）のテスト用に、JMA走時表と同じ形式の合成ファイルを作る補助。
走時 = 震源距離 ÷ 一定速度（P=6.0km/s、S=3.5km/s）。実際の走時表ではない（実ファイルに依存しないため）。
"""
import math

VP = 6.0
VS = 3.5
DEPTHS = [0, 10, 20, 50, 100]
DISTS = list(range(0, 52, 2)) + list(range(55, 205, 5)) + list(range(210, 2010, 10))


def write_table(path, depths=DEPTHS, dists=DISTS):
    lines = []
    for d in depths:
        for x in dists:
            r = math.hypot(d, x)
            lines.append(f"P {r / VP:8.3f} S {r / VS:8.3f} {d:3d} {x:6d}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path
