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
https://smi.lmoniexp.bosai.go.jp/data/map_img/RealTimeImg/jma_s/{YYYYMMDD}/{YYYYMMDDHHMMSS}.jma_s.gif
（防災科研 リアルタイム震度モニタの公開画像。系統は jma_s のみを使用する）

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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
import aiohttp

from core.config import (
    CHANNEL_ID, KYOSHIN_CHANNEL_ID, ENABLE_KYOSHIN,
    KYOSHIN_IMAGE_DELAY_SEC, KYOSHIN_IMAGE_STEP_SEC,
    KYOSHIN_IMAGE_MAX_RETRY, KYOSHIN_POLL_INTERVAL_SEC, KYOSHIN_NOTIFY_INTERVAL_SEC,
    KYOSHIN_ACTIVE_SHINDO_FLOOR,
    KYOSHIN_RISE_THRESHOLD, KYOSHIN_NEIGHBOR_TRIGGER_COUNT,
    KYOSHIN_BASELINE_WINDOW_START_SEC, KYOSHIN_BASELINE_WINDOW_END_SEC,
    KYOSHIN_HISTORY_WINDOW_SEC, KYOSHIN_EVENT_TIMEOUT_SEC,
    KYOSHIN_MIN_NOTIFY_PHASE, KYOSHIN_MIN_STATIONS_SHINDO0, KYOSHIN_MIN_STATIONS_SHINDO1,
    KYOSHIN_DEBUG_SAVE_IMAGE, KYOSHIN_DEBUG_IMAGE_DIR,
    KYOSHIN_STATIONS_SOURCE_URL, KYOSHIN_STATIONS_CACHE_PATH,
    KYOSHIN_STATIONS_REFRESH_SEC, KYOSHIN_NEIGHBOR_K,
)
from core.kyoshin_detector import DetectorConfig, SeismicEvent
from core.kyoshin_image_monitor import KyoshinImageMonitor
from core.kyoshin_stations import (
    StationStore, KyoshinStationMeta, build_k_nearest_neighbors, make_shindo_decoder,
)
from core.kyoshin_shared import (
    DualImageFetcher, fetch_vibration_level, shindo_to_color,
)

logger = logging.getLogger("QTLBot")

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

JMA_S_BASE = "https://smi.lmoniexp.bosai.go.jp/data/map_img/RealTimeImg/jma_s"

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


