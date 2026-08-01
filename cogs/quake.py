"""
cogs/quake.py
=============
地震情報（震度速報・各地の震度・震源に関する情報等）専用の Cog。

【新設の経緯】
令和8年熊本地震では、緊急地震速報と地震情報がほぼ同時に、かつ短時間に
大量発生した。従来は両方が同じ QuakeEewCog・同じ音声キューを共有して
いたため、片方の情報を読み上げ中にもう片方の重要な情報が来ても、
キューの末尾に積まれて再生が遅れる問題があった。

このCogは P2P地震情報 API のポーリング・地震情報の通知・読み上げ・
MP3再生のみを扱う。EEW（緊急地震速報）は cogs/eew.py の EewCog が
別Cogとして扱う。音声再生は cogs/audio_shared.py の AudioCog に委譲し
（AudioClientMixin 経由）、両Cogが同じ音声キューを共有することで、
優先度に基づいた一貫した順序で再生されるようにする。

【この Cog が担当する機能】
- P2P地震情報 WebSocket（code=551, core.p2p_ws_hub 経由）からの地震情報通知
- P2P CDN画像のリトライ添付

【WebSocket移行について（2026-08 feature/p2p-websocket-migration）】
従来は本Cog自身が /v2/history を3秒間隔でポーリングしていたが、
core.p2p_ws_hub.P2PWebSocketHub が保持する単一WebSocket接続から
code=551 のメッセージを受け取る方式に移行した（handle_p2p_quake）。
ポーリングは廃止したが、Bot起動直後は「起動前から存在していた最新の
地震情報を誤って新着として通知してしまう」のを防ぐため、on_ready 時に
一度だけ /v2/history を参照して last_quake_id を初期化する
（_init_last_quake_id）。

【他モジュールとの依存関係】
- core.config      : 環境変数由来の設定値（チャンネルID・フィルター等）
- core.constants   : INT_MAP, SHINDO_COLORS, QUAKE_TYPE_MAP, TSUNAMI_MAP
- core.helpers     : format_jma_time
- core.audio.AudioClientMixin : speak_local, play_mp3（AudioCog に委譲）
- core.p2p_image.P2PImageMixin : p2p_image_url, _attach_p2p_image（多重継承で利用）
- core.fetch_backoff.FetchBackoff : Circuit Breaker（連続失敗時のバックオフ）
"""
import discord
from discord.ext import commands
import aiohttp
import asyncio
import traceback
import functools
from datetime import datetime
import logging

from core.config import (
    CHANNEL_ID, QUAKE_CHANNEL_ID,
    QUAKE_MIN_SCALE, QUAKE_MIN_MAG, QUAKE_MIN_DEPTH, QUAKE_MAX_DEPTH,
    QUAKE_ENABLE_SCALE_PROMPT, QUAKE_ENABLE_DESTINATION,
    QUAKE_ENABLE_SCALE_AND_DEST, QUAKE_ENABLE_DETAIL_SCALE,
    QUAKE_ENABLE_FOREIGN, QUAKE_ENABLE_OTHER,
)
from core.constants import (
    INT_MAP, SHINDO_COLORS, QUAKE_TYPE_MAP, TSUNAMI_MAP,
    PREFECTURE_MAP, group_points_by_scale, scale_one_level_down,
)
from core.helpers import format_jma_time, format_latlon
from core.audio import AudioClientMixin
from core.p2p_image import P2PImageMixin

logger = logging.getLogger("QTLBot")


