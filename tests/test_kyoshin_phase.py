"""
tests/test_kyoshin_phase.py
===========================
2026-10-17追加: 強震モニタ検知の揺れの強さを4段階（弱い揺れ／揺れ／強い揺れ／
非常に強い揺れ）で扱うことと、latest.json の配信元のテスト。
通知・ログに震度階級への換算結果を出さない方針のため、ここで検証するのは
内部のフェーズ判定と定性的な表示名のみ。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest  # noqa: E402

from core.kyoshin_detector import DetectorConfig, _phase_from_shindo  # noqa: E402
from cogs import kyoshin_monitor as km  # noqa: E402


@pytest.mark.parametrize("shindo,expected", [
    (-3.0, "Weak"), (-1.2, "Weak"), (0.0, "Weak"), (0.4, "Weak"),
    (0.5, "Medium"), (1.0, "Medium"), (3.4, "Medium"), (4.4, "Medium"),
    (4.5, "Strong"), (5.0, "Strong"), (5.4, "Strong"),
    (5.5, "Stronger"), (6.5, "Stronger"), (6.9, "Stronger"),
])
def test_phase_boundaries(shindo, expected):
    assert _phase_from_shindo(shindo, DetectorConfig()) == expected


def test_phase_labels_are_qualitative_only():
    assert km.PHASE_LABEL_JA == {
        "Weak": "弱い揺れ", "Medium": "揺れ", "Strong": "強い揺れ", "Stronger": "非常に強い揺れ",
    }
    for label in km.PHASE_LABEL_JA.values():
        assert "震度" not in label and not any(c.isdigit() for c in label)


def test_legacy_weaker_is_treated_as_weakest():
    assert km._phase_index("Weaker") == km._phase_index("Weak") == 0
    assert km._phase_index("Stronger") == 3


def test_latest_json_uses_img_svr_host():
    assert km.KYOSHIN_LATEST_TIME_API == (
        "https://www.lmoni.bosai.go.jp/img_svr/webservice/server/pros/latest.json"
    )
    # 画像の配信元と同じホストであること（片方だけ旧ホストのまま、を防ぐ）
    assert km.KYOSHIN_LATEST_TIME_API.split("/")[2] == km.JMA_S_BASE.split("/")[2]


def test_latest_failure_warning_is_rate_limited(caplog):
    cog = km.KyoshinMonitorCog.__new__(km.KyoshinMonitorCog)
    with caplog.at_level("WARNING", logger="QTLBot"):
        cog._warn_latest_failure("a")
        cog._warn_latest_failure("b")
    assert sum("latest.json" in r.message for r in caplog.records) == 1


# ---------- 振動レベル音は振動レベルの受信1回につき1回（2026-10-17） ----------
def _cog_for_sound():
    cog = km.KyoshinMonitorCog.__new__(km.KyoshinMonitorCog)
    cog._vib_at = None
    cog._vib_sound_at = None
    played = []

    async def fake_play(level):
        played.append(level)

    cog._play_vibration_sound = fake_play
    return cog, played


def test_vibration_sound_once_per_received_sample():
    cog, played = _cog_for_sound()

    async def run():
        # 1回目の受信 → 通知ループが1秒間隔で2回呼んでも音は1回
        cog._vib_at = 100.0
        await cog._play_vibration_sound_once_per_sample(150)
        await cog._play_vibration_sound_once_per_sample(150)
        # 2秒後の次の受信 → もう1回
        cog._vib_at = 102.0
        await cog._play_vibration_sound_once_per_sample(900)
        await cog._play_vibration_sound_once_per_sample(900)

    asyncio.run(run())
    assert played == [150, 900]


def test_vibration_sound_not_played_without_sample():
    cog, played = _cog_for_sound()
    asyncio.run(cog._play_vibration_sound_once_per_sample(150))
    assert played == []
