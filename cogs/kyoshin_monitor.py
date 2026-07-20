"""
cogs/kyoshin_monitor.py
========================
core.kyoshin_detector / core.kyoshin_image_monitor / core.kyoshin_image_analyzer /
core.kyoshin_cluster_tracker を統合し、強震モニタ画像の解析による
揺れ検知と、Discord への画像通知を実際に行う Cog。

【検知パイプライン（方針転換後）】
1. KyoshinImageAnalyzer.analyze(): 画像をHSVマスク処理し、
   震度1相当以上の色を持つピクセルが一定数集中しているグリッドセルのみ抽出
   （背景の青色域は最初から解析対象に含めない）
2. ClusterTracker.update(): 抽出されたアクティブセル群を8近傍で連結成分化し、
   最小サイズ以上・複数フレーム連続で持続しているクラスタのみ confirmed とする
3. EventManager.ingest_confirmed(): confirmed 済みのセルのみを直接取り込む
   （EventManager独自の敏感な上昇トリガー・近隣検証はバイパスする）

参考:
- https://qiita.com/ingen084/items/82985e8d3227c97c608d
- ユーザー提供の詳細仕様書（HSVマスク・DBSCANクラスタリング・
  複数フレーム検証を組み合わせた揺れ検知アルゴリズム）

【画像取得元】
https://smi.lmoniexp.bosai.go.jp/data/map_img/RealTimeImg/jma_s/{YYYYMMDD}/{YYYYMMDDHHMMSS}.jma_s.gif
（防災科研 リアルタイム震度モニタの公開画像。系統は jma_s のみを使用する）

画像の時刻決定には、まず以下の latest.json API から実際に配信されている
最新時刻(latest_time)を取得し、その時刻をもとに画像URLを構築する
（従来の「現在時刻から遡ってリトライ探索する」方式は、latest.json取得に
失敗した場合のフォールバックとしてのみ使用する）。
https://smi.lmoniexp.bosai.go.jp/webservice/server/pros/latest.json

【Discord通知の内容について】
- リアルタイム震度の具体的な数値は表示しない
  （防災科研の利用規約に配慮し、定性的なフェーズ名のみを表示する）
- 検出観測点（グリッドセル）数が1件以下の場合は「検出していない」扱いとし、
  通知を送信しない（孤立した末端セルだけが残っている状態を
  誤って継続通知しないようにするため）
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
    KYOSHIN_MIN_CLUSTER_SIZE, KYOSHIN_REQUIRED_FRAMES, KYOSHIN_MIN_ACTIVE_PIXELS,
    KYOSHIN_MIN_NOTIFY_PHASE, KYOSHIN_MIN_STATIONS_FOR_NOTIFICATION,
    KYOSHIN_DEBUG_SAVE_IMAGE, KYOSHIN_DEBUG_IMAGE_DIR,
)
from core.kyoshin_detector import DetectorConfig, SeismicEvent
from core.kyoshin_image_monitor import KyoshinImageMonitor
from core.kyoshin_image_analyzer import KyoshinImageAnalyzer
from core.kyoshin_cluster_tracker import ClusterTracker

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
    """強震モニタ画像の解析（HSVマスク＋クラスタリング＋複数フレーム検証）による揺れ検知・Discord通知を行う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel = None
        self.kyoshin_channel = None
        self.session: aiohttp.ClientSession | None = None

        self.analyzer = KyoshinImageAnalyzer(
            grid_size=KYOSHIN_GRID_SIZE,
            min_active_pixels=KYOSHIN_MIN_ACTIVE_PIXELS,
        )
        self.cluster_tracker = ClusterTracker(
            min_cluster_size=KYOSHIN_MIN_CLUSTER_SIZE,
            required_consecutive_frames=KYOSHIN_REQUIRED_FRAMES,
        )
        self._stations_registered = False
        self._last_image_url: str | None = None

        self.monitor = KyoshinImageMonitor(
            get_readings=self._fetch_current_shindo_map,
            send_kyoshin_image=self._send_kyoshin_image,
            on_event_ended=self._on_event_ended,
            config=DetectorConfig(),
            poll_interval_sec=KYOSHIN_POLL_INTERVAL_SEC,
            image_interval_sec=KYOSHIN_NOTIFY_INTERVAL_SEC,
            use_confirmed_ingest=True,  # ClusterTracker確定済みセルのみ受け取る
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
                "（HSVマスク＋クラスタリング＋複数フレーム検証方式）"
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
        強震モニタ画像(jma_s系統)を取得し、
        1. HSVマスク処理でアクティブセルを抽出(KyoshinImageAnalyzer)
        2. クラスタリング＋複数フレーム検証(ClusterTracker)
        を経て、confirmed(確定)されたセルのみを返す。

        画像が取得できない、または confirmed なセルが無い場合は空の dict を返す。
        """
        if not _PIL_AVAILABLE:
            return {}

        image_bytes, url = await self._download_latest_image()
        if image_bytes is None:
            # 画像取得失敗時もクラスタ追跡は更新しておく（空フレームとして扱い、
            # 継続中のクラスタが「消失」とみなされ streak がリセットされるのを許容する）
            self.cluster_tracker.update([])
            return {}

        self._last_image_url = url

        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except Exception as e:
            logger.warning(f"KyoshinMonitorCog: 画像デコードに失敗しました: {e}")
            self.cluster_tracker.update([])
            return {}

        self._register_stations_once(img.width, img.height)

        # Stage1: HSVマスク処理によるアクティブセル抽出
        active_readings = self.analyzer.analyze(img)

        # Stage2+3: クラスタリング＋複数フレーム持続確認
        tracker_result = self.cluster_tracker.update(active_readings)

        if not tracker_result.confirmed_cell_ids:
            return {}

        if KYOSHIN_DEBUG_SAVE_IMAGE:
            self._save_debug_image(image_bytes, len(tracker_result.confirmed_cell_ids))

        # confirmed なセルのみ、震度値と共に返す
        shindo_by_cell = {r.cell_id: r.shindo for r in active_readings}
        return {
            cell_id: shindo_by_cell[cell_id]
            for cell_id in tracker_result.confirmed_cell_ids
            if cell_id in shindo_by_cell
        }

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
        揺れ検知イベントが継続中、強震モニタ画像をDiscordに送信する。

        検出観測点数が KYOSHIN_MIN_STATIONS_FOR_NOTIFICATION 未満の場合は
        「検出していない」扱いとして通知を送らない。
        また event.phase が KYOSHIN_MIN_NOTIFY_PHASE より弱い場合も
        通知を送らない（検知自体は継続するが、通知だけ抑制する）。
        リアルタイム震度の具体的な数値は防災科研の利用規約に配慮し表示しない。
        """
        member_count = len(event.member_station_ids)
        if member_count < KYOSHIN_MIN_STATIONS_FOR_NOTIFICATION:
            logger.debug(
                f"KyoshinMonitorCog: 観測点数{member_count}件のため通知をスキップします"
                f"（検出していない扱い、閾値={KYOSHIN_MIN_STATIONS_FOR_NOTIFICATION}）"
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
        if not self._last_image_url:
            return

        phase_label = PHASE_LABEL_JA.get(event.phase, "揺れを検知")

        embed = discord.Embed(
            title="強震モニタ（画像解析検知）",
            description=(
                f"{phase_label}\n"
                f"検知観測点数: {member_count}\n\n"
                f"※気象庁公式の情報ではありません。強震モニタ画像の解析による"
                f"参考情報です。具体的な震度の数値はここでは表示していません。"
            ),
            color=0xFF6600,
            timestamp=datetime.now(),
        )
        embed.set_image(url=self._last_image_url)
        embed.set_footer(text="防災科研 強震モニタ (jma_s) の画像解析による自動検知")

        await channel.send(embed=embed)

    async def _on_event_ended(self, event_id: str) -> None:
        logger.info(f"KyoshinMonitorCog: イベント {event_id[:8]} の揺れ検知が終了しました")
