"""
ERROR ログ → 管理者チャンネル通知（core/error_notify_handler.py）のテスト（2026-10-11追加）。

確認すること:
1. ERROR 以上のみ通知され、WARNING 以下は通知されない
2. skip_admin_notify 付きは通知されない（通知処理自身の失敗ログによる再帰防止）
3. 例外情報・traceback 文面から、原因（例外行）が通知メッセージに含まれる
4. ワーカースレッドからのログでもイベントループ側で通知される
5. 通知関数が例外を投げても呼び出し元（ログ出力）に影響しない
"""
import asyncio
import logging
import threading

from core.error_notify_handler import (
    SKIP_ATTR, AdminErrorLogHandler, build_error_summary,
)


def _make_logger(handler) -> logging.Logger:
    lg = logging.getLogger(f"test_err_{id(handler)}")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.handlers = [handler]
    return lg


async def _run(action):
    """handler を組み立て、action(logger) 実行後に通知結果のリストを返す。"""
    got: list[tuple[str, str]] = []

    async def notify(msg, etype):
        got.append((msg, etype))

    handler = AdminErrorLogHandler(asyncio.get_running_loop(), notify)
    lg = _make_logger(handler)
    await action(lg)
    await asyncio.sleep(0.05)  # call_soon_threadsafe / create_task の完了待ち
    return got


def test_only_error_and_above_notified():
    async def action(lg):
        lg.info("info")
        lg.warning("warn")
        lg.error("err")
        lg.critical("crit")

    got = asyncio.run(_run(action))
    assert [m for m, _ in got] == ["err", "crit"]


def test_skip_attr_is_ignored():
    async def action(lg):
        lg.error("再帰になるログ", extra={SKIP_ATTR: True})
        lg.error("通常のエラー")

    got = asyncio.run(_run(action))
    assert [m for m, _ in got] == ["通常のエラー"]


def test_exc_info_puts_exception_first():
    async def action(lg):
        try:
            raise ValueError("boom")
        except ValueError:
            lg.error("Fetch X エラー", exc_info=True)

    got = asyncio.run(_run(action))
    assert got[0][0] == "ValueError: boom | Fetch X エラー"


def test_traceback_text_uses_last_line():
    rec = logging.LogRecord(
        "QTLBot", logging.ERROR, __file__, 1,
        "Fetch Y エラー:\nTraceback (most recent call last):\n  File ...\nKeyError: 'a'",
        None, None, func="fetch_y",
    )
    msg, etype = build_error_summary(rec)
    assert msg == "KeyError: 'a' | Fetch Y エラー:"
    assert etype.endswith(".fetch_y")


def test_log_from_worker_thread():
    async def action(lg):
        t = threading.Thread(target=lambda: lg.error("thread error"))
        t.start()
        await asyncio.get_running_loop().run_in_executor(None, t.join)

    got = asyncio.run(_run(action))
    assert [m for m, _ in got] == ["thread error"]


def test_notify_failure_does_not_break_logging():
    async def action(lg):
        lg.error("first")
        lg.error("second")

    async def run():
        calls = []

        async def notify(msg, etype):
            calls.append(msg)
            raise RuntimeError("send failed")

        handler = AdminErrorLogHandler(asyncio.get_running_loop(), notify)
        lg = _make_logger(handler)
        await action(lg)  # 例外が伝播しないこと
        await asyncio.sleep(0.05)
        return calls

    assert asyncio.run(run()) == ["first", "second"]


def test_closed_loop_is_ignored():
    loop = asyncio.new_event_loop()
    loop.close()

    async def notify(msg, etype):  # 呼ばれない
        raise AssertionError

    lg = _make_logger(AdminErrorLogHandler(loop, notify))
    lg.error("after shutdown")  # 例外を出さないこと