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
# 音声設定（AquesTalkPi / ScratchTTS）
# ===============================
# TTS_ENGINE: "aquestalk"（デフォルト、ローカルのAquesTalkPiバイナリを使用）
#             または "scratchtts"（Scratch の音声合成APIをネットワーク経由で使用）
TTS_ENGINE = os.getenv("TTS_ENGINE", "aquestalk").strip().lower()
if TTS_ENGINE not in ("aquestalk", "scratchtts"):
    logger.warning(f"TTS_ENGINE の値が不正です（{TTS_ENGINE!r}）。aquestalk にフォールバックします")
    TTS_ENGINE = "aquestalk"

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

# ScratchTTS のピッチシフト（core/tts_engines.py._pitch_shift_ffmpeg）で
# 使用する ffmpeg の実行コマンド（またはフルパス）。
# デフォルトはコマンド名のみとし、OSのPATHから解決させる
# （Raspberry Pi OS 等、`apt install ffmpeg` で標準的にPATHへ入る
# 環境を主に想定）。PATHが通っていない・別名でインストールされている
# 等の環境では、ここに絶対パスを指定することで対応できる。
# 【2026-08-04】入力音声のサンプルレート取得は ffprobe ではなく
# Python標準の wave モジュールで完結させる方針としたため、
# ffprobe（FFPROBE_PATH）への依存は撤廃した。ScratchTTSのレスポンスが
# WAV形式でない場合はピッチシフト自体を諦める（元音声のまま再生する）。
FFMPEG_PATH  = os.getenv("FFMPEG_PATH", "ffmpeg")

if TTS_ENGINE == "aquestalk" and not AQUESTALK_PATH:
    logger.info("AQUESTALK_PATH 未設定のため音声読み上げ機能は無効です（TTS_ENGINE=aquestalk）")

# ── ScratchTTS（Scratch音声合成API） ──
# エンドポイント仕様: https://synthesis-service.scratch.mit.edu/synth
#   ?locale=<言語ロケール>&gender=<female|male>&text=<喋る内容>
# ネットワーク経由のためレイテンシがAquesTalkPiより大きく、
# 外部サービスの可用性に依存する点に留意（fetch失敗時は読み上げをスキップする）。
SCRATCHTTS_URL    = os.getenv("SCRATCHTTS_URL", "https://synthesis-service.scratch.mit.edu/synth")
SCRATCHTTS_LOCALE = os.getenv("SCRATCHTTS_LOCALE", "ja-JP")
SCRATCHTTS_GENDER = os.getenv("SCRATCHTTS_GENDER", "female").strip().lower()
if SCRATCHTTS_GENDER not in ("female", "male"):
    logger.warning(f"SCRATCHTTS_GENDER の値が不正です（{SCRATCHTTS_GENDER!r}）。female にフォールバックします")
    SCRATCHTTS_GENDER = "female"
SCRATCHTTS_TIMEOUT_SEC = float(os.getenv("SCRATCHTTS_TIMEOUT_SEC", "10"))

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

# Web Dashboard のグラフ・履歴表示（/status/history）用のスナップショット
# 記録間隔・保持件数。RESOURCE_CHECK_INTERVAL（ログ出力用、デフォルト
# 1時間）とは別に、グラフとして見るには細かい間隔での記録が望ましい
# ため、専用の設定値を用意した。デフォルトは300秒（5分）間隔 ×
# 288件 = 24時間分。メモリ上のリングバッファ（deque）にのみ保持し、
# 外部DB等は使用しない（Bot再起動でリセットされる）。
STATUS_HISTORY_INTERVAL = _env_int("STATUS_HISTORY_INTERVAL", 300)
STATUS_HISTORY_MAXLEN   = _env_int("STATUS_HISTORY_MAXLEN", 288)
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

# 各地の震度に関する情報で、最大震度より1階級小さい震度を観測した
# 観測点数がこの件数以上の場合、都道府県ごとに1地点だけを代表として
# 表示し「（以下略）」を付けて省略する（通知文の肥大化防止）。
QUAKE_INTENSITY_COLLAPSE_THRESHOLD = _env_int("QUAKE_INTENSITY_COLLAPSE_THRESHOLD", 10)

# 緊急地震速報（EEW）で、警報対象の府県予報区数（region_map.json変換後）
# がこの件数以上の場合、pref_region_map.json でさらに地方予報区へ変換して
# 通知・読み上げる（例: 「北海道道南,北海道道央,...,千葉」10件以上
# → 「北海道,東北,関東」）。
EEW_REGION_COLLAPSE_THRESHOLD = _env_int("EEW_REGION_COLLAPSE_THRESHOLD", 10)

