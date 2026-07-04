"""
bot.py
======
QTL_Bot の起動専用エントリーポイント。

【Step6 時点の状態: 全機能の切り出しが完了】
分割済みの Cog は以下の6つ:
  - QuakeEewCog（地震・EEW）
  - TsunamiCog（津波・観測・予報・南海トラフ〈tsunami API経由〉）
  - VolcanoCog（火山情報・噴火速報・噴火警報）
  - UsgsCog（USGS海外地震情報）
  - OtherInfoCog（長周期地震動・気象庁その他情報〈quake API経由〉）
  - SystemCog（!status・Web Dashboard・エラー監視・リソース監視）

これで旧 bot.py（分割前の単一ファイル版）の全機能が新構成へ移行完了した。

★★★ 重要: 本番投入前に必ず旧 bot.py と並行稼働させないこと ★★★
旧 bot.py をまだ動かしている環境がある場合、必ず停止してから
この新構成に切り替えること。両方を同時に動かすと全ての情報が
二重通知される。

【既知の設計事項】
南海トラフ地震臨時情報・顕著な地震の震源要素更新のお知らせは、
tsunami API経由（TsunamiCog）と quake API経由（OtherInfoCog）の
2つの独立した経路で検知される。これは元のbot.py（分割前）から
存在した設計であり、Cog分割による新規バグではない
（詳細は cogs/other.py の docstring 参照）。

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

            # ── Step1: 地震・EEW Cog ──
            from cogs.quake import QuakeEewCog
            await bot.add_cog(QuakeEewCog(bot))
            logger.info("QuakeEewCog を登録しました")

            # ── Step2: 津波 Cog ──
            from cogs.tsunami import TsunamiCog
            await bot.add_cog(TsunamiCog(bot))
            logger.info("TsunamiCog を登録しました")

            # ── Step3: 火山 Cog ──
            from cogs.volcano import VolcanoCog
            await bot.add_cog(VolcanoCog(bot))
            logger.info("VolcanoCog を登録しました")

            # ── Step4: USGS Cog ──
            from cogs.usgs import UsgsCog
            await bot.add_cog(UsgsCog(bot))
            logger.info("UsgsCog を登録しました")

            # ── Step6: 長周期地震動・気象庁その他情報 Cog ──
            from cogs.other import OtherInfoCog
            await bot.add_cog(OtherInfoCog(bot))
            logger.info("OtherInfoCog を登録しました")

            # ── Step5: System Cog（!status・Web Dashboard・エラー監視） ──
            from cogs.system import SystemCog
            await bot.add_cog(SystemCog(bot))
            logger.info("SystemCog を登録しました")

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