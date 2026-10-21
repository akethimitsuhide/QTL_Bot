"""長周期地震動・その他（cogs/other.py）、USGS、火山の読み上げが、テンプレート経由でも従来と同じ文言になることの確認。"""
import asyncio
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")


import cogs.other as other_mod
import cogs.usgs as usgs_mod
import cogs.volcano as volcano_mod
from core.yomiage import (
    OTHER_DEFAULTS, OTHER_TAGS, USGS_DEFAULTS, USGS_TAGS, VOLCANO_DEFAULTS, VOLCANO_TAGS, TemplateSet,
)


class _Channel:
    async def send(self, *args, **kwargs):
        pass


def _stub(cls, channel_attrs):
    cog = object.__new__(cls)
    for name in channel_attrs:
        setattr(cog, name, _Channel())
    cog.spoken = []

    async def speak_local(text, priority=2):
        cog.spoken.append(text)

    async def noop(*args, **kwargs):
        pass

    cog.speak_local = speak_local
    cog.play_mp3 = noop
    cog.session = None
    return cog


def _other():
    return _stub(other_mod.OtherInfoCog, ("other_channel", "channel"))


def _run(coro):
    asyncio.run(coro)


# ---------------- 長周期地震動 ----------------
_LONG_PERIOD = {
    "Control": {"PublishingOffice": "気象庁"},
    "Body": {
        "Earthquake": {"OriginTime": "2026-10-20T08:20:00+09:00", "Magnitude": "6.8",
                       "Hypocenter": {"Area": {"Name": "能登半島沖"}}},
        "Intensity": {"Observation": {"MaxLgInt": "3", "Pref": [{"Area": [{"Name": "能登", "MaxLgInt": "3"}]}]}},
    },
}


def test_long_period_wording_unchanged():
    cog = _other()
    _run(cog.notify_long_period(detail_data=_LONG_PERIOD, is_test=True))
    assert cog.spoken == ["長周期地震動に関する観測情報。8時20分頃に発生した能登半島沖を震源とする"
                          " マグニチュード6.8の地震により、最大長周期地震動階級3を観測しました。"]


def test_long_period_custom_template(monkeypatch):
    monkeypatch.setattr(other_mod, "_YOMI", TemplateSet(
        OTHER_DEFAULTS, OTHER_TAGS, {"OTHER_LONG_PERIOD": "長周期地震動、階級{最大階級}。[{発生時刻}。]"}))
    cog = _other()
    _run(cog.notify_long_period(detail_data=_LONG_PERIOD, is_test=True))
    assert cog.spoken == ["長周期地震動、階級3。8時20分頃。"]


# ---------------- 気象庁のその他の情報 ----------------
def test_advisory_and_hypocenter_update_wording_unchanged():
    cog = _other()
    _run(cog.notify_quake_advisory(is_test=True, detail_data={
        "Control": {"PublishingOffice": "気象庁"},
        "Head": {"Title": "北海道・三陸沖後発地震注意情報"}, "Body": {"EarthquakeInfo": {"Text": "本文"}}}))
    _run(cog.notify_quake_advisory(is_test=True, detail_data={
        "Control": {"PublishingOffice": "気象庁"},
        "Head": {"Title": "顕著な地震の震源要素更新のお知らせ"},
        "Body": {"Earthquake": {"OriginTime": "2026-10-20T08:20:00+09:00", "Magnitude": "6.8",
                                "Hypocenter": {"Area": {"Name": "能登半島沖"}}}, "Comments": {}}}))
    assert cog.spoken == [
        "北海道・三陸沖後発地震注意情報 が発表されました",
        "8時20分頃の地震について、震源要素を更新しました。 震源地 能登半島沖 マグニチュード M6.8 深さ 不明",
    ]


