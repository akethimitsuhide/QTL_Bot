"""
core/ws_helpers.py
===================
WebSocket 接続・自動再接続まわりの共通ヘルパー。

EewCog（Wolfx EEW / P2P EEW）と将来的に他のWebSocketクライアントからも
利用できるよう、Cogに依存しない独立関数として提供する。
"""
import asyncio
import logging

import websockets

logger = logging.getLogger("QTLBot")


async def ws_connect_loop(
    is_closed_fn,
    url: str,
    label: str,
    handler,
    on_disconnect=None,
    init_delay: int = 5,
    max_delay: int = 60,
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
    """
    delay = init_delay
    consecutive_failures = 0
    while not is_closed_fn():
        try:
            async with websockets.connect(
                url,
                ping_interval=30,
                ping_timeout=15,
                close_timeout=10,
            ) as ws:
                logger.info(f"{label} WebSocket 接続完了")
                delay = init_delay  # 接続成功でリセット
                consecutive_failures = 0
                await handler(ws)
        except asyncio.CancelledError:
            logger.info(f"{label} WebSocket ループがキャンセルされました")
            raise
        except Exception as e:
            consecutive_failures += 1
            logger.warning(
                f"{label} WebSocket 切断 (再接続失敗 #{consecutive_failures}): {e}"
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
