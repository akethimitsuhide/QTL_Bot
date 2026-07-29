"""
bot.py
======
QTL_Bot の起動専用エントリーポイント。

【Step8 時点の状態: EEW/地震情報Cog分割 + 共通AudioCog導入】
分割済みの Cog は以下の9つ:
  - ApmCog（Mackerel APM連携。デフォルト無効、全Cogより先に登録）
  - AudioCog（音声読み上げ・MP3再生の実体。EewCog/QuakeInfoCogが共有）
  - EewCog（緊急地震速報〈Wolfx/P2P EEW〉専用）
  - QuakeInfoCog（地震情報〈震度速報・各地の震度等〉専用）
  - TsunamiCog（津波・観測・予報・南海トラフ〈tsunami API経由〉）
  - VolcanoCog（火山情報・噴火速報・噴火警報）
  - UsgsCog（USGS海外地震情報）
  - OtherInfoCog（長周期地震動・気象庁その他情報〈quake API経由〉）
  - SystemCog（!status・Web Dashboard・エラー監視・リソース監視）
  - KyoshinMonitorCog（強震モニタ画像の色相解析による揺れ検知。
    グリッド分割による疑似観測点方式。Pillowが必要）

【Step8での変更点（令和8年熊本地震を受けて）】
令和8年熊本地震では、緊急地震速報と地震情報がほぼ同時に、かつ短時間に
大量発生し、旧QuakeEewCogの単一音声キューでは片方の情報を読み上げ中に
もう片方の重要な情報が来ても再生が遅れる問題があった。
そこで旧 QuakeEewCog を EewCog（EEW専用）と QuakeInfoCog（地震情報専用）
に分割し、両者が共有する音声キューの実体を AudioCog に集約した。
AudioCog は EewCog・QuakeInfoCog より前に登録する必要がある
（両Cogの on_ready が AudioCog を bot.get_cog() で参照するため）。

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

            # ── APM (Mackerel連携) Cog ──
            # 他の全Cogより先に登録する（aiohttpの自動計装を確実に効かせるため）。
            # APM_ENABLED=false（デフォルト）の場合は内部で何もしない。
            from cogs.apm import ApmCog
            await bot.add_cog(ApmCog(bot))
            logger.info("ApmCog を登録しました")

            # ── AudioCog（音声読み上げ・MP3再生の実体） ──
            # EewCog・QuakeInfoCog が on_ready / 通知処理内で
            # self.bot.get_cog("AudioCog") を参照するため、必ず両者より先に登録する。
            from cogs.audio_shared import AudioCog
            await bot.add_cog(AudioCog(bot))
            logger.info("AudioCog を登録しました")

            # ── EEW（緊急地震速報）Cog ──
            from cogs.eew import EewCog
            await bot.add_cog(EewCog(bot))
            logger.info("EewCog を登録しました")

            # ── 地震情報 Cog ──
            from cogs.quake import QuakeInfoCog
            await bot.add_cog(QuakeInfoCog(bot))
            logger.info("QuakeInfoCog を登録しました")

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

            # ── Step7: 強震モニタ画像解析による揺れ検知 Cog ──
            # ENABLE_KYOSHIN=false で無効化可能。Pillowが未インストールの場合も
            # on_ready内で検知して安全にスキップする。
            from cogs.kyoshin_monitor import KyoshinMonitorCog
            await bot.add_cog(KyoshinMonitorCog(bot))
            logger.info("KyoshinMonitorCog を登録しました")

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