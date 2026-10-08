"""
GIS地図描画（core/gis_render.py）のスレッド安全性テスト（2026-10-09追加）。

各 render_* は asyncio.to_thread でワーカースレッドから呼ばれる。描画倍率 _scale が
グローバル変数のままだと、複数の描画が同時に走ったとき倍率が混ざって画像が崩れる。
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core import gis_render as g  # noqa: E402


def test_px_scale_is_isolated_per_thread():
    """あるスレッドが _supersampled() の中にいても、他スレッドの _px() には影響しない。"""
    inside = threading.Event()
    release = threading.Event()
    seen = {}

    def worker():
        with g._supersampled():
            seen["worker"] = g._px(10)
            inside.set()
            release.wait(5)

    t = threading.Thread(target=worker)
    t.start()
    assert inside.wait(5)
    try:
        assert g._px(10) == 10            # 別スレッド（ここ）は倍率1のまま
    finally:
        release.set()
        t.join()
    assert seen["worker"] == 10 * g._SUPERSAMPLE
    assert g._px(10) == 10                # 抜けた後も1


@pytest.mark.skipif(not g._ready(), reason="GIS_MAP_ENABLE=true かつ GISデータ取得済みの環境でのみ実行")
def test_concurrent_renders_match_sequential_output():
    """同時に描画しても、逐次描画と同じバイト列（＝倍率が混ざっていない）になる。"""
    jobs = [
        lambda: g.render_shindo_map(hypocenter_lonlat=(139.7, 35.7)),
        lambda: g.render_shindo_map(hypocenter_lonlat=(142.0, 38.0), enlarge_hypocenter_mark=True),
        lambda: g.render_eew_warn_map({"東京都"}, (139.7, 35.7)),
    ]
    baseline = [j() for j in jobs]
    assert all(baseline)
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [(i, ex.submit(jobs[i])) for i in list(range(3)) * 4]
        for i, f in futures:
            assert f.result() == baseline[i]
