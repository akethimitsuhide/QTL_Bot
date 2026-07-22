"""
cogs/kyoshin_monitor.py
========================
core.kyoshin_detector / core.kyoshin_image_monitor / core.kyoshin_image_analyzer を
統合し、強震モニタ画像の解析による揺れ検知と、Discord への画像通知を
実際に行う Cog。

【検知パイプライン（2026-07-22 方針転換後）】
1. KyoshinImageAnalyzer.analyze_all(): 画像を全グリッドセル（アクティブ・
   非アクティブ問わず）の代表震度に変換する
2. EventManager.ingest(): 各セルの震度の時系列（過去10秒分）を追跡し、
   「上昇幅がしきい値を超えたセル」を検出したら、その近隣セルも
   同時に上昇しているかで真偽を判定する（ingen084氏の記事のアルゴリズム）
3. EventManager.tick(): 判定された観測点群をイベントとしてまとめる

【方針転換の経緯】
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

参考:
- https://qiita.com/ingen084/items/82985e8d3227c97c608d
  （強震モニタの画像から揺れていることを検知する／ingen084氏）

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
- 検出観測点（グリッドセル）数は通知本文には表示しない
  （内部的な閾値判定にのみ使用する）
- 通知に必要な最小観測点数は、実震度（震度0相当 / 震度1相当以上）に
  応じて2段階に分ける（KYOSHIN_MIN_STATIONS_SHINDO0 / SHINDO1）
"""
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
    KYOSHIN_GRID_SIZE, KYOSHIN_IMAGE_DELAY_SEC, KYOSHIN_IMAGE_STEP_SEC,
    KYOSHIN_IMAGE_MAX_RETRY, KYOSHIN_POLL_INTERVAL_SEC, KYOSHIN_NOTIFY_INTERVAL_SEC,
    KYOSHIN_MIN_ACTIVE_PIXELS, KYOSHIN_ACTIVE_SHINDO_FLOOR,
    KYOSHIN_RISE_THRESHOLD, KYOSHIN_NEIGHBOR_TRIGGER_COUNT,
    KYOSHIN_BASELINE_WINDOW_START_SEC, KYOSHIN_BASELINE_WINDOW_END_SEC,
    KYOSHIN_HIGH_VALUE_BYPASS_SHINDO, KYOSHIN_HISTORY_WINDOW_SEC,
    KYOSHIN_MIN_NOTIFY_PHASE, KYOSHIN_MIN_STATIONS_SHINDO0, KYOSHIN_MIN_STATIONS_SHINDO1,
    KYOSHIN_DEBUG_SAVE_IMAGE, KYOSHIN_DEBUG_IMAGE_DIR,
)
from core.kyoshin_detector import DetectorConfig, SeismicEvent
from core.kyoshin_image_monitor import KyoshinImageMonitor
from core.kyoshin_image_analyzer import KyoshinImageAnalyzer
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
    """強震モニタ画像の解析（震度の時系列上昇幅＋近隣同時上昇の検証）による揺れ検知・Discord通知を行う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel = None
        self.kyoshin_channel = None
        self.session: aiohttp.ClientSession | None = None

        self.analyzer = KyoshinImageAnalyzer(
            grid_size=KYOSHIN_GRID_SIZE,
            active_shindo_floor=KYOSHIN_ACTIVE_SHINDO_FLOOR,
            min_active_pixels=KYOSHIN_MIN_ACTIVE_PIXELS,
        )
        self._stations_registered = False
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
                high_value_bypass_shindo=KYOSHIN_HIGH_VALUE_BYPASS_SHINDO,
                history_window_sec=KYOSHIN_HISTORY_WINDOW_SEC,
            ),
            poll_interval_sec=KYOSHIN_POLL_INTERVAL_SEC,
            image_interval_sec=KYOSHIN_NOTIFY_INTERVAL_SEC,
            # EventManager.ingest()（震度の時系列上昇幅＋近隣同時上昇の
            # 検証。ingen084氏の記事のアルゴリズム）を検知の主軸とする。
            # ClusterTracker（静的な絶対震度の隣接判定）は誤検知の
            # 温床だったため使用しない。
            use_confirmed_ingest=False,
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

        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = self.bot.loop.create_task(self.monitor.run())
            logger.info(
                "KyoshinMonitorCog: 監視ループを開始しました"
                "（震度の時系列上昇幅＋近隣同時上昇の検証方式）"
            )

    # ===============================
    # 画像取得・解析（検知パイプライン）
    # ===============================
    def _register_stations_once(self, image_width: int, image_height: int) -> None:
        if self._stations_registered:
            return
        grid = self.analyzer.build_station_grid(image_width, image_height)
        for cell_id, neighbors in grid.items():
            self.monitor.event_manager.register_station(cell_id, neighbors=neighbors)
        self._stations_registered = True
        logger.info(f"KyoshinMonitorCog: 疑似観測点を{len(grid)}件登録しました（grid_size={KYOSHIN_GRID_SIZE}px）")

    async def _fetch_current_shindo_map(self) -> dict[str, float]:
        """
        強震モニタ画像(jma_s系統)を取得し、全グリッドセル（アクティブ・
        非アクティブ問わず）の代表震度を dict[cell_id, shindo] で返す。

        「本物の地震かどうか」の判定はここでは行わない。ここでは
        画像→震度マップへの変換のみを行い、実際の判定（時系列上昇幅の
        追跡・近隣同時上昇の検証）は core.kyoshin_detector.EventManager.
        ingest() / tick()（呼び出し元の core.kyoshin_image_monitor.
        KyoshinImageMonitor.run()）に委ねる。

        画像が取得できない場合は空の dict を返す（EventManagerには
        何もフィードされず、そのフレームの観測値は欠測扱いになる）。
        """
        if not _PIL_AVAILABLE:
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

        self._register_stations_once(img.width, img.height)

        # 全グリッドセル（非アクティブ含む）の代表震度を返す。
        # EventManager.ingest() が時系列の上昇幅を正しく追跡できるよう、
        # 揺れていないセルにも背景相当の代表値を明示的にフィードする。
        shindo_map = self.analyzer.analyze_all(img)

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
