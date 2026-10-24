"""通知種類ごとの ON/OFF（EEW_ENABLE・QUAKE_ENABLE・VOLCANO_ENABLE）のテスト。"""
import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

import cogs.eew as eew_mod
import cogs.quake as quake_mod
import cogs.volcano as volcano_mod
from core import runtime_settings


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _flags(**overrides):
    """指定した項目だけ上書きし、残りは実際の実行時設定の値を返す。"""
    return SimpleNamespace(get=lambda key: overrides[key] if key in overrides else runtime_settings.get(key))


def _stub(cls, attrs):
    cog = object.__new__(cls)
    cog.channel = _Channel()
    for a in attrs:
        setattr(cog, a, cog.channel)
    cog.spoken = []

    async def speak_local(text, priority=2):
        cog.spoken.append(text)

    async def noop(*args, **kwargs):
        pass

    cog.speak_local, cog.play_mp3, cog.play_quake_sound, cog.session = speak_local, noop, noop, None
    return cog


_QUAKE = {"id": "1", "issue": {"type": "DetailScale", "source": "気象庁"},
          "earthquake": {"time": "2026/10/20 08:20:00", "maxScale": 45, "domesticTsunami": "None",
                         "hypocenter": {"name": "能登半島沖", "magnitude": 6.1, "depth": 10}},
          "points": [{"addr": "志賀町", "scale": 45, "pref": "石川県"}], "comments": {}}
_ERUPTION = {"headTitle": "火山名　諏訪之瀬島　噴火速報", "reportDatetime": "2026-10-20T08:20:00+09:00"}


def test_new_flags_are_runtime_settings_and_default_on():
    for key in ("EEW_ENABLE", "QUAKE_ENABLE", "VOLCANO_ENABLE"):
        assert key in runtime_settings.SPEC_BY_KEY and runtime_settings.SPEC_BY_KEY[key].kind == "bool"
        assert runtime_settings.get(key) is True


def test_quake_off_stops_everything_but_test_notifications_still_work(monkeypatch):
    monkeypatch.setattr(quake_mod, "rs", _flags(QUAKE_ENABLE=False))
    cog = _stub(quake_mod.QuakeInfoCog, ("quake_channel",))
    asyncio.run(cog.notify_quake(_QUAKE))
    assert cog.channel.sent == [] and cog.spoken == []
    asyncio.run(cog.notify_quake(_QUAKE, is_test=True))             # --test_* などのテスト通知は止めない
    assert cog.channel.sent and cog.spoken


def test_quake_on_notifies(monkeypatch):
    monkeypatch.setattr(quake_mod, "rs", _flags())
    cog = _stub(quake_mod.QuakeInfoCog, ("quake_channel",))
    asyncio.run(cog.notify_quake(_QUAKE))
    assert cog.channel.sent and cog.spoken


def test_volcano_off_and_on(monkeypatch):
    cog = _stub(volcano_mod.VolcanoCog, ("volcano_channel",))
    monkeypatch.setattr(volcano_mod, "rs", _flags(VOLCANO_ENABLE=False))
    asyncio.run(cog._notify_eruption(_ERUPTION, "e1"))
    assert cog.channel.sent == [] and cog.spoken == []
    monkeypatch.setattr(volcano_mod, "rs", _flags())
    asyncio.run(cog._notify_eruption(_ERUPTION, "e1"))
    assert cog.channel.sent and cog.spoken == ["噴火速報。諏訪之瀬島で噴火が発生しました。"]


def test_eew_off_sends_nothing(monkeypatch):
    data = {"EventID": "E1", "Serial": 1, "isWarn": False, "Hypocenter": "三陸沖", "MaxIntensity": "3"}
    monkeypatch.setattr(eew_mod, "rs", _flags(EEW_ENABLE=False))
    cog = _stub(eew_mod.EewCog, ("eew_channel", "p2p_eew_channel"))
    asyncio.run(cog.notify_eew(data, start_monitor=False))
    assert cog.channel.sent == [] and cog.spoken == []
