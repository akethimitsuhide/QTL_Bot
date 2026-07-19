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

【Discord通知の内容について】
- リアルタイム震度の具体的な数値は表示しない
  （防災科研の利用規約に配慮し、定性的なフェーズ名のみを表示する）
- 検出観測点（グリッドセル）数が1件以下の場合は「検出していない」扱いとし、
  通知を送信しない（孤立した末端セルだけが残っている状態を
  誤って継続通知しないようにするため）
"""
import io
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
import aiohttp

from core.config import CHANNEL_ID, KYOSHIN_CHANNEL_ID, ENABLE_KYOSHIN
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

# NIEDの画像URLはJST基準で命名されているため、サーバーのローカルタイムゾーン
# 設定（例: ラズパイがUTCのまま等）に依存させず、常に明示的にJSTで計算する。
JST = ZoneInfo("Asia/Tokyo")

GRID_SIZE = 10

IMAGE_DELAY_SEC = 6
IMAGE_STEP_SEC = 3
IMAGE_MAX_RETRY = 4

# 通知抑止の最小観測点数（これ以下は「検出していない」扱いにする）
MIN_STATIONS_FOR_NOTIFICATION = 2

# 定性的なフェーズ名の日本語表示（震度の具体的数値は一切出さない）
PHASE_LABEL_JA = {
    "Weaker":   "微弱な揺れの可能性",
    "Weak":     "弱い揺れの可能性",
    "Medium":   "揺れを検知",
    "Strong":   "強い揺れを検知",
    "Stronger": "非常に強い揺れを検知",
}


class KyoshinMonitorCog(commands.Cog):
    """強震モニタ画像の解析（HSVマスク＋クラスタリング＋複数フレーム検証）による揺れ検知・Discord通知を行う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel = None
        self.kyoshin_channel = None
        self.session: aiohttp.ClientSession | None = None

        self.analyzer = KyoshinImageAnalyzer(grid_size=GRID_SIZE)
        self.cluster_tracker = ClusterTracker()
        self._stations_registered = False
        self._last_image_url: str | None = None

        self.monitor = KyoshinImageMonitor(
            get_readings=self._fetch_current_shindo_map,
            send_kyoshin_image=self._send_kyoshin_image,
            on_event_ended=self._on_event_ended,
            config=DetectorConfig(),
            poll_interval_sec=2.0,
            image_interval_sec=3.0,
            use_confirmed_ingest=True,  # ClusterTracker確定済みセルのみ受け取る
        )
        self._monitor_task = None

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
        logger.info(f"KyoshinMonitorCog: 疑似観測点を{len(grid)}件登録しました（grid_size={GRID_SIZE}px）")

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

        # confirmed なセルのみ、震度値と共に返す
        shindo_by_cell = {r.cell_id: r.shindo for r in active_readings}
        return {
            cell_id: shindo_by_cell[cell_id]
            for cell_id in tracker_result.confirmed_cell_ids
            if cell_id in shindo_by_cell
        }

    async def _download_latest_image(self) -> tuple[bytes | None, str | None]:
        """
        現在時刻から少し過去に遡りながら、実際に存在する最新の jma_s 画像を探してダウンロードする。
        """
        for i in range(IMAGE_MAX_RETRY):
            dt = datetime.now(JST) - timedelta(seconds=IMAGE_DELAY_SEC + IMAGE_STEP_SEC * i)
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

        検出観測点数が MIN_STATIONS_FOR_NOTIFICATION 未満の場合は
        「検出していない」扱いとして通知を送らない。
        リアルタイム震度の具体的な数値は防災科研の利用規約に配慮し表示しない。
        """
        member_count = len(event.member_station_ids)
        if member_count < MIN_STATIONS_FOR_NOTIFICATION:
            logger.debug(
                f"KyoshinMonitorCog: 観測点数{member_count}件のため通知をスキップします"
                f"（検出していない扱い、閾値={MIN_STATIONS_FOR_NOTIFICATION}）"
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
