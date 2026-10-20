"""QuakeInfoCog.notify_quake の読み上げ文が、テンプレート経由でも従来と同じになることの確認。"""
import asyncio
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

import cogs.quake as quake_mod
from core.yomiage import QUAKE_DEFAULTS, QUAKE_TAGS, TemplateSet


class _Channel:
    async def send(self, *args, **kwargs):
        pass


def _cog():
    cog = object.__new__(quake_mod.QuakeInfoCog)
    cog.quake_channel = cog.channel = _Channel()
    cog.spoken = []

    async def speak_local(text, priority=2):
        cog.spoken.append(text)

    async def noop(*args, **kwargs):
        pass

    cog.speak_local = speak_local
    cog.play_mp3 = noop
    cog.play_quake_sound = noop
    return cog


def _detail_scale(max_scale=45, tsunami="None"):
    return {
        "id": "1",
        "issue": {"type": "DetailScale", "source": "気象庁"},
        "earthquake": {"time": "2026/10/20 08:20:00", "maxScale": max_scale, "domesticTsunami": tsunami,
                       "hypocenter": {"name": "能登半島沖", "magnitude": 6.1, "depth": 10}},
        "points": [{"addr": "志賀町", "scale": 45, "pref": "石川県"}, {"addr": "輪島市", "scale": 45, "pref": "石川県"}],
        "comments": {},
    }


def test_strong_detail_scale_matches_previous_wording():
    cog = _cog()
    asyncio.run(cog.notify_quake(_detail_scale(), is_test=True))
    assert cog.spoken == [
        "【テスト】各地の震度に関する情報。8時20分頃、最大震度5弱を観測する地震がありました。"
        " 震源地は 能登半島沖。 震源の深さは 約10km。 マグニチュードは M6.1 と推定されます。"
        "この地震で、最大震度5弱を、志賀町、輪島市で観測しました。 この地震による津波の心配はありません。"
    ]


def test_custom_template_is_applied(monkeypatch):
    monkeypatch.setattr(quake_mod, "_YOMI", TemplateSet(
        QUAKE_DEFAULTS, QUAKE_TAGS,
        {"QUAKE_SCALE_DEST_STRONG": "最大震度{最大震度}、{最大震度観測地点}。[{津波}]"}))
    cog = _cog()
    asyncio.run(cog.notify_quake(_detail_scale(tsunami="Unknown"), is_test=True))
    assert cog.spoken == ["最大震度5弱、志賀町、輪島市。"]          # 津波の案内が無いので [ ] は省略
