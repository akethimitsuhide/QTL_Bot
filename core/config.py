"""
core/config.py
===============
QTL_Bot の全環境変数・グローバル設定値を1箇所に集約するモジュール。

【設計方針】
- .env の読み込みと全ての os.getenv() 呼び出しはこのファイルのみで行う。
- 他のモジュール（cogs/*.py, core/*.py）は
  `from core.config import CHANNEL_ID, QUAKE_MIN_SCALE, ...`
  のように必要な定数だけを import する。
- 循環importを避けるため、このファイルは logger 以外の
  他モジュール（cogs等）に依存してはならない。

【bot.py からの移行元】
元 bot.py の 1〜90行目付近（import 直後〜Bot初期化前）に相当。
"""
import os
import logging
from dotenv import load_dotenv

# ===============================
# ロガー（設定値読み込み時のログ出力用）
# ===============================
# 本格的なハンドラー設定は core/logging_setup.py の setup_logging() が行う。
# ここでは「モジュール名 QTLBot」のロガーを取得するだけ。
logger = logging.getLogger("QTLBot")

# ===============================
# .env 読み込み
# ===============================
load_dotenv()


# ===============================
# 環境変数ヘルパー
# ===============================
def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        logger.critical(f"環境変数 {key} が設定されていません。.env を確認してください。")
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


# ===============================
# ロギング設定
# ===============================
LOG_MAX_BYTES             = int(os.getenv("LOG_MAX_BYTES", "10485760"))   # 10 MB
LOG_BACKUP_COUNT          = int(os.getenv("LOG_BACKUP_COUNT", "7"))
LOG_LEVEL                 = os.getenv("LOG_LEVEL", "INFO")
LOG_LEVEL_FILE            = os.getenv("LOG_LEVEL_FILE", LOG_LEVEL)
LOG_LEVEL_CONSOLE         = os.getenv("LOG_LEVEL_CONSOLE", LOG_LEVEL)
LOG_DUPLICATE_THRESHOLD   = int(os.getenv("LOG_DUPLICATE_THRESHOLD", "60"))
LOG_SUPPRESS_HTTP_SUCCESS = os.getenv("LOG_SUPPRESS_HTTP_SUCCESS", "true").lower() == "true"

# ===============================
# Discord 基本設定
# ===============================
BOT_TOKEN = _require_env("BOT_TOKEN")

_channel_id_raw = _require_env("CHANNEL_ID")
try:
    CHANNEL_ID = int(_channel_id_raw)
except ValueError:
    logger.critical(f"CHANNEL_ID は数値で指定してください（現在の値: {_channel_id_raw!r}）")
    raise SystemExit(1)

# ===============================
# 音声設定（AquesTalkPi）
# ===============================
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

# ===============================
# チャンネル ID 設定
# ===============================
EEW_CHANNEL_ID       = int(os.getenv("EEW_CHANNEL_ID",       os.getenv("CHANNEL_ID", "0")))
QUAKE_CHANNEL_ID     = int(os.getenv("QUAKE_CHANNEL_ID",     os.getenv("CHANNEL_ID", "0")))
TSUNAMI_CHANNEL_ID   = int(os.getenv("TSUNAMI_CHANNEL_ID",   os.getenv("CHANNEL_ID", "0")))
OTHER_CHANNEL_ID     = int(os.getenv("OTHER_CHANNEL_ID",     os.getenv("CHANNEL_ID", "0")))
P2P_EEW_CHANNEL_ID   = int(os.getenv("P2P_EEW_CHANNEL_ID",   os.getenv("EEW_CHANNEL_ID", os.getenv("CHANNEL_ID", "0"))))
KYOSHIN_CHANNEL_ID   = int(os.getenv("KYOSHIN_CHANNEL_ID",   os.getenv("OTHER_CHANNEL_ID", os.getenv("CHANNEL_ID", "0"))))
ADMIN_CHANNEL_ID     = int(os.getenv("ADMIN_CHANNEL_ID",   "0"))
VOLCANO_CHANNEL_ID   = int(os.getenv("VOLCANO_CHANNEL_ID",   os.getenv("CHANNEL_ID", "0")))
USGS_CHANNEL_ID      = int(os.getenv("USGS_CHANNEL_ID", os.getenv("QUAKE_CHANNEL_ID", os.getenv("CHANNEL_ID", "0"))))

