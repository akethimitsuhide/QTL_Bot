"""
core/quake_history_backfill.py
================================
core/quake_history_log.py の構造化ログ（QUAKE_RECORD_V1）を追加する
以前に発生した地震について、P2P地震情報 API から可能な範囲で遡って
情報を取得し、qtlbot.log へ書き足す「一括バックフィル」を提供する。

【実行方法】
    python3 bot.py --backfill_quake_history

【なぜ「完全な過去分の復元」ではなく「可能な範囲」なのか】
qtlbot.log に元々残っている地震情報は "P2P地震情報取得: id=xxxx" 程度
で、緯度経度・マグニチュード等の詳細を持たない。そのため、過去分を
復元するにはログ内のidを手がかりに、P2P地震情報 API から改めて詳細
JSONを取得する必要がある。

しかし P2P地震情報 API の GET /v2/history は「特定のidを指定して
1件だけ取得する」エンドポイントを提供していない。取得できるのは
「?offset=N&limit=M で新しい順にページ送りする」方式のみで、実運用上
このoffsetにも上限があり、無制限に過去へ遡れるわけではない
（QUAKE_HISTORY_BACKFILL_MAX_ITEMS で安全な上限を設定している）。

そのため本モジュールは「idを個別指定して確実に復元する」のではなく、
「直近の履歴を新しい順にページ送りして取得し、まだ構造化ログに
残っていないidがあれば書き足す」という、ベストエフォートの実装に
とどめている。QUAKE_HISTORY_BACKFILL_MAX_ITEMS（既定1000件）より
古い地震はこの方法では復元できない（実害としては、qtlbot.logの
保有期間よりAPIの実質的な遡り可能範囲の方が狭い場合、その差分は
復元できない、という制約になる）。
"""
import asyncio
import logging

import aiohttp

from core.config import (
    P2P_API,
    QUAKE_HISTORY_BACKFILL_MAX_ITEMS,
    QUAKE_HISTORY_BACKFILL_PAGE_SIZE,
    QUAKE_HISTORY_BACKFILL_REQUEST_DELAY_SEC,
)
from core.constants import QUAKE_TYPE_MAP
from core.quake_history_log import (
    build_quake_record, format_quake_record_log_line, load_quake_history,
)

logger = logging.getLogger("QTLBot")


async def _fetch_page(session: aiohttp.ClientSession, offset: int, limit: int) -> list[dict]:
    """P2P地震情報APIから1ページ分（code=551のみ）を取得する。失敗時は空リストを返す。"""
    try:
        async with session.get(
            P2P_API,
            params={"codes": 551, "limit": limit, "offset": offset},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"backfill_quake_history: HTTP {resp.status} (offset={offset})")
                return []
            data = await resp.json()
            return data if isinstance(data, list) else []
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"backfill_quake_history: 取得エラー (offset={offset}): {type(e).__name__}: {e}")
        return []


async def backfill_quake_history(
    max_items: int | None = None,
    page_size: int | None = None,
) -> tuple[int, int]:
    """
    P2P地震情報APIを新しい順にページ送りして取得し、まだ
    core.quake_history_log の構造化ログに存在しないidのみ、
    qtlbot.log へ QUAKE_RECORD_V1 行として書き足す。

    Returns
    -------
    (examined, newly_logged) : (APIから取得できた件数, 新たに書き足した件数)
    """
    max_items = max_items or QUAKE_HISTORY_BACKFILL_MAX_ITEMS
    page_size = page_size or QUAKE_HISTORY_BACKFILL_PAGE_SIZE

    existing_ids = {r["id"] for r in load_quake_history()}
    logger.info(
        f"backfill_quake_history: 開始します "
        f"（既存記録 {len(existing_ids)} 件、最大取得件数 {max_items} 件）"
    )

    examined = 0
    newly_logged = 0

    async with aiohttp.ClientSession() as session:
        offset = 0
        while offset < max_items:
            limit = min(page_size, max_items - offset)
            page = await _fetch_page(session, offset, limit)
            if not page:
                logger.info(f"backfill_quake_history: offset={offset} で空応答のため終了します")
                break

            for item in page:
                examined += 1
                item_id = item.get("id") or item.get("_id")
                if not item_id or item_id in existing_ids:
                    continue

                issue_type = item.get("issue", {}).get("type", "Other")
                title = QUAKE_TYPE_MAP.get(issue_type, "地震情報")
                record = build_quake_record(item, title)
                if record is None:
                    continue

                logger.info(format_quake_record_log_line(record))
                existing_ids.add(item_id)
                newly_logged += 1

            offset += len(page)
            if len(page) < limit:
                # APIがこれ以上古いデータを返さなくなった（実質的な下限に到達）
                logger.info(f"backfill_quake_history: offset={offset} 付近で件数が減少したため終了します")
                break

            await asyncio.sleep(QUAKE_HISTORY_BACKFILL_REQUEST_DELAY_SEC)

    logger.info(
        f"backfill_quake_history: 完了しました "
        f"（APIから確認 {examined} 件、新規に記録 {newly_logged} 件）"
    )
    return examined, newly_logged
