"""地震感知情報のレベル閾値フィルター（MIN_LEVEL / MAX_LEVEL）のテスト（2026-10-27追加）。

レベルは数値が大きいほど信頼度が高い（1が最低・4が最高）。従来のドキュメントは逆に書かれており、
MAX_LEVEL=1 では「信頼度が最も低いレベル1だけ」が通知されていた。信頼度で絞るための MIN_LEVEL を追加。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest  # noqa: E402

import cogs.jishin_kanchi as jk  # noqa: E402

# P2P仕様書の confidence 定数値 → レベル
CONF = {1: 0.97015, 2: 0.96774, 3: 0.97024, 4: 0.98052}


class _Passed(BaseException):
    """フィルターを通過したことを検知するための目印（except Exception に捕まらない）。"""


def _passes(monkeypatch, level: int, min_level: int, max_level: int) -> bool:
    monkeypatch.setattr(jk, "JISHIN_KANCHI_MIN_LEVEL", min_level)
    monkeypatch.setattr(jk, "JISHIN_KANCHI_MAX_LEVEL", max_level)
    monkeypatch.setattr(jk, "JISHIN_KANCHI_MIN_COUNT", 1)

    def passed(_confidence):
        raise _Passed

    monkeypatch.setattr(jk, "confidence_to_level_label", passed)   # フィルター通過直後に呼ばれる
    cog = jk.JishinKanchiCog.__new__(jk.JishinKanchiCog)
    cog.jishin_kanchi_channel = cog.channel = object()
    try:
        asyncio.run(cog.notify_jishin_kanchi({"confidence": CONF[level], "count": 5}))
    except _Passed:
        return True
    return False


@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_defaults_notify_every_level(monkeypatch, level):
    assert _passes(monkeypatch, level, min_level=1, max_level=4) is True


def test_min_level_suppresses_low_confidence(monkeypatch):
    got = [lv for lv in (1, 2, 3, 4) if _passes(monkeypatch, lv, min_level=3, max_level=4)]
    assert got == [3, 4]                    # 信頼度の高いレベル3・4だけ


def test_max_level_1_only_lets_lowest_confidence_through(monkeypatch):
    """既存設定（MAX_LEVEL=1）の挙動は変えていない。最も信頼度が低いレベル1のみ通知される。"""
    got = [lv for lv in (1, 2, 3, 4) if _passes(monkeypatch, lv, min_level=1, max_level=1)]
    assert got == [1]


def test_min_and_max_combine(monkeypatch):
    got = [lv for lv in (1, 2, 3, 4) if _passes(monkeypatch, lv, min_level=2, max_level=3)]
    assert got == [2, 3]


def test_min_above_max_notifies_nothing(monkeypatch):
    assert [lv for lv in (1, 2, 3, 4) if _passes(monkeypatch, lv, min_level=4, max_level=2)] == []


def test_hidden_confidence_is_still_skipped(monkeypatch):
    monkeypatch.setattr(jk, "JISHIN_KANCHI_MIN_LEVEL", 1)
    monkeypatch.setattr(jk, "JISHIN_KANCHI_MAX_LEVEL", 4)
    monkeypatch.setattr(jk, "confidence_to_level_label", lambda c: (_ for _ in ()).throw(_Passed()))
    cog = jk.JishinKanchiCog.__new__(jk.JishinKanchiCog)
    cog.jishin_kanchi_channel = cog.channel = object()
    asyncio.run(cog.notify_jishin_kanchi({"confidence": 0, "count": 5}))   # 例外が出なければ通知スキップ
