"""KYOSHIN_LOG_LEVEL（強震モニタ・揺れ検知ログの抑制）のテスト。"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core.logging_setup import _KyoshinLogFilter, _parse_level  # noqa: E402


def _record(filename: str, level: int) -> logging.LogRecord:
    r = logging.LogRecord("QTLBot", level, f"/app/core/{filename}", 1, "msg", None, None)
    r.filename = filename
    return r


def test_kyoshin_info_is_suppressed_at_warning():
    f = _KyoshinLogFilter(logging.WARNING)
    assert f.filter(_record("kyoshin_detector.py", logging.INFO)) is False   # イベントのマージ等
    assert f.filter(_record("kyoshin_monitor.py", logging.INFO)) is False


def test_kyoshin_warning_and_error_remain():
    f = _KyoshinLogFilter(logging.WARNING)
    assert f.filter(_record("kyoshin_detector.py", logging.WARNING)) is True
    assert f.filter(_record("kyoshin_monitor.py", logging.ERROR)) is True


def test_other_modules_are_unaffected():
    f = _KyoshinLogFilter(logging.ERROR)
    assert f.filter(_record("eew.py", logging.INFO)) is True
    assert f.filter(_record("quake.py", logging.DEBUG)) is True


def test_parse_level():
    assert _parse_level("warning") == logging.WARNING
    assert _parse_level(" ERROR ") == logging.ERROR
    assert _parse_level("bogus") == logging.INFO      # 未知の値は従来どおり INFO
