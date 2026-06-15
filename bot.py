import discord
from discord.ext import commands, tasks
import aiohttp
import json
import asyncio
import websockets
import traceback
import os
import time
import hashlib
from typing import Dict, Tuple, Optional
import logging
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv

try:
    import pygame
    pygame.mixer.init()
    _PYGAME_AVAILABLE = True
except Exception as e:
    _PYGAME_AVAILABLE = False
    print(f"[WARNING] pygame.mixer の初期化に失敗しました。MP3再生は無効です: {e}")

# ===============================
# ロガー設定
# ===============================
import logging.handlers as _log_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        _log_handlers.RotatingFileHandler(
            "qtlbot.log",
            encoding="utf-8",
            maxBytes=5 * 1024 * 1024,  # 1ファイル最大 5MB
            backupCount=3,              # 最大 3世代（qtlbot.log, .1, .2, .3）保持
        ),
    ],
)
logger = logging.getLogger("QTLBot")

# ===============================
# 環境変数読み込み & ヘルパー関数
# ===============================
load_dotenv()

# ===== ロギング設定定数（load_dotenv 直後） =====
LOG_MAX_BYTES       = int(os.getenv("LOG_MAX_BYTES", "10485760"))   # 10 MB
LOG_BACKUP_COUNT    = int(os.getenv("LOG_BACKUP_COUNT", "7"))        # 7 ファイル保持
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO")                 # 後方互換
LOG_LEVEL_FILE      = os.getenv("LOG_LEVEL_FILE", LOG_LEVEL)         # ファイルログレベル
LOG_LEVEL_CONSOLE   = os.getenv("LOG_LEVEL_CONSOLE", LOG_LEVEL)      # コンソールログレベル
LOG_DUPLICATE_THRESHOLD = int(os.getenv("LOG_DUPLICATE_THRESHOLD", "60"))  # 重複抑制秒数
LOG_SUPPRESS_HTTP_SUCCESS = os.getenv("LOG_SUPPRESS_HTTP_SUCCESS", "true").lower() == "true"

def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        logger.critical(f"❌ 環境変数 {key} が設定されていません。.env を確認してください。")
        raise SystemExit(1)
    return value

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key, "")
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes")

BOT_TOKEN = _require_env("BOT_TOKEN")

_channel_id_raw = _require_env("CHANNEL_ID")
try:
    CHANNEL_ID = int(_channel_id_raw)
except ValueError:
    logger.critical(f"❌ CHANNEL_ID は数値で指定してください（現在の値: {_channel_id_raw!r}）")
    raise SystemExit(1)

_raw_aquestalk = os.getenv("AQUESTALK_PATH", "").strip().rstrip("/")
if _raw_aquestalk:
    if os.path.isdir(_raw_aquestalk):
        _raw_aquestalk = os.path.join(_raw_aquestalk, "AquesTalkPi")
        logger.info(
            f"AQUESTALK_PATH にディレクトリが指定されています。"
            f"実行ファイルを自動補完しました: {_raw_aquestalk}"
        )
    if not os.path.isfile(_raw_aquestalk):
        logger.warning(f"AQUESTALK_PATH のファイルが見つかりません: {_raw_aquestalk} → 音声機能を無効化します")
        _raw_aquestalk = ""
    elif not os.access(_raw_aquestalk, os.X_OK):
        logger.warning(f"AQUESTALK_PATH に実行権限がありません: {_raw_aquestalk} → 音声機能を無効化します")
        logger.warning("  修正するには: chmod +x " + _raw_aquestalk)
        _raw_aquestalk = ""
AQUESTALK_PATH  = _raw_aquestalk or None
AQUESTALK_SPEED = int(os.getenv("AQUESTALK_SPEED", "150"))
AUDIO_PLAYER    = os.getenv("AUDIO_PLAYER", "aplay")

EEW_CHANNEL_ID       = int(os.getenv("EEW_CHANNEL_ID",       os.getenv("CHANNEL_ID", "0")))
QUAKE_CHANNEL_ID     = int(os.getenv("QUAKE_CHANNEL_ID",     os.getenv("CHANNEL_ID", "0")))
TSUNAMI_CHANNEL_ID   = int(os.getenv("TSUNAMI_CHANNEL_ID",   os.getenv("CHANNEL_ID", "0")))
OTHER_CHANNEL_ID     = int(os.getenv("OTHER_CHANNEL_ID",     os.getenv("CHANNEL_ID", "0")))

# 新規追加: ソース別チャンネル
P2P_EEW_CHANNEL_ID   = int(os.getenv("P2P_EEW_CHANNEL_ID",   os.getenv("EEW_CHANNEL_ID", os.getenv("CHANNEL_ID", "0"))))
KYOSHIN_CHANNEL_ID   = int(os.getenv("KYOSHIN_CHANNEL_ID",   os.getenv("OTHER_CHANNEL_ID", os.getenv("CHANNEL_ID", "0"))))
LMONI_EEW_CHANNEL_ID = int(os.getenv("LMONI_EEW_CHANNEL_ID", os.getenv("EEW_CHANNEL_ID", os.getenv("CHANNEL_ID", "0"))))
ADMIN_CHANNEL_ID   = int(os.getenv("ADMIN_CHANNEL_ID",   "0"))
VOLCANO_CHANNEL_ID   = int(os.getenv("VOLCANO_CHANNEL_ID",   os.getenv("CHANNEL_ID", "0")))

# USGS 地震情報設定（_env_bool定義後）
USGS_ENABLED        = _env_bool("USGS_ENABLED", True)
USGS_CHANNEL_ID     = int(os.getenv("USGS_CHANNEL_ID", os.getenv("QUAKE_CHANNEL_ID", os.getenv("CHANNEL_ID", "0"))))
USGS_MAGNITUDE_MIN  = float(os.getenv("USGS_MAGNITUDE_MIN", "5.0"))
USGS_FETCH_INTERVAL = int(os.getenv("USGS_FETCH_INTERVAL", "600"))  # 10分
USGS_REGION_LAT_MIN = float(os.getenv("USGS_REGION_LAT_MIN", "20"))
USGS_REGION_LAT_MAX = float(os.getenv("USGS_REGION_LAT_MAX", "50"))
USGS_REGION_LON_MIN = float(os.getenv("USGS_REGION_LON_MIN", "120"))
USGS_REGION_LON_MAX = float(os.getenv("USGS_REGION_LON_MAX", "180"))
USGS_NOTIFICATION_COOLDOWN = int(os.getenv("USGS_NOTIFICATION_COOLDOWN", "300"))  # 5分

# ===== リソース監視設定 =====
RESOURCE_MONITORING_ENABLED = _env_bool("RESOURCE_MONITORING_ENABLED", True)
RESOURCE_CHECK_INTERVAL = _env_int("RESOURCE_CHECK_INTERVAL", 3600)  # 1時間
DISK_WARNING_THRESHOLD = _env_int("DISK_WARNING_THRESHOLD", 80)  # 80%
DISK_ERROR_THRESHOLD = _env_int("DISK_ERROR_THRESHOLD", 90)  # 90%

# ===== ヘルスチェック設定 =====
HEALTH_CHECK_TIMEOUT = 5  # API ping のタイムアウト（秒）
HEALTH_CHECK_CACHE_TTL = 30  # ヘルスチェック結果キャッシュ時間（秒）
ERROR_NOTIFICATION_TTL = 3600  # エラー通知の重複防止時間（秒）

# ===============================
# フィルター設定（情報種別ごと）
# ===============================

# EEW フィルター（Wolfx）
EEW_MIN_INTENSITY   = _env_int("EEW_MIN_INTENSITY",   0)   # 0=全て通知（INT_MAP の数値キー）

# 地震情報フィルター
QUAKE_MIN_SCALE     = _env_int("QUAKE_MIN_SCALE",     0)   # 0=全て / 10=震度1以上 / 30=震度3以上 ...
QUAKE_MIN_MAG       = float(os.getenv("QUAKE_MIN_MAG", "0.0"))
QUAKE_MIN_DEPTH     = _env_int("QUAKE_MIN_DEPTH",     0)
QUAKE_MAX_DEPTH     = _env_int("QUAKE_MAX_DEPTH",     9999)
QUAKE_ENABLE_SCALE_PROMPT    = _env_bool("QUAKE_ENABLE_SCALE_PROMPT",    True)
QUAKE_ENABLE_DESTINATION     = _env_bool("QUAKE_ENABLE_DESTINATION",     True)
QUAKE_ENABLE_SCALE_AND_DEST  = _env_bool("QUAKE_ENABLE_SCALE_AND_DEST",  True)
QUAKE_ENABLE_DETAIL_SCALE    = _env_bool("QUAKE_ENABLE_DETAIL_SCALE",    True)
QUAKE_ENABLE_FOREIGN         = _env_bool("QUAKE_ENABLE_FOREIGN",         True)
QUAKE_ENABLE_OTHER           = _env_bool("QUAKE_ENABLE_OTHER",           True)

# 津波情報フィルター
TSUNAMI_ENABLE      = _env_bool("TSUNAMI_ENABLE",     True)

# その他通知フィルター
ENABLE_LONG_PERIOD  = _env_bool("ENABLE_LONG_PERIOD",  True)
ENABLE_ADVISORY     = _env_bool("ENABLE_ADVISORY",     True)
ENABLE_TSUNAMI_OBS  = _env_bool("ENABLE_TSUNAMI_OBS",  True)
ENABLE_KYOSHIN      = _env_bool("ENABLE_KYOSHIN",      True)  # 強震モニタ

# EEW 冗長化設定
EEW_FALLBACK_TIMEOUT = _env_int("EEW_FALLBACK_TIMEOUT", 30)  # Wolfx無受信が何秒続いたらフォールバック起動するか
WOLFX_HEARTBEAT_TIMEOUT = _env_int("WOLFX_HEARTBEAT_TIMEOUT", 90)  # heartbeat timeout (seconds)

# API レート制限 / Circuit breaker
FETCH_FAILURE_THRESHOLD = _env_int("FETCH_FAILURE_THRESHOLD", 3)
FETCH_BACKOFF_SECONDS   = _env_int("FETCH_BACKOFF_SECONDS", 60)

# キュー設定（メモリ枯渇対策）
SPEECH_QUEUE_MAXSIZE = _env_int("SPEECH_QUEUE_MAXSIZE", 200)  # 音声読み上げキューの最大サイズ
MP3_QUEUE_MAXSIZE = _env_int("MP3_QUEUE_MAXSIZE", 50)  # MP3再生キューの最大サイズ

# !status コマンド設定
STATUS_SHOW_CPU     = _env_bool("STATUS_SHOW_CPU",    True)
STATUS_SHOW_MEM     = _env_bool("STATUS_SHOW_MEM",    True)
STATUS_SHOW_DISK    = _env_bool("STATUS_SHOW_DISK",   True)
STATUS_SHOW_UPTIME  = _env_bool("STATUS_SHOW_UPTIME", True)

WOLFX_WSS     = "wss://ws-api.wolfx.jp/jma_eew"
P2P_WSS       = "wss://api.p2pquake.net/v2/ws"
P2P_API       = "https://api.p2pquake.net/v2/history"
LMONI_EEW_BASE = "https://www.lmoni.bosai.go.jp/monitor/webservice/hypo/eew"

def load_region_map():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, "region_map.json")

    if not os.path.exists(path):
        logger.warning("region_map.json が存在しません")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.error(f"region_map.json 読み込みエラー: {e}")

    return {}

REGION_MAP = load_region_map()

INT_MAP = {
    -1: "不明",
    10: "1",
    20: "2",
    30: "3",
    40: "4",
    45: "5弱",
    46: "推定5弱以上",
    50: "5強",
    55: "6弱",
    60: "6強",
    70: "7",
}

# 震度色
SHINDO_COLORS = {
    -1: 0x62626B,   # 不明
    0:  0x62626B,   # 震度0
    10: 0x3098BD,   # 震度1
    20: 0x4CD0A7,   # 震度2
    30: 0xF6CB51,   # 震度3
    40: 0xFF9939,   # 震度4
    45: 0xE52A18,   # 震度5弱
    50: 0xC31B1B,   # 震度5強
    55: 0xA30A6B,   # 震度6弱
    60: 0x86046E,   # 震度6強
    70: 0x54068E,   # 震度7
}

# 長周期地震動階級色
LG_COLORS = {
    "1": 0xF2CF57,    # 階級1
    "2": 0xD73B15,    # 階級2
    "3": 0xB3091D,    # 階級3
    "4": 0x890076,    # 階級4
    "不明": 0x62626B,    # 不明
}

TSUNAMI_MAP = {
    "None": "津波の心配なし",
    "Unknown": "津波の有無は不明",
    "Checking": "津波の有無を調査中",
    "NonEffective": "若干の海面変動（被害の心配なし）",
    "Watch": "津波注意報",
    "Warning": "津波警報",
    "MajorWarning": "大津波警報",
}

QUAKE_TYPE_MAP = {
    "ScalePrompt": "震度速報",
    "Destination": "震源に関する情報",
    "ScaleAndDestination": "震度・震源に関する情報",
    "DetailScale": "各地の震度に関する情報",
    "Foreign": "遠地地震に関する情報",
    "Other": "その他の情報",
}

# ===============================
# Bot初期化
# ===============================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ===============================
# Cog本体
# ===============================

class QuakeTsunamiCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel         = None
        self.eew_channel     = None
        self.quake_channel   = None
        self.tsunami_channel = None
        self.other_channel   = None
        self.p2p_eew_channel   = None
        self.kyoshin_channel   = None
        self.lmoni_eew_channel = None
        self.session = None
        self.last_quake_id = None
        self.last_tsunami_id = None
        self.last_eew_event_id = None
        self.last_eew_serial = 0
        self.recent_eews = {}
        self.last_long_period_id = None
        self.last_quake_advisory_id = None
        self.last_tsunami_observation_id = None
        self.last_advisory_ids = {}
        self.prev_obs_heights = defaultdict(dict)
        self.vibration_monitor_task = None
        self.lmoni_eew_task = None
        self.monitored_event_id = None
        self.last_eew_data = None
        self.last_warn_areas = set()

        # 各APIの最終受信時刻・受信カウント（!status 用）
        self._last_recv: dict[str, datetime | None] = {
            "wolfx":   None,
            "p2p_eew": None,
            "lmoni":   None,
            "quake":   None,
            "tsunami": None,
            "long_period":    None,
            "tsunami_obs":    None,
            "quake_advisory": None,
            "volcano":        None,  # 火山情報
            "usgs":           None,  # USGS 地震情報
        }
        self._recv_count: dict[str, int] = {k: 0 for k in self._last_recv}
        self._bot_start_time: datetime = datetime.now()
        self._start_time: float = time.time()  # ← 追加: Web Dashboard uptime用

        # USGS 地震情報管理
        self.last_usgs_ids: dict[str, float] = {}  # USGS Event ID → 通知時刻（cooldown 用）
        self._usgs_last_fetch_time: datetime | None = None
        self.usgs_channel = None

        # Wolfx フォールバック管理
        self._wolfx_last_recv: datetime | None = None
        self._wolfx_last_eew_recv: datetime | None = None
        self._wolfx_last_heartbeat: float | None = None
        self._wolfx_ws_alive: bool = False
        self._wolfx_heartbeat_timeout_warned: bool = False
        self._fallback_active: bool = False
        self._fallback_task:   asyncio.Task | None = None
        self._fetch_failures: dict[str, int] = {
            "quake": 0,
            "tsunami": 0,
            "long_period": 0,
            "tsunami_obs": 0,
            "quake_advisory": 0,
        }
        self._fetch_backoff_until: dict[str, float] = {
            "quake": 0.0,
            "tsunami": 0.0,
            "long_period": 0.0,
            "tsunami_obs": 0.0,
            "quake_advisory": 0.0,
        }
        self.speech_queue = asyncio.PriorityQueue(maxsize=SPEECH_QUEUE_MAXSIZE)
        self.speech_task = None
        self.mp3_queue = asyncio.Queue(maxsize=MP3_QUEUE_MAXSIZE)
        self.mp3_task = None
        self.audio_files = {
            "low_alert": "low_alert.mp3",
            "koushin": "koushin.mp3",
            "saisyu": "saisyu.mp3",
            "eew3": "eew3.mp3",
            "high_alert": "high_alert.mp3",
            "eewC": "eewC.mp3",
            "vxse51": "vxse51.mp3",
            "vxse52": "vxse52.mp3",
            "vxse53": "vxse53.mp3",
            "vxse5c": "vxse5c.mp3",
        }
        self.audio_flags = {"warning": False, "int3": False, "first": False, "final": False, "cancel": False}

        self.headers = {"Accept-Encoding": "identity"}

        # ===============================
        # フェーズ2: 重複リクエスト防止の Lock と LRU キャッシュ管理
        # ===============================
        # 各 API の排他制御用 Lock（複数リクエスト防止）
        self._fetch_quake_lock = asyncio.Lock()
        self._fetch_tsunami_lock = asyncio.Lock()
        self._fetch_long_period_lock = asyncio.Lock()
        self._fetch_tsunami_obs_lock = asyncio.Lock()
        self._fetch_quake_advisory_lock = asyncio.Lock()

        # recent_eews の LRU 管理（メモリリーク対策）
        # 最大50個の EEW を保持し、超過時は最も古いものを削除
        self.recent_eews_max_size = 50

        # 火山情報ポーリング管理
        self._last_volcano_event_id = None        # 最後に通知した eventId（status 表示用）
        self._last_volcano_info_map: dict = {}    # 前回の info.json を {eventId: item} で保持（差分検知用）
        self.volcano_task = None
        self._last_volcano_recv_time: datetime | None = None  # 最後の火山情報受信時刻
        self._volcano_recv_count: int = 0  # 火山情報受信回数
        
        # ===============================
        # A-1: ヘルスチェック関連
        # ===============================
        self.health_check_cache = None  # ヘルスチェック結果キャッシュ
        self.last_health_check_time = None  # 最後のヘルスチェック時刻
        
        # ===============================
        # A-4: エラー監視関連
        # ===============================
        self.admin_channel = None  # 管理者チャンネル（エラー通知先）
        self.error_summary_task = None  # 日次エラーサマリータスク
        self.error_notification_cache: dict = {}  # エラーハッシュ → 最後の通知時刻
        self.error_count_today: int = 0  # 本日のエラー件数
        self.daily_error_summary: dict = {}  # エラータイプ → 発生回数
        
        # ===============================
        # A-5: リソース監視関連
        # ===============================
        self.resource_monitor_task = None  # リソース監視タスク

    # ===============================
    # Cog起動・終了
    # ===============================

    async def cog_load(self):
        try:
            logger.debug("■ cog_load() 実行開始")
            
            # イベントループ内で asyncio.Queue を初期化（__init__では実行不可）
            logger.debug("  → 音声キューを初期化中...")
            self.speech_queue = asyncio.PriorityQueue(maxsize=SPEECH_QUEUE_MAXSIZE)
            self.mp3_queue = asyncio.Queue(maxsize=MP3_QUEUE_MAXSIZE)
            logger.debug("  → 音声キューを初期化しました")
            
            logger.debug("  → aiohttp セッションを作成中...")
            self.session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(
                    total=30,        # 全体タイムアウト: 10→30秒（P2P遅延対策）
                    connect=10,      # 接続確立タイムアウト
                    sock_read=20,    # レスポンス読み取りタイムアウト
                ),
                connector=aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
            )
            logger.info("✓ aiohttp セッションを作成しました")
        except Exception as e:
            logger.error(f"❌ cog_load() エラー: {type(e).__name__}: {e}", exc_info=True)
            raise

    async def cog_unload(self):
        for loop_task in (
            self.fetch_quake,
            self.fetch_tsunami,
            self.fetch_long_period,
            self.fetch_tsunami_observation,
            self.fetch_quake_advisory,
            self.fetch_usgs_quake,
        ):
            if loop_task.is_running():
                loop_task.cancel()

        for bg_task in (
            self.vibration_monitor_task,
            self.lmoni_eew_task,
            self.speech_task,
            self.mp3_task,
        ):
            if bg_task and not bg_task.done():
                bg_task.cancel()

        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("aiohttp セッションを閉じました")
        
        # error_summary_worker タスクをキャンセル
        if self.error_summary_task and not self.error_summary_task.done():
            self.error_summary_task.cancel()
            logger.info("error_summary_worker タスクをキャンセルしました")
        
        # resource_monitor タスクをキャンセル
        if self.resource_monitor_task and not self.resource_monitor_task.done():
            self.resource_monitor_task.cancel()
            logger.info("resource_monitor タスクをキャンセルしました")

    @commands.Cog.listener()
    async def on_ready(self):
        self.channel         = self.bot.get_channel(CHANNEL_ID)
        self.eew_channel     = self.bot.get_channel(EEW_CHANNEL_ID)     or self.channel
        self.quake_channel   = self.bot.get_channel(QUAKE_CHANNEL_ID)   or self.channel
        self.tsunami_channel = self.bot.get_channel(TSUNAMI_CHANNEL_ID) or self.channel
        
        # ===============================
        # 津波機能の確認ログ
        # ===============================
        if TSUNAMI_ENABLE:
            logger.info(f"✓ 津波情報機能: 有効（チャンネルID: {TSUNAMI_CHANNEL_ID}）")
        else:
            logger.warning(f"⚠️  津波情報機能: 無効（TSUNAMI_ENABLE=false）")
        self.other_channel   = self.bot.get_channel(OTHER_CHANNEL_ID)   or self.channel
        
        # 新規追加: ソース別チャンネル
        self.p2p_eew_channel   = self.bot.get_channel(P2P_EEW_CHANNEL_ID)   or self.eew_channel
        self.kyoshin_channel   = self.bot.get_channel(KYOSHIN_CHANNEL_ID)   or self.other_channel
        self.lmoni_eew_channel = self.bot.get_channel(LMONI_EEW_CHANNEL_ID) or self.eew_channel
        self.volcano_channel   = self.bot.get_channel(VOLCANO_CHANNEL_ID) or self.channel  # 火山情報チャンネル
        
        # USGS 地震情報チャンネル
        self.usgs_channel = self.bot.get_channel(USGS_CHANNEL_ID) or self.quake_channel

        if not self.fetch_quake.is_running():
            self.fetch_quake.start()

        if not self.fetch_tsunami.is_running():
            self.fetch_tsunami.start()
            logger.info("✓ P2P津波情報ポーリングタスクを開始しました")
        else:
            logger.info("⚠️  P2P津波情報ポーリングタスクは既に実行中です")

        if not self.fetch_long_period.is_running():
            self.fetch_long_period.start()

        if not self.fetch_quake_advisory.is_running():
            self.fetch_quake_advisory.start()

        if not self.fetch_tsunami_observation.is_running():
            self.fetch_tsunami_observation.start()

        self.bot.loop.create_task(self.connect_eew_ws())
        # P2P EEW WebSocket と LMoni EEW ポーリングは現在停止中
        # self.bot.loop.create_task(self.connect_p2p_eew_ws())
        # self.lmoni_eew_task = self.bot.loop.create_task(self.fetch_lmoni_eew_loop())

        # Wolfx フォールバック監視（一定時間受信なしで P2P/LMoni を起動）
        self.bot.loop.create_task(self._eew_fallback_monitor())

        if self.speech_task is None or self.speech_task.done():
            self.speech_task = self.bot.loop.create_task(self.speech_worker())

        if self.mp3_task is None or self.mp3_task.done():
            self.mp3_task = self.bot.loop.create_task(self.mp3_worker())

        # Web Dashboard起動（WEB_DASHBOARD_ENABLED環境変数でコントロール）
        if _env_bool("WEB_DASHBOARD_ENABLED", False):
            self.bot.loop.create_task(self.start_web_dashboard())

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

        # USGS 地震情報ポーリング開始
        if USGS_ENABLED:
            if not self.fetch_usgs_quake.is_running():
                self.fetch_usgs_quake.start()
                logger.info(f"✓ USGS地震情報ポーリングタスクを開始しました（間隔: {USGS_FETCH_INTERVAL}秒）")
                
                # 起動時最新情報を取得・通知
                self.bot.loop.create_task(self._fetch_and_notify_latest_usgs())
            else:
                logger.info("⚠️ USGS地震情報ポーリングタスクは既に実行中です")
        else:
            logger.info("⚠️ USGS地震情報機能: 無効（USGS_ENABLED=false）")

        # スラッシュコマンドを同期
        try:
            synced = await self.bot.tree.sync()
            logger.info(f"✓ スラッシュコマンドを同期しました（{len(synced)}件）")
        except Exception as e:
            logger.warning(f"⚠️ スラッシュコマンド同期失敗: {e}")

        logger.info(f"✅ ログイン完了: {self.bot.user}")

    # ===============================
    # 自動取得
    # ===============================


        # ===============================
        # A-4: エラー監視設定
        # ===============================
        # 管理者チャンネルを設定
        if ADMIN_CHANNEL_ID != 0:
            self.admin_channel = self.bot.get_channel(ADMIN_CHANNEL_ID)
            if self.admin_channel:
                logger.info(f"✓ 管理者チャンネルを設定しました（ID: {ADMIN_CHANNEL_ID}）")
            else:
                logger.warning(f"⚠️ 管理者チャンネルが見つかりません（ID: {ADMIN_CHANNEL_ID}）")
        
        # 日次エラーサマリータスクを開始
        if not self.error_summary_task:
            self.error_summary_task = self.bot.loop.create_task(self.error_summary_worker())
            logger.info("✓ 日次エラーサマリータスクを開始しました")

        # ===============================
        # A-5: リソース監視設定
        # ===============================
        # リソース監視タスクを開始
        if not hasattr(self, 'resource_monitor_task'):
            self.resource_monitor_task = None
        
        if not self.resource_monitor_task:
            self.resource_monitor_task = self.bot.loop.create_task(self.resource_monitor())
            logger.info("✓ リソース監視タスクを開始しました")

    @tasks.loop(seconds=60)
    async def fetch_long_period(self):
        try:
            async with self.session.get(
                "https://www.jma.go.jp/bosai/ltpgm/data/list.json",
                ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                if not data:
                    return

                latest = data[0]
                event_id = latest.get("eid")

                if self.last_long_period_id is None:
                    self.last_long_period_id = event_id
                    await self.notify_long_period(latest, extra_note="（ボット起動時の最新情報）")
                    return

                if self.last_long_period_id != event_id:
                    self.last_long_period_id = event_id
                    await self.notify_long_period(latest)

        except Exception:
            logger.error(f"Fetch Long-Period エラー:\n{traceback.format_exc()}")


    @fetch_long_period.before_loop
    async def before_fetch_long_period(self):
        """セッション初期化完了まで待機"""
        await self.bot.wait_until_ready()

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

                TARGET_TITLES = [
                    "津波観測に関する情報",
                    "沖合の津波観測に関する情報",
                    "各地の満潮時刻・津波到達予想時刻に関する情報",
                    "津波予報",
                    "津波注意報",
                    "津波警報",
                    "大津波警報",
                ]

                for item in data:
                    ttl = item.get("ttl", "")
                    if not any(x in ttl for x in TARGET_TITLES):
                        continue

                    event_id = item.get("eid") or item.get("ctt")
                    # report_time も含めてチェック（同じ ID でも更新される場合がある）
                    report_time = item.get("rdt", "")
                    current_key = f"{event_id}_{report_time}"
                    
                    if self.last_tsunami_observation_id == current_key:
                        logger.debug(f"fetch_tsunami_observation: 既出情報をスキップ (ID: {event_id}, 時刻: {report_time})")
                        break

                    json_filename = item.get("json")
                    if not json_filename:
                        continue

                    logger.info(f"津波観測情報取得: ID={event_id}, 時刻={report_time}")
                    
                    detail_url = f"https://www.jma.go.jp/bosai/tsunami/data/{json_filename}"
                    async with self.session.get(detail_url, timeout=aiohttp.ClientTimeout(total=25)) as detail_resp:
                        if detail_resp.status != 200:
                            logger.warning(f"津波観測情報詳細取得失敗: HTTP {detail_resp.status}")
                            continue
                        detail = await detail_resp.json()

                    # 通知成功後に lastID を更新
                    await self.notify_tsunami_observation(detail, list_item=item)
                    self.last_tsunami_observation_id = current_key
                    logger.info(f"津波観測情報を通知しました")
                    break

        except Exception:
            logger.error(f"Fetch Tsunami Observation エラー:\n{traceback.format_exc()}")


    @fetch_tsunami_observation.before_loop
    async def before_fetch_tsunami_observation(self):
        """セッション初期化完了まで待機"""
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def fetch_quake_advisory(self):
        try:
            async with self.session.get(
                "https://www.jma.go.jp/bosai/quake/data/list.json",
                ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                if not data or not isinstance(data, list):
                    return

                ADVISORY_TTL = 7 * 24 * 3600
                now_ts = datetime.now().timestamp()
                self.last_advisory_ids = {
                    eid: ts for eid, ts in self.last_advisory_ids.items()
                    if now_ts - ts < ADVISORY_TTL
                }

                for item in data:
                    ttl = item.get("ttl", "")
                    event_id = item.get("eid") or item.get("ctt") or str(hash(str(item)))

                    if not any(keyword in ttl for keyword in [
                        "北海道・三陸沖後発地震注意情報",
                        "南海トラフ地震臨時情報",
                        "顕著な地震の震源要素更新のお知らせ"
                    ]):
                        continue

                    if event_id in self.last_advisory_ids:
                        continue

                    self.last_advisory_ids[event_id] = now_ts

                    json_filename = item.get("json")
                    if json_filename:
                        detail_url = f"https://www.jma.go.jp/bosai/quake/data/{json_filename}"
                        async with self.session.get(detail_url, timeout=aiohttp.ClientTimeout(total=25)) as detail_resp:
                            if detail_resp.status == 200:
                                detail = await detail_resp.json()
                                await self.notify_quake_advisory(list_item=item, detail_data=detail)
                            else:
                                await self.notify_quake_advisory(list_item=item)
                    else:
                        await self.notify_quake_advisory(list_item=item)

        except Exception as e:
            logger.error(f"Fetch Quake Advisory エラー:\n{traceback.format_exc()}")



    @fetch_quake_advisory.before_loop
    async def before_fetch_quake_advisory(self):
        """セッション初期化完了まで待機"""
        await self.bot.wait_until_ready()

    # ===============================
    # USGS 地震情報 ポーリング
    # ===============================

    @tasks.loop(seconds=USGS_FETCH_INTERVAL)
    async def fetch_usgs_quake(self):
        """USGS GeoJSON から地震情報を取得し、フィルタリング後に通知

        - エンドポイント: all_day（過去24時間）を使用
          all_hour（過去1時間）はポーリング間隔10分と相性が悪く、
          cooldown が切れた既存IDを再通知してしまう。
        - 新規判定: last_usgs_ids に存在しない event_id のみ通知
        - cooldown: 通知済み ID を USGS_NOTIFICATION_COOLDOWN 秒保持し、
          時刻切れエントリは都度削除してメモリリークを防止
        """
        if not USGS_ENABLED:
            return

        # 使用エンドポイント: significant_week より all_day が網羅性高い
        # M5.0+ に絞るなら significant_week でも可だが、地域フィルターと組み合わせるため all_day を使用
        url = (
            "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"
        )

        try:
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
                headers={"User-Agent": "QTLBot/1.0 (Discord earthquake bot; contact via GitHub)"},
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"USGS fetch failed: HTTP {resp.status}")
                    self._record_fetch_failure("usgs", f"HTTP {resp.status}")
                    return

                data = await resp.json(content_type=None)
                features = data.get("features", [])

                if not features:
                    logger.debug("USGS: no earthquakes in the past 24 hours")
                    return

                self._last_recv["usgs"] = datetime.now()
                self._reset_fetch_backoff("usgs")

                now = time.time()

                # cooldown 切れエントリを削除（メモリリーク防止）
                self.last_usgs_ids = {
                    eid: ts for eid, ts in self.last_usgs_ids.items()
                    if now - ts < USGS_NOTIFICATION_COOLDOWN
                }

                notified = 0
                for feature in features:
                    try:
                        props = feature.get("properties", {})
                        geom  = feature.get("geometry", {})
                        coords = geom.get("coordinates", [None, None, None])

                        event_id = feature.get("id", "")
                        if not event_id:
                            continue

                        mag  = props.get("mag")
                        place = props.get("place", "Unknown")

                        # フィルター①: マグニチュード下限
                        if mag is None or mag < USGS_MAGNITUDE_MIN:
                            continue

                        # フィルター②: 地域（緯度・経度）
                        lon = coords[0] if len(coords) > 0 else None
                        lat = coords[1] if len(coords) > 1 else None
                        if lat is None or lon is None:
                            continue

                        if not (USGS_REGION_LAT_MIN <= lat <= USGS_REGION_LAT_MAX and
                                USGS_REGION_LON_MIN <= lon <= USGS_REGION_LON_MAX):
                            logger.debug(f"USGS skip: 地域外 {event_id} lat={lat:.1f} lon={lon:.1f}")
                            continue

                        # フィルター③: 重複通知防止（cooldown 内は通知しない）
                        if event_id in self.last_usgs_ids:
                            logger.debug(
                                f"USGS skip: cooldown {event_id} "
                                f"({now - self.last_usgs_ids[event_id]:.0f}s ago)"
                            )
                            continue

                        # 通知済みとして登録
                        self.last_usgs_ids[event_id] = now
                        self._recv_count["usgs"] = self._recv_count.get("usgs", 0) + 1

                        logger.info(f"USGS地震検知: {event_id} M{mag} {place}")
                        await self.notify_usgs_quake(feature)
                        notified += 1

                        # 過剰通知防止: 1回のポーリングで最大3件まで
                        if notified >= 3:
                            logger.info("USGS: 1ポーリングあたりの上限(3件)に達しました")
                            break

                    except Exception as e:
                        logger.error(f"USGS feature処理エラー: {e}", exc_info=True)
                        continue

                logger.debug(f"USGS: {len(features)}件中 {notified}件通知")

        except asyncio.TimeoutError:
            logger.warning("USGS fetch timeout")
            self._record_fetch_failure("usgs", "timeout")
        except aiohttp.ClientError as e:
            logger.warning(f"USGS fetch network error: {e}")
            self._record_fetch_failure("usgs", f"ClientError: {e}")
        except Exception as e:
            logger.error(f"USGS fetch error: {e}", exc_info=True)

    @fetch_usgs_quake.before_loop
    async def before_fetch_usgs_quake(self):
        """セッション初期化完了まで待機"""
        await self.bot.wait_until_ready()

    async def _fetch_and_notify_latest_usgs(self):
        """起動時に最新のUSGS地震情報を取得・通知する（all_day から最新1件）"""
        try:
            await asyncio.sleep(3)  # ポーリングタスク起動まで少し待機

            async with self.session.get(
                "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
                timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
                headers={"User-Agent": "QTLBot/1.0 (Discord earthquake bot; contact via GitHub)"},
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"USGS startup fetch failed: HTTP {resp.status}")
                    return

                data = await resp.json(content_type=None)
                features = data.get("features", [])

                if not features:
                    logger.debug("USGS: no earthquakes at startup")
                    return

                now = time.time()

                # フィルター通過した最新1件を通知
                for feature in features:
                    props  = feature.get("properties", {})
                    geom   = feature.get("geometry", {})
                    coords = geom.get("coordinates", [None, None, None])
                    mag      = props.get("mag")
                    event_id = feature.get("id", "")
                    lon = coords[0] if len(coords) > 0 else None
                    lat = coords[1] if len(coords) > 1 else None

                    if mag is None or mag < USGS_MAGNITUDE_MIN:
                        continue
                    if lat is None or lon is None:
                        continue
                    if not (USGS_REGION_LAT_MIN <= lat <= USGS_REGION_LAT_MAX and
                            USGS_REGION_LON_MIN <= lon <= USGS_REGION_LON_MAX):
                        continue

                    # Cooldown に登録して通知（以後ポーリングで重複しない）
                    self.last_usgs_ids[event_id] = now
                    self._recv_count["usgs"] = self._recv_count.get("usgs", 0) + 1
                    self._last_recv["usgs"] = datetime.now()

                    logger.info(f"USGS起動時最新情報: {event_id} M{mag}")
                    await self.notify_usgs_quake(
                        feature,
                        extra_note="（ボット起動時の最新情報）"
                    )
                    break  # 最新1件のみ

        except asyncio.TimeoutError:
            logger.debug("USGS startup fetch timeout")
        except Exception as e:
            logger.debug(f"USGS startup fetch error: {e}")

    # ===============================
    # WebSocket 共通再接続ヘルパー
    # ===============================
    async def _ws_connect_loop(
        self,
        url: str,
        label: str,
        handler,
        init_delay: int = 5,
        max_delay: int = 60,
    ):
        """
        WebSocket に接続し、切断時に指数バックオフで自動再接続する共通ループ。
        
        接続失敗時は指数バックオフで再接続間隔を延長し、DDoS リスクを軽減します：
          初回: 5秒 → 10秒 → 20秒 → ... → 60秒（最大値）→ ずっと60秒

        Parameters
        ----------
        url       : 接続先 URL
        label     : ログに表示する名称（例: "Wolfx EEW"）
        handler   : 接続後に呼び出す非同期コルーチン関数 (ws) -> None
        init_delay: 初回再接続待機秒数（デフォルト5秒）
        max_delay : 最大再接続待機秒数（デフォルト60秒）
        """
        delay = init_delay
        consecutive_failures = 0
        while not self.bot.is_closed():
            try:
                async with websockets.connect(
                    url,
                    ping_interval=30,
                    ping_timeout=15,
                    close_timeout=10,
                ) as ws:
                    logger.info(f"🌐 {label} WebSocket 接続完了")
                    delay = init_delay  # 接続成功でリセット
                    consecutive_failures = 0
                    await handler(ws)
            except asyncio.CancelledError:
                logger.info(f"{label} WebSocket ループがキャンセルされました")
                raise
            except Exception as e:
                consecutive_failures += 1
                logger.warning(
                    f"{label} WebSocket 切断 (再接続失敗 #{consecutive_failures}): {e}"
                )
                # Mark Wolfx connection as dead on disconnect
                if "Wolfx" in label:
                    self._reset_wolfx_ws_state()

            if not self.bot.is_closed():
                logger.info(
                    f"{label} WS: {delay}秒後に再接続 "
                    f"(exponential backoff: {consecutive_failures} failures)"
                )
                await asyncio.sleep(delay)
                # ← exponential backoff: 次回以降、待機時間を2倍に
                delay = min(delay * 2, max_delay)

    def _reset_wolfx_ws_state(self):
        """Wolfx WebSocket 接続状態をリセット（再接続時に呼び出し）"""
        self._wolfx_last_heartbeat = None
        self._wolfx_last_recv = None  # ← 追加：EEW受信タイムスタンプもリセット
        self._wolfx_last_eew_recv = None  # ← 追加：EEW受信タイムスタンプもリセット
        self._wolfx_ws_alive = False
        self._wolfx_heartbeat_timeout_warned = False

    def _fetch_backoff_is_active(self, key: str) -> bool:
        now = time.monotonic()
        until = self._fetch_backoff_until.get(key, 0.0)
        if until > now:
            return True
        if self._fetch_failures.get(key, 0) > 0:
            self._fetch_failures[key] = 0
            self._fetch_backoff_until[key] = 0.0
        return False

    def _reset_fetch_backoff(self, key: str) -> None:
        self._fetch_failures[key] = 0
        self._fetch_backoff_until[key] = 0.0

    def _record_fetch_failure(self, key: str, reason: str) -> None:
        self._fetch_failures[key] = self._fetch_failures.get(key, 0) + 1
        if self._fetch_failures[key] >= FETCH_FAILURE_THRESHOLD:
            self._fetch_backoff_until[key] = time.monotonic() + FETCH_BACKOFF_SECONDS
            logger.warning(
                f"{key} fetch failure threshold reached ({self._fetch_failures[key]}): "
                f"backoff for {FETCH_BACKOFF_SECONDS}s ({reason})"
            )

    # ===============================
    # WebSocket（Wolfx EEW）
    # ===============================
    async def connect_eew_ws(self):
        async def _handle(ws):
            self._reset_wolfx_ws_state()

            async for msg in ws:
                try:
                    data = json.loads(msg)
                    msg_type = data.get("type", "").lower()

                    # heartbeat packet: WebSocket alive signal
                    if msg_type == "heartbeat":
                        self._wolfx_last_heartbeat = time.monotonic()
                        self._wolfx_ws_alive = True
                        self._wolfx_heartbeat_timeout_warned = False
                        logger.debug(f"Wolfx heartbeat received (ts={data.get('timestamp')})")
                        continue

                    # EEW packet
                    if msg_type != "jma_eew":
                        continue

                    event_id = data.get("EventID")
                    serial   = int(data.get("Serial", 0))
                    now = datetime.now()
                    self._wolfx_last_recv = now
                    self._wolfx_last_eew_recv = now
                    self._wolfx_ws_alive = True
                    self._wolfx_heartbeat_timeout_warned = False
                    self._last_recv["wolfx"] = self._wolfx_last_recv
                    self._recv_count["wolfx"] += 1
                    logger.info(f"EEW 検知: EventID={event_id} Serial={serial} → 通知")
                    if (self.last_eew_event_id is None or
                            self.last_eew_event_id != event_id or
                            self.last_eew_serial < serial):
                        self.last_eew_event_id = event_id
                        self.last_eew_serial   = serial
                        await self.notify_eew(data, source="wolfx")
                except Exception:
                    logger.error(f"EEW 処理エラー:\n{traceback.format_exc()}")

        await self._ws_connect_loop(WOLFX_WSS, "Wolfx EEW", _handle)

    # ===============================
    # WebSocket（P2P EEW code=556）
    # ===============================
    async def connect_p2p_eew_ws(self):
        async def _handle(ws):
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    if data.get("code") != 556:
                        continue
                    wolfx_data = self._convert_p2p_eew_to_wolfx(data)
                    if not wolfx_data:
                        continue
                    event_id = wolfx_data.get("EventID")
                    serial   = wolfx_data.get("Serial", 1)
                    self._last_recv["p2p_eew"] = datetime.now()
                    self._recv_count["p2p_eew"] += 1
                    logger.info(f"P2P EEW 検知: EventID={event_id} Serial={serial}")
                    await self.notify_eew(wolfx_data, source="p2p_eew")
                except Exception:
                    logger.error(f"P2P EEW メッセージ処理エラー:\n{traceback.format_exc()}")

        await self._ws_connect_loop(P2P_WSS, "P2P地震情報 EEW", _handle)

    def _convert_p2p_eew_to_wolfx(self, p2p_data: dict) -> dict | None:
        """
        P2P地震情報の緊急地震速報（code=556）を Wolfx 形式に変換する。

        実際のフィールド仕様（確認済み）:
          issue.eventId   : EventID（"20260515202209" 形式）
          issue.serial    : 情報番号（文字列）
          earthquake.hypocenter.depth     : 深さ km（int）
          earthquake.hypocenter.magnitude : マグニチュード（float）
          earthquake.hypocenter.name      : 震央地名
          earthquake.originTime           : 発生時刻（"2026/05/15 20:22:02" 形式）
          earthquake.condition            : "仮定震源要素" のとき PLUM 法
          areas[].scaleFrom / scaleTo     : int（-1/0/10/20/30/40/45/50/55/60/70）
                                            scaleTo のみ 99（以上）あり
          areas[].kindCode                : "10"=警報未到達 / "11"=到達済 / "19"=PLUM法
          areas[].arrivalTime             : 到達予測時刻（null の場合あり）
          areas[].name                    : 細分区域名
          areas[].pref                    : 府県予報区
        """
        try:
            issue    = p2p_data.get("issue", {})
            event_id = issue.get("eventId", "")
            serial   = self.safe_int(issue.get("serial", "1")) or 1

            if p2p_data.get("cancelled"):
                return {
                    "type":      "jma_eew",
                    "Title":     "緊急地震速報（警報）",
                    "EventID":   event_id,
                    "Serial":    serial,
                    "isCancel":  True,
                    "isFinal":   False,
                    "Magnitude": -1.0,
                    "Depth":     -1,
                }

            eq    = p2p_data.get("earthquake", {}) or {}
            hypo  = eq.get("hypocenter", {}) or {}
            areas = p2p_data.get("areas", []) or []

            # PLUM 法判定
            is_plum = (
                eq.get("condition") == "仮定震源要素"
                or any(str(a.get("kindCode", "")) == "19" for a in areas)
            )

            # scaleFrom の Enum に 99 は存在しない（scaleTo のみ）
            scale_map = {
                -1: "不明", 0: "0", 10: "1", 20: "2", 30: "3", 40: "4",
                45: "5弱", 50: "5強", 55: "6弱", 60: "6強", 70: "7",
            }

            kind_type_map = {
                "10": "警報",    # 主要動未到達
                "11": "到達済",  # 主要動到達済み
                "19": "警報",    # PLUM法
            }

            max_scale_val = -1
            warn_areas    = []
            has_warn      = False  # kindCode=10 or 19 が1つでもあれば isWarn=True

            for area in areas:
                kind_code = str(area.get("kindCode", ""))
                sf = self.safe_int(area.get("scaleFrom", -1))
                st = self.safe_int(area.get("scaleTo",   -1))

                # 最大震度追跡（99は70相当で比較）
                effective = min(st, 70) if st not in (-1, 99) else (sf if sf != -1 else -1)
                if effective > max_scale_val:
                    max_scale_val = effective

                # kindCode=10 or 19 のエリアが1つでもあれば警報フラグ
                # kindCode=11（到達済）だけの場合は isWarn=False のまま
                if kind_code in ("10", "19"):
                    has_warn = True

                shindo1 = scale_map.get(sf, "不明")
                shindo2 = scale_map.get(min(st, 70) if st == 99 else st, shindo1)

                warn_areas.append({
                    "Chiiki":      area.get("name", ""),
                    "Pref":        area.get("pref", ""),
                    "Shindo1":     shindo1,
                    "Shindo2":     shindo2,
                    "Type":        kind_type_map.get(kind_code, "警報"),
                    "KindCode":    kind_code,
                    # arrivalTime は null の場合があるので None → "" に正規化
                    "ArrivalTime": area.get("arrivalTime") or "",
                })

            # MaxIntensity
            if any(self.safe_int(a.get("scaleTo", -1)) == 99 for a in areas):
                max_intensity = "7以上"
            else:
                max_intensity = scale_map.get(max_scale_val, "不明")

            raw_depth = hypo.get("depth", -1)
            depth = self.safe_int(raw_depth) if self.safe_int(raw_depth) != -1 else -1
            mag   = self.safe_float(hypo.get("magnitude", -1))

            return {
                "type":         "jma_eew",
                "Title":        "緊急地震速報（警報）",
                "EventID":      event_id,
                "Serial":       serial,
                "isCancel":     False,
                "isFinal":      False,
                "isTraining":   bool(p2p_data.get("test", False)),
                "isAssumption": is_plum,
                "isWarn":       has_warn,
                "OriginTime":   eq.get("originTime", ""),
                "ArrivalTime":  eq.get("arrivalTime", ""),
                "Hypocenter":   hypo.get("name", "不明") or "不明",
                "Latitude":     self.safe_float(hypo.get("latitude",  -200)),
                "Longitude":    self.safe_float(hypo.get("longitude", -200)),
                "Depth":        depth,
                "Magnitude":    mag if mag > 0 else -1,
                "Magunitude":   str(mag) if mag > 0 else "不明",
                "MaxIntensity": max_intensity,
                "WarnArea":     warn_areas,
                "_source":      "P2P地震情報",
            }

        except Exception:
            logger.error(f"P2P→Wolfx変換エラー:\n{traceback.format_exc()}")
            return None

    # ===============================
    # P2P地震情報 ポーリング（地震情報）
    # ===============================

    @tasks.loop(seconds=3)
    async def fetch_quake(self):
        if self._fetch_backoff_is_active("quake"):
            return

        # Lock を使った排他制御（asyncio.Lock で thread-safe に）
        if self._fetch_quake_lock.locked():
            return
        
        async with self._fetch_quake_lock:
            try:
                # 再試行ロジック: exponential backoff (1s, 2s, 4s)
                retry_delays = [1, 2, 4]
                last_error = None
                
                for attempt in range(len(retry_delays) + 1):
                    try:
                        async with self.session.get(
                            "https://api.p2pquake.net/v2/history?codes=551&limit=1",
                            ) as resp:
                            if resp.status == 200:
                                # 成功時: 通常処理へ
                                data_list = await resp.json()
                                logger.debug(f"fetch_tsunami: API 呼び出し成功 (ステータス: 200, データ件数: {len(data_list)})")
                                if not data_list:
                                    logger.debug("fetch_tsunami: 津波情報なし")
                                    return

                                data = data_list[0]
                                data_id = data.get("id")

                                if self.last_quake_id is None:
                                    self.last_quake_id = data_id
                                    self._reset_fetch_backoff("quake")
                                    await self.notify_quake(data, extra_note="（ボット起動時の最新情報）")
                                    return

                                if data_id == self.last_quake_id:
                                    self._reset_fetch_backoff("quake")
                                    return

                                self.last_quake_id = data_id
                                self._last_recv["quake"] = datetime.now()
                                self._recv_count["quake"] += 1
                                logger.info(f"P2P地震情報取得: id={data_id}")
                                self._reset_fetch_backoff("quake")
                                await self.notify_quake(data)
                                return
                            
                            elif resp.status >= 500:
                                # 5xx: 再試行対象
                                last_error = f"HTTP {resp.status}"
                                if attempt < len(retry_delays):
                                    delay = retry_delays[attempt]
                                    logger.debug(f"fetch_quake: {delay}秒後に再試行 (HTTP {resp.status})")
                                    await asyncio.sleep(delay)
                                    continue
                            else:
                                # 4xx など: 再試行しない
                                self._record_fetch_failure("quake", f"HTTP {resp.status}")
                                return
                    
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        # 接続エラーやタイムアウト: 再試行対象
                        last_error = str(e)
                        if attempt < len(retry_delays):
                            delay = retry_delays[attempt]
                            logger.debug(f"fetch_quake: {delay}秒後に再試行 ({type(e).__name__})")
                            await asyncio.sleep(delay)
                            continue
                
                # 全再試行失敗
                self._record_fetch_failure("quake", f"retry exhausted: {last_error}")

            except Exception:
                self._record_fetch_failure("quake", "exception")
                logger.error(f"Fetch Quake エラー:\n{traceback.format_exc()}")


    @fetch_quake.before_loop
    async def before_fetch_quake(self):
        """セッション初期化完了まで待機"""
        await self.bot.wait_until_ready()

    # ===============================
    # P2P地震情報 ポーリング（津波情報）
    # ===============================

    @tasks.loop(seconds=10)
    async def fetch_tsunami(self):
        # デバッグログ
        if self._fetch_backoff_is_active("tsunami"):
            logger.debug("fetch_tsunami: バックオフ中でスキップ")
            return

        # Lock を使った排他制御
        if self._fetch_tsunami_lock.locked():
            logger.debug("fetch_tsunami: ロック中でスキップ")
            return
        
        async with self._fetch_tsunami_lock:
            try:
                # 再試行ロジック: exponential backoff (1s, 2s, 4s)
                retry_delays = [1, 2, 4]
                last_error = None
                
                for attempt in range(len(retry_delays) + 1):
                    try:
                        logger.debug(f"fetch_tsunami: API 呼び出し開始 (試行 {attempt+1}/{len(retry_delays)+1})")
                        async with self.session.get(
                            "https://api.p2pquake.net/v2/history?codes=552&limit=1",
                            ) as resp:
                            if resp.status == 200:
                                # 成功時: 通常処理へ
                                data_list = await resp.json()
                                logger.debug(f"fetch_tsunami: API 呼び出し成功 (ステータス: 200, データ件数: {len(data_list)})")
                                if not data_list:
                                    logger.debug("fetch_tsunami: 津波情報なし")
                                    return

                                data = data_list[0]
                                data_id = data.get("id")

                                if self.last_tsunami_id is None:
                                    self.last_tsunami_id = data_id
                                    self._reset_fetch_backoff("tsunami")
                                    return

                                if data_id == self.last_tsunami_id:
                                    self._reset_fetch_backoff("tsunami")
                                    return

                                self.last_tsunami_id = data_id
                                self._last_recv["tsunami"] = datetime.now()
                                self._recv_count["tsunami"] += 1
                                logger.info(f"P2P津波情報取得: id={data_id}")
                                self._reset_fetch_backoff("tsunami")
                                await self.notify_tsunami(data)
                                return
                            
                            elif resp.status >= 500:
                                # 5xx: 再試行対象
                                last_error = f"HTTP {resp.status}"
                                if attempt < len(retry_delays):
                                    delay = retry_delays[attempt]
                                    logger.debug(f"fetch_tsunami: {delay}秒後に再試行 (HTTP {resp.status})")
                                    await asyncio.sleep(delay)
                                    continue
                            else:
                                # 4xx など: 再試行しない
                                self._record_fetch_failure("tsunami", f"HTTP {resp.status}")
                                return
                    
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        # 接続エラーやタイムアウト: 再試行対象
                        last_error = str(e)
                        if attempt < len(retry_delays):
                            delay = retry_delays[attempt]
                            logger.debug(f"fetch_tsunami: {delay}秒後に再試行 ({type(e).__name__})")
                            await asyncio.sleep(delay)
                            continue
                
                # 全再試行失敗
                self._record_fetch_failure("tsunami", f"retry exhausted: {last_error}")

            except Exception:
                self._record_fetch_failure("tsunami", "exception")
                logger.error(f"Fetch Tsunami エラー:\n{traceback.format_exc()}")


    @fetch_tsunami.before_loop
    async def before_fetch_tsunami(self):
        """セッション初期化完了まで待機"""
        logger.info("fetch_tsunami: wait_until_ready() を実行中...")
        await self.bot.wait_until_ready()
        logger.info("✓ fetch_tsunami: Bot の準備完了")

    # ===============================
    # ヘルパー
    # ===============================

    def safe_float(self, value):
        try:
            return float(value)
        except Exception:
            return 0.0

    def safe_int(self, value):
        try:
            return int(str(value).replace("km", "").strip())
        except Exception:
            return 0

    @staticmethod
    def safe_bool(value) -> bool:
        """bool / "true" / "1" / 1 など複数形式の真偽値を統一的に bool に変換する"""
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        return str(value).lower() in ("true", "1", "yes")

    def _truncate_embed_description(
        self, 
        text: str, 
        max_chars: int = 4096,
        suffix: str = "\n\n（長すぎるため一部省略）"
    ) -> str:
        """
        Discord Embed の description フィールドを正確に切り詰める。
        
        Discord API は Embed.description で最大 4096 文字をサポート。
        Unicode マルチバイト文字の途中で切られてエラーになることを防ぐ。
        
        Parameters
        ----------
        text : str
            切り詰め対象のテキスト
        max_chars : int
            最大文字数（デフォルト4096）。Discord API 制限に合わせて設定
        suffix : str
            切り詰め時に追加するサフィックス
            
        Returns
        -------
        str : 切り詰めされたテキスト（suffix込みで max_chars 以内）
        """
        if len(text) <= max_chars:
            return text
        
        # suffix を考慮して、実際の切り詰め位置を計算
        available = max_chars - len(suffix)
        
        if available <= 0:
            # suffix が長すぎる場合は、suffix なしで切り詰め
            logger.warning(
                f"_truncate_embed_description: suffix が長すぎます "
                f"(suffix={len(suffix)} chars, max={max_chars}). "
                f"suffix なしで切り詰めます"
            )
            return text[:max_chars]
        
        return text[:available] + suffix

    @staticmethod
    def format_jma_time(raw: str) -> str:
        """
        気象庁 JSON の各種時刻文字列を「YYYY年M月D日H時MM分頃」形式に変換する。

        対応フォーマット:
          2026/04/18 13:20    (P2P 形式)
          2026-04-18T13:20:00 (ISO 8601)
          2026-04-18 13:20    (スペース区切り)
        変換できない場合は入力をそのまま返す。
        """
        if not raw or raw in ("不明", "調査中"):
            return raw
        try:
            normalized = raw[:16].replace("T", " ").replace("-", "/")
            dt = datetime.strptime(normalized, "%Y/%m/%d %H:%M")
            return (
                f"{dt.year}年{dt.month}月{dt.day}日"
                f"{dt.hour}時{dt.minute:02d}分頃"
            )
        except Exception:
            return raw

    # ===============================
    # ローカル音声再生
    # ===============================
    async def speak_local(self, text: str, priority: int = 2):
        if not text or not text.strip():
            return
        try:
            self.speech_queue.put_nowait((priority, text))
            logger.info(f"音声キュー追加 [優先度{priority}] (深さ: {self.speech_queue.qsize()}/{SPEECH_QUEUE_MAXSIZE}): {text[:60]}")
        except asyncio.QueueFull:
            logger.warning(f"音声キューが満杯です (深さ: {self.speech_queue.qsize()}): {text[:60]} はスキップされました")


    async def speech_worker(self):
        if not AQUESTALK_PATH:
            logger.info("AQUESTALK_PATH 未設定のため音声読み上げ機能は無効です")
            return
        logger.info(f"音声読み上げ開始: {AQUESTALK_PATH} / player={AUDIO_PLAYER} / speed={AQUESTALK_SPEED}")
        queue_warn_threshold = max(SPEECH_QUEUE_MAXSIZE * 0.8, 1)
        
        while not self.bot.is_closed():
            try:
                priority, text = await self.speech_queue.get()
                queue_size = self.speech_queue.qsize()
                
                # キュー圧力が高い場合は警告
                if queue_size >= queue_warn_threshold:
                    logger.warning(f"音声キュー圧力高 (深さ: {queue_size}/{SPEECH_QUEUE_MAXSIZE})")
                
                logger.info(f"音声再生開始 [優先度{priority}] (キュー深さ: {queue_size}): {text[:60]}")
                escaped = text.replace('"', '\\"')

                tts_proc = await asyncio.create_subprocess_exec(
                    AQUESTALK_PATH, "-s", str(AQUESTALK_SPEED), escaped,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                tts_out, tts_err = await tts_proc.communicate()
                logger.info(f"AquesTalkPi 終了コード={tts_proc.returncode} 出力バイト数={len(tts_out)}")
                if tts_err:
                    logger.warning(f"AquesTalkPi stderr: {tts_err.decode(errors='replace')[:200]}")

                if tts_out:
                    play_proc = await asyncio.create_subprocess_exec(
                        AUDIO_PLAYER, "-",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, play_err = await play_proc.communicate(input=tts_out)
                    logger.info(f"{AUDIO_PLAYER} 終了コード={play_proc.returncode}")
                    # aplay は正常時も再生情報を stderr に出力するため、
                    # 終了コードが 0 以外のときのみ警告を出す
                    if play_err and play_proc.returncode != 0:
                        logger.warning(f"{AUDIO_PLAYER} stderr: {play_err.decode(errors='replace')[:200]}")
                else:
                    logger.warning(f"音声生成失敗（出力なし）: {text[:60]}")

                self.speech_queue.task_done()
                await asyncio.sleep(0.8)
            except Exception as e:
                logger.error(f"音声再生エラー: {e}")

    async def mp3_worker(self):
        if not _PYGAME_AVAILABLE:
            logger.info("MP3再生機能は無効です")
            return

        logger.info("MP3再生ワーカーを起動します")
        queue_warn_threshold = max(MP3_QUEUE_MAXSIZE * 0.8, 1)
        
        while not self.bot.is_closed():
            try:
                key, path = await self.mp3_queue.get()
                queue_size = self.mp3_queue.qsize()
                
                # キュー圧力が高い場合は警告
                if queue_size >= queue_warn_threshold:
                    logger.warning(f"MP3キュー圧力高 (深さ: {queue_size}/{MP3_QUEUE_MAXSIZE})")
                
                logger.info(f"play_mp3: 再生開始 key={key} (キュー深さ: {queue_size})")
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._play_mp3_blocking, path, key)
                logger.info(f"play_mp3: 再生完了 key={key}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"MP3再生ワーカーでエラー key={key}: {e}")
            finally:
                try:
                    self.mp3_queue.task_done()
                except Exception:
                    pass

    def _play_mp3_blocking(self, path: str, key: str):
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.1)
        except Exception as e:
            logger.error(f"play_mp3: 再生エラー key={key}: {e}")

    # ===============================
    # mp3音声再生
    # ===============================
    async def play_mp3(self, key: str):
        if not _PYGAME_AVAILABLE:
            logger.warning("play_mp3: pygame.mixer が利用できないため再生をスキップします")
            return

        filename = self.audio_files.get(key)
        if not filename:
            logger.warning(f"play_mp3: キー '{key}' が audio_files に存在しません")
            return

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if not os.path.exists(path):
            logger.warning(f"play_mp3: ファイルが見つかりません → {path}")
            return

        try:
            self.mp3_queue.put_nowait((key, path))
            logger.info(f"play_mp3: キューに追加 key={key} (深さ: {self.mp3_queue.qsize()}/{MP3_QUEUE_MAXSIZE})")
        except asyncio.QueueFull:
            logger.warning(f"play_mp3: MP3キューが満杯です (深さ: {self.mp3_queue.qsize()}) → キー '{key}' はスキップされました")

    # ===============================
    # 緊急地震速報通知
    # ===============================
    async def notify_eew(self, data, is_test=False, channel_override=None,
                         start_monitor=True, source: str = "wolfx"):
        """
        EEW を Discord に通知する。

        Parameters
        ----------
        source : EEW の取得元。チャンネル選択に使用。
            "wolfx"   → eew_channel
            "p2p_eew" → p2p_eew_channel
            "lmoni"   → lmoni_eew_channel
        channel_override : 明示的にチャンネルを指定する場合（後方互換）
        """
        # ソース別チャンネル選択（channel_override が指定された場合はそちらを優先）
        _source_channel_map = {
            "wolfx":   self.eew_channel,
            "p2p_eew": self.p2p_eew_channel,
            "lmoni":   self.lmoni_eew_channel,
        }
        channel = channel_override or _source_channel_map.get(source, self.eew_channel) or self.channel
        if not channel:
            return

        try:
            event_id = data.get("EventID")
            serial = int(data.get("Serial", 1))
            is_final = data.get("isFinal", False)
            is_cancel = data.get("isCancel", False)

            now = datetime.now().timestamp()
            
            # ===============================
            # LRU キャッシュ管理（メモリリーク対策）
            # ===============================
            # TTL チェック：300秒以上前のエントリを削除
            self.recent_eews = {
                eid: (d, t) for eid, (d, t) in self.recent_eews.items() 
                if now - t < 300
            }
            
            # サイズ制限：最大 recent_eews_max_size 個まで保持
            # 超過時は最も古いエントリを削除
            while len(self.recent_eews) >= self.recent_eews_max_size:
                oldest_eid = min(
                    self.recent_eews.keys(),
                    key=lambda eid: self.recent_eews[eid][1]
                )
                logger.debug(f"recent_eews LRU削除: {oldest_eid} (最大数{self.recent_eews_max_size}に達した)")
                del self.recent_eews[oldest_eid]
            
            # 新規エントリを追加
            self.recent_eews[event_id] = (data, now)

            if start_monitor and serial == 1 and self.monitored_event_id is None:
                self.monitored_event_id = event_id
                if self.vibration_monitor_task:
                    self.vibration_monitor_task.cancel()
                self.vibration_monitor_task = asyncio.create_task(self.vibration_monitor_loop(event_id))
                logger.info(f"EEW 第一報検知 → 強震モニタ監視開始 (EventID={event_id})")

            if is_cancel:
                origin_time = data.get("OriginTime", "不明")
                title = "緊急地震速報 (キャンセル報)"
                if is_test:
                    title = "【テスト】" + title

                embed = discord.Embed(title=title, color=0x00FF00, timestamp=datetime.now())
                embed.description = f"**発生時刻： {origin_time}**\n先程の緊急地震速報はキャンセルされました。"
                if is_test:
                    embed.set_footer(text="※これはテスト通知です。")
                await channel.send(embed=embed)

                if event_id == self.monitored_event_id:
                    self.monitored_event_id = None
                return
            if len(self.recent_eews) > 1:
                embed_summary = discord.Embed(title="複数の緊急地震速報が発表されています", color=0xFF0000, timestamp=datetime.now())
                summary_desc = ""
                sorted_eews = sorted(self.recent_eews.items(), key=lambda x: x[1][1])
                for eid, (old_data, _) in sorted_eews:
                    title_text = old_data.get('Title', '緊急地震速報')
                    s = int(old_data.get("Serial", 1))
                    f = old_data.get("isFinal", False)
                    serial_text = "最終報" if f else f"第{s}報"
                    hypo = old_data.get("Hypocenter", "不明")
                    max_int = old_data.get("MaxIntensity", "不明")
                    
                    # PLUM法の場合はマグニチュードを「推定なし」と表示
                    if old_data.get("isAssumption", False):
                        mag = "推定なし"
                    else:
                        mag = old_data.get("Magnitude") or old_data.get("Magunitude") or "不明"
                    
                    summary_desc += f"**{title_text} ({serial_text})**\n震源地: {hypo} / 予想最大震度: {max_int} / M{mag}\n\n"
                embed_summary.description = summary_desc.strip()
                await channel.send(embed=embed_summary)

            title_text = data.get('Title', '緊急地震速報')
            serial_text = "最終報" if is_final else f"第{serial}報"
            title = f"{title_text} ({serial_text})"
            if is_test:
                title = "【テスト】" + title

            origin_time = data.get("OriginTime", "不明")
            hypo = data.get("Hypocenter", "不明")
            is_plum = data.get("isAssumption", False)
            
            if is_plum:
                hypo += " (※PLUM法による予測)"
                mag = "推定なし"
                depth_str = "推定なし"
                depth = -1
            else:
                mag = data.get("Magnitude") or data.get("Magunitude") or "不明"
                depth_str = str(data.get("Depth", "不明")).replace("km", "").strip()
                depth = self.safe_int(depth_str)

            max_int_str = data.get("MaxIntensity", "不明")
            max_int_val = next((k for k, v in INT_MAP.items() if v == max_int_str), None)
            color = SHINDO_COLORS.get(max_int_val, 0x62626B)

            embed = discord.Embed(title=title, color=color, timestamp=datetime.now())

            # PLUM法の場合は「約」を付けない
            depth_display = f"約{depth_str}km" if not is_plum else depth_str
            
            description = (
                f"**発生時刻： {origin_time}**\n"
                f"**震源地： {hypo}**\n"
                f"**予想最大震度： {max_int_str}**\n"
                f"**マグニチュード： M{mag}**\n"
                f"**深さ： {depth_display}**"
            )

            notes = []
            if data.get("isWarn"):
                notes.append("**⚠強い揺れに警戒してください。**")

            # PLUM法の場合は震源情報が推定不可のため、深さ・マグニチュード依存の判定をスキップ
            if not is_plum:
                is_sea = self.safe_bool(data.get("isSea", False))
                if is_sea and self.safe_float(mag) >= 6.8 and self.safe_int(depth) <= 151:
                    notes.append("**⚠念の為海岸から離れてください。**")

                if self.safe_int(depth) >= 151:
                    notes.append("**⚠震源が深いため、遠方でも揺れる可能性があります。**")

            if notes:
                description += "\n\n" + "\n".join(notes)

            warn_areas = data.get("WarnArea", [])
            if warn_areas:
                alert_regions = set()
                for area in warn_areas:
                    chiiki = area.get("Chiiki")
                    if chiiki and area.get("Type", "").lower() in ("警報", "到達済"):
                        alert_regions.add(REGION_MAP.get(chiiki, "その他"))
                if alert_regions:
                    description += "\n\n**【強い揺れが予想される地域】**\n"
                    for region in sorted(alert_regions):
                        description += f"■ {region}　"

            if warn_areas:
                forecast_groups = defaultdict(list)
                def shindo_rank(s):
                    if s in INT_MAP.values():
                        for num, txt in INT_MAP.items():
                            if txt == s: return num
                    return 0

                is_assumption = data.get("isAssumption", False)

                for area in warn_areas:
                    chiiki = area.get("Chiiki")
                    if not chiiki: continue
                    shindo1 = area.get("Shindo1", "不明")
                    shindo2 = area.get("Shindo2", shindo1)

                    if is_assumption:
                        if shindo1 != "不明":
                            label = f"震度{shindo1}程度"
                            forecast_groups[label].append(chiiki)
                        continue

                    if shindo1 != "不明":
                        if shindo1 == shindo2:
                            label = f"震度{shindo1}程度"
                        else:
                            r1 = shindo_rank(shindo1)
                            r2 = shindo_rank(shindo2)
                            high, low = (shindo1, shindo2) if r1 >= r2 else (shindo2, shindo1)
                            label = f"震度{high}〜{low}程度"
                        forecast_groups[label].append(chiiki)

                if forecast_groups:
                    description += "\n\n**【地域ごとの予想震度】**"
                    sorted_labels = sorted(forecast_groups.keys(), key=lambda lbl: max(shindo_rank(s.strip("震度程度〜")) for s in lbl.split("〜")), reverse=True)
                    for label in sorted_labels:
                        areas = sorted(forecast_groups[label])
                        area_text = "\n".join(f"　{a}" for a in areas)
                        description += f"\n■ {label}\n{area_text}"

            # Discord API の制限（4096文字）に対応した正確な切り詰め
            description = self._truncate_embed_description(
                description,
                max_chars=4096,
                suffix="\n\n（地域が多いため一部省略）"
            )

            embed.description = description
            if is_test:
                embed.set_footer(text="※これはテスト通知です。")

            await channel.send(embed=embed)

            if (is_final or is_cancel) and event_id == self.monitored_event_id:
                self.monitored_event_id = None
                if self.vibration_monitor_task:
                    self.vibration_monitor_task.cancel()
            asyncio.create_task(self.generate_and_speak_eew(data))
            await self.play_eew_sound(data)

        except Exception as e:
            logger.error(f"notify_eew エラー:\n{traceback.format_exc()}")

    # ===============================
    # 読み上げエンジン
    # ===============================
    async def generate_and_speak_eew(self, data):
        """QuakeTsunami_antei.html の generateAndPlaySpeech をPythonで再現"""
        serial = int(data.get("Serial", 1))
        is_warn = data.get("isWarn", False)
        is_plum = data.get("isAssumption", False)
        hypo = data.get("Hypocenter", "不明")
        max_int_str = data.get("MaxIntensity", "")

        current_warn_areas = set()
        for area in data.get("WarnArea", []):
            if area.get("Type", "").lower() == "警報":
                chiiki = area.get("Chiiki")
                if chiiki:
                    current_warn_areas.add(REGION_MAP.get(chiiki, chiiki))

        prev_warn_areas = set()
        if self.last_eew_data:
            for area in self.last_eew_data.get("WarnArea", []):
                if area.get("Type", "").lower() == "警報":
                    chiiki = area.get("Chiiki")
                    if chiiki:
                        prev_warn_areas.add(REGION_MAP.get(chiiki, chiiki))

        area_changed = current_warn_areas != prev_warn_areas
        warn_area_text = "、".join(sorted(current_warn_areas)) + "では" if current_warn_areas else ""

        text = ""
        priority = 3

        if serial == 1 or self.last_eew_data is None:
            if is_warn:
                priority = 1
                text = f"緊急地震速報。{warn_area_text}強い揺れに警戒してください。{hypo}で地震。推定最大震度{max_int_str}。"
            else:
                priority = 3
                text = f"{hypo}で地震。推定最大震度{max_int_str}。"
                if is_plum:
                    text += "（PLUM法）"

        else:
            prev_max_int = self.last_eew_data.get("MaxIntensity", "")
            prev_hypo = self.last_eew_data.get("Hypocenter", "")

            if is_warn and area_changed and current_warn_areas:
                priority = 1
                text = f"緊急地震速報。{warn_area_text}強い揺れに警戒してください。"

            elif self._is_intensity_changed_significantly(prev_max_int, max_int_str):
                priority = 2
                text = f"推定最大震度{max_int_str}"

            elif hypo != prev_hypo:
                priority = 3
                text = f"{hypo}で地震。推定最大震度{max_int_str}。"

        if text:
            await self.speak_local(text, priority)

        self.last_eew_data = data.copy()

    def _is_intensity_changed_significantly(self, prev: str, current: str) -> bool:
        order = ["不明", "1", "2", "3", "4", "5弱", "推定5弱以上", "5強", "6弱", "6強", "7"]
        try:
            idx_prev = order.index(prev)
            idx_cur = order.index(current)
            return abs(idx_cur - idx_prev) >= 1
        except ValueError:
            return False

    # ===============================
    # EEW 音声再生ロジック
    # ===============================
    async def play_eew_sound(self, data):
        is_cancel = data.get("isCancel", False)
        serial = int(data.get("Serial", 1))
        is_final = data.get("isFinal", False)
        is_warn = data.get("isWarn", False)
        event_id = data.get("EventID")
        max_int_str = data.get("MaxIntensity", "不明")

        if self.last_eew_event_id != event_id:
            self.audio_flags = {"warning": False, "int3": False, "first": False, "final": False, "cancel": False}
            self.last_warn_areas = set()
            self.last_eew_event_id = event_id

        if is_cancel:
            if not self.audio_flags.get("cancel"):
                await self.play_mp3("eewC")
                self.audio_flags["cancel"] = True
                logger.debug("音声: eewC (キャンセル)")
            self.last_eew_data = data.copy()
            return

        current_warn_areas = {
            a.get("Chiiki") for a in data.get("WarnArea", [])
            if a.get("Type") == "警報"
        }

        should_play_high_alert = False
        if is_warn:
            if not self.audio_flags.get("warning"):
                should_play_high_alert = True
                self.audio_flags["warning"] = True
            else:
                new_areas = current_warn_areas - self.last_warn_areas
                if new_areas:
                    should_play_high_alert = True
                    logger.info(f"警報地域が追加されました: {new_areas}")

        if should_play_high_alert:
            await self.play_mp3("high_alert")
            self.last_warn_areas = current_warn_areas
            logger.debug(f"音声: high_alert (地域数: {len(current_warn_areas)})")
            self.last_eew_data = data.copy()
            return

        int3_or_higher = ["3", "4", "5弱", "5強", "6弱", "6強", "7", "推定5弱以上"]
        if not is_warn and max_int_str in int3_or_higher:
            if not self.audio_flags.get("int3"):
                await self.play_mp3("eew3")
                self.audio_flags["int3"] = True
                logger.debug("音声: eew3 (震度3以上)")
                self.last_eew_data = data.copy()
                return

        if is_final and not self.audio_flags.get("final"):
            await self.play_mp3("saisyu")
            self.audio_flags["final"] = True
            logger.debug("音声: saisyu (最終報)")

        elif serial > 1:
            await self.play_mp3("koushin")
            logger.debug("音声: koushin (更新)")

        elif serial == 1 and not self.audio_flags.get("first"):
            await self.play_mp3("low_alert")
            self.audio_flags["first"] = True
            logger.debug("音声: low_alert (初報)")

        self.last_eew_data = data.copy()

    # ===============================
    # P2P地震情報 画像URL生成
    # ===============================
    @staticmethod
    def p2p_image_url(image_id: str) -> str | None:
        if not image_id:
            return None
        return f"https://cdn.p2pquake.net/app/images/{image_id}_trim_big.png"

    # ===============================
    # ===============================
    # 長周期地震動モニタ EEW ポーリング
    # ===============================
    async def fetch_lmoni_eew_loop(self):
        """
        防災科研・長周期地震動モニタの EEW JSON を5秒ごとにポーリングし、
        新しい EEW を検知したら lmoni_eew_channel へ notify_eew で通知する。

        URL例: https://www.lmoni.bosai.go.jp/monitor/webservice/hypo/eew/20260512193259.json
        ファイル名は YYYYMMDDHHMMSS 形式で、サーバー側の遅延を考慮して
        現在時刻の数秒前から探索する。
        """
        DELAY_SEC  = 5    # サーバー遅延の考慮（秒）
        STEP_SEC   = 5    # 見つからない場合にさらに遡るステップ
        MAX_RETRY  = 6    # 最大試行回数（最大 DELAY_SEC + STEP_SEC * MAX_RETRY 秒前まで）
        POLL_SEC   = 5    # ポーリング間隔（秒）

        last_event_id = None

        async def find_latest_lmoni_eew() -> dict | None:
            """現在時刻から遡って最新の EEW JSON を返す。見つからなければ None。"""
            for i in range(MAX_RETRY):
                dt = datetime.now() - timedelta(seconds=DELAY_SEC + STEP_SEC * i)
                dt_str = dt.strftime("%Y%m%d%H%M%S")
                url = f"{LMONI_EEW_BASE}/{dt_str}.json"
                try:
                    async with self.session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            return await resp.json(content_type=None)
                except Exception:
                    pass
            return None

        def convert_lmoni_to_wolfx(data: dict) -> dict | None:
            """
            長周期地震動モニタ EEW JSON を Wolfx 形式に変換する。

            実際のフィールド仕様（確認済み）:
              report_id       : EventID に使用（"20260519020432" 形式）
              report_num      : 情報番号（文字列、空文字の場合あり）
              is_final        : 最終報フラグ（bool or 文字列）
              is_cancel       : 取消フラグ（bool or 文字列）
              is_training     : 訓練フラグ
              region_name     : 震央地名（"三陸沖" など）
              magunitude      : マグニチュード（typo: magnitude ではない）
              depth           : 深さ（"10km" 形式）
              latitude        : 緯度（文字列 "39.7"）
              longitude       : 経度（文字列 "143.5"）
              origin_time     : 発生時刻（"20260519020429" 形式 YYYYMMDDHHMMSS）
              calcintensity   : 最大予測震度（文字列 "1"）
              alertflg        : "予報" / "警報"
              avrarea_list    : 予測震度のある地域名リスト（細分区域名）
              avrval          : 平均震度（参考値）
            """
            try:
                event_id  = data.get("report_id", "")
                serial    = self.safe_int(data.get("report_num", "1")) or 1
                is_final  = self.safe_bool(data.get("is_final",  False))
                is_cancel = self.safe_bool(data.get("is_cancel", False))
                is_training = self.safe_bool(data.get("is_training", False))

                hypo_name = data.get("region_name", "不明") or "不明"

                # magunitude: LMoni のフィールド名は typo
                mag = self.safe_float(data.get("magunitude") or data.get("magnitude") or -1)

                # depth: "10km" 形式なので safe_int で km 部分を除去
                depth = self.safe_int(data.get("depth", "-1") or "-1")

                lat = self.safe_float(data.get("latitude",  -200) or -200)
                lon = self.safe_float(data.get("longitude", -200) or -200)

                # origin_time: "20260519020429" (YYYYMMDDHHMMSS) → "YYYY/MM/DD HH:MM" に変換
                raw_origin = str(data.get("origin_time", "") or "")
                if len(raw_origin) >= 12:
                    origin_time = (
                        f"{raw_origin[0:4]}/{raw_origin[4:6]}/{raw_origin[6:8]} "
                        f"{raw_origin[8:10]}:{raw_origin[10:12]}"
                    )
                else:
                    origin_time = raw_origin

                # 最大予測震度: calcintensity ("1", "2", ... "7" など) → INT_MAP の値に変換
                calc_int_raw = self.safe_int(data.get("calcintensity", -1))
                calc_int_map = {
                    -1: "不明", 0: "0", 1: "1", 2: "2", 3: "3", 4: "4",
                    5: "5弱", 6: "5強", 7: "6弱", 8: "6強", 9: "7"
                }
                max_intensity = calc_int_map.get(calc_int_raw, str(calc_int_raw) if calc_int_raw >= 0 else "不明")

                # 警報フラグ
                alertflg = data.get("alertflg", "")
                is_warn = alertflg == "警報"

                # avrarea_list → WarnArea（全エリアを同一震度で登録）
                warn_areas = []
                area_list = data.get("avrarea_list", []) or []
                for area_name in area_list:
                    if not area_name:
                        continue
                    warn_areas.append({
                        "Chiiki":  area_name,
                        "Shindo1": max_intensity,
                        "Shindo2": max_intensity,
                        "Type":    "警報" if is_warn else "予報",
                    })

                return {
                    "type":         "jma_eew",
                    "Title":        "緊急地震速報（長周期地震動モニタ）",
                    "EventID":      event_id,
                    "Serial":       serial,
                    "isFinal":      is_final,
                    "isCancel":     is_cancel,
                    "isTraining":   is_training,
                    "isAssumption": False,
                    "isWarn":       is_warn,
                    "OriginTime":   origin_time,
                    "Hypocenter":   hypo_name,
                    "Latitude":     lat,
                    "Longitude":    lon,
                    "Depth":        depth,
                    "Magnitude":    mag if mag > 0 else -1,
                    "Magunitude":   str(mag) if mag > 0 else "不明",
                    "MaxIntensity": max_intensity,
                    "WarnArea":     warn_areas,
                    "_source":      "長周期地震動モニタ",
                }
            except Exception:
                logger.error(f"LMoni→Wolfx変換エラー:\n{traceback.format_exc()}")
                return None

        logger.info("🌐 長周期地震動モニタ EEW ポーリング開始")

        while not self.bot.is_closed():
            try:
                raw = await find_latest_lmoni_eew()
                if raw:
                    # データなし（EEW未発表時）はスキップ
                    result = raw.get("result", {})
                    if result.get("message") == "データがありません":
                        await asyncio.sleep(POLL_SEC)
                        continue

                    event_id = raw.get("event_id") or raw.get("report_id")
                    if not event_id:
                        await asyncio.sleep(POLL_SEC)
                        continue
                    serial   = self.safe_int(raw.get("report_num", 1)) or 1

                    # 同一EventID+Serial は重複スキップ
                    sig = f"{event_id}_{serial}"
                    if sig != last_event_id:
                        last_event_id = sig
                        wolfx = convert_lmoni_to_wolfx(raw)
                        if wolfx:
                            logger.info(
                                f"LMoni EEW 検知: EventID={event_id} Serial={serial} "
                                f"震源={wolfx['Hypocenter']}"
                            )
                            await self.notify_eew(
                                wolfx,
                                source="lmoni",
                                start_monitor=False,
                            )
            except Exception:
                logger.error(f"LMoni EEW ポーリングエラー:\n{traceback.format_exc()}")

            await asyncio.sleep(POLL_SEC)

    # ===============================
    # EEW フォールバック監視
    # ===============================
    async def _eew_fallback_monitor(self):
        """
        Wolfx WebSocket heartbeat と EEW 受信を監視し、以下の条件で
        P2P EEW / LMoni EEW をフォールバックとして起動/停止する。

        起動条件：
          - WebSocket heartbeat 消失（monotonic 時刻で timeout 超過）
          - かつ EEW 未受信が EEW_FALLBACK_TIMEOUT 秒以上

        停止条件：
          - heartbeat が復活 または EEW が復活
        """
        await asyncio.sleep(60)  # 起動直後は待機（Wolfx 接続確立を待つ）
        logger.info(
            f"EEW フォールバック監視開始 "
            f"(heartbeat_timeout={WOLFX_HEARTBEAT_TIMEOUT}s, eew_timeout={EEW_FALLBACK_TIMEOUT}s)"
        )

        while not self.bot.is_closed():
            await asyncio.sleep(10)
            now = datetime.now()
            now_mono = time.monotonic()

            # 1. Heartbeat 監視: WebSocket 接続の生死判定
            if self._wolfx_last_heartbeat is not None:
                hb_elapsed = now_mono - self._wolfx_last_heartbeat
                if hb_elapsed > WOLFX_HEARTBEAT_TIMEOUT:
                    # Heartbeat timeout
                    if not self._wolfx_heartbeat_timeout_warned:
                        logger.debug(
                            f"Wolfx heartbeat timeout ({hb_elapsed:.1f}s > {WOLFX_HEARTBEAT_TIMEOUT}s) "
                            f"→ WebSocket 接続の生死不確定"
                        )
                        self._wolfx_heartbeat_timeout_warned = True
                    self._wolfx_ws_alive = False
                else:
                    # Heartbeat alive
                    if self._wolfx_ws_alive == False and self._wolfx_heartbeat_timeout_warned:
                        logger.info(
                            f"✅ Wolfx heartbeat 復旧 (elapsed={hb_elapsed:.1f}s)"
                        )
                        self._wolfx_heartbeat_timeout_warned = False
                    self._wolfx_ws_alive = True
            else:
                # まだ heartbeat 未受信（起動直後）
                self._wolfx_ws_alive = False

            # 2. EEW 受信監視
            eew_timeout_exceeded = False
            if self._wolfx_last_eew_recv is None:
                eew_elapsed = None
                eew_timeout_exceeded = True  # Never received = timeout already exceeded
            else:
                eew_elapsed = (now - self._wolfx_last_eew_recv).total_seconds()
                eew_timeout_exceeded = eew_elapsed > EEW_FALLBACK_TIMEOUT

            # 3. Fallback 起動判定
            # WebSocket heartbeat が消失 かつ EEW タイムアウト → fallback 起動
            should_activate_fallback = (
                not self._wolfx_ws_alive
                and eew_timeout_exceeded
                and not self._fallback_active
            )

            if should_activate_fallback:
                if self._wolfx_last_eew_recv is None:
                    eew_str = "起動後未受信"
                else:
                    eew_str = f"{int(eew_elapsed)}秒間 無受信"
                logger.warning(
                    f"⚠️ Wolfx EEW が応答しません "
                    f"(heartbeat timeout {hb_elapsed:.1f}秒、EEW {eew_str})"
                )
                logger.info(
                    f"P2P地震情報 / 長周期地震動モニタへフォールバックしました"
                )
                self._fallback_active = True
                if self._fallback_task is None or self._fallback_task.done():
                    async def _fallback_runner():
                        await asyncio.gather(
                            self.connect_p2p_eew_ws(),
                            self.fetch_lmoni_eew_loop(),
                            return_exceptions=True,
                        )
                    self._fallback_task = self.bot.loop.create_task(_fallback_runner())
                    logger.debug("P2P EEW / LMoni EEW フォールバックタスク起動完了")

            # 4. Fallback 停止判定
            # heartbeat 復活 または EEW 復活 → fallback 停止
            should_deactivate_fallback = (
                (self._wolfx_ws_alive or not eew_timeout_exceeded)
                and self._fallback_active
            )

            if should_deactivate_fallback:
                reason = ""
                if self._wolfx_ws_alive:
                    reason = "heartbeat 復活"
                if not eew_timeout_exceeded:
                    reason += (" & " if reason else "") + "EEW 復活"
                logger.warning(
                    f"✅ Wolfx EEW 復旧 ({reason})"
                )
                self._fallback_active = False
                if self._fallback_task and not self._fallback_task.done():
                    self._fallback_task.cancel()
                    logger.debug("P2P/LMoni EEW フォールバック停止")
                    self._fallback_task = None
                logger.info("P2P EEW / LMoni EEW フォールバック停止完了")

    # 強震モニタ監視ループ（振動レベル + 強震モニタ画像 + 長周期地震動モニタ画像）
    # ===============================
    async def vibration_monitor_loop(self, target_event_id):
        KWATCH_URL = "https://kwatch-24h.net/EQLevel.json"
        JMA_S_BASE = "https://smi.lmoniexp.bosai.go.jp/data/map_img/RealTimeImg/jma_s"
        LMONI_BASE = "https://www.lmoni.bosai.go.jp/monitor/data/data/map_img/RealTimeImg/abrspmx_s"
        DELAY_SEC  = 4
        STEP_SEC   = 5
        MAX_RETRY  = 4

        # 直前に取得できた画像URLをキャッシュして同一秒への重複HEADリクエストを防ぐ
        _last_jma_s_url:  str | None = None
        _last_lmoni_url:  str | None = None
        _last_jma_s_ts:   str        = ""
        _last_lmoni_ts:   str        = ""

        async def find_monitor_image(base_url: str, suffix: str,
                                     last_url: str | None, last_ts: str
                                     ) -> tuple[str | None, str]:
            """
            現在時刻 - DELAY_SEC から遡って画像 URL を返す。
            直前と同じタイムスタンプなら HEAD リクエストを省略してキャッシュを返す。
            戻り値: (url | None, timestamp_str)
            """
            for i in range(MAX_RETRY):
                dt = datetime.now() - timedelta(seconds=DELAY_SEC + STEP_SEC * i)
                ts = dt.strftime("%Y%m%d%H%M%S")
                # 同じタイムスタンプなら前回結果を再利用
                if ts == last_ts:
                    return last_url, last_ts
                url = f"{base_url}/{dt.strftime('%Y%m%d')}/{ts}.{suffix}.gif"
                try:
                    async with self.session.head(url, timeout=3) as resp:
                        if resp.status == 200:
                            return url, ts
                except Exception:
                    pass
            return None, last_ts

        start_time = datetime.now().timestamp()
        if not ENABLE_KYOSHIN:
            return
        channel = self.kyoshin_channel or self.channel

        try:
            while (self.monitored_event_id == target_event_id and
                   not self.bot.is_closed() and
                   datetime.now().timestamp() - start_time < 300):

                level = None
                try:
                    async with self.session.get(KWATCH_URL, timeout=3) as resp:
                        if resp.status == 200:
                            kdata = await resp.json()
                            level = int(kdata.get("l", 0))
                except Exception as e:
                    logger.error(f"強震モニタ: 振動レベル取得エラー: {e}")

                _last_jma_s_url, _last_jma_s_ts  = await find_monitor_image(
                    JMA_S_BASE, "jma_s", _last_jma_s_url, _last_jma_s_ts
                )
                _last_lmoni_url, _last_lmoni_ts = await find_monitor_image(
                    LMONI_BASE, "abrspmx_s", _last_lmoni_url, _last_lmoni_ts
                )

                if level is not None or _last_jma_s_url or _last_lmoni_url:
                    if level is not None:
                        if level >= 1000:
                            color = 0xFF0000
                        elif level >= 100:
                            color = 0xFFFF00
                        else:
                            color = 0xFFFFFF
                        level_str = f"**振動レベル: {level}**\n"
                    else:
                        color = 0xAAAAAA
                        level_str = "**振動レベル: 取得中...**\n"

                    description = (
                        level_str
                        + "\n※気象庁からの情報ではありません。あくまで参考値としてお使いください。"
                    )
                    embed = discord.Embed(
                        title="強震モニタ",
                        description=description,
                        color=color,
                        timestamp=datetime.now()
                    )

                    if _last_jma_s_url:
                        embed.set_image(url=_last_jma_s_url)
                    if _last_lmoni_url:
                        embed.set_thumbnail(url=_last_lmoni_url)

                    await channel.send(embed=embed)

                await asyncio.sleep(5)

        except asyncio.CancelledError:
            logger.info(f"強震モニタ監視終了 (EventID={target_event_id})")
        except Exception as e:
            logger.error(f"強震モニタ監視ループ エラー: {e}")
        finally:
            if self.monitored_event_id == target_event_id:
                self.monitored_event_id = None

    # ===============================
    # 地震情報通知
    # ===============================
    async def notify_quake(self, data, is_test=False, extra_note=None):
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

        # ===== フィルター判定（テスト通知は除外しない）=====
        if not is_test:
            # 情報種別フィルター
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

        name = hypo.get("name", "調査中")
        mag = hypo.get("magnitude", -1)
        depth = hypo.get("depth", -1)

        mag_str = f"M{mag}" if mag != -1 else "調査中"
        depth_str = f"約{depth}km" if depth != -1 else "調査中"

        occur_time = self.format_jma_time(eq.get("time", "不明"))

        description = (
            f"**発表機関： {source}**\n"
            f"**発生時刻： {occur_time}**\n"
            f"**震源地： {name}**\n"
            f"**最大震度： {max_scale_str}**\n"
            f"**マグニチュード： {mag_str}**\n"
            f"**深さ： {depth_str}**"
        )

        dom_tsunami = eq.get("domesticTsunami", "None")
        description += f"\n\n{TSUNAMI_MAP.get(dom_tsunami, '情報なし')}"

        # 訂正情報の表示（コメントの前）
        # None または "Unknown" の場合は表示しない
        correct = data.get("issue", {}).get("correct")
        if correct and correct not in ("Unknown",):
            correction_messages = {
                "ScaleOnly": "震度が更新されました",
                "DestinationOnly": "震源が更新されました", 
                "ScaleAndDestination": "震度・震源が更新されました"
            }
            correction_text = correction_messages.get(correct)
            if correction_text:
                description += f"\n\n**⚠ {correction_text}**"

        if comments:
            description += f"\n\n{comments}"

        embed.description = description

        quake_id = data.get("id")
        footer_parts = [extra_note] if extra_note else []
        if is_test:
            footer_parts.append("※これはテスト通知です。")
        if footer_parts:
            embed.set_footer(text=" | ".join(footer_parts))

        # 全ての地震情報種別で画像を表示
        if quake_id:
            map_url = self.p2p_image_url(quake_id)
            if map_url:
                embed.set_image(url=map_url)

        await channel.send(embed=embed)

        # occur_time は既に「YYYY年M月D日H時MM分頃」形式なので
        # 読み上げ用に「H時MM分頃、」部分だけ抽出する
        time_str = ""
        if occur_time and "時" in occur_time and "分頃" in occur_time:
            try:
                # 「H時MM分頃」部分を取り出す（例: "2026年4月18日8時20分頃" → "8時20分頃、"）
                t_part = occur_time[occur_time.index("日") + 1:]  # "8時20分頃"
                time_str = t_part + "、"
            except Exception:
                time_str = ""

        # 津波情報の読み上げ文
        tsunami_speak = ""
        tsunami_speak_map = {
            "MajorWarning": "現在、大津波警報を発表中です。",
            "Warning":      "現在、津波警報を発表中です。",
            "Watch":        "現在、津波注意報を発表中です。",
        }
        if dom_tsunami in tsunami_speak_map:
            tsunami_speak = " " + tsunami_speak_map[dom_tsunami]

        # issue_type 別の読み上げ
        if issue_type == "ScalePrompt":
            # 震度速報: 震源情報なし
            speak_text = (
                f"{title}。"
                f"{time_str}最大震度{max_scale_str}を観測する地震がありました。"
                f"震源地・規模は調査中です。{tsunami_speak}"
            )
        elif issue_type == "Destination":
            # 震源に関する情報: 震度なし
            speak_text = (
                f"{title}。"
                f"{time_str}地震がありました。"
                f" 震源地は {name}。"
                f" 深さは {depth_str}。"
                f" マグニチュードは {mag_str}。{tsunami_speak}"
            )
        elif issue_type in ("ScaleAndDestination", "DetailScale"):
            # 震度・震源情報: フル読み上げ
            speak_text = (
                f"{title}。"
                f"{time_str}最大震度{max_scale_str}を観測する地震がありました。{tsunami_speak}"
                f" 震源地は {name}。"
                f" 震源の深さは {depth_str}。"
                f" マグニチュードは {mag_str} と推定されます。"
            )
        elif issue_type == "Foreign":
            # 遠地地震
            speak_text = (
                f"{title}。"
                f"{time_str}遠地地震がありました。"
                f" 震源地は {name}。"
                f" マグニチュードは {mag_str}。{tsunami_speak}"
            )
        else:
            # Other など
            speak_text = (
                f"{title}。{tsunami_speak}"
                f" 震源地 {name}。 最大震度 {max_scale_str}。 {mag_str}。"
            )

        await self.speak_local(speak_text)

        await self.play_quake_sound(data)

    # ===============================
    # P2P地震情報のmp3音声
    # ===============================
    async def play_quake_sound(self, data):
        issue_type = data.get("issue", {}).get("type", "Other")
        if issue_type == "ScalePrompt":
            await self.play_mp3("vxse51")
        elif issue_type == "Destination":
            await self.play_mp3("vxse52")
        else:
            await self.play_mp3("vxse53")

    # ===============================
    # 津波情報通知
    # ===============================
    async def notify_tsunami(self, data, is_test=False):
        if not TSUNAMI_ENABLE and not is_test:
            return
        channel = self.tsunami_channel or self.channel
        if not channel:
            return

        try:
            cancelled = data.get("cancelled", False)
            areas = data.get("areas", [])
            time_str = self.format_jma_time(data.get("issue", {}).get("time", "不明"))
            tsunami_id = data.get("id")

            source = data.get("issue", {}).get("source", "P2P地震情報")

            title = "津波情報"
            if cancelled:
                title = "津波警報解除"
            if is_test:
                title = "【テスト】 " + title

            description = f"**発表機関： {source}**\n**発表時刻： {time_str}**\n\n"

            if cancelled:
                description += "すべての津波予報が解除されました。"
            else:
                area_groups = defaultdict(list)
                max_grade = "Unknown"
                for area in areas:
                    name = area.get("name", "不明")
                    grade = area.get("grade", "Unknown")
                    grade_str = TSUNAMI_MAP.get(grade, grade)
                    immediate = area.get("immediate", False)
                    max_h = area.get("maxHeight", {}).get("description", "不明")
                    first_cond = area.get("firstHeight", {}).get("condition", "")
                    first_time = area.get("firstHeight", {}).get("arrivalTime", "")

                    note = f"{grade_str}"
                    if immediate:
                        note += "　（ただちに来襲）"
                    if first_cond:
                        note += f" {first_cond}"
                    if first_time:
                        note += f"（{first_time}）"
                    if max_h != "不明":
                        note += f" 高さ:{max_h}"

                    area_groups[grade].append(f"　{name}　{note}")
                    if grade in ("MajorWarning", "Warning"):
                        max_grade = grade

                # 警報レベル別の注意喚起文
                alert_msg = ""
                if max_grade == "MajorWarning":
                    alert_msg = "\n🚨 **東日本大震災を思い出して！** 🚨\n"
                elif max_grade == "Warning":
                    alert_msg = "\n⚠️ **すぐ逃げて！** ⚠️\n"
                elif "Watch" in str(area_groups.keys()):
                    alert_msg = "\n⚠️ **海岸から離れて！** ⚠️\n"
                
                for grade in sorted(area_groups.keys(), key=lambda g: ["MajorWarning","Warning","Watch","Unknown"].index(g)):
                    items = area_groups[grade]
                    description += f"**■ {TSUNAMI_MAP.get(grade, grade)}**\n" + "　\n".join(items) + "\n"
                
                # 注意喚起を追加
                if alert_msg:
                    description += alert_msg

                # ===== 追加: コメント情報（Warning Comment）=====
                warning_comment = data.get("comments", {}).get("warningComment", {}).get("text", "")
                if warning_comment:
                    description += f"\n⚠️ **注意**: {warning_comment}\n"

            # ===== 追加: 原因地震情報の強化 =====
            eq = data.get("earthquake", {})
            if eq:
                eq_source = eq.get("source", "")
                if eq_source:
                    # source が既に description に含まれているかチェック
                    if "※" not in description:
                        description += f"\n\n※原因地震情報は {eq_source} からの情報です"

            # Discord API の制限（4096文字）に対応した正確な切り詰め
            description = self._truncate_embed_description(
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

            if tsunami_id:
                map_url = self.p2p_image_url(tsunami_id)
                if map_url:
                    embed.set_image(url=map_url)

            await channel.send(embed=embed)
            speak_text = f"{title} が発表されました"
            if cancelled:
                speak_text = "津波情報が解除されました"
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
        except Exception as e:
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
            report_time = self.format_jma_time(head.get("ReportDateTime", "不明"))
            
            # 原因地震情報
            cause_text = ""
            eq_list = body.get("Earthquake", [])
            if eq_list and len(eq_list) > 0:
                eq = eq_list[0] if isinstance(eq_list, list) else eq_list
                origin_time = self.format_jma_time(eq.get("OriginTime", "不明"))
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
                
                cause_text = f"原因地震： {hypo_name}　M{magnitude}{depth_str}（{origin_time}発生）{source_note}\n\n"
            
            # 説明文の開始
            description = (
                f"**{title}**\n\n"
                f"**発表機関:** {source}\n"
                f"**発表時刻:** {report_time}\n"
            )
            
            if cause_text:
                description += f"**{cause_text}**"
            
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
                description += f"\n⚠️ **注意**: {warning_comment}\n"
            
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
            logger.info(f"津波観測情報を通知しました: {title}")
            
        except Exception as e:
            logger.error(f"notify_tsunami_observation エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")


    async def notify_long_period(self, list_item, is_test=False, extra_note=None):
        if not ENABLE_LONG_PERIOD and not is_test:
            return
        channel = self.other_channel or self.channel
        if not channel:
            return

        try:
            json_filename = list_item.get("json")
            if not json_filename:
                return

            detail_url = f"https://www.jma.go.jp/bosai/ltpgm/data/{json_filename}"
            async with self.session.get(detail_url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status != 200:
                    return
                detail = await resp.json()

            source = detail.get("Control", {}).get("PublishingOffice", "気象庁")

            body = detail.get("Body", {})
            eq = body.get("Earthquake", {})
            intensity = body.get("Intensity", {}).get("Observation", {})

            origin_time = self.format_jma_time(eq.get("OriginTime", "不明"))

            hypo_name = eq.get("Hypocenter", {}).get("Area", {}).get("Name", "不明")
            magnitude = eq.get("Magnitude", "不明")
            max_lg = str(intensity.get("MaxLgInt", "不明"))

            depth_str = "不明"
            coord = eq.get("Hypocenter", {}).get("Area", {}).get("Coordinate", "")
            if coord and '-' in coord:
                try:
                    depth_m = int(coord.split('-')[-1].split('/')[0])
                    depth_km = abs(depth_m) // 1000
                    if depth_km > 0:
                        depth_str = f"{depth_km}km"
                except Exception:
                    pass

            lg_groups = defaultdict(list)
            for pref in intensity.get("Pref", []):
                for area in pref.get("Area", []):
                    area_name = area.get("Name", "")
                    lg_int = area.get("MaxLgInt", "不明")
                    if lg_int != "不明" and area_name:
                        lg_groups[lg_int].append(area_name)

            description = (
                f"**発表機関： {source}**\n"
                f"**発生時刻： {origin_time}**\n"
                f"**震源地： {hypo_name}**\n"
                f"**最大長周期地震動階級： {max_lg}**\n"
                f"**マグニチュード： M{magnitude}**\n"
                f"**深さ： 約{depth_str}**\n"
            )

            if lg_groups:
                description += "\n**各地の長周期地震動階級**"
                for lg in sorted(lg_groups.keys(), reverse=True):
                    description += f"\n■ 階級{lg}"
                    for area in sorted(lg_groups[lg]):
                        description += f"\n　{area}"

            color = LG_COLORS.get(max_lg, 0x62626B)

            embed = discord.Embed(
                title="長周期地震動に関する観測情報",
                description=description,
                color=color,
                timestamp=datetime.now()
            )

            footer = extra_note or ""
            if is_test:
                if footer:
                    footer += " | "
                footer += "※これはテスト通知です。"
            if footer:
                embed.set_footer(text=footer)

            await channel.send(embed=embed)

            # 読み上げ用に時刻の「H時MM分頃」部分を抽出
            time_speak = ""
            if origin_time and "時" in origin_time and "分頃" in origin_time:
                try:
                    time_speak = origin_time[origin_time.index("日") + 1:] + "に"
                except Exception:
                    pass

            speak_text = (
                f"長周期地震動に関する観測情報。"
                f"{time_speak}発生した"
                F"{hypo_name}を震源とする"
                f" マグニチュード{magnitude}の地震により、"
                f"最大長周期地震動階級{max_lg}を観測しました。"
            )
            await self.speak_local(speak_text)

        except Exception as e:
            logger.error(f"notify_long_period エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")

    # ===============================
    # 後発地震注意情報・南海トラフ臨時情報・顕著な地震の震源要素更新のお知らせ 通知
    # ===============================
    async def notify_quake_advisory(self, list_item=None, is_test=False, extra_note=None, detail_data=None):
        if not ENABLE_ADVISORY and not is_test:
            return
        channel = self.other_channel or self.channel
        if not channel:
            return

        try:
            if detail_data is None:
                json_filename = list_item.get("json")
                if not json_filename:
                    return
                detail_url = f"https://www.jma.go.jp/bosai/quake/data/{json_filename}"
                async with self.session.get(detail_url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status != 200:
                        return
                    detail = await resp.json()
            else:
                detail = detail_data

            source = detail.get("Control", {}).get("PublishingOffice", "気象庁")
            head = detail.get("Head", {})
            body = detail.get("Body", {})
            title_text = head.get("Title", "特別情報")
            if "顕著な地震の震源要素更新のお知らせ" in title_text:
                eq = body.get("Earthquake", {})
                origin_time = self.format_jma_time(eq.get("OriginTime", "不明"))

                hypo_name = eq.get("Hypocenter", {}).get("Area", {}).get("Name", "不明")
                magnitude = eq.get("Magnitude", "不明")

                depth_str = "不明"
                coord = eq.get("Hypocenter", {}).get("Area", {}).get("Coordinate_WGS", "")
                if coord and '-' in coord:
                    try:
                        depth_m = int(coord.split('-')[-1].split('/')[0])
                        depth_km = abs(depth_m) // 1000
                        if depth_km > 0:
                            depth_str = f"{depth_km}km"
                    except Exception:
                        pass

                comment = body.get("Comments", {}).get("FreeFormComment", "")

                description = (
                    f"**発表機関： {source}**\n"
                    f"**発生時刻： {origin_time}**\n"
                    f"**震源地： {hypo_name}**\n"
                    f"**マグニチュード： M{magnitude}**\n"
                    f"**深さ： {depth_str}**\n\n"
                    f"{comment}"
                )

                embed = discord.Embed(
                    title=f"{title_text}",
                    description=description,
                    color=0xFF4500,
                    timestamp=datetime.now()
                )

            else:
                text = body.get("EarthquakeInfo", {}).get("Text", "詳細情報なし")
                embed = discord.Embed(
                    title=f"{title_text}",
                    description=f"**発表機関： {source}**\n\n{text}",
                    color=0xFF4500,
                    timestamp=datetime.now()
                )
            footer_parts = []
            base_dir = os.path.dirname(os.path.abspath(__file__))
            image_file = None
            image_filename = None

            if "北海道・三陸沖後発地震注意情報" in title_text:
                image_filename = "hokkaido_bosaitaiou_area.png"
            elif "南海トラフ地震臨時情報" in title_text:
                image_filename = "nankai_bosaitaiou_area.png"

            if image_filename:
                image_path = os.path.join(base_dir, image_filename)
                if os.path.exists(image_path):
                    image_file = discord.File(image_path, filename=image_filename)
                    embed.set_image(url=f"attachment://{image_filename}")
                else:
                    footer_parts.append("⚠ 地図画像が見つかりません（同じフォルダに置いてください）")

            if extra_note:
                footer_parts.append(extra_note)
            if is_test:
                footer_parts.append("※これはテスト通知です。")
            if footer_parts:
                embed.set_footer(text=" | ".join(footer_parts))
            if image_file:
                await channel.send(embed=embed, file=image_file)
            else:
                await channel.send(embed=embed)

            speak_text = f"{title_text} が発表されました"
            if "顕著な地震の震源要素更新のお知らせ" in title_text:
                # origin_time は「YYYY年M月D日H時MM分頃」形式なので「H時MM分頃」部分を抽出
                time_speak = "先程"
                if origin_time and "日" in origin_time and "時" in origin_time:
                    try:
                        time_speak = origin_time[origin_time.index("日") + 1:].replace("頃", "")
                        # 例: "8時20分" → "8時20分"
                    except Exception:
                        pass
                speak_text = (
                    f"{time_speak}頃の地震について、震源要素を更新しました。"
                    f" 震源地 {hypo_name}"
                    f" マグニチュード M{magnitude}"
                    f" 深さ {depth_str}"
                )
            await self.speak_local(speak_text)

        except Exception as e:
            logger.error(f"notify_quake_advisory エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")

    # ===============================
    # USGS 地震情報通知
    # ===============================

    async def notify_usgs_quake(self, feature: dict, extra_note: str = ""):
        """
        USGS GeoJSON feature を Discord に通知する
        
        Parameters
        ----------
        feature : dict
            USGS GeoJSON の feature オブジェクト
        extra_note : str
            追加の注記（例：「（ボット起動時の最新情報）」）
        """
        channel = self.usgs_channel or self.channel
        if not channel:
            return
        
        try:
            props = feature.get("properties", {})
            geom = feature.get("geometry", {})
            coords = geom.get("coordinates", [None, None, None])
            
            event_id = feature.get("id", "Unknown")
            mag = props.get("mag", 0)
            place = props.get("place", "Unknown")
            time_ms = props.get("time", 0)
            lon, lat, depth_km = coords[0], coords[1], coords[2] if len(coords) > 2 else 0
            
            # 時刻をフォーマット（UNIX ミリ秒 → 日本時間）
            try:
                import datetime as dt
                quake_time = dt.datetime.fromtimestamp(
                    time_ms / 1000, 
                    tz=dt.timezone(dt.timedelta(hours=9))  # JST
                )
                occur_time_str = quake_time.strftime("%Y年%m月%d日%H時%M分")
            except Exception:
                occur_time_str = "不明"
            
            # Embed の色（マグニチュードで変更）
            if mag >= 7.0:
                color = 0xFF0000  # 赤
            elif mag >= 6.0:
                color = 0xFF6600  # オレンジ
            elif mag >= 5.0:
                color = 0xFFFF00  # 黄
            else:
                color = 0x00FF00  # 緑
            
            # Embed を作成
            title = f"🌍 USGS 地震情報 (M{mag:.1f})"
            description = (
                f"**発生時刻**: {occur_time_str}\n"
                f"**震源地**: {place}\n"
                f"**マグニチュード**: M{mag:.1f}\n"
                f"**深さ**: {depth_km:.1f} km\n"
                f"**座標**: {lat:.2f}°N, {lon:.2f}°E"
            )
            
            if extra_note:
                description += f"\n\n{extra_note}"
            
            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=datetime.now()
            )
            embed.set_footer(text="USGS Earthquake Hazards Program")
            
            # USGS リンク
            usgs_url = f"https://earthquake.usgs.gov/earthquakes/events/{event_id}/"
            embed.add_field(name="詳細情報", value=f"[USGS]({usgs_url})", inline=False)
            
            await channel.send(embed=embed)
            
            # 読み上げ
            speak_text = (
                f"USGS地震情報。"
                f"{occur_time_str}、"
                f"{place}で"
                f"マグニチュード{mag:.1f}の地震が発生しました。"
            )
            await self.speak_local(speak_text, priority=3)
            
            logger.info(f"USGS地震情報を通知しました: {event_id} / M{mag:.1f}")
        
        except Exception as e:
            logger.error(f"notify_usgs_quake エラー: {e}")
            logger.error(f"詳細:\n{traceback.format_exc()}")

# ===============================
# 火山情報通知
# ===============================

    async def fetch_volcano_info(self):
        """
        JMA 火山情報を取得・通知する。

        【処理手順】
        ① info.json をフェッチし、全オブジェクトの eventId を取得
        ② 前回取得と比較して変更・追加されたオブジェクトの eventId を抽出
        ③ 各 eventId で info/{eventId}.json をフェッチして Discord に通知
        ④ 次回ループのために現在の info.json を保存

        差分検知キー: _last_volcano_info_list（前回の info.json 全体を dict で保持）
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

        # Step②: 前回リストと比較して変更・追加された eventId を抽出
        # 初回は先頭1件のみ通知し、2回目以降は差分をすべて通知
        prev: dict[str, dict] = getattr(self, "_last_volcano_info_map", {})
        curr: dict[str, dict] = {item["eventId"]: item for item in info_list if item.get("eventId")}

        if not prev:
            # 初回: 先頭1件だけ通知（大量通知を防ぐ）
            first_item = info_list[0]
            event_id = first_item.get("eventId")
            if event_id:
                target_ids = [event_id]
                logger.info(f"Volcano: 初回起動 先頭1件を通知 eventId={event_id}")
            else:
                logger.debug("Volcano: 初回起動 eventId なし")
                self._last_volcano_info_map = curr
                return
        else:
            # 2回目以降: 前回にない eventId = 新規 or 更新として通知
            target_ids = [
                eid for eid in curr
                if eid not in prev
            ]
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
    # !status / /qtl_status コマンド
    # ===============================

    def _build_status_embed(self) -> discord.Embed:
        """ステータス Embed を組み立てて返す（!status と /qtl_status 共通）"""
        try:
            import psutil
            proc = psutil.Process()
            cpu  = proc.cpu_percent(interval=0.5)
            mem  = proc.memory_info().rss / 1024 / 1024
            mem_total = psutil.virtual_memory().total / 1024 / 1024
            disk = psutil.disk_usage("/")
            _psutil_ok = True
        except ImportError:
            _psutil_ok = False
            cpu = mem = mem_total = disk = None

        now = datetime.now()
        uptime = now - self._bot_start_time
        h, rem = divmod(int(uptime.total_seconds()), 3600)
        m, s   = divmod(rem, 60)
        uptime_str = f"{uptime.days}日 {h % 24}時間 {m}分 {s}秒"
        ping_ms = round(self.bot.latency * 1000)

        def api_status(key: str, warn_sec: int = 300, err_sec: int = 600) -> tuple[str, str]:
            t = self._last_recv.get(key)
            count = self._recv_count.get(key, 0)
            if t is None:
                return "⚪", "未受信"
            diff = int((now - t).total_seconds())
            time_str = t.strftime("%H:%M:%S")
            count_str = f"(計{count}件)"
            if diff < warn_sec:
                icon = "🟢"
            elif diff < err_sec:
                icon = "🟡"
            else:
                icon = "🔴"
            if diff < 60:
                ago = f"{diff}秒前"
            elif diff < 3600:
                ago = f"{diff // 60}分{diff % 60}秒前"
            else:
                ago = f"{diff // 3600}時間前"
            return icon, f"{time_str} ({ago}) {count_str}"

        def eew_source_status(is_active: bool, last_t: datetime | None) -> str:
            if not is_active:
                return "⚪ STANDBY"
            if last_t is None:
                return "🟢 ONLINE (未受信)"
            diff = int((now - last_t).total_seconds())
            if diff < EEW_FALLBACK_TIMEOUT:
                return f"🟢 ONLINE ({diff}秒前)"
            return f"🔴 OFFLINE ({diff}秒前)"

        def task_status(task_loop) -> str:
            if task_loop is None:
                return "⚪ 未起動"
            if task_loop.is_running():
                return "🟢 稼働中"
            if task_loop.failed():
                return "🔴 エラー停止"
            return "🟡 停止"

        def asyncio_task_status(task) -> str:
            if task is None:
                return "⚪ 未起動"
            if not task.done():
                return "🟢 稼働中"
            if task.cancelled():
                return "🟡 キャンセル"
            if task.exception() is not None:
                return "🔴 エラー停止"
            return "⚪ 完了"

        # Wolfx 状態
        now_mono = time.monotonic()
        if self._wolfx_last_heartbeat is None:
            wolfx_icon, wolfx_detail = "⚪", "heartbeat 未受信（起動中）"
        else:
            hb_elapsed = now_mono - self._wolfx_last_heartbeat
            if hb_elapsed < WOLFX_HEARTBEAT_TIMEOUT:
                wolfx_icon = "🟢"
                eew_detail = ""
                if self._wolfx_last_eew_recv is not None:
                    eew_diff = int((now - self._wolfx_last_eew_recv).total_seconds())
                    eew_detail = f", EEW {eew_diff}秒前"
                wolfx_detail = f"ONLINE ({hb_elapsed:.1f}s{eew_detail})"
            else:
                wolfx_icon = "🔴"
                wolfx_detail = f"heartbeat TIMEOUT ({hb_elapsed:.1f}s > {WOLFX_HEARTBEAT_TIMEOUT}s)"
                if self._fallback_active:
                    wolfx_detail += " → フォールバック中"

        color = 0x00FF00 if ping_ms < 100 else (0xFFFF00 if ping_ms < 300 else 0xFF0000)
        embed = discord.Embed(
            title="📊 QTL_Bot ステータス",
            color=color,
            timestamp=now,
        )

        # ── システム ──
        sys_lines = [f"⏱ **稼働時間**: {uptime_str}", f"📡 **Ping**: {ping_ms}ms"]
        if _psutil_ok:
            if STATUS_SHOW_CPU:
                sys_lines.append(f"🖥 **CPU**: {cpu:.1f}%")
            if STATUS_SHOW_MEM:
                sys_lines.append(f"🧠 **RAM**: {mem:.0f} / {mem_total:.0f} MB ({mem / mem_total * 100:.1f}%)")
            if STATUS_SHOW_DISK:
                sys_lines.append(f"💾 **Disk**: {disk.percent:.1f}% ({disk.used // 1024**3:.1f}/{disk.total // 1024**3:.1f} GB)")
        embed.add_field(name="🖥 システム", value="\n".join(sys_lines), inline=False)

        # ── EEW ──
        eew_lines = [
            f"{wolfx_icon} **Wolfx**: {wolfx_detail}",
            # P2P / LMoni は fallback 中のみ ACTIVE（Wolfx が正常時は STANDBY）
            f"{eew_source_status(self._fallback_active, self._last_recv.get('p2p_eew'))} **P2P EEW (fallback)**",
            f"{eew_source_status(self._fallback_active, self._last_recv.get('lmoni'))} **LMoni EEW (fallback)**",
        ]
        if self._fallback_active:
            eew_lines.append("⚠️ **フォールバック動作中** (Wolfx 停止検出)")
        embed.add_field(name="🚨 EEW", value="\n".join(eew_lines), inline=False)

        # ── API 受信状況 ──
        api_rows = [
            ("地震情報 (P2P)",   "quake",           120, 600),
            ("津波情報 (P2P)",   "tsunami",          60, 300),
            ("長周期地震動",     "long_period",      120, 600),
            ("津波観測情報",     "tsunami_obs",      120, 600),
            ("気象庁その他",     "quake_advisory",   120, 600),
            ("火山情報",         "volcano",         120, 600),
            ("USGS 地震情報",    "usgs",            600, 1200),
        ]
        api_lines = []
        for label, key, warn, err in api_rows:
            icon, detail = api_status(key, warn, err)
            api_lines.append(f"{icon} **{label}**: {detail}")
        embed.add_field(name="📡 API 受信状況", value="\n".join(api_lines), inline=False)

        # ── タスク稼働状態 ──
        task_lines = [
            f"{task_status(self.fetch_quake)} **fetch_quake**",
            f"{task_status(self.fetch_tsunami)} **fetch_tsunami**",
            f"{task_status(self.fetch_long_period)} **fetch_long_period**",
            f"{task_status(self.fetch_tsunami_observation)} **fetch_tsunami_observation**",
            f"{task_status(self.fetch_quake_advisory)} **fetch_quake_advisory**",
            f"{task_status(self.fetch_usgs_quake) if USGS_ENABLED else '⚪ 無効'} **fetch_usgs_quake**",
            f"{asyncio_task_status(self.speech_task)} **speech_worker**",
            f"{asyncio_task_status(self.mp3_task)} **mp3_worker**",
            f"{asyncio_task_status(self.volcano_task)} **volcano_poller**",
        ]
        embed.add_field(name="⚙️ タスク稼働状態", value="\n".join(task_lines), inline=False)

        # ── USGS 設定 ──
        if USGS_ENABLED:
            usgs_lines = [
                f"対象地域: 緯度 {USGS_REGION_LAT_MIN}〜{USGS_REGION_LAT_MAX} / 経度 {USGS_REGION_LON_MIN}〜{USGS_REGION_LON_MAX}",
                f"M下限: {USGS_MAGNITUDE_MIN} / ポーリング間隔: {USGS_FETCH_INTERVAL}秒 / 重複防止: {USGS_NOTIFICATION_COOLDOWN}秒",
            ]
            embed.add_field(name="🌍 USGS 設定", value="\n".join(usgs_lines), inline=False)

        # ── フィルター設定 ──
        if STATUS_SHOW_UPTIME:
            filter_lines = [
                f"震度下限: {INT_MAP.get(QUAKE_MIN_SCALE, str(QUAKE_MIN_SCALE))} / M下限: {QUAKE_MIN_MAG} / 深さ: {QUAKE_MIN_DEPTH}〜{QUAKE_MAX_DEPTH}km",
                f"EEWフォールバック閾値: {EEW_FALLBACK_TIMEOUT}秒",
            ]
            embed.add_field(name="⚙️ フィルター", value="\n".join(filter_lines), inline=False)

        return embed

    @commands.command(name="status")
    @commands.has_permissions(administrator=True)
    async def cmd_status(self, ctx):
        """Bot の稼働状態・各API受信状況・Ping を表示する"""
        embed = self._build_status_embed()
        await ctx.send(embed=embed)

    @cmd_status.error
    async def cmd_status_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ このコマンドはサーバー管理者のみ実行可能です。", delete_after=5)

    @discord.app_commands.command(name="qtl_status", description="QTL_Bot の稼働状態・各API受信状況を表示します（管理者専用）")
    @discord.app_commands.default_permissions(administrator=True)
    async def slash_qtl_status(self, interaction: discord.Interaction):
        """スラッシュコマンド版ステータス表示"""
        await interaction.response.defer(ephemeral=False)
        embed = self._build_status_embed()
        await interaction.followup.send(embed=embed)

    # ===============================
    # Web ダッシュボード
    # ===============================
    async def start_web_dashboard(self):
        """Web ダッシュボード（aiohttp）を起動"""
        from aiohttp import web
        try:
            import psutil
        except ImportError:
            psutil = None
        
        port = int(os.getenv("WEB_DASHBOARD_PORT", "8080"))
        
        async def status_handler(request):
            """GET /status - ステータス JSON を返す（拡充版）"""
            try:
                now = datetime.now()
                uptime_seconds = int(time.time() - self._start_time)
                uptime_str = self._format_uptime(uptime_seconds)

                # システムリソース
                system_info: dict = {}
                if psutil:
                    try:
                        proc = psutil.Process(os.getpid())
                        mem = proc.memory_info().rss / 1024 / 1024
                        mem_total = psutil.virtual_memory().total / 1024 / 1024
                        disk = psutil.disk_usage("/")
                        system_info = {
                            "cpu_percent": proc.cpu_percent(interval=None),
                            "memory_mb": round(mem, 1),
                            "memory_total_mb": round(mem_total, 1),
                            "memory_percent": round(mem / mem_total * 100, 1),
                            "disk_percent": disk.percent,
                            "disk_free_gb": round(disk.free / 1024**3, 2),
                        }
                    except Exception:
                        pass

                # API 受信状況ヘルパー
                def _api_info(key: str) -> dict:
                    t = self._last_recv.get(key)
                    return {
                        "last_recv_time": t.isoformat() if t else None,
                        "recv_count": self._recv_count.get(key, 0),
                    }

                # EEW 状態
                now_mono = time.monotonic()
                wolfx_hb = self._wolfx_last_heartbeat
                if wolfx_hb is None:
                    wolfx_ws_status = "connecting"
                    wolfx_hb_elapsed = None
                else:
                    wolfx_hb_elapsed = round(now_mono - wolfx_hb, 2)
                    wolfx_ws_status = "online" if wolfx_hb_elapsed < WOLFX_HEARTBEAT_TIMEOUT else "timeout"

                eew_info = {
                    "wolfx": {
                        "ws_status": wolfx_ws_status,
                        "heartbeat_elapsed_sec": wolfx_hb_elapsed,
                        "heartbeat_timeout_sec": WOLFX_HEARTBEAT_TIMEOUT,
                        "last_eew_id": self.last_eew_event_id,
                        **_api_info("wolfx"),
                    },
                    "p2p_eew": {
                        "fallback_active": self._fallback_active,
                        **_api_info("p2p_eew"),
                    },
                    "lmoni_eew": _api_info("lmoni"),
                    "fallback_active": self._fallback_active,
                }

                # タスク稼働状態
                def _loop_status(t) -> str:
                    if t is None: return "not_started"
                    if t.is_running(): return "running"
                    if t.failed(): return "error"
                    return "stopped"

                def _task_status(t) -> str:
                    if t is None: return "not_started"
                    if not t.done(): return "running"
                    if t.cancelled(): return "cancelled"
                    try:
                        t.exception()
                    except Exception:
                        return "error"
                    return "done"

                tasks_info = {
                    "fetch_quake": _loop_status(self.fetch_quake),
                    "fetch_tsunami": _loop_status(self.fetch_tsunami),
                    "fetch_long_period": _loop_status(self.fetch_long_period),
                    "fetch_tsunami_observation": _loop_status(self.fetch_tsunami_observation),
                    "fetch_quake_advisory": _loop_status(self.fetch_quake_advisory),
                    "fetch_usgs_quake": _loop_status(self.fetch_usgs_quake) if USGS_ENABLED else "disabled",
                    "speech_worker": _task_status(self.speech_task),
                    "mp3_worker": _task_status(self.mp3_task),
                    "volcano_poller": _task_status(self.volcano_task),
                }

                # USGS
                usgs_info: dict = {"enabled": USGS_ENABLED}
                if USGS_ENABLED:
                    usgs_last_ids = list(self.last_usgs_ids.keys())[-5:] if self.last_usgs_ids else []
                    usgs_info.update({
                        "magnitude_min": USGS_MAGNITUDE_MIN,
                        "fetch_interval_sec": USGS_FETCH_INTERVAL,
                        "region": {
                            "lat": [USGS_REGION_LAT_MIN, USGS_REGION_LAT_MAX],
                            "lon": [USGS_REGION_LON_MIN, USGS_REGION_LON_MAX],
                        },
                        "last_event_ids": usgs_last_ids,
                        **_api_info("usgs"),
                    })

                status_data = {
                    "status": "online",
                    "timestamp": now.isoformat(),
                    "bot_user": str(self.bot.user),
                    "uptime": uptime_str,
                    "uptime_seconds": uptime_seconds,
                    "ping_ms": round(self.bot.latency * 1000),
                    "system": system_info,
                    "eew": eew_info,
                    "api_status": {
                        "wolfx": self._last_recv.get("wolfx").isoformat() if self._last_recv.get("wolfx") else None,
                        "p2p_eew": self._last_recv.get("p2p_eew").isoformat() if self._last_recv.get("p2p_eew") else None,
                        "quake": self._last_recv.get("quake").isoformat() if self._last_recv.get("quake") else None,
                        "tsunami": self._last_recv.get("tsunami").isoformat() if self._last_recv.get("tsunami") else None,
                        "volcano": self._last_recv.get("volcano").isoformat() if self._last_recv.get("volcano") else None,
                    },
                    "recv_count": {
                        "wolfx": self._recv_count.get("wolfx", 0),
                        "p2p_eew": self._recv_count.get("p2p_eew", 0),
                        "quake": self._recv_count.get("quake", 0),
                        "tsunami": self._recv_count.get("tsunami", 0),
                        "long_period": self._recv_count.get("long_period", 0),
                        "tsunami_obs": self._recv_count.get("tsunami_obs", 0),
                        "volcano": self._recv_count.get("volcano", 0),
                        "usgs": self._recv_count.get("usgs", 0),
                    },
                    "monitoring": {
                        "quake": _api_info("quake"),
                        "tsunami": _api_info("tsunami"),
                        "long_period": _api_info("long_period"),
                        "tsunami_obs": _api_info("tsunami_obs"),
                        "quake_advisory": _api_info("quake_advisory"),
                        "volcano": {
                            "last_event_id": self._last_volcano_event_id,
                            "polling_status": _task_status(self.volcano_task),
                            **_api_info("volcano"),
                            "total_recv_count": self._volcano_recv_count,
                        },
                        "usgs": usgs_info,
                    },
                    "tasks": tasks_info,
                    # 後方互換フィールド
                    "last_eew": {
                        "event_id": self.last_eew_event_id,
                        "timestamp": self._last_recv.get("wolfx").isoformat() if self._last_recv.get("wolfx") else None,
                    },
                    "volcano_monitoring": {
                        "last_event_id": self._last_volcano_event_id,
                        "last_recv_time": self._last_volcano_recv_time.isoformat() if self._last_volcano_recv_time else None,
                        "polling_status": "active" if self.volcano_task and not self.volcano_task.done() else "inactive",
                        "total_recv_count": self._volcano_recv_count,
                    },
                    "memory_usage_mb": system_info.get("memory_mb", 0),
                }
                return web.json_response(status_data)
            except Exception as e:
                logger.error(f"Web ダッシュボード /status エラー: {e}")
                return web.json_response({"error": str(e)}, status=500)
        
        async def health_handler(request):
            """GET /health - ヘルスチェック"""
            return web.json_response({"status": "online"})
        
        async def health_full_handler(request):
            """GET /health/full - 詳細ヘルスチェック（各 API の疎通確認）"""
            try:
                result = await self.check_api_status()
                return web.json_response(result)
            except Exception as e:
                logger.error(f"/health/full エラー: {e}", exc_info=True)
                return web.json_response(
                    {"status": "error", "error": str(e)},
                    status=500
                )
        
        try:
            self._web_app = web.Application()
            self._web_app.router.add_get("/status", status_handler)
            self._web_app.router.add_get("/health", health_handler)
            self._web_app.router.add_get("/health/full", health_full_handler)
            
            self._web_runner = web.AppRunner(self._web_app)
            await self._web_runner.setup()
            site = web.TCPSite(self._web_runner, "0.0.0.0", port)
            await site.start()
            
            logger.info(f"✅ Web ダッシュボード起動: http://localhost:{port}/status")
        except Exception as e:
            logger.error(f"Web ダッシュボード起動失敗: {e}")
    
    def _format_uptime(self, seconds: int) -> str:
        """秒数を 'Xd XXh XXm' 形式に変換"""
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        return f"{days}d {hours}h {minutes}m"




    async def resource_monitor(self) -> None:
        """1時間ごとにリソース使用率をログに記録"""
        # psutil の可用性確認
        try:
            import psutil
        except ImportError:
            logger.warning("psutil がインストールされていません。リソース監視は無効です。")
            return
        
        if not RESOURCE_MONITORING_ENABLED:
            logger.info("リソース監視は無効です。")
            return
        
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(RESOURCE_CHECK_INTERVAL)
                
                # CPU・メモリ情報を取得
                try:
                    proc = psutil.Process()
                    cpu_percent = proc.cpu_percent(interval=1)
                    mem_info = proc.memory_info()
                    mem_mb = mem_info.rss / 1024 / 1024
                    
                    # ディスク情報を取得
                    disk_info = psutil.disk_usage('/')
                    disk_percent = disk_info.percent
                    disk_free_gb = disk_info.free / 1024 / 1024 / 1024
                    
                    # ログレベルを判定
                    log_msg = (
                        f"リソース監視 - CPU: {cpu_percent:.1f}%, "
                        f"MEM: {mem_mb:.1f}MB, "
                        f"DISK: {disk_percent}% (空き容量: {disk_free_gb:.1f}GB)"
                    )
                    
                    if disk_percent >= DISK_ERROR_THRESHOLD:
                        logger.error(f"❌ {log_msg} - ディスク使用率が {DISK_ERROR_THRESHOLD}% を超えています")
                    elif disk_percent >= DISK_WARNING_THRESHOLD:
                        logger.warning(f"⚠️  {log_msg} - ディスク使用率が {DISK_WARNING_THRESHOLD}% を超えています")
                    else:
                        logger.info(f"📊 {log_msg}")
                
                except Exception as e:
                    logger.error(f"リソース情報取得エラー: {e}")
            
            except asyncio.CancelledError:
                logger.info("resource_monitor が停止しました")
                break
            except Exception as e:
                logger.error(f"resource_monitor エラー: {e}")
                await asyncio.sleep(60)  # エラー時は 1 分待機して再試行

    async def check_api_status(self) -> dict:
        """各 API（Wolfx, JMA, P2P）の疎通確認"""
        import socket
        
        # キャッシュの確認（30秒以内なら使用）
        if (self.health_check_cache and 
            self.last_health_check_time and
            (datetime.now() - self.last_health_check_time).total_seconds() < HEALTH_CHECK_CACHE_TTL):
            return self.health_check_cache
        
        result = {
            "overall_status": "healthy",
            "last_check_time": datetime.now().isoformat(),
            "api_status": {
                "wolfx": {"ok": False, "latency_ms": None, "error": None},
                "jma": {"ok": False, "latency_ms": None, "error": None},
                "p2p": {"ok": False, "latency_ms": None, "error": None},
                "usgs": {"ok": False, "latency_ms": None, "error": None},
            }
        }
        
        try:
            import time
            
            # Wolfx WebSocket ping（TCP 接続確認）
            try:
                start = time.time()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(HEALTH_CHECK_TIMEOUT)
                await asyncio.wait_for(
                    asyncio.to_thread(sock.connect, ('api.wolfx.jp', 443)),
                    timeout=HEALTH_CHECK_TIMEOUT
                )
                sock.close()
                latency = (time.time() - start) * 1000
                result["api_status"]["wolfx"] = {
                    "ok": True,
                    "latency_ms": round(latency, 1),
                    "error": None
                }
            except Exception as e:
                result["api_status"]["wolfx"]["error"] = str(type(e).__name__)
            
            # JMA API ping
            try:
                start = time.time()
                async with self.session.get(
                    'https://www.jma.go.jp/bosai/common/const/area.json',
                    timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)
                ) as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        result["api_status"]["jma"] = {
                            "ok": True,
                            "latency_ms": round(latency, 1),
                            "error": None
                        }
            except Exception as e:
                result["api_status"]["jma"]["error"] = str(type(e).__name__)
            
            # P2P 地震情報 API ping
            try:
                start = time.time()
                async with self.session.get(
                    'https://api.p2pquake.net/v2/status',
                    timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)
                ) as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        result["api_status"]["p2p"] = {
                            "ok": True,
                            "latency_ms": round(latency, 1),
                            "error": None
                        }
            except Exception as e:
                result["api_status"]["p2p"]["error"] = str(type(e).__name__)
            
            # USGS API ping
            try:
                start = time.time()
                async with self.session.get(
                    'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson',
                    timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)
                ) as resp:
                    if resp.status == 200:
                        latency = (time.time() - start) * 1000
                        result["api_status"]["usgs"] = {
                            "ok": True,
                            "latency_ms": round(latency, 1),
                            "error": None
                        }
            except Exception as e:
                result["api_status"]["usgs"]["error"] = str(type(e).__name__)
            
            # overall_status の判定
            all_ok = all(api["ok"] for api in result["api_status"].values())
            result["overall_status"] = "healthy" if all_ok else "degraded"
            
        except Exception as e:
            logger.error(f"ヘルスチェック中にエラー: {e}", exc_info=True)
            result["overall_status"] = "unhealthy"
        
        # キャッシュに保存
        self.health_check_cache = result
        self.last_health_check_time = datetime.now()
        
        return result

    async def notify_error(self, error_msg: str, error_type: str = "Unknown") -> None:
        """エラーを管理者チャンネルに通知（重複防止付き）"""
        if not self.admin_channel:
            return  # 管理者チャンネルが設定されていない場合はスキップ
        
        try:
            # エラーハッシュを生成（同じエラーの重複防止用）
            error_hash = hash(f"{error_type}:{error_msg[:100]}")
            
            # 重複チェック（1時間以内に通知済みなら スキップ）
            current_time = datetime.now()
            if error_hash in self.error_notification_cache:
                last_notified = self.error_notification_cache[error_hash]
                if (current_time - last_notified).total_seconds() < ERROR_NOTIFICATION_TTL:
                    logger.debug(f"エラー通知をスキップ（重複防止）: {error_type}")
                    return
            
            # キャッシュを更新
            self.error_notification_cache[error_hash] = current_time
            
            # 日次エラー集計を更新
            self.error_count_today += 1
            if error_type not in self.daily_error_summary:
                self.daily_error_summary[error_type] = 0
            self.daily_error_summary[error_type] += 1
            
            # Discord embed を生成
            embed = discord.Embed(
                title="⚠️ エラー発生",
                description=f"**タイプ**: {error_type}\n**メッセージ**: {error_msg[:500]}",
                color=discord.Color.red(),
                timestamp=current_time
            )
            embed.add_field(name="発生時刻", value=current_time.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
            embed.add_field(name="本日のエラー件数", value=str(self.error_count_today), inline=True)
            embed.add_field(name="エラータイプ別", value=str(self.daily_error_summary), inline=False)
            embed.set_footer(text="QTL_Bot エラー監視")
            
            # Discord に送信
            await self.admin_channel.send(embed=embed)
            logger.info(f"エラー通知を送信しました: {error_type}")
            
        except Exception as e:
            logger.error(f"エラー通知の送信に失敗: {e}", exc_info=True)

    async def error_summary_worker(self) -> None:
        """毎日 00:00 に日次エラーサマリーを生成・送信"""
        while not self.bot.is_closed():
            try:
                # 次の 00:00 まで待機
                now = datetime.now()
                tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                wait_seconds = (tomorrow - now).total_seconds()
                
                logger.debug(f"日次エラーサマリー: {wait_seconds:.0f}秒後に実行")
                await asyncio.sleep(wait_seconds)
                
                # 日次サマリーを生成
                if not self.admin_channel or self.error_count_today == 0:
                    logger.debug("エラーサマリー: エラーがないため送信をスキップ")
                    # リセット
                    self.error_count_today = 0
                    self.daily_error_summary = {}
                    continue
                
                # embed を生成
                summary_text = "\n".join([
                    f"  • {etype}: {count} 件"
                    for etype, count in sorted(self.daily_error_summary.items(), key=lambda x: x[1], reverse=True)
                ])
                
                embed = discord.Embed(
                    title="📊 日次エラーサマリー",
                    description=f"**集計日**: {datetime.now().strftime('%Y-%m-%d')}\n**総エラー数**: {self.error_count_today} 件",
                    color=discord.Color.orange(),
                    timestamp=datetime.now()
                )
                embed.add_field(name="エラータイプ別集計", value=summary_text or "なし", inline=False)
                embed.set_footer(text="QTL_Bot エラー監視")
                
                # Discord に送信
                await self.admin_channel.send(embed=embed)
                logger.info(f"日次エラーサマリーを送信しました（{self.error_count_today}件）")
                
                # リセット
                self.error_count_today = 0
                self.daily_error_summary = {}
                
            except asyncio.CancelledError:
                logger.info("error_summary_worker が停止しました")
                break
            except Exception as e:
                logger.error(f"error_summary_worker エラー: {e}", exc_info=True)
                await asyncio.sleep(60)  # エラー時は 1 分待機して再試行

# ===============================
# Bot起動部
# ===============================

async def main():
    # ロギングをセットアップ
    setup_logging()
    
    async with bot:
        try:
            logger.info("■ Cog 初期化開始...")
            cog = QuakeTsunamiCog(bot)
            logger.info("  → QuakeTsunamiCog インスタンス化完了")
            
            logger.info("■ bot.add_cog() 実行中...")
            await bot.add_cog(cog)
            logger.info("  → Cog の追加完了")
            
            logger.info("■ bot.start() 実行中...")
            await bot.start(BOT_TOKEN)
        except Exception as e:
            logger.error(f"❌ Bot 起動エラー（詳細）: {type(e).__name__}: {e}", exc_info=True)
            raise


# ===== ロギング設定関数 =====

class _RateLimitedHandler(logging.Handler):
    """同一メッセージの重複ログを指定秒数抑制するハンドラーラッパー"""
    def __init__(self, inner: logging.Handler, threshold_sec: int = 60):
        super().__init__()
        self._inner = inner
        self._threshold = threshold_sec
        self._cache: dict[str, float] = {}
        self.setFormatter(inner.formatter)

    def setLevel(self, level):
        super().setLevel(level)
        self._inner.setLevel(level)

    def emit(self, record: logging.LogRecord):
        # ERROR/CRITICAL は常に出力
        if record.levelno >= logging.ERROR:
            self._inner.emit(record)
            return
        key = f"{record.levelno}:{record.getMessage()}"
        now = time.monotonic()
        last = self._cache.get(key, 0.0)
        if now - last < self._threshold:
            return
        self._cache[key] = now
        # 古いキャッシュを定期クリア（メモリリーク防止）
        if len(self._cache) > 2000:
            cutoff = now - self._threshold * 10
            self._cache = {k: v for k, v in self._cache.items() if v > cutoff}
        self._inner.emit(record)


class _SuppressHttpSuccessFilter(logging.Filter):
    """aiohttp.access ロガーの 2xx 成功ログを抑制するフィルター"""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # " 200 " や " 204 " などの成功ステータスを含む行を除外
        for code in (" 200 ", " 204 ", " 206 ", " 304 "):
            if code in msg:
                return False
        return True


def setup_logging():
    """ロギングハンドラーをセットアップ（ローテーション対応 + ログ肥大化対策）"""
    from logging.handlers import RotatingFileHandler
    global logger

    file_level   = getattr(logging, LOG_LEVEL_FILE.upper(),    logging.DEBUG)
    console_level = getattr(logging, LOG_LEVEL_CONSOLE.upper(), logging.INFO)

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── ファイルハンドラー（詳細・ローテーション）──
    _file_inner = RotatingFileHandler(
        "qtlbot.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    _file_inner.setLevel(file_level)
    _file_inner.setFormatter(fmt)
    file_handler = _RateLimitedHandler(_file_inner, LOG_DUPLICATE_THRESHOLD)
    file_handler.setLevel(file_level)

    # ── コンソールハンドラー（INFO 以上のみ + 重複抑制）──
    _con_inner = logging.StreamHandler()
    _con_inner.setLevel(console_level)
    _con_inner.setFormatter(fmt)
    console_handler = _RateLimitedHandler(_con_inner, LOG_DUPLICATE_THRESHOLD)
    console_handler.setLevel(console_level)

    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.setLevel(logging.DEBUG)  # ハンドラー側でフィルタリング

    # aiohttp.access ロガーの成功ログを抑制
    if LOG_SUPPRESS_HTTP_SUCCESS:
        access_logger = logging.getLogger("aiohttp.access")
        access_logger.addFilter(_SuppressHttpSuccessFilter())

    logger.info(
        f"ロギングをセットアップしました "
        f"(FILE={LOG_LEVEL_FILE}/CONSOLE={LOG_LEVEL_CONSOLE}, "
        f"maxSize={LOG_MAX_BYTES}bytes, dup抑制={LOG_DUPLICATE_THRESHOLD}s)"
    )


# ===== ヘルスチェック設定 =====
HEALTH_CHECK_TIMEOUT = 5  # API ping のタイムアウト（秒）
HEALTH_CHECK_CACHE_TTL = 30  # ヘルスチェック結果キャッシュ時間（秒）
ERROR_NOTIFICATION_TTL = 3600  # エラー通知の重複防止時間（秒）

# ===== A-2: ログローテーション設定（重複定義は除去済）=====
# LOG_MAX_BYTES / LOG_BACKUP_COUNT / LOG_LEVEL はファイル先頭で定義済み

# ===== A-5: リソース監視設定 =====
RESOURCE_MONITORING_ENABLED = os.getenv("RESOURCE_MONITORING_ENABLED", "true").lower() == "true"
RESOURCE_CHECK_INTERVAL = int(os.getenv("RESOURCE_CHECK_INTERVAL", "3600"))  # 1 時間
DISK_WARNING_THRESHOLD = int(os.getenv("DISK_WARNING_THRESHOLD", "80"))  # 80%
DISK_ERROR_THRESHOLD = int(os.getenv("DISK_ERROR_THRESHOLD", "90"))  # 90%
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 キーボード割り込みで終了します")
    except Exception as e:
        logger.error(f"予期しないエラーで終了: {e}\n{traceback.format_exc()}")
    finally:
        logger.info("Bot シャットダウン完了")
        logger.info("Bot 停止")