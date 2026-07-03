"""
cogs/other.py
=============
長周期地震動、および気象庁「その他」情報（後発地震注意情報・南海トラフ地震臨時情報・
顕著な地震の震源要素更新のお知らせ）を扱う Cog。

【この Cog が担当する機能】
- 長周期地震動観測情報のポーリング（1分間隔）・通知
- 気象庁 quake API 経由での特別情報のポーリング（1分間隔）・通知
  - 北海道・三陸沖後発地震注意情報（区域図添付）
  - 南海トラフ地震臨時情報（区域図添付）
  - 顕著な地震の震源要素更新のお知らせ

【Step6 時点の設計メモ: tsunami.py との重複に見える点について】
気象庁は「南海トラフ地震臨時情報」「顕著な地震の震源要素更新のお知らせ」を
実は2つの異なるAPIエンドポイントに重複して掲載している:
  - bosai/tsunami/data/list.json 経由 → cogs/tsunami.py の
    notify_nankai_trough() / notify_hypocenter_update() が処理
  - bosai/quake/data/list.json 経由 → この Cog の
    notify_quake_advisory() が処理（地図画像添付・独自ttlフィルタあり）
これは元のbot.py（分割前）から存在した設計であり、Cog分割によって
新たに生まれた重複ではない。将来的に一本化する場合は、
どちらか一方の経路を正とし、他方を削除する判断が必要になる
（現時点ではどちらも独立して動作するため、実質的に同じ情報が
2回通知される可能性がある。Step7以降の検討課題）。

【他モジュールとの依存関係】
- core.config       : CHANNEL_ID, OTHER_CHANNEL_ID, ENABLE_LONG_PERIOD, ENABLE_ADVISORY
- core.constants    : LG_COLORS
- core.helpers      : safe_int, safe_float, safe_bool,
                       truncate_embed_description, format_jma_time
- core.audio.AudioMixin : speak_local, play_mp3（多重継承で利用）
"""
import os
import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import traceback
from datetime import datetime
from collections import defaultdict
import logging

from core.config import (
    CHANNEL_ID, OTHER_CHANNEL_ID,
    ENABLE_LONG_PERIOD, ENABLE_ADVISORY,
    SPEECH_QUEUE_MAXSIZE, MP3_QUEUE_MAXSIZE,
)
from core.constants import LG_COLORS
from core.helpers import (
    safe_int, safe_float, safe_bool,
    truncate_embed_description, format_jma_time,
)
from core.audio import AudioMixin

logger = logging.getLogger("QTLBot")


