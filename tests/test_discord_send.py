"""
Discord 送信の再送ヘルパー（core/discord_send.py）のテスト（2026-10-12追加）。

確認すること:
1. 成功時は1回だけ送信し、nonce が付く
2. 一時的な失敗（503・ネットワークエラー・タイムアウト）は再送し、nonce は全試行で同一（二重投稿防止）
3. 待機は 2秒 → 4秒（指数）。試行を使い切ったら最後の例外をそのまま送出
4. 再送しないもの: Forbidden / NotFound / 400系 / ValueError、CancelledError
5. _attempts=1 なら再送なし
6. BytesIO の添付は再送前に巻き戻される。閉じられたファイルは再送しない
7. 呼び出し側が指定した nonce は尊重される
"""
import asyncio
import io
import os
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import aiohttp  # noqa: E402
import discord  # noqa: E402
import pytest  # noqa: E402

from core import discord_send  # noqa: E402
from core.discord_send import send_with_retry  # noqa: E402


def _resp(status, reason="x"):
    return SimpleNamespace(status=status, reason=reason)


class FakeChannel:
    """side_effects の順に結果を返す/例外を投げる。呼び出しを記録する。"""

    def __init__(self, *effects):
        self._effects = list(effects)
        self.calls = []

    async def send(self, *args, **kwargs):
        self.calls.append((args, dict(kwargs)))
        eff = self._effects.pop(0) if self._effects else "sent"
        if isinstance(eff, BaseException):
            raise eff
        return eff


@pytest.fixture
def sleeps(monkeypatch):
    rec = []

    async def fake_sleep(sec):
        rec.append(sec)

    monkeypatch.setattr(discord_send.asyncio, "sleep", fake_sleep)
    return rec


def run(coro):
    return asyncio.run(coro)


def test_success_sends_once_with_nonce(sleeps):
    ch = FakeChannel("msg")
    assert run(send_with_retry(ch, embed="e")) == "msg"
    assert len(ch.calls) == 1 and "nonce" in ch.calls[0][1] and ch.calls[0][1]["embed"] == "e"
    assert sleeps == []


def test_retries_503_then_succeeds_with_same_nonce(sleeps):
    ch = FakeChannel(discord.DiscordServerError(_resp(503), "unavailable"),
                     discord.DiscordServerError(_resp(503), "unavailable"), "ok")
    assert run(send_with_retry(ch, embed="e")) == "ok"
    assert len(ch.calls) == 3
    nonces = {c[1]["nonce"] for c in ch.calls}
    assert len(nonces) == 1                      # 二重投稿防止のため全試行で同一
    assert sleeps == [2.0, 4.0]


@pytest.mark.parametrize("exc", [
    aiohttp.ClientConnectionError("down"),
    asyncio.TimeoutError(),
    ConnectionResetError("reset"),
])
def test_network_errors_are_retried(exc, sleeps):
    ch = FakeChannel(exc, "ok")
    assert run(send_with_retry(ch, embed="e")) == "ok"
    assert len(ch.calls) == 2


def test_exhausted_raises_last_exception(sleeps):
    errs = [discord.DiscordServerError(_resp(503), f"e{i}") for i in range(3)]
    ch = FakeChannel(*errs)
    with pytest.raises(discord.DiscordServerError) as ei:
        run(send_with_retry(ch, embed="e"))
    assert ei.value is errs[-1]
    assert len(ch.calls) == 3 and sleeps == [2.0, 4.0]


@pytest.mark.parametrize("exc", [
    discord.Forbidden(_resp(403), "no perm"),
    discord.NotFound(_resp(404), "gone"),
    discord.HTTPException(_resp(400), "bad request"),
    ValueError("bug"),
])
def test_non_retryable_raise_immediately(exc, sleeps):
    ch = FakeChannel(exc)
    with pytest.raises(type(exc)):
        run(send_with_retry(ch, embed="e"))
    assert len(ch.calls) == 1 and sleeps == []


def test_cancelled_error_propagates(sleeps):
    ch = FakeChannel(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        run(send_with_retry(ch, embed="e"))
    assert len(ch.calls) == 1


def test_attempts_one_means_no_retry(sleeps):
    ch = FakeChannel(discord.DiscordServerError(_resp(503), "x"), "ok")
    with pytest.raises(discord.DiscordServerError):
        run(send_with_retry(ch, embed="e", _attempts=1))
    assert len(ch.calls) == 1


def test_caller_nonce_is_respected(sleeps):
    ch = FakeChannel("ok")
    run(send_with_retry(ch, embed="e", nonce=12345))
    assert ch.calls[0][1]["nonce"] == 12345


def test_label_and_attempts_are_not_forwarded_to_send(sleeps):
    ch = FakeChannel("ok")
    run(send_with_retry(ch, _label="EEW", _attempts=2, embed="e"))
    assert set(ch.calls[0][1]) == {"embed", "nonce"}


def test_bytesio_file_is_rewound_before_retry(sleeps):
    f = discord.File(io.BytesIO(b"abc"), filename="a.png")
    reads = []

    class Ch(FakeChannel):
        async def send(self, *a, **kw):
            reads.append(kw["file"].fp.read())      # 送信が中身を読み進める状況を再現
            return await super().send(*a, **kw)

    ch = Ch(aiohttp.ClientConnectionError("x"), "ok")
    run(send_with_retry(ch, embed="e", file=f))
    assert reads == [b"abc", b"abc"]


def test_closed_file_is_not_retried(sleeps):
    buf = io.BytesIO(b"abc")
    f = discord.File(buf, filename="a.png")
    buf.close = buf.__class__.close.__get__(buf)  # File が差し替えた close を元に戻す
    buf.close()

    ch = FakeChannel(aiohttp.ClientConnectionError("x"), "ok")
    with pytest.raises(aiohttp.ClientConnectionError):
        run(send_with_retry(ch, embed="e", file=f))
    assert len(ch.calls) == 1


def test_files_list_is_rewound_too(sleeps):
    files = [discord.File(io.BytesIO(b"1"), filename="1.png"), discord.File(io.BytesIO(b"2"), filename="2.png")]
    seen = []

    class Ch(FakeChannel):
        async def send(self, *a, **kw):
            seen.append([x.fp.read() for x in kw["files"]])
            return await super().send(*a, **kw)

    ch = Ch(discord.DiscordServerError(_resp(503), "x"), "ok")
    run(send_with_retry(ch, files=files))
    assert seen == [[b"1", b"2"], [b"1", b"2"]]
