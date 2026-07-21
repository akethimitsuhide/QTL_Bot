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


def _getenv_nonempty(key: str, default: str) -> str:
    """
    os.getenv() の空文字フォールバック問題を回避するヘルパー。

    .env に `KEY=`（値が空）とだけ書かれている場合、os.getenv(key, default)
    は「キーが存在する」とみなして空文字列を返してしまい、default 側へ
    フォールバックしない。特にチャンネルID系の多段フォールバック
    （例: EEW_CHANNEL_ID未設定→CHANNEL_IDを使う、という設計）では、
    .env.example をそのまま .env にコピーしただけの状態（各行が
    `KEY=` の空値）で int() 変換が失敗し Bot が起動できなくなる。
    このヘルパーは値が空文字列の場合も「未設定」として扱い、
    default 引数（＝呼び出し側で組み立てた次のフォールバック値）を返す。
    """
    value = os.getenv(key)
    if value is None or value.strip() == "":
        return default
    return value


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
EEW_CHANNEL_ID       = int(_getenv_nonempty("EEW_CHANNEL_ID",       _getenv_nonempty("CHANNEL_ID", "0")))
QUAKE_CHANNEL_ID     = int(_getenv_nonempty("QUAKE_CHANNEL_ID",     _getenv_nonempty("CHANNEL_ID", "0")))
TSUNAMI_CHANNEL_ID   = int(_getenv_nonempty("TSUNAMI_CHANNEL_ID",   _getenv_nonempty("CHANNEL_ID", "0")))
OTHER_CHANNEL_ID     = int(_getenv_nonempty("OTHER_CHANNEL_ID",     _getenv_nonempty("CHANNEL_ID", "0")))
P2P_EEW_CHANNEL_ID   = int(_getenv_nonempty("P2P_EEW_CHANNEL_ID",   _getenv_nonempty("EEW_CHANNEL_ID", _getenv_nonempty("CHANNEL_ID", "0"))))
KYOSHIN_CHANNEL_ID   = int(_getenv_nonempty("KYOSHIN_CHANNEL_ID",   _getenv_nonempty("OTHER_CHANNEL_ID", _getenv_nonempty("CHANNEL_ID", "0"))))
ADMIN_CHANNEL_ID     = int(_getenv_nonempty("ADMIN_CHANNEL_ID",   "0"))
VOLCANO_CHANNEL_ID   = int(_getenv_nonempty("VOLCANO_CHANNEL_ID",   _getenv_nonempty("CHANNEL_ID", "0")))
USGS_CHANNEL_ID      = int(_getenv_nonempty("USGS_CHANNEL_ID", _getenv_nonempty("QUAKE_CHANNEL_ID", _getenv_nonempty("CHANNEL_ID", "0"))))

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
# 強震モニタ画像解析（Kyoshin）詳細設定
# ===============================
# 画像取得・解析パイプラインの各段階（グリッド分割 → 全セル震度化 →
# 時系列上昇幅の追跡 → 近隣同時上昇の検証 → 通知）を個別に
# チューニングできるようにする。値の意味は cogs/kyoshin_monitor.py
# 冒頭のdocstring、および core/kyoshin_detector.py の DetectorConfig を参照。
#
# 【2026-07-22 検知アルゴリズムの方針転換】
# 当初は「HSVマスクで抽出したアクティブセルを8近傍で連結成分化し、
# 最小サイズ以上・複数フレーム持続で確定させる」(ClusterTracker)方式
# だったが、これは震度の絶対値が静的に隣接しているかしか見ておらず、
# 単一観測点由来のGIF圧縮ノイズが偶然2〜3セルにまたがるだけで
# 誤検知に至るケースが多発した。
# ingen084氏の記事(https://qiita.com/ingen084/items/82985e8d3227c97c608d)
# が提唱する「観測点ごとの震度の時系列上昇幅を追跡し、近隣観測点も
# 同時に上昇しているか」という動的な変化ベースの判定（
# core.kyoshin_detector.EventManager.ingest()に実装済み）の方が、
# 静的な絶対値の隣接判定よりも本物の地震と単発ノイズを区別する
# 能力が高いと判断し、こちらを検知の主軸に切り替えた。
# ClusterTracker関連の設定(旧KYOSHIN_MIN_CLUSTER_SIZE / 
# KYOSHIN_REQUIRED_FRAMES)は廃止した。
KYOSHIN_GRID_SIZE            = _env_int("KYOSHIN_GRID_SIZE", 10)              # px。画像を何px四方の疑似観測点セルに分割するか
KYOSHIN_IMAGE_DELAY_SEC      = _env_int("KYOSHIN_IMAGE_DELAY_SEC", 6)         # 秒。NIED側の配信遅延を見込んで遡る基準秒数
KYOSHIN_IMAGE_STEP_SEC       = _env_int("KYOSHIN_IMAGE_STEP_SEC", 3)          # 秒。画像が見つからない場合にさらに遡るステップ幅
KYOSHIN_IMAGE_MAX_RETRY      = _env_int("KYOSHIN_IMAGE_MAX_RETRY", 4)         # 回。画像検索の最大リトライ回数
KYOSHIN_POLL_INTERVAL_SEC    = float(os.getenv("KYOSHIN_POLL_INTERVAL_SEC", "2.0"))   # 秒。観測値取り込み〜tick()のポーリング間隔
KYOSHIN_NOTIFY_INTERVAL_SEC  = float(os.getenv("KYOSHIN_NOTIFY_INTERVAL_SEC", "2.0")) # 秒。イベント継続中の画像通知の再送間隔
# ↑ 3.0→2.0に変更。EEW発表時の振動モニタ通知(cogs/quake.py側)と
#   間隔を統一するため。

