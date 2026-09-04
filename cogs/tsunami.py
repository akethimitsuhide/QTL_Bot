"""
cogs/tsunami.py
================
津波情報関連の通知を扱う Cog。

【この Cog が担当する機能】
- P2P地震情報 WebSocket（code=552, core.p2p_ws_hub 経由）からの津波情報通知
- JMA tsunami API のポーリング（60秒間隔）・種別振り分け
  - 津波観測情報（VTSE41/51/52）
  - 津波予報・警報・注意報（VTSE41系のForecast）
  - 顕著な地震の震源要素更新のお知らせ（VXSE61）・
    南海トラフ地震臨時情報・関連解説情報（VYSE50）
    ※フェッチ（tsunami/data/list.jsonのポーリング）は本Cogが担うが、
    通知処理の実体は cogs/other.py（OtherInfoCog）
    notify_hypocenter_update() / notify_nankai_trough() に委譲する
    （2026-09-01移設。津波固有の処理を含まず地震情報の一種であるため、
    OtherInfoCogに置く方が実態に合っていると判断した。詳細は
    OtherInfoCog冒頭のdocstring・fetch_tsunami_observation内の
    コメント参照）

【WebSocket移行について（2026-08 feature/p2p-websocket-migration）】
従来は本Cog自身が /v2/history?codes=552 を10秒間隔でポーリングしていたが
（fetch_tsunami）、core.p2p_ws_hub.P2PWebSocketHub が保持する単一WebSocket
接続から code=552 のメッセージを受け取る方式に移行した
（handle_p2p_tsunami）。ポーリングは廃止したが、Bot起動直後に既存の最新
津波情報を誤って新着として通知しないよう、on_ready 時に一度だけ
/v2/history を参照して last_tsunami_id を初期化する
（_init_last_tsunami_id）。

なお、JMA tsunami API（fetch_tsunami_observation, 60秒間隔）は P2P
WebSocketとは無関係の別経路（気象庁HPのXML/JSON）のため、今回の移行対象
外とし、従来通りポーリングを継続する。

【他モジュールとの依存関係】
- core.config       : TSUNAMI_ENABLE, CHANNEL_ID, TSUNAMI_CHANNEL_ID,
                       EWS_ENABLE, EWS_REGION, EWS_BLOCKS,
                       EWS_PRETONE_SEC, EWS_POSTTONE_SEC
- core.constants    : TSUNAMI_MAP, TSUNAMI_GRADE_ORDER, _tsunami_height_key
- core.helpers      : safe_int, safe_float, safe_bool,
                       truncate_embed_description, format_jma_time
- core.audio.AudioMixin       : speak_local, play_mp3, play_ews_pcm（多重継承で利用）
- core.ews_signal   : generate_ews_pcm（2026-09-01追加。津波警報・
                       大津波警報発表/更新時のEWS信号音再生用）
- core.p2p_image.P2PImageMixin : p2p_image_url, _attach_p2p_image（多重継承で利用。
  2026-08-02: 内容検証を強化した安定版としてembed埋め込み方式を再度採用）

【設計メモ】
- Circuit Breaker（連続失敗時のバックオフ）は core.fetch_backoff.FetchBackoff
  に共通化済み（quake.py 等で採用）。本Cogはfetch_tsunamiの旧ポーリング処理が
  P2P津波情報WebSocket化により廃止されたため、そもそもCircuit Breakerを
  使用していない（2026-08: 使われなくなっていた重複実装を削除した）。
- 音声再生（speak_local/play_mp3）は AudioMixin 経由で「自分自身の」
  speech_queue/mp3_queue/audio_files を使う設計のため、
  この Cog も __init__ で自前のキューを持つ
  （Step1の quake.py と同様、Step2時点ではまだ音声再生の一本化は行わない）。
"""
import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import traceback
from datetime import datetime
from collections import defaultdict
import logging

from core.config import (
    CHANNEL_ID, TSUNAMI_CHANNEL_ID,
    TSUNAMI_ENABLE,
    SPEECH_QUEUE_MAXSIZE, MP3_QUEUE_MAXSIZE,
    EWS_ENABLE, EWS_REGION, EWS_BLOCKS, EWS_PRETONE_SEC, EWS_POSTTONE_SEC,
)
from core.constants import TSUNAMI_MAP, TSUNAMI_GRADE_ORDER, _tsunami_height_key, format_tsunami_height_value
from core.helpers import truncate_embed_description, format_jma_time
from core.audio import AudioMixin
from core.ews_signal import generate_ews_pcm
from core.p2p_image import P2PImageMixin
from core.notification_log import record_notification
from core.delivery_stats import record_delivery

logger = logging.getLogger("QTLBot")

# 津波警報等級の重要度順（高い順）。notify_tsunami の Embed本文組み立てと
# 読み上げ文言の両方から参照するため、モジュールレベルに定義する
# （以前は notify_tsunami 内の cancelled=False 分岐だけのローカル変数
# だったため、cancelled=True 側の読み上げロジックから参照すると
# NameError になっていた）。
# 2026-08: core.constants.TSUNAMI_GRADE_ORDER に共有定数として集約。
# cogs/quake.py の EWS 重複再生防止でも同じ順序が必要になったため。
# このモジュール内では従来通り GRADE_ORDER の名前で参照できるよう
# エイリアスとして残す（呼び出し箇所を書き換える必要をなくすため）。
GRADE_ORDER = TSUNAMI_GRADE_ORDER


