"""
cogs/usgs.py
============
米国地質調査所（USGS）の海外地震情報を扱う Cog。

【この Cog が担当する機能】
- USGS GeoJSON API のポーリング（USGS_FETCH_INTERVAL秒間隔、デフォルト600秒）
- 対象地域・マグニチュード閾値でフィルタリングして通知
- 起動時は既存の地震をすべて「既読」として記録するのみで通知しない
  （2回目以降のポーリングで新規発生分のみ通知）
- 重複通知防止（USGS_NOTIFICATION_COOLDOWN秒）

【他モジュールとの依存関係】
- core.config       : USGS_ENABLED, USGS_CHANNEL_ID, USGS_MAGNITUDE_MIN,
                       USGS_FETCH_INTERVAL, USGS_REGION_LAT_MIN/MAX,
                       USGS_REGION_LON_MIN/MAX, USGS_NOTIFICATION_COOLDOWN,
                       CHANNEL_ID, FETCH_FAILURE_THRESHOLD, FETCH_BACKOFF_SECONDS
- core.helpers      : safe_int, safe_float, safe_bool,
                       truncate_embed_description, format_jma_time
- core.audio.AudioMixin : speak_local, play_mp3（多重継承で利用）

【Step4 時点の設計メモ】
- 元のbot.pyでは USGS_CHANNEL_ID 未設定時に QUAKE_CHANNEL_ID（quake_channel）
  へフォールバックしていたが、Cog分割によりQuakeEewCogとUsgsCogは
  互いの内部状態を直接参照できない。
  そのため本Cogでは「USGS_CHANNEL_ID → CHANNEL_ID」のフォールバックのみ行う。
  quake_channel と同じ値にしたい場合は .env で USGS_CHANNEL_ID を
  明示的に QUAKE_CHANNEL_ID と同じ値に設定すること。
- Circuit Breaker（_fetch_backoff_is_active 等）は quake.py/tsunami.py と
  同じロジックをこの Cog 内にも個別実装している
  （Step5でSystemCog切り出し時に共通化を検討）。
"""
import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import traceback
import time
from datetime import datetime
import logging

from core.config import (
    CHANNEL_ID, USGS_CHANNEL_ID,
    USGS_ENABLED, USGS_MAGNITUDE_MIN, USGS_FETCH_INTERVAL,
    USGS_REGION_LAT_MIN, USGS_REGION_LAT_MAX,
    USGS_REGION_LON_MIN, USGS_REGION_LON_MAX,
    USGS_NOTIFICATION_COOLDOWN,
    FETCH_FAILURE_THRESHOLD, FETCH_BACKOFF_SECONDS,
    SPEECH_QUEUE_MAXSIZE, MP3_QUEUE_MAXSIZE,
)
from core.helpers import (
    safe_int, safe_float, safe_bool,
    truncate_embed_description, format_jma_time,
)
from core.audio import AudioMixin

logger = logging.getLogger("QTLBot")