# ===============================
# 津波・その他通知フィルター
# ===============================
TSUNAMI_ENABLE     = _env_bool("TSUNAMI_ENABLE", True)
ENABLE_LONG_PERIOD = _env_bool("ENABLE_LONG_PERIOD", True)
ENABLE_ADVISORY    = _env_bool("ENABLE_ADVISORY", True)
ENABLE_TSUNAMI_OBS = _env_bool("ENABLE_TSUNAMI_OBS", True)
ENABLE_KYOSHIN     = _env_bool("ENABLE_KYOSHIN", True)

# ===============================
# EWS（緊急警報放送）信号音
# ===============================
# 津波警報・大津波警報（津波注意報・津波予報は対象外）が発表・更新された際に、
# 昭和60年郵政省告示第405号に準拠したAFSK緊急警報信号音を生成・再生する機能。
# 外部WAVファイルへは出力せず、メモリ上で生成したPCMデータを直接再生する
# （詳細は core/ews_signal.py 参照）。
EWS_ENABLE        = _env_bool("EWS_ENABLE", False)
# 告示別表第1号の地域符号キー（例: "全国共通", "東京都", "熊本県" 等）。
# core/ews_signal.py の REGION_CODES に定義されていないキーを指定した場合は
# "全国共通" にフォールバックする。
EWS_REGION        = _getenv_nonempty("EWS_REGION", "全国共通")
# 信号送信ブロック数（1ブロック=58bit、約0.9秒）。値が大きいほど信号音が長くなる。
EWS_BLOCKS        = _env_int("EWS_BLOCKS", 6)
# 前置・後置固定音（1024Hz単一正弦波）の長さ（秒）。
EWS_PRETONE_SEC    = float(os.getenv("EWS_PRETONE_SEC", "0.2"))
EWS_POSTTONE_SEC   = float(os.getenv("EWS_POSTTONE_SEC", "0.2"))

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
#
# 【2026-07-23 検知終了ロジックの根本的な再設計】
# 「一度検知すると通知が止まらない」バグが3回にわたり形を変えて
# 再発した。原因は「震度が高いというだけで無条件に上昇中とみなす
# 救済ロジック(旧KYOSHIN_HIGH_VALUE_BYPASS_SHINDO)」と「観測点ごとの
# 複雑な動的延長タイマー(旧KYOSHIN_STALE_AFTER_SEC等)」の組み合わせが
# 構造的に「自己終息しない状態」を生み出しやすい設計だったこと。
# 救済ロジックを完全に撤廃し（上昇判定は基準値との差分のみで行う。
# 震度が一定のまま推移すれば基準値も収束し、差分は自然にゼロへ近づく
# ため、上昇判定自体が放っておいても自己終息する）、イベントの生死は
# 「イベントに属するいずれかの観測点で最後に本物の上昇があった時刻」
# という単一の値(last_rise_at)だけで判定するよう単純化した。
# 旧KYOSHIN_HIGH_VALUE_BYPASS_SHINDO・旧KYOSHIN_STALE_AFTER_SECは廃止し、
# 単一のKYOSHIN_EVENT_TIMEOUT_SECに統合した。
KYOSHIN_GRID_SIZE            = _env_int("KYOSHIN_GRID_SIZE", 10)              # px。旧・画像ピクセルグリッド疑似観測点方式で使用（2026-08 実観測点方式へ切替のため廃止。後方互換のため定義のみ残す）
KYOSHIN_IMAGE_DELAY_SEC      = _env_int("KYOSHIN_IMAGE_DELAY_SEC", 6)         # 秒。NIED側の配信遅延を見込んで遡る基準秒数
KYOSHIN_IMAGE_STEP_SEC       = _env_int("KYOSHIN_IMAGE_STEP_SEC", 3)          # 秒。画像が見つからない場合にさらに遡るステップ幅
KYOSHIN_IMAGE_MAX_RETRY      = _env_int("KYOSHIN_IMAGE_MAX_RETRY", 4)         # 回。画像検索の最大リトライ回数

