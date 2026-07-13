"""
cogs/kyoshin_monitor.py
========================
core.kyoshin_detector / core.kyoshin_image_monitor / core.kyoshin_image_analyzer
を統合し、強震モニタ画像の色相解析による揺れ検知と、
Discord への画像通知を実際に行う Cog。

【画像取得元】
https://smi.lmoniexp.bosai.go.jp/data/map_img/RealTimeImg/jma_s/{YYYYMMDD}/{YYYYMMDDHHMMSS}.jma_s.gif
（防災科研 リアルタイム震度モニタの公開画像。系統は jma_s のみを使用する）

【グリッド分割による疑似観測点方式】
画像を KyoshinImageAnalyzer でグリッド分割し、各セルの色相から
震度を算出、セル座標を「疑似観測点ID」として EventManager に供給する。
全国の実観測点座標データは使用していない
（詳細は core/kyoshin_image_analyzer.py のdocstring参照）。

【色相→震度変換テーブルの較正】
core.kyoshin_image_analyzer.HUE_TO_SHINDO_ANCHORS は、
防災科研公式のカラースケール画像（震度7[赤]〜震度-3[青]の凡例）を
実際にピクセル解析して作成した実データ較正済みテーブル。

【新規の依存パッケージ】
Pillow (画像デコード用)。requirements.txt に追加が必要。
"""
import io
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands
import aiohttp

from core.config import CHANNEL_ID, KYOSHIN_CHANNEL_ID, ENABLE_KYOSHIN
from core.kyoshin_detector import DetectorConfig, SeismicEvent
from core.kyoshin_image_monitor import KyoshinImageMonitor
from core.kyoshin_image_analyzer import KyoshinImageAnalyzer

logger = logging.getLogger("QTLBot")

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

JMA_S_BASE = "https://smi.lmoniexp.bosai.go.jp/data/map_img/RealTimeImg/jma_s"

# グリッド分割の粒度（ピクセル）。画像サイズは 352x400 を前提。
GRID_SIZE = 10

# 画像は数秒〜十数秒遅れで生成されるため、取得時は少し過去の時刻から遡って探す。
IMAGE_DELAY_SEC = 6
IMAGE_STEP_SEC = 3
IMAGE_MAX_RETRY = 4


class KyoshinMonitorCog(commands.Cog):
    """強震モニタ画像の色相解析による揺れ検知・Discord通知を行う Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel = None
        self.kyoshin_channel = None
        self.session: aiohttp.ClientSession | None = None

        self.analyzer = KyoshinImageAnalyzer(grid_size=GRID_SIZE)
        self._stations_registered = False
        self._last_image_url: str | None = None  # 直近取得できた画像URL（通知用）

        self.monitor = KyoshinImageMonitor(
            get_readings=self._fetch_current_shindo_map,
            send_kyoshin_image=self._send_kyoshin_image,
            on_event_ended=self._on_event_ended,
            config=DetectorConfig(),
            poll_interval_sec=2.0,
            image_interval_sec=3.0,
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
            logger.info("KyoshinMonitorCog: 監視ループを開始しました（グリッド分割による疑似観測点方式）")

    # ===============================
    # 画像取得・解析
    # ===============================
    def _register_stations_once(self, image_width: int, image_height: int) -> None:
        """初回のみ、画像サイズに基づいてグリッド全セルを疑似観測点として登録する。"""
        if self._stations_registered:
            return
        grid = self.analyzer.build_station_grid(image_width, image_height)
        for cell_id, neighbors in grid.items():
            self.monitor.event_manager.register_station(cell_id, neighbors=neighbors)
        self._stations_registered = True
        logger.info(f"KyoshinMonitorCog: 疑似観測点を{len(grid)}件登録しました（grid_size={GRID_SIZE}px）")

    async def _fetch_current_shindo_map(self) -> dict[str, float]:
        """
        強震モニタ画像(jma_s系統)を取得し、グリッドセルごとの震度を解析して返す。
        画像が取得できない場合は空の dict を返す（EventManager側は変化なしとして扱う）。
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

        readings = self.analyzer.analyze(img)
        return {r.cell_id: r.shindo for r in readings}

    async def _download_latest_image(self) -> tuple[bytes | None, str | None]:
        """
        現在時刻から少し過去に遡りながら、実際に存在する最新の jma_s 画像を探してダウンロードする。
        URL例: https://smi.lmoniexp.bosai.go.jp/data/map_img/RealTimeImg/jma_s/20260711/20260711234106.jma_s.gif
        """
        for i in range(IMAGE_MAX_RETRY):
            dt = datetime.now() - timedelta(seconds=IMAGE_DELAY_SEC + IMAGE_STEP_SEC * i)
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
        """揺れ検知イベントが継続中、強震モニタ画像をDiscordに送信する。"""
        channel = self.kyoshin_channel or self.channel
        if not channel:
            return
        if not self._last_image_url:
            return

        embed = discord.Embed(
            title="強震モニタ（画像解析検知）",
            description=(
                f"推定リアルタイム震度: {event.max_shindo:.1f} (フェーズ: {event.phase})\n"
                f"検知観測点数: {len(event.member_station_ids)}\n\n"
                f"※気象庁公式の情報ではありません。強震モニタ画像の色相解析による"
                f"参考値です。詳細は `!status` 等で確認してください。"
            ),
            color=0xFF6600,
            timestamp=datetime.now(),
        )
        embed.set_image(url=self._last_image_url)
        embed.set_footer(text="防災科研 強震モニタ (jma_s) の色相解析による自動検知")

        await channel.send(embed=embed)

    async def _on_event_ended(self, event_id: str) -> None:
        logger.info(f"KyoshinMonitorCog: イベント {event_id[:8]} の揺れ検知が終了しました")