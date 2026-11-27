"""
core/error_notify_queue.py
===========================
管理者チャンネルへのエラー通知が「ネットワーク断で送れなかった」場合に、
通知を捨てずに保留し、復旧後にまとめて送るための部品（純関数・単純なクラスのみ）。

【背景】
DNS 障害や回線断の間は Discord へ接続できないため、管理者チャンネルへのエラー通知も
失敗する。従来は通知済みとして記録したうえで ERROR（トレースバック付き）を出して
終わりだったため、①切断中にログが大量に荒れる ②復旧後も通知が届かない（同じエラーは
1時間抑制されるため）という問題があった。

【方針】
- 接続系の例外（is_network_error）で送信に失敗した通知は PendingErrorQueue に保留する。
- 復旧後（接続の再開・定期リトライ）に build_flush_text で1通にまとめて送る。
- 保留は件数上限つき（古いものから破棄し、破棄件数を記録）。メモリを増やさない。
- Discord・core.config には依存しない（環境変数なしで単体テストできる）。
"""
from __future__ import annotations

import asyncio
import socket
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import aiohttp
import discord

# 保留する通知の最大件数（超えたら古いものから破棄する）
PENDING_MAX_ITEMS = 50
# まとめ通知に載せる最大件数（残りは「ほか N 件」）
FLUSH_MAX_LINES = 10
# 1件あたりの表示文字数
_LINE_MAX_CHARS = 120

# Discord 側の障害を示す HTTP ステータス（一時的なので保留して再送する）
_RETRYABLE_HTTP_STATUS = (500, 502, 503, 504)


def is_network_error(exc: BaseException | None) -> bool:
    """
    例外（または原因の連鎖）が「接続できない・応答が無い」系かどうかを返す。
    DNS 失敗・接続拒否・タイムアウト・Discord ゲートウェイ切断・Discord 側の 5xx が該当。
    権限エラー（Forbidden）・不正リクエスト（4xx）などは、再送しても直らないため該当しない。
    """
    seen = 0
    while exc is not None and seen < 6:
        if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError,
                            asyncio.TimeoutError, ConnectionError, socket.gaierror,
                            discord.GatewayNotFound, discord.ConnectionClosed)):
            return True
        if isinstance(exc, discord.HTTPException) and exc.status in _RETRYABLE_HTTP_STATUS:
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


@dataclass
class PendingError:
    """保留中のエラー通知1件。"""
    err_type: str
    message: str
    first_at: datetime
    count: int = 1


class PendingErrorQueue:
    """送れなかったエラー通知の保留キュー（キーは notify_error の重複判定ハッシュ）。"""

    def __init__(self, max_items: int = PENDING_MAX_ITEMS):
        self._max = max_items
        self._items: "OrderedDict[object, PendingError]" = OrderedDict()
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._items)

    def add(self, key: object, err_type: str, message: str, now: datetime) -> None:
        """同じキーは件数を加算し、新しいキーは末尾に追加する（上限超過は古い順に破棄）。"""
        item = self._items.get(key)
        if item is not None:
            item.count += 1
            return
        self._items[key] = PendingError(err_type, message, now)
        while len(self._items) > self._max:
            self._items.popitem(last=False)
            self.dropped += 1

    def drain(self) -> tuple[list[PendingError], int]:
        """全件と破棄件数を取り出して空にする。"""
        items = list(self._items.values())
        dropped = self.dropped
        self._items.clear()
        self.dropped = 0
        return items, dropped

    def restore(self, items: Iterable[PendingError], dropped: int) -> None:
        """送信に失敗した分を先頭へ戻す（送信中に追加された分は後ろに残す）。"""
        newer = list(self._items.items())
        self._items.clear()
        for i, it in enumerate(items):
            self._items[("restored", i)] = it   # ハッシュ値と衝突しないタプルキー
        for k, it in newer:
            self._items[k] = it
        self.dropped += dropped
        while len(self._items) > self._max:
            self._items.popitem(last=False)
            self.dropped += 1


def build_flush_text(items: list[PendingError], dropped: int) -> str:
    """保留分を1通にまとめる本文（Embed の description 用）。"""
    lines = []
    for it in items[:FLUSH_MAX_LINES]:
        msg = " ".join(it.message.split())
        if len(msg) > _LINE_MAX_CHARS:
            msg = msg[:_LINE_MAX_CHARS - 1] + "…"
        times = f" ×{it.count}" if it.count > 1 else ""
        lines.append(f"• `{it.first_at.strftime('%H:%M:%S')}` **{it.err_type}**{times}: {msg}")
    rest = len(items) - FLUSH_MAX_LINES
    if rest > 0:
        lines.append(f"…ほか {rest} 件")
    if dropped:
        lines.append(f"（保留上限のため古い {dropped} 件は破棄されました）")
    return "\n".join(lines)
