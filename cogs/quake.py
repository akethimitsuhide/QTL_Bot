"""
cogs/quake.py
=============
地震情報・緊急地震速報（EEW）関連の通知を扱う Cog。

【この Cog が担当する機能】
- Wolfx WebSocket からの EEW（緊急地震速報）受信・通知・読み上げ・MP3再生
- P2P EEW WebSocket（緊急地震速報（警報）専用・常時稼働）
- P2P地震情報 API のポーリング（3秒間隔）・地震情報通知
- EEW発生時の強震モニタ監視ループ（振動レベル・画像・MP3アラート）
- P2P CDN画像のリトライ添付

【他モジュールとの依存関係】
- core.config      : 環境変数由来の設定値（チャンネルID・フィルター等）
- core.constants   : INT_MAP, SHINDO_COLORS, QUAKE_TYPE_MAP, TSUNAMI_MAP, REGION_MAP
- core.helpers     : safe_int, safe_float, safe_bool,
                      truncate_embed_description, format_jma_time
- core.audio.AudioMixin      : speak_local, play_mp3 等（多重継承で利用）
- core.p2p_image.P2PImageMixin : p2p_image_url, _attach_p2p_image（多重継承で利用）

【Step1 時点の設計メモ】
- 本 Cog は独自の aiohttp.ClientSession を保持する（cog_load/cog_unloadで管理）。
  将来的に全Cog共通のHTTPセッションに統合する場合は、
  core/http.py のようなモジュールを新設し、Bot全体で1つのセッションを
  共有する設計に発展させることを検討する（Step3以降の課題）。
- 音声再生系（speech_queue, mp3_queue, audio_files）はこの Cog がオーナーとなる。
  他のCog（tsunami/volcano等）が分割された際は、
  AudioMixin を継承した上でこの Cog の play_mp3 等を
  `self.bot.get_cog("QuakeEewCog").play_mp3(...)` のように呼び出すか、
  音声再生専用の AudioCog に一本化するかを Step2 で判断する。
"""
import discord
from discord.ext import commands, tasks
import aiohttp
import json
import asyncio
import websockets
import traceback
import os
import time
from datetime import datetime, timedelta
from collections import defaultdict
import logging

from core.config import (
    CHANNEL_ID, EEW_CHANNEL_ID, QUAKE_CHANNEL_ID, P2P_EEW_CHANNEL_ID,
    KYOSHIN_CHANNEL_ID, OTHER_CHANNEL_ID,
    WOLFX_WSS, P2P_WSS,
    WOLFX_HEARTBEAT_TIMEOUT,
    FETCH_FAILURE_THRESHOLD, FETCH_BACKOFF_SECONDS,
    EEW_MIN_INTENSITY,
    QUAKE_MIN_SCALE, QUAKE_MIN_MAG, QUAKE_MIN_DEPTH, QUAKE_MAX_DEPTH,
    QUAKE_ENABLE_SCALE_PROMPT, QUAKE_ENABLE_DESTINATION,
    QUAKE_ENABLE_SCALE_AND_DEST, QUAKE_ENABLE_DETAIL_SCALE,
    QUAKE_ENABLE_FOREIGN, QUAKE_ENABLE_OTHER,
    SPEECH_QUEUE_MAXSIZE, MP3_QUEUE_MAXSIZE,
    ENABLE_KYOSHIN,
)
from core.constants import (
    INT_MAP, SHINDO_COLORS, QUAKE_TYPE_MAP, TSUNAMI_MAP, REGION_MAP,
)
from core.helpers import (
    safe_int, safe_float, safe_bool,
    truncate_embed_description, format_jma_time,
)
from core.audio import AudioMixin
from core.p2p_image import P2PImageMixin

logger = logging.getLogger("QTLBot")


