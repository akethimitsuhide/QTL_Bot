"""津波の読み上げテンプレート（.env.yomiage の YOMI_TSUNAMI_*）のテスト。"""
import asyncio
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

import cogs.tsunami as tsunami_mod
from core.tsunami_speech import build_offshore_observation_speech, build_tide_time_speech
from core.yomiage import TSUNAMI_DEFAULTS, TSUNAMI_TAGS, TemplateSet


def _yomi(**overrides):
    return TemplateSet(TSUNAMI_DEFAULTS, TSUNAMI_TAGS, overrides)


_OFFSHORE = {
    "Head": {"ReportDateTime": "2026-04-20T17:44:00+09:00"},
    "Body": {"Tsunami": {"Observation": {"Item": [{"Area": {"Name": None}, "Station": [
        {"Name": "宮城金華山沖", "FirstHeight": {}, "MaxHeight": {"Condition": "観測中"}}]}]}}},
}

_TIDE = {"Body": {"Tsunami": {"Forecast": {"Item": [
    {"Area": {"Name": "岩手県"}, "Category": {"Kind": {"Name": "津波警報", "Code": "51"}},
     "FirstHeight": {"ArrivalTime": "2026-04-20T17:00:00+09:00", "Condition": "ただちに津波来襲と予測"},
     "MaxHeight": {"TsunamiHeight": "3"}}]}}}}


def test_offshore_custom_template():
    y = _yomi(TSUNAMI_OFFSHORE="沖で津波を観測。[{発表時刻}。][{観測点}です。]")
    assert build_offshore_observation_speech(_OFFSHORE, yomi=y)[0] == "沖で津波を観測。17時44分頃。宮城金華山沖です。"


def test_offshore_default_unchanged_without_yomi():
    text, pri = build_offshore_observation_speech(_OFFSHORE)
    assert text == "沖合の津波観測に関する情報。17時44分頃、次の沖合で津波を観測しました。宮城金華山沖。" and pri == 2


def test_tide_time_custom_template_and_default():
    text, pri, _ = build_tide_time_speech(_TIDE)
    assert text == ("津波の到達予想時刻を発表しました。最も早い到達は、17時00分頃、岩手県です。"
                    "岩手県ではただちに津波が来襲すると予測されています。")
    y = _yomi(TSUNAMI_TIDE_TIME="{予報区}に{到達予想時刻}到達。[{即時地域}は急いで避難。]")
    assert build_tide_time_speech(_TIDE, yomi=y)[0] == "岩手県に17時00分頃到達。岩手県は急いで避難。"


# ---- notify_tsunami（P2P地震情報の津波予報）を実際に通す ----
class _Channel:
    async def send(self, *args, **kwargs):
        pass


def _cog():
    cog = object.__new__(tsunami_mod.TsunamiCog)
    cog.tsunami_channel = cog.channel = _Channel()
    cog.spoken = []

    async def speak_local(text, priority=2):
        cog.spoken.append(text)

    async def noop(*args, **kwargs):
        pass

    cog.speak_local = speak_local
    cog.play_mp3 = noop
    cog._play_ews_signal = noop
    cog.session = None
    return cog


def _p2p(cancelled=False, grade="Warning", names=("岩手県",)):
    return {"id": "t1", "cancelled": cancelled, "issue": {"time": "2026/04/20 17:00:00", "source": "気象庁"},
            "areas": [{"name": n, "grade": grade, "immediate": False} for n in names]}


def test_p2p_issued_and_cancel_wording_unchanged():
    cog = _cog()
    asyncio.run(cog.notify_tsunami(_p2p(), is_test=True))
    asyncio.run(cog.notify_tsunami(_p2p(cancelled=True), is_test=True))
    assert cog.spoken == ["岩手県に津波警報が発表されました", "津波情報が解除されました"]


def test_p2p_custom_template(monkeypatch):
    monkeypatch.setattr(tsunami_mod, "_YOMI", _yomi(TSUNAMI_P2P_ISSUED="{区分}。{地域}。すぐ逃げてください。"))
    cog = _cog()
    asyncio.run(cog.notify_tsunami(_p2p(names=("岩手県", "宮城県", "福島県", "茨城県")), is_test=True))
    assert cog.spoken == ["津波警報。岩手県、宮城県、福島県等。すぐ逃げてください。"]


# ---- notify_tsunami_forecast（気象庁の津波警報・注意報・予報）を実際に通す ----
def _jma(code, kind, headline=""):
    return {"Control": {"PublishingOffice": "気象庁"},
            "Head": {"Title": "津波警報・注意報・予報", "ReportDateTime": "2026-04-20T17:00:00+09:00",
                     "EventID": "E", "Headline": {"Text": headline}},
            "Body": {"Tsunami": {"Forecast": {"Item": [
                {"Area": {"Name": "岩手県"}, "Category": {"Kind": {"Name": kind, "Code": code}}}]}}}}


def test_jma_issued_and_cancel_wording_unchanged():
    expected = {("51", "津波警報", ""): "津波警報が発表されました",
                ("50", "津波警報解除", ""): "津波警報が解除されました",
                ("60", "津波注意報解除", "津波注意報を解除しました。"): "津波注意報を解除しました。"}
    for (code, kind, headline), text in expected.items():
        cog = _cog()
        asyncio.run(cog.notify_tsunami_forecast(_jma(code, kind, headline), is_test=True))
        assert cog.spoken == [text]


def test_jma_custom_templates(monkeypatch):
    monkeypatch.setattr(tsunami_mod, "_YOMI", _yomi(
        TSUNAMI_JMA_ISSUED="{区分}です。避難してください。",
        TSUNAMI_JMA_CANCEL="{解除対象}の解除。[{気象庁見出し}]"))
    cog = _cog()
    asyncio.run(cog.notify_tsunami_forecast(_jma("51", "津波警報"), is_test=True))
    asyncio.run(cog.notify_tsunami_forecast(_jma("50", "津波警報解除"), is_test=True))
    assert cog.spoken == ["津波警報です。避難してください。", "津波警報の解除。"]
