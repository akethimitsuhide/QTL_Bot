"""core/channels.py（通知チャンネルの複数指定）のテスト。"""
import asyncio
import io
import os
from types import SimpleNamespace

import discord
import pytest

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

from core.channels import ChannelGroup, parse_channel_ids, resolve_channels
from core.discord_send import send_with_retry


def _resp(status):
    return SimpleNamespace(status=status, reason="x")


class FakeChannel:
    def __init__(self, cid, fail=None):
        self.id = cid
        self.fail = list(fail or [])      # 呼ばれるたびに先頭から取り出して raise（None なら成功）
        self.sent = []

    async def send(self, *args, **kwargs):
        files = kwargs.get("files") or ([kwargs["file"]] if kwargs.get("file") else [])
        self.sent.append({"args": args, "nonce": kwargs.get("nonce"),
                          "files": [(f.filename, f.fp.read()) for f in files], "embed": kwargs.get("embed")})
        if self.fail:
            exc = self.fail.pop(0)
            if exc is not None:
                raise exc
        return SimpleNamespace(channel_id=self.id)


# ---------- 解析 ----------
def test_parse_channel_ids_flexible_and_deduplicated():
    ids, rejected = parse_channel_ids("111, 222;333  111,,")
    assert ids == [111, 222, 333] and rejected == []


def test_parse_channel_ids_rejects_garbage():
    ids, rejected = parse_channel_ids("111,abc,-5,0,12.5")
    assert ids == [111] and rejected == ["abc", "-5", "0", "12.5"]
    assert parse_channel_ids("") == ([], []) and parse_channel_ids(None) == ([], [])


# ---------- resolve ----------
class FakeBot:
    def __init__(self, channels):
        self._c = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self._c.get(cid)


def test_resolve_single_returns_plain_channel_and_missing_falls_back():
    a, b = FakeChannel(1), FakeChannel(2)
    bot = FakeBot([a, b])
    assert resolve_channels(bot, [1]) is a                       # 1つだけなら従来どおり素のチャンネル
    assert resolve_channels(bot, [99], fallback=b) is b           # 見つからなければ fallback
    assert resolve_channels(bot, [], fallback=None) is None
    group = resolve_channels(bot, [1, 99, 2])                     # 見つからないIDは無視して残りでグループ化
    assert isinstance(group, ChannelGroup) and group.channels == (a, b) and group.id == 1


# ---------- ChannelGroup ----------
def test_group_sends_to_all_with_distinct_stable_nonces():
    a, b = FakeChannel(1), FakeChannel(2)
    group = ChannelGroup([a, b])
    asyncio.run(group.send(embed="E", nonce=1000))
    assert [m["embed"] for m in (a.sent[0], b.sent[0])] == ["E", "E"]
    assert a.sent[0]["nonce"] == 1000 and b.sent[0]["nonce"] == 1001    # チャンネルごとに別の固定値
    asyncio.run(group.send(embed="E", nonce=1000))
    assert a.sent[1]["nonce"] == 1000 and b.sent[1]["nonce"] == 1001    # 再送でも同じ値（二重投稿防止）


def test_group_recreates_files_per_channel():
    a, b = FakeChannel(1), FakeChannel(2)
    f = discord.File(io.BytesIO(b"PNGDATA"), filename="map.png")
    asyncio.run(ChannelGroup([a, b]).send(embed="E", file=f))
    assert a.sent[0]["files"] == [("map.png", b"PNGDATA")] and b.sent[0]["files"] == [("map.png", b"PNGDATA")]
    f2 = [discord.File(io.BytesIO(b"1"), filename="a.png"), discord.File(io.BytesIO(b"2"), filename="b.png")]
    asyncio.run(ChannelGroup([a, b]).send(files=f2))
    assert b.sent[1]["files"] == [("a.png", b"1"), ("b.png", b"2")]


