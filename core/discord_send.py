"""
core/discord_send.py
=====================
Discord への通知送信を、一時的な失敗に対して再送する共通ヘルパー。

【discord.py 自身の再試行との役割分担】（discord.py 2.7.1 のソースで確認）
discord.py は内部で最大5回、次の場合だけ自動で再試行する:
  - 429（レート制限。retry_after 待ち）
  - HTTP 500 / 502 / 504 / 524（1・3・5・7秒待ち）
  - 接続リセット（errno 54 / 10054）
次は再試行されず、そのまま例外になる。ここを補うのがこのヘルパー:
  - HTTP 503 などの DiscordServerError（上記の再試行を使い切った 5xx を含む）
  - DNS 失敗・接続失敗・タイムアウトなどのネットワークエラー（aiohttp.ClientError 等）

【再送しないもの】
Forbidden（権限不足）・NotFound（チャンネル削除）・その他の 4xx・RateLimited、
および引数や実装の誤り（ValueError / TypeError 等）。再送しても直らず、
原因を隠すだけになるため、従来どおり即座に呼び出し元へ例外を返す。

【二重投稿の防止】
Discord の nonce（enforce_nonce は discord.py が自動で付与）を、1回の論理的な送信の間
は固定して使い回す。タイムアウトで失敗扱いになったが実際には届いていた場合でも、
再送は新しいメッセージにならず、既存のメッセージが返る。

【添付ファイル】
BytesIO ベースの discord.File は再送前に先頭へ巻き戻す。ファイルが既に閉じられている
（パス指定で File が自分で開いたもの）場合は安全に再送できないため、再送せず
元の例外を返す（呼び出し側は BytesIO で渡すこと）。

【Webhook への追加配信】
通知を Discord へ送る前に、同じ Embed を Webhook（core/webhook_notify.py）へも配信する。
Webhook の送信は別タスクで、Discord 側の成否・再送とは独立。

【最終的に失敗した場合】
最後の例外をそのまま送出する。呼び出し側の既存の try/except・失敗記録は従来どおり動く。
"""
import asyncio
import logging
import secrets

import aiohttp
import discord

from core.config import DISCORD_SEND_RETRY_ATTEMPTS
from core import webhook_notify

logger = logging.getLogger("QTLBot")

_BASE_DELAY_SEC = 2.0
_MAX_DELAY_SEC = 10.0

# 一時的な失敗として再送する例外
RETRYABLE_EXCEPTIONS = (
    discord.DiscordServerError,
    aiohttp.ClientError,
    asyncio.TimeoutError,
    ConnectionError,
)


def _retry_delay(retry_index: int) -> float:
    """retry_index=0 が最初の再送。2秒 → 4秒 → 8秒 → 10秒（上限）。"""
    return min(_BASE_DELAY_SEC * (2 ** retry_index), _MAX_DELAY_SEC)


def _files_of(kwargs: dict) -> list:
    files = list(kwargs.get("files") or [])
    if kwargs.get("file") is not None:
        files.append(kwargs["file"])
    return files


def _rewind_files(kwargs: dict) -> bool:
    """
    再送のために添付ファイルを先頭へ戻す。戻せない（既に閉じられている）場合は False。
    """
    files = _files_of(kwargs)
    if any(getattr(f.fp, "closed", False) for f in files):
        return False
    for f in files:
        f.reset()
    return True


async def send_with_retry(channel, *args, _attempts: int | None = None, _label: str = "", **kwargs):
    """
    channel.send(*args, **kwargs) を、一時的な失敗に限って再送しながら実行する。

    Parameters
    ----------
    _attempts : 総試行回数（初回を含む）。None なら DISCORD_SEND_RETRY_ATTEMPTS。1 で再送なし。
    _label    : 再送ログに出す識別名（任意）。
    """
    attempts = max(1, _attempts if _attempts is not None else DISCORD_SEND_RETRY_ATTEMPTS)
    # 論理的な1送信につき nonce を固定（再送で二重投稿にならないようにする）
    kwargs.setdefault("nonce", secrets.randbits(64))
    tag = f"[{_label}] " if _label else ""

    # Webhook（Discord / Slack）への追加配信。別タスクで行い、Discord 側の成否とは独立
    # （Discord が不調でも届く）。例外は内部で握りつぶすため、ここで Discord 送信は妨げない。
    webhook_notify.dispatch_from_send(kwargs, _label)

    for attempt in range(1, attempts + 1):
        try:
            return await channel.send(*args, **kwargs)
        except RETRYABLE_EXCEPTIONS as e:
            if attempt >= attempts:
                logger.warning(f"{tag}Discord 送信に失敗しました（{attempt}/{attempts}回目・再送を使い切りました）: {type(e).__name__}: {e}")
                raise
            if not _rewind_files(kwargs):
                logger.warning(f"{tag}Discord 送信に失敗しましたが、添付ファイルが再利用できないため再送しません: {type(e).__name__}: {e}")
                raise
            delay = _retry_delay(attempt - 1)
            logger.warning(
                f"{tag}Discord 送信に失敗（{attempt}/{attempts}回目）: {type(e).__name__}: {e} "
                f"→ {delay:.0f}秒後に再送します"
            )
            await asyncio.sleep(delay)
