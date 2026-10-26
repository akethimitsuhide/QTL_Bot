"""EewCog.generate_and_speak_eew が読み上げテンプレート経由でも従来と同じ文言を出すことの確認。"""
import asyncio
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

import cogs.eew as eew_mod
from core.yomiage import EEW_DEFAULTS, EEW_TAGS, TemplateSet


def _cog():
    cog = object.__new__(eew_mod.EewCog)
    cog.eew_state = {"E1": {}}
    cog.spoken = []

    async def speak_local(text, priority=2):
        cog.spoken.append((priority, text))

    cog.speak_local = speak_local
    cog._update_cumulative_warn_areas = lambda event_id, data, now: set(data.get("_areas", []))
    return cog


def _run(cog, data):
    asyncio.run(cog.generate_and_speak_eew(data))


def test_first_warn_matches_previous_wording():
    cog = _cog()
    _run(cog, {"EventID": "E1", "Serial": 1, "isWarn": True, "Hypocenter": "三陸沖",
               "MaxIntensity": "5弱", "_areas": ["岩手県"]})
    assert cog.spoken == [(1, "緊急地震速報。岩手県では強い揺れに警戒してください。三陸沖で地震。推定最大震度5弱。")]


def test_first_forecast_plum_and_update_flow():
    cog = _cog()
    _run(cog, {"EventID": "E1", "Serial": 1, "isWarn": False, "isAssumption": True,
               "Hypocenter": "石川県能登地方", "MaxIntensity": "3"})
    _run(cog, {"EventID": "E1", "Serial": 2, "isWarn": False, "Hypocenter": "石川県能登地方", "MaxIntensity": "4"})
    _run(cog, {"EventID": "E1", "Serial": 3, "isWarn": False, "Hypocenter": "能登半島沖", "MaxIntensity": "4"})
    assert cog.spoken == [
        (3, "石川県能登地方で地震。推定最大震度3。（PLUM法）"),
        (2, "推定最大震度4"),
        (3, "能登半島沖で地震。推定最大震度4。"),
    ]


def test_custom_template_is_applied(monkeypatch):
    monkeypatch.setattr(eew_mod, "_YOMI", TemplateSet(
        EEW_DEFAULTS, EEW_TAGS,
        {"EEW_FIRST_FORECAST": "{震源}、[マグニチュード{マグニチュード}、][深さ{深さ}キロ。]"}))
    cog = _cog()
    _run(cog, {"EventID": "E1", "Serial": 1, "Hypocenter": "宮城県沖", "MaxIntensity": "4", "Magnitude": 6.1})
    assert cog.spoken == [(3, "宮城県沖、マグニチュード6.1、")]     # 深さ不明の [ ] は省略される


# ── キャンセル報の読み上げ（EEW_CANCEL） ──
def _cancel_cog(monkeypatch):
    cog = object.__new__(eew_mod.EewCog)
    cog.spoken, cog.sounds, cog.sent = [], [], []
    cog.eew_channel = cog.p2p_eew_channel = cog.channel = object()
    cog.recent_eews, cog.recent_eews_max_size = {}, 50
    cog.eew_state, cog._eew_state_max_size = {}, 50
    cog._last_eew_report = {}
    cog.monitored_event_id = None
    cog.eew_monitor_timeout_task = None

    async def speak_local(text, priority=2):
        cog.spoken.append((priority, text))

    async def play_eew_sound(data, cumulative_warn_areas=None):
        cog.sounds.append(data.get("isCancel"))

    async def fake_send(channel, **kw):
        cog.sent.append(kw["embed"].description)

    cog.speak_local = speak_local
    cog.play_eew_sound = play_eew_sound
    monkeypatch.setattr(eew_mod, "send_with_retry", fake_send)
    monkeypatch.setattr(eew_mod, "record_delivery", lambda *a, **k: None)
    monkeypatch.setattr(eew_mod, "record_notification", lambda *a, **k: None)
    monkeypatch.setattr(eew_mod.rs, "get", lambda key, *a, **k: True, raising=False)
    return cog


def test_cancel_report_is_spoken(monkeypatch):
    cog = _cancel_cog(monkeypatch)
    # P2P のキャンセル報は震源などを含まない最小形（EventID・Serial・isCancel のみ）でも読み上げる
    asyncio.run(cog.notify_eew({"EventID": "E1", "Serial": 2, "isCancel": True, "OriginTime": "不明"}))
    assert cog.spoken == [(1, "先程の緊急地震速報はキャンセルされました。")]
    assert cog.sent and "キャンセルされました" in cog.sent[0]
    assert cog.sounds == [True]            # 従来どおりキャンセル音も鳴らす


def test_cancel_custom_template(monkeypatch):
    monkeypatch.setattr(eew_mod, "_YOMI", TemplateSet(
        EEW_DEFAULTS, EEW_TAGS, {"EEW_CANCEL": "{震源}の緊急地震速報は取り消されました。"}))
    cog = _cancel_cog(monkeypatch)
    asyncio.run(cog.notify_eew({"EventID": "E1", "Serial": 2, "isCancel": True, "Hypocenter": "三陸沖"}))
    assert cog.spoken == [(1, "三陸沖の緊急地震速報は取り消されました。")]
