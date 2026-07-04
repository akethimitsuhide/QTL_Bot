"""
cogs/system.py
===============
Bot全体の稼働状況を横断的に集約する Cog。

【この Cog が担当する機能】
- !status / /qtl_status コマンド（Bot稼働状態のEmbed表示）
- Web Dashboard（GET /status, /health, /health/full）
- エラー自動通知（管理者チャンネルへの通知・日次サマリー）
- リソース監視（CPU/メモリ/ディスク使用率の定期ログ）

【他モジュールとの依存関係】
- core.config      : 各種設定値（ADMIN_CHANNEL_ID, WEB_DASHBOARD_PORT 等）
- core.constants   : INT_MAP
- core.cog_utils   : get_cog_attr（他Cogの状態を安全に取得する）

【Step5 時点の設計: なぜ他Cogの内部状態を直接参照するのか】
分割前は単一クラスの self._last_recv 等を直接読むだけで済んでいたが、
Cog分割後は QuakeEewCog・TsunamiCog・VolcanoCog・UsgsCog がそれぞれ
自分自身の _last_recv / _recv_count / 各種タスクハンドルを保持している。

SystemCog はこれらを `core.cog_utils.get_cog_attr(self.bot, "QuakeEewCog", "_last_recv")`
のような形で「Cog名を指定して安全に読みにいく」。
これは理想的には「各Cogが自分の状態をイベントやプロパティとして公開する」
設計の方が疎結合だが、Step5時点では既存コードの構造をなるべく壊さず移行する
ことを優先し、直接属性アクセス方式を採用している。
Cog名は固定文字列 "QuakeEewCog" 等を使うため、Cogクラス名を変更する際は
この Cog 内の参照も合わせて更新すること（Step6以降でのリファクタリング候補）。
"""
import os
import time
import socket
import logging
import asyncio
import traceback
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
import aiohttp

from core.config import (
    CHANNEL_ID, ADMIN_CHANNEL_ID,
    WOLFX_HEARTBEAT_TIMEOUT,
    USGS_ENABLED, USGS_MAGNITUDE_MIN, USGS_FETCH_INTERVAL,
    USGS_REGION_LAT_MIN, USGS_REGION_LAT_MAX,
    USGS_REGION_LON_MIN, USGS_REGION_LON_MAX, USGS_NOTIFICATION_COOLDOWN,
    QUAKE_MIN_SCALE, QUAKE_MIN_MAG, QUAKE_MIN_DEPTH, QUAKE_MAX_DEPTH,
    STATUS_SHOW_CPU, STATUS_SHOW_MEM, STATUS_SHOW_DISK, STATUS_SHOW_UPTIME,
    RESOURCE_MONITORING_ENABLED, RESOURCE_CHECK_INTERVAL,
    DISK_WARNING_THRESHOLD, DISK_ERROR_THRESHOLD,
    HEALTH_CHECK_TIMEOUT, HEALTH_CHECK_CACHE_TTL, ERROR_NOTIFICATION_TTL,
)
from core.constants import INT_MAP
from core.cog_utils import get_cog_attr

logger = logging.getLogger("QTLBot")

WEB_DASHBOARD_PORT = int(os.getenv("WEB_DASHBOARD_PORT", "8080"))