class UsgsCog(commands.Cog, AudioMixin):
    """USGS 海外地震情報を扱う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # -- チャンネル（on_ready で解決） --
        self.channel      = None
        self.usgs_channel = None

        # -- HTTPセッション（この Cog 専用） --
        self.session = None
        self.headers = {"Accept-Encoding": "identity"}

        # -- USGS 重複排除状態 --
        self.last_usgs_ids: dict[str, float] = {}  # USGS Event ID → 通知時刻（cooldown 用）
        self._usgs_initialized = False  # 初回ポーリングでは通知せずIDのみ記録
        self._usgs_last_fetch_time = None

        # -- 受信統計（!status 用。将来的にSystemCogと統合予定） --
        self._last_recv = {"usgs": None}
        self._recv_count = {"usgs": 0}

        # -- fetch_usgs_quake の Circuit Breaker --
        self._fetch_failures = {"usgs": 0}
        self._fetch_backoff_until = {"usgs": 0.0}

        # -- 音声再生（AudioMixin が要求する属性） --
        self.speech_queue = asyncio.PriorityQueue(maxsize=SPEECH_QUEUE_MAXSIZE)
        self.speech_task = None
        self.mp3_queue = asyncio.Queue(maxsize=MP3_QUEUE_MAXSIZE)
        self.mp3_task = None
        self.audio_files = {}  # USGS通知は現状専用MP3を使わない

    # ===============================
    # Circuit Breaker ヘルパー
    # ===============================
    def _fetch_backoff_is_active(self, key):
        now = time.monotonic()
        until = self._fetch_backoff_until.get(key, 0.0)
        if until > now:
            return True
        if self._fetch_failures.get(key, 0) > 0:
            self._fetch_failures[key] = 0
            self._fetch_backoff_until[key] = 0.0
        return False

    def _reset_fetch_backoff(self, key):
        self._fetch_failures[key] = 0
        self._fetch_backoff_until[key] = 0.0

    def _record_fetch_failure(self, key, reason):
        self._fetch_failures[key] = self._fetch_failures.get(key, 0) + 1
        if self._fetch_failures[key] >= FETCH_FAILURE_THRESHOLD:
            self._fetch_backoff_until[key] = time.monotonic() + FETCH_BACKOFF_SECONDS
            logger.warning(
                f"{key} fetch failure threshold reached ({self._fetch_failures[key]}): "
                f"backoff for {FETCH_BACKOFF_SECONDS}s ({reason})"
            )

    # ===============================
    # Cog起動・終了
    # ===============================

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
            connector=aiohttp.TCPConnector(limit=50, ttl_dns_cache=300),
        )
        logger.info("UsgsCog: aiohttp セッションを作成しました")

    async def cog_unload(self):
        if self.fetch_usgs_quake.is_running():
            self.fetch_usgs_quake.cancel()

        for bg_task in (self.speech_task, self.mp3_task):
            if bg_task and not bg_task.done():
                bg_task.cancel()

        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("UsgsCog: aiohttp セッションを閉じました")

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel      = self.bot.get_channel(CHANNEL_ID)
        self.usgs_channel = self.bot.get_channel(USGS_CHANNEL_ID) or self.channel

        if USGS_ENABLED:
            if not self.fetch_usgs_quake.is_running():
                self.fetch_usgs_quake.start()
                logger.info(f"USGS地震情報ポーリングタスクを開始しました（間隔: {USGS_FETCH_INTERVAL}秒）")
            else:
                logger.info("USGS地震情報ポーリングタスクは既に実行中です")
        else:
            logger.info("USGS地震情報機能: 無効（USGS_ENABLED=false）")

        if self.speech_task is None or self.speech_task.done():
            self.speech_task = self.bot.loop.create_task(self.speech_worker())

        if self.mp3_task is None or self.mp3_task.done():
            self.mp3_task = self.bot.loop.create_task(self.mp3_worker())

        logger.info("UsgsCog: on_ready 完了")

    # ===============================
    # ポーリング・通知
    # ===============================

    # ===============================
    # USGS 地震情報 ポーリング
    # ===============================

    @tasks.loop(seconds=USGS_FETCH_INTERVAL)
    async def fetch_usgs_quake(self):
        """USGS GeoJSON から地震情報を取得し、フィルタリング後に通知

        - エンドポイント: all_day（過去24時間）を使用
          all_hour（過去1時間）はポーリング間隔10分と相性が悪く、
          cooldown が切れた既存IDを再通知してしまう。
        - 新規判定: last_usgs_ids に存在しない event_id のみ通知
        - cooldown: 通知済み ID を USGS_NOTIFICATION_COOLDOWN 秒保持し、
          時刻切れエントリは都度削除してメモリリークを防止
        """
        if not USGS_ENABLED:
            return

        # 使用エンドポイント: significant_week より all_day が網羅性高い
        # M5.0+ に絞るなら significant_week でも可だが、地域フィルターと組み合わせるため all_day を使用
        url = (
            "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"
        )

        try:
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
                headers={"User-Agent": "QTLBot/1.0 (Discord earthquake bot; contact via GitHub)"},
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"USGS fetch failed: HTTP {resp.status}")
                    self._record_fetch_failure("usgs", f"HTTP {resp.status}")
                    return

                data = await resp.json(content_type=None)
                features = data.get("features", [])

                if not features:
                    logger.debug("USGS: no earthquakes in the past 24 hours")
                    return

                self._last_recv["usgs"] = datetime.now()
                self._reset_fetch_backoff("usgs")

                now = time.time()

                # cooldown 切れエントリを削除（メモリリーク防止）
                self.last_usgs_ids = {
                    eid: ts for eid, ts in self.last_usgs_ids.items()
                    if now - ts < USGS_NOTIFICATION_COOLDOWN
                }

                notified = 0
                for feature in features:
                    try:
                        props = feature.get("properties", {})
                        geom  = feature.get("geometry", {})
                        coords = geom.get("coordinates", [None, None, None])

                        event_id = feature.get("id", "")
                        if not event_id:
                            continue

                        mag  = props.get("mag")
                        place = props.get("place", "Unknown")

                        # フィルター①: マグニチュード下限
                        if mag is None or mag < USGS_MAGNITUDE_MIN:
                            continue

                        # フィルター②: 地域（緯度・経度）
                        lon = coords[0] if len(coords) > 0 else None
                        lat = coords[1] if len(coords) > 1 else None
                        if lat is None or lon is None:
                            continue

                        if not (USGS_REGION_LAT_MIN <= lat <= USGS_REGION_LAT_MAX and
                                USGS_REGION_LON_MIN <= lon <= USGS_REGION_LON_MAX):
                            logger.debug(f"USGS skip: 地域外 {event_id} lat={lat:.1f} lon={lon:.1f}")
                            continue

                        # フィルター③: 重複通知防止（cooldown 内は通知しない）
                        if event_id in self.last_usgs_ids:
                            logger.debug(
                                f"USGS skip: cooldown {event_id} "
                                f"({now - self.last_usgs_ids[event_id]:.0f}s ago)"
                            )
                            continue

                        # 通知済みとして登録
                        self.last_usgs_ids[event_id] = now
                        self._recv_count["usgs"] = self._recv_count.get("usgs", 0) + 1

                        # 初回ポーリングは「起動前から存在した地震」の可能性が高いため通知しない。
                        # IDだけ記録し、次回以降の本当の新規発生時のみ通知する。
                        if not self._usgs_initialized:
                            logger.info(f"USGS起動時の既存情報を記録（通知はしない）: {event_id} M{mag} {place}")
                            continue

                        logger.info(f"USGS地震検知: {event_id} M{mag} {place}")
                        await self.notify_usgs_quake(feature)
                        notified += 1

                        # 過剰通知防止: 1回のポーリングで最大3件まで
                        if notified >= 3:
                            logger.info("USGS: 1ポーリングあたりの上限(3件)に達しました")
                            break

                    except Exception as e:
                        logger.error(f"USGS feature処理エラー: {e}", exc_info=True)
                        continue

                self._usgs_initialized = True

                logger.debug(f"USGS: {len(features)}件中 {notified}件通知")

        except asyncio.TimeoutError:
            logger.warning("USGS fetch timeout")
            self._record_fetch_failure("usgs", "timeout")
        except aiohttp.ClientError as e:
            logger.warning(f"USGS fetch network error: {e}")
            self._record_fetch_failure("usgs", f"ClientError: {e}")
        except Exception as e:
            logger.error(f"USGS fetch error: {e}", exc_info=True)

    @fetch_usgs_quake.before_loop
    async def before_fetch_usgs_quake(self):
        """セッション初期化完了まで待機"""
        await self.bot.wait_until_ready()

    # ===============================
    # USGS 地震情報通知
    # ===============================

    async def notify_usgs_quake(self, feature: dict, extra_note: str = ""):
        """
        USGS GeoJSON feature を Discord に通知する
        
        Parameters
        ----------
        feature : dict
            USGS GeoJSON の feature オブジェクト
        extra_note : str
            追加の注記（例：「（ボット起動時の最新情報）」）
        """
        channel = self.usgs_channel or self.channel
        if not channel:
            return
        
        try:
            props = feature.get("properties", {})
            geom = feature.get("geometry", {})
            coords = geom.get("coordinates", [None, None, None])
            
            event_id = feature.get("id", "Unknown")
            mag = props.get("mag", 0)
            place = props.get("place", "Unknown")
            time_ms = props.get("time", 0)
            lon, lat, depth_km = coords[0], coords[1], coords[2] if len(coords) > 2 else 0
            
            # 時刻をフォーマット（UNIX ミリ秒 → 日本時間）
            try:
                import datetime as dt
                quake_time = dt.datetime.fromtimestamp(
                    time_ms / 1000, 
                    tz=dt.timezone(dt.timedelta(hours=9))  # JST
                )
                occur_time_str = quake_time.strftime("%Y年%m月%d日%H時%M分")
            except Exception:
                occur_time_str = "不明"
            
            # Embed の色（マグニチュードで変更）
            if mag >= 7.0:
                color = 0xFF0000  # 赤
            elif mag >= 6.0:
                color = 0xFF6600  # オレンジ
            elif mag >= 5.0:
                color = 0xFFFF00  # 黄
            else:
                color = 0x00FF00  # 緑
            
            # Embed を作成
            title = f"USGS 地震情報 (M{mag:.1f})"
            description = (
                f"**発生時刻**: {occur_time_str}\n"
                f"**震源地**: {place}\n"
                f"**マグニチュード**: M{mag:.1f}\n"
                f"**深さ**: {depth_km:.1f} km\n"
                f"**座標**: {lat:.2f}°N, {lon:.2f}°E"
            )
            
            if extra_note:
                description += f"\n\n{extra_note}"
            
            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=datetime.now()
            )
            embed.set_footer(text="USGS Earthquake Hazards Program")
            
            # USGS リンク
            usgs_url = f"https://earthquake.usgs.gov/earthquakes/events/{event_id}/"
            embed.add_field(name="詳細情報", value=f"[USGS]({usgs_url})", inline=False)
            
            await channel.send(embed=embed)
            
            # 読み上げ
            speak_text = (
                f"USGS地震情報。"
                f"{occur_time_str}、"
                f"{place}で"
                f"マグニチュード{mag:.1f}の地震が発生しました。"
            )
            await self.speak_local(speak_text, priority=3)
            
            logger.info(f"USGS地震情報を通知しました: {event_id} / M{mag:.1f}")
        
        except Exception as e:
            logger.error(f"notify_usgs_quake エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")