KYOSHIN_MIN_ACTIVE_PIXELS    = _env_int("KYOSHIN_MIN_ACTIVE_PIXELS", 2)       # 個。1セル内でこの数以上「揺れ候補ピクセル」がないとアクティブとみなさない
# ↑ このピクセル数フィルタは、KyoshinImageAnalyzer.analyze_all() が
#   各セルの代表震度を計算する際の一次フィルタとして引き続き使う
#   （明らかに単一ピクセルしかないセルにまで反応しないようにするため）。
#   真の検知判定（誤検知対策の主眼）は下記のKYOSHIN_RISE_THRESHOLD /
#   KYOSHIN_NEIGHBOR_TRIGGER_COUNTが担う。

# HSVマスク処理で「揺れ候補ピクセル」とみなす実震度の下限値。
# これ未満の実震度に相当する色（背景の青〜水色域）は、GIFノイズの
# 温床であるため最初から解析対象に含めない（analyze_all()の一次フィルタ）。
# 【2026-07-21 緩和の経緯・1回目】当初は1.0（気象庁震度階級の震度1相当）
# だったが、実際の地震（気象庁震度1、山梨県東部・富士五湖）で
# 画像解析側の検知が一切反応しなかった事例が発生した。気象庁震度階級は
# 離散値、防災科研リアルタイム震度は連続値であり、気象庁震度1の地震でも
# 各観測点のリアルタイム震度実数値は0.5〜1.4程度に分布しうる。1.0だと
# その多くがHSVマスク段階で除外され、後段の判定に一切到達しない
# （＝検知そのものが起きない）ことが主要因だったと判断し、0.5に緩和した。
# 【2026-07-21 巻き戻し】0.5→0.2へさらに緩和したところ、単一観測点の
# GIF圧縮ノイズ（実震度0.2程度）まで解析対象に含まれるようになり、
# 数秒おきの誤検知（他社製ソフトでは検知しないレベルのノイズ）が
# 多発した。0.5に戻す。
# 【2026-07-22 追記】検知アルゴリズムをEventManager.ingest()による
# 時系列上昇幅ベースの判定に切り替えたため、この閾値はあくまで
# 「明らかに揺れていない色を除外する一次フィルタ」としての役割に
# 限定される。誤検知対策の主眼はKYOSHIN_RISE_THRESHOLDと
# KYOSHIN_NEIGHBOR_TRIGGER_COUNTに移した。
KYOSHIN_ACTIVE_SHINDO_FLOOR  = float(os.getenv("KYOSHIN_ACTIVE_SHINDO_FLOOR", "0.5"))

