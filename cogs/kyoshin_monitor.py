"""
cogs/kyoshin_monitor.py
========================
core.kyoshin_detector / core.kyoshin_image_monitor / core.kyoshin_stations を
統合し、強震モニタ画像の解析による揺れ検知と、Discord への画像通知を
実際に行う Cog。

【検知パイプライン（2026-08 実観測点方式への移行後）】
1. core.kyoshin_stations.StationStore: ingen084氏の
   kyoshin-monitor-observation-points（intensity-points.json）から
   実観測点データ（コード・名称・緯度経度・画像上のピクセル座標）を
   取得・キャッシュする。起動時はキャッシュ優先、以後
   KYOSHIN_STATIONS_REFRESH_SEC 秒（既定1時間）ごとに再取得する
2. core.kyoshin_stations.build_k_nearest_neighbors(): 緯度経度から
   K近傍（既定6件）の近隣観測点リストを計算する
3. 画像取得後、各観測点のピクセル座標を直接サンプリングし、
   core.kyoshin_stations.color2position()（フォーク版
   akethimitsuhide/kyoshin-monitor-python から移植）で実震度に変換する
4. EventManager.ingest(): 各観測点の震度の時系列（過去10秒分）を追跡し、
   「上昇幅がしきい値を超えた観測点」を検出したら、K近傍の観測点も
   同時に上昇しているかで真偽を判定する（ingen084氏の記事のアルゴリズム）
5. EventManager.tick(): 判定された観測点群をイベントとしてまとめる

【方針転換の経緯（2026-07-22、画像ピクセルグリッド方式への転換）】
当初はHSVマスクで抽出した「アクティブセル（絶対震度が閾値以上）」を
8近傍の連結成分でクラスタリングし、複数フレーム持続を見る
（core.kyoshin_cluster_tracker.ClusterTracker）方式だったが、これは
「震度の絶対値が静的に隣接している」ことしか見ておらず、単一観測点
由来のGIF圧縮ノイズが偶然2〜3セルにまたがるだけで誤検知に至る
ケースが多発した。

ingen084氏の記事（強震モニタの画像から揺れていることを検知する）が
提唱する「観測点ごとの震度の時系列上昇幅を追跡し、近隣観測点も
同時に上昇しているか」という動的な変化ベースの判定の方が、
静的な絶対値の隣接判定よりも本物の地震と単発ノイズを区別する
能力が高いと判断し、EventManager.ingest()（この記事のアルゴリズムを
実装したコアロジック）を検知の主軸に据える方針に転換した。
ClusterTrackerは使用しないこととした。

【方針転換の経緯（2026-08、実観測点方式への移行）】
上記の転換時点では「観測点ごとの座標・ピクセル位置対応表」を
持っていなかったため、画像をN×Nピクセルのグリッドセルに分割し、
セルを疑似観測点として扱っていた（core.kyoshin_image_analyzer の
build_station_grid / analyze_all、現在は未使用）。
ingen084氏の kyoshin-monitor-observation-points リポジトリが
配布する intensity-points.json に、まさにこの対応表（実観測点の
座標・ピクセル位置）が含まれていたため、疑似グリッドを廃止し、
実観測点データへ全面的に切り替えた。EventManagerのコアロジック
（ingest/tick、近隣クロスバリデーション、イベントマージ、
自動終了と最大震度保持、ブラックリスト）自体は無変更で、
「何を観測点として登録するか」だけが変わった形になる。

参考:
- https://qiita.com/ingen084/items/82985e8d3227c97c608d
  （強震モニタの画像から揺れていることを検知する／ingen084氏）
- https://github.com/ingen084/kyoshin-monitor-observation-points
  （観測点データ intensity-points.json の配布元）
- https://github.com/akethimitsuhide/kyoshin-monitor-python
  （フォーク元: https://github.com/t0729/kyoshin-monitor-python。
  色→震度変換式 color2position() の移植元）

【画像取得元】
https://www.lmoni.bosai.go.jp/img_svr/data/map_img/RealTimeImg/jma_s/{YYYYMMDD}/{YYYYMMDDHHMMSS}.jma_s.gif
（防災科研 リアルタイム震度モニタの公開画像。系統は jma_s のみを使用する。
2026-08-19: 配信元を smi.lmoniexp.bosai.go.jp から
www.lmoni.bosai.go.jp/img_svr に変更した）

画像の時刻決定には、まず以下の latest.json API から実際に配信されている
最新時刻(latest_time)を取得し、その時刻をもとに画像URLを構築する
（従来の「現在時刻から遡ってリトライ探索する」方式は、latest.json取得に
失敗した場合のフォールバックとしてのみ使用する）。
https://smi.lmoniexp.bosai.go.jp/webservice/server/pros/latest.json

【Discord通知の内容について】
- 検知した揺れ検知イベントの通知には、jma_s系統・abrspmx_s(LMoni)系統の
  両方の画像と、kwatch-24h.netの振動レベルを表示する
- 通知の色は、jma_s系統から推定した実震度に基づく独自カラーマップ
  （core.kyoshin_shared.JMA_S_SHINDO_COLORS）で決定する
- 検出観測点数は通知本文には表示しない（内部的な閾値判定にのみ使用する）
- 通知に必要な最小観測点数は、実震度（震度0相当 / 震度1相当以上）に
  応じて2段階に分ける（KYOSHIN_MIN_STATIONS_SHINDO0 / SHINDO1）
"""
import asyncio
import io
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
import aiohttp

