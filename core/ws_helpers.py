"""
core/ws_helpers.py
===================
WebSocket 接続・自動再接続まわりの共通ヘルパー。

EewCog（Wolfx EEW / P2P EEW）と将来的に他のWebSocketクライアントからも
利用できるよう、Cogに依存しない独立関数として提供する。
"""
import asyncio
import logging
import time

import websockets

logger = logging.getLogger("QTLBot")


def format_duration(seconds: float) -> str:
    """経過秒数を「1時間2分」「7分12秒」「45秒」のような日本語表記にする（切断ログ用）。"""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}時間{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


async def ws_connect_loop(
    is_closed_fn,
    url: str,
    label: str,
    handler,
    on_disconnect=None,
    init_delay: int = 5,
    max_delay: int = 60,
    on_connect=None,
):
    """
    WebSocket に接続し、切断時に指数バックオフで自動再接続する共通ループ。

    接続失敗時は指数バックオフで再接続間隔を延長し、DDoS リスクを軽減する：
      初回: 5秒 → 10秒 → 20秒 → ... → 60秒（最大値）→ ずっと60秒

    Parameters
    ----------
    is_closed_fn : Bot が終了処理中かどうかを返す呼び出し可能オブジェクト
                   （例: lambda: bot.is_closed()）
    url          : 接続先 URL
    label        : ログに表示する名称（例: "Wolfx EEW"）
    handler      : 接続後に呼び出す非同期コルーチン関数 (ws) -> None
    on_disconnect: 切断時に呼び出すコールバック（引数なし）。省略可
    init_delay   : 初回再接続待機秒数（デフォルト5秒）
    max_delay    : 最大再接続待機秒数（デフォルト60秒）
    on_connect   : 接続確立（handler呼び出し直前）に呼び出すコールバック
                   （引数なし）。省略可（2026-10-01追加。P2PWebSocketHubが
                   障害時フォールバックの判定に使う）
    """
    delay = init_delay
    consecutive_failures = 0
    while not is_closed_fn():
        connected_at = None  # 接続を確立した時刻（切断ログに接続時間を出し、原因の切り分けに使う）
        try:
            async with websockets.connect(
                url,
                ping_interval=30,
                ping_timeout=15,
                close_timeout=10,
            ) as ws:
                logger.info(f"{label} WebSocket 接続完了")
                connected_at = time.monotonic()
                delay = init_delay  # 接続成功でリセット
                consecutive_failures = 0
                if on_connect is not None:
                    on_connect()
                await handler(ws)
            # サーバーが正常に切断した（close frame あり）場合
            logger.info(
                f"{label} WebSocket が閉じられました（接続時間: "
                f"{format_duration(time.monotonic() - connected_at)}）"
            )
        except asyncio.CancelledError:
            logger.info(f"{label} WebSocket ループがキャンセルされました")
            raise
        except Exception as e:
            if connected_at is not None:
                # 【2026-11修正】接続を確立したあとの切断は、再接続の失敗ではない。従来は
                # 通常の切断でも「再接続失敗 #1」と出て紛らわしく、何分で切れたかも分からなかった。
                # 接続時間を出す（一定時間で切れるなら NAT/ルーターのタイムアウトなどと切り分けられる）。
                logger.warning(
                    f"{label} WebSocket 切断 "
                    f"(接続時間: {format_duration(time.monotonic() - connected_at)}): {e}"
                )
            else:
                consecutive_failures += 1
                logger.warning(
                    f"{label} WebSocket 接続失敗 (再接続失敗 #{consecutive_failures}): {e}"
                )
            if on_disconnect is not None:
                on_disconnect()

        if not is_closed_fn():
            logger.info(
                f"{label} WS: {delay}秒後に再接続 "
                f"(exponential backoff: {consecutive_failures} failures)"
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