class QuakeEewCog(commands.Cog, AudioMixin, P2PImageMixin):
    """地震情報・EEW（緊急地震速報）を扱う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # ── チャンネル（on_ready で解決） ──
        self.channel        = None
        self.eew_channel    = None
        self.quake_channel  = None
        self.p2p_eew_channel = None
        self.kyoshin_channel = None
        self.other_channel  = None

        # ── HTTPセッション（この Cog 専用） ──
        self.session: aiohttp.ClientSession | None = None
        self.headers = {"Accept-Encoding": "identity"}

        # ── 地震情報の重複排除状態 ──
        self.last_quake_id = None
        self.last_eew_event_id = None
        self.last_eew_serial = 0
        self.recent_eews: dict = {}
        self.recent_eews_max_size = 50
        self.last_eew_data = None
        self.last_warn_areas: set = set()
        self.monitored_event_id = None
        self.vibration_monitor_task: asyncio.Task | None = None
        self._last_zencyu_time: datetime | None = None  # zencyu.mp3 最終再生時刻（15分クールダウン）

        # ── 受信統計（!status 用。メインCog側の _last_recv と将来統合予定） ──
        self._last_recv: dict[str, datetime | None] = {
            "wolfx":   None,
            "p2p_eew": None,
            "quake":   None,
        }
        self._recv_count: dict[str, int] = {k: 0 for k in self._last_recv}

        # ── P2P EEW タスク（緊急地震速報（警報）専用・常時稼働） ──
        self.p2p_eew_task: asyncio.Task | None = None

        # ── Wolfx 接続状態管理 ──
        self._wolfx_last_recv: datetime | None = None
        self._wolfx_last_eew_recv: datetime | None = None
        self._wolfx_last_heartbeat: float | None = None
        self._wolfx_ws_alive: bool = False
        self._wolfx_heartbeat_timeout_warned: bool = False

        # ── fetch_quake の Circuit Breaker ──
        self._fetch_failures: dict[str, int] = {"quake": 0}
        self._fetch_backoff_until: dict[str, float] = {"quake": 0.0}
        self._fetch_quake_lock = asyncio.Lock()

        # ── 音声再生（AudioMixin が要求する属性） ──
        self.speech_queue = asyncio.PriorityQueue(maxsize=SPEECH_QUEUE_MAXSIZE)
        self.speech_task: asyncio.Task | None = None
        self.mp3_queue = asyncio.Queue(maxsize=MP3_QUEUE_MAXSIZE)
        self.mp3_task: asyncio.Task | None = None
        self.audio_files = {
            "low_alert": "low_alert.mp3",
            "koushin":   "koushin.mp3",
            "saisyu":    "saisyu.mp3",
            "eew3":      "eew3.mp3",
            "high_alert": "high_alert.mp3",
            "eewC":      "eewC.mp3",
            "vxse51":    "vxse51.mp3",
            "vxse52":    "vxse52.mp3",
            "vxse53":    "vxse53.mp3",
            "vxse5c":    "vxse5c.mp3",
            "zencyu":    "zencyu.mp3",
            "lv100":     "lv100.mp3",
            "lv1000":    "lv1000.mp3",
            "lv2000":    "lv2000.mp3",
        }
        self.audio_flags = {"warning": False, "int3": False, "first": False, "final": False, "cancel": False}

    # ===============================
    # Cog起動・終了
    # ===============================

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
            connector=aiohttp.TCPConnector(limit=50, ttl_dns_cache=300),
        )
        logger.info("QuakeEewCog: aiohttp セッションを作成しました")

    async def cog_unload(self):
        if self.fetch_quake.is_running():
            self.fetch_quake.cancel()

        for bg_task in (self.vibration_monitor_task, self.p2p_eew_task,
                        self.speech_task, self.mp3_task):
            if bg_task and not bg_task.done():
                bg_task.cancel()

        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("QuakeEewCog: aiohttp セッションを閉じました")

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel         = self.bot.get_channel(CHANNEL_ID)
        self.eew_channel     = self.bot.get_channel(EEW_CHANNEL_ID)   or self.channel
        self.quake_channel   = self.bot.get_channel(QUAKE_CHANNEL_ID) or self.channel
        self.other_channel   = self.bot.get_channel(OTHER_CHANNEL_ID) or self.channel
        self.p2p_eew_channel = self.bot.get_channel(P2P_EEW_CHANNEL_ID) or self.eew_channel
        self.kyoshin_channel = self.bot.get_channel(KYOSHIN_CHANNEL_ID) or self.other_channel

        if not self.fetch_quake.is_running():
            self.fetch_quake.start()

        self.bot.loop.create_task(self.connect_eew_ws())

        if self.p2p_eew_task is None or self.p2p_eew_task.done():
            self.p2p_eew_task = self.bot.loop.create_task(self.connect_p2p_eew_ws())
            logger.info("P2P EEW WebSocket 接続開始（緊急地震速報（警報）専用）")

        if self.speech_task is None or self.speech_task.done():
            self.speech_task = self.bot.loop.create_task(self.speech_worker())

        if self.mp3_task is None or self.mp3_task.done():
            self.mp3_task = self.bot.loop.create_task(self.mp3_worker())

        logger.info("QuakeEewCog: on_ready 完了")

    async def _ws_connect_loop(
        self,
        url: str,
        label: str,
        handler,
        init_delay: int = 5,
        max_delay: int = 60,
    ):
        """
        WebSocket に接続し、切断時に指数バックオフで自動再接続する共通ループ。
        
        接続失敗時は指数バックオフで再接続間隔を延長し、DDoS リスクを軽減します：
          初回: 5秒 → 10秒 → 20秒 → ... → 60秒（最大値）→ ずっと60秒

        Parameters
        ----------
        url       : 接続先 URL
        label     : ログに表示する名称（例: "Wolfx EEW"）
        handler   : 接続後に呼び出す非同期コルーチン関数 (ws) -> None
        init_delay: 初回再接続待機秒数（デフォルト5秒）
        max_delay : 最大再接続待機秒数（デフォルト60秒）
        """
        delay = init_delay
        consecutive_failures = 0
        while not self.bot.is_closed():
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
                # Mark Wolfx connection as dead on disconnect
                if "Wolfx" in label:
                    self._reset_wolfx_ws_state()

            if not self.bot.is_closed():
                logger.info(
                    f"{label} WS: {delay}秒後に再接続 "
                    f"(exponential backoff: {consecutive_failures} failures)"
                )
                await asyncio.sleep(delay)
                # ← exponential backoff: 次回以降、待機時間を2倍に
                delay = min(delay * 2, max_delay)

    def _reset_wolfx_ws_state(self):
        """Wolfx WebSocket 接続状態をリセット（再接続時に呼び出し）"""
        self._wolfx_last_heartbeat = None
        self._wolfx_last_recv = None  # ← 追加：EEW受信タイムスタンプもリセット
        self._wolfx_last_eew_recv = None  # ← 追加：EEW受信タイムスタンプもリセット
        self._wolfx_ws_alive = False
        self._wolfx_heartbeat_timeout_warned = False

    def _fetch_backoff_is_active(self, key: str) -> bool:
        now = time.monotonic()
        until = self._fetch_backoff_until.get(key, 0.0)
        if until > now:
            return True
        if self._fetch_failures.get(key, 0) > 0:
            self._fetch_failures[key] = 0
            self._fetch_backoff_until[key] = 0.0
        return False

    def _reset_fetch_backoff(self, key: str) -> None:
        self._fetch_failures[key] = 0
        self._fetch_backoff_until[key] = 0.0

    def _record_fetch_failure(self, key: str, reason: str) -> None:
        self._fetch_failures[key] = self._fetch_failures.get(key, 0) + 1
        if self._fetch_failures[key] >= FETCH_FAILURE_THRESHOLD:
            self._fetch_backoff_until[key] = time.monotonic() + FETCH_BACKOFF_SECONDS
            logger.warning(
                f"{key} fetch failure threshold reached ({self._fetch_failures[key]}): "
                f"backoff for {FETCH_BACKOFF_SECONDS}s ({reason})"
            )

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
                    if (self.last_eew_event_id is None or
                            self.last_eew_event_id != event_id or
                            self.last_eew_serial < serial):
                        self.last_eew_event_id = event_id
                        self.last_eew_serial   = serial
                        await self.notify_eew(data, source="wolfx")
                except Exception:
                    logger.error(f"EEW 処理エラー:\n{traceback.format_exc()}")

        await self._ws_connect_loop(WOLFX_WSS, "Wolfx EEW", _handle)

    # ===============================
    # WebSocket（P2P EEW code=556）
    # ===============================
    async def connect_p2p_eew_ws(self):
        async def _handle(ws):
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    if data.get("code") != 556:
                        continue
                    wolfx_data = self._convert_p2p_eew_to_wolfx(data)
                    if not wolfx_data:
                        continue
                    event_id = wolfx_data.get("EventID")
                    serial   = wolfx_data.get("Serial", 1)
                    self._last_recv["p2p_eew"] = datetime.now()
                    self._recv_count["p2p_eew"] += 1
                    logger.info(f"P2P EEW 検知: EventID={event_id} Serial={serial}")
                    await self.notify_eew(wolfx_data, source="p2p_eew")
                except Exception:
                    logger.error(f"P2P EEW メッセージ処理エラー:\n{traceback.format_exc()}")

        await self._ws_connect_loop(P2P_WSS, "P2P地震情報 EEW", _handle)

    def _convert_p2p_eew_to_wolfx(self, p2p_data: dict) -> dict | None:
        """
        P2P地震情報の緊急地震速報（code=556）を Wolfx 形式に変換する。

        実際のフィールド仕様（確認済み）:
          issue.eventId   : EventID（"20260515202209" 形式）
          issue.serial    : 情報番号（文字列）
          earthquake.hypocenter.depth     : 深さ km（int）
          earthquake.hypocenter.magnitude : マグニチュード（float）
          earthquake.hypocenter.name      : 震央地名
          earthquake.originTime           : 発生時刻（"2026/05/15 20:22:02" 形式）
          earthquake.condition            : "仮定震源要素" のとき PLUM 法
          areas[].scaleFrom / scaleTo     : int（-1/0/10/20/30/40/45/50/55/60/70）
                                            scaleTo のみ 99（以上）あり
          areas[].kindCode                : "10"=警報未到達 / "11"=到達済 / "19"=PLUM法
          areas[].arrivalTime             : 到達予測時刻（null の場合あり）
          areas[].name                    : 細分区域名
          areas[].pref                    : 府県予報区
        """
        try:
            issue    = p2p_data.get("issue", {})
            event_id = issue.get("eventId", "")
            serial   = safe_int(issue.get("serial", "1")) or 1

            if p2p_data.get("cancelled"):
                return {
                    "type":      "jma_eew",
                    "Title":     "緊急地震速報（警報）",
                    "EventID":   event_id,
                    "Serial":    serial,
                    "isCancel":  True,
                    "isFinal":   False,
                    "Magnitude": -1.0,
                    "Depth":     -1,
                }

            eq    = p2p_data.get("earthquake", {}) or {}
            hypo  = eq.get("hypocenter", {}) or {}
            areas = p2p_data.get("areas", []) or []

            # PLUM 法判定
            is_plum = (
                eq.get("condition") == "仮定震源要素"
                or any(str(a.get("kindCode", "")) == "19" for a in areas)
            )

            # scaleFrom の Enum に 99 は存在しない（scaleTo のみ）
            scale_map = {
                -1: "不明", 0: "0", 10: "1", 20: "2", 30: "3", 40: "4",
                45: "5弱", 50: "5強", 55: "6弱", 60: "6強", 70: "7",
            }

            kind_type_map = {
                "10": "警報",    # 主要動未到達
                "11": "到達済",  # 主要動到達済み
                "19": "警報",    # PLUM法
            }

            max_scale_val = -1
            warn_areas    = []
            has_warn      = False  # kindCode=10 or 19 が1つでもあれば isWarn=True

            for area in areas:
                kind_code = str(area.get("kindCode", ""))
                sf = safe_int(area.get("scaleFrom", -1))
                st = safe_int(area.get("scaleTo",   -1))

                # 最大震度追跡（99は70相当で比較）
                effective = min(st, 70) if st not in (-1, 99) else (sf if sf != -1 else -1)
                if effective > max_scale_val:
                    max_scale_val = effective

                # kindCode=10 or 19 のエリアが1つでもあれば警報フラグ
                # kindCode=11（到達済）だけの場合は isWarn=False のまま
                if kind_code in ("10", "19"):
                    has_warn = True

                shindo1 = scale_map.get(sf, "不明")
                shindo2 = scale_map.get(min(st, 70) if st == 99 else st, shindo1)

                warn_areas.append({
                    "Chiiki":      area.get("name", ""),
                    "Pref":        area.get("pref", ""),
                    "Shindo1":     shindo1,
                    "Shindo2":     shindo2,
                    "Type":        kind_type_map.get(kind_code, "警報"),
                    "KindCode":    kind_code,
                    # arrivalTime は null の場合があるので None → "" に正規化
                    "ArrivalTime": area.get("arrivalTime") or "",
                })

            # MaxIntensity
            if any(safe_int(a.get("scaleTo", -1)) == 99 for a in areas):
                max_intensity = "7以上"
            else:
                max_intensity = scale_map.get(max_scale_val, "不明")

            raw_depth = hypo.get("depth", -1)
            depth = safe_int(raw_depth) if safe_int(raw_depth) != -1 else -1
            mag   = safe_float(hypo.get("magnitude", -1))

            return {
                "type":         "jma_eew",
                "Title":        "緊急地震速報（警報）",
                "EventID":      event_id,
                "Serial":       serial,
                "isCancel":     False,
                "isFinal":      False,
                "isTraining":   bool(p2p_data.get("test", False)),
                "isAssumption": is_plum,
                "isWarn":       has_warn,
                "OriginTime":   eq.get("originTime", ""),
                "ArrivalTime":  eq.get("arrivalTime", ""),
                "Hypocenter":   hypo.get("name", "不明") or "不明",
                "Latitude":     safe_float(hypo.get("latitude",  -200)),
                "Longitude":    safe_float(hypo.get("longitude", -200)),
                "Depth":        depth,
                "Magnitude":    mag if mag > 0 else -1,
                "Magunitude":   str(mag) if mag > 0 else "不明",
                "MaxIntensity": max_intensity,
                "WarnArea":     warn_areas,
                "_source":      "P2P地震情報",
            }

        except Exception:
            logger.error(f"P2P→Wolfx変換エラー:\n{traceback.format_exc()}")
            return None

    # ===============================
    # P2P地震情報 ポーリング（地震情報）
    # ===============================


    @tasks.loop(seconds=3)
    async def fetch_quake(self):
        if self._fetch_backoff_is_active("quake"):
            return

        # Lock を使った排他制御（asyncio.Lock で thread-safe に）
        if self._fetch_quake_lock.locked():
            return
        
        async with self._fetch_quake_lock:
            try:
                # 再試行ロジック: exponential backoff (1s, 2s, 4s)
                retry_delays = [1, 2, 4]
                last_error = None
                
                for attempt in range(len(retry_delays) + 1):
                    try:
                        async with self.session.get(
                            "https://api.p2pquake.net/v2/history?codes=551&limit=1",
                            ) as resp:
                            if resp.status == 200:
                                # 成功時: 通常処理へ
                                data_list = await resp.json()
                                if not data_list:
                                    return

                                data = data_list[0]
                                data_id = data.get("id")

                                if self.last_quake_id is None:
                                    self.last_quake_id = data_id
                                    self._reset_fetch_backoff("quake")
                                    await self.notify_quake(data, extra_note="（ボット起動時の最新情報）")
                                    return

                                if data_id == self.last_quake_id:
                                    self._reset_fetch_backoff("quake")
                                    return

                                self.last_quake_id = data_id
                                self._last_recv["quake"] = datetime.now()
                                self._recv_count["quake"] += 1
                                logger.info(f"P2P地震情報取得: id={data_id}")
                                self._reset_fetch_backoff("quake")
                                await self.notify_quake(data)
                                return
                            
                            elif resp.status >= 500:
                                # 5xx: 再試行対象
                                last_error = f"HTTP {resp.status}"
                                if attempt < len(retry_delays):
                                    delay = retry_delays[attempt]
                                    logger.debug(f"fetch_quake: {delay}秒後に再試行 (HTTP {resp.status})")
                                    await asyncio.sleep(delay)
                                    continue
                            else:
                                # 4xx など: 再試行しない
                                self._record_fetch_failure("quake", f"HTTP {resp.status}")
                                return
                    
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        # 接続エラーやタイムアウト: 再試行対象
                        last_error = str(e)
                        if attempt < len(retry_delays):
                            delay = retry_delays[attempt]
                            logger.debug(f"fetch_quake: {delay}秒後に再試行 ({type(e).__name__})")
                            await asyncio.sleep(delay)
                            continue
                
                # 全再試行失敗
                self._record_fetch_failure("quake", f"retry exhausted: {last_error}")

            except Exception:
                self._record_fetch_failure("quake", "exception")
                logger.error(f"Fetch Quake エラー:\n{traceback.format_exc()}")


    @fetch_quake.before_loop
    async def before_fetch_quake(self):
        """セッション初期化完了まで待機"""
        await self.bot.wait_until_ready()

    # ===============================
    # P2P地震情報 ポーリング（津波情報）
    # ===============================


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
        # ソース別チャンネル選択（channel_override が指定された場合はそちらを優先）
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
            # TTL チェック：300秒以上前のエントリを削除
            self.recent_eews = {
                eid: (d, t) for eid, (d, t) in self.recent_eews.items() 
                if now - t < 300
            }
            
            # サイズ制限：最大 recent_eews_max_size 個まで保持
            # 超過時は最も古いエントリを削除
            while len(self.recent_eews) >= self.recent_eews_max_size:
                oldest_eid = min(
                    self.recent_eews.keys(),
                    key=lambda eid: self.recent_eews[eid][1]
                )
                logger.debug(f"recent_eews LRU削除: {oldest_eid} (最大数{self.recent_eews_max_size}に達した)")
                del self.recent_eews[oldest_eid]
            
            # 新規エントリを追加
            self.recent_eews[event_id] = (data, now)

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

                if event_id == self.monitored_event_id:
                    self.monitored_event_id = None
                return
            if len(self.recent_eews) > 1:
                embed_summary = discord.Embed(title="複数の緊急地震速報が発表されています", color=0xFF0000, timestamp=datetime.now())
                summary_desc = ""
                sorted_eews = sorted(self.recent_eews.items(), key=lambda x: x[1][1])
                for eid, (old_data, _) in sorted_eews:
                    title_text = old_data.get('Title', '緊急地震速報')
                    s = int(old_data.get("Serial", 1))
                    f = old_data.get("isFinal", False)
                    serial_text = "最終報" if f else f"第{s}報"
                    hypo = old_data.get("Hypocenter", "不明")
                    max_int = old_data.get("MaxIntensity", "不明")
                    
                    # PLUM法の場合はマグニチュードを「推定なし」と表示
                    if old_data.get("isAssumption", False):
                        mag = "推定なし"
                    else:
                        mag = old_data.get("Magnitude") or old_data.get("Magunitude") or "不明"
                    
                    summary_desc += f"**{title_text} ({serial_text})**\n震源地: {hypo} / 予想最大震度: {max_int} / M{mag}\n\n"
                embed_summary.description = summary_desc.strip()
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

            # PLUM法の場合は「約」を付けない
            depth_display = f"約{depth_str}km" if not is_plum else depth_str
            
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

            # PLUM法の場合は震源情報が推定不可のため、深さ・マグニチュード依存の判定をスキップ
            if not is_plum:
                is_sea = safe_bool(data.get("isSea", False))
                if is_sea and safe_float(mag) >= 6.8 and safe_int(depth) <= 151:
                    notes.append("**⚠念の為海岸から離れてください。**")

                if safe_int(depth) >= 151:
                    notes.append("**⚠震源が深いため、遠方でも揺れる可能性があります。**")

            if notes:
                description += "\n\n" + "\n".join(notes)

            warn_areas = data.get("WarnArea", [])
            if warn_areas:
                alert_regions = set()
                for area in warn_areas:
                    chiiki = area.get("Chiiki")
                    if chiiki and area.get("Type", "").lower() in ("警報", "到達済"):
                        alert_regions.add(REGION_MAP.get(chiiki, "その他"))
                if alert_regions:
                    description += "\n\n**【強い揺れが予想される地域】**\n"
                    for region in sorted(alert_regions):
                        description += f"■ {region}　"

            if warn_areas:
                forecast_groups = defaultdict(list)
                def shindo_rank(s):
                    if s in INT_MAP.values():
                        for num, txt in INT_MAP.items():
                            if txt == s: return num
                    return 0

                is_assumption = data.get("isAssumption", False)

                for area in warn_areas:
                    chiiki = area.get("Chiiki")
                    if not chiiki: continue
                    shindo1 = area.get("Shindo1", "不明")
                    shindo2 = area.get("Shindo2", shindo1)

                    if is_assumption:
                        if shindo1 != "不明":
                            label = f"震度{shindo1}程度"
                            forecast_groups[label].append(chiiki)
                        continue

                    if shindo1 != "不明":
                        if shindo1 == shindo2:
                            label = f"震度{shindo1}程度"
                        else:
                            r1 = shindo_rank(shindo1)
                            r2 = shindo_rank(shindo2)
                            high, low = (shindo1, shindo2) if r1 >= r2 else (shindo2, shindo1)
                            label = f"震度{high}〜{low}程度"
                        forecast_groups[label].append(chiiki)

                if forecast_groups:
                    description += "\n\n**【地域ごとの予想震度】**"
                    sorted_labels = sorted(forecast_groups.keys(), key=lambda lbl: max(shindo_rank(s.strip("震度程度〜")) for s in lbl.split("〜")), reverse=True)
                    for label in sorted_labels:
                        areas = sorted(forecast_groups[label])
                        area_text = "\n".join(f"　{a}" for a in areas)
                        description += f"\n■ {label}\n{area_text}"

            # Discord API の制限（4096文字）に対応した正確な切り詰め
            description = truncate_embed_description(
                description,
                max_chars=4096,
                suffix="\n\n（地域が多いため一部省略）"
            )

            embed.description = description
            if is_test:
                embed.set_footer(text="※これはテスト通知です。")

            await channel.send(embed=embed)

            if (is_final or is_cancel) and event_id == self.monitored_event_id:
                self.monitored_event_id = None
                if self.vibration_monitor_task:
                    self.vibration_monitor_task.cancel()
            asyncio.create_task(self.generate_and_speak_eew(data))
            await self.play_eew_sound(data)

        except Exception as e:
            logger.error(f"notify_eew エラー:\n{traceback.format_exc()}")

    # ===============================
    # 読み上げエンジン
    # ===============================
    async def generate_and_speak_eew(self, data):
        """QuakeTsunami_antei.html の generateAndPlaySpeech をPythonで再現"""
        serial = int(data.get("Serial", 1))
        is_warn = data.get("isWarn", False)
        is_plum = data.get("isAssumption", False)
        hypo = data.get("Hypocenter", "不明")
        max_int_str = data.get("MaxIntensity", "")

        current_warn_areas = set()
        for area in data.get("WarnArea", []):
            if area.get("Type", "").lower() == "警報":
                chiiki = area.get("Chiiki")
                if chiiki:
                    current_warn_areas.add(REGION_MAP.get(chiiki, chiiki))

        prev_warn_areas = set()
        if self.last_eew_data:
            for area in self.last_eew_data.get("WarnArea", []):
                if area.get("Type", "").lower() == "警報":
                    chiiki = area.get("Chiiki")
                    if chiiki:
                        prev_warn_areas.add(REGION_MAP.get(chiiki, chiiki))

        area_changed = current_warn_areas != prev_warn_areas
        warn_area_text = "、".join(sorted(current_warn_areas)) + "では" if current_warn_areas else ""

        text = ""
        priority = 3

        if serial == 1 or self.last_eew_data is None:
            if is_warn:
                priority = 1
                text = f"緊急地震速報。{warn_area_text}強い揺れに警戒してください。{hypo}で地震。推定最大震度{max_int_str}。"
            else:
                priority = 3
                text = f"{hypo}で地震。推定最大震度{max_int_str}。"
                if is_plum:
                    text += "（PLUM法）"

        else:
            prev_max_int = self.last_eew_data.get("MaxIntensity", "")
            prev_hypo = self.last_eew_data.get("Hypocenter", "")

            if is_warn and area_changed and current_warn_areas:
                priority = 1
                text = f"緊急地震速報。{warn_area_text}強い揺れに警戒してください。"

            elif self._is_intensity_changed_significantly(prev_max_int, max_int_str):
                priority = 2
                text = f"推定最大震度{max_int_str}"

            elif hypo != prev_hypo:
                priority = 3
                text = f"{hypo}で地震。推定最大震度{max_int_str}。"

        if text:
            await self.speak_local(text, priority)

        self.last_eew_data = data.copy()

    def _is_intensity_changed_significantly(self, prev: str, current: str) -> bool:
        order = ["不明", "1", "2", "3", "4", "5弱", "推定5弱以上", "5強", "6弱", "6強", "7"]
        try:
            idx_prev = order.index(prev)
            idx_cur = order.index(current)
            return abs(idx_cur - idx_prev) >= 1
        except ValueError:
            return False

    # ===============================
    # EEW 音声再生ロジック
    # ===============================
    async def play_eew_sound(self, data):
        is_cancel = data.get("isCancel", False)
        serial = int(data.get("Serial", 1))
        is_final = data.get("isFinal", False)
        is_warn = data.get("isWarn", False)
        event_id = data.get("EventID")
        max_int_str = data.get("MaxIntensity", "不明")

        if self.last_eew_event_id != event_id:
            self.audio_flags = {"warning": False, "int3": False, "first": False, "final": False, "cancel": False}
            self.last_warn_areas = set()
            self.last_eew_event_id = event_id

        if is_cancel:
            if not self.audio_flags.get("cancel"):
                await self.play_mp3("eewC")
                self.audio_flags["cancel"] = True
                logger.debug("音声: eewC (キャンセル)")
            self.last_eew_data = data.copy()
            return

        current_warn_areas = {
            a.get("Chiiki") for a in data.get("WarnArea", [])
            if a.get("Type") == "警報"
        }

        should_play_high_alert = False
        if is_warn:
            if not self.audio_flags.get("warning"):
                should_play_high_alert = True
                self.audio_flags["warning"] = True
            else:
                new_areas = current_warn_areas - self.last_warn_areas
                if new_areas:
                    should_play_high_alert = True
                    logger.info(f"警報地域が追加されました: {new_areas}")

        if should_play_high_alert:
            await self.play_mp3("high_alert")
            self.last_warn_areas = current_warn_areas
            logger.debug(f"音声: high_alert (地域数: {len(current_warn_areas)})")
            # last_eew_data はここで更新しない。
            # generate_and_speak_eew が後から実行されるとき prev==current となり
            # area_changed=False になってTTS警報地域が読み上げられなくなるため。
            return

        int3_or_higher = ["3", "4", "5弱", "5強", "6弱", "6強", "7", "推定5弱以上"]
        if not is_warn and max_int_str in int3_or_higher:
            if not self.audio_flags.get("int3"):
                await self.play_mp3("eew3")
                self.audio_flags["int3"] = True
                logger.debug("音声: eew3 (震度3以上)")
                self.last_eew_data = data.copy()
                return

        if is_final and not self.audio_flags.get("final"):
            await self.play_mp3("saisyu")
            self.audio_flags["final"] = True
            logger.debug("音声: saisyu (最終報)")

        elif serial > 1:
            await self.play_mp3("koushin")
            logger.debug("音声: koushin (更新)")

        elif serial == 1 and not self.audio_flags.get("first"):
            await self.play_mp3("low_alert")
            self.audio_flags["first"] = True
            logger.debug("音声: low_alert (初報)")

        self.last_eew_data = data.copy()

    # ===============================
    # ===============================
    # 長周期地震動モニタ EEW ポーリング
    # ===============================
    # 強震モニタ監視ループ（振動レベル + 強震モニタ画像 + 長周期地震動モニタ画像）
    # ===============================
    async def vibration_monitor_loop(self, target_event_id):
        KWATCH_URL = "https://kwatch-24h.net/EQLevel.json"
        JMA_S_BASE = "https://smi.lmoniexp.bosai.go.jp/data/map_img/RealTimeImg/jma_s"
        LMONI_BASE = "https://www.lmoni.bosai.go.jp/monitor/data/data/map_img/RealTimeImg/abrspmx_s"
        DELAY_SEC  = 4
        STEP_SEC   = 3
        MAX_RETRY  = 4

        # 直前に取得できた画像URLをキャッシュして同一秒への重複HEADリクエストを防ぐ
        _last_jma_s_url:  str | None = None
        _last_lmoni_url:  str | None = None
        _last_jma_s_ts:   str        = ""
        _last_lmoni_ts:   str        = ""
        # 振動レベル MP3 のtier管理（tier変化時のみ再生）
        # 0=100未満, 1=100-999, 2=1000-1999, 3=2000以上
        _prev_vib_tier: int = 0

        async def find_monitor_image(base_url: str, suffix: str,
                                     last_url: str | None, last_ts: str
                                     ) -> tuple[str | None, str]:
            """
            現在時刻 - DELAY_SEC から遡って画像 URL を返す。
            直前と同じタイムスタンプなら HEAD リクエストを省略してキャッシュを返す。
            戻り値: (url | None, timestamp_str)
            """
            for i in range(MAX_RETRY):
                dt = datetime.now() - timedelta(seconds=DELAY_SEC + STEP_SEC * i)
                ts = dt.strftime("%Y%m%d%H%M%S")
                # 同じタイムスタンプなら前回結果を再利用
                if ts == last_ts:
                    return last_url, last_ts
                url = f"{base_url}/{dt.strftime('%Y%m%d')}/{ts}.{suffix}.gif"
                try:
                    async with self.session.head(url, timeout=3) as resp:
                        if resp.status == 200:
                            return url, ts
                except Exception:
                    pass
            return None, last_ts

        start_time = datetime.now().timestamp()
        if not ENABLE_KYOSHIN:
            return
        channel = self.kyoshin_channel or self.channel

        try:
            while (self.monitored_event_id == target_event_id and
                   not self.bot.is_closed() and
                   datetime.now().timestamp() - start_time < 300):

                level = None
                try:
                    async with self.session.get(KWATCH_URL, timeout=3) as resp:
                        if resp.status == 200:
                            kdata = await resp.json()
                            level = int(kdata.get("l", 0))
                except Exception as e:
                    logger.error(f"強震モニタ: 振動レベル取得エラー: {e}")

                _last_jma_s_url, _last_jma_s_ts  = await find_monitor_image(
                    JMA_S_BASE, "jma_s", _last_jma_s_url, _last_jma_s_ts
                )
                _last_lmoni_url, _last_lmoni_ts = await find_monitor_image(
                    LMONI_BASE, "abrspmx_s", _last_lmoni_url, _last_lmoni_ts
                )

                # ── 振動レベル MP3（tier 該当中はループごとに継続再生）──
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
                    # tier 0（100未満）以外は該当tierの間、ループ（3秒）ごとに再生し続ける
                    mp3_key = {3: "lv2000", 2: "lv1000", 1: "lv100"}.get(cur_tier)
                    if mp3_key:
                        await self.play_mp3(mp3_key)
                        logger.debug(f"振動レベル MP3 再生: {mp3_key} (level={level})")

                if level is not None or _last_jma_s_url or _last_lmoni_url:
                    if level is not None:
                        if level >= 1000:
                            color = 0xFF0000
                        elif level >= 100:
                            color = 0xFFFF00
                        else:
                            color = 0xFFFFFF
                        level_str = f"**振動レベル: {level}**\n"
                    else:
                        color = 0xAAAAAA
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

                    if _last_jma_s_url:
                        embed.set_image(url=_last_jma_s_url)
                    if _last_lmoni_url:
                        embed.set_thumbnail(url=_last_lmoni_url)

                    await channel.send(embed=embed)

                await asyncio.sleep(3)

        except asyncio.CancelledError:
            logger.info(f"強震モニタ監視終了 (EventID={target_event_id})")
        except Exception as e:
            logger.error(f"強震モニタ監視ループ エラー: {e}")
        finally:
            if self.monitored_event_id == target_event_id:
                self.monitored_event_id = None

    # ===============================
    # 地震情報通知
    # ===============================
    async def notify_quake(self, data, is_test=False, extra_note=None):
        channel = self.quake_channel or self.channel
        if not channel:
            return

        if isinstance(data, list) and data:
            data = data[0]

        if data.get("_source") == "JMA":
            data = self._convert_jma_quake_to_p2p(data)

        issue_type = data.get("issue", {}).get("type", "Other")
        eq = data.get("earthquake", {})
        hypo = eq.get("hypocenter", {})

        # ===== フィルター判定（テスト通知は除外しない）=====
        if not is_test:
            # 情報種別フィルター
            type_filter_map = {
                "ScalePrompt":       QUAKE_ENABLE_SCALE_PROMPT,
                "Destination":       QUAKE_ENABLE_DESTINATION,
                "ScaleAndDestination": QUAKE_ENABLE_SCALE_AND_DEST,
                "DetailScale":       QUAKE_ENABLE_DETAIL_SCALE,
                "Foreign":           QUAKE_ENABLE_FOREIGN,
                "Other":             QUAKE_ENABLE_OTHER,
            }
            if not type_filter_map.get(issue_type, True):
                return

            max_scale_val = eq.get("maxScale", -1)
            if max_scale_val != -1 and max_scale_val < QUAKE_MIN_SCALE:
                return

            mag_val = hypo.get("magnitude", -1)
            if mag_val != -1 and mag_val < QUAKE_MIN_MAG:
                return

            depth_val = hypo.get("depth", -1)
            if depth_val != -1:
                if depth_val < QUAKE_MIN_DEPTH or depth_val > QUAKE_MAX_DEPTH:
                    return

        source = data.get("issue", {}).get("source", "P2P地震情報")

        max_scale_val = eq.get("maxScale", -1)
        max_scale_str = INT_MAP.get(max_scale_val, "不明")
        color = SHINDO_COLORS.get(max_scale_val, 0x62626B)

        comments = data.get("comments", {}).get("freeFormComment", "")
        is_volcano = "大規模な噴火が発生しました" in comments

        title = f"{QUAKE_TYPE_MAP.get(issue_type, '地震情報')}"
        if is_volcano:
            title = "大規模噴火に関する情報"
        if is_test:
            title = "【テスト】" + title

        embed = discord.Embed(title=title, color=color, timestamp=datetime.now())

        name = hypo.get("name", "調査中")
        mag = hypo.get("magnitude", -1)
        depth = hypo.get("depth", -1)

        mag_str = f"M{mag}" if mag != -1 else "調査中"
        depth_str = f"約{depth}km" if depth != -1 else "調査中"

        occur_time = format_jma_time(eq.get("time", "不明"))

        description = (
            f"**発表機関： {source}**\n"
            f"**発生時刻： {occur_time}**\n"
            f"**震源地： {name}**\n"
            f"**最大震度： {max_scale_str}**\n"
            f"**マグニチュード： {mag_str}**\n"
            f"**深さ： {depth_str}**"
        )

        dom_tsunami = eq.get("domesticTsunami", "None")
        description += f"\n\n{TSUNAMI_MAP.get(dom_tsunami, '情報なし')}"

        # 訂正情報の表示（コメントの前）
        # None または "Unknown" の場合は表示しない
        correct = data.get("issue", {}).get("correct")
        if correct and correct not in ("Unknown",):
            correction_messages = {
                "ScaleOnly": "震度が更新されました",
                "DestinationOnly": "震源が更新されました", 
                "ScaleAndDestination": "震度・震源が更新されました"
            }
            correction_text = correction_messages.get(correct)
            if correction_text:
                description += f"\n\n**[訂正] {correction_text}**"

        if comments:
            description += f"\n\n{comments}"

        embed.description = description

        quake_id = data.get("id")
        footer_parts = [extra_note] if extra_note else []
        if is_test:
            footer_parts.append("※これはテスト通知です。")
        if footer_parts:
            embed.set_footer(text=" | ".join(footer_parts))

        # 全ての地震情報種別で画像を表示
        # CDN の画像生成遅延があるため、先にメッセージを送信してから非同期で追加
        sent_msg = await channel.send(embed=embed)
        if quake_id:
            self.bot.loop.create_task(self._attach_p2p_image(sent_msg, quake_id))

        # 震度速報で最大震度6弱以上 → 2.5秒後に zencyu.mp3 再生（15分クールダウン）
        if issue_type == "ScalePrompt" and max_scale_val >= 55:
            now_dt = datetime.now()
            cooldown_ok = (
                self._last_zencyu_time is None
                or (now_dt - self._last_zencyu_time).total_seconds() >= 900
            )
            if cooldown_ok:
                self._last_zencyu_time = now_dt
                async def _play_zencyu_delayed():
                    await asyncio.sleep(2.5)
                    await self.play_mp3("zencyu")
                    logger.info(f"zencyu.mp3 再生: maxScale={max_scale_val} ({max_scale_str})")
                self.bot.loop.create_task(_play_zencyu_delayed())
            else:
                remain = 900 - int((now_dt - self._last_zencyu_time).total_seconds())
                logger.debug(f"zencyu.mp3 クールダウン中 (あと{remain}秒)")

        # occur_time は既に「YYYY年M月D日H時MM分頃」形式なので
        # 読み上げ用に「H時MM分頃、」部分だけ抽出する
        time_str = ""
        if occur_time and "時" in occur_time and "分頃" in occur_time:
            try:
                # 「H時MM分頃」部分を取り出す（例: "2026年4月18日8時20分頃" → "8時20分頃、"）
                t_part = occur_time[occur_time.index("日") + 1:]  # "8時20分頃"
                time_str = t_part + "、"
            except Exception:
                time_str = ""

        # 津波情報の読み上げ文
        tsunami_speak = ""
        tsunami_speak_map = {
            "MajorWarning": "現在、大津波警報を発表中です。",
            "Warning":      "現在、津波警報を発表中です。",
            "Watch":        "現在、津波注意報を発表中です。",
        }
        if dom_tsunami in tsunami_speak_map:
            tsunami_speak = " " + tsunami_speak_map[dom_tsunami]

        # issue_type 別の読み上げ
        if issue_type == "ScalePrompt":
            # 震度速報: 震源情報なし
            speak_text = (
                f"{title}。"
                f"{time_str}最大震度{max_scale_str}を観測する地震がありました。"
                f"震源地・規模は調査中です。{tsunami_speak}"
            )
        elif issue_type == "Destination":
            # 震源に関する情報: 震度なし
            speak_text = (
                f"{title}。"
                f"{time_str}地震がありました。"
                f" 震源地は {name}。"
                f" 深さは {depth_str}。"
                f" マグニチュードは {mag_str}。{tsunami_speak}"
            )
        elif issue_type in ("ScaleAndDestination", "DetailScale"):
            # 震度・震源情報: フル読み上げ
            speak_text = (
                f"{title}。"
                f"{time_str}最大震度{max_scale_str}を観測する地震がありました。{tsunami_speak}"
                f" 震源地は {name}。"
                f" 震源の深さは {depth_str}。"
                f" マグニチュードは {mag_str} と推定されます。"
            )
        elif issue_type == "Foreign":
            # 遠地地震
            speak_text = (
                f"{title}。"
                f"{time_str}遠地地震がありました。"
                f" 震源地は {name}。"
                f" マグニチュードは {mag_str}。{tsunami_speak}"
            )
        else:
            # Other など
            speak_text = (
                f"{title}。{tsunami_speak}"
                f" 震源地 {name}。 最大震度 {max_scale_str}。 {mag_str}。"
            )

        await self.speak_local(speak_text)

        await self.play_quake_sound(data)

    # ===============================
    # P2P地震情報のmp3音声
    # ===============================
    async def play_quake_sound(self, data):
        issue_type = data.get("issue", {}).get("type", "Other")
        if issue_type == "ScalePrompt":
            await self.play_mp3("vxse51")
        elif issue_type == "Destination":
            await self.play_mp3("vxse52")
        else:
            await self.play_mp3("vxse53")
