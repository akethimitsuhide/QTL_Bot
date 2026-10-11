"""
core/error_notify_handler.py
=============================
ERROR 以上のログを管理者チャンネルへのエラー通知（SystemCog.notify_error）に
つなぐ logging.Handler。

【背景】
SystemCog.notify_error（重複防止付きの管理者チャンネル通知）と日次サマリーは
実装済みだったが、どこからも呼ばれておらず、README の「エラー自動通知」が
実際には動いていなかった。各所の logger.error(...) を個別に書き換えず、
ログ側から一括で接続する。

【設計上の注意】
- emit() は同期関数で、to_thread() のワーカースレッドからも呼ばれうる。
  そのため loop.call_soon_threadsafe() でイベントループ側へ渡す。
- 通知処理自身の失敗ログで通知が再帰しないよう、extra={"skip_admin_notify": True}
  を付けたレコードは無視する（SystemCog 側の該当箇所で付与）。
- 同じエラーの重複抑制（1時間）・件数集計は notify_error 側が担う。
  このモジュールは「ログ → (メッセージ, 種別)」の変換と受け渡しのみ。
- core.config には依存しない（単体テストを環境変数なしで実行できるようにする）。
"""
import asyncio
import logging
from typing import Awaitable, Callable

# notify_error(error_msg, error_type) の型
NotifyFunc = Callable[[str, str], Awaitable[None]]

# このキーが True のレコードは管理者通知の対象外にする
SKIP_ATTR = "skip_admin_notify"


def build_error_summary(record: logging.LogRecord) -> tuple[str, str]:
    """
    ログレコードから (通知メッセージ, エラー種別) を作る。

    - 種別: 「モジュール名.関数名」（日次サマリーの集計キーになる）
    - メッセージ: 例外があれば「例外型: 内容 | ログ1行目」。
      traceback.format_exc() をそのまま埋め込んだ文面でも、先頭行だけでは
      原因が分からないため、最終行（例外行）を前に出す。
    """
    msg = record.getMessage()
    lines = [ln for ln in msg.splitlines() if ln.strip()]
    head = lines[0].strip() if lines else ""

    exc_line = ""
    if record.exc_info and record.exc_info[0] is not None:
        exc_line = f"{record.exc_info[0].__name__}: {record.exc_info[1]}"
    elif lines and "Traceback (most recent call last)" in msg:
        exc_line = lines[-1].strip()

    text = f"{exc_line} | {head}" if exc_line and exc_line != head else head
    return text or "（メッセージなし）", f"{record.module}.{record.funcName}"


class AdminErrorLogHandler(logging.Handler):
    """ERROR 以上のログを notify_error へ非同期に渡すハンドラー。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, notify: NotifyFunc):
        super().__init__(level=logging.ERROR)
        self._loop = loop
        self._notify = notify
        # create_task の戻り値を保持して GC による途中破棄を防ぐ
        self._tasks: set[asyncio.Task] = set()

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR or getattr(record, SKIP_ATTR, False):
            return
        try:
            msg, err_type = build_error_summary(record)
            self._loop.call_soon_threadsafe(self._schedule, msg, err_type)
        except RuntimeError:
            # イベントループが閉じている（シャットダウン中）。通知は諦める
            pass
        except Exception:
            self.handleError(record)

    def _schedule(self, msg: str, err_type: str) -> None:
        task = self._loop.create_task(self._notify(msg, err_type))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)