"""
cogs/eew.py
===========
緊急地震速報（EEW）専用の Cog。

【新設の経緯】
令和8年熊本地震では、緊急地震速報と地震情報（震度速報・各地の震度等）が
ほぼ同時に、かつ短時間に大量発生した。従来は両方が同じ QuakeEewCog・
同じ音声キューを共有していたため、片方の情報を読み上げ中にもう片方の
重要な情報が来ても、キューの末尾に積まれて再生が遅れる問題があった。

このCogは EEW（Wolfx WebSocket / P2P EEW WebSocket）のみを扱い、
地震情報（震度速報・各地の震度・P2P地震情報ポーリング）は
cogs/quake.py の QuakeInfoCog が別Cogとして扱う。
音声再生は cogs/audio_shared.py の AudioCog に委譲し（AudioClientMixin
経由）、両Cogが同じ音声キューを共有することで、優先度に基づいた
一貫した順序で再生されるようにする。

【この Cog が担当する機能】
- Wolfx WebSocket からの EEW（緊急地震速報）受信・通知・読み上げ・MP3再生
- P2P EEW WebSocket（緊急地震速報（警報）専用・常時稼働）
- EEW発生時の強震モニタ監視ループ（振動レベル・画像・MP3アラート）

【他モジュールとの依存関係】
- core.config      : 環境変数由来の設定値（チャンネルID・フィルター等）
- core.constants   : INT_MAP, SHINDO_COLORS, REGION_MAP
- core.helpers     : safe_int, safe_float, safe_bool, truncate_embed_description
- core.audio.AudioClientMixin : speak_local, play_mp3（AudioCog に委譲）
- core.ws_helpers  : ws_connect_loop（WebSocket自動再接続の共通ループ）
- core.fetch_backoff.FetchBackoff : Circuit Breaker（未使用だが将来のfetch系拡張に備え保持）
- core.kyoshin_shared : DualImageFetcher, fetch_vibration_level, shindo_to_color,
                        estimate_max_shindo_from_image（強震モニタ監視ループ用）
"""
import discord
from discord.ext import commands
import aiohttp
import json
import asyncio
import traceback
import time
from datetime import datetime
import logging

from core.config import (
    CHANNEL_ID, EEW_CHANNEL_ID, P2P_EEW_CHANNEL_ID,
    KYOSHIN_CHANNEL_ID, OTHER_CHANNEL_ID,
    WOLFX_WSS,
    ENABLE_KYOSHIN,
    EEW_REGION_COLLAPSE_THRESHOLD,
)
from core.constants import INT_MAP, SHINDO_COLORS, REGION_MAP, collapse_regions_if_needed
from core.helpers import (
    safe_int, safe_float, safe_bool, truncate_embed_description,
)
from core.audio import AudioClientMixin
from core.notification_log import record_notification
from core.delivery_stats import record_delivery
from core.eew_convert import (
    convert_p2p_eew_to_wolfx, extract_alert_regions,
    build_forecast_groups, format_forecast_section, merge_forecast_groups,
)
from core.p2p_image import P2PImageMixin
from core.ws_helpers import ws_connect_loop
from core.kyoshin_shared import (
    DualImageFetcher, fetch_vibration_level, shindo_to_color,
    estimate_max_shindo_from_image,
)

logger = logging.getLogger("QTLBot")


