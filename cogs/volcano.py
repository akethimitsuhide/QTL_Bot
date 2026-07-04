"""
cogs/volcano.py
================
火山情報関連の通知を扱う Cog。

【この Cog が担当する機能】
- JMA 火山情報 API のポーリング（1分間隔）・差分検知・通知
- 噴火速報（eruption.json）のポーリング（1分間隔）・通知
- 噴火警報（warning.json）のポーリング（1分間隔）・通知

【他モジュールとの依存関係】
- core.config       : CHANNEL_ID, VOLCANO_CHANNEL_ID
- core.helpers      : safe_int, safe_float, safe_bool,
                       truncate_embed_description, format_jma_time
- core.audio.AudioMixin : speak_local, play_mp3（多重継承で利用。
                          火山情報は現状MP3を使わないが、将来の拡張に備えて継承しておく）

【Step3 時点の設計メモ】
- volcano/eruption/warning の3種類はそれぞれ独立した1分間隔ポーラーとして
  on_ready 内で起動時刻をずらして開始する（5秒・8秒・11秒後）。
  これは3つのAPIへの同時アクセスを避けるための工夫で、元のbot.py設計を踏襲。
- 差分検知ロジックは3種類とも「前回の eventId/json と比較」というシンプルな
  パターンだが、まだ core/ 側の共通ヘルパーには切り出していない
  （Step4以降、usgs.py 切り出し時に類似パターンが増えたら検討）。
"""
import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import traceback
from datetime import datetime
import logging

from core.config import (
    CHANNEL_ID, VOLCANO_CHANNEL_ID,
    SPEECH_QUEUE_MAXSIZE, MP3_QUEUE_MAXSIZE,
)
from core.helpers import (
    safe_int, safe_float, safe_bool,
    truncate_embed_description, format_jma_time,
)
from core.audio import AudioMixin

logger = logging.getLogger("QTLBot")