def test_nankai_wording_unchanged_and_custom(monkeypatch):
    detail = {"Control": {"PublishingOffice": "気象庁"},
              "Head": {"Title": "南海トラフ地震臨時情報（巨大地震警戒）", "ReportDateTime": "2026-10-20T08:20:00+09:00"},
              "Body": {"EarthquakeInfo": {"InfoSerial": {"Name": "巨大地震警戒"}, "Text": "本文"}}}
    cog = _other()
    _run(cog.notify_nankai_trough(detail, is_test=True))
    assert cog.spoken == ["南海トラフ地震臨時情報。巨大地震警戒が発表されました。"]
    monkeypatch.setattr(other_mod, "_YOMI", TemplateSet(
        OTHER_DEFAULTS, OTHER_TAGS, {"OTHER_NANKAI": "{情報種別}。すぐに備えてください。"}))
    cog = _other()
    _run(cog.notify_nankai_trough(detail, is_test=True))
    assert cog.spoken == ["巨大地震警戒。すぐに備えてください。"]


# ---------------- USGS ----------------
def test_usgs_wording_unchanged_and_custom(monkeypatch):
    feature = {"id": "us1", "properties": {"mag": 6.84, "place": "Fiji region", "time": 1760916000000},
               "geometry": {"coordinates": [178.0, -18.0, 10.0]}}
    cog = _stub(usgs_mod.UsgsCog, ("usgs_channel", "channel"))
    _run(cog.notify_usgs_quake(feature))
    assert cog.spoken == ["USGS地震情報。2025年10月20日08時20分、Fiji regionでマグニチュード6.8の地震が発生しました。"]
    monkeypatch.setattr(usgs_mod, "_YOMI", TemplateSet(
        USGS_DEFAULTS, USGS_TAGS, {"USGS_QUAKE": "{場所}でM{マグニチュード}。"}))
    cog = _stub(usgs_mod.UsgsCog, ("usgs_channel", "channel"))
    _run(cog.notify_usgs_quake(feature))
    assert cog.spoken == ["Fiji regionでM6.8。"]


# ---------------- 火山 ----------------
def _volcano():
    return _stub(volcano_mod.VolcanoCog, ("volcano_channel", "channel"))


def test_volcano_wording_unchanged_and_custom(monkeypatch):
    info = {"headTitle": "噴火警戒レベルを引き上げ", "reportDatetime": "2026-10-20T08:20:00+09:00",
            "volcanoInfos": [{"items": [{"code": "03"}]}]}
    eruption = {"headTitle": "火山名　諏訪之瀬島　噴火速報", "reportDatetime": "2026-10-20T08:20:00+09:00"}
    warning = {"reportDatetime": "2026-10-20T08:20:00+09:00", "volcanoInfos": [
        {"type": "対象火山", "items": [{"name": "噴火警報（火口周辺）", "condition": "引上げ",
                                       "areas": [{"name": "浅間山"}]}]}]}
    cog = _volcano()
    _run(cog._notify_volcano(info, "e1"))
    _run(cog._notify_eruption(eruption, "e2"))
    _run(cog._notify_warning(warning, "e3"))
    assert cog.spoken == [
        "火山情報。噴火警戒レベルを引き上げ。警戒レベル3。",
        "噴火速報。諏訪之瀬島で噴火が発生しました。",
        "噴火警報。浅間山で噴火警報（火口周辺）が引上げされました。",
    ]
    monkeypatch.setattr(volcano_mod, "_YOMI", TemplateSet(
        VOLCANO_DEFAULTS, VOLCANO_TAGS,
        {"VOLCANO_ERUPTION_FLASH": "{火山名}が噴火。", "VOLCANO_ERUPTION_WARNING": "{火山名}、{警報名}、[{状況}]。"}))
    cog = _volcano()
    _run(cog._notify_eruption(eruption, "e2"))
    _run(cog._notify_warning(warning, "e3"))
    assert cog.spoken == ["諏訪之瀬島が噴火。", "浅間山、噴火警報（火口周辺）、引上げ。"]
