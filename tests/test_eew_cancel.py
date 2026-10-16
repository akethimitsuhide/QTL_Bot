"""
tests/test_eew_cancel.py
========================
EEWキャンセル報の重複判定（core/eew_convert.should_process_eew）と、P2P地震情報の
キャンセル報の変換（convert_p2p_eew_to_wolfx）のテスト（2026-10-16追加）。

不具合: 2026-10-16 の過大予測の警報（EventID=20261016123810）で、Wolfx は最終報
（Serial=42）の後に同じ Serial=42 で isCancel=true を再送し、P2P地震情報は直前の報と
同じ issue.serial でキャンセル報（cancelled=true）を配信した。従来は「Serial が既知の最大より
大きいときだけ通知」だったため、どちらのキャンセル報も重複扱いで捨てられていた。
"""
import os
import sys
from collections import OrderedDict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core.eew_convert import convert_p2p_eew_to_wolfx, should_process_eew  # noqa: E402

EID = "20261016123810"


def test_cancel_with_same_serial_is_processed():
    seen, cancelled = {}, OrderedDict()
    assert should_process_eew(seen, cancelled, EID, 41, False) is True
    assert should_process_eew(seen, cancelled, EID, 42, False) is True      # 最終報
    assert should_process_eew(seen, cancelled, EID, 42, True) is True       # 同じSerialのキャンセル報


def test_cancel_is_processed_only_once():
    seen, cancelled = {EID: 42}, OrderedDict()
    assert should_process_eew(seen, cancelled, EID, 42, True) is True
    assert should_process_eew(seen, cancelled, EID, 42, True) is False      # 再送は抑制
    assert should_process_eew(seen, cancelled, EID, 43, True) is False


def test_normal_reports_still_deduplicated():
    seen, cancelled = {}, OrderedDict()
    assert should_process_eew(seen, cancelled, EID, 1, False) is True
    assert should_process_eew(seen, cancelled, EID, 1, False) is False      # 重複
    assert should_process_eew(seen, cancelled, EID, 3, False) is True
    assert should_process_eew(seen, cancelled, EID, 2, False) is False      # 逆行


def test_cancel_does_not_touch_other_events():
    seen, cancelled = {"A": 5, "B": 3}, OrderedDict()
    assert should_process_eew(seen, cancelled, "A", 5, True) is True
    assert should_process_eew(seen, cancelled, "B", 3, True) is True
    assert seen == {"A": 5, "B": 3}


def test_cancelled_store_is_bounded():
    seen, cancelled = {}, OrderedDict()
    for i in range(30):
        assert should_process_eew(seen, cancelled, f"E{i}", 1, True, cancelled_max=10) is True
    assert len(cancelled) == 10
    assert "E29" in cancelled and "E0" not in cancelled


def test_p2p_cancel_message_converts_to_cancel():
    # 2026-10-16 に実際に配信された code=556 のキャンセル報（history.json）
    raw = {
        "areas": None, "cancelled": True, "code": 556, "earthquake": None,
        "id": "6ad19ea2e88ee598246bf3f1",
        "issue": {"eventId": EID, "serial": "2", "time": "2026/10/16 12:48:50"},
    }
    data = convert_p2p_eew_to_wolfx(raw)
    assert data["isCancel"] is True
    assert data["EventID"] == EID
    assert data["Serial"] == 2
