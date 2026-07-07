"""
cogs/apm.py
===========
Mackerel APM（トレーシング）連携のライフサイクル管理を行う Cog。

【この Cog が担当する機能】
- Bot起動時、core.apm.setup_apm() を呼び出して OpenTelemetry SDK を初期化する
  （APM_ENABLED=false の場合は何もしない）
- Bot終了時、core.apm.shutdown_apm() を呼び出して未送信スパンをフラッシュする

【他モジュールとの依存関係】
- core.apm : setup_apm, shutdown_apm, is_apm_active
- core.config : APM_ENABLED（ログ表示用）

【重要: この Cog は bot.py で他の全Cogより先に登録すること】
aiohttp クライアントの自動計装（AioHttpClientInstrumentor）は
aiohttp.ClientSession のメソッドをパッチする方式のため、他のCogが
cog_load() で自分の aiohttp.ClientSession を生成する前に本Cogの
cog_load() が完了している方が確実にトレースを取得できる。
discord.py の bot.add_cog() は逐次 await されるため、bot.py で
このCogを最初に add_cog すれば、後続の全Cogの HTTPリクエストが
計装対象になる。
"""
import logging

import discord
from discord.ext import commands

from core.config import APM_ENABLED
from core.apm import setup_apm, shutdown_apm, is_apm_active

logger = logging.getLogger("QTLBot")


class ApmCog(commands.Cog):
    """Mackerel APM連携の起動・終了処理のみを担当する軽量 Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._enabled_at_startup = False

    async def cog_load(self):
        self._enabled_at_startup = setup_apm()

    async def cog_unload(self):
        shutdown_apm()

    @commands.Cog.listener()
    async def on_ready(self):
        if APM_ENABLED and self._enabled_at_startup:
            logger.info("ApmCog: APM連携が有効な状態でBotが起動しました")
        elif APM_ENABLED and not self._enabled_at_startup:
            logger.warning(
                "ApmCog: APM_ENABLED=true ですが初期化に失敗しています。"
                "ログ上部の APM: から始まるメッセージを確認してください。"
            )
        else:
            logger.debug("ApmCog: APM連携は無効です（APM_ENABLED=false）")

    def apm_status_summary(self) -> str:
        """!status / /qtl_status から呼べる簡易ステータス文字列。"""
        if not APM_ENABLED:
            return "[ - ] 無効（APM_ENABLED=false）"
        return "[OK] 有効" if is_apm_active() else "[NG] 有効設定だが初期化失敗"