class TsunamiCog(commands.Cog, AudioMixin, P2PImageMixin):
    """津波情報（P2P・JMA）を扱う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # -- チャンネル（on_ready で解決） --
        self.channel         = None
        self.tsunami_channel = None
        self.other_channel   = None  # 長周期地震動・気象庁その他情報用

        # -- HTTPセッション（この Cog 専用） --
        self.session = None
        self.headers = {"Accept-Encoding": "identity"}

        # -- 津波情報の重複排除状態 --
        self.last_tsunami_id = None
        self.last_tsunami_observation_id = None
        self._tsunami_observation_initialized = False  # 初回ポーリングでは通知せずIDのみ記録

        # 【2026-09-01 追加】EWS（緊急警報放送）信号音のイベント単位管理。
        # 経緯: EWS信号音（core/ews_signal.py, core.audio.AudioMixin.
        # play_ews_pcm）は、以前は cogs/quake.py の地震情報通知
        # （domesticTsunamiフィールドがWarning/MajorWarningの場合）
        # からのみ再生されており、津波情報そのもの（本Cogの
        # notify_tsunami / notify_tsunami_forecast）が発表・更新
        # されたタイミングでは一切再生されていなかった（実機テストで
        # 「津波警報発表・更新時にEWSの音が再生されない」として報告
        # された不具合）。cogs/quake.py._should_play_ews /
        # _play_ews_signal と同じロジック（同一イベントでの警報区分の
        # エスカレーション時のみ再度再生する）をここに移植する。
        # キー: イベント識別子（津波情報のid、またはtsunami_forecast
        # のEventID）、値: 直近に再生した際の警報区分（grade文字列）。
        self._ews_last_grade_by_event: dict[str, str] = {}

        # -- 受信統計（!status 用。将来的にSystemCogと統合予定） --
        self._last_recv = {"tsunami": None}
        self._recv_count = {"tsunami": 0}

        # -- 音声再生（AudioMixin が要求する属性） --
        self.speech_queue = asyncio.PriorityQueue(maxsize=SPEECH_QUEUE_MAXSIZE)
        self.speech_task = None
        self.mp3_queue = asyncio.Queue(maxsize=MP3_QUEUE_MAXSIZE)
        self.mp3_task = None
        self.audio_files = {
            "vxse51": "vxse51.mp3",
            "vxse52": "vxse52.mp3",
            "vxse53": "vxse53.mp3",
            "vxse5c": "vxse5c.mp3",
        }

    # ===============================
    # Cog起動・終了
    # ===============================

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
            connector=aiohttp.TCPConnector(limit=50, ttl_dns_cache=300),
        )
        logger.info("TsunamiCog: aiohttp セッションを作成しました")

    async def cog_unload(self):
        if self.fetch_tsunami_observation.is_running():
            self.fetch_tsunami_observation.cancel()

        for bg_task in (self.speech_task, self.mp3_task):
            if bg_task and not bg_task.done():
                bg_task.cancel()

        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("TsunamiCog: aiohttp セッションを閉じました")

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel         = self.bot.get_channel(CHANNEL_ID)
        self.tsunami_channel = self.bot.get_channel(TSUNAMI_CHANNEL_ID) or self.channel

        if TSUNAMI_ENABLE:
            logger.info(f"津波情報機能: 有効（チャンネルID: {TSUNAMI_CHANNEL_ID}）")
        else:
            logger.warning("津波情報機能: 無効（TSUNAMI_ENABLE=false）")

        # P2P津波情報（code=552）の受信は core.p2p_ws_hub.P2PWebSocketHub が
        # 一元管理する接続から配信される（bot.py 側で
        # hub.register("tsunami", self.handle_p2p_tsunami) により登録される）。
        # ここでは起動前から存在していた最新情報を誤って新着扱いしないよう、
        # last_tsunami_id のみ一度初期化しておく。
        if self.last_tsunami_id is None:
            await self._init_last_tsunami_id()

        if not self.fetch_tsunami_observation.is_running():
            self.fetch_tsunami_observation.start()

        if self.speech_task is None or self.speech_task.done():
            self.speech_task = self.bot.loop.create_task(self.speech_worker())

        if self.mp3_task is None or self.mp3_task.done():
            self.mp3_task = self.bot.loop.create_task(self.mp3_worker())

        logger.info("TsunamiCog: on_ready 完了")

    # ===============================
    # ポーリング・通知
    # ===============================

    @tasks.loop(seconds=60)
    async def fetch_tsunami_observation(self):
        try:
            async with self.session.get(
                "https://www.jma.go.jp/bosai/tsunami/data/list.json",
                ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                if not data:
                    return

                # 観測情報（observation）：既存処理
                OBS_TITLES = [
                    "津波観測に関する情報",
                    "沖合の津波観測に関する情報",
                    "各地の満潮時刻・津波到達予想時刻に関する情報",
                ]
                # 予報・警報情報（forecast）
                FORECAST_TITLES = [
                    "津波予報",
                    "津波注意報",
                    "津波警報",
                    "大津波警報",
                ]
                # 震源要素更新のお知らせ（VXSE61）
                HYPO_UPDATE_TITLES = [
                    "顕著な地震の震源要素更新のお知らせ",
                ]
                # 南海トラフ地震関連情報（VYSE50）
                NANKAI_TITLES = [
                    "南海トラフ地震臨時情報",
                    "南海トラフ地震関連解説情報",
                ]

                for item in data:
                    ttl = item.get("ttl", "")
                    is_obs          = any(x in ttl for x in OBS_TITLES)
                    is_forecast     = any(x in ttl for x in FORECAST_TITLES)
                    is_hypo_update  = any(x in ttl for x in HYPO_UPDATE_TITLES)
                    is_nankai       = any(x in ttl for x in NANKAI_TITLES)
                    if not (is_obs or is_forecast or is_hypo_update or is_nankai):
                        continue

                    event_id    = item.get("eid") or item.get("ctt")
                    report_time = item.get("rdt", "")
                    current_key = f"{event_id}_{report_time}"

                    if self.last_tsunami_observation_id == current_key:
                        logger.debug(f"fetch_tsunami_observation: 既出情報をスキップ (ID: {event_id}, 時刻: {report_time})")
                        break

                    json_filename = item.get("json")
                    if not json_filename:
                        continue

                    # 初回ポーリングは「起動前から存在した情報」の可能性が高いため通知しない。
                    # IDだけ記録し、次回以降の本当の新規発生時のみ通知する。
                    if not self._tsunami_observation_initialized:
                        self.last_tsunami_observation_id = current_key
                        logger.info(f"fetch_tsunami_observation: 起動時の既存情報を記録（通知はしない） ID={event_id}")
                        break

                    detail_url = f"https://www.jma.go.jp/bosai/tsunami/data/{json_filename}"
                    async with self.session.get(detail_url, timeout=aiohttp.ClientTimeout(total=25)) as detail_resp:
                        if detail_resp.status != 200:
                            logger.warning(f"津波情報詳細取得失敗: HTTP {detail_resp.status}")
                            continue
                        detail = await detail_resp.json(content_type=None)

                    # 通知成功後に lastID を更新
                    if is_forecast:
                        logger.info(f"津波予報/警報取得: ID={event_id}, 時刻={report_time}, 種別={ttl}")
                        await self.notify_tsunami_forecast(detail, list_item=item)
                    elif is_hypo_update:
                        # 【2026-09-01 移設】notify_hypocenter_update は
                        # cogs/other.py（OtherInfoCog）へ移設した。
                        # 内容としては地震情報の一種で津波固有の処理を
                        # 含まないため、津波専用のTsunamiCogよりも
                        # OtherInfoCogに置く方が実態に合っている
                        # （詳細はOtherInfoCog側のコメント参照）。
                        # フェッチ自体（このポーリングループ）は
                        # tsunami/data/list.json 由来のままここに残し、
                        # 通知処理のみクロスCog呼び出しで委譲する。
                        logger.info(f"震源要素更新取得: ID={event_id}, 時刻={report_time}")
                        other_cog = self.bot.get_cog("OtherInfoCog")
                        if other_cog:
                            await other_cog.notify_hypocenter_update(detail, list_item=item)
                        else:
                            logger.error("OtherInfoCog が見つからないため震源要素更新を通知できません")
                    elif is_nankai:
                        # 【2026-09-01 移設】notify_nankai_trough も同様に
                        # cogs/other.py（OtherInfoCog）へ移設した。
                        logger.info(f"南海トラフ地震関連情報取得: ID={event_id}, 時刻={report_time}, 種別={ttl}")
                        other_cog = self.bot.get_cog("OtherInfoCog")
                        if other_cog:
                            await other_cog.notify_nankai_trough(detail, list_item=item)
                        else:
                            logger.error("OtherInfoCog が見つからないため南海トラフ地震関連情報を通知できません")
                    else:
                        logger.info(f"津波観測情報取得: ID={event_id}, 時刻={report_time}")
                        await self.notify_tsunami_observation(detail, list_item=item)
                    self.last_tsunami_observation_id = current_key
                    break

                self._tsunami_observation_initialized = True

        except Exception:
            logger.error(f"Fetch Tsunami Observation エラー:\n{traceback.format_exc()}")


    @fetch_tsunami_observation.before_loop
    async def before_fetch_tsunami_observation(self):
        """セッション初期化完了まで待機"""
        await self.bot.wait_until_ready()


    async def _init_last_tsunami_id(self):
        """
        Bot起動直後、/v2/history を一度だけ参照して last_tsunami_id を
        初期化する。WebSocket移行前はポーリングループの初回実行時に
        これを行っていたが、ポーリング自体を廃止したため on_ready から
        明示的に1回だけ呼び出す。

        失敗しても致命的ではない（次にWebSocket経由で新しい津波情報が
        来た時点で last_tsunami_id が更新されるだけ）ため、例外はログのみ
        で握りつぶす。
        """
        try:
            async with self.session.get(
                "https://api.p2pquake.net/v2/history?codes=552&limit=1",
            ) as resp:
                if resp.status == 200:
                    data_list = await resp.json()
                    if data_list:
                        self.last_tsunami_id = data_list[0].get("id")
                        logger.info(
                            f"_init_last_tsunami_id: 起動時の既存最新情報を記録"
                            f"（通知はしない） id={self.last_tsunami_id}"
                        )
        except Exception as e:
            logger.warning(f"_init_last_tsunami_id: 初期化に失敗しました（続行します）: {e}")

    async def handle_p2p_tsunami(self, data: dict) -> None:
        """
        core.p2p_ws_hub.P2PWebSocketHub から code=552（津波予報・警報・
        注意報）のメッセージを受け取るハンドラ。

        【移行前との違い】
        以前は /v2/history?codes=552 を10秒間隔でポーリングしていた
        （fetch_tsunami）。WebSocket移行により、接続・code フィルタリング・
        重複排除（id ベース）は core.p2p_ws_hub.P2PWebSocketHub が
        一元的に行うようになったため、このメソッドは「552のデータを
        受け取った後の通知処理」のみを担当する。

        なお、JMA tsunami API 由来の津波観測情報（VTSE41/51/52等、
        fetch_tsunami_observation が処理するもの）はこのハンドラの対象外。
        """
        try:
            # 【2026-08-02 修正】P2P地震情報WebSocket APIはメッセージに
            # よって "id" ではなく "_id" を使うことがある（quake.py側の
            # handle_p2p_quakeと同じ理由）。両対応する。
            data_id = data.get("id") or data.get("_id")

            if data_id is None:
                logger.warning(
                    f"handle_p2p_tsunami: 受信データに id が含まれていません。"
                    f"原因調査用の生データ: {data!r}"
                )

            if self.last_tsunami_id is None:
                if data_id is not None:
                    self.last_tsunami_id = data_id
                    logger.info(f"handle_p2p_tsunami: 起動時の既存最新情報を記録（通知はしない） id={data_id}")
                else:
                    # 起動直後の最初のメッセージがidなしだった場合、
                    # last_tsunami_idをNoneのままにしておく。次に来る
                    # 有効なid付きメッセージから通常運用を開始する。
                    logger.warning(
                        "handle_p2p_tsunami: 起動時最初の受信メッセージにidが"
                        "ありません。次の有効なメッセージまで初期化を待機します"
                    )
                return

            if data_id is not None and data_id == self.last_tsunami_id:
                # ハブ側で既にid単位の重複排除は行われているはずだが、
                # 念のためこの階層でも同一IDの連続処理を防ぐ。
                # data_id が None の場合はこの等値比較で誤ってスキップ
                # されないよう明示的に除外する（None == None による
                # 「idが無い通知が繰り返し来た場合に2件目以降が
                # 誤って握りつぶされる」事故を防ぐ）。
                return

            if data_id is not None:
                self.last_tsunami_id = data_id
            self._last_recv["tsunami"] = datetime.now()
            self._recv_count["tsunami"] += 1
            logger.info(f"P2P津波情報取得: id={data_id}")
            await self.notify_tsunami(data)

        except Exception:
            logger.error(f"handle_p2p_tsunami エラー:\n{traceback.format_exc()}")


    # ===============================
    # 津波予想高さ文字列フォーマット
    # ===============================
    @staticmethod
    def _format_tsunami_height_value(raw: str) -> str:
        """
        JMA tsunami JSON の MaxHeight.TsunamiHeight（文字列）を表示用に整形する。

        実装本体は core.constants.format_tsunami_height_value に切り出した
        （2026-08 リファクタリング。cogs/tsunami.py の肥大化対策）。
        呼び出し方法・戻り値の仕様は変更していない。
        """
        return format_tsunami_height_value(raw)

    def _should_play_ews(self, event_key: str, grade: str) -> bool:
        """
        【2026-09-01 追加】同一の津波イベントについて EWS 信号音を
        再度鳴らすべきかを判定する（cogs/quake.py._should_play_ews
        からの移植。判定ロジックは同一）。

        経緯: EWS信号音（core/ews_signal.py, core.audio.AudioMixin.
        play_ews_pcm）は、以前は cogs/quake.py の地震情報通知
        （domesticTsunamiフィールドがWarning/MajorWarningの場合）
        からのみ再生されており、津波情報そのもの（本Cogの
        notify_tsunami / notify_tsunami_forecast）が発表・更新
        されたタイミングでは一切再生されていなかった（実機テストで
        「津波警報発表・更新時にEWSの音が再生されない」として報告
        された不具合）。

        quake.py版は真のイベント識別子が無いP2P地震情報形式に対する
        近似キー（発生時刻+震源地名）を使っていたが、津波情報は
        notify_tsunami側でP2Pの"id"、notify_tsunami_forecast側で
        JMAの"EventID"という、より確実な識別子を呼び出し元から
        受け取れるため、本メソッドではevent_keyを呼び出し元に
        計算させる設計にしている（quake.py側のように内部で
        eq/hypoから逆算しない）。

        判定ルール:
        - このキーで一度もEWSを鳴らしていなければ True（初回は必ず鳴らす）
        - 既に鳴らした際の区分より今回の区分の方が深刻
          （Warning → MajorWarning 等）であれば True
        - それ以外（同一区分の繰り返し等）は False
        """
        prev_grade = self._ews_last_grade_by_event.get(event_key)

        if prev_grade is None:
            self._ews_last_grade_by_event[event_key] = grade
            return True

        try:
            prev_rank = TSUNAMI_GRADE_ORDER.index(prev_grade)
            curr_rank = TSUNAMI_GRADE_ORDER.index(grade)
        except ValueError:
            # 未知の区分値の場合は安全側に倒して鳴らす
            self._ews_last_grade_by_event[event_key] = grade
            return True

        if curr_rank < prev_rank:
            # 順位が若いほど深刻（MajorWarning=0 < Warning=1）
            self._ews_last_grade_by_event[event_key] = grade
            logger.info(
                f"EWS: 同一津波イベント({event_key})で警報区分がエスカレーション "
                f"({prev_grade} → {grade}) のため再度信号音を再生します"
            )
            return True

        logger.debug(
            f"EWS: 同一津波イベント({event_key})・区分据え置き "
            f"({prev_grade} → {grade}) のため信号音の再生をスキップします"
        )
        return False

    async def _play_ews_signal(self, grade: str) -> None:
        """
        core.ews_signal.generate_ews_pcm でメモリ上にPCMを生成し、
        外部ファイルへ一切出力せずそのまま再生する
        （cogs/quake.py._play_ews_signal と同じ実装）。
        """
        try:
            pcm_bytes = generate_ews_pcm(
                region_key=EWS_REGION,
                blocks=EWS_BLOCKS,
                pre_tone_sec=EWS_PRETONE_SEC,
                post_tone_sec=EWS_POSTTONE_SEC,
            )
            logger.info(f"EWS信号音を再生します（津波警報区分={grade}, region={EWS_REGION}）")
            await self.play_ews_pcm(pcm_bytes, label=f"ews_tsunami_{grade}")
        except Exception:
            logger.error(f"EWS信号音の生成・再生でエラー:\n{traceback.format_exc()}")

    async def notify_tsunami(self, data, is_test=False):
        if not TSUNAMI_ENABLE and not is_test:
            return
        channel = self.tsunami_channel or self.channel
        if not channel:
            return

        try:
            cancelled = data.get("cancelled", False)
            areas = data.get("areas", [])
            time_str = format_jma_time(data.get("issue", {}).get("time", "不明"))
            # 画像URL生成・重複判定用のID。P2P地震情報WebSocket APIは
            # メッセージによって "id" ではなく "_id" を使うことがあるため
            # 両対応する（quake.py側と同じ理由）。
            tsunami_id = data.get("id") or data.get("_id")

            source = data.get("issue", {}).get("source", "P2P地震情報")

            # cancelled=True のときも読み上げブロックから安全に参照できるよう、
            # cancelled 分岐の外（try直下）で初期化しておく
            # 【2026-08-31 修正】値の型を list[str]（area_nameのみ）から
            # list[dict]（name/immediate/firstHeight情報を含む）に変更。
            # 公式仕様（P2P地震情報 津波予報API）のfirstHeight/immediate
            # フィールドを反映するため（詳細は下記のループ内コメント参照）。
            grade_height_map: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
            max_grade = "Unknown"

            title = "津波情報"
            if cancelled:
                title = "津波警報解除"
            if is_test:
                title = "【テスト】 " + title

            description = f"**発表機関： {source}**\n**発表時刻： {time_str}**\n\n"

            if cancelled:
                description += "すべての津波予報が解除されました。"
            else:
                # 【2026-08-31 修正】以前は area_name の文字列のみを保持する
                # {grade: {height_desc: [area_name, ...]}} という2段階
                # グループ化だったため、公式仕様（ユーザー提供のP2P地震情報
                # 津波予報API仕様書）にある firstHeight（到達予想時刻・
                # 状況）・immediate（ただちに来襲）が実装から欠落していた。
                # 元のbot.py（分割前）にはこれらの情報を反映する処理が
                # 存在しており、cogs/tsunami.py への分割時に取りこぼされた
                # リグレッションだったことが判明したため、area_name だけ
                # でなく immediate/firstHeight も保持するよう修正する。
                # {grade: {height_desc: [{"name":..., "immediate":...,
                #                          "first_cond":..., "first_time":...}, ...]}}
                for area in areas:
                    name  = area.get("name", "不明")
                    grade = area.get("grade", "Unknown")
                    max_h = area.get("maxHeight", {}).get("description", "")
                    immediate = bool(area.get("immediate", False))
                    first_height = area.get("firstHeight") or {}
                    first_cond = first_height.get("condition", "")
                    first_time = first_height.get("arrivalTime", "")
                    grade_height_map[grade][max_h].append({
                        "name": name,
                        "immediate": immediate,
                        "first_cond": first_cond,
                        "first_time": first_time,
                    })
                    if grade in ("MajorWarning", "Warning"):
                        max_grade = grade

                # 警報レベル別の注意喚起文
                alert_msg = ""
                if max_grade == "MajorWarning":
                    alert_msg = "\n【大至急】今すぐ高台に避難してください。\n"
                elif max_grade == "Warning":
                    alert_msg = "\n今すぐ海岸・河川から離れて避難してください。\n"
                elif "Watch" in grade_height_map:
                    alert_msg = "\n海岸・河川に近づかないでください。\n"

                if alert_msg:
                    description += alert_msg

                for grade in sorted(
                    grade_height_map.keys(),
                    key=lambda g: GRADE_ORDER.index(g) if g in GRADE_ORDER else 99
                ):
                    label = TSUNAMI_MAP.get(grade, grade)
                    description += f"**■ {label}**\n"
                    height_dict = grade_height_map[grade]
                    for height_desc in sorted(
                        height_dict.keys(), key=_tsunami_height_key, reverse=True
                    ):
                        area_entries = height_dict[height_desc]
                        if height_desc:
                            description += f"予想高さ {height_desc}\n"
                        for entry in area_entries:
                            line = f"　{entry['name']}"
                            annotations = []
                            if entry["immediate"]:
                                annotations.append("ただちに来襲")
                            if entry["first_cond"]:
                                annotations.append(entry["first_cond"])
                            if entry["first_time"]:
                                annotations.append(f"到達予想: {entry['first_time']}")
                            if annotations:
                                line += "　（" + "・".join(annotations) + "）"
                            description += line + "\n"

                # ===== 追加: コメント情報（Warning Comment）=====
                warning_comment = data.get("comments", {}).get("warningComment", {}).get("text", "")
                if warning_comment:
                    description += f"\n**注意:** {warning_comment}\n"

            # ===== 追加: 原因地震情報の強化 =====
            eq = data.get("earthquake", {})
            if eq:
                eq_source = eq.get("source", "")
                if eq_source:
                    # source が既に description に含まれているかチェック
                    if "※" not in description:
                        description += f"\n\n※原因地震情報は {eq_source} からの情報です"

            # Discord API の制限（4096文字）に対応した正確な切り詰め
            description = truncate_embed_description(
                description,
                max_chars=4096,
                suffix="\n\n（地域が多いため一部省略）"
            )

            color_map = {
                "MajorWarning": 0xD344FC,
                "Warning":      0xF93022,
                "Watch":        0xEEDB2D,
                "Unknown":      0x56BCFC,
            }
            color = 0x00FF00 if cancelled else color_map.get(max_grade, 0x56BCFC)

            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=datetime.now()
            )

            footer_parts = []
            if is_test:
                footer_parts.append("※これはテスト通知です。")
            if footer_parts:
                embed.set_footer(text=" | ".join(footer_parts))

            sent_msg = await channel.send(embed=embed)
            if not is_test:
                record_delivery(True, "津波情報")
                record_notification("津波情報", title)
            # 2026-08-02: CDN反映のリトライ埋め込み方式を、内容検証を
            # 強化した上で再度採用する（core/p2p_image.py の
            # _attach_p2p_image docstring参照）。
            if tsunami_id:
                self.bot.loop.create_task(self._attach_p2p_image(sent_msg, tsunami_id))

            # ── 読み上げ文言 ──
            # 以前は「{title} が発表されました」の固定文言のみで、Embed本文には
            # 含まれている対象地域名・警報種別・予想される高さが読み上げには
            # 一切反映されていなかった。ここでは grade_height_map を使って
            # 「{地域名}に{警報種別}が発表されました」の形で組み立てる。
            if cancelled:
                speak_text = "津波情報が解除されました"
            elif grade_height_map:
                # 最も重要度の高い grade を一つ選び、その地域名を読み上げる
                top_grade = min(
                    grade_height_map.keys(),
                    key=lambda g: GRADE_ORDER.index(g) if g in GRADE_ORDER else 99
                )
                grade_label = TSUNAMI_MAP.get(top_grade, top_grade)
                # top_grade に属する地域名を height_desc をまたいで集約
                # 【2026-08-31 修正】grade_height_map の値が area_name の
                # 文字列から、immediate/firstHeight情報を含む辞書
                # （{"name":..., "immediate":..., ...}）に変わったため、
                # ここでも "name" キーを取り出すよう対応する。
                area_names = []
                for entries in grade_height_map[top_grade].values():
                    area_names.extend(e["name"] for e in entries)
                if len(area_names) == 1:
                    area_text = area_names[0]
                elif len(area_names) <= 3:
                    area_text = "、".join(area_names)
                else:
                    area_text = "、".join(area_names[:3]) + "等"
                speak_text = f"{area_text}に{grade_label}が発表されました"
            else:
                speak_text = f"{title} が発表されました"

            await self.speak_local(speak_text)
            if not cancelled:
                if any(a.get("grade") == "MajorWarning" for a in areas):
                    await self.play_mp3("vxse51")
                elif any(a.get("grade") == "Warning" for a in areas):
                    await self.play_mp3("vxse52")
                elif any(a.get("grade") == "Watch" for a in areas):
                    await self.play_mp3("vxse5c")
                else:
                    await self.play_mp3("vxse53")

            # ── EWS（緊急警報放送）信号音 ──
            # 【2026-09-01 追加】津波警報（Warning）・大津波警報
            # （MajorWarning）が発表・更新された際にEWS信号音を鳴らす。
            # 従来はcogs/quake.pyの地震情報通知（domesticTsunami経由）
            # からのみ再生されており、津波情報そのもの（本メソッド）
            # では一切再生されていなかった不具合の修正（詳細は
            # _should_play_ews / _play_ews_signal のdocstring参照）。
            # is_test時は本番のEWS状態（_ews_last_grade_by_event）を
            # 汚染しないよう、判定を経ずに毎回鳴らす。
            if EWS_ENABLE and not cancelled and max_grade in ("Warning", "MajorWarning"):
                event_key = str(tsunami_id or f"{data.get('issue', {}).get('time', '')}")
                if is_test or self._should_play_ews(event_key, max_grade):
                    await self._play_ews_signal(max_grade)
        except Exception as e:
            record_delivery(False, "津波情報", str(e))
            logger.error(f"notify_tsunami エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")

    # ===============================
    # 津波到達時刻情報通知（VTSE41/51/52）
    # ===============================
    async def notify_tsunami_observation(self, detail, list_item=None, is_test=False):
        channel = self.tsunami_channel or self.channel
        if not channel:
            return
        try:
            ttl = list_item.get("ttl", "津波情報") if list_item else "津波情報"
            title = f"{ttl}"
            if is_test:
                title = "【テスト】 " + title

            report_time = "不明"
            head = detail.get("Head", {})
            body = detail.get("Body", {})
            
            # 発表機関と発表時刻
            source = detail.get("Control", {}).get("PublishingOffice", "気象庁")
            report_time = format_jma_time(head.get("ReportDateTime", "不明"))
            
            # 原因地震情報
            cause_text = ""
            eq_list = body.get("Earthquake", [])
            if eq_list and len(eq_list) > 0:
                eq = eq_list[0] if isinstance(eq_list, list) else eq_list
                origin_time = format_jma_time(eq.get("OriginTime", "不明"))
                hypo = eq.get("Hypocenter", {})
                hypo_name = hypo.get("Area", {}).get("Name", "不明")
                magnitude = eq.get("Magnitude", "不明")
                depth = hypo.get("Depth", "")
                depth_str = f"　深さ{depth}" if depth else ""
                
                # ===== 追加: Earthquake.Source を含める =====
                eq_source = eq.get("Source", "")
                source_note = ""
                if eq_source:
                    source_note = f" ※原因地震情報は {eq_source} からの情報です"
                
                # 【2026-09-02 修正】以前は末尾に "\n\n" を含んだ文字列を
                # そのまま "**{cause_text}**" で囲んでいたため、太字終了の
                # "**" が改行を挟んだ次の行に孤立して表示される崩れが
                # あった（実際の通知文で確認）。太字で囲む対象からは
                # 改行を除外し、"\n\n" は呼び出し側で付与するよう修正する。
                cause_text = f"原因地震： {hypo_name}　M{magnitude}{depth_str}（{origin_time}発生）{source_note}"
            
            # 説明文の開始
            # 【2026-09-02 修正】embed.title と全く同じ文言を description の
            # 1行目にも太字で重複表示していたため削除した
            # （Discord上でタイトルと本文の両方に同じ文言が出てしまっていた）。
            description = (
                f"**発表機関:** {source}\n"
                f"**発表時刻:** {report_time}\n"
            )
            
            if cause_text:
                description += f"\n**{cause_text}**\n"
            
            # 津波観測情報
            tsunami = body.get("Tsunami", {})
            obs = tsunami.get("Observation", {})
            items = obs.get("Item", [])
            
            if items:
                # 注意喚起文
                description += "\n**避難を続けて！**\n"
                
                # 各地域の観測点情報
                for item in items:
                    area = item.get("Area", {})
                    area_name = area.get("Name", "不明")
                    
                    description += f"**■ {area_name}**\n"
                    
                    stations = item.get("Station", [])
                    if not stations:
                        description += "　観測点なし\n"
                        continue
                    
                    for st in stations:
                        st_name = st.get("Name", "不明")
                        
                        # 高さ情報を取得
                        max_h = st.get("MaxHeight", {})
                        height_str = ""
                        
                        if isinstance(max_h, dict):
                            condition = max_h.get("Condition", "")
                            value = max_h.get("value")
                            tsunami_height = max_h.get("TsunamiHeight", "")
                            
                            if condition:
                                height_str = condition
                            elif value is not None:
                                height_str = f"{value}"
                            elif tsunami_height:
                                height_str = tsunami_height
                        
                        if not height_str:
                            height_str = "欠測"

                        if height_str in ["微弱", "弱", "低い", "欠測", "観測中"]:
                            height_display = height_str
                        elif height_str.startswith("<0.2"):
                            height_display = "0.2m未満"
                        elif height_str and height_str[0].isdigit():
                            height_display = f"{height_str}m"
                        else:
                            height_display = height_str
                        
                        # 到達情報を取得
                        first_h = st.get("FirstHeight", {})
                        arrival_text = ""
                        
                        if isinstance(first_h, dict):
                            arrival_cond = first_h.get("Condition", "")
                            if arrival_cond and height_str != "欠測":
                                arrival_text = f"　{arrival_cond}"
                        
                        # 観測地点の表示
                        description += f"　　{st_name}　　{height_display}{arrival_text}\n"
                
                description += "\n"
            
            # コメント情報
            comment = tsunami.get("Comment", {})
            free_form = comment.get("FreeFormComment", "")
            if free_form:
                description += f"{free_form}\n"
            
            # ===== 追加: Warning Comment =====
            warning_comment = comment.get("WarningComment", {}).get("Text", "")
            if warning_comment:
                description += f"\n**注意:** {warning_comment}\n"
            
            # 予報情報（参考）
            forecast = tsunami.get("Forecast", {})
            forecast_items = forecast.get("Item", [])
            if forecast_items:
                # 最大の警報レベルを取得
                max_grade = "Unknown"
                for fcast in forecast_items:
                    cat = fcast.get("Category", {})
                    kind = cat.get("Kind", {}).get("Name", "")
                    if "大津波警報" in kind:
                        max_grade = "MajorWarning"
                        break
                    elif "津波警報" in kind:
                        max_grade = "Warning"
                    elif "津波注意報" in kind and max_grade == "Unknown":
                        max_grade = "Watch"
                
                # 警報ステータスを追加
                if max_grade == "MajorWarning":
                    description += "\n現在、大津波警報が発表されています"
                elif max_grade == "Warning":
                    description += "\n現在、津波警報が発表されています"
                elif max_grade == "Watch":
                    description += "\n現在、津波注意報が発表されています"
            
            # Embed 作成
            color = 0x00BFFF
            embed = discord.Embed(
                title=title,
                description=description,
                color=color
            )
            
            # メンション
            mention = ""
            if any("大津波警報" in str(x) for x in forecast_items):
                mention = f"{self.bot.user.mention} "
            
            await channel.send(mention, embed=embed)
            if not is_test:
                record_delivery(True, "津波観測情報")
                record_notification("津波観測情報", title)
            logger.info(f"津波観測情報を通知しました: {title}")
            
        except Exception as e:
            record_delivery(False, "津波観測情報", str(e))
            logger.error(f"notify_tsunami_observation エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")


    async def notify_tsunami_forecast(self, detail: dict, list_item: dict | None = None, is_test: bool = False) -> None:
        """
        気象庁 tsunami/data/{json} から取得した予報・警報情報を Discord に通知する。
        観測情報（Observation）は除外し、Forecast（警報種別・エリア）を中心に表示する。
        スタイルは notify_tsunami_observation に準拠。

        記事: https://qiita.com/KAI_Mutsumi/items/178739fa4bb95f2c574c 参考
        警報コード:
          00/60 → なし  50 → 解除  51 → 津波警報  52/53 → 大津波警報
          62 → 津波注意報  71/72/73 → 津波予報
        """
        channel = self.tsunami_channel or self.channel
        if not channel:
            return

        # 気象庁公式資料準拠の警報レベルマッピング。
        # 00=津波なし, 50=津波警報・大津波警報の解除, 51=津波警報,
        # 52=大津波警報, 53=大津波警報(新規発表/切替),
        # 60=津波注意報の解除（「なし」ではなく「注意報から解除された」の意味）,
        # 62=津波注意報, 71/72/73=津波予報（若干の海面変動）
        # ※ 73 は「津波警報解除、津波予報への切替」を意味する特殊コードだが、
        #   このBotでは「警報が完全に終わったわけではなく予報段階に移行した」
        #   という状態のため、危険度としては予報(lv=1)として扱う。
        WARN_LEVELS = {
            "00": 0, "50": 0,   # なし・警報系解除
            "51": 4,            # 津波警報
            "52": 5, "53": 5,   # 大津波警報
            "60": 0,            # 津波注意報解除（危険度としては0だが、意味は「なし」ではない）
            "62": 2,            # 津波注意報
            "71": 1, "72": 1, "73": 1,  # 津波予報
        }
        WARN_COLORS = {
            5: 0xC800FF,  # 大津波警報: 紫
            4: 0xFF2800,  # 津波警報: 赤
            2: 0xFAF500,  # 津波注意報: 黄
            1: 0x80FFFF,  # 津波予報: 水色
            0: 0xC8C8CB,  # なし・解除: グレー
        }
        WARN_LABEL = {
            5: "大津波警報", 4: "津波警報", 2: "津波注意報",
            1: "津波予報",   0: "解除/なし",
        }
        # 注意喚起文（絵文字なし）
        WARN_ALERT = {
            5: "【大至急】今すぐ高台に避難してください。",
            4: "今すぐ海岸・河川から離れて避難してください。",
            2: "海岸・河川に近づかないでください。",
        }
        WARN_MP3 = {5: "vxse51", 4: "vxse52", 2: "vxse5c"}

        try:
            ttl     = list_item.get("ttl", "津波情報") if list_item else "津波情報"
            title   = ("【テスト】 " if is_test else "") + ttl
            control = detail.get("Control", {})
            head    = detail.get("Head", {})
            body    = detail.get("Body", {})
            tsunami = body.get("Tsunami", {})

            source      = control.get("PublishingOffice", "気象庁")
            report_time = format_jma_time(head.get("ReportDateTime", "不明"))

            # ── 原因地震情報 ──
            cause_text = ""
            eq_list = body.get("Earthquake", [])
            if eq_list:
                eq = eq_list[0] if isinstance(eq_list, list) else eq_list
                origin_time = format_jma_time(eq.get("OriginTime", "不明"))
                hypo      = eq.get("Hypocenter", {})
                hypo_name = hypo.get("Area", {}).get("Name", "不明")
                magnitude = eq.get("Magnitude", "不明")
                depth     = hypo.get("Depth", "")
                depth_str = f"　深さ{depth}" if depth else ""
                eq_source = eq.get("Source", "")
                source_note = f" ※原因地震情報は {eq_source} からの情報です" if eq_source else ""
                # 【2026-09-02 修正】notify_tsunami_observationと同様、太字終了の
                # "**" が改行を挟んで孤立する崩れを解消するため、改行を
                # 呼び出し側に移した。
                cause_text = f"原因地震： {hypo_name}　M{magnitude}{depth_str}（{origin_time}発生）{source_note}"

            # ── Forecast（予報区別の警報種別） ──
            forecast = tsunami.get("Forecast", {})
            forecast_items = forecast.get("Item", []) if isinstance(forecast, dict) else []
            if isinstance(forecast_items, dict):
                forecast_items = [forecast_items]

            max_level = 0
            # {level: {height_desc: [area_name]}} の2段階グループ化
            level_height_areas: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

            # 解除コードの内訳を集計する。
            # Code: 50 = 津波警報・大津波警報の解除、60 = 津波注意報の解除、
            # 00 = 元々津波なし。同じ配信内にこれらが混在することもあるため、
            # 実際に解除されたレベルの最大値を別途追跡する
            # （例: 大津波警報→注意報に切り下げられた地域と、注意報→解除に
            #   なった地域が同時に配信されるケースがある）。
            cancelled_from_level = 0  # 0=なし, 2=注意報解除, 4=警報/大津波警報解除
            has_any_cancel_code = False

            for fi in forecast_items:
                area_name = fi.get("Area", {}).get("Name", "不明")
                kind_code = fi.get("Category", {}).get("Kind", {}).get("Code", "00")
                lv = WARN_LEVELS.get(kind_code, 0)

                if kind_code == "50":
                    has_any_cancel_code = True
                    cancelled_from_level = max(cancelled_from_level, 4)
                elif kind_code == "60":
                    has_any_cancel_code = True
                    cancelled_from_level = max(cancelled_from_level, 2)

                # 予想高さ取得
                # TsunamiHeight は dict（{"description": "..."}）の場合と
                # 文字列（"<0.2", ">10", "5" 等）の場合がある
                max_h_obj = fi.get("MaxHeight", {})
                height_desc = ""
                if isinstance(max_h_obj, dict):
                    th = max_h_obj.get("TsunamiHeight")
                    if isinstance(th, dict):
                        height_desc = (
                            th.get("description")
                            or th.get("Description")
                            or max_h_obj.get("Description")
                            or ""
                        )
                    elif isinstance(th, str) and th:
                        height_desc = self._format_tsunami_height_value(th)
                    else:
                        height_desc = max_h_obj.get("Description", "")

                level_height_areas[lv][height_desc].append(area_name)
                if lv > max_level:
                    max_level = lv

            # ── 解除判定 ──
            # Headline.Text は気象庁側で既に正確な文言（「津波注意報を
            # 解除しました。」等）が入っているため、これを最優先で使う。
            # 取得できない場合のみ、上で集計した cancelled_from_level から
            # 実際に解除された種別を組み立てる（従来はここが固定文言
            # 「津波警報が解除されました」になっており、実際には津波注意報
            # のみが解除された場合でも誤って警報解除と案内してしまっていた）。
            is_cancelled = max_level == 0 and bool(forecast_items) and has_any_cancel_code
            headline_text = (head.get("Headline", {}) or {}).get("Text", "").strip()
            if is_cancelled:
                if headline_text:
                    cancel_speak_text = headline_text
                elif cancelled_from_level == 4:
                    cancel_speak_text = "津波警報が解除されました"
                elif cancelled_from_level == 2:
                    cancel_speak_text = "津波注意報が解除されました"
                else:
                    cancel_speak_text = "津波の心配はなくなりました"
            else:
                cancel_speak_text = ""

            # ── description 組み立て（notify_tsunami_observation スタイル準拠） ──
            # 【2026-09-02 修正】embed.title と同じ文言をdescriptionの1行目にも
            # 重複表示していたため削除した。
            description = (
                f"**発表機関:** {source}\n"
                f"**発表時刻:** {report_time}\n"
            )
            if cause_text:
                description += f"\n**{cause_text}**\n"

            if is_cancelled:
                description += cancel_speak_text or "すべての津波警報・注意報・予報が解除されました。"
            else:
                # 注意喚起文（絵文字なし）
                alert_msg = WARN_ALERT.get(max_level, "")
                if alert_msg:
                    description += f"\n{alert_msg}\n"

                # 警報レベル別・予想高さ別エリア一覧（高いレベル・高い高さ順）
                for lv in (5, 4, 2, 1):
                    height_dict = level_height_areas.get(lv, {})
                    if not height_dict:
                        continue
                    label = WARN_LABEL[lv]
                    description += f"\n**■ {label}**\n"
                    for height_desc in sorted(
                        height_dict.keys(), key=_tsunami_height_key, reverse=True
                    ):
                        area_names = height_dict[height_desc]
                        if height_desc:
                            description += f"予想高さ {height_desc}\n"
                        for n in area_names:
                            description += f"　{n}\n"

                # コメント・警報コメント
                comment   = tsunami.get("Comment", {})
                free_form = (comment.get("FreeFormComment") or "") if isinstance(comment, dict) else ""
                warn_cmt  = (comment.get("WarningComment", {}) or {}).get("Text", "") if isinstance(comment, dict) else ""
                if free_form:
                    description += f"\n{free_form}\n"
                if warn_cmt:
                    description += f"\n**注意:** {warn_cmt}\n"

            # ── Body.Text と Body.Comments.FreeFormComment を末尾に追加 ──
            body_text = (body.get("Text") or "").strip()
            body_cmts = body.get("Comments", {}) or {}
            free_form = ((body_cmts.get("FreeFormComment") or "") if isinstance(body_cmts, dict) else "").strip()
            if body_text:
                description += f"\n\n{body_text}"
            if free_form:
                description += f"\n\n{free_form}"

            description = truncate_embed_description(description)

            # ── Embed 作成 ──
            embed_color = WARN_COLORS.get(max_level, 0xC8C8CB) if not is_cancelled else 0x00FF00
            embed = discord.Embed(
                title=title,
                description=description,
                color=embed_color,
            )
            footer_parts = []
            if is_test:
                footer_parts.append("※これはテスト通知です。")
            if footer_parts:
                embed.set_footer(text=" | ".join(footer_parts))

            await channel.send(embed=embed)
            if not is_test:
                record_delivery(True, "津波予報")
                record_notification("津波予報", title)
            logger.info(f"津波予報/警報通知完了: {title} max_level={max_level}")


            # ── 読み上げ・音声 ──
            if is_cancelled:
                await self.speak_local(cancel_speak_text)
            else:
                speak_label = WARN_LABEL.get(max_level, "津波情報")
                await self.speak_local(f"{speak_label}が発表されました")
                mp3_key = WARN_MP3.get(max_level)
                if mp3_key:
                    await self.play_mp3(mp3_key)

            # ── EWS（緊急警報放送）信号音 ──
            # 【2026-09-01 追加】notify_tsunami と同じ理由・同じ仕組みで
            # 追加。max_level（5=大津波警報, 4=津波警報）を
            # TSUNAMI_GRADE_ORDER の文字列表現に変換した上で
            # _should_play_ews に渡す（_should_play_ews / _play_ews_signal
            # のdocstring参照）。is_test時は本番のEWS状態を汚染しない
            # よう、判定を経ずに毎回鳴らす。
            ews_grade_map = {5: "MajorWarning", 4: "Warning"}
            ews_grade = ews_grade_map.get(max_level)
            if EWS_ENABLE and not is_cancelled and ews_grade:
                event_key = str(head.get("EventID") or ttl)
                if is_test or self._should_play_ews(event_key, ews_grade):
                    await self._play_ews_signal(ews_grade)

        except Exception as e:
            record_delivery(False, "津波予報", str(e))
            logger.error(f"notify_tsunami_forecast エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")

    # ===============================
    # 【2026-09-01 移設】notify_hypocenter_update / notify_nankai_trough は
    # cogs/other.py（OtherInfoCog）へ移設した。理由: これら2つの情報種別は
    # 内容としては地震情報の一種で、津波固有の処理（津波高さ・到達予想
    # 時刻等）を一切含まないため、津波専用のTsunamiCogよりも、地震のその他
    # 特別情報を扱うOtherInfoCogに置く方が実態に合っていると判断した
    # （送信先チャンネルも self.tsunami_channel から self.other_channel に
    # 変更済み。OtherInfoCog.notify_quake_advisory と同じ運用に統一）。
    # フェッチ自体（tsunami/data/list.jsonのポーリング）は上記
    # fetch_tsunami_observation に残したままで、通知処理のみ
    # self.bot.get_cog("OtherInfoCog") 経由でクロスCog呼び出ししている
    # （fetch_tsunami_observation内のコメント参照）。
    # ===============================