class OtherInfoCog(commands.Cog, AudioMixin):
    """長周期地震動・気象庁その他特別情報を扱う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # -- チャンネル（on_ready で解決） --
        self.channel       = None
        self.other_channel = None

        # -- HTTPセッション（この Cog 専用） --
        self.session: aiohttp.ClientSession | None = None
        self.headers = {"Accept-Encoding": "identity"}

        # -- 長周期地震動の重複排除状態 --
        self.last_long_period_id = None

        # -- 気象庁その他情報の重複排除状態（7日間TTL） --
        self.last_advisory_ids: dict = {}

        # -- 受信統計（!status 用。将来的にSystemCogと統合予定） --
        self._last_recv = {"long_period": None, "quake_advisory": None}
        self._recv_count = {"long_period": 0, "quake_advisory": 0}

        # -- 音声再生（AudioMixin が要求する属性） --
        self.speech_queue = asyncio.PriorityQueue(maxsize=SPEECH_QUEUE_MAXSIZE)
        self.speech_task: asyncio.Task | None = None
        self.mp3_queue = asyncio.Queue(maxsize=MP3_QUEUE_MAXSIZE)
        self.mp3_task: asyncio.Task | None = None
        self.audio_files = {}  # このCogは専用MP3を使わない（speak_localのみ利用）

    # ===============================
    # Cog起動・終了
    # ===============================

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
            connector=aiohttp.TCPConnector(limit=50, ttl_dns_cache=300),
        )
        logger.info("OtherInfoCog: aiohttp セッションを作成しました")

    async def cog_unload(self):
        for loop_task in (self.fetch_long_period, self.fetch_quake_advisory):
            if loop_task.is_running():
                loop_task.cancel()

        for bg_task in (self.speech_task, self.mp3_task):
            if bg_task and not bg_task.done():
                bg_task.cancel()

        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("OtherInfoCog: aiohttp セッションを閉じました")

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel       = self.bot.get_channel(CHANNEL_ID)
        self.other_channel = self.bot.get_channel(OTHER_CHANNEL_ID) or self.channel

        if not self.fetch_long_period.is_running():
            self.fetch_long_period.start()

        if not self.fetch_quake_advisory.is_running():
            self.fetch_quake_advisory.start()

        if self.speech_task is None or self.speech_task.done():
            self.speech_task = self.bot.loop.create_task(self.speech_worker())

        if self.mp3_task is None or self.mp3_task.done():
            self.mp3_task = self.bot.loop.create_task(self.mp3_worker())

        logger.info("OtherInfoCog: on_ready 完了")

    # ===============================
    # ポーリング・通知
    # ===============================

    @tasks.loop(seconds=60)
    async def fetch_long_period(self):
        try:
            async with self.session.get(
                "https://www.jma.go.jp/bosai/ltpgm/data/list.json",
                ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                if not data:
                    return

                latest = data[0]
                event_id = latest.get("eid")

                if self.last_long_period_id is None:
                    self.last_long_period_id = event_id
                    await self.notify_long_period(latest, extra_note="（ボット起動時の最新情報）")
                    return

                if self.last_long_period_id != event_id:
                    self.last_long_period_id = event_id
                    await self.notify_long_period(latest)

        except Exception:
            logger.error(f"Fetch Long-Period エラー:\n{traceback.format_exc()}")


    @fetch_long_period.before_loop
    async def before_fetch_long_period(self):
        """セッション初期化完了まで待機"""
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def fetch_quake_advisory(self):
        try:
            async with self.session.get(
                "https://www.jma.go.jp/bosai/quake/data/list.json",
                ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                if not data or not isinstance(data, list):
                    return

                ADVISORY_TTL = 7 * 24 * 3600
                now_ts = datetime.now().timestamp()
                self.last_advisory_ids = {
                    eid: ts for eid, ts in self.last_advisory_ids.items()
                    if now_ts - ts < ADVISORY_TTL
                }

                for item in data:
                    ttl = item.get("ttl", "")
                    event_id = item.get("eid") or item.get("ctt") or str(hash(str(item)))

                    if not any(keyword in ttl for keyword in [
                        "北海道・三陸沖後発地震注意情報",
                        "南海トラフ地震臨時情報",
                        "顕著な地震の震源要素更新のお知らせ"
                    ]):
                        continue

                    if event_id in self.last_advisory_ids:
                        continue

                    self.last_advisory_ids[event_id] = now_ts

                    json_filename = item.get("json")
                    if json_filename:
                        detail_url = f"https://www.jma.go.jp/bosai/quake/data/{json_filename}"
                        async with self.session.get(detail_url, timeout=aiohttp.ClientTimeout(total=25)) as detail_resp:
                            if detail_resp.status == 200:
                                detail = await detail_resp.json()
                                await self.notify_quake_advisory(list_item=item, detail_data=detail)
                            else:
                                await self.notify_quake_advisory(list_item=item)
                    else:
                        await self.notify_quake_advisory(list_item=item)

        except Exception as e:
            logger.error(f"Fetch Quake Advisory エラー:\n{traceback.format_exc()}")



    @fetch_quake_advisory.before_loop
    async def before_fetch_quake_advisory(self):
        """セッション初期化完了まで待機"""
        await self.bot.wait_until_ready()


    async def notify_long_period(self, list_item, is_test=False, extra_note=None):
        if not ENABLE_LONG_PERIOD and not is_test:
            return
        channel = self.other_channel or self.channel
        if not channel:
            return

        try:
            json_filename = list_item.get("json")
            if not json_filename:
                return

            detail_url = f"https://www.jma.go.jp/bosai/ltpgm/data/{json_filename}"
            async with self.session.get(detail_url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status != 200:
                    return
                detail = await resp.json()

            source = detail.get("Control", {}).get("PublishingOffice", "気象庁")

            body = detail.get("Body", {})
            eq = body.get("Earthquake", {})
            intensity = body.get("Intensity", {}).get("Observation", {})

            origin_time = format_jma_time(eq.get("OriginTime", "不明"))

            hypo_name = eq.get("Hypocenter", {}).get("Area", {}).get("Name", "不明")
            magnitude = eq.get("Magnitude", "不明")
            max_lg = str(intensity.get("MaxLgInt", "不明"))

            depth_str = "不明"
            coord = eq.get("Hypocenter", {}).get("Area", {}).get("Coordinate", "")
            if coord and '-' in coord:
                try:
                    depth_m = int(coord.split('-')[-1].split('/')[0])
                    depth_km = abs(depth_m) // 1000
                    if depth_km > 0:
                        depth_str = f"{depth_km}km"
                except Exception:
                    pass

            lg_groups = defaultdict(list)
            for pref in intensity.get("Pref", []):
                for area in pref.get("Area", []):
                    area_name = area.get("Name", "")
                    lg_int = area.get("MaxLgInt", "不明")
                    if lg_int != "不明" and area_name:
                        lg_groups[lg_int].append(area_name)

            description = (
                f"**発表機関： {source}**\n"
                f"**発生時刻： {origin_time}**\n"
                f"**震源地： {hypo_name}**\n"
                f"**最大長周期地震動階級： {max_lg}**\n"
                f"**マグニチュード： M{magnitude}**\n"
                f"**深さ： 約{depth_str}**\n"
            )

            if lg_groups:
                description += "\n**各地の長周期地震動階級**"
                for lg in sorted(lg_groups.keys(), reverse=True):
                    description += f"\n■ 階級{lg}"
                    for area in sorted(lg_groups[lg]):
                        description += f"\n　{area}"

            color = LG_COLORS.get(max_lg, 0x62626B)

            embed = discord.Embed(
                title="長周期地震動に関する観測情報",
                description=description,
                color=color,
                timestamp=datetime.now()
            )

            footer = extra_note or ""
            if is_test:
                if footer:
                    footer += " | "
                footer += "※これはテスト通知です。"
            if footer:
                embed.set_footer(text=footer)

            await channel.send(embed=embed)

            # 読み上げ用に時刻の「H時MM分頃」部分を抽出
            time_speak = ""
            if origin_time and "時" in origin_time and "分頃" in origin_time:
                try:
                    time_speak = origin_time[origin_time.index("日") + 1:] + "に"
                except Exception:
                    pass

            speak_text = (
                f"長周期地震動に関する観測情報。"
                f"{time_speak}発生した"
                F"{hypo_name}を震源とする"
                f" マグニチュード{magnitude}の地震により、"
                f"最大長周期地震動階級{max_lg}を観測しました。"
            )
            await self.speak_local(speak_text)

        except Exception as e:
            logger.error(f"notify_long_period エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")

    async def notify_quake_advisory(self, list_item=None, is_test=False, extra_note=None, detail_data=None):
        if not ENABLE_ADVISORY and not is_test:
            return
        channel = self.other_channel or self.channel
        if not channel:
            return

        try:
            if detail_data is None:
                json_filename = list_item.get("json")
                if not json_filename:
                    return
                detail_url = f"https://www.jma.go.jp/bosai/quake/data/{json_filename}"
                async with self.session.get(detail_url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status != 200:
                        return
                    detail = await resp.json()
            else:
                detail = detail_data

            source = detail.get("Control", {}).get("PublishingOffice", "気象庁")
            head = detail.get("Head", {})
            body = detail.get("Body", {})
            title_text = head.get("Title", "特別情報")
            if "顕著な地震の震源要素更新のお知らせ" in title_text:
                eq = body.get("Earthquake", {})
                origin_time = format_jma_time(eq.get("OriginTime", "不明"))

                hypo_name = eq.get("Hypocenter", {}).get("Area", {}).get("Name", "不明")
                magnitude = eq.get("Magnitude", "不明")

                depth_str = "不明"
                coord = eq.get("Hypocenter", {}).get("Area", {}).get("Coordinate_WGS", "")
                if coord and '-' in coord:
                    try:
                        depth_m = int(coord.split('-')[-1].split('/')[0])
                        depth_km = abs(depth_m) // 1000
                        if depth_km > 0:
                            depth_str = f"{depth_km}km"
                    except Exception:
                        pass

                comment = body.get("Comments", {}).get("FreeFormComment", "")

                description = (
                    f"**発表機関： {source}**\n"
                    f"**発生時刻： {origin_time}**\n"
                    f"**震源地： {hypo_name}**\n"
                    f"**マグニチュード： M{magnitude}**\n"
                    f"**深さ： {depth_str}**\n\n"
                    f"{comment}"
                )

                embed = discord.Embed(
                    title=f"{title_text}",
                    description=description,
                    color=0xFF4500,
                    timestamp=datetime.now()
                )

            else:
                text = body.get("EarthquakeInfo", {}).get("Text", "詳細情報なし")
                embed = discord.Embed(
                    title=f"{title_text}",
                    description=f"**発表機関： {source}**\n\n{text}",
                    color=0xFF4500,
                    timestamp=datetime.now()
                )
            footer_parts = []
            base_dir = os.path.dirname(os.path.abspath(__file__))
            image_file = None
            image_filename = None

            if "北海道・三陸沖後発地震注意情報" in title_text:
                image_filename = "hokkaido_bosaitaiou_area.png"
            elif "南海トラフ地震臨時情報" in title_text:
                image_filename = "nankai_bosaitaiou_area.png"

            if image_filename:
                image_path = os.path.join(base_dir, image_filename)
                if os.path.exists(image_path):
                    image_file = discord.File(image_path, filename=image_filename)
                    embed.set_image(url=f"attachment://{image_filename}")
                else:
                    footer_parts.append("地図画像が見つかりません（同じフォルダに置いてください）")

            if extra_note:
                footer_parts.append(extra_note)
            if is_test:
                footer_parts.append("※これはテスト通知です。")
            if footer_parts:
                embed.set_footer(text=" | ".join(footer_parts))
            if image_file:
                await channel.send(embed=embed, file=image_file)
            else:
                await channel.send(embed=embed)

            speak_text = f"{title_text} が発表されました"
            if "顕著な地震の震源要素更新のお知らせ" in title_text:
                # origin_time は「YYYY年M月D日H時MM分頃」形式なので「H時MM分頃」部分を抽出
                time_speak = "先程"
                if origin_time and "日" in origin_time and "時" in origin_time:
                    try:
                        time_speak = origin_time[origin_time.index("日") + 1:].replace("頃", "")
                        # 例: "8時20分" → "8時20分"
                    except Exception:
                        pass
                speak_text = (
                    f"{time_speak}頃の地震について、震源要素を更新しました。"
                    f" 震源地 {hypo_name}"
                    f" マグニチュード M{magnitude}"
                    f" 深さ {depth_str}"
                )
            await self.speak_local(speak_text)

        except Exception as e:
            logger.error(f"notify_quake_advisory エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")
