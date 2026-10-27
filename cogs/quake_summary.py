"""
cogs/quake_summary.py
=====================
日次・週次の地震サマリー（2026-10-27〜）。

QUAKE_SUMMARY_DAILY_ENABLE / QUAKE_SUMMARY_WEEKLY_ENABLE が有効なときだけ、指定時刻に
前日（または直前7日）の地震を要約して投稿する。どちらも無効なら何もしない。
集計・Embed 組み立ては core/quake_summary.py（詳細な設計方針もそちら）。

【Bot が止まっていた間の分】
投稿時刻にBotが停止していた場合、その回は投稿しない（再開後に遅れて投稿することはしない）。
次回の投稿時刻の集計には、ログが残っている限りその間の地震も含まれる（週次なら7日分）。
"""
import asyncio
import logging
from datetime import datetime, timedelta

from discord.ext import commands

from core.channels import resolve_channels
from core.config import (
    QUAKE_SUMMARY_CHANNEL_IDS, QUAKE_SUMMARY_DAILY_ENABLE, QUAKE_SUMMARY_WEEKLY_ENABLE,
    QUAKE_SUMMARY_TIME, QUAKE_SUMMARY_WEEKLY_WEEKDAY, QUAKE_SUMMARY_TOP_N, QUAKE_SUMMARY_SKIP_EMPTY,
)
from core.discord_send import send_with_retry
from core.quake_history_log import load_quake_history
from core.quake_summary import (
    build_summary_embed, next_run_time, oldest_log_time, parse_summary_time, summarize, summary_period,
)

logger = logging.getLogger("QTLBot")


class QuakeSummaryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._tasks: list[asyncio.Task] = []

    async def cog_load(self):
        if not (QUAKE_SUMMARY_DAILY_ENABLE or QUAKE_SUMMARY_WEEKLY_ENABLE):
            logger.debug("QuakeSummaryCog: 日次・週次とも無効です")
            return
        if QUAKE_SUMMARY_DAILY_ENABLE:
            self._tasks.append(asyncio.create_task(self._worker("daily")))
        if QUAKE_SUMMARY_WEEKLY_ENABLE:
            self._tasks.append(asyncio.create_task(self._worker("weekly")))
        logger.info(
            f"地震サマリーを有効化しました（日次={QUAKE_SUMMARY_DAILY_ENABLE}, 週次={QUAKE_SUMMARY_WEEKLY_ENABLE}, "
            f"時刻={QUAKE_SUMMARY_TIME}）"
        )

    async def cog_unload(self):
        for task in self._tasks:
            if not task.done():
                task.cancel()

    async def _worker(self, kind: str) -> None:
        """kind（"daily" / "weekly"）の投稿時刻まで待って送る、を繰り返す。"""
        await self.bot.wait_until_ready()
        hour, minute = parse_summary_time(QUAKE_SUMMARY_TIME)
        weekday = QUAKE_SUMMARY_WEEKLY_WEEKDAY if kind == "weekly" else None
        last_run: datetime | None = None
        while not self.bot.is_closed():
            try:
                now = datetime.now()
                # 起床がわずかに早くても同じ時刻を二重に処理しないよう、前回の投稿時刻の後から数える
                base = now if last_run is None else max(now, last_run + timedelta(seconds=1))
                run_at = next_run_time(base, hour, minute, weekday)
                logger.debug(f"QuakeSummaryCog({kind}): 次回 {run_at.isoformat()}")
                await asyncio.sleep(max((run_at - now).total_seconds(), 1.0))
                last_run = run_at
                await self._send_summary(kind, run_at)
            except asyncio.CancelledError:
                logger.info(f"QuakeSummaryCog({kind}) が停止しました")
                break
            except Exception as e:
                logger.error(f"QuakeSummaryCog({kind}) エラー: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _send_summary(self, kind: str, run_at: datetime) -> None:
        start, end = summary_period(kind, run_at)
        # ログ全体の走査は重いので、イベントループを止めないようスレッドで行う（1日1回程度）
        records = await asyncio.to_thread(load_quake_history)
        log_oldest = await asyncio.to_thread(oldest_log_time)
        stats = summarize(records, start, end, QUAKE_SUMMARY_TOP_N)

        if kind == "daily" and QUAKE_SUMMARY_SKIP_EMPTY and stats["domestic_total"] == 0:
            logger.debug("QuakeSummaryCog(daily): 国内の地震が0件のため投稿をスキップします")
            return
        channel = resolve_channels(self.bot, QUAKE_SUMMARY_CHANNEL_IDS)
        if not channel:
            logger.warning("QuakeSummaryCog: 送信先チャンネルが見つかりません")
            return

        embed = build_summary_embed(stats, kind, now=datetime.now(), log_oldest=log_oldest)
        await send_with_retry(channel, _label="地震サマリー", embed=embed)
        logger.info(
            f"地震サマリーを送信しました（{kind}, {start:%Y-%m-%d}〜{end:%Y-%m-%d}, 国内{stats['domestic_total']}件）"
        )


async def setup(bot):
    await bot.add_cog(QuakeSummaryCog(bot))
