"""
bot.py
======
QTL_Bot の起動専用エントリーポイント。

【Step1 時点の状態】
現時点で分割済みの Cog は QuakeEewCog（地震・EEW）のみ。
津波・火山・USGS・Web Dashboard・エラー監視・!status コマンドなどは
まだ旧 bot.py（分割前の単一ファイル版）に残っている。

★★★ 重要: Step1 は「並行稼働させながらの段階的移行」ではない ★★★
このファイルは新しいマルチCog構成の「型」を示すためのものであり、
実際に本番運用へ投入する場合は、旧 bot.py のうち
QuakeEewCog に移した機能（EEW・地震情報通知）をコメントアウトまたは
削除してから、この新構成と役割を入れ替える必要がある。
（両方を同時に動かすと、地震情報が二重通知される）

Step2 以降で tsunami / volcano / usgs / system の各Cogを
順次切り出し、最終的に旧 bot.py は削除する。

【起動手順】
    python bot.py
"""
import asyncio
import logging
import traceback

import discord
from discord.ext import commands

from core.config import BOT_TOKEN
from core.logging_setup import setup_logging

logger = logging.getLogger("QTLBot")

# ===============================
# Discord Bot 初期化
# ===============================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def main():
    setup_logging()

    async with bot:
        try:
            logger.info("Cog 初期化開始...")

            # ── Step1: 地震・EEW Cog のみ登録 ──
            from cogs.quake import QuakeEewCog
            await bot.add_cog(QuakeEewCog(bot))
            logger.info("QuakeEewCog を登録しました")

            # ── Step2以降でここに追加していく ──
            # from cogs.tsunami import TsunamiCog
            # await bot.add_cog(TsunamiCog(bot))
            #
            # from cogs.volcano import VolcanoCog
            # await bot.add_cog(VolcanoCog(bot))
            #
            # from cogs.usgs import UsgsCog
            # await bot.add_cog(UsgsCog(bot))
            #
            # from cogs.system import SystemCog
            # await bot.add_cog(SystemCog(bot))

            logger.info("bot.start() 実行中...")
            await bot.start(BOT_TOKEN)
        except Exception as e:
            logger.error(f"Bot 起動エラー（詳細）: {type(e).__name__}: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("キーボード割り込みで終了します")
    except Exception as e:
        logger.error(f"予期しないエラーで終了: {e}\n{traceback.format_exc()}")
    finally:
        logger.info("Bot シャットダウン完了")