class EewCog(commands.Cog, AudioClientMixin, P2PImageMixin):
    """緊急地震速報（EEW）を扱う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # ── チャンネル（on_ready で解決） ──
        self.channel         = None
        self.eew_channel     = None
        self.p2p_eew_channel = None
        self.kyoshin_channel = None
        self.other_channel   = None

        # ── HTTPセッション（この Cog 専用。強震モニタ画像取得に使用） ──
        self.session: aiohttp.ClientSession | None = None
        self.headers = {"Accept-Encoding": "identity"}

        # ソースごと（wolfx / p2p_eew）に、EventIDごとの最大Serialを
        # 個別に記録する。あくまで「同一ソースからの重複/逆行メッセージ」
        # を弾くための管理であり、ソースをまたいだ重複排除には使わない。
        #
        # 【2026-08-23 修正の経緯】
        # 以前はソースをまたいだ単一の辞書 {event_id: max_serial} を
        # Wolfx・P2P両方のハンドラで共有していた。これは「同じEventIDの
        # EEWを両ソースがほぼ同時に配信してきた場合の二重通知防止」を
        # 意図した設計だったが、実際にはWolfxとP2P地震情報は同一の
        # EventIDに対して独立にSerial番号を採番しており、両者の値は
        # 対応しない（例: 実際の茨城県南部の地震で、Wolfx側がSerial=2に
        # 進んだ直後にP2P側のSerial=1が届いたところ、共有辞書の
        # 「既知の最大Serial=2」より小さいとみなされ「重複/逆行」として
        # 弾かれてしまい、以降そのイベントのP2P EEWが一切通知されなく
        # なるという実害が発生した）。
        #
        # ソースごとに辞書を分離することで、各ソース内部での重複/逆行
        # メッセージの抑制（本来の目的）は維持しつつ、一方のソースの
        # 採番が他方の処理を誤って抑制する問題を解消する。
        self.eew_max_serial_seen: dict[str, dict[str, int]] = {
            "wolfx": {}, "p2p_eew": {},
        }
        self.recent_eews: dict = {}
        self.recent_eews_max_size = 50
        self.last_eew_data = None
        self.last_eew_event_id = None  # !status 等からの参照互換用

        # ── EventID単位の警報対象地域・状態管理 ──
        # 複数のEEWが同時進行しているとき、last_eew_data / last_warn_areas /
        # audio_flags を単一の値で共有すると別のEEW同士で状態が混線してしまう
        # ため、EventIDをキーにした辞書で独立管理する。
        # cumulative_warn_areas: そのEEWで過去に一度でも警報(Type=="警報")対象
        #   になった地域名（REGION_MAP変換後）の累積セット。予想震度の
        #   発表・未発表の入れ替わりで一時的に要素が欠けても、このセットが
        #   縮小することはなく、常に「今までで一番広い範囲」を保持する。
        self.eew_state: dict[str, dict] = {}
        self._eew_state_max_size = 50
        self.monitored_event_id = None
        self.vibration_monitor_task: asyncio.Task | None = None

        # ── 受信統計（!status 用） ──
        self._last_recv: dict[str, datetime | None] = {
            "wolfx":   None,
            "p2p_eew": None,
            # 【2026-08-27 追加】EEW発表時の長周期地震動モニタ
            # （vibration_monitor_loop。jma_s/abrspmx_s画像＋振動レベル）
            # の通知送信回数・時刻を !status / /qtl_status で確認できる
            # ようにするため追加。KyoshinMonitorCog側の常時画像解析検知
            # （"kyoshin"キー）とは独立した別機能のため、キー名も分ける。
            "long_period_monitor": None,
        }
        self._recv_count: dict[str, int] = {k: 0 for k in self._last_recv}

        # ── Wolfx 接続状態管理 ──
        self._wolfx_last_recv: datetime | None = None
        self._wolfx_last_eew_recv: datetime | None = None
        self._wolfx_last_heartbeat: float | None = None
        self._wolfx_ws_alive: bool = False
        self._wolfx_heartbeat_timeout_warned: bool = False

    # ===============================
    # Cog起動・終了
    # ===============================

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
            connector=aiohttp.TCPConnector(limit=50, ttl_dns_cache=300),
        )
        logger.info("EewCog: aiohttp セッションを作成しました")

    async def cog_unload(self):
        for bg_task in (self.vibration_monitor_task,):
            if bg_task and not bg_task.done():
                bg_task.cancel()

        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("EewCog: aiohttp セッションを閉じました")

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel         = self.bot.get_channel(CHANNEL_ID)
        self.eew_channel     = self.bot.get_channel(EEW_CHANNEL_ID) or self.channel
        self.other_channel   = self.bot.get_channel(OTHER_CHANNEL_ID) or self.channel
        self.p2p_eew_channel = self.bot.get_channel(P2P_EEW_CHANNEL_ID) or self.eew_channel
        self.kyoshin_channel = self.bot.get_channel(KYOSHIN_CHANNEL_ID) or self.other_channel

        self.bot.loop.create_task(self.connect_eew_ws())

        # P2P地震情報（code=556, EEW）の受信は core.p2p_ws_hub.P2PWebSocketHub
        # が一元管理する接続から配信される（bot.py 側で hub.register("eew", ...)
        # により self.handle_p2p_eew が登録される）。このCog自身では
        # WebSocket接続を保持しない。

        logger.info("EewCog: on_ready 完了")

    def _reset_wolfx_ws_state(self):
        """Wolfx WebSocket 接続状態をリセット（再接続時に呼び出し）"""
        self._wolfx_last_heartbeat = None
        self._wolfx_last_recv = None
        self._wolfx_last_eew_recv = None
        self._wolfx_ws_alive = False
        self._wolfx_heartbeat_timeout_warned = False

    # ===============================
    # WebSocket（Wolfx EEW）
    # ===============================
    async def connect_eew_ws(self):
        async def _handle(ws):
            self._reset_wolfx_ws_state()

            async for msg in ws:
                try:
                    data = json.loads(msg)
                    msg_type = data.get("type", "").lower()

                    # heartbeat packet: WebSocket alive signal
                    if msg_type == "heartbeat":
                        self._wolfx_last_heartbeat = time.monotonic()
                        self._wolfx_ws_alive = True
                        self._wolfx_heartbeat_timeout_warned = False
                        logger.debug(f"Wolfx heartbeat received (ts={data.get('timestamp')})")
                        continue

                    # EEW packet
                    if msg_type != "jma_eew":
                        continue

                    event_id = data.get("EventID")
                    serial   = int(data.get("Serial", 0))
                    now = datetime.now()
                    self._wolfx_last_recv = now
                    self._wolfx_last_eew_recv = now
                    self._wolfx_ws_alive = True
                    self._wolfx_heartbeat_timeout_warned = False
                    self._last_recv["wolfx"] = self._wolfx_last_recv
                    self._recv_count["wolfx"] += 1
                    logger.info(f"EEW 検知: EventID={event_id} Serial={serial} → 通知")
                    wolfx_seen = self.eew_max_serial_seen["wolfx"]
                    if serial > wolfx_seen.get(event_id, 0):
                        wolfx_seen[event_id] = serial
                        await self.notify_eew(data, source="wolfx")
                    else:
                        logger.debug(
                            f"EEW 重複/逆行のためスキップ: EventID={event_id} "
                            f"Serial={serial} (既知の最大Serial={wolfx_seen.get(event_id, 0)})"
                        )
                except Exception:
                    logger.error(f"EEW 処理エラー:\n{traceback.format_exc()}")

        await ws_connect_loop(
            self.bot.is_closed, WOLFX_WSS, "Wolfx EEW", _handle,
            on_disconnect=self._reset_wolfx_ws_state,
        )

    # ===============================
    # WebSocket（P2P地震情報 code=556, ハブ経由）
    # ===============================
    async def handle_p2p_eew(self, data: dict) -> None:
        """
        core.p2p_ws_hub.P2PWebSocketHub から code=556（緊急地震速報（警報））
        のメッセージを受け取るハンドラ。

        【移行前との違い】
        以前はこのCog自身が P2P_WSS への WebSocket 接続を保持し
        （connect_p2p_eew_ws）、受信ループの中で code フィルタリングも
        行っていた。WebSocket移行により、接続・code フィルタリング・
        重複排除（id/_idベース）は core.p2p_ws_hub.P2PWebSocketHub が
        一元的に行うようになったため、このメソッドは「556のデータを
        受け取った後の処理」のみを担当する。
        """
        try:
            wolfx_data = self._convert_p2p_eew_to_wolfx(data)
            if not wolfx_data:
                return
            event_id = wolfx_data.get("EventID")
            serial   = wolfx_data.get("Serial", 1)
            self._last_recv["p2p_eew"] = datetime.now()
            self._recv_count["p2p_eew"] += 1
            logger.info(f"P2P EEW 検知: EventID={event_id} Serial={serial}")
            p2p_seen = self.eew_max_serial_seen["p2p_eew"]
            if serial > p2p_seen.get(event_id, 0):
                p2p_seen[event_id] = serial
                await self.notify_eew(wolfx_data, source="p2p_eew")
            else:
                logger.debug(
                    f"P2P EEW 重複/逆行のためスキップ: EventID={event_id} "
                    f"Serial={serial} (既知の最大Serial={p2p_seen.get(event_id, 0)})"
                )
        except Exception:
            logger.error(f"P2P EEW メッセージ処理エラー:\n{traceback.format_exc()}")

    def _convert_p2p_eew_to_wolfx(self, p2p_data: dict) -> dict | None:
        """
        P2P地震情報の緊急地震速報（code=556）を Wolfx 形式に変換する。

        実装本体は core.eew_convert.convert_p2p_eew_to_wolfx に切り出した
        （2026-08 リファクタリング。cogs/eew.py の肥大化対策として、
        Cogの状態に依存しない純粋な変換ロジックをモジュール分割した）。
        呼び出し方法・戻り値の仕様は変更していない。フィールド仕様の
        詳細コメントは core/eew_convert.py 側を参照。
        """
        return convert_p2p_eew_to_wolfx(p2p_data)

    # ===============================
    # 警報対象地域の累積管理
    # ===============================
    def _extract_alert_regions(self, data: dict) -> set:
        """
        Wolfx形式のEEWデータから、警報(Type in ("警報","到達済"))対象の
        地域名（REGION_MAP変換後）の集合を抽出する。

        実装本体は core.eew_convert.extract_alert_regions に切り出した
        （2026-08 リファクタリング）。
        """
        return extract_alert_regions(data, REGION_MAP)

    def _update_cumulative_warn_areas(self, event_id: str, data: dict, now: float) -> set:
        """
        EventIDごとに、警報対象地域の「これまでで一番広い範囲」を保持・更新する。

        WarnArea は予想震度の発表・未発表の切り替わりで要素が増減することが
        あり、単純に「今回のdataだけ」を見て通知本文を作ると対象地域が
        減ったように見えてしまう。そこで、一度でも警報対象になった地域は
        そのEEW（EventID）が終息するまで保持し続け、常に累積の和集合を返す。
        """
        state = self.eew_state.setdefault(event_id, {
            "cumulative_warn_areas": set(),
            "last_warn_areas": set(),
            "audio_flags": {"warning": False, "int3": False, "first": False, "final": False, "cancel": False},
            "audio_warn_areas": set(),
            "prev_data": None,
        })
        state["_touched"] = now

        current_regions = self._extract_alert_regions(data)
        state["cumulative_warn_areas"] |= current_regions
        return state["cumulative_warn_areas"]

    async def notify_eew(self, data, is_test=False, channel_override=None,
                         start_monitor=True, source: str = "wolfx"):
        """
        EEW を Discord に通知する。

        Parameters
        ----------
        source : EEW の取得元。チャンネル選択に使用。
            "wolfx"   → eew_channel
            "p2p_eew" → p2p_eew_channel
        channel_override : 明示的にチャンネルを指定する場合（後方互換）
        """
        _source_channel_map = {
            "wolfx":   self.eew_channel,
            "p2p_eew": self.p2p_eew_channel,
        }
        channel = channel_override or _source_channel_map.get(source, self.eew_channel) or self.channel
        if not channel:
            return

        try:
            event_id = data.get("EventID")
            serial = int(data.get("Serial", 1))
            is_final = data.get("isFinal", False)
            is_cancel = data.get("isCancel", False)

            now = datetime.now().timestamp()

            # ===============================
            # LRU キャッシュ管理（メモリリーク対策）
            # ===============================
            self.recent_eews = {
                eid: (d, t) for eid, (d, t) in self.recent_eews.items()
                if now - t < 300
            }

            while len(self.recent_eews) >= self.recent_eews_max_size:
                oldest_eid = min(
                    self.recent_eews.keys(),
                    key=lambda eid: self.recent_eews[eid][1]
                )
                logger.debug(f"recent_eews LRU削除: {oldest_eid} (最大数{self.recent_eews_max_size}に達した)")
                del self.recent_eews[oldest_eid]

            self.recent_eews[event_id] = (data, now)

            # ===============================
            # EventID単位の累積警報対象地域を更新
            # ===============================
            self.eew_state = {
                eid: st for eid, st in self.eew_state.items()
                if eid in self.recent_eews
            }
            while len(self.eew_state) >= self._eew_state_max_size:
                oldest_eid = min(
                    self.eew_state.keys(),
                    key=lambda eid: self.eew_state[eid].get("_touched", 0)
                )
                del self.eew_state[oldest_eid]

            cumulative_warn_areas = self._update_cumulative_warn_areas(event_id, data, now)

            if start_monitor and serial == 1 and self.monitored_event_id is None:
                self.monitored_event_id = event_id
                if self.vibration_monitor_task:
                    self.vibration_monitor_task.cancel()
                self.vibration_monitor_task = asyncio.create_task(self.vibration_monitor_loop(event_id))
                logger.info(f"EEW 第一報検知 → 強震モニタ監視開始 (EventID={event_id})")

            if is_cancel:
                origin_time = data.get("OriginTime", "不明")
                title = "緊急地震速報 (キャンセル報)"
                if is_test:
                    title = "【テスト】" + title

                embed = discord.Embed(title=title, color=0x00FF00, timestamp=datetime.now())
                embed.description = f"**発生時刻： {origin_time}**\n先程の緊急地震速報はキャンセルされました。"
                if is_test:
                    embed.set_footer(text="※これはテスト通知です。")
                await channel.send(embed=embed)
                if not is_test:
                    record_delivery(True, "EEW")
                    record_notification("EEW", title, data.get("Hypocenter", ""))
                    self.monitored_event_id = None
                return

            if len(self.recent_eews) > 1:
                embed_summary = discord.Embed(title="複数の緊急地震速報が発表されています", color=0xFF0000, timestamp=datetime.now())
                summary_desc = ""
                sorted_eews = sorted(self.recent_eews.items(), key=lambda x: x[1][1])
                merged_warn_regions = set()
                any_warn = False
                for eid, (old_data, _) in sorted_eews:
                    title_text = old_data.get('Title', '緊急地震速報')
                    s = int(old_data.get("Serial", 1))
                    f = old_data.get("isFinal", False)
                    serial_text = "最終報" if f else f"第{s}報"
                    hypo = old_data.get("Hypocenter", "不明")
                    max_int = old_data.get("MaxIntensity", "不明")

                    if old_data.get("isAssumption", False):
                        mag = "推定なし"
                    else:
                        mag = old_data.get("Magnitude") or old_data.get("Magunitude") or "不明"

                    depth_text = str(old_data.get("Depth", "不明")).replace("km", "").strip()
                    depth_display = "推定なし" if old_data.get("isAssumption", False) else f"約{depth_text}km"

                    summary_desc += f"**{title_text} ({serial_text})**\n震源地: {hypo} / 予想最大震度: {max_int} / M{mag} / 深さ: {depth_display}\n\n"

                    if old_data.get("isWarn"):
                        any_warn = True
                    eid_state = self.eew_state.get(eid)
                    if eid_state:
                        merged_warn_regions |= eid_state["cumulative_warn_areas"]
                    else:
                        merged_warn_regions |= self._extract_alert_regions(old_data)

                notes = []
                if any_warn:
                    notes.append("**⚠強い揺れに警戒してください。**")
                    if any(
                        safe_bool(d.get("isSea", False)) and safe_float(
                            d.get("Magnitude") or d.get("Magunitude") or 0
                        ) >= 6.8 and safe_int(str(d.get("Depth", "999")).replace("km", "").strip()) <= 151
                        for _, (d, _) in sorted_eews if not d.get("isAssumption", False)
                    ):
                        notes.append("**⚠ 念の為海岸から離れてください**")
                if notes:
                    summary_desc += "\n".join(notes) + "\n"

                if merged_warn_regions:
                    summary_desc += "\n**【強い揺れが予想される地域】**\n"
                    for region in sorted(merged_warn_regions):
                        summary_desc += f"■ {region}　"

                # 【2026-08-27 追加】単一EEW通知と同様、地域ごとの予想震度も
                # 「強い揺れが予想される地域」の下に表示する。複数EEWを
                # 横断してマージし、同じ地域が複数のEEWで異なる予想震度に
                # なっている場合はより大きい方を採用する（merge_forecast_groups参照）。
                warn_areas_list = [
                    (d.get("WarnArea", []), d.get("isAssumption", False))
                    for _, (d, _) in sorted_eews
                ]
                merged_forecast_groups = merge_forecast_groups(warn_areas_list, INT_MAP)
                summary_desc += format_forecast_section(merged_forecast_groups, INT_MAP)

                # Discord API の制限（4096文字）に対応した正確な切り詰め
                # （単一EEW通知のdescriptionと同様。複数EEW×地域ごとの
                # 予想震度は行数が非常に多くなり得るため必須）
                summary_desc = truncate_embed_description(
                    summary_desc.strip(),
                    max_chars=4096,
                    suffix="\n\n（地域が多いため一部省略）"
                )
                embed_summary.description = summary_desc
                await channel.send(embed=embed_summary)

            title_text = data.get('Title', '緊急地震速報')
            serial_text = "最終報" if is_final else f"第{serial}報"
            title = f"{title_text} ({serial_text})"
            if is_test:
                title = "【テスト】" + title

            origin_time = data.get("OriginTime", "不明")
            hypo = data.get("Hypocenter", "不明")
            is_plum = data.get("isAssumption", False)

            if is_plum:
                hypo += " (※PLUM法による予測)"
                mag = "推定なし"
                depth_str = "推定なし"
                depth = -1
            else:
                mag = data.get("Magnitude") or data.get("Magunitude") or "不明"
                depth_str = str(data.get("Depth", "不明")).replace("km", "").strip()
                depth = safe_int(depth_str)

            max_int_str = data.get("MaxIntensity", "不明")
            max_int_val = next((k for k, v in INT_MAP.items() if v == max_int_str), None)
            color = SHINDO_COLORS.get(max_int_val, 0x62626B)

            embed = discord.Embed(title=title, color=color, timestamp=datetime.now())

            if is_plum:
                depth_display = depth_str
            elif depth == 0:
                depth_display = "ごく浅い"
            else:
                depth_display = f"約{depth_str}km"

            description = (
                f"**発生時刻： {origin_time}**\n"
                f"**震源地： {hypo}**\n"
                f"**予想最大震度： {max_int_str}**\n"
                f"**マグニチュード： M{mag}**\n"
                f"**深さ： {depth_display}**"
            )

            notes = []
            if data.get("isWarn"):
                notes.append("**⚠強い揺れに警戒してください。**")

            if not is_plum:
                is_sea = safe_bool(data.get("isSea", False))
                if is_sea and safe_float(mag) >= 6.8 and safe_int(depth) <= 151:
                    notes.append("**⚠念の為海岸から離れてください。**")

                if safe_int(depth) >= 151:
                    notes.append("**⚠震源が深いため、遠方でも揺れる可能性があります。**")

            if notes:
                description += "\n\n" + "\n".join(notes)

            warn_areas = data.get("WarnArea", [])
            if cumulative_warn_areas:
                display_regions = collapse_regions_if_needed(
                    cumulative_warn_areas, EEW_REGION_COLLAPSE_THRESHOLD
                )
                description += "\n\n**【強い揺れが予想される地域】**\n"
                for region in display_regions:
                    description += f"■ {region}　"

            if warn_areas:
                is_assumption = data.get("isAssumption", False)
                forecast_groups = build_forecast_groups(warn_areas, INT_MAP, is_assumption=is_assumption)
                description += format_forecast_section(forecast_groups, INT_MAP)

            description = truncate_embed_description(
                description,
                max_chars=4096,
                suffix="\n\n（地域が多いため一部省略）"
            )

            embed.description = description
            if is_test:
                embed.set_footer(text="※これはテスト通知です。")

            try:
                sent_msg = await channel.send(embed=embed)
            except Exception as e:
                record_delivery(False, "EEW", str(e))
                raise
            if not is_test:
                record_delivery(True, "EEW")
                record_notification("EEW", title, hypo if isinstance(hypo, str) else "")

            # ── P2P地震情報の地図画像添付（2026-08-19追加） ──
            # Wolfx由来のEEW（source="wolfx"、主系統）には地図画像のIDが
            # 存在しないため対象外。P2P由来（source="p2p_eew"、フォール
            # バック系統）の場合のみ、地震情報通知（cogs/quake.py）と
            # 同じ生成方法（cdn.p2pquake.net/app/images/{id}_trim_big.png）
            # で地図画像を取得し、Embed最下部（set_image）に追加する。
            # CDN側の生成遅延を考慮したリトライ処理は
            # core.p2p_image.P2PImageMixin._attach_p2p_image に委譲する
            # （地震情報通知と全く同じ仕組みを流用）。
            if not is_test and not is_cancel and source == "p2p_eew":
                p2p_image_id = data.get("_p2p_image_id")
                if p2p_image_id:
                    self.bot.loop.create_task(
                        self._attach_p2p_image(sent_msg, p2p_image_id)
                    )

            if (is_final or is_cancel) and event_id == self.monitored_event_id:
                self.monitored_event_id = None
                if self.vibration_monitor_task:
                    self.vibration_monitor_task.cancel()
            # generate_and_speak_eew と play_eew_sound はどちらも
            # self.eew_state[event_id] の同じフィールド（last_warn_areas /
            # prev_data）を参照・更新する。以前は generate_and_speak_eew を
            # asyncio.create_task で非同期実行していたため、play_eew_sound
            # が先に完了して state を書き換えてしまい、generate_and_speak_eew
            # 側の「前回との比較」が常に「変化なし」に潰れてしまう競合状態
            # (race condition) があった（例: 警報対象地域が拡大しても
            # 「緊急地震速報。○○では強い揺れに警戒してください」が
            # 一切読み上げられず、high_alert.mp3 だけが鳴る）。
            # 読み上げ判定（何をどう読むか）を先に確定・実行し、
            # その後に効果音を鳴らす順序に統一することで、
            # 両者が同じ state を同時に触らないようにする。
            await self.generate_and_speak_eew(data, cumulative_warn_areas)
            await self.play_eew_sound(data, cumulative_warn_areas)

        except Exception as e:
            record_delivery(False, "EEW", str(e))
            logger.error(f"notify_eew エラー:\n{traceback.format_exc()}")

    # ===============================
    # 読み上げエンジン
    # ===============================
    async def generate_and_speak_eew(self, data, cumulative_warn_areas: set | None = None):
        """QuakeTsunami_antei.html の generateAndPlaySpeech をPythonで再現"""
        event_id = data.get("EventID")
        serial = int(data.get("Serial", 1))
        is_warn = data.get("isWarn", False)
        is_plum = data.get("isAssumption", False)
        hypo = data.get("Hypocenter", "不明")
        max_int_str = data.get("MaxIntensity", "")

        state = self.eew_state.get(event_id)
        prev_data = state.get("prev_data") if state else None

        current_warn_areas = cumulative_warn_areas
        if current_warn_areas is None:
            current_warn_areas = self._update_cumulative_warn_areas(event_id, data, datetime.now().timestamp())

        prev_warn_areas = state.get("last_warn_areas", set()) if state else set()

        area_increased = bool(current_warn_areas - prev_warn_areas)
        warn_area_display = collapse_regions_if_needed(
            current_warn_areas, EEW_REGION_COLLAPSE_THRESHOLD
        )
        warn_area_text = "、".join(warn_area_display) + "では" if warn_area_display else ""

        text = ""
        priority = 3

        if serial == 1 or prev_data is None:
            if is_warn:
                priority = 1
                text = f"緊急地震速報。{warn_area_text}強い揺れに警戒してください。{hypo}で地震。推定最大震度{max_int_str}。"
            else:
                priority = 3
                text = f"{hypo}で地震。推定最大震度{max_int_str}。"
                if is_plum:
                    text += "（PLUM法）"

        else:
            prev_max_int = prev_data.get("MaxIntensity", "")
            prev_hypo = prev_data.get("Hypocenter", "")
            intensity_changed = self._is_intensity_changed_significantly(prev_max_int, max_int_str)

            if is_warn and area_increased:
                priority = 1
                text = f"緊急地震速報。{warn_area_text}強い揺れに警戒してください。"

            elif intensity_changed:
                priority = 2
                text = f"推定最大震度{max_int_str}"

            elif hypo != prev_hypo:
                priority = 3
                text = f"{hypo}で地震。推定最大震度{max_int_str}。"

        if text:
            await self.speak_local(text, priority)

        if state is not None:
            state["last_warn_areas"] = set(current_warn_areas)
            state["prev_data"] = data.copy()
        self.last_eew_data = data.copy()

    def _is_intensity_changed_significantly(self, prev: str, current: str) -> bool:
        order = ["不明", "1", "2", "3", "4", "5弱", "推定5弱以上", "5強", "6弱", "6強", "7", "7以上"]
        try:
            idx_prev = order.index(prev)
            idx_cur = order.index(current)
            return abs(idx_cur - idx_prev) >= 1
        except ValueError:
            return True

    # ===============================
    # EEW 音声再生ロジック
    # ===============================
    async def play_eew_sound(self, data, cumulative_warn_areas: set | None = None):
        """
        EEW 用の効果音（MP3）を鳴らすかどうかだけを判定する。

        読み上げ（generate_and_speak_eew）が使う state["last_warn_areas"] /
        state["prev_data"] は一切更新しない。これらは「次に読み上げる時に
        何と比較するか」を決める読み上げ専用の状態であり、効果音の再生判定
        （state["audio_flags"] と state["audio_warn_areas"]）とは独立させる
        ことで、どちらの処理が先に完了しても互いの判定に影響しないようにする。
        （両者が同じフィールドを共有していたことで、地域拡大時に
        「緊急地震速報。○○では強い揺れに警戒してください」が読み上げられず
        high_alert.mp3 だけが鳴る不具合があった。）
        """
        is_cancel = data.get("isCancel", False)
        serial = int(data.get("Serial", 1))
        is_final = data.get("isFinal", False)
        is_warn = data.get("isWarn", False)
        event_id = data.get("EventID")
        max_int_str = data.get("MaxIntensity", "不明")

        state = self.eew_state.setdefault(event_id, {
            "cumulative_warn_areas": set(),
            "last_warn_areas": set(),
            "audio_flags": {"warning": False, "int3": False, "first": False, "final": False, "cancel": False},
            "audio_warn_areas": set(),
            "prev_data": None,
        })
        state.setdefault("audio_warn_areas", set())
        audio_flags = state["audio_flags"]

        self.last_eew_event_id = event_id

        if is_cancel:
            if not audio_flags.get("cancel"):
                await self.play_mp3("eewC")
                audio_flags["cancel"] = True
                logger.debug("音声: eewC (キャンセル)")
            return

        current_warn_areas = cumulative_warn_areas
        if current_warn_areas is None:
            current_warn_areas = self._update_cumulative_warn_areas(event_id, data, datetime.now().timestamp())

        should_play_high_alert = False
        if is_warn:
            if not audio_flags.get("warning"):
                should_play_high_alert = True
                audio_flags["warning"] = True
            else:
                new_areas = current_warn_areas - state["audio_warn_areas"]
                if new_areas:
                    should_play_high_alert = True
                    logger.info(f"警報地域が追加されました: {new_areas}")

        if should_play_high_alert:
            await self.play_mp3("high_alert")
            state["audio_warn_areas"] = set(current_warn_areas)
            logger.debug(f"音声: high_alert (地域数: {len(current_warn_areas)})")
            return

        int3_or_higher = ["3", "4", "5弱", "5強", "6弱", "6強", "7", "推定5弱以上"]
        if not is_warn and max_int_str in int3_or_higher:
            if not audio_flags.get("int3"):
                await self.play_mp3("eew3")
                audio_flags["int3"] = True
                logger.debug("音声: eew3 (震度3以上)")
                return

        if is_final and not audio_flags.get("final"):
            await self.play_mp3("saisyu")
            audio_flags["final"] = True
            logger.debug("音声: saisyu (最終報)")

        elif serial > 1:
            await self.play_mp3("koushin")
            logger.debug("音声: koushin (更新)")

        elif serial == 1 and not audio_flags.get("first"):
            await self.play_mp3("low_alert")
            audio_flags["first"] = True
            logger.debug("音声: low_alert (初報)")

        state["prev_data"] = data.copy()
        self.last_eew_data = data.copy()

    # ===============================
    # 強震モニタ監視ループ（振動レベル + 強震モニタ画像 + 長周期地震動モニタ画像）
    # ===============================
    async def vibration_monitor_loop(self, target_event_id):
        """
        EEW発生時（第一報検知時）から一定時間、jma_s系統・abrspmx_s(LMoni)系統の
        両画像と振動レベルを2秒間隔でDiscordに通知し続けるループ。

        通知の色は jma_s画像から推定した実震度に基づき、
        core.kyoshin_shared.JMA_S_SHINDO_COLORS の独自カラーマップで決定する
        （kyoshin_monitor.py側の画像解析検知通知と共通のロジック・カラーマップを使用）。
        """
        POLL_INTERVAL_SEC = 2

        image_fetcher = DualImageFetcher()

        _prev_vib_tier: int = 0

        start_time = datetime.now().timestamp()
        if not ENABLE_KYOSHIN:
            return
        channel = self.kyoshin_channel or self.channel

        try:
            while (self.monitored_event_id == target_event_id and
                   not self.bot.is_closed() and
                   datetime.now().timestamp() - start_time < 300):

                level = await fetch_vibration_level(self.session)
                jma_s_url, lmoni_url = await image_fetcher.fetch_urls(self.session)

                if level is not None:
                    if level >= 2000:
                        cur_tier = 3
                    elif level >= 1000:
                        cur_tier = 2
                    elif level >= 100:
                        cur_tier = 1
                    else:
                        cur_tier = 0
                    if cur_tier != _prev_vib_tier:
                        logger.info(f"振動レベル tier 変化: {_prev_vib_tier} → {cur_tier} (level={level})")
                        _prev_vib_tier = cur_tier
                    mp3_key = {3: "lv2000", 2: "lv1000", 1: "lv100"}.get(cur_tier)
                    if mp3_key:
                        await self.play_mp3(mp3_key)
                        logger.debug(f"振動レベル MP3 再生: {mp3_key} (level={level})")

                if level is not None or jma_s_url or lmoni_url:
                    max_shindo = None
                    if jma_s_url:
                        jma_s_bytes = await image_fetcher.fetch_jma_s_bytes(self.session)
                        if jma_s_bytes:
                            # 【2026-08-29 修正】estimate_max_shindo_from_image は
                            # KyoshinImageAnalyzer による全画素の色相解析（Pure Pythonの
                            # ピクセルループ）を内部で行うCPUバウンド処理であり、これを
                            # イベントループ上で同期呼び出ししていた。EEW発表中は本ループが
                            # 2秒間隔・最大5分間動き続けるため、その間Discordのハートビート
                            # 送信・音声キューの消費・他Cogの非同期処理までブロックしうる。
                            # よりによって「EEW発表中」という最も通知の即時性が求められる
                            # 場面でイベントループが詰まるのは本末転倒なため、
                            # cogs/kyoshin_monitor.py._register_stations と同様に
                            # run_in_executor でワーカースレッドへオフロードする。
                            loop = asyncio.get_running_loop()
                            max_shindo = await loop.run_in_executor(
                                None, estimate_max_shindo_from_image, jma_s_bytes
                            )
                    color = shindo_to_color(max_shindo)

                    if level is not None:
                        level_str = f"**振動レベル: {level}**\n"
                    else:
                        level_str = "**振動レベル: 取得中...**\n"

                    description = (
                        level_str
                        + "\n※気象庁からの情報ではありません。あくまで参考値としてお使いください。"
                    )
                    embed = discord.Embed(
                        title="強震モニタ",
                        description=description,
                        color=color,
                        timestamp=datetime.now()
                    )

                    if jma_s_url:
                        embed.set_image(url=jma_s_url)
                    if lmoni_url:
                        embed.set_thumbnail(url=lmoni_url)

                    await channel.send(embed=embed)
                    self._last_recv["long_period_monitor"] = datetime.now()
                    self._recv_count["long_period_monitor"] += 1

                await asyncio.sleep(POLL_INTERVAL_SEC)

        except asyncio.CancelledError:
            logger.info(f"強震モニタ監視終了 (EventID={target_event_id})")
        except Exception as e:
            logger.error(f"強震モニタ監視ループ エラー: {e}")
        finally:
            if self.monitored_event_id == target_event_id:
                self.monitored_event_id = None