# 「上昇トリガー」とみなす実震度の上昇幅（過去10秒前の値との差分）。
# ingen084氏の記事の核心となるパラメータ。単一観測点の震度が
# この幅以上急上昇した場合にのみ「候補」として扱う（絶対値ではなく
# 変化量を見ることで、常に薄く色が乗っているセルなどの静的ノイズを
# 自然に除外できる）。
# デフォルトは core.kyoshin_detector.DetectorConfig の既定値(0.5)と
# 同じにしている。
KYOSHIN_RISE_THRESHOLD = float(os.getenv("KYOSHIN_RISE_THRESHOLD", "0.5"))

# 上昇トリガーが立った観測点について、8近傍の観測点のうち何点が
# 「同時に」上昇トリガーを満たしていれば本物の揺れとみなすか。
# ingen084氏の記事における「周囲の観測点も上昇していた場合、
# 揺れている、という判定を行う」の実装部分。値を上げるほど、
# より広範囲で同時多発的な上昇でないと確定しなくなり、誤検知に
# 対して厳しくなる（その分、検知の即応性・感度は下がる）。
KYOSHIN_NEIGHBOR_TRIGGER_COUNT = _env_int("KYOSHIN_NEIGHBOR_TRIGGER_COUNT", 2)

# 通知を送信する最小フェーズ（定性的な強さの下限）。
# Weaker < Weak < Medium < Strong < Stronger の順に強い。
# 例えば "Medium" を指定すると、Weaker/Weak 相当のイベントは検知はするが
# Discord 通知は送らない（誤検知抑制・通知過多防止のための調整用）。
# ingen084氏の記事の基準に準拠し、Bot側も最弱フェーズから通知する
# デフォルトとする。
KYOSHIN_MIN_NOTIFY_PHASE     = os.getenv("KYOSHIN_MIN_NOTIFY_PHASE", "Weaker")

# 通知を送るために必要な最小の検出観測点（グリッドセル）数を、
# 震度帯によって切り替える（震度が低いほど誤検知の可能性が高いため、
# より多くの観測点での同時検出を要求する）。
# 実震度(event.max_shindo)が1.0未満（震度0相当）の場合はこちら。
KYOSHIN_MIN_STATIONS_SHINDO0 = _env_int("KYOSHIN_MIN_STATIONS_SHINDO0", 4)
# 実震度が1.0以上（震度1相当以上）の場合はこちら。
KYOSHIN_MIN_STATIONS_SHINDO1 = _env_int("KYOSHIN_MIN_STATIONS_SHINDO1", 2)

# デバッグ用: アクティブセルが1件以上あったフレームの元画像をローカルに
# 一時保存するか。誤検知の事後検証用。常時有効にするとディスクを
# 圧迫するため、通常運用では false を推奨。
KYOSHIN_DEBUG_SAVE_IMAGE     = _env_bool("KYOSHIN_DEBUG_SAVE_IMAGE", False)
KYOSHIN_DEBUG_IMAGE_DIR      = os.getenv("KYOSHIN_DEBUG_IMAGE_DIR", "./kyoshin_debug_images")

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

# ===============================
# APM (Application Performance Monitoring) 設定
# ===============================
# Mackerel の APM（トレーシング）連携。OpenTelemetry (OTLP) 経由で送信する。
# デフォルトは無効。有効にする場合は下記の環境変数と、
# requirements.txt の "APM (Mackerel連携)" セクションのパッケージが必要。
#
# 値は Mackerel 公式ドキュメント
# (https://mackerel.io/ja/docs/entry/tracing/installations/python) の
# 記載に基づく。APM_OTLP_ENDPOINT は /v1/traces を含む完全な URL を指定すること
# （core/apm.py 側でパスを追加結合しないため、末尾は必ず /v1/traces にする）。
APM_ENABLED            = _env_bool("APM_ENABLED", False)
APM_SERVICE_NAME       = os.getenv("APM_SERVICE_NAME", "QTL_Bot")
APM_MACKEREL_API_KEY   = os.getenv("APM_MACKEREL_API_KEY", "")
APM_OTLP_ENDPOINT      = os.getenv("APM_OTLP_ENDPOINT", "https://otlp-vaxila.mackerelio.com/v1/traces")
APM_OTLP_API_KEY_HEADER = os.getenv("APM_OTLP_API_KEY_HEADER", "Mackerel-Api-Key")
