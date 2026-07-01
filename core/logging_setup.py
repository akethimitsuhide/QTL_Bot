"""
core/logging_setup.py
======================
ロギングハンドラーのセットアップ、ログローテーション不整合の修正、
重複ログ抑制、aiohttp成功ログ抑制を提供するモジュール。

【bot.py からの移行元】
元 bot.py の _RateLimitedHandler / _SuppressHttpSuccessFilter /
_align_logfiles_on_startup / setup_logging() 定義（旧ファイル末尾付近）。

【使い方】
    from core.logging_setup import setup_logging
    setup_logging()   # main() の最初に1回だけ呼ぶ
"""
import os
import time
import logging
from logging.handlers import RotatingFileHandler

from core.config import (
    LOG_MAX_BYTES, LOG_BACKUP_COUNT,
    LOG_LEVEL_FILE, LOG_LEVEL_CONSOLE,
    LOG_DUPLICATE_THRESHOLD, LOG_SUPPRESS_HTTP_SUCCESS,
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


class _SuppressHttpSuccessFilter(logging.Filter):
    """aiohttp.access ロガーの 2xx 成功ログを抑制するフィルター。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for code in (" 200 ", " 204 ", " 206 ", " 304 "):
            if code in msg:
                return False
        return True


def _align_logfiles_on_startup(base: str, backup_count: int) -> None:
    """
    起動・再起動時に base 〜 base.{backup_count} の中で
    最も mtime が新しいファイルを base の位置（書き込み先）に持ってくる。

    RotatingFileHandler は常に base に書き込むため、このまま起動すると
    ローテーション直後に作られた空の base に書き込みが始まってしまう。
    この関数を setup_logging() の先頭で呼ぶことで
    「最新ファイルに追記」の挙動を実現する。
    """
    candidates: dict[str, float] = {}
    for path in [base] + [f"{base}.{i}" for i in range(1, backup_count + 1)]:
        if os.path.exists(path):
            candidates[path] = os.path.getmtime(path)

    if not candidates:
        return

    newest = max(candidates, key=candidates.__getitem__)
    if newest == base:
        return

    sorted_files = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
    positions    = [base] + [f"{base}.{i}" for i in range(1, backup_count + 1)]

    tmp_map: dict[str, str] = {}
    for src, _ in sorted_files:
        tmp = src + ".__qtlbot_align_tmp__"
        os.rename(src, tmp)
        tmp_map[src] = tmp

    for (src, _), dst in zip(sorted_files, positions):
        os.rename(tmp_map[src], dst)

    print(f"[LogAlign] 最新ログファイル {newest} → {base} に再配置しました", flush=True)


def setup_logging() -> None:
    """ロギングハンドラーをセットアップする（ローテーション対応 + ログ肥大化対策）。"""
    global logger

    # 起動時: mtime が最新のログファイルを qtlbot.log に持ってきてから handler を生成
    _align_logfiles_on_startup("qtlbot.log", LOG_BACKUP_COUNT)

    file_level    = getattr(logging, LOG_LEVEL_FILE.upper(), logging.DEBUG)
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
    logger.propagate = False        # ルートロガーへの伝播を止める（重複出力防止の安全策）

    if LOG_SUPPRESS_HTTP_SUCCESS:
        access_logger = logging.getLogger("aiohttp.access")
        access_logger.addFilter(_SuppressHttpSuccessFilter())

    logger.info(
        f"ロギングをセットアップしました "
        f"(FILE={LOG_LEVEL_FILE}/CONSOLE={LOG_LEVEL_CONSOLE}, "
        f"maxSize={LOG_MAX_BYTES}bytes, dup抑制={LOG_DUPLICATE_THRESHOLD}s)"
    )