# 【2026-08-19 1.0に短縮】観測値取り込み〜tick()のポーリング間隔／
# イベント継続中の画像通知の再送間隔。より短い間隔で検知・通知できる
# ようにする一方、防災科研サーバーへのリクエスト頻度が単純に倍増する
# ため、KyoshinMonitorCog側でEEW発表中（EewCog.monitored_event_idが
# 設定されている間）はこのポーリング・画像通知を一時的に中断し、
# EewCog.vibration_monitor_loopに画像取得を一本化する連携を追加した
# （cogs/kyoshin_monitor.py の _is_eew_active 参照）。EEW最終報が
# 出た時点で通常のポーリングを自動的に再開する。
KYOSHIN_POLL_INTERVAL_SEC    = float(os.getenv("KYOSHIN_POLL_INTERVAL_SEC", "1.0"))   # 秒。観測値取り込み〜tick()のポーリング間隔
KYOSHIN_NOTIFY_INTERVAL_SEC  = float(os.getenv("KYOSHIN_NOTIFY_INTERVAL_SEC", "1.0")) # 秒。イベント継続中の画像通知の再送間隔

KYOSHIN_MIN_ACTIVE_PIXELS    = _env_int("KYOSHIN_MIN_ACTIVE_PIXELS", 2)       # 個。旧・画像ピクセルグリッド疑似観測点方式で使用（2026-08 実観測点方式へ切替のため廃止。後方互換のため定義のみ残す）

# HSVマスク処理で「揺れ候補ピクセル」とみなす実震度の下限値。
# core.kyoshin_shared.estimate_max_shindo_from_image
# （EEW発表時トリガーの振動モニタ機能）が引き続き使用する。
KYOSHIN_ACTIVE_SHINDO_FLOOR  = float(os.getenv("KYOSHIN_ACTIVE_SHINDO_FLOOR", "0.5"))

# 「上昇トリガー」とみなす実震度の上昇幅（基準値との差分）。
# ingen084氏の記事の核心となるパラメータ。単一観測点の震度が
# この幅以上急上昇した場合にのみ「候補」として扱う（絶対値ではなく
# 変化量を見ることで、常に薄く色が乗っているセルなどの静的ノイズを
# 自然に除外できる）。震度の絶対値による無条件の救済判定は行わない
# （震度が高止まりし続ける限り真であり続けてしまい、「検知が終わらない
# バグ」の直接原因になるため）。
#
# 【2026-08 一時的な引き上げ（0.5→1.0）】
# 実観測点データ方式への移行により、旧・画像ピクセルグリッド方式
# （N×Nピクセルの最大値、MIN_ACTIVE_PIXELS>=2フィルタ付き＝平滑化
# された低ノイズ信号）から、各観測点1ピクセルのみをそのまま使う
# 方式（平滑化なし＝高ノイズ信号）に変わった。0.5は低ノイズ信号
# 向けに調整された値だったため、高ノイズ信号に対しては閾値が
# 低すぎて誤検知が多発した。恒久対策（観測点座標周辺の複数ピクセル
# を平均・中央値で平滑化するパッチサンプリング等）を検討するまでの
# 一時的な緩和措置として1.0に引き上げる。
KYOSHIN_RISE_THRESHOLD = float(os.getenv("KYOSHIN_RISE_THRESHOLD", "1.0"))

# ===============================
# 強震モニタ: 実観測点データソース（2026-08〜）
# ===============================
# 画像ピクセルの疑似グリッド分割方式から、実際の観測点データ
# （ingen084氏の kyoshin-monitor-observation-points リポジトリが
# 配布する intensity-points.json）を用いる方式に切り替えた。
# 参考実装: https://github.com/akethimitsuhide/kyoshin-monitor-python
# （フォーク元: https://github.com/t0729/kyoshin-monitor-python）
#
# 起動時にキャッシュファイルが無い/古い場合はソースURLから取得し、
# 以後 KYOSHIN_STATIONS_REFRESH_SEC 秒ごとにバックグラウンドで
# 再取得・キャッシュ更新する（観測点データは「やや古い」ことがある
# 前提のため、定期的に追従する設計）。
KYOSHIN_STATIONS_SOURCE_URL = os.getenv(
    "KYOSHIN_STATIONS_SOURCE_URL",
    "https://raw.githubusercontent.com/ingen084/kyoshin-monitor-observation-points/refs/heads/master/intensity-points.json",
)
KYOSHIN_STATIONS_CACHE_PATH = os.getenv("KYOSHIN_STATIONS_CACHE_PATH", "kyoshin_stations_cache.json")
KYOSHIN_STATIONS_REFRESH_SEC = _env_int("KYOSHIN_STATIONS_REFRESH_SEC", 3600)  # 秒。1時間ごとに再フェッチ

