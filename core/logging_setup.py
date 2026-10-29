"""
core/logging_setup.py
======================
ロギングハンドラーのセットアップ、ログの圧縮保存（起動時・ローテーション時）、
重複ログ抑制、aiohttp成功ログ抑制を提供するモジュール。

【bot.py からの移行元】
元 bot.py の _RateLimitedHandler / _SuppressHttpSuccessFilter /
setup_logging() 定義（旧ファイル末尾付近）。起動時の「最新ログを書き込み先へ並べ替える」
処理（_align_logfiles_on_startup）は、起動時に全ログを .gz へ退避する方式に置き換えたため廃止。

【使い方】
    from core.logging_setup import setup_logging
    setup_logging()   # main() の最初に1回だけ呼ぶ
"""
import time
import logging

from core.config import (
    LOG_MAX_BYTES, LOG_BACKUP_COUNT, LOG_KEEP_DAYS,
    LOG_LEVEL_FILE, LOG_LEVEL_CONSOLE,
    LOG_DUPLICATE_THRESHOLD, LOG_SUPPRESS_HTTP_SUCCESS,
    LOG_FILE_PATH, KYOSHIN_LOG_LEVEL,
)

from core.log_archive import (
    ArchivingRotatingFileHandler, stage_existing_logs, start_background_archive,
)

logger = logging.getLogger("QTLBot")


class _RateLimitedHandler(logging.Handler):
    """同一メッセージの重複ログを指定秒数抑制するハンドラーラッパー。"""

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
        if len(self._cache) > 2000:
            cutoff = now - self._threshold * 10
            self._cache = {k: v for k, v in self._cache.items() if v > cutoff}
        self._inner.emit(record)


class _KyoshinLogFilter(logging.Filter):
    """強震モニタ・揺れ検知（kyoshin*.py）のログのうち、指定レベル未満を抑制するフィルター。

    強震モニタ系のモジュールはすべて同じロガー "QTLBot" を使うため、ロガー名ではなく
    ログの発生元ファイル名（kyoshin_monitor.py / kyoshin_detector.py など）で判定する。
    """

    def __init__(self, min_level: int):
        super().__init__()
        self._min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.filename.startswith("kyoshin") and record.levelno < self._min_level
        )


def _parse_level(name: str, default: int = logging.INFO) -> int:
    """ログレベル名（"WARNING" など）を数値へ。未知の値は default。"""
    level = getattr(logging, str(name).strip().upper(), None)
    return level if isinstance(level, int) else default


class _SuppressHttpSuccessFilter(logging.Filter):
    """aiohttp.access ロガーの 2xx 成功ログを抑制するフィルター。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for code in (" 200 ", " 204 ", " 206 ", " 304 "):
            if code in msg:
                return False
        return True


def setup_logging(archive_existing: bool = False) -> None:
    """
    ロギングハンドラーをセットアップする（ローテーション対応 + ログ肥大化対策）。

    archive_existing=True（通常起動）: 起動前に残っていたログをすべて .gz に圧縮して元を消す
    （改名だけ先に行い、圧縮は別スレッド。core/log_archive.py）。CLI モードや、他に稼働中の
    プロセスがある場合は False にして、稼働中プロセスのログを動かさない。
    """
    # 【2026-08-29 修正】従来は "qtlbot.log" という相対パスを直接
    # 埋め込んでいたため、systemdサービス化等で WorkingDirectory の
    # 設定を誤ると意図しないディレクトリにログが書かれる（最悪、
    # 権限エラーでログ自体が書けない）事故が起こりうる状態だった。
    # core.config.LOG_FILE_PATH は、未設定時はプロジェクトルート基準の
    # 絶対パスをデフォルトとしつつ、.env の LOG_FILE_PATH で明示的に
    # 上書きできるようにしたもの（ログの出力先を変えたい運用にも対応）。
    log_path = LOG_FILE_PATH

    # 起動時: 前回までのログを圧縮待ちへ改名（一瞬）。圧縮そのものは handler を作った後に別スレッドで行う
    staged = stage_existing_logs(log_path) if archive_existing else 0

    file_level    = getattr(logging, LOG_LEVEL_FILE.upper(), logging.DEBUG)
    console_level = getattr(logging, LOG_LEVEL_CONSOLE.upper(), logging.INFO)

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── ファイルハンドラー（詳細・ローテーション）──
    _file_inner = ArchivingRotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        keep_count=LOG_BACKUP_COUNT,
        keep_days=LOG_KEEP_DAYS,
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
    logger.propagate = False        # ルートロガーへの伝播を止める（重複出力防止の安全策）

    # 揺れ検知（kyoshin*.py）のログだけ別の最低レベルで絞る。setup_logging() を
    # 再度呼んでもフィルターが重複しないよう、既存分を外してから付ける。
    for f in [f for f in logger.filters if isinstance(f, _KyoshinLogFilter)]:
        logger.removeFilter(f)
    kyoshin_level = _parse_level(KYOSHIN_LOG_LEVEL)
    if kyoshin_level > logging.INFO:
        logger.addFilter(_KyoshinLogFilter(kyoshin_level))

    if LOG_SUPPRESS_HTTP_SUCCESS:
        access_logger = logging.getLogger("aiohttp.access")
        access_logger.addFilter(_SuppressHttpSuccessFilter())

    keep_note = f"{LOG_KEEP_DAYS}日" if LOG_KEEP_DAYS > 0 else f"{LOG_BACKUP_COUNT}世代"
    logger.info(
        f"ロギングをセットアップしました "
        f"(path={log_path}, FILE={LOG_LEVEL_FILE}/CONSOLE={LOG_LEVEL_CONSOLE}, "
        f"maxSize={LOG_MAX_BYTES}bytes, dup抑制={LOG_DUPLICATE_THRESHOLD}s, 圧縮ログの保持={keep_note})"
    )
    if archive_existing:
        # 前回までのログの圧縮・期限切れの削除（別スレッド。通常運用の処理を待たせない）
        if staged:
            logger.info(f"前回までのログ {staged} 件を .gz に圧縮します（バックグラウンド）")
        start_background_archive(log_path, LOG_BACKUP_COUNT, LOG_KEEP_DAYS)