class SystemCog(commands.Cog):
    """Bot全体の稼働状況集約・エラー監視・Web Dashboardを扱う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.channel = None
        self.admin_channel = None

        # -- HTTPセッション（ヘルスチェック用） --
        self.session: aiohttp.ClientSession | None = None

        # -- 起動時刻 --
        self._bot_start_time: datetime = datetime.now()
        self._start_time: float = time.time()

        # -- /status 用の永続 psutil.Process（リクエスト時のみCPU計測） --
        self._status_psutil_proc = None
        try:
            import psutil as _psutil_init
            self._status_psutil_proc = _psutil_init.Process(os.getpid())
            self._status_psutil_proc.cpu_percent(interval=None)  # 基準値をプライミング
        except Exception:
            pass

        # -- ヘルスチェックキャッシュ --
        self.health_check_cache = None
        self.last_health_check_time = None

        # -- エラー監視 --
        self.error_summary_task: asyncio.Task | None = None
        self.error_notification_cache: dict = {}
        self.error_count_today: int = 0
        self.daily_error_summary: dict = {}

        # -- リソース監視 --
        self.resource_monitor_task: asyncio.Task | None = None

        # -- Web Dashboard --
        self._web_app = None
        self._web_runner = None

    # ===============================
    # Cog起動・終了
    # ===============================

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
        )
        logger.info("SystemCog: aiohttp セッションを作成しました")

    async def cog_unload(self):
        if self.error_summary_task and not self.error_summary_task.done():
            self.error_summary_task.cancel()
            logger.info("error_summary_worker タスクをキャンセルしました")

        if self.resource_monitor_task and not self.resource_monitor_task.done():
            self.resource_monitor_task.cancel()
            logger.info("resource_monitor タスクをキャンセルしました")

        if self._web_runner:
            await self._web_runner.cleanup()

        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("SystemCog: aiohttp セッションを閉じました")

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel = self.bot.get_channel(CHANNEL_ID)

        if ADMIN_CHANNEL_ID != 0:
            self.admin_channel = self.bot.get_channel(ADMIN_CHANNEL_ID)
            if self.admin_channel:
                logger.info(f"管理者チャンネルを設定しました（ID: {ADMIN_CHANNEL_ID}）")
            else:
                logger.warning(f"管理者チャンネルが見つかりません（ID: {ADMIN_CHANNEL_ID}）")

        if not self.error_summary_task:
            self.error_summary_task = self.bot.loop.create_task(self.error_summary_worker())
            logger.info("日次エラーサマリータスクを開始しました")

        if not self.resource_monitor_task:
            self.resource_monitor_task = self.bot.loop.create_task(self.resource_monitor())
            logger.info("リソース監視タスクを開始しました")

        if os.getenv("WEB_DASHBOARD_ENABLED", "true").lower() == "true":
            self.bot.loop.create_task(self.start_web_dashboard())

        # スラッシュコマンドを同期
        # 複数Cogに分割された今も、この処理は1箇所（SystemCog）でのみ実行すれば良い
        # （bot.tree はグローバルなコマンドツリーであり、Cog横断で共有される）
        try:
            synced = await self.bot.tree.sync()
            logger.info(f"スラッシュコマンドを同期しました（{len(synced)}件）")
        except Exception as e:
            logger.warning(f"スラッシュコマンド同期失敗: {e}")

        # Bot起動通知（管理者チャンネル宛）
        # 他Cogのon_readyが出揃うのを少し待ってから送る
        self.bot.loop.create_task(self._notify_startup())

        logger.info("SystemCog: on_ready 完了")

    async def _notify_startup(self) -> None:
        """Bot起動完了を管理者チャンネルに通知する。"""
        if not self.admin_channel:
            logger.debug("管理者チャンネル未設定のため起動通知はスキップします")
            return

        await asyncio.sleep(5)  # 他Cogのon_readyが出揃うのを待つ

        try:
            cog_names = list(self.bot.cogs.keys())
            embed = discord.Embed(
                title="QTL_Bot 起動完了",
                description=f"Bot が起動し、稼働を開始しました。",
                color=discord.Color.green(),
                timestamp=datetime.now(),
            )
            embed.add_field(name="ログインユーザー", value=str(self.bot.user), inline=False)
            embed.add_field(name="登録Cog数", value=f"{len(cog_names)}件", inline=True)
            embed.add_field(name="Cog一覧", value=", ".join(cog_names) if cog_names else "なし", inline=False)
            embed.set_footer(text="QTL_Bot システム通知")

            await self.admin_channel.send(embed=embed)
            logger.info("Bot起動通知を管理者チャンネルに送信しました")
        except Exception as e:
            logger.error(f"Bot起動通知の送信に失敗: {e}", exc_info=True)

    # ===============================
    # 他Cogの状態を集約するヘルパー
    # ===============================

    def _quake_attr(self, name, default=None):
        return get_cog_attr(self.bot, "QuakeEewCog", name, default)

    def _tsunami_attr(self, name, default=None):
        return get_cog_attr(self.bot, "TsunamiCog", name, default)

    def _volcano_attr(self, name, default=None):
        return get_cog_attr(self.bot, "VolcanoCog", name, default)

    def _usgs_attr(self, name, default=None):
        return get_cog_attr(self.bot, "UsgsCog", name, default)

    def _other_attr(self, name, default=None):
        return get_cog_attr(self.bot, "OtherInfoCog", name, default)

    def _merged_last_recv(self) -> dict:
        """全Cogの _last_recv を1つの dict にマージして返す。"""
        merged: dict = {}
        for attr_getter in (self._quake_attr, self._tsunami_attr,
                             self._volcano_attr, self._usgs_attr,
                             self._other_attr):
            d = attr_getter("_last_recv", {}) or {}
            merged.update(d)
        return merged

    def _merged_recv_count(self) -> dict:
        merged: dict = {}
        for attr_getter in (self._quake_attr, self._tsunami_attr,
                             self._volcano_attr, self._usgs_attr,
                             self._other_attr):
            d = attr_getter("_recv_count", {}) or {}
            merged.update(d)
        return merged

    # ===============================
    # !status / /qtl_status コマンド
    # ===============================

    def _build_status_embed(self) -> discord.Embed:
        """ステータス Embed を組み立てて返す（!status と /qtl_status 共通）"""
        try:
            import psutil
            proc = psutil.Process()
            cpu  = proc.cpu_percent(interval=0.5)
            mem  = proc.memory_info().rss / 1024 / 1024
            mem_total = psutil.virtual_memory().total / 1024 / 1024
            disk = psutil.disk_usage("/")
            _psutil_ok = True
        except ImportError:
            _psutil_ok = False
            cpu = mem = mem_total = disk = None

        now = datetime.now()
        uptime = now - self._bot_start_time
        h, rem = divmod(int(uptime.total_seconds()), 3600)
        m, s   = divmod(rem, 60)
        uptime_str = f"{uptime.days}日 {h % 24}時間 {m}分 {s}秒"
        ping_ms = round(self.bot.latency * 1000)

        last_recv = self._merged_last_recv()
        recv_count = self._merged_recv_count()

        def api_status(key: str, warn_sec: int = 300, err_sec: int = 600) -> tuple[str, str]:
            t = last_recv.get(key)
            count = recv_count.get(key, 0)
            if t is None:
                return "[ - ]", "未受信"
            diff = int((now - t).total_seconds())
            time_str = t.strftime("%H:%M:%S")
            count_str = f"(計{count}件)"
            if diff < warn_sec:
                icon = "[OK]"
            elif diff < err_sec:
                icon = "[--]"
            else:
                icon = "[NG]"
            if diff < 60:
                ago = f"{diff}秒前"
            elif diff < 3600:
                ago = f"{diff // 60}分{diff % 60}秒前"
            else:
                ago = f"{diff // 3600}時間前"
            return icon, f"{time_str} ({ago}) {count_str}"

        def task_status(task_loop) -> str:
            if task_loop is None:
                return "[ - ] 未起動"
            if task_loop.is_running():
                return "[OK] 稼働中"
            if task_loop.failed():
                return "[NG] エラー停止"
            return "[--] 停止"

        def asyncio_task_status(task) -> str:
            if task is None:
                return "[ - ] 未起動"
            if not task.done():
                return "[OK] 稼働中"
            if task.cancelled():
                return "[--] キャンセル"
            if task.exception() is not None:
                return "[NG] エラー停止"
            return "[ - ] 完了"

        # Wolfx 状態（QuakeEewCog から取得）
        now_mono = time.monotonic()
        wolfx_last_heartbeat = self._quake_attr("_wolfx_last_heartbeat")
        wolfx_last_eew_recv = self._quake_attr("_wolfx_last_eew_recv")
        if wolfx_last_heartbeat is None:
            wolfx_icon, wolfx_detail = "[ - ]", "heartbeat 未受信（起動中）"
        else:
            hb_elapsed = now_mono - wolfx_last_heartbeat
            if hb_elapsed < WOLFX_HEARTBEAT_TIMEOUT:
                wolfx_icon = "[OK]"
                eew_detail = ""
                if wolfx_last_eew_recv is not None:
                    eew_diff = int((now - wolfx_last_eew_recv).total_seconds())
                    eew_detail = f", EEW {eew_diff}秒前"
                wolfx_detail = f"ONLINE ({hb_elapsed:.1f}s{eew_detail})"
            else:
                wolfx_icon = "[NG]"
                wolfx_detail = f"heartbeat TIMEOUT ({hb_elapsed:.1f}s > {WOLFX_HEARTBEAT_TIMEOUT}s)"

        color = 0x00FF00 if ping_ms < 100 else (0xFFFF00 if ping_ms < 300 else 0xFF0000)
        embed = discord.Embed(
            title="QTL_Bot ステータス",
            color=color,
            timestamp=now,
        )

        # -- システム --
        sys_lines = [f"稼働時間: {uptime_str}", f"Ping: {ping_ms}ms"]
        if _psutil_ok:
            if STATUS_SHOW_CPU:
                sys_lines.append(f"CPU: {cpu:.1f}%")
            if STATUS_SHOW_MEM:
                sys_lines.append(f"RAM: {mem:.0f} / {mem_total:.0f} MB ({mem / mem_total * 100:.1f}%)")
            if STATUS_SHOW_DISK:
                sys_lines.append(f"Disk: {disk.percent:.1f}% ({disk.used // 1024**3:.1f}/{disk.total // 1024**3:.1f} GB)")
        embed.add_field(name="システム", value="\n".join(sys_lines), inline=False)

        # -- EEW --
        p2p_eew_t = last_recv.get('p2p_eew')
        if p2p_eew_t is None:
            p2p_status = "[ - ] 未受信"
        else:
            p2p_diff = int((now - p2p_eew_t).total_seconds())
            p2p_status = f"[OK] {p2p_eew_t.strftime('%H:%M:%S')} ({p2p_diff}秒前) (計{recv_count.get('p2p_eew', 0)}件)"
        eew_lines = [
            f"{wolfx_icon} Wolfx: {wolfx_detail}",
            f"P2P EEW (警報専用・常時): {p2p_status}",
        ]
        embed.add_field(name="EEW", value="\n".join(eew_lines), inline=False)

        # -- API 受信状況 --
        api_rows = [
            ("地震情報 (P2P)",   "quake",           120, 600),
            ("津波情報 (P2P)",   "tsunami",          60, 300),
            ("長周期地震動",     "long_period",      120, 600),
            ("津波観測情報",     "tsunami_obs",      120, 600),
            ("気象庁その他",     "quake_advisory",   120, 600),
            ("火山情報",         "volcano",         120, 600),
            ("USGS 地震情報",    "usgs",            600, 1200),
        ]
        api_lines = []
        for label, key, warn, err in api_rows:
            icon, detail = api_status(key, warn, err)
            api_lines.append(f"{icon} **{label}**: {detail}")
        embed.add_field(name="API 受信状況", value="\n".join(api_lines), inline=False)

        # -- タスク稼働状態 --
        task_lines = [
            f"{task_status(self._quake_attr('fetch_quake'))} **fetch_quake**",
            f"{task_status(self._tsunami_attr('fetch_tsunami'))} **fetch_tsunami**",
            f"{task_status(self._tsunami_attr('fetch_tsunami_observation'))} **fetch_tsunami_observation**",
            f"{task_status(self._usgs_attr('fetch_usgs_quake')) if USGS_ENABLED else '[ - ] 無効'} **fetch_usgs_quake**",
            f"{asyncio_task_status(self._quake_attr('speech_task'))} **speech_worker (quake)**",
            f"{asyncio_task_status(self._quake_attr('mp3_task'))} **mp3_worker (quake)**",
            f"{asyncio_task_status(self._volcano_attr('volcano_task'))} **volcano_poller**",
            f"{asyncio_task_status(self._volcano_attr('eruption_task'))} **eruption_poller**",
            f"{asyncio_task_status(self._volcano_attr('warning_task'))} **warning_poller**",
            f"{task_status(self._other_attr('fetch_long_period'))} **fetch_long_period**",
            f"{task_status(self._other_attr('fetch_quake_advisory'))} **fetch_quake_advisory**",
        ]
        embed.add_field(name="タスク稼働状態", value="\n".join(task_lines), inline=False)

        # -- USGS 設定 --
        if USGS_ENABLED:
            usgs_lines = [
                f"対象地域: 緯度 {USGS_REGION_LAT_MIN}〜{USGS_REGION_LAT_MAX} / 経度 {USGS_REGION_LON_MIN}〜{USGS_REGION_LON_MAX}",
                f"M下限: {USGS_MAGNITUDE_MIN} / ポーリング間隔: {USGS_FETCH_INTERVAL}秒 / 重複防止: {USGS_NOTIFICATION_COOLDOWN}秒",
            ]
            embed.add_field(name="USGS 設定", value="\n".join(usgs_lines), inline=False)

        # -- フィルター設定 --
        if STATUS_SHOW_UPTIME:
            filter_lines = [
                f"震度下限: {INT_MAP.get(QUAKE_MIN_SCALE, str(QUAKE_MIN_SCALE))} / M下限: {QUAKE_MIN_MAG} / 深さ: {QUAKE_MIN_DEPTH}〜{QUAKE_MAX_DEPTH}km",
            ]
            embed.add_field(name="フィルター", value="\n".join(filter_lines), inline=False)

        return embed

    @commands.command(name="status")
    @commands.has_permissions(administrator=True)
    async def cmd_status(self, ctx):
        """Bot の稼働状態・各API受信状況・Ping を表示する"""
        embed = self._build_status_embed()
        await ctx.send(embed=embed)

    @cmd_status.error
    async def cmd_status_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("このコマンドはサーバー管理者のみ実行可能です。", delete_after=5)

    @discord.app_commands.command(name="qtl_status", description="QTL_Bot の稼働状態・各API受信状況を表示します（管理者専用）")
    @discord.app_commands.default_permissions(administrator=True)
    async def slash_qtl_status(self, interaction: discord.Interaction):
        """スラッシュコマンド版ステータス表示"""
        await interaction.response.defer(ephemeral=False)
        embed = self._build_status_embed()
        await interaction.followup.send(embed=embed)

    # ===============================
    # Web ダッシュボード
    # ===============================
    async def start_web_dashboard(self):
        """Web ダッシュボード（aiohttp）を起動"""
        from aiohttp import web
        try:
            import psutil
        except ImportError:
            psutil = None

        port = WEB_DASHBOARD_PORT

        async def status_handler(request):
            """GET /status - ステータス JSON を返す（拡充版）"""
            try:
                now = datetime.now()
                uptime_seconds = int(time.time() - self._start_time)
                uptime_str = self._format_uptime(uptime_seconds)

                last_recv = self._merged_last_recv()
                recv_count = self._merged_recv_count()

                # システムリソース（リクエスト時のみ計測）
                system_info: dict = {}
                if psutil:
                    try:
                        if self._status_psutil_proc is not None:
                            proc = self._status_psutil_proc
                            cpu_val = proc.cpu_percent(interval=None)
                        else:
                            proc = psutil.Process(os.getpid())
                            cpu_val = proc.cpu_percent(interval=None)
                        mem = proc.memory_info().rss / 1024 / 1024
                        mem_total = psutil.virtual_memory().total / 1024 / 1024
                        disk = psutil.disk_usage("/")
                        system_info = {
                            "cpu_percent": cpu_val,
                            "memory_mb": round(mem, 1),
                            "memory_total_mb": round(mem_total, 1),
                            "memory_percent": round(mem / mem_total * 100, 1),
                            "disk_percent": disk.percent,
                            "disk_free_gb": round(disk.free / 1024**3, 2),
                        }
                    except Exception:
                        pass

                def _api_info(key: str) -> dict:
                    t = last_recv.get(key)
                    return {
                        "last_recv_time": t.isoformat() if t else None,
                        "recv_count": recv_count.get(key, 0),
                    }

                # EEW 状態（QuakeEewCog から取得）
                now_mono = time.monotonic()
                wolfx_hb = self._quake_attr("_wolfx_last_heartbeat")
                if wolfx_hb is None:
                    wolfx_ws_status = "connecting"
                    wolfx_hb_elapsed = None
                else:
                    wolfx_hb_elapsed = round(now_mono - wolfx_hb, 2)
                    wolfx_ws_status = "online" if wolfx_hb_elapsed < WOLFX_HEARTBEAT_TIMEOUT else "timeout"

                eew_info = {
                    "wolfx": {
                        "ws_status": wolfx_ws_status,
                        "heartbeat_elapsed_sec": wolfx_hb_elapsed,
                        "heartbeat_timeout_sec": WOLFX_HEARTBEAT_TIMEOUT,
                        "last_eew_id": self._quake_attr("last_eew_event_id"),
                        **_api_info("wolfx"),
                    },
                    "p2p_eew": {
                        **_api_info("p2p_eew"),
                    },
                }

                def _loop_status(t) -> str:
                    if t is None: return "not_started"
                    if t.is_running(): return "running"
                    if t.failed(): return "error"
                    return "stopped"

                def _task_status(t) -> str:
                    if t is None: return "not_started"
                    if not t.done(): return "running"
                    if t.cancelled(): return "cancelled"
                    try:
                        t.exception()
                    except Exception:
                        return "error"
                    return "done"

                tasks_info = {
                    "fetch_quake": _loop_status(self._quake_attr("fetch_quake")),
                    "fetch_tsunami": _loop_status(self._tsunami_attr("fetch_tsunami")),
                    "fetch_tsunami_observation": _loop_status(self._tsunami_attr("fetch_tsunami_observation")),
                    "fetch_usgs_quake": _loop_status(self._usgs_attr("fetch_usgs_quake")) if USGS_ENABLED else "disabled",
                    "speech_worker_quake": _task_status(self._quake_attr("speech_task")),
                    "mp3_worker_quake": _task_status(self._quake_attr("mp3_task")),
                    "volcano_poller": _task_status(self._volcano_attr("volcano_task")),
                    "eruption_poller": _task_status(self._volcano_attr("eruption_task")),
                    "warning_poller": _task_status(self._volcano_attr("warning_task")),
                    "fetch_long_period": _loop_status(self._other_attr("fetch_long_period")),
                    "fetch_quake_advisory": _loop_status(self._other_attr("fetch_quake_advisory")),
                }

                usgs_info: dict = {"enabled": USGS_ENABLED}
                if USGS_ENABLED:
                    last_usgs_ids_dict = self._usgs_attr("last_usgs_ids", {}) or {}
                    usgs_last_ids = list(last_usgs_ids_dict.keys())[-5:] if last_usgs_ids_dict else []
                    usgs_info.update({
                        "magnitude_min": USGS_MAGNITUDE_MIN,
                        "fetch_interval_sec": USGS_FETCH_INTERVAL,
                        "region": {
                            "lat": [USGS_REGION_LAT_MIN, USGS_REGION_LAT_MAX],
                            "lon": [USGS_REGION_LON_MIN, USGS_REGION_LON_MAX],
                        },
                        "last_event_ids": usgs_last_ids,
                        **_api_info("usgs"),
                    })

                last_volcano_event_id = self._volcano_attr("_last_volcano_event_id")
                last_volcano_recv_time = self._volcano_attr("_last_volcano_recv_time")
                volcano_recv_count = self._volcano_attr("_volcano_recv_count", 0)
                volcano_task = self._volcano_attr("volcano_task")

                status_data = {
                    "status": "online",
                    "timestamp": now.isoformat(),
                    "bot_user": str(self.bot.user),
                    "uptime": uptime_str,
                    "uptime_seconds": uptime_seconds,
                    "ping_ms": round(self.bot.latency * 1000),
                    "system": system_info,
                    "eew": eew_info,
                    "api_status": {
                        "wolfx": last_recv.get("wolfx").isoformat() if last_recv.get("wolfx") else None,
                        "p2p_eew": last_recv.get("p2p_eew").isoformat() if last_recv.get("p2p_eew") else None,
                        "quake": last_recv.get("quake").isoformat() if last_recv.get("quake") else None,
                        "tsunami": last_recv.get("tsunami").isoformat() if last_recv.get("tsunami") else None,
                        "volcano": last_recv.get("volcano").isoformat() if last_recv.get("volcano") else None,
                    },
                    "recv_count": {
                        "wolfx": recv_count.get("wolfx", 0),
                        "p2p_eew": recv_count.get("p2p_eew", 0),
                        "quake": recv_count.get("quake", 0),
                        "tsunami": recv_count.get("tsunami", 0),
                        "long_period": recv_count.get("long_period", 0),
                        "tsunami_obs": recv_count.get("tsunami_obs", 0),
                        "volcano": recv_count.get("volcano", 0),
                        "usgs": recv_count.get("usgs", 0),
                    },
                    "monitoring": {
                        "quake": _api_info("quake"),
                        "tsunami": _api_info("tsunami"),
                        "long_period": _api_info("long_period"),
                        "tsunami_obs": _api_info("tsunami_obs"),
                        "quake_advisory": _api_info("quake_advisory"),
                        "volcano": {
                            "last_event_id": last_volcano_event_id,
                            "polling_status": _task_status(volcano_task),
                            **_api_info("volcano"),
                            "total_recv_count": volcano_recv_count,
                        },
                        "usgs": usgs_info,
                    },
                    "tasks": tasks_info,
                    # 後方互換フィールド
                    "last_eew": {
                        "event_id": self._quake_attr("last_eew_event_id"),
                        "timestamp": last_recv.get("wolfx").isoformat() if last_recv.get("wolfx") else None,
                    },
                    "volcano_monitoring": {
                        "last_event_id": last_volcano_event_id,
                        "last_recv_time": last_volcano_recv_time.isoformat() if last_volcano_recv_time else None,
                        "polling_status": "active" if volcano_task and not volcano_task.done() else "inactive",
                        "total_recv_count": volcano_recv_count,
                    },
                    "memory_usage_mb": system_info.get("memory_mb", 0),
                }
                return web.json_response(status_data)
            except Exception as e:
                logger.error(f"Web ダッシュボード /status エラー: {e}")
                return web.json_response({"error": str(e)}, status=500)

        async def health_handler(request):
            """GET /health - ヘルスチェック"""
            return web.json_response({"status": "online"})

        async def health_full_handler(request):
            """GET /health/full - 詳細ヘルスチェック（各 API の疎通確認）"""
            try:
                result = await self.check_api_status()
                return web.json_response(result)
            except Exception as e:
                logger.error(f"/health/full エラー: {e}", exc_info=True)
                return web.json_response(
                    {"status": "error", "error": str(e)},
                    status=500
                )

        try:
            self._web_app = web.Application()
            self._web_app.router.add_get("/status", status_handler)
            self._web_app.router.add_get("/health", health_handler)
            self._web_app.router.add_get("/health/full", health_full_handler)

            self._web_runner = web.AppRunner(self._web_app)
            await self._web_runner.setup()
            site = web.TCPSite(self._web_runner, "0.0.0.0", port)
            await site.start()

            logger.info(f"Web ダッシュボード起動: http://localhost:{port}/status")
        except Exception as e:
            logger.error(f"Web ダッシュボード起動失敗: {e}")

    def _format_uptime(self, seconds: int) -> str:
        """秒数を 'Xd XXh XXm' 形式に変換"""
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        return f"{days}d {hours}h {minutes}m"

    # ===============================
    # リソース監視
    # ===============================
    async def resource_monitor(self) -> None:
        """1時間ごとにリソース使用率をログに記録"""
        try:
            import psutil
        except ImportError:
            logger.warning("psutil がインストールされていません。リソース監視は無効です。")
            return

        if not RESOURCE_MONITORING_ENABLED:
            logger.info("リソース監視は無効です。")
            return

        while not self.bot.is_closed():
            try:
                await asyncio.sleep(RESOURCE_CHECK_INTERVAL)

                try:
                    proc = psutil.Process()
                    cpu_percent = proc.cpu_percent(interval=1)
                    mem_info = proc.memory_info()
                    mem_mb = mem_info.rss / 1024 / 1024

                    disk_info = psutil.disk_usage('/')
                    disk_percent = disk_info.percent
                    disk_free_gb = disk_info.free / 1024 / 1024 / 1024

                    log_msg = (
                        f"リソース監視 - CPU: {cpu_percent:.1f}%, "
                        f"MEM: {mem_mb:.1f}MB, "
                        f"DISK: {disk_percent}% (空き容量: {disk_free_gb:.1f}GB)"
                    )

                    if disk_percent >= DISK_ERROR_THRESHOLD:
                        logger.error(f"[ERROR] {log_msg} - ディスク使用率が {DISK_ERROR_THRESHOLD}% を超えています")
                    elif disk_percent >= DISK_WARNING_THRESHOLD:
                        logger.warning(f"[WARN] {log_msg} - ディスク使用率が {DISK_WARNING_THRESHOLD}% を超えています")
                    else:
                        logger.info(f"{log_msg}")

                except Exception as e:
                    logger.error(f"リソース情報取得エラー: {e}")

            except asyncio.CancelledError:
                logger.info("resource_monitor が停止しました")
                break
            except Exception as e:
                logger.error(f"resource_monitor エラー: {e}")
                await asyncio.sleep(60)

    # ===============================
    # ヘルスチェック
    # ===============================
    async def check_api_status(self) -> dict:
        """各 API（Wolfx, JMA, P2P, USGS）の疎通確認"""
        if (self.health_check_cache and
            self.last_health_check_time and
            (datetime.now() - self.last_health_check_time).total_seconds() < HEALTH_CHECK_CACHE_TTL):
            return self.health_check_cache

        result = {
            "overall_status": "healthy",
            "last_check_time": datetime.now().isoformat(),
            "api_status": {
                "wolfx": {"ok": False, "latency_ms": None, "error": None},
                "jma": {"ok": False, "latency_ms": None, "error": None},
                "p2p": {"ok": False, "latency_ms": None, "error": None},
                "usgs": {"ok": False, "latency_ms": None, "error": None},
            }
        }

        try:
            # Wolfx WebSocket ping（TCP 接続確認）
            try:
                start = time.time()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(HEALTH_CHECK_TIMEOUT)
                await asyncio.wait_for(
                    asyncio.to_thread(sock.connect, ('api.wolfx.jp', 443)),
                    timeout=HEALTH_CHECK_TIMEOUT
                )
                sock.close()
                latency = (time.time() - start) * 1000
                result["api_status"]["wolfx"] = {
                    "ok": True, "latency_ms": round(latency, 1), "error": None,
                }
            except Exception as e:
                result["api_status"]["wolfx"]["error"] = str(type(e).__name__)

            # JMA API ping
            try:
                start = time.time()
                async with self.session.get(
                    'https://www.jma.go.jp/bosai/common/const/area.json',
                    timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)
                ) as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        result["api_status"]["jma"] = {
                            "ok": True, "latency_ms": round(latency, 1), "error": None,
                        }
            except Exception as e:
                result["api_status"]["jma"]["error"] = str(type(e).__name__)

            # P2P 地震情報 API ping
            try:
                start = time.time()
                async with self.session.get(
                    'https://api.p2pquake.net/v2/status',
                    timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)
                ) as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        result["api_status"]["p2p"] = {
                            "ok": True, "latency_ms": round(latency, 1), "error": None,
                        }
            except Exception as e:
                result["api_status"]["p2p"]["error"] = str(type(e).__name__)

            # USGS API ping
            try:
                start = time.time()
                async with self.session.get(
                    'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson',
                    timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)
                ) as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        result["api_status"]["usgs"] = {
                            "ok": True, "latency_ms": round(latency, 1), "error": None,
                        }
            except Exception as e:
                result["api_status"]["usgs"]["error"] = str(type(e).__name__)

            all_ok = all(api["ok"] for api in result["api_status"].values())
            result["overall_status"] = "healthy" if all_ok else "degraded"

        except Exception as e:
            logger.error(f"ヘルスチェック中にエラー: {e}", exc_info=True)
            result["overall_status"] = "unhealthy"

        self.health_check_cache = result
        self.last_health_check_time = datetime.now()

        return result

    # ===============================
    # エラー自動通知
    # ===============================
    async def notify_error(self, error_msg: str, error_type: str = "Unknown") -> None:
        """エラーを管理者チャンネルに通知（重複防止付き）。他Cogからも呼べる。"""
        if not self.admin_channel:
            return

        try:
            error_hash = hash(f"{error_type}:{error_msg[:100]}")

            current_time = datetime.now()
            if error_hash in self.error_notification_cache:
                last_notified = self.error_notification_cache[error_hash]
                if (current_time - last_notified).total_seconds() < ERROR_NOTIFICATION_TTL:
                    logger.debug(f"エラー通知をスキップ（重複防止）: {error_type}")
                    return

            self.error_notification_cache[error_hash] = current_time

            self.error_count_today += 1
            if error_type not in self.daily_error_summary:
                self.daily_error_summary[error_type] = 0
            self.daily_error_summary[error_type] += 1

            embed = discord.Embed(
                title="エラー発生",
                description=f"**タイプ**: {error_type}\n**メッセージ**: {error_msg[:500]}",
                color=discord.Color.red(),
                timestamp=current_time
            )
            embed.add_field(name="発生時刻", value=current_time.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
            embed.add_field(name="本日のエラー件数", value=str(self.error_count_today), inline=True)
            embed.add_field(name="エラータイプ別", value=str(self.daily_error_summary), inline=False)
            embed.set_footer(text="QTL_Bot エラー監視")

            await self.admin_channel.send(embed=embed)
            logger.info(f"エラー通知を送信しました: {error_type}")

        except Exception as e:
            logger.error(f"エラー通知の送信に失敗: {e}", exc_info=True)

    async def error_summary_worker(self) -> None:
        """毎日 00:00 に日次エラーサマリーを生成・送信"""
        while not self.bot.is_closed():
            try:
                now = datetime.now()
                tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                wait_seconds = (tomorrow - now).total_seconds()

                logger.debug(f"日次エラーサマリー: {wait_seconds:.0f}秒後に実行")
                await asyncio.sleep(wait_seconds)

                if not self.admin_channel or self.error_count_today == 0:
                    logger.debug("エラーサマリー: エラーがないため送信をスキップ")
                    self.error_count_today = 0
                    self.daily_error_summary = {}
                    continue

                summary_text = "\n".join([
                    f"  • {etype}: {count} 件"
                    for etype, count in sorted(self.daily_error_summary.items(), key=lambda x: x[1], reverse=True)
                ])

                embed = discord.Embed(
                    title="日次エラーサマリー",
                    description=f"**集計日**: {datetime.now().strftime('%Y-%m-%d')}\n**総エラー数**: {self.error_count_today} 件",
                    color=discord.Color.orange(),
                    timestamp=datetime.now()
                )
                embed.add_field(name="エラータイプ別集計", value=summary_text or "なし", inline=False)
                embed.set_footer(text="QTL_Bot エラー監視")

                await self.admin_channel.send(embed=embed)
                logger.info(f"日次エラーサマリーを送信しました（{self.error_count_today}件）")

                self.error_count_today = 0
                self.daily_error_summary = {}

            except asyncio.CancelledError:
                logger.info("error_summary_worker が停止しました")
                break
            except Exception as e:
                logger.error(f"error_summary_worker エラー: {e}", exc_info=True)
                await asyncio.sleep(60)