class VolcanoCog(commands.Cog, AudioMixin):
    """火山情報（火山情報・噴火速報・噴火警報）を扱う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # -- チャンネル（on_ready で解決） --
        self.channel        = None
        self.volcano_channel = None

        # -- HTTPセッション（この Cog 専用） --
        self.session = None
        self.headers = {"Accept-Encoding": "identity"}

        # -- 火山情報の差分検知状態 --
        self._last_volcano_event_id = None
        self._last_volcano_info_map: dict = {}
        self.volcano_task = None
        self._last_volcano_recv_time = None
        self._volcano_recv_count = 0

        # -- 噴火速報の差分検知状態 --
        self.eruption_task = None
        self._last_eruption_id = None
        self._last_eruption_recv_time = None
        self._eruption_recv_count = 0

        # -- 噴火警報の差分検知状態 --
        self.warning_task = None
        self._last_warning_id = None
        self._last_warning_recv_time = None
        self._warning_recv_count = 0

        # -- 受信統計（!status 用。将来的にSystemCogと統合予定） --
        self._last_recv = {"volcano": None, "eruption": None, "warning": None}
        self._recv_count = {"volcano": 0, "eruption": 0, "warning": 0}

        # -- 音声再生（AudioMixin が要求する属性） --
        self.speech_queue = asyncio.PriorityQueue(maxsize=SPEECH_QUEUE_MAXSIZE)
        self.speech_task = None
        self.mp3_queue = asyncio.Queue(maxsize=MP3_QUEUE_MAXSIZE)
        self.mp3_task = None
        self.audio_files = {}  # 火山情報は現状専用MP3を使わない

    # ===============================
    # Cog起動・終了
    # ===============================

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
            connector=aiohttp.TCPConnector(limit=50, ttl_dns_cache=300),
        )
        logger.info("VolcanoCog: aiohttp セッションを作成しました")

    async def cog_unload(self):
        for bg_task in (self.volcano_task, self.eruption_task, self.warning_task,
                        self.speech_task, self.mp3_task):
            if bg_task and not bg_task.done():
                bg_task.cancel()

        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("VolcanoCog: aiohttp セッションを閉じました")

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel         = self.bot.get_channel(CHANNEL_ID)
        self.volcano_channel = self.bot.get_channel(VOLCANO_CHANNEL_ID) or self.channel

        # 火山情報ポーリング開始（1分ごと）
        if self.volcano_task is None or self.volcano_task.done():
            async def volcano_poller():
                await asyncio.sleep(5)  # 起動後5秒待機
                while not self.bot.is_closed():
                    try:
                        await self.fetch_volcano_info()
                    except Exception as e:
                        logger.error(f"Volcano poller error: {e}")
                    await asyncio.sleep(60)  # 1分ごと

            self.volcano_task = self.bot.loop.create_task(volcano_poller())
            logger.info("Volcano polling started (every 1 minute)")

        # 噴火速報ポーリング開始（1分ごと）
        if self.eruption_task is None or self.eruption_task.done():
            async def eruption_poller():
                await asyncio.sleep(8)  # 起動後8秒待機
                while not self.bot.is_closed():
                    try:
                        await self.fetch_eruption_info()
                    except Exception as e:
                        logger.error(f"Eruption poller error: {e}")
                    await asyncio.sleep(60)

            self.eruption_task = self.bot.loop.create_task(eruption_poller())
            logger.info("Eruption polling started (every 1 minute)")

        # 噴火警報ポーリング開始（1分ごと）
        if self.warning_task is None or self.warning_task.done():
            async def warning_poller():
                await asyncio.sleep(11)  # 起動後11秒待機
                while not self.bot.is_closed():
                    try:
                        await self.fetch_warning_info()
                    except Exception as e:
                        logger.error(f"Warning poller error: {e}")
                    await asyncio.sleep(60)

            self.warning_task = self.bot.loop.create_task(warning_poller())
            logger.info("Warning polling started (every 1 minute)")

        if self.speech_task is None or self.speech_task.done():
            self.speech_task = self.bot.loop.create_task(self.speech_worker())

        if self.mp3_task is None or self.mp3_task.done():
            self.mp3_task = self.bot.loop.create_task(self.mp3_worker())

        logger.info("VolcanoCog: on_ready 完了")

    # ===============================
    # ポーリング・通知
    # ===============================

    # ===============================
    # 火山情報通知
    # ===============================

    async def fetch_volcano_info(self):
        """
        JMA 火山情報を取得・通知する。

        【処理手順】
        ① info.json をフェッチし、全オブジェクトの eventId と reportDatetime を取得
        ② 前回取得と比較し、eventId が新規、または reportDatetime が更新された
           オブジェクトを抽出する
        ③ 各対象で info/{eventId}.json をフェッチして Discord に通知
        ④ 次回ループのために現在の info.json を保存

        差分検知キー: _last_volcano_info_map（前回の info.json 全体を
        {eventId: item} の dict で保持。item には reportDatetime を含む）

        【設計変更の経緯】
        当初は eventId の新規追加のみを検知トリガーにしていたが、
        同一火山・同一警戒レベルのまま状況説明のみ更新される
        （reportDatetime だけが進み eventId は変わらない）ケースで
        通知が来ない問題があった。reportDatetime の変化も検知対象に加えることで、
        「新規の火山情報」と「既存情報の更新」の両方を確実に捕捉する。
        """
        HEADERS = {"User-Agent": "QTLBot/1.0 (Discord earthquake bot; contact via GitHub)"}

        try:
            # Step①: info.json をフェッチ
            async with self.session.get(
                "https://www.jma.go.jp/bosai/volcano/data/info.json",
                timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
                headers=HEADERS,
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"Volcano list fetch failed: HTTP {resp.status}")
                    return

                info_list: list[dict] = await resp.json(content_type=None)
                if not info_list:
                    logger.debug("Volcano: info.json is empty")
                    return

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.debug(f"Volcano info.json fetch error: {type(e).__name__}")
            return
        except Exception as e:
            logger.error(f"Volcano info.json fetch unexpected error: {e}")
            return

        # Step②: 前回リストと比較して「新規 eventId」または
        #         「reportDatetime が変化した既存 eventId」を抽出
        # 初回は先頭1件のみ通知し、2回目以降は差分をすべて通知
        prev: dict[str, dict] = getattr(self, "_last_volcano_info_map", {})
        curr: dict[str, dict] = {item["eventId"]: item for item in info_list if item.get("eventId")}

        if not prev:
            # 初回起動: 通知はせず現在のリストを記録するだけ（起動時のうるさい通知を防ぐ）
            logger.info(f"Volcano: 初回起動 現在の情報を記録（通知はしない） 件数={len(curr)}")
            self._last_volcano_info_map = curr
            return
        else:
            # 2回目以降: 新規 eventId、または reportDatetime が更新された eventId を対象にする
            target_ids = []
            for eid, item in curr.items():
                if eid not in prev:
                    target_ids.append(eid)
                    continue
                prev_report_dt = prev[eid].get("reportDatetime")
                curr_report_dt = item.get("reportDatetime")
                if curr_report_dt and curr_report_dt != prev_report_dt:
                    target_ids.append(eid)

            if not target_ids:
                logger.debug("Volcano: no change")
                self._last_volcano_info_map = curr
                return
            logger.info(f"Volcano: {len(target_ids)}件の新規/更新を検知 {target_ids}")

        # Step③: 各 eventId で詳細を取得して通知
        for event_id in target_ids:
            try:
                detail_url = f"https://www.jma.go.jp/bosai/volcano/data/info/{event_id}.json"
                async with self.session.get(
                    detail_url,
                    timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
                    headers=HEADERS,
                ) as detail_resp:
                    if detail_resp.status != 200:
                        logger.warning(f"Volcano detail fetch failed: HTTP {detail_resp.status} eventId={event_id}")
                        continue

                    detail: dict = await detail_resp.json(content_type=None)

                await self._notify_volcano(detail, event_id)

                # 受信統計を更新
                self._last_recv["volcano"] = datetime.now()
                self._recv_count["volcano"] = self._recv_count.get("volcano", 0) + 1
                self._last_volcano_recv_time = datetime.now()
                self._volcano_recv_count += 1
                self._last_volcano_event_id = event_id  # status 表示用（最後に処理した ID）

            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.warning(f"Volcano detail fetch error: {type(e).__name__} eventId={event_id}")
            except Exception as e:
                logger.error(f"Volcano detail error: {e} eventId={event_id}", exc_info=True)

            # JMAサーバーへの負荷軽減: 複数件ある場合は間隔を空ける
            if len(target_ids) > 1:
                await asyncio.sleep(2)

        # Step④: 次回ループのために現在のリストを保存
        self._last_volcano_info_map = curr

    async def _notify_volcano(self, detail: dict, event_id: str) -> None:
        """
        火山情報 detail JSON から Discord Embed を作成して送信する。

        使用フィールド:
          headTitle       : Embed タイトル
          reportDatetime  : 発表日時（JST ISO 形式）
          volcanoHeadline : 概要
          volcanoActivity : 詳細
          volcanoPrevention: 防災上の注意
        """
        channel = self.volcano_channel or self.channel
        if not channel:
            logger.warning("Volcano: 通知チャンネルが見つかりません")
            return

        try:
            head_title        = detail.get("headTitle", "火山情報")
            report_datetime   = detail.get("reportDatetime", "")
            volcano_headline  = (detail.get("volcanoHeadline") or "").strip()
            volcano_activity  = (detail.get("volcanoActivity") or "").strip()
            volcano_prevention = (detail.get("volcanoPrevention") or "").strip()

            # 発表日時フォーマット
            formatted_time = report_datetime  # フォールバック: そのまま
            if report_datetime:
                try:
                    # ISO 形式 "2026-06-15T12:00:00+09:00" → "2026年6月15日12時00分"
                    dt = datetime.fromisoformat(report_datetime)
                    formatted_time = (
                        f"{dt.year}年{dt.month}月{dt.day}日"
                        f"{dt.hour}時{dt.minute:02d}分"
                    )
                except Exception:
                    pass

            # 警戒レベルを抽出して色を決定
            alert_code = "00"
            volcano_infos = detail.get("volcanoInfos") or []
            if volcano_infos:
                items = volcano_infos[0].get("items") or []
                if items:
                    alert_code = items[0].get("code", "00")

            COLOR_MAP = {
                "01": 0x9932CC,  # 紫  L1
                "02": 0xFF0000,  # 赤  L2
                "03": 0xFF6600,  # 橙  L3
                "04": 0xFFD700,  # 黄  L4
                "05": 0x0000FF,  # 青  L5
            }
            embed_color = COLOR_MAP.get(alert_code, 0x808080)

            # Embed 組み立て
            embed = discord.Embed(
                title=head_title or "火山情報",
                description=volcano_headline or "火山情報が発表されました。",
                color=embed_color,
                timestamp=datetime.now(),
            )

            # 発表日時
            if formatted_time:
                embed.add_field(name="発表日時", value=formatted_time, inline=True)

            # 詳細（volcanoActivity）
            if volcano_activity:
                # Discord フィールド上限 1024文字
                if len(volcano_activity) > 1020:
                    volcano_activity = volcano_activity[:1020] + "…"
                embed.add_field(name="詳細", value=volcano_activity, inline=False)

            # 防災上の注意（volcanoPrevention）
            if volcano_prevention:
                if len(volcano_prevention) > 1020:
                    volcano_prevention = volcano_prevention[:1020] + "…"
                embed.add_field(name="防災上の注意", value=volcano_prevention, inline=False)

            embed.set_footer(text=f"気象庁 | eventId: {event_id}")

            await channel.send(embed=embed)
            logger.info(f"Volcano 通知完了: {head_title} eventId={event_id}")

            # 読み上げ（警戒レベル L1〜L3 のみ）
            if alert_code in ("01", "02", "03"):
                level = {"01": "1", "02": "2", "03": "3"}[alert_code]
                speak_text = f"火山情報。{head_title}。警戒レベル{level}。"
                await self.speak_local(speak_text, priority=1)

        except Exception as e:
            logger.error(f"_notify_volcano エラー: {e}", exc_info=True)

    # ===============================
    # 噴火速報ポーリング (VFVO50)
    # ===============================

    async def fetch_eruption_info(self) -> None:
        """
        JMA volcano API から噴火速報を取得・通知する。
        URL: https://www.jma.go.jp/bosai/volcano/data/eruption.json
        リストは昇順（末尾が最新）。アイテム自体にデータが含まれ詳細URLは不要。
        構造: [{controlTitle, headTitle, reportDatetime, eventId, infoType, eruptionAreas}]
        """
        HEADERS = {"User-Agent": "QTLBot/1.0 (Discord earthquake bot; contact via GitHub)"}
        LIST_URL = "https://www.jma.go.jp/bosai/volcano/data/eruption.json"

        try:
            async with self.session.get(
                LIST_URL,
                timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
                headers=HEADERS,
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"Eruption fetch failed: HTTP {resp.status}")
                    return
                items: list[dict] = await resp.json(content_type=None)

            if not items:
                return

            # リストは昇順 → 末尾が最新
            latest = items[-1]
            event_id = str(latest.get("eventId", ""))
            if not event_id:
                return

            if self._last_eruption_id is None:
                self._last_eruption_id = event_id
                logger.info(f"Eruption: 初回起動スキップ eventId={event_id}")
                return
            if self._last_eruption_id == event_id:
                return

            self._last_eruption_id = event_id
            await self._notify_eruption(latest, event_id)
            self._last_recv["eruption"] = datetime.now()
            self._recv_count["eruption"] = self._recv_count.get("eruption", 0) + 1
            self._last_eruption_recv_time = datetime.now()
            self._eruption_recv_count += 1

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.debug(f"Eruption fetch error: {type(e).__name__}")
        except Exception as e:
            logger.error(f"fetch_eruption_info エラー: {e}", exc_info=True)

    async def _notify_eruption(self, item: dict, event_id: str) -> None:
        """噴火速報を Discord に通知する。
        item: eruption.json の1エントリ
        """
        channel = self.volcano_channel or self.channel
        if not channel:
            logger.warning("Eruption: 通知チャンネルが見つかりません")
            return
        try:
            head_title   = item.get("headTitle", "噴火速報")
            report_dt    = format_jma_time(item.get("reportDatetime", ""))
            info_type    = item.get("infoType", "")

            # headTitle 例: "火山名　諏訪之瀬島　噴火速報"
            # 中央の部分を火山名として取得
            parts = [p.strip() for p in head_title.split("\u3000") if p.strip()]
            # ["火山名", "諏訪之瀬島", "噴火速報"] のような構造
            if len(parts) >= 3:
                volcano_name = parts[1]
                title = parts[-1]  # "噴火速報"
            elif len(parts) == 2:
                volcano_name = parts[0]
                title = parts[1]
            else:
                volcano_name = ""
                title = head_title

            description = f"**発表日時:** {report_dt}\n"
            if volcano_name:
                description += f"**火山名:** {volcano_name}\n"
            if info_type:
                description += f"**情報種別:** {info_type}\n"

            embed = discord.Embed(
                title=head_title,
                description=description,
                color=0xFF4500,
                timestamp=datetime.now(),
            )
            embed.set_footer(text=f"気象庁 噴火速報 | eventId: {event_id}")
            await channel.send(embed=embed)
            logger.info(f"噴火速報通知完了: {head_title} eventId={event_id}")

            speak_text = f"噴火速報。{volcano_name or title}で噴火が発生しました。"
            await self.speak_local(speak_text, priority=0)

        except Exception as e:
            logger.error(f"_notify_eruption エラー: {e}", exc_info=True)

    # ===============================
    # 噴火警報ポーリング
    # ===============================

    async def fetch_warning_info(self) -> None:
        """
        JMA volcano API から噴火警報を取得・通知する。
        URL: https://www.jma.go.jp/bosai/volcano/data/warning.json
        リストは昇順（末尾が最新）。アイテム自体にデータが含まれ詳細URLは不要。
        構造: [{reportDatetime, eventId, areas, volcanoInfos:[{type, items:[{name,code,lastCode,condition,areas}]}]}]
        """
        HEADERS = {"User-Agent": "QTLBot/1.0 (Discord earthquake bot; contact via GitHub)"}
        LIST_URL = "https://www.jma.go.jp/bosai/volcano/data/warning.json"

        try:
            async with self.session.get(
                LIST_URL,
                timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
                headers=HEADERS,
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"Warning fetch failed: HTTP {resp.status}")
                    return
                items: list[dict] = await resp.json(content_type=None)

            if not items:
                return

            # リストは昇順 → 末尾が最新
            latest = items[-1]
            event_id = str(latest.get("eventId", ""))
            if not event_id:
                return

            if self._last_warning_id is None:
                self._last_warning_id = event_id
                logger.info(f"Warning: 初回起動スキップ eventId={event_id}")
                return
            if self._last_warning_id == event_id:
                return

            self._last_warning_id = event_id
            await self._notify_warning(latest, event_id)
            self._last_recv["warning"] = datetime.now()
            self._recv_count["warning"] = self._recv_count.get("warning", 0) + 1
            self._last_warning_recv_time = datetime.now()
            self._warning_recv_count += 1

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.debug(f"Warning fetch error: {type(e).__name__}")
        except Exception as e:
            logger.error(f"fetch_warning_info エラー: {e}", exc_info=True)

    async def _notify_warning(self, item: dict, event_id: str) -> None:
        """噴火警報を Discord に通知する。item は warning.json の1エントリ。"""
        channel = self.volcano_channel or self.channel
        if not channel:
            logger.warning("Warning: 通知チャンネルが見つかりません")
            return
        try:
            report_dt     = format_jma_time(item.get("reportDatetime", ""))
            volcano_infos = item.get("volcanoInfos", [])

            # 対象火山の警報種別・火山名を取得
            volcano_name = ""
            warn_name    = ""
            condition    = ""

            for vi in volcano_infos:
                vi_type  = vi.get("type", "")
                vi_items = vi.get("items", [])
                if not vi_items:
                    continue
                it0 = vi_items[0]
                if "対象火山" in vi_type:
                    warn_name = it0.get("name", "")
                    condition = it0.get("condition", "")
                    areas0    = it0.get("areas", [])
                    if areas0:
                        volcano_name = areas0[0].get("name", "")
                    break

            # 色: condition で決定
            CONDITION_COLOR = {
                "引上げ": 0xFF0000,
                "発表":   0xFF4500,
                "継続":   0xFFA500,
                "引下げ": 0x00AA00,
            }
            embed_color = CONDITION_COLOR.get(condition, 0xFF6600)

            title = f"{volcano_name} 噴火警報・予報" if volcano_name else "噴火警報・予報"

            description = f"**発表日時:** {report_dt}\n"
            if volcano_name:
                description += f"**火山名:** {volcano_name}\n"
            if warn_name:
                cond_str = f"（{condition}）" if condition else ""
                description += f"**警報種別:** {warn_name}{cond_str}\n"

            # 対象市町村
            muni_lines = []
            for vi in volcano_infos:
                if "対象市町村" in vi.get("type", "") and "防災対応" not in vi.get("type", ""):
                    for it in vi.get("items", []):
                        for a in it.get("areas", []):
                            muni_lines.append(f"　{a.get('name', '')}")
            if muni_lines:
                description += "**対象市町村:**\n" + "\n".join(muni_lines) + "\n"

            # 防災対応
            bousai_lines = []
            for vi in volcano_infos:
                if "防災対応" in vi.get("type", ""):
                    for it in vi.get("items", []):
                        it_name = it.get("name", "")
                        it_cond = it.get("condition", "")
                        if it_name:
                            cond_str = f"（{it_cond}）" if it_cond else ""
                            bousai_lines.append(f"　{it_name}{cond_str}")
            if bousai_lines:
                description += "**防災対応:**\n" + "\n".join(bousai_lines) + "\n"

            embed = discord.Embed(
                title=title,
                description=description,
                color=embed_color,
                timestamp=datetime.now(),
            )
            embed.set_footer(text=f"気象庁 噴火警報 | eventId: {event_id}")
            await channel.send(embed=embed)
            logger.info(f"噴火警報通知完了: {title} eventId={event_id}")

            speak_text = f"噴火警報。{volcano_name or '火山'}で{warn_name}が{condition or '発表'}されました。"
            await self.speak_local(speak_text, priority=0)

        except Exception as e:
            logger.error(f"_notify_warning エラー: {e}", exc_info=True)