def test_group_permanent_failure_does_not_block_others():
    forbidden = discord.Forbidden(_resp(403), "no permission")
    a, b = FakeChannel(1, fail=[forbidden]), FakeChannel(2)
    asyncio.run(ChannelGroup([a, b]).send(embed="E"))               # 例外にならない
    assert len(b.sent) == 1


def test_group_all_failed_raises_first_error():
    forbidden = discord.Forbidden(_resp(403), "no permission")
    a, b = FakeChannel(1, fail=[forbidden]), FakeChannel(2, fail=[forbidden])
    with pytest.raises(discord.Forbidden):
        asyncio.run(ChannelGroup([a, b]).send(embed="E"))


def test_group_transient_failure_raises_so_that_retry_can_resend():
    a, b = FakeChannel(1), FakeChannel(2, fail=[discord.DiscordServerError(_resp(503), "down")])
    with pytest.raises(discord.DiscordServerError):
        asyncio.run(ChannelGroup([a, b]).send(embed="E"))
    assert len(a.sent) == 1                                        # 成功したチャンネルへは送れている


def test_send_with_retry_over_group_recovers_and_keeps_nonce(monkeypatch):
    monkeypatch.setattr("core.discord_send._BASE_DELAY_SEC", 0.0)
    monkeypatch.setattr("core.discord_send._MAX_DELAY_SEC", 0.0)
    a = FakeChannel(1)
    b = FakeChannel(2, fail=[discord.DiscordServerError(_resp(503), "down"), None])
    asyncio.run(send_with_retry(ChannelGroup([a, b]), embed=None, _attempts=3, _label="テスト"))
    # a には2回（初回＋全体の再送）届くが、nonce が同じなので Discord 側では二重投稿にならない
    assert [m["nonce"] for m in a.sent][0] == [m["nonce"] for m in a.sent][1]
    assert len(b.sent) == 2


def test_group_requires_channels():
    with pytest.raises(ValueError):
        ChannelGroup([])


# ---------- config ----------
def _isolate_channel_env(monkeypatch):
    """実機の .env・環境変数に左右されないようにする（config を reload するテスト用）。

    reload のたびに core.config は .env を読み直し、読み込み済みの環境変数も残る。
    実機では P2P_EEW_CHANNEL_ID などが入っているため、フォールバックの検証が崩れる。
    """
    import core.env_loader
    for key in list(os.environ):
        if key.endswith("_CHANNEL_ID"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(core.env_loader, "load_env_files", lambda *a, **k: [])


def test_config_parses_multiple_channel_ids(monkeypatch):
    import importlib
    _isolate_channel_env(monkeypatch)
    monkeypatch.setenv("CHANNEL_ID", "111, 222")
    monkeypatch.setenv("EEW_CHANNEL_ID", "333;444")
    monkeypatch.setenv("TSUNAMI_CHANNEL_ID", "")
    monkeypatch.setenv("QUAKE_CHANNEL_ID", "abc")
    import core.config as cfg
    cfg = importlib.reload(cfg)
    try:
        assert cfg.CHANNEL_IDS == [111, 222] and cfg.CHANNEL_ID == 111
        assert cfg.EEW_CHANNEL_IDS == [333, 444] and cfg.EEW_CHANNEL_ID == 333
        assert cfg.TSUNAMI_CHANNEL_IDS == [111, 222]                 # 空は CHANNEL_ID へフォールバック
        assert cfg.QUAKE_CHANNEL_IDS == [111, 222]                   # 無効値は無視してフォールバック
        assert cfg.P2P_EEW_CHANNEL_IDS == [333, 444]                 # EEW_CHANNEL_ID へフォールバック
        assert cfg.ADMIN_CHANNEL_IDS == [] and cfg.ADMIN_CHANNEL_ID == 0
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_config_rejects_invalid_channel_id(monkeypatch):
    import importlib
    import core.config as cfg
    _isolate_channel_env(monkeypatch)
    monkeypatch.setenv("CHANNEL_ID", "12x")
    with pytest.raises(SystemExit):
        importlib.reload(cfg)
    monkeypatch.undo()
    importlib.reload(cfg)