from core.config import (
    CHANNEL_ID, KYOSHIN_CHANNEL_ID, ENABLE_KYOSHIN,
    KYOSHIN_IMAGE_DELAY_SEC, KYOSHIN_IMAGE_STEP_SEC,
    KYOSHIN_IMAGE_MAX_RETRY,
    KYOSHIN_RECV_JMA_S_ENABLE, KYOSHIN_RECV_LMONI_ENABLE, KYOSHIN_RECV_VIBRATION_ENABLE,
    KYOSHIN_RECV_IMAGE_INTERVAL_SEC, KYOSHIN_RECV_VIBRATION_INTERVAL_SEC,
    KYOSHIN_VIBRATION_DETECT_LEVEL, KYOSHIN_NOTIFY_ENABLE, KYOSHIN_NOTIFY_ON_EEW,
    KYOSHIN_NOTIFY_ON_DETECT,
    KYOSHIN_ACTIVE_SHINDO_FLOOR,
    KYOSHIN_RISE_THRESHOLD, KYOSHIN_NEIGHBOR_TRIGGER_COUNT,
    KYOSHIN_BASELINE_WINDOW_START_SEC, KYOSHIN_BASELINE_WINDOW_END_SEC,
    KYOSHIN_HISTORY_WINDOW_SEC, KYOSHIN_EVENT_TIMEOUT_SEC,
    KYOSHIN_STARTUP_WARMUP_SEC,
    KYOSHIN_MIN_NOTIFY_PHASE, KYOSHIN_MIN_STATIONS_SHINDO0, KYOSHIN_MIN_STATIONS_SHINDO1,
    KYOSHIN_DEBUG_SAVE_IMAGE, KYOSHIN_DEBUG_IMAGE_DIR,
    KYOSHIN_STATIONS_SOURCE_URL, KYOSHIN_STATIONS_CACHE_PATH,
    KYOSHIN_STATIONS_REFRESH_SEC, KYOSHIN_NEIGHBOR_K,
    KYOSHIN_SLOW_FETCH_THRESHOLD_SEC,
    KYOSHIN_PATCH_RADIUS, KYOSHIN_PATCH_AGGREGATION,
    KYOSHIN_DECODE_FAILURE_LOG_INTERVAL_SEC,
    KYOSHIN_DETECT_VIBRATION_SOUND,
)
from core.audio import AudioClientMixin
from core.kyoshin_detector import DetectorConfig, SeismicEvent
from core.kyoshin_image_monitor import KyoshinImageMonitor
from core.kyoshin_stations import (
    StationStore, KyoshinStationMeta, build_k_nearest_neighbors,
    make_shindo_decoder, make_patch_shindo_sampler,
)
from core.kyoshin_shared import (
    DualImageFetcher, fetch_vibration_level, shindo_to_color, vibration_to_color,
    vibration_tier, VIBRATION_TIER_MP3,
)

logger = logging.getLogger("QTLBot")

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

JMA_S_BASE = "https://www.lmoni.bosai.go.jp/img_svr/data/map_img/RealTimeImg/jma_s"

# 実際に配信されている最新画像の時刻を取得するAPI。
# レスポンス例:
#   {"request_time": "2026/07/20 07:51:22", "latest_time": "2026/07/20 07:51:21",
#    "result": {"status": "success", "message": ""}, "security": {...}}
# latest_time は日本時間(JST)で "YYYY/MM/DD HH:MM:SS" 形式。
KYOSHIN_LATEST_TIME_API = "https://smi.lmoniexp.bosai.go.jp/webservice/server/pros/latest.json"

# NIEDの画像URLはJST基準で命名されているため、サーバーのローカルタイムゾーン
# 設定（例: ラズパイがUTCのまま等）に依存させず、常に明示的にJSTで計算する。
JST = ZoneInfo("Asia/Tokyo")

# フェーズの強さの序列（KYOSHIN_MIN_NOTIFY_PHASE との比較に使用）
PHASE_ORDER = ["Weaker", "Weak", "Medium", "Strong", "Stronger"]

# 定性的なフェーズ名の日本語表示（震度の具体的数値は一切出さない）
PHASE_LABEL_JA = {
    "Weaker":   "微弱な揺れの可能性",
    "Weak":     "弱い揺れの可能性",
    "Medium":   "揺れを検知",
    "Strong":   "強い揺れを検知",
    "Stronger": "非常に強い揺れを検知",
}


def _phase_index(phase: str) -> int:
    try:
        return PHASE_ORDER.index(phase)
    except ValueError:
        return 0


