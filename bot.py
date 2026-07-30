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

【CLIテスト実行モード】
自動テストが存在しなかった問題への対応として、以下のようにサンプルJSON
データを使って特定Cogの通知ロジックだけを動かす簡易テスト実行に対応した：

    python3 bot.py --test_eew tests/fixtures/eew_sample.json
    python3 bot.py --test_quake tests/fixtures/quake_sample.json
    python3 bot.py --test_tsunami tests/fixtures/tsunami_sample.json
    （その他の対応Cogは core/test_runner.py の TEST_TARGETS を参照）

実際にDiscordへ接続し、対象Cogの notify_* 関数を is_test=True で呼び出す
（＝実チャンネルに「【テスト】」接頭辞付きの通知が実際に送信され、
目視・耳で確認できる）。テスト完了後は自動的にプロセスを終了する。

【起動手順】
    python bot.py
"""
import asyncio
import logging
import sys
import traceback

import discord
from discord.ext import commands

from core.config import BOT_TOKEN
from core.logging_setup import setup_logging
from core.test_runner import parse_test_args, run_cli_test

logger = logging.getLogger("QTLBot")

# コマンドライン引数の解析（通常起動時は None）。
# main() の外（モジュールロード時）で行うことで、テストモードかどうかを
# Cog登録処理に反映できるようにする。
_test_target = parse_test_args(sys.argv[1:])

# ===============================
# Discord Bot 初期化
# ===============================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

if _test_target is not None:
    @bot.event
    async def on_ready():
        """
        CLIテストモード専用の on_ready フック。
        全Cogの登録・各Cog自身の on_ready 処理が完了した後に発火するため、
        ここでテスト対象Cogの notify_* を安全に呼び出せる。
        """
        cog_key, json_path = _test_target
        # 各Cogのon_ready（チャンネル解決等）が先に完了するよう少し待つ。
        # discord.pyのon_readyは全Cogのon_readyと並行して発火しうるため、
        # 確実性を優先して固定の待機時間を設ける。
        await asyncio.sleep(2)
        await run_cli_test(bot, cog_key, json_path)


async def main():
    setup_logging()

    if _test_target is not None:
        cog_key, json_path = _test_target
        logger.warning(
            f"★★★ CLIテストモードで起動します ★★★ "
            f"対象: --test_{cog_key} {json_path}"
        )
        print(f"[TEST] CLIテストモードで起動します（対象: --test_{cog_key} {json_path}）")
        print("[TEST] 全Cogは通常通り起動します（実際の通知経路の検証のため）。")
        print("[TEST] on_ready後に指定したテストを1回実行し、完了後にプロセスを終了します")

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
        finally:
            # TTS_ENGINE=scratchtts 使用時に生成されるモジュールレベルの
            # ClientSession シングルトンを、Bot終了時に確実に閉じる。
            # 未使用（AquesTalkPiのみ利用）の場合はセッションが未生成のため
            # close_scratchtts_session() は何もせず即座に返る。
            from core.tts_engines import close_scratchtts_session
            await close_scratchtts_session()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("キーボード割り込みで終了します")
    except Exception as e:
        logger.error(f"予期しないエラーで終了: {e}\n{traceback.format_exc()}")
    finally:
        logger.info("Bot シャットダウン完了")
