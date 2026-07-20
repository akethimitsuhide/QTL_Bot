"""
core/kyoshin_shared.py
========================
強震モニタ関連の通知（EEW発表時の振動モニタ / 画像解析による揺れ検知）
の両方から共通して使う部品を集約するモジュール。

【このモジュールが存在する理由】
以前は cogs/quake.py の vibration_monitor_loop（EEW発表時トリガー）と
cogs/kyoshin_monitor.py（画像解析による揺れ検知トリガー）が、
それぞれ独立に「jma_s系統・abrspmx_s系統の画像取得」「振動レベル取得」
のロジックを持っていた。両者の通知仕様（震度に応じた色分け・
取得間隔）を統一するにあたり、ロジックの二重管理を避けるため、
共通部分をこのモジュールに集約した。

含まれる機能:
1. JMA_S_SHINDO_COLORS: jma_s系統の実震度からDiscord Embed色を
   決定する独自カラーマップ
2. estimate_max_shindo_from_image(): jma_s画像から画面内の
   最大実震度を推定する（KyoshinImageAnalyzerのHSV変換ロジックを流用）
3. DualImageFetcher: jma_s系統・abrspmx_s(LMoni)系統の両画像を
   直近キャッシュ付きで取得するヘルパークラス
4. fetch_vibration_level(): kwatch-24h.net から現在の振動レベルを取得
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from core.kyoshin_image_analyzer import KyoshinImageAnalyzer

logger = logging.getLogger("QTLBot")

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# NIED/防災科研の画像URLはJST基準で命名されているため、常に明示的にJSTで計算する。
JST = ZoneInfo("Asia/Tokyo")

JMA_S_BASE = "https://smi.lmoniexp.bosai.go.jp/data/map_img/RealTimeImg/jma_s"
LMONI_BASE = "https://www.lmoni.bosai.go.jp/monitor/data/data/map_img/RealTimeImg/abrspmx_s"
KWATCH_URL = "https://kwatch-24h.net/EQLevel.json"

# 画像検索の遅延・ステップ・最大リトライ（両系統共通）
IMAGE_DELAY_SEC = 4
IMAGE_STEP_SEC = 3
IMAGE_MAX_RETRY = 4

# ===============================
# 震度に応じた独自カラーマップ（jma_s系統の実震度ベース）
# ===============================
# 気象庁の公式な震度階級色とは別に、Bot独自の通知色として定義する。
# 実震度(shindo)がキー以上であれば、その色を採用する（降順に判定）。
JMA_S_SHINDO_COLORS: list[tuple[float, int]] = [
    (6.5, 0x8B00FF),   # 震度7相当   紫
    (5.5, 0xFF0000),   # 震度6強相当 赤
    (4.5, 0xFF4500),   # 震度6弱相当 橙赤
    (3.5, 0xFF8C00),   # 震度5強相当 橙
    (2.5, 0xFFD700),   # 震度5弱相当 金
    (1.5, 0xFFFF00),   # 震度4相当   黄
    (0.5, 0x00FF7F),   # 震度3相当   黄緑〜緑
    (-0.5, 0x00CED1),  # 震度2相当   水色
    (-999.0, 0x4682B4),  # 震度1未満（震度0相当以下） 青（デフォルト）
]


def shindo_to_color(shindo: float | None) -> int:
    """
    実震度(shindo)から独自カラーマップに基づくDiscord Embed色を返す。
    shindo が None（未取得）の場合はグレーを返す。
    """
    if shindo is None:
        return 0x808080
    for floor, color in JMA_S_SHINDO_COLORS:
        if shindo >= floor:
            return color
    return JMA_S_SHINDO_COLORS[-1][1]


# jma_s画像からの震度推定専用アナライザ（グリッド分割は使わず画面全体で判定するため grid_size は大きめにする）
_shindo_estimator = KyoshinImageAnalyzer(grid_size=10)


def estimate_max_shindo_from_image(image_bytes: bytes) -> float | None:
    """
    jma_s画像のバイト列から、画面内の最大実震度を推定する。
    画像デコードに失敗した場合や、揺れ候補ピクセルが存在しない
    （＝震度1相当未満のみ）場合は None を返す。
    """
    if not _PIL_AVAILABLE:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        logger.debug(f"kyoshin_shared: 震度推定用の画像デコードに失敗しました: {e}")
        return None

    readings = _shindo_estimator.analyze(img)
    if not readings:
        return None
    return max(r.shindo for r in readings)


@dataclass
class DualImageFetcher:
    """
    jma_s系統・abrspmx_s(LMoni)系統の両画像を、直近キャッシュ付きで取得する。

    同一秒のタイムスタンプへの重複リクエストを避けるため、
    直前に見つかった画像のURLとタイムスタンプを保持する。
    session（aiohttp.ClientSession）は呼び出し側から都度渡す。
    """
    _last_jma_s_url: str | None = field(default=None, init=False)
    _last_lmoni_url: str | None = field(default=None, init=False)
    _last_jma_s_ts: str = field(default="", init=False)
    _last_lmoni_ts: str = field(default="", init=False)

    async def _find_image(
        self, session: aiohttp.ClientSession, base_url: str, suffix: str,
        last_url: str | None, last_ts: str,
    ) -> tuple[str | None, str]:
        for i in range(IMAGE_MAX_RETRY):
            dt = datetime.now(JST) - timedelta(seconds=IMAGE_DELAY_SEC + IMAGE_STEP_SEC * i)
            ts = dt.strftime("%Y%m%d%H%M%S")
            if ts == last_ts:
                return last_url, last_ts
            url = f"{base_url}/{dt.strftime('%Y%m%d')}/{ts}.{suffix}.gif"
            try:
                async with session.head(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        return url, ts
            except Exception:
                pass
        return None, last_ts

    async def fetch_urls(self, session: aiohttp.ClientSession) -> tuple[str | None, str | None]:
        """
        (jma_s画像URL, LMoni画像URL) のタプルを返す。
        見つからなかった系統は None になる。
        """
        self._last_jma_s_url, self._last_jma_s_ts = await self._find_image(
            session, JMA_S_BASE, "jma_s", self._last_jma_s_url, self._last_jma_s_ts
        )
        self._last_lmoni_url, self._last_lmoni_ts = await self._find_image(
            session, LMONI_BASE, "abrspmx_s", self._last_lmoni_url, self._last_lmoni_ts
        )
        return self._last_jma_s_url, self._last_lmoni_url

    async def fetch_jma_s_bytes(self, session: aiohttp.ClientSession) -> bytes | None:
        """
        直近で見つかった jma_s画像URLの中身をダウンロードする
        （震度推定用。fetch_urls() を先に呼んでURLを更新しておくこと）。
        """
        if not self._last_jma_s_url:
            return None
        try:
            async with session.get(
                self._last_jma_s_url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception as e:
            logger.debug(f"kyoshin_shared: jma_s画像ダウンロード失敗: {e}")
        return None


async def fetch_vibration_level(session: aiohttp.ClientSession) -> int | None:
    """
    kwatch-24h.net から現在の振動レベル(0以上の整数)を取得する。
    取得に失敗した場合は None を返す。
    """
    try:
        async with session.get(KWATCH_URL, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return int(data.get("l", 0))
    except Exception as e:
        logger.debug(f"kyoshin_shared: 振動レベル取得エラー: {e}")
    return None