class KyoshinMonitorCog(commands.Cog, AudioClientMixin):
    """強震モニタ画像の解析（震度の時系列上昇幅＋K近傍同時上昇の検証）による揺れ検知・Discord通知を行う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel = None
        self.kyoshin_channel = None
        self.session: aiohttp.ClientSession | None = None

        self._station_store = StationStore(
            source_url=KYOSHIN_STATIONS_SOURCE_URL,
            cache_path=KYOSHIN_STATIONS_CACHE_PATH,
            refresh_interval_sec=KYOSHIN_STATIONS_REFRESH_SEC,
        )
        # station_code -> KyoshinStationMeta（サンプリング用の座標参照に使う）
        self._station_by_code: dict[str, KyoshinStationMeta] = {}
        self._stations_initialized = False
        self._stations_refresh_task = None

        self._shindo_from_rgb = make_shindo_decoder(
            inactive_sentinel=KYOSHIN_ACTIVE_SHINDO_FLOOR - 1.0
        )
        # 【2026-09-09 追加】KYOSHIN_PATCH_RADIUS>=1のときのみ使うパッチ
        # サンプラー。半径0（既定）のままなら作らず、従来通り
        # self._shindo_from_rgb による単一ピクセル方式を使う
        # （_fetch_current_shindo_map 参照）。
        self._shindo_from_patch = None
        if KYOSHIN_PATCH_RADIUS >= 1:
            self._shindo_from_patch = make_patch_shindo_sampler(
                inactive_sentinel=KYOSHIN_ACTIVE_SHINDO_FLOOR - 1.0,
                radius=KYOSHIN_PATCH_RADIUS,
                aggregation=KYOSHIN_PATCH_AGGREGATION,
            )
            logger.info(
                f"KyoshinMonitorCog: パッチサンプリングを有効化しました "
                f"(radius={KYOSHIN_PATCH_RADIUS}, "
                f"patch={2*KYOSHIN_PATCH_RADIUS+1}x{2*KYOSHIN_PATCH_RADIUS+1}, "
                f"aggregation={KYOSHIN_PATCH_AGGREGATION})"
            )

        self._last_image_url: str | None = None
        # 【2026-09-22 追加】検知に使った最新フレームの取得時刻（monotonic）と
        # 画像自体の時刻（latest.json の latest_time）。通知では、この
        # フレームのURLをそのまま表示に再利用し（_send_notification）、
        # 通知遅延の計測ログ（画像の古さ）にも使う。
        self._last_image_at: float | None = None
        self._last_image_time_jst: datetime | None = None
        self._dual_image_fetcher = DualImageFetcher()
        # イベントID -> 初回通知を送った時刻(loop time)。検知→初回通知の
        # 遅延をINFOログに1回だけ出すための管理（_on_event_endedで削除）。
        self._notified_events: dict[str, float] = {}

        # 【2026-09-09 追加】画像デコード失敗ログの集約カウンタ。
        # _log_decode_failure 参照。1秒間隔ポーリングでは画像が生成
        # 途中のタイミングで捕まることがあり、想定内の一時的事象として
        # 比較的頻繁に発生しうる。毎回WARNINGを出すとログノイズになり、
        # 本当に見るべき異常（強震モニタの誤検知やWebSocket切断等）を
        # 埋もれさせるリスクがあるため、一定間隔で件数をまとめて
        # 1回だけWARNINGを出す方式にした（詳細は毎回DEBUGに出す）。
        self._decode_failure_count_in_window = 0
        self._decode_failure_window_start_mono: float | None = None

        # !status / /qtl_status・Web Dashboard から他Cogと統一的に参照
        # できるよう、他の受信系Cogと同じ _last_recv / _recv_count の
        # 形式で「実際に検知通知をDiscordへ送信した」回数・時刻を記録する
        # （2026-08-27 追加）。
        # "kyoshin": 揺れ検知時の通知、"long_period_monitor": EEW発表時の通知
        # （どちらもこのCogが送信する。2026-10-05に EewCog から移管）
        self._last_recv: dict[str, datetime | None] = {"kyoshin": None, "long_period_monitor": None}
        self._recv_count: dict[str, int] = {"kyoshin": 0, "long_period_monitor": 0}

        # 【2026-10-05】受信結果の共有状態（通知ループが参照する）
        # 振動レベル（kwatch-24h.net）の最新値と受信時刻（monotonic）
        self._vib_level: int | None = None
        self._vib_at: float | None = None
        self._vib_detecting_prev = False
        # 最新フレーム（jma_s）内の最大実震度。通知色に使う（揺れ候補が無ければ None）
        self._latest_max_shindo: float | None = None
        self._detect_suspended = False   # EEW通知のため検知通知を中断中か（ログ用）
        self._vib_task = None
        self._notify_task = None
        # 通知間隔＝有効な受信のうち最短の受信間隔（各下限以上なので1秒以上）
        _intervals = []
        if KYOSHIN_RECV_JMA_S_ENABLE or KYOSHIN_RECV_LMONI_ENABLE:
            _intervals.append(KYOSHIN_RECV_IMAGE_INTERVAL_SEC)
        if KYOSHIN_RECV_VIBRATION_ENABLE:
            _intervals.append(KYOSHIN_RECV_VIBRATION_INTERVAL_SEC)
        self._notify_interval_sec: float | None = min(_intervals) if _intervals else None

        self.monitor = KyoshinImageMonitor(
            get_readings=self._fetch_current_shindo_map,
            send_kyoshin_image=None,   # 通知は _notify_loop が一元管理する（2026-10-05）
            on_event_ended=self._on_event_ended,
            config=DetectorConfig(
                rise_threshold=KYOSHIN_RISE_THRESHOLD,
                neighbor_trigger_count=KYOSHIN_NEIGHBOR_TRIGGER_COUNT,
                baseline_window_start_sec=KYOSHIN_BASELINE_WINDOW_START_SEC,
                baseline_window_end_sec=KYOSHIN_BASELINE_WINDOW_END_SEC,
                history_window_sec=KYOSHIN_HISTORY_WINDOW_SEC,
                event_timeout_sec=KYOSHIN_EVENT_TIMEOUT_SEC,
                startup_warmup_sec=KYOSHIN_STARTUP_WARMUP_SEC,
            ),
            poll_interval_sec=KYOSHIN_RECV_IMAGE_INTERVAL_SEC,
        )
        self._monitor_task = None

        if KYOSHIN_DEBUG_SAVE_IMAGE:
            os.makedirs(KYOSHIN_DEBUG_IMAGE_DIR, exist_ok=True)
            logger.info(
                f"KyoshinMonitorCog: デバッグ画像保存を有効化しました "
                f"(保存先: {KYOSHIN_DEBUG_IMAGE_DIR})"
            )

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
        )
        logger.info("KyoshinMonitorCog: aiohttp セッションを作成しました")

    async def cog_unload(self):
        for t in (self._stations_refresh_task, self._vib_task, self._notify_task):
            if t and not t.done():
                t.cancel()
        if self._monitor_task and not self._monitor_task.done():
            await self.monitor.stop()
            self._monitor_task.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel = self.bot.get_channel(CHANNEL_ID)
        self.kyoshin_channel = self.bot.get_channel(KYOSHIN_CHANNEL_ID) or self.channel

        if not ENABLE_KYOSHIN:
            logger.info("KyoshinMonitorCog: ENABLE_KYOSHIN=false のため起動しません")
            return

        if self._notify_interval_sec is None:
            logger.info(
                "KyoshinMonitorCog: 強震モニタ・長周期地震動モニタ・振動レベルの受信が"
                "すべて無効のため起動しません"
            )
            return

        # jma_s の画像解析による揺れ検知には Pillow と観測点データが必要。
        # 無い場合も、EEW発表時・振動レベル検知時の通知は続ける。
        if KYOSHIN_RECV_JMA_S_ENABLE:
            if not _PIL_AVAILABLE:
                logger.error(
                    "KyoshinMonitorCog: Pillow (PIL) がインストールされていないため、"
                    "強震モニタ画像の解析による揺れ検知は無効です（EEW・振動レベルの通知は継続）。"
                    "requirements.txt の Pillow を pip install してください。"
                )
            elif not self._stations_initialized:
                await self._init_stations()

            if self._monitor_task is None or self._monitor_task.done():
                self._monitor_task = self.bot.loop.create_task(self.monitor.run())
                logger.info(
                    "KyoshinMonitorCog: 強震モニタ(jma_s)の受信・監視ループを開始しました"
                    f"（間隔 {KYOSHIN_RECV_IMAGE_INTERVAL_SEC:g} 秒。震度の時系列上昇幅＋K近傍同時上昇の検証方式）"
                )

            if _PIL_AVAILABLE and (
                self._stations_refresh_task is None or self._stations_refresh_task.done()
            ):
                self._stations_refresh_task = self.bot.loop.create_task(self._stations_refresh_loop())

        if KYOSHIN_RECV_VIBRATION_ENABLE and (self._vib_task is None or self._vib_task.done()):
            self._vib_task = self.bot.loop.create_task(self._vibration_poll_loop())
            logger.info(
                "KyoshinMonitorCog: 振動レベルの受信ループを開始しました"
                f"（間隔 {KYOSHIN_RECV_VIBRATION_INTERVAL_SEC:g} 秒、"
                f"{KYOSHIN_VIBRATION_DETECT_LEVEL} 以上で揺れを検知）"
            )

        if KYOSHIN_NOTIFY_ENABLE and (KYOSHIN_NOTIFY_ON_EEW or KYOSHIN_NOTIFY_ON_DETECT):
            if self._notify_task is None or self._notify_task.done():
                self._notify_task = self.bot.loop.create_task(self._notify_loop())
                logger.info(
                    f"KyoshinMonitorCog: 通知ループを開始しました（間隔 {self._notify_interval_sec:g} 秒、"
                    f"EEW時={KYOSHIN_NOTIFY_ON_EEW}, 検知時={KYOSHIN_NOTIFY_ON_DETECT}）"
                )
        else:
            logger.info("KyoshinMonitorCog: 通知は無効です（受信・検知のみ）")

    # ===============================
    # 観測点データの初期化・定期更新
    # ===============================
    async def _init_stations(self) -> None:
        """
        起動時に1回だけ呼ぶ。観測点データをロード（キャッシュ優先）し、
        K近傍を計算して EventManager に登録する。
        """
        stations = await self._station_store.load_or_fetch(self.session)
        if not stations:
            logger.error(
                "KyoshinMonitorCog: 観測点データが0件のため、強震モニタの検知は"
                "実質的に機能しません（次回の定期更新で回復する可能性があります）"
            )
            return

        await self._register_stations(stations)

    async def _register_stations(self, stations: list[KyoshinStationMeta]) -> None:
        """
        観測点リストから K近傍を計算し、EventManager へ登録する。

        K近傍計算はCPUバウンドな処理（約1,600件で0.1〜0.2秒程度、
        グリッドバケット法で高速化済み）だが、念のため run_in_executor で
        イベントループをブロックしないようにする。

        既に登録済みの観測点（_station_by_code に存在する）は、
        EventManager.register_station() を再度呼ばない
        （register_station は Station を新規生成するため、再登録すると
        進行中のイベント所属や履歴がリセットされてしまう）。
        近隣リストのみ更新する。
        """
        loop = self.bot.loop
        neighbor_map = await loop.run_in_executor(
            None, build_k_nearest_neighbors, stations, KYOSHIN_NEIGHBOR_K
        )

        new_count = 0
        for st in stations:
            self._station_by_code[st.code] = st
            neighbors = neighbor_map.get(st.code, [])

            existing = self.monitor.event_manager.stations.get(st.code)
            if existing is None:
                self.monitor.event_manager.register_station(
                    st.code, neighbors=neighbors, is_island=st.is_island
                )
                new_count += 1
            else:
                # 進行中の検知状態（history/event_id/blacklisted等）を
                # 保持したまま、近隣リストのみ更新する
                existing.neighbors = neighbors

        self._stations_initialized = True
        logger.info(
            f"KyoshinMonitorCog: 実観測点を{len(stations)}件登録しました"
            f"（新規{new_count}件、K近傍={KYOSHIN_NEIGHBOR_K}）"
        )

    async def _stations_refresh_loop(self) -> None:
        """
        KYOSHIN_STATIONS_REFRESH_SEC 秒（既定1時間）ごとに観測点データを
        再取得し、EventManagerへの登録を更新するバックグラウンドタスク。
        """
        while not self.bot.is_closed():
            await asyncio.sleep(KYOSHIN_STATIONS_REFRESH_SEC)
            try:
                stations = await self._station_store.refresh(self.session)
                if stations:
                    await self._register_stations(stations)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"KyoshinMonitorCog: 観測点データの定期更新でエラー: {e}", exc_info=True)

    # ===============================
    # 画像取得・解析（検知パイプライン）
    # ===============================
    def _log_decode_failure(self, exc: Exception) -> None:
        """
        画像デコード失敗（KYOSHIN_RECV_IMAGE_INTERVAL_SEC間隔で発生しうる、
        想定内の一時的事象）のログを、KYOSHIN_DECODE_FAILURE_LOG_INTERVAL_SEC
        秒ごとに1回だけWARNINGへ集約して出す。ウィンドウ内の詳細は
        毎回DEBUGへ出すため、DEBUGログを有効にすれば従来通り1件ずつ
        確認できる。
        """
        now_mono = time.monotonic()
        self._decode_failure_count_in_window += 1

        window_elapsed = (
            None if self._decode_failure_window_start_mono is None
            else now_mono - self._decode_failure_window_start_mono
        )
        if window_elapsed is None or window_elapsed >= KYOSHIN_DECODE_FAILURE_LOG_INTERVAL_SEC:
            count = self._decode_failure_count_in_window
            if count > 1:
                logger.warning(
                    f"KyoshinMonitorCog: 画像デコードに失敗しました（直近"
                    f"{KYOSHIN_DECODE_FAILURE_LOG_INTERVAL_SEC}秒間で{count}回、"
                    f"最新のエラー: {exc}）"
                )
            else:
                logger.warning(f"KyoshinMonitorCog: 画像デコードに失敗しました: {exc}")
            self._decode_failure_window_start_mono = now_mono
            self._decode_failure_count_in_window = 0
        else:
            logger.debug(f"KyoshinMonitorCog: 画像デコードに失敗しました（集約中）: {exc}")

    async def _fetch_current_shindo_map(self) -> dict[str, float]:
        """
        強震モニタ画像(jma_s系統)を取得し、登録済み実観測点それぞれの
        ピクセル座標をサンプリングして dict[station_code, shindo] で返す。

        「本物の地震かどうか」の判定はここでは行わない。ここでは
        画像→震度マップへの変換のみを行い、実際の判定（時系列上昇幅の
        追跡・K近傍同時上昇の検証）は core.kyoshin_detector.EventManager.
        ingest() / tick()（呼び出し元の core.kyoshin_image_monitor.
        KyoshinImageMonitor.run()）に委ねる。

        画像上のカラースケール外（背景・地図色等）のピクセルは、
        core.kyoshin_stations.make_shindo_decoder が返す「静穏相当の
        代表値」に変換される（震度7センチネルを誤って注入しないための
        安全策。core/kyoshin_stations.py のモジュールdocstring参照）。

        jma_s の受信が無効・画像が取得できない・観測点データが未初期化
        （Pillow未導入を含む）の場合は空の dict を返す
        （EventManagerには何もフィードされず、そのフレームの観測値は
        欠測扱いになる。進行中のイベントがある場合は、新たな上昇が
        観測されないまま KYOSHIN_EVENT_TIMEOUT_SEC が経過すると自然に
        終了する）。

        【2026-10-05】EEW発表中も受信・検知は止めない（以前は EewCog 側の
        取得と重複しないよう空を返していた）。EEW発表中は通知ループが検知による
        通知だけを中断する（_notify_tick 参照）。取得した最新フレームのURLと
        最大実震度は、EEW通知・検知通知の表示にも共用する。
        """
        if not KYOSHIN_RECV_JMA_S_ENABLE:
            return {}

        # 【2026-08-29 追加】このメソッドは KYOSHIN_RECV_IMAGE_INTERVAL_SEC
        # （既定1.0秒）ごとに24時間365日呼ばれる、強震モニタ機能の
        # 定常的な負荷の中心部分。ダウンロード（ネットワークI/O）と
        # デコード＋観測点サンプリング（CPUバウンド）を分けて計測し、
        # Raspberry Pi等での実測に基づいた最適化判断ができるようにする
        # （憶測でポーリング間隔やアルゴリズムを変更しないための材料）。
        # 通常はDEBUGログにのみ出力し、KYOSHIN_SLOW_FETCH_THRESHOLD_SEC
        # （既定0.5秒）を超えた場合のみ、運用中でも気づけるようWARNING
        # ログも出す。
        t_download_start = time.perf_counter()
        image_bytes, url = await self._download_latest_image()
        download_elapsed = time.perf_counter() - t_download_start

        if image_bytes is None:
            logger.debug(
                f"KyoshinMonitorCog: 画像取得に失敗しました "
                f"(download={download_elapsed*1000:.1f}ms)"
            )
            return {}

        self._last_image_url = url
        self._last_image_at = time.monotonic()
        self._latest_max_shindo = None

        # Pillow・観測点データが無い場合は、画像（通知表示用）の取得だけ行い解析はしない
        if not _PIL_AVAILABLE or not self._stations_initialized:
            return {}

        t_decode_start = time.perf_counter()
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except Exception as e:
            self._log_decode_failure(e)
            return {}

        pixels = img.load()
        w, h = img.size

        shindo_map: dict[str, float] = {}
        for code, st in self._station_by_code.items():
            x, y = st.pixel_x, st.pixel_y
            if not (0 <= x < w and 0 <= y < h):
                # 画像範囲外の座標（観測点データが画像サイズと不整合。
                # 配信元の画像仕様変更等で起こりうる）は静穏扱いにする
                continue
            if self._shindo_from_patch is not None:
                shindo_map[code] = self._shindo_from_patch(pixels, x, y, w, h)
            else:
                r, g, b = pixels[x, y]
                shindo_map[code] = self._shindo_from_rgb(r, g, b)
        decode_sampling_elapsed = time.perf_counter() - t_decode_start

        total_elapsed = download_elapsed + decode_sampling_elapsed
        log_msg = (
            f"KyoshinMonitorCog: shindo_map生成 "
            f"download={download_elapsed*1000:.1f}ms "
            f"decode+sampling={decode_sampling_elapsed*1000:.1f}ms "
            f"合計={total_elapsed*1000:.1f}ms "
            f"(観測点{len(shindo_map)}件, ポーリング間隔={KYOSHIN_RECV_IMAGE_INTERVAL_SEC}秒)"
        )
        if decode_sampling_elapsed >= KYOSHIN_SLOW_FETCH_THRESHOLD_SEC:
            logger.warning(
                f"{log_msg} — decode+samplingがKYOSHIN_SLOW_FETCH_THRESHOLD_SEC"
                f"（{KYOSHIN_SLOW_FETCH_THRESHOLD_SEC}秒）を超過しました。"
                f"CPU負荷や高速化の要否を検討する材料としてください。"
            )
        else:
            logger.debug(log_msg)

        active_values = [v for v in shindo_map.values() if v >= KYOSHIN_ACTIVE_SHINDO_FLOOR]
        self._latest_max_shindo = max(active_values) if active_values else None

        if KYOSHIN_DEBUG_SAVE_IMAGE:
            active_count = len(active_values)
            if active_count > 0:
                self._save_debug_image(image_bytes, active_count)

        return shindo_map

    def _save_debug_image(self, image_bytes: bytes, confirmed_count: int) -> None:
        """
        confirmed判定が出たフレームの元画像をローカルに保存する（デバッグ・事後検証用）。
        KYOSHIN_DEBUG_SAVE_IMAGE=true のときのみ呼ばれる。
        保存自体の失敗はBot本体の動作に影響させないよう握りつぶしてログのみ出す。
        """
        try:
            ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S_%f")
            filename = f"kyoshin_{ts}_cells{confirmed_count}.gif"
            path = os.path.join(KYOSHIN_DEBUG_IMAGE_DIR, filename)
            with open(path, "wb") as f:
                f.write(image_bytes)
            logger.debug(f"KyoshinMonitorCog: デバッグ画像を保存しました: {path}")
        except Exception as e:
            logger.warning(f"KyoshinMonitorCog: デバッグ画像の保存に失敗しました: {e}")

    async def _fetch_latest_image_time(self) -> datetime | None:
        """
        latest.json から実際に配信されている最新画像の時刻(latest_time)を取得する。

        取得・パースに失敗した場合は None を返す（呼び出し側は従来の
        リトライ探索方式にフォールバックする）。
        """
        try:
            async with self.session.get(
                KYOSHIN_LATEST_TIME_API, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    logger.debug(
                        f"KyoshinMonitorCog: latest.json取得失敗 (status={resp.status})"
                    )
                    return None
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.debug(f"KyoshinMonitorCog: latest.json取得エラー: {e}")
            return None

        try:
            if data.get("result", {}).get("status") != "success":
                logger.debug(
                    f"KyoshinMonitorCog: latest.jsonのresult.statusが異常: {data.get('result')}"
                )
                return None
            latest_time_str = data["latest_time"]
            # "YYYY/MM/DD HH:MM:SS" 形式（JST）としてパースする
            dt = datetime.strptime(latest_time_str, "%Y/%m/%d %H:%M:%S").replace(tzinfo=JST)
            return dt
        except (KeyError, ValueError) as e:
            logger.debug(f"KyoshinMonitorCog: latest.jsonのパースに失敗しました: {e}")
            return None

    async def _download_latest_image(self) -> tuple[bytes | None, str | None]:
        """
        実際に存在する最新の jma_s 画像をダウンロードする。

        まず latest.json から配信中の最新時刻を取得し、その時刻ちょうどの
        画像を1回だけ取得する。latest.json取得・当該時刻画像取得のいずれかに
        失敗した場合は、現在時刻から少し過去に遡りながら探索する従来方式に
        フォールバックする。
        """
        latest_dt = await self._fetch_latest_image_time()
        self._last_image_time_jst = latest_dt  # 取得失敗時は None（画像の古さの計測は省略）
        if latest_dt is not None:
            ts = latest_dt.strftime("%Y%m%d%H%M%S")
            url = f"{JMA_S_BASE}/{latest_dt.strftime('%Y%m%d')}/{ts}.jma_s.gif"
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return data, url
                    logger.debug(
                        f"KyoshinMonitorCog: latest.json時刻の画像取得失敗 "
                        f"(status={resp.status}, url={url})。従来方式にフォールバックします"
                    )
            except Exception as e:
                logger.debug(
                    f"KyoshinMonitorCog: latest.json時刻の画像取得エラー ({url}): {e}。"
                    f"従来方式にフォールバックします"
                )

        # ── フォールバック: 現在時刻から遡りながらリトライ探索 ──
        for i in range(KYOSHIN_IMAGE_MAX_RETRY):
            dt = datetime.now(JST) - timedelta(seconds=KYOSHIN_IMAGE_DELAY_SEC + KYOSHIN_IMAGE_STEP_SEC * i)
            ts = dt.strftime("%Y%m%d%H%M%S")
            url = f"{JMA_S_BASE}/{dt.strftime('%Y%m%d')}/{ts}.jma_s.gif"
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return data, url
            except Exception as e:
                logger.debug(f"KyoshinMonitorCog: 画像取得失敗 ({url}): {e}")
        return None, None

    # ===============================
    # 振動レベルの受信・検知（2026-10-05。EewCog.vibration_monitor_loop から集約）
    # ===============================
    def _fresh_vib_level(self) -> int | None:
        """直近の振動レベル。受信が途絶えて古い（受信間隔の3倍・最低10秒超）場合は None。"""
        if self._vib_level is None or self._vib_at is None:
            return None
        if time.monotonic() - self._vib_at > max(10.0, 3 * KYOSHIN_RECV_VIBRATION_INTERVAL_SEC):
            return None
        return self._vib_level

    def _vibration_detecting(self) -> bool:
        """振動レベルが KYOSHIN_VIBRATION_DETECT_LEVEL 以上か（振動レベルによる揺れの検知）。"""
        if not KYOSHIN_RECV_VIBRATION_ENABLE:
            return False
        level = self._fresh_vib_level()
        return level is not None and level >= KYOSHIN_VIBRATION_DETECT_LEVEL

    async def _vibration_poll_loop(self) -> None:
        """振動レベル（kwatch-24h.net）を KYOSHIN_RECV_VIBRATION_INTERVAL_SEC（2秒以上）間隔で受信する。"""
        while not self.bot.is_closed():
            try:
                level = await fetch_vibration_level(self.session)
                if level is not None:
                    self._vib_level = level
                    self._vib_at = time.monotonic()
                detecting = self._vibration_detecting()
                if detecting != self._vib_detecting_prev:
                    self._vib_detecting_prev = detecting
                    logger.info(
                        f"KyoshinMonitorCog: 振動レベルによる揺れの検知を"
                        f"{'開始' if detecting else '終了'}しました (level={self._vib_level})"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"KyoshinMonitorCog: 振動レベル受信ループでエラー: {e}", exc_info=True)
            await asyncio.sleep(KYOSHIN_RECV_VIBRATION_INTERVAL_SEC)

    # ===============================
    # 通知（EEW時・検知時の両方をここで一元管理する）
    # ===============================
    def _event_is_notifiable(self, event: SeismicEvent) -> bool:
        """
        画像解析で検知したイベントが通知対象か。
        観測点数（震度0相当: KYOSHIN_MIN_STATIONS_SHINDO0、震度1相当以上:
        KYOSHIN_MIN_STATIONS_SHINDO1）と、フェーズ（KYOSHIN_MIN_NOTIFY_PHASE以上）で判定する。
        満たさない場合も検知自体は継続する（通知だけ抑制する）。
        """
        member_count = len(event.member_station_ids)
        min_stations = (
            KYOSHIN_MIN_STATIONS_SHINDO1 if event.max_shindo >= 1.0 else KYOSHIN_MIN_STATIONS_SHINDO0
        )
        if member_count < min_stations:
            # 【2026-10-03修正】防災科研の利用規約に配慮し、実測震度の数値はログに出力しない
            logger.debug(
                f"KyoshinMonitorCog: 観測点数{member_count}件のため通知対象外 "
                f"（検出していない扱い、閾値={min_stations}）"
            )
            return False
        if _phase_index(event.phase) < _phase_index(KYOSHIN_MIN_NOTIFY_PHASE):
            logger.debug(
                f"KyoshinMonitorCog: フェーズ{event.phase}は"
                f"KYOSHIN_MIN_NOTIFY_PHASE={KYOSHIN_MIN_NOTIFY_PHASE}未満のため通知対象外"
            )
            return False
        return True

    def _pick_notifiable_event(self) -> SeismicEvent | None:
        """通知対象のイベントのうち最大実震度のものを返す（通知は1本に集約する）。"""
        best = None
        for event in list(self.monitor.event_manager.events.values()):
            if self._event_is_notifiable(event) and (best is None or event.max_shindo > best.max_shindo):
                best = event
        return best

    def _detection_active(self) -> bool:
        """揺れを検知中か（画像解析のイベントが存在する、または振動レベルが検知水準以上）。"""
        return bool(self.monitor.event_manager.events) or self._vibration_detecting()

    async def _notify_loop(self) -> None:
        """
        通知ループ。通知間隔＝有効な受信のうち最短の受信間隔（1秒以上）で
        _notify_tick を繰り返す。EEW通知と検知通知を同じループで送るため、
        どちらの場合も通知間隔が守られる。
        """
        interval = self._notify_interval_sec or 1.0
        while not self.bot.is_closed():
            t0 = time.monotonic()
            try:
                await self._notify_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"KyoshinMonitorCog: 通知ループでエラー: {e}", exc_info=True)
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - t0)))

    async def _notify_tick(self) -> None:
        """
        1回分の通知判定。
          1. EEW発表中（第一報〜最終報/キャンセル報、最長 EEW_MONITOR_MAX_SEC 秒）で、
             EEW時の通知が有効なら EEW通知を送る。この間、検知による通知は中断する
             （検知自体は続ける）。
          2. それ以外で、検知中（画像解析イベントまたは振動レベル検知）かつ検知時の通知が
             有効なら検知通知を送る。検知が終了すれば通知も止まる。
        EEW時の通知が無効なら、EEW発表中も検知通知は中断しない。
        """
        if KYOSHIN_NOTIFY_ON_EEW and self._is_eew_active():
            if not self._detect_suspended and self._detection_active():
                self._detect_suspended = True
                logger.info("KyoshinMonitorCog: EEW通知のため、検知による通知を中断します（検知は継続）")
            await self._send_notification(eew=True, event=None)
            return

        if self._detect_suspended:
            self._detect_suspended = False
            if self._detection_active():
                logger.info("KyoshinMonitorCog: EEW通知が終了したため、検知による通知を再開します")

        if not KYOSHIN_NOTIFY_ON_DETECT:
            return
        event = self._pick_notifiable_event()
        if event is None and not self._vibration_detecting():
            return
        await self._send_notification(eew=False, event=event)

    async def _send_notification(self, eew: bool, event: SeismicEvent | None) -> None:
        """
        強震モニタ(jma_s)・長周期地震動モニタ(LMoni)の画像と振動レベルを含む通知を
        Discordへ送る。受信が無効な項目は含めない。通知色は jma_s の実震度
        （検知通知は検知イベントの実震度）に基づく独自カラーマップ、なければ振動レベルに基づく。
        """
        channel = self.kyoshin_channel or self.channel
        if not channel:
            return

        vib_level = self._fresh_vib_level() if KYOSHIN_RECV_VIBRATION_ENABLE else None

        # 受信済みの最新フレーム（jma_s）。受信が途絶えて古い場合は表示しない
        jma_s_url = None
        if (KYOSHIN_RECV_JMA_S_ENABLE and self._last_image_url is not None
                and self._last_image_at is not None
                and time.monotonic() - self._last_image_at <= max(10.0, 3 * KYOSHIN_RECV_IMAGE_INTERVAL_SEC)):
            jma_s_url = self._last_image_url
        lmoni_url = None
        if KYOSHIN_RECV_LMONI_ENABLE:
            lmoni_url = await self._dual_image_fetcher.fetch_lmoni_url(self.session)
        if not jma_s_url and not lmoni_url and vib_level is None:
            return

        # 振動レベル音。Discord送信より先に鳴らす（送信待ちで音を遅らせないため）。
        # EEW通知時は常に、検知通知時は KYOSHIN_DETECT_VIBRATION_SOUND が有効なとき鳴らす
        if vib_level is not None and (eew or KYOSHIN_DETECT_VIBRATION_SOUND):
            await self._play_vibration_sound(vib_level)

        if not KYOSHIN_RECV_VIBRATION_ENABLE:
            level_str = ""
        elif vib_level is not None:
            level_str = f"**振動レベル: {vib_level}**\n"
        else:
            level_str = "**振動レベル: 取得中...**\n"

        if event is not None:
            color = shindo_to_color(event.max_shindo)
        elif self._latest_max_shindo is not None:
            color = shindo_to_color(self._latest_max_shindo)
        elif vib_level is not None:
            color = vibration_to_color(vib_level)
        else:
            color = shindo_to_color(None)

        if eew:
            title = "強震モニタ"
            description = level_str + "\n※気象庁からの情報ではありません。あくまで参考値としてお使いください。"
            footer = None
        elif event is not None:
            title = "強震モニタ（画像解析検知）"
            description = (
                f"{PHASE_LABEL_JA.get(event.phase, '揺れを検知')}\n{level_str}\n"
                f"※気象庁公式の情報ではありません。強震モニタ画像の解析による参考情報です。"
            )
            footer = "防災科研 強震モニタ (jma_s / abrspmx_s) の画像解析による自動検知"
        else:
            title = "強震モニタ（振動レベル検知）"
            description = (
                f"振動レベルが{KYOSHIN_VIBRATION_DETECT_LEVEL}以上です\n{level_str}\n"
                f"※気象庁公式の情報ではありません。振動レベルによる参考情報です。"
            )
            footer = "振動レベル (kwatch-24h.net) による自動検知"

        embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now())
        if jma_s_url:
            embed.set_image(url=jma_s_url)
        if lmoni_url:
            embed.set_thumbnail(url=lmoni_url)
        if footer:
            embed.set_footer(text=footer)

        await channel.send(embed=embed)
        key = "long_period_monitor" if eew else "kyoshin"
        self._last_recv[key] = datetime.now()
        self._recv_count[key] += 1

        if event is None:
            return

        # 【2026-09-22 追加】検知→初回通知の遅延の計測ログ（イベントごとに1回）。
        # 「他のソフトより通知が遅い」原因が、検知アルゴリズム側（イベント
        # 生成そのものが遅い）か、通知ゲート（観測点数・フェーズ）待ちか、
        # 画像自体の古さかを、実機ログから切り分けるための情報。
        if event.event_id not in self._notified_events:
            now_loop = asyncio.get_running_loop().time()
            # マージで消えたイベントは _on_event_ended が呼ばれず残るため、
            # 溜まり続けないよう古いものから間引く。
            if len(self._notified_events) >= 100:
                for old_id in sorted(self._notified_events, key=self._notified_events.get)[:50]:
                    del self._notified_events[old_id]
            self._notified_events[event.event_id] = now_loop
            image_age = (
                f"{(datetime.now(JST) - self._last_image_time_jst).total_seconds():.1f}秒"
                if self._last_image_time_jst is not None else "不明"
            )
            logger.info(
                f"KyoshinMonitorCog: イベント {event.event_id[:8]} 初回通知 "
                f"(イベント生成→通知={now_loop - event.created_at:.1f}秒, "
                # 【2026-10-03修正】防災科研の利用規約に配慮し、強震モニタ由来の
                # 実測震度の数値はログに出力しない。
                f"検出観測点={len(event.member_station_ids)}件, フェーズ={event.phase}, "
                f"解析画像の古さ={image_age})"
            )

    async def _play_vibration_sound(self, vib_level: int) -> None:
        """
        振動レベルに応じた効果音（lv100/lv1000/lv2000）を鳴らす。

        通知ループは1秒間隔で繰り返し呼ばれるため、そのまま毎回キューへ
        積むと再生が実時間より遅れて溜まっていく（MP3の再生時間の方が
        長い）。共通AudioCogのMP3キューに未再生の音が残っている間は
        積み増さず、EEWの効果音（high_alert等）を待たせないようにもする。
        """
        key = VIBRATION_TIER_MP3.get(vibration_tier(vib_level))
        if key is None:
            return
        audio_cog = self.bot.get_cog("AudioCog")
        if audio_cog is not None and audio_cog.mp3_queue.qsize() > 0:
            logger.debug(f"KyoshinMonitorCog: 再生待ちの音声があるため振動レベル音をスキップ ({key})")
            return
        await self.play_mp3(key)

    async def _on_event_ended(self, event_id: str) -> None:
        self._notified_events.pop(event_id, None)
        logger.info(f"KyoshinMonitorCog: イベント {event_id[:8]} の揺れ検知が終了しました")

    # ===============================
    # EEWとの連携（防災科研への負荷軽減）
    # ===============================
    def _is_eew_active(self) -> bool:
        """
        現在、EEW（緊急地震速報）が発表中（第一報〜最終報/キャンセル報、または
        通知の上限時間まで）かどうかを返す。

        EewCog.monitored_event_id は、EEW第一報検知時に設定され、最終報・キャンセル報、
        または EEW_MONITOR_MAX_SEC 秒の経過で None に戻る（cogs/eew.py 参照）。
        これをそのままEEW発表中かどうかの判定に使う。EewCog が未登録の場合は
        安全側に倒して False（EEWは発表されていない扱い）を返す。

        【2026-10-05 変更】以前はこの判定で画像解析検知の取得そのものを止め、
        EewCog.vibration_monitor_loop に取得を任せていた。取得・通知をこのCogに集約
        したため、現在は「検知による通知を中断するか」の判定にだけ使う。
        """
        eew_cog = self.bot.get_cog("EewCog")
        if eew_cog is None:
            return False
        return getattr(eew_cog, "monitored_event_id", None) is not None