class KyoshinMonitorCog(commands.Cog):
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

        self._last_image_url: str | None = None
        self._dual_image_fetcher = DualImageFetcher()

        self.monitor = KyoshinImageMonitor(
            get_readings=self._fetch_current_shindo_map,
            send_kyoshin_image=self._send_kyoshin_image,
            on_event_ended=self._on_event_ended,
            config=DetectorConfig(
                rise_threshold=KYOSHIN_RISE_THRESHOLD,
                neighbor_trigger_count=KYOSHIN_NEIGHBOR_TRIGGER_COUNT,
                baseline_window_start_sec=KYOSHIN_BASELINE_WINDOW_START_SEC,
                baseline_window_end_sec=KYOSHIN_BASELINE_WINDOW_END_SEC,
                history_window_sec=KYOSHIN_HISTORY_WINDOW_SEC,
                event_timeout_sec=KYOSHIN_EVENT_TIMEOUT_SEC,
            ),
            poll_interval_sec=KYOSHIN_POLL_INTERVAL_SEC,
            image_interval_sec=KYOSHIN_NOTIFY_INTERVAL_SEC,
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
        if self._stations_refresh_task and not self._stations_refresh_task.done():
            self._stations_refresh_task.cancel()
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

        if not _PIL_AVAILABLE:
            logger.error(
                "KyoshinMonitorCog: Pillow (PIL) がインストールされていないため起動できません。"
                "requirements.txt の Pillow を pip install してください。"
            )
            return

        await self._init_stations()

        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = self.bot.loop.create_task(self.monitor.run())
            logger.info(
                "KyoshinMonitorCog: 監視ループを開始しました"
                "（震度の時系列上昇幅＋K近傍同時上昇の検証方式・実観測点データ使用）"
            )

        if self._stations_refresh_task is None or self._stations_refresh_task.done():
            self._stations_refresh_task = self.bot.loop.create_task(self._stations_refresh_loop())

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

        画像が取得できない場合、または観測点データが未初期化の場合は
        空の dict を返す（EventManagerには何もフィードされず、その
        フレームの観測値は欠測扱いになる）。
        """
        if not _PIL_AVAILABLE or not self._stations_initialized:
            return {}

        image_bytes, url = await self._download_latest_image()
        if image_bytes is None:
            return {}

        self._last_image_url = url

        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except Exception as e:
            logger.warning(f"KyoshinMonitorCog: 画像デコードに失敗しました: {e}")
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
            r, g, b = pixels[x, y]
            shindo_map[code] = self._shindo_from_rgb(r, g, b)

        if KYOSHIN_DEBUG_SAVE_IMAGE:
            active_count = sum(
                1 for v in shindo_map.values()
                if v >= KYOSHIN_ACTIVE_SHINDO_FLOOR
            )
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
    # Discord通知
    # ===============================
    async def _send_kyoshin_image(self, event: SeismicEvent) -> None:
        """
        揺れ検知イベントが継続中、強震モニタの通知をDiscordに送信する。

        通知に必要な最小観測点数は実震度によって切り替える
        （震度0相当: KYOSHIN_MIN_STATIONS_SHINDO0 件以上、
         震度1相当以上: KYOSHIN_MIN_STATIONS_SHINDO1 件以上）。
        観測点数そのものは通知本文には表示しない（内部の閾値判定にのみ使用）。

        また event.phase が KYOSHIN_MIN_NOTIFY_PHASE より弱い場合も
        通知を送らない（検知自体は継続するが、通知だけ抑制する）。

        通知には jma_s系統・abrspmx_s(LMoni)系統の両画像と、
        kwatch-24h.netの振動レベルを含める。通知色は jma_s系統の
        実震度(event.max_shindo)に基づく独自カラーマップで決定する。
        """
        member_count = len(event.member_station_ids)
        min_stations = (
            KYOSHIN_MIN_STATIONS_SHINDO1
            if event.max_shindo >= 1.0
            else KYOSHIN_MIN_STATIONS_SHINDO0
        )
        if member_count < min_stations:
            logger.debug(
                f"KyoshinMonitorCog: 観測点数{member_count}件のため通知をスキップします"
                f"（検出していない扱い、実震度={event.max_shindo:.2f}、閾値={min_stations}）"
            )
            return

        if _phase_index(event.phase) < _phase_index(KYOSHIN_MIN_NOTIFY_PHASE):
            logger.debug(
                f"KyoshinMonitorCog: フェーズ{event.phase}は"
                f"KYOSHIN_MIN_NOTIFY_PHASE={KYOSHIN_MIN_NOTIFY_PHASE}未満のため通知をスキップします"
            )
            return

        channel = self.kyoshin_channel or self.channel
        if not channel:
            return

        jma_s_url, lmoni_url = await self._dual_image_fetcher.fetch_urls(self.session)
        if not jma_s_url and not lmoni_url:
            return

        vib_level = await fetch_vibration_level(self.session)

        phase_label = PHASE_LABEL_JA.get(event.phase, "揺れを検知")
        color = shindo_to_color(event.max_shindo)

        level_str = f"**振動レベル: {vib_level}**\n" if vib_level is not None else "**振動レベル: 取得中...**\n"

        embed = discord.Embed(
            title="強震モニタ（画像解析検知）",
            description=(
                f"{phase_label}\n"
                f"{level_str}\n"
                f"※気象庁公式の情報ではありません。強震モニタ画像の解析による"
                f"参考情報です。"
            ),
            color=color,
            timestamp=datetime.now(),
        )
        if jma_s_url:
            embed.set_image(url=jma_s_url)
        if lmoni_url:
            embed.set_thumbnail(url=lmoni_url)
        embed.set_footer(text="防災科研 強震モニタ (jma_s / abrspmx_s) の画像解析による自動検知")

        await channel.send(embed=embed)

    async def _on_event_ended(self, event_id: str) -> None:
        logger.info(f"KyoshinMonitorCog: イベント {event_id[:8]} の揺れ検知が終了しました")
