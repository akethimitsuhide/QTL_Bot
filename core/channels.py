"""
core/channels.py - 通知チャンネルの複数指定（.env の *_CHANNEL_ID にカンマ区切りで複数のIDを書ける）

【使い方（.env）】
  EEW_CHANNEL_ID=111111111111111111,222222222222222222
  → EEW の通知を 2 つのチャンネル（別サーバーでも可。Bot が参加していること）へ同時に送る。
  区切りは「,」「;」空白のどれでもよい。同じIDの重複は1つにまとめる。
  1つだけ書いた場合は従来と全く同じ動作（この場合は ChannelGroup を使わず、Discord の
  チャンネルをそのまま返す）。

【設計】
  - 各 Cog が使っている Discord チャンネルのインターフェースは `await channel.send(...)` だけ
    （送信結果やチャンネルの属性は使っていない）。そのため、複数のチャンネルをまとめる
    ChannelGroup も `send` だけを同じ形で持たせ、Cog 側の送信処理は変更しない。
  - 送信は全チャンネルへ並行して行う。1つのチャンネルが失敗しても他のチャンネルへは届く。
  - 再送（core/discord_send.py の send_with_retry）との関係:
      * 一時的な失敗（5xx・ネットワーク）が1つでもあれば例外を送出し、send_with_retry が
        全体を再送する。届いているチャンネルへの二重投稿は、チャンネルごとに固定した nonce
        （Discord が同じ nonce の再送を新規投稿にしない）で防ぐ。
      * 権限不足（Forbidden）などの恒久的な失敗は、警告ログを出すだけで他のチャンネルへの
        送信を妨げない（全チャンネルが失敗した場合のみ例外を送出する）。
      * nonce は「同じ投稿者・同じ nonce」で重複判定されるため、チャンネルごとに別の値に
        する（同じ値を使うと2つ目以降のチャンネルへ投稿されない恐れがある）。
  - 添付ファイル（discord.File）は1回の送信で消費されるため、チャンネルごとに作り直す。
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Iterable, Optional, Sequence

import aiohttp
import discord

logger = logging.getLogger("QTLBot")

# 一時的な失敗として再送する例外（core/discord_send.py と共通）
RETRYABLE_EXCEPTIONS = (
    discord.DiscordServerError,
    aiohttp.ClientError,
    asyncio.TimeoutError,
    ConnectionError,
)

_SPLIT_RE = re.compile(r"[,;\s]+")
_MAX_NONCE = (1 << 64) - 1


def parse_channel_ids(raw: Optional[str]) -> tuple[list[int], list[str]]:
    """
    "111,222;333" → ([111, 222, 333], 却下した語のリスト)。順序は保ち、重複は除く。
    数字以外・0以下は却下（却下した語は戻り値で返し、呼び出し側が警告を出す）。
    """
    ids: list[int] = []
    rejected: list[str] = []
    for token in _SPLIT_RE.split((raw or "").strip()):
        if not token:
            continue
        if not token.isdigit() or int(token) <= 0:
            rejected.append(token)
            continue
        value = int(token)
        if value not in ids:
            ids.append(value)
    return ids, rejected


class ChannelGroup:
    """複数の Discord チャンネルへ同じ内容を送る。`send` のみ Discord のチャンネルと同じ形で持つ。"""

    def __init__(self, channels: Sequence):
        if not channels:
            raise ValueError("ChannelGroup には1つ以上のチャンネルが必要です")
        self.channels = tuple(channels)

    @property
    def id(self) -> int:
        """代表（先頭）のチャンネルID（ログ表示用）。"""
        return getattr(self.channels[0], "id", 0)

    def __len__(self) -> int:
        return len(self.channels)

    def __repr__(self) -> str:
        return f"ChannelGroup({[getattr(c, 'id', '?') for c in self.channels]})"

    async def send(self, *args, **kwargs):
        files = _snapshot_files(kwargs)
        base_nonce = kwargs.get("nonce")

        async def _one(index: int, channel):
            kw = dict(kwargs)
            if files is not None:
                kw.pop("file", None)
                kw.pop("files", None)
                made = [discord.File(io.BytesIO(data), filename=name, spoiler=spoiler, description=desc)
                        for data, name, spoiler, desc in files]
                if len(made) == 1 and "files" not in kwargs:
                    kw["file"] = made[0]
                else:
                    kw["files"] = made
            if isinstance(base_nonce, int):
                kw["nonce"] = (base_nonce + index) & _MAX_NONCE      # チャンネルごとに固定の別の値
            return await channel.send(*args, **kw)

        results = await asyncio.gather(
            *(_one(i, ch) for i, ch in enumerate(self.channels)), return_exceptions=True)
        errors = [(ch, r) for ch, r in zip(self.channels, results) if isinstance(r, BaseException)]
        # CancelledError 等は握りつぶさずそのまま伝える
        for _, err in errors:
            if not isinstance(err, Exception):
                raise err
        successes = [r for r in results if not isinstance(r, BaseException)]
        if not errors:
            return successes[0]
        for ch, err in errors:
            logger.warning(f"通知チャンネル {getattr(ch, 'id', '?')} への送信に失敗: {type(err).__name__}: {err}")
        if not successes:
            raise errors[0][1]
        transient = next((e for _, e in errors if isinstance(e, RETRYABLE_EXCEPTIONS)), None)
        if transient is not None:
            raise transient          # 一時的な失敗は send_with_retry に全体を再送させる（nonce で二重投稿を防ぐ）
        return successes[0]          # 恒久的な失敗（権限不足など）は、他のチャンネルへの送信を妨げない


def _snapshot_files(kwargs: dict) -> Optional[list[tuple[bytes, str, bool, Optional[str]]]]:
    """添付ファイルの中身を読み出しておく（チャンネルごとに作り直すため）。添付が無ければ None。"""
    files = list(kwargs.get("files") or [])
    if kwargs.get("file") is not None:
        files.append(kwargs["file"])
    if not files:
        return None
    snap = []
    for f in files:
        f.fp.seek(0)
        snap.append((f.fp.read(), f.filename, bool(getattr(f, "spoiler", False)), getattr(f, "description", None)))
        f.fp.seek(0)
    return snap


def resolve_channels(bot, ids: Iterable[int], fallback=None):
    """
    IDのリストを Discord のチャンネルに解決する。
      - 見つかったのが1つ  → そのチャンネル（従来と同じ）
      - 見つかったのが複数 → ChannelGroup
      - 1つも見つからない  → fallback（従来の「get_channel(ID) or 既定チャンネル」と同じ）
    見つからないIDは警告ログを出す（Bot が参加していない・IDの誤りなど）。
    """
    found = []
    for cid in ids:
        channel = bot.get_channel(cid)
        if channel is None:
            logger.warning(f"通知チャンネルが見つかりません（ID: {cid}）。Bot がそのチャンネルを見られるか、IDが正しいか確認してください")
        else:
            found.append(channel)
    if not found:
        return fallback
    if len(found) == 1:
        return found[0]
    return ChannelGroup(found)
