"""
tests/test_eew_dual_source_dedupe.py
====================================
Wolfx と P2P の両経路から同じ EEW 第1報が届いたときの、読み上げ・効果音の重複防止のテスト（2026-11追加）。

不具合（2026-11-19 の熊本県天草・芦北地方の警報で実機ログに出た）:
  - 両経路は同じ eew_state[EventID] を共有するが、読み上げは serial == 1 なら前回の内容を見ずに
    常に「初報」として読んでいたため、「緊急地震速報。…」が2回読み上げられた。
  - 効果音は、警報の初報で high_alert を鳴らしても「初報」の印が付かず、2回目の初報で
    low_alert が続けて鳴った。
修正後: 同じ種別（警報/予報）の第1報を読み上げ済みなら通常の差分判定に回す。
警報/予報の種別が違う第1報や、内容が変わった報は従来どおり読み上げる。テスト通知は対象外。
"""
import asyncio
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

import cogs.eew as eew_mod  # noqa: E402


def _cog():
    cog = object.__new__(eew_mod.EewCog)
    cog.eew_state = {}
    cog.spoken, cog.mp3 = [], []

    async def speak_local(text, priority=2):
        cog.spoken.append((priority, text))

    async def play_mp3(key):
        cog.mp3.append(key)

    cog.speak_local = speak_local
    cog.play_mp3 = play_mp3
    return cog


def _deliver(cog, data, is_test=False):
    """notify_eew の読み上げ〜効果音の部分（累積地域の更新 → 読み上げ → 効果音）を1回分実行する。"""
    areas = cog._update_cumulative_warn_areas(
        data["EventID"], {**data, "WarnArea": [{"Chiiki": "熊本県", "Type": "予報"}] if data.get("isWarn") else []}, 1.0)

    async def run():
        await cog.generate_and_speak_eew(data, areas, is_test=is_test)
        await cog.play_eew_sound(data, areas)
    asyncio.run(run())


def _report(serial=1, warn=True, intensity="5弱", hypo="熊本県天草・芦北地方", **kw):
    return {"EventID": "E1", "Serial": serial, "isWarn": warn, "isAssumption": serial == 1,
            "Hypocenter": hypo, "MaxIntensity": intensity, **kw}


def test_same_first_warn_from_two_sources_is_voiced_once():
    """実機の再現: P2P と Wolfx の両方から同じ警報の第1報 → 読み上げ1回・効果音は high_alert のみ。"""
    cog = _cog()
    _deliver(cog, _report())          # P2P 経由
    _deliver(cog, _report())          # Wolfx 経由（同じ報）
    assert len(cog.spoken) == 1 and cog.spoken[0][0] == 1
    assert cog.mp3 == ["high_alert"]


def test_same_first_forecast_from_two_sources_is_voiced_once():
    cog = _cog()
    _deliver(cog, _report(warn=False, intensity="3"))
    _deliver(cog, _report(warn=False, intensity="3"))
    assert len(cog.spoken) == 1
    assert cog.mp3 == ["eew3"]


def test_later_reports_are_still_voiced_after_duplicate_first():
    cog = _cog()
    _deliver(cog, _report())
    _deliver(cog, _report())          # 重複（無音）
    _deliver(cog, _report(serial=2, intensity="6弱"))   # 震度が変わった続報
    assert [p for p, _ in cog.spoken] == [1, 2]
    assert "推定最大震度6弱" in cog.spoken[1][1]
    assert cog.mp3 == ["high_alert", "koushin"]


def test_first_warn_after_first_forecast_is_still_voiced():
    """安全面: 予報の第1報を読んだ後に届いた『警報』の第1報は、種別が違うので初報として必ず読む。"""
    cog = _cog()
    _deliver(cog, _report(warn=False, intensity="3"))
    _deliver(cog, _report(warn=True, intensity="5弱"))
    assert [p for p, _ in cog.spoken] == [3, 1]
    assert "緊急地震速報" in cog.spoken[1][1]


def test_repeated_first_with_changed_content_is_still_voiced():
    """同じ第1報でも、震度が違う内容が別経路から届いたら、差分として読む（握りつぶさない）。"""
    cog = _cog()
    _deliver(cog, _report(intensity="5弱"))
    _deliver(cog, _report(intensity="6弱"))
    assert len(cog.spoken) == 2 and "推定最大震度6弱" in cog.spoken[1][1]


def test_test_notifications_are_not_deduplicated():
    """テスト通知は同じ EventID を繰り返し使うため、毎回読み上げる（従来どおり）。"""
    cog = _cog()
    _deliver(cog, _report(), is_test=True)
    _deliver(cog, _report(), is_test=True)
    assert len(cog.spoken) == 2


def test_single_source_flow_is_unchanged():
    """単一経路の通常の流れ（初報 → 続報 → 最終報）の読み上げ・効果音は従来どおり。"""
    cog = _cog()
    _deliver(cog, _report(warn=False, intensity="3"))
    _deliver(cog, _report(serial=2, warn=False, intensity="4"))
    _deliver(cog, _report(serial=3, warn=False, intensity="4", isFinal=True))
    assert [p for p, _ in cog.spoken] == [3, 2]
    assert cog.mp3 == ["eew3", "koushin", "saisyu"]
