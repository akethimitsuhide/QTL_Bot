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
- notify_hypocenter_update() / notify_nankai_trough()
  （2026-09-01、cogs/tsunami.pyから移設。下記【重複について】参照）

【Step6 時点の設計メモ、2026-09-01 追記: tsunami.py との重複に見える点について】
気象庁は「南海トラフ地震臨時情報」「顕著な地震の震源要素更新のお知らせ」を
実は2つの異なるAPIエンドポイントに重複して掲載している:
  - bosai/tsunami/data/list.json 経由 → フェッチ処理は cogs/tsunami.py の
    fetch_tsunami_observation に残したまま、通知処理のみ本Cogの
    notify_nankai_trough() / notify_hypocenter_update() へ委譲される
    （2026-09-01移設。津波固有の処理を含まず地震情報の一種であるため、
    津波専用のTsunamiCogよりも本Cogに置く方が実態に合っていると判断した。
    送信先も notify_quake_advisory と揃えて OTHER_CHANNEL_ID 系に統一）
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
from core.helpers import format_jma_time, truncate_embed_description
from core.audio import AudioMixin
from core.notification_log import record_notification
from core.delivery_stats import record_delivery

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
        self._quake_advisory_initialized = False  # 初回ポーリングでは通知せずIDのみ記録

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
                    logger.info(f"fetch_long_period: 起動時の既存最新情報を記録（通知はしない） eid={event_id}")
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

                    # 初回ポーリングは「起動前から存在した情報」の可能性が高いため通知しない。
                    # IDだけ記録し、次回以降の本当の新規発生時のみ通知する。
                    if not self._quake_advisory_initialized:
                        logger.info(f"fetch_quake_advisory: 起動時の既存情報を記録（通知はしない） eid={event_id}")
                        continue

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

                self._quake_advisory_initialized = True

        except Exception:
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
            if not is_test:
                record_delivery(True, "長周期地震動")
                record_notification("長周期地震動", "長周期地震動に関する観測情報", hypo_name)

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
            record_delivery(False, "長周期地震動", str(e))
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
            # 【2026-09-01 修正】以前は os.path.dirname(os.path.abspath(__file__))
            # （1階層上）を使っており、これは cogs/other.py 自身が置かれている
            # cogs/ ディレクトリを指してしまっていた。北海道・三陸沖後発地震
            # 注意情報／南海トラフ地震臨時情報の地図画像（hokkaido_bosaitaiou_
            # area.png / nankai_bosaitaiou_area.png）は、README・.env.example
            # の案内通りプロジェクトルート（bot.pyと同じディレクトリ）に配置
            # する運用のため、画像がその通りに配置されていても cogs/ 配下を
            # 探してしまい常に「見つからない」扱いになっていた（実機テストで
            # 発見）。core/audio.py の MP3ファイルパス解決
            # （os.path.dirname を2回適用してプロジェクトルートを得るパターン）
            # と同じ方式に修正し、cogs/ からさらに1階層上（プロジェクトルート）
            # を指すようにする。
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
            if not is_test:
                record_delivery(True, "気象庁その他")
                record_notification("気象庁その他", title_text)

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
            record_delivery(False, "気象庁その他", str(e))
            logger.error(f"notify_quake_advisory エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")

    # ===============================
    # 顕著な地震の震源要素更新のお知らせ／南海トラフ地震関連情報
    # （2026-09-01: cogs/tsunami.py から移設）
    # ===============================
    # 【移設の経緯】
    # これら2つの情報種別は、気象庁の tsunami/data/list.json と
    # quake/data/list.json の両方に重複して掲載されており
    # （南海トラフ地震臨時情報／顕著な地震の震源要素更新のお知らせは
    # 意図的に2つのエンドポイントで重複配信される。本クラスの
    # notify_quake_advisory がquakeエンドポイント経由、以下の2メソッド
    # がtsunamiエンドポイント経由）、内容としては「地震情報」の一種
    # であり、津波固有の処理（津波高さ・到達予想時刻等）を一切含まない。
    # そのため、津波情報専用のTsunamiCogよりも、地震のその他特別情報を
    # 扱うOtherInfoCogに置く方が実態に合っていると判断し移設した。
    #
    # 【送信先チャンネルの変更】
    # 移設前（cogs/tsunami.py在籍時）は self.tsunami_channel 宛てだった
    # が、移設後は同じ情報種別を扱う notify_quake_advisory と揃えて
    # self.other_channel 宛てに変更した（ユーザー確認済み、2026-09-01）。
    #
    # 【呼び出し元】
    # 実際の取得・ディスパッチ処理（tsunami/data/list.json のポーリング
    # ループ、ttlによるタイトル判別）は cogs/tsunami.py の
    # fetch_tsunami_observation に残したままにしている（ネットワーク
    # フェッチ自体を重複させない・移動範囲を最小限にするため）。
    # 呼び出し側は self.bot.get_cog("OtherInfoCog") 経由でこれらの
    # メソッドを呼ぶ（cogs/tsunami.py 側のコメント参照）。
    async def notify_hypocenter_update(self, detail: dict, list_item: dict | None = None, is_test: bool = False) -> None:
        """
        顕著な地震の震源要素更新のお知らせ（VXSE61）を通知する。
        津波情報等で使われる精密な震源要素（度単位）が更新されたことを伝える情報。
        """
        channel = self.other_channel or self.channel
        if not channel:
            return
        try:
            ttl   = list_item.get("ttl", "震源要素更新のお知らせ") if list_item else "震源要素更新のお知らせ"
            title = ("【テスト】 " if is_test else "") + ttl
            head  = detail.get("Head", {})
            body  = detail.get("Body", {})
            control = detail.get("Control", {})

            publisher    = control.get("PublishingOffice", "気象庁")
            report_time  = format_jma_time(head.get("ReportDateTime", "不明"))
            target_time  = format_jma_time(head.get("TargetDateTime", ""))
            headline     = head.get("Headline", {}).get("Text", "")

            eq = body.get("Earthquake", {})
            origin_time = format_jma_time(eq.get("OriginTime", "不明"))
            hypo = eq.get("Hypocenter", {})
            hypo_name = hypo.get("Area", {}).get("Name", "不明")
            magnitude = eq.get("Magnitude", "不明")

            free_form = body.get("Comments", {}).get("FreeFormComment", "")

            description = (
                f"**発表機関:** {publisher}\n"
                f"**発表時刻:** {report_time}\n"
            )
            if target_time:
                description += f"**更新時刻:** {target_time}\n"
            description += (
                f"\n**原因地震：** {hypo_name}　M{magnitude}（{origin_time}発生）\n"
            )
            if headline:
                description += f"\n{headline}\n"
            if free_form:
                description += f"\n{free_form}"

            description = truncate_embed_description(description)

            embed = discord.Embed(
                title=title,
                description=description,
                color=0x808080,
                timestamp=datetime.now(),
            )
            if is_test:
                embed.set_footer(text="※これはテスト通知です。")

            await channel.send(embed=embed)
            if not is_test:
                record_delivery(True, "震源要素更新")
                record_notification("震源要素更新", title)
            logger.info(f"震源要素更新通知完了: {title}")

        except Exception as e:
            record_delivery(False, "震源要素更新", str(e))
            logger.error(f"notify_hypocenter_update エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")

    async def notify_nankai_trough(self, detail: dict, list_item: dict | None = None, is_test: bool = False) -> None:
        """
        南海トラフ地震臨時情報・関連解説情報（VYSE50）を通知する。
        """
        channel = self.other_channel or self.channel
        if not channel:
            return
        try:
            ttl   = list_item.get("ttl", "南海トラフ地震に関連する情報") if list_item else "南海トラフ地震に関連する情報"
            head  = detail.get("Head", {})
            body  = detail.get("Body", {})
            control = detail.get("Control", {})

            # Head.Title 例: "南海トラフ地震臨時情報（巨大地震警戒）" / "（調査中）" / "（巨大地震注意）" / "（調査終了）"
            head_title = head.get("Title", ttl)
            title = ("【テスト】 " if is_test else "") + head_title

            publisher   = control.get("PublishingOffice", "気象庁")
            report_time = format_jma_time(head.get("ReportDateTime", "不明"))
            headline    = head.get("Headline", {}).get("Text", "")

            eq_info = body.get("EarthquakeInfo", {})
            info_serial = eq_info.get("InfoSerial", {}).get("Name", "")
            body_text   = eq_info.get("Text", "")
            next_advisory = body.get("NextAdvisory", "")

            # キーワード別の色・緊急度
            KEYWORD_COLOR = {
                "巨大地震警戒": 0xFF0000,
                "巨大地震注意": 0xFFA500,
                "調査中":     0xFFD700,
                "調査終了":   0x808080,
            }
            embed_color = KEYWORD_COLOR.get(info_serial, 0xFFA500)

            description = (
                f"**発表機関:** {publisher}\n"
                f"**発表時刻:** {report_time}\n"
            )
            if info_serial:
                description += f"**情報種別:** {info_serial}\n"
            if headline.strip():
                description += f"\n{headline.strip()}\n"
            if body_text.strip():
                description += f"\n{body_text.strip()}\n"
            if next_advisory.strip():
                description += f"\n**次回発表:** {next_advisory.strip()}"

            description = truncate_embed_description(description)

            embed = discord.Embed(
                title=title,
                description=description,
                color=embed_color,
                timestamp=datetime.now(),
            )
            if is_test:
                embed.set_footer(text="※これはテスト通知です。")

            await channel.send(embed=embed)
            if not is_test:
                record_delivery(True, "南海トラフ")
                record_notification("南海トラフ", title)
            logger.info(f"南海トラフ地震関連情報通知完了: {title}")

            # 読み上げ（巨大地震警戒・注意のみ）
            if info_serial in ("巨大地震警戒", "巨大地震注意"):
                await self.speak_local(f"南海トラフ地震臨時情報。{info_serial}が発表されました。", priority=1)

        except Exception as e:
            record_delivery(False, "南海トラフ", str(e))
            logger.error(f"notify_nankai_trough エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")