# ===============================
# USGS 地震情報設定
# ===============================
USGS_ENABLED               = _env_bool("USGS_ENABLED", True)
USGS_MAGNITUDE_MIN         = float(os.getenv("USGS_MAGNITUDE_MIN", "5.0"))
USGS_FETCH_INTERVAL        = int(os.getenv("USGS_FETCH_INTERVAL", "600"))
USGS_REGION_LAT_MIN        = float(os.getenv("USGS_REGION_LAT_MIN", "20"))
USGS_REGION_LAT_MAX        = float(os.getenv("USGS_REGION_LAT_MAX", "50"))
USGS_REGION_LON_MIN        = float(os.getenv("USGS_REGION_LON_MIN", "120"))
USGS_REGION_LON_MAX        = float(os.getenv("USGS_REGION_LON_MAX", "180"))
USGS_NOTIFICATION_COOLDOWN = int(os.getenv("USGS_NOTIFICATION_COOLDOWN", "300"))

# ===============================
# リソース監視設定
# ===============================
RESOURCE_MONITORING_ENABLED = _env_bool("RESOURCE_MONITORING_ENABLED", True)
RESOURCE_CHECK_INTERVAL     = _env_int("RESOURCE_CHECK_INTERVAL", 3600)
DISK_WARNING_THRESHOLD      = _env_int("DISK_WARNING_THRESHOLD", 80)
DISK_ERROR_THRESHOLD        = _env_int("DISK_ERROR_THRESHOLD", 90)

# ===============================
# ヘルスチェック設定
# ===============================
HEALTH_CHECK_TIMEOUT   = 5
HEALTH_CHECK_CACHE_TTL = 30
ERROR_NOTIFICATION_TTL = 3600

# ===============================
# EEW フィルター（Wolfx）
# ===============================
EEW_MIN_INTENSITY = _env_int("EEW_MIN_INTENSITY", 0)

# ===============================
# 地震通知フィルター
# ===============================
QUAKE_MIN_SCALE              = _env_int("QUAKE_MIN_SCALE", 0)
QUAKE_MIN_MAG                = float(os.getenv("QUAKE_MIN_MAG", "0.0"))
QUAKE_MIN_DEPTH              = _env_int("QUAKE_MIN_DEPTH", 0)
QUAKE_MAX_DEPTH              = _env_int("QUAKE_MAX_DEPTH", 9999)
QUAKE_ENABLE_SCALE_PROMPT    = _env_bool("QUAKE_ENABLE_SCALE_PROMPT", True)
QUAKE_ENABLE_DESTINATION     = _env_bool("QUAKE_ENABLE_DESTINATION", True)
QUAKE_ENABLE_SCALE_AND_DEST  = _env_bool("QUAKE_ENABLE_SCALE_AND_DEST", True)
QUAKE_ENABLE_DETAIL_SCALE    = _env_bool("QUAKE_ENABLE_DETAIL_SCALE", True)
QUAKE_ENABLE_FOREIGN         = _env_bool("QUAKE_ENABLE_FOREIGN", True)
QUAKE_ENABLE_OTHER           = _env_bool("QUAKE_ENABLE_OTHER", True)

# ===============================
# 津波・その他通知フィルター
# ===============================
TSUNAMI_ENABLE     = _env_bool("TSUNAMI_ENABLE", True)
ENABLE_LONG_PERIOD = _env_bool("ENABLE_LONG_PERIOD", True)
ENABLE_ADVISORY    = _env_bool("ENABLE_ADVISORY", True)
ENABLE_TSUNAMI_OBS = _env_bool("ENABLE_TSUNAMI_OBS", True)
ENABLE_KYOSHIN     = _env_bool("ENABLE_KYOSHIN", True)

# ===============================
# EEW / API エラー挙動設定
# ===============================
WOLFX_HEARTBEAT_TIMEOUT = _env_int("WOLFX_HEARTBEAT_TIMEOUT", 90)
FETCH_FAILURE_THRESHOLD = _env_int("FETCH_FAILURE_THRESHOLD", 3)
FETCH_BACKOFF_SECONDS   = _env_int("FETCH_BACKOFF_SECONDS", 60)

# ===============================
# キュー設定
# ===============================
SPEECH_QUEUE_MAXSIZE = _env_int("SPEECH_QUEUE_MAXSIZE", 200)
MP3_QUEUE_MAXSIZE    = _env_int("MP3_QUEUE_MAXSIZE", 50)

# ===============================
# !status / /qtl_status 表示設定
# ===============================
STATUS_SHOW_CPU    = _env_bool("STATUS_SHOW_CPU", True)
STATUS_SHOW_MEM    = _env_bool("STATUS_SHOW_MEM", True)
STATUS_SHOW_DISK   = _env_bool("STATUS_SHOW_DISK", True)
STATUS_SHOW_UPTIME = _env_bool("STATUS_SHOW_UPTIME", True)

# ===============================
# 外部 API エンドポイント
# ===============================
WOLFX_WSS = "wss://ws-api.wolfx.jp/jma_eew"
P2P_WSS   = "wss://api.p2pquake.net/v2/ws"
P2P_API   = "https://api.p2pquake.net/v2/history"