# 近隣観測点として扱う件数（K近傍方式）。画像ピクセルグリッドの8近傍に
# 相当する概念だが、実観測点は分布密度が地域によって大きく異なる
# （都市部は密、山間部・離島は疎）ため、固定半径ではなく件数固定の
# K近傍方式を採用する。
KYOSHIN_NEIGHBOR_K = _env_int("KYOSHIN_NEIGHBOR_K", 6)

# 基準値(baseline)は「過去10〜25秒前」の範囲内サンプルの平均で計算する
# （参考: https://qiita.com/ingen084/items/82985e8d3227c97c608d
#  のHTML実装が採用するbaselineAvg方式）。単一の「N秒前ちょうどの値」
# より、ノイズ1点に基準値が左右されにくく安定する。
KYOSHIN_BASELINE_WINDOW_START_SEC = float(os.getenv("KYOSHIN_BASELINE_WINDOW_START_SEC", "10.0"))
KYOSHIN_BASELINE_WINDOW_END_SEC   = float(os.getenv("KYOSHIN_BASELINE_WINDOW_END_SEC", "25.0"))

# 観測点ごとに保持する震度履歴の長さ（秒）。KYOSHIN_BASELINE_WINDOW_END_SEC
# より短くすると基準値の計算に必要なサンプルが欠落するため、
# 通常はBASELINE_WINDOW_END_SECと同じか、それ以上の値にすること。
KYOSHIN_HISTORY_WINDOW_SEC = float(os.getenv("KYOSHIN_HISTORY_WINDOW_SEC", "25.0"))

# 上昇トリガーが立った観測点について、K近傍（KYOSHIN_NEIGHBOR_K）の
# 観測点のうち何点が「同時に」上昇トリガーを満たしていれば本物の
# 揺れとみなすか。ingen084氏の記事における「周囲の観測点も上昇して
# いた場合、揺れている、という判定を行う」の実装部分。値を上げる
# ほど、より広範囲で同時多発的な上昇でないと確定しなくなり、
# 誤検知に対して厳しくなる（その分、検知の即応性・感度は下がる）。
#
# 【2026-08 一時的な引き上げ（2→3、K=6中）】
# 実観測点データのK近傍は、都市部を中心に画像上でわずか数ピクセル
# しか離れていないケースが過半数を占めることが判明した
# （K近傍ペア9,792件中、4〜6px以内が62.8%、2〜3px以内が22.0%）。
# 強震モニタ画像は離散観測点を空間補間して描画していると考えられ、
# 数ピクセル程度の近接では色ノイズが独立ではなく相関しやすい。
# そのため「K近傍のうち2点」という以前の閾値では、単一のノイズ源が
# 複数の近隣観測点を同時に「上昇」させてしまい、空間クロスバリ
# デーションが機能しない誤検知が多発した。恒久対策（観測点座標
# 周辺の複数ピクセルを平均・中央値で平滑化するパッチサンプリング等）
# を検討するまでの一時的な緩和措置として、K=6中3点（過半数）に
# 引き上げる。この分、M2・最大震度1程度の小規模地震で検知漏れが
# 増える可能性があるが、現状は誤検知過多の方が優先度の高い問題と
# 判断した。
KYOSHIN_NEIGHBOR_TRIGGER_COUNT = _env_int("KYOSHIN_NEIGHBOR_TRIGGER_COUNT", 3)

# イベントの生死を決めるたった1つの値。イベントに属するいずれかの
# 観測点で最後に「本物の上昇トリガー」が立ってから、この秒数が
# 経過したら、震度の絶対値に関わらずイベントを終了する。
# 値を大きくすると「揺れが収まったと判定するまでの猶予」が長くなる
# （誤って早期に打ち切るリスクは下がるが、本当に収まった後も通知が
# 続く時間は長くなる）。
KYOSHIN_EVENT_TIMEOUT_SEC = float(os.getenv("KYOSHIN_EVENT_TIMEOUT_SEC", "45.0"))

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

# P2P地震情報の地図画像埋め込み（CDNへのリトライポーリング）を一時的に
# 無効化するフラグ。原因切り分けのため画像埋め込み処理自体をオフに
# できるようにする。無効化時は core.p2p_image.P2PImageMixin
# .build_p2p_image_url_text() が生成する画像URLを通知本文に含めることで、
# Discord自体のリンクプレビュー機能により画像が自動展開される
# （Bot側でのリトライ・embed編集は行わない）。
P2P_IMAGE_ATTACH_ENABLED = _env_bool("P2P_IMAGE_ATTACH_ENABLED", True)

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