class QuakeInfoCog(commands.Cog, AudioClientMixin, P2PImageMixin):
    """地震情報（震度速報・各地の震度等）を扱う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.channel       = None
        self.quake_channel = None

        self.session: aiohttp.ClientSession | None = None
        self.headers = {"Accept-Encoding": "identity"}

        self.last_quake_id = None

        self._last_zencyu_time: datetime | None = None

        self._last_recv: dict[str, datetime | None] = {"quake": None}
        self._recv_count: dict[str, int] = {"quake": 0}

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
            connector=aiohttp.TCPConnector(limit=50, ttl_dns_cache=300),
        )
        logger.info("QuakeInfoCog: aiohttp セッションを作成しました")

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("QuakeInfoCog: aiohttp セッションを閉じました")

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel       = self.bot.get_channel(CHANNEL_ID)
        self.quake_channel = self.bot.get_channel(QUAKE_CHANNEL_ID) or self.channel

        # P2P地震情報（code=551）の受信は core.p2p_ws_hub.P2PWebSocketHub が
        # 一元管理する接続から配信される（bot.py 側で
        # hub.register("quake", self.handle_p2p_quake) により登録される）。
        # ここでは起動前から存在していた最新情報を誤って新着扱いしないよう、
        # last_quake_id のみ一度初期化しておく。
        if self.last_quake_id is None:
            await self._init_last_quake_id()

        logger.info("QuakeInfoCog: on_ready 完了")

    async def _init_last_quake_id(self):
        """
        Bot起動直後、/v2/history を一度だけ参照して last_quake_id を
        初期化する。WebSocket移行前はポーリングループの初回実行時に
        これを行っていたが、ポーリング自体を廃止したため on_ready から
        明示的に1回だけ呼び出す。

        ここで初期化しておかないと、起動前から存在していた最新の地震情報を
        「起動後に届いた新着メッセージ」と誤認して重複通知してしまう
        （WebSocketは接続後、直近の履歴を送ってくることがあるため）。

        失敗しても致命的ではない（次にWebSocket経由で新しい地震情報が
        来た時点で last_quake_id が更新されるだけ）ため、例外はログのみ
        で握りつぶす。
        """
        try:
            async with self.session.get(
                "https://api.p2pquake.net/v2/history?codes=551&limit=1",
            ) as resp:
                if resp.status == 200:
                    data_list = await resp.json()
                    if data_list:
                        self.last_quake_id = data_list[0].get("id")
                        logger.info(
                            f"_init_last_quake_id: 起動時の既存最新情報を記録"
                            f"（通知はしない） id={self.last_quake_id}"
                        )
        except Exception as e:
            logger.warning(f"_init_last_quake_id: 初期化に失敗しました（続行します）: {e}")

    async def handle_p2p_quake(self, data: dict) -> None:
        """
        core.p2p_ws_hub.P2PWebSocketHub から code=551（地震情報）の
        メッセージを受け取るハンドラ。

        【移行前との違い】
        以前は /v2/history を3秒間隔でポーリングしていた（fetch_quake）。
        WebSocket移行により、接続・code フィルタリング・重複排除
        （id ベース）は core.p2p_ws_hub.P2PWebSocketHub が一元的に
        行うようになったため、このメソッドは「551のデータを受け取った
        後の通知処理」のみを担当する。
        """
        try:
            data_id = data.get("id")

            if self.last_quake_id is None:
                self.last_quake_id = data_id
                logger.info(f"handle_p2p_quake: 起動時の既存最新情報を記録（通知はしない） id={data_id}")
                return

            if data_id == self.last_quake_id:
                # ハブ側で既にid単位の重複排除は行われているはずだが、
                # 念のためこの階層でも同一IDの連続処理を防ぐ
                return

            self.last_quake_id = data_id
            self._last_recv["quake"] = datetime.now()
            self._recv_count["quake"] += 1
            logger.info(f"P2P地震情報取得: id={data_id}")
            await self.notify_quake(data)

        except Exception:
            logger.error(f"handle_p2p_quake エラー:\n{traceback.format_exc()}")

    async def notify_quake(self, data, is_test=False, extra_note=None, skip_speech=False):
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
        points = data.get("points", [])
        # 震度別グルーピング（震度速報の都道府県別/各地の震度に関する情報の
        # 観測点別リスト表示、両方で使う共通データ）
        points_by_scale = group_points_by_scale(points)

        if not is_test:
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

        name = hypo.get("name") or "調査中"
        mag = hypo.get("magnitude", -1)
        depth = hypo.get("depth", -1)
        latitude = hypo.get("latitude", -200)
        longitude = hypo.get("longitude", -200)

        mag_str = f"M{mag}" if mag != -1 else "調査中"
        if depth == 0:
            depth_str = "ごく浅い"
        elif depth != -1:
            depth_str = f"約{depth}km"
        else:
            depth_str = "調査中"

        occur_time = format_jma_time(eq.get("time", "不明"))

        name_display = "調査中" if issue_type == "ScalePrompt" else name
        # 「震源に関する情報」（Destination）で地図画像が表示できなかった場合、
        # 震源地の横に緯度経度を付記する（要件2）。他のissue_typeでは付記しない。
        show_latlon_in_name = (
            issue_type == "Destination"
            and latitude != -200 and longitude != -200
        )
        show_intensity = issue_type != "Destination"
        desc_lines = [
            f"**発表機関： {source}**",
            f"**発生時刻： {occur_time}**",
            f"**震源地： {name_display}**",
        ]
        if show_intensity:
            desc_lines.append(f"**最大震度： {max_scale_str}**")
        desc_lines += [
            f"**マグニチュード： {mag_str}**",
            f"**深さ： {depth_str}**",
        ]
        description = "\n".join(desc_lines)

        dom_tsunami = eq.get("domesticTsunami", "None")
        description += f"\n\n{TSUNAMI_MAP.get(dom_tsunami, '情報なし')}"

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

        sent_msg = await channel.send(embed=embed)
        if quake_id:
            on_failure = None
            if issue_type == "ScalePrompt":
                on_failure = functools.partial(
                    self._on_quake_image_failure_scale_prompt,
                    points_by_scale=points_by_scale,
                )
            elif issue_type == "Destination":
                latlon_text = format_latlon(latitude, longitude) if show_latlon_in_name else ""
                on_failure = functools.partial(
                    self._on_quake_image_failure_destination,
                    name=name, latlon_text=latlon_text,
                )
            elif issue_type in ("ScaleAndDestination", "DetailScale") and max_scale_val >= 30:
                on_failure = functools.partial(
                    self._on_quake_image_failure_detail_scale,
                    points_by_scale=points_by_scale, max_scale_val=max_scale_val,
                )
            self.bot.loop.create_task(
                self._attach_p2p_image(sent_msg, quake_id, on_failure=on_failure)
            )

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

        time_str = ""
        if occur_time and "時" in occur_time and "分頃" in occur_time:
            try:
                t_part = occur_time[occur_time.index("日") + 1:]
                time_str = t_part + "、"
            except Exception:
                time_str = ""

        tsunami_speak = ""
        tsunami_speak_map = {
            "MajorWarning": "現在、大津波警報を発表中です。",
            "Warning":      "現在、津波警報を発表中です。",
            "Watch":        "現在、津波注意報を発表中です。",
        }
        if dom_tsunami in tsunami_speak_map:
            tsunami_speak = " " + tsunami_speak_map[dom_tsunami]

        if issue_type == "ScalePrompt":
            # 要件1: 最大震度を観測した区域名をprefecture_map.jsonで
            # 都道府県名に変換し、重複を排除して読み上げる。
            max_addrs = points_by_scale.get(max_scale_val, [])
            if not max_addrs and max_scale_val == 45:
                max_addrs = points_by_scale.get(46, [])
            prefectures = []
            seen_pref = set()
            for addr in max_addrs:
                pref = PREFECTURE_MAP.get(addr)
                if not pref or pref in seen_pref:
                    continue
                seen_pref.add(pref)
                prefectures.append(pref)
            if len(prefectures) == 1:
                pref_text = prefectures[0]
            elif prefectures:
                pref_text = "、".join(prefectures)
            else:
                pref_text = "調査中"
            speak_text = (
                f"{title}。"
                f"{time_str}最大震度{max_scale_str}を、"
                f"{pref_text}で観測しました。"
                f"震源地・規模は調査中です。今後の情報に注意してください。"
            )
        elif issue_type == "Destination":
            speak_text = (
                f"{title}。"
                f"{time_str}地震がありました。"
                f" 震源地は {name}。"
                f" 深さは {depth_str}。"
                f" マグニチュードは {mag_str}。{tsunami_speak}"
            )
        elif issue_type in ("ScaleAndDestination", "DetailScale"):
            if max_scale_val >= 30:
                # 要件3: 最大震度3以上の場合、最大震度を観測した地点名を
                # 読み上げ文言に含める。
                max_addrs = points_by_scale.get(max_scale_val, [])
                if not max_addrs and max_scale_val == 45:
                    # 46（推定5弱以上）は45と同階級として扱う
                    max_addrs = points_by_scale.get(46, [])
                if len(max_addrs) == 1:
                    max_addr_text = max_addrs[0]
                elif len(max_addrs) > 1:
                    max_addr_text = "、".join(max_addrs)
                else:
                    max_addr_text = "調査中"
                speak_text = (
                    f"{title}。"
                    f"{time_str}最大震度{max_scale_str}を観測する地震がありました。"
                    f" 震源地は {name}。"
                    f" 震源の深さは {depth_str}。"
                    f" マグニチュードは {mag_str} と推定されます。"
                    f"この地震で、最大震度{max_scale_str}を、"
                    f"{max_addr_text}で観測しました。{tsunami_speak}"
                )
            else:
                speak_text = (
                    f"{title}。"
                    f"{time_str}最大震度{max_scale_str}を観測する地震がありました。{tsunami_speak}"
                    f" 震源地は {name}。"
                    f" 震源の深さは {depth_str}。"
                    f" マグニチュードは {mag_str} と推定されます。"
                )
        elif issue_type == "Foreign":
            foreign_event_text = "遠地地震がありました。" if is_volcano else "海外で規模の大きな地震がありました。"
            speak_text = (
                f"{title}。"
                f"{time_str}{foreign_event_text}"
                f" 震源地は {name}。"
                f" マグニチュードは {mag_str}。{tsunami_speak}"
            )
        else:
            speak_text = (
                f"{title}。{tsunami_speak}"
                f" 震源地 {name}。 最大震度 {max_scale_str}。 {mag_str}。"
            )

        if not skip_speech:
            await self.speak_local(speak_text)

        if not skip_speech:
            await self.play_quake_sound(data)

    def _convert_jma_quake_to_p2p(self, data: dict) -> dict:
        """
        JMA形式の地震情報をP2P地震情報形式に変換する（プレースホルダー）。

        現時点で "_source": "JMA" を付与するデータ源が実装されていないため、
        本来は未到達コードだが、将来この経路が使われた際に AttributeError
        で notify_quake 全体がクラッシュしないよう、最低限のフォールバックを行う。
        """
        logger.warning(
            "_convert_jma_quake_to_p2p: JMA形式データの変換処理が未実装のため、"
            "受け取ったデータをそのまま返します（本来のP2P形式と構造が異なる可能性があります）"
        )
        return data

    async def play_quake_sound(self, data):
        issue_type = data.get("issue", {}).get("type", "Other")
        if issue_type == "ScalePrompt":
            await self.play_mp3("vxse51")
        elif issue_type == "Destination":
            await self.play_mp3("vxse52")
        else:
            await self.play_mp3("vxse53")

    @staticmethod
    def _build_intensity_list_text(points_by_scale: dict[int, list[str]],
                                    min_scale: int | None = None) -> str:
        """
        points_by_scale（{scale: [addr,...]}）から、
        「■ 震度○\n　地点1\n　地点2」形式のテキストブロックを、
        震度の高い順に連結して組み立てる。

        min_scale を指定すると、その震度未満の階級は出力しない
        （各地の震度に関する情報で「最大震度〜1階級下まで」に絞る用途）。
        """
        from core.constants import SCALE_ORDER, INT_MAP

        blocks = []
        for scale_val in SCALE_ORDER:
            addrs = points_by_scale.get(scale_val)
            if not addrs:
                continue
            if min_scale is not None and scale_val < min_scale:
                continue
            label = INT_MAP.get(scale_val, str(scale_val))
            lines = [f"■ 震度{label}"]
            lines += [f"　{addr}" for addr in addrs]
            blocks.append("\n".join(lines))
        return "\n".join(blocks)

    async def _on_quake_image_failure_scale_prompt(self, message: discord.Message,
                                                     url: str, points_by_scale: dict) -> None:
        """
        震度速報（ScalePrompt）で地図画像が表示できなかった場合のフォールバック。
        津波の有無の記述の下に、各地の震度一覧を追記する（要件1）。
        """
        try:
            embed = message.embeds[0].copy()
            intensity_text = self._build_intensity_list_text(points_by_scale)
            if intensity_text:
                embed.description = f"{embed.description}\n\n{intensity_text}"
            await message.edit(embed=embed)
            logger.info("震度速報: 地図画像取得失敗のため、震度一覧をEmbedに追記しました")
        except Exception as e:
            logger.warning(f"震度速報: 画像失敗時フォールバック処理エラー: {e}")

    async def _on_quake_image_failure_destination(self, message: discord.Message,
                                                    url: str, name: str,
                                                    latlon_text: str) -> None:
        """
        震源に関する情報（Destination）で地図画像が表示できなかった場合の
        フォールバック。リンクのみ貼付し、震源地の横に緯度経度を付記する（要件2）。
        """
        try:
            embed = message.embeds[0].copy()
            if latlon_text:
                embed.description = embed.description.replace(
                    f"**震源地： {name}**",
                    f"**震源地： {name}（{latlon_text}）**",
                )
            embed.description = f"{embed.description}\n\n[地図画像を見る]({url})"
            await message.edit(embed=embed)
            logger.info("震源に関する情報: 地図画像取得失敗のため、リンク＋緯度経度を追記しました")
        except Exception as e:
            logger.warning(f"震源に関する情報: 画像失敗時フォールバック処理エラー: {e}")

    async def _on_quake_image_failure_detail_scale(self, message: discord.Message,
                                                     url: str, points_by_scale: dict,
                                                     max_scale_val: int) -> None:
        """
        各地の震度に関する情報（ScaleAndDestination/DetailScale）で
        地図画像が表示できなかった場合のフォールバック。
        リンクのみ貼付し、最大震度〜1階級下までの観測点一覧を追記する（要件3）。
        """
        try:
            embed = message.embeds[0].copy()
            min_scale = scale_one_level_down(max_scale_val)
            intensity_text = self._build_intensity_list_text(
                points_by_scale, min_scale=min_scale
            )
            appendix = f"[地図画像を見る]({url})"
            if intensity_text:
                appendix += f"\n\n{intensity_text}"
            embed.description = f"{embed.description}\n\n{appendix}"
            await message.edit(embed=embed)
            logger.info("各地の震度に関する情報: 地図画像取得失敗のため、リンク＋震度一覧を追記しました")
        except Exception as e:
            logger.warning(f"各地の震度に関する情報: 画像失敗時フォールバック処理エラー: {e}")
