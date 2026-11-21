"""
tests/test_maintenance.py
=========================
core/maintenance.py（強震モニタ・長周期地震動モニタのメンテナンスモードの状態管理）のテスト（2026-11追加）。

- 期間の文字列の解釈（単位必須・上限・下限）
- 開始・解除・期限による自動解除
- 状態ファイルへの保存と、再起動後の引き継ぎ（期限を含む）
- 状態ファイルが壊れている・期限切れ・保存に失敗した場合の扱い
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from core.maintenance import (  # noqa: E402
    MAX_DURATION_SEC, MaintenanceState, format_remaining, parse_duration,
)

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 11, 21, 10, 0, tzinfo=JST).timestamp()


# ===============================
# 期間の解釈
# ===============================
@pytest.mark.parametrize("text, expected", [
    ("2h", 7200), ("90m", 5400), ("1d", 86400), ("1h30m", 5400), ("1m", 60),
    ("2時間30分", 9000), ("1日", 86400), ("1日2時間", 93600),
    (" 2 H ", 7200),            # 空白・大文字小文字は無視
    ("２ｈ", 7200),             # 全角の数字・英字
    ("1.5h", 5400),             # 小数
    ("30d", MAX_DURATION_SEC),  # 上限ちょうどは許可
])
def test_parse_duration_valid(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text, hint", [
    ("", "空"), ("   ", "空"),
    ("90", "単位"),             # 単位なしは、分か時間かが曖昧なので受け付けない
    ("abc", "解釈"), ("2x", "解釈"), ("h", "解釈"),
    ("30s", "解釈"),            # 秒は非対応
    ("-1h", "解釈"),
    ("0m", "1分以上"), ("0.5m", "1分以上"),
    ("31d", "30日以内"), ("999d", "30日以内"),
])
def test_parse_duration_invalid(text, hint):
    with pytest.raises(ValueError) as e:
        parse_duration(text)
    assert hint in str(e.value)


@pytest.mark.parametrize("seconds, expected", [
    (0, "0秒"), (30, "30秒"), (300, "5分"), (4800, "1時間20分"), (7200, "2時間0分"),
    (7199.9, "2時間0分"),       # 四捨五入（開始直後の「2h」が「1時間59分」にならない）
    (93600, "1日2時間"), (-5, "0秒"),
])
def test_format_remaining(seconds, expected):
    assert format_remaining(seconds) == expected


# ===============================
# 開始・解除・自動解除
# ===============================
@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "maint.json")


def test_default_state_is_inactive_and_creates_no_file(path):
    state = MaintenanceState.load(path)
    assert not state.is_active() and state.describe() == "通常稼働"
    assert not os.path.exists(path)


def test_start_with_duration_and_auto_expire(path):
    state = MaintenanceState.load(path)
    state.start(60, started_by="admin", now=NOW)
    assert state.is_active(now=NOW + 59)
    assert state.remaining(now=NOW + 30) == 30
    assert not state.is_active(now=NOW + 60)          # 期限に達したら自動で解除される
    assert not state.active and not os.path.exists(path)


def test_start_without_duration_lasts_until_manual_end(path):
    state = MaintenanceState.load(path)
    state.start(None, now=NOW)
    assert state.is_active(now=NOW + 10 * 86400)      # 何日経っても続く
    assert state.remaining(now=NOW + 10) is None
    assert "手動で解除するまで" in state.describe(now=NOW)


def test_end_returns_whether_it_was_active(path):
    state = MaintenanceState.load(path)
    assert state.end() is False                        # もともとメンテナンスではない
    state.start(None)
    assert os.path.exists(path)
    assert state.end() is True
    assert not state.is_active() and not os.path.exists(path)
    assert state.end() is False


def test_start_again_replaces_the_period(path):
    state = MaintenanceState.load(path)
    state.start(3600, now=NOW)
    state.start(None, now=NOW + 10)                    # 期間を省略 → 手動解除までに置き換わる
    assert state.until is None and state.is_active(now=NOW + 7200)


def test_describe_shows_jst_end_time_and_remaining(path):
    state = MaintenanceState.load(path)
    state.start(5400, now=NOW)                         # 10:00 に開始、90分
    assert state.describe(now=NOW) == "メンテナンス中（2026/11/21 11:30 まで・残り1時間30分）"


# ===============================
# 保存と再起動後の引き継ぎ
# ===============================
def test_state_survives_restart_including_deadline(path):
    state = MaintenanceState.load(path)
    state.start(3600, started_by="admin (1)", now=NOW)
    reloaded = MaintenanceState.load(path, now=NOW + 600)   # 10分後に再起動した想定
    assert reloaded.is_active(now=NOW + 600)
    assert reloaded.until == NOW + 3600                      # 期限は保持される
    assert reloaded.started_by == "admin (1)" and reloaded.started_at == NOW


def test_manual_end_survives_restart(path):
    state = MaintenanceState.load(path)
    state.start(None)
    state.end()
    assert not MaintenanceState.load(path).is_active()


def test_deadline_passed_while_bot_was_stopped(path):
    state = MaintenanceState.load(path)
    state.start(60, now=NOW)
    reloaded = MaintenanceState.load(path, now=NOW + 3600)   # 停止中に期限が過ぎた
    assert not reloaded.is_active(now=NOW + 3600)
    assert not os.path.exists(path)


def test_save_is_atomic_and_leaves_no_temp_files(path):
    state = MaintenanceState.load(path)
    state.start(3600)
    state.start(None)
    leftovers = [f for f in os.listdir(os.path.dirname(path)) if f != os.path.basename(path)]
    assert leftovers == []
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["active"] is True


# ===============================
# 異常系: 壊れたファイル・保存失敗
# ===============================
@pytest.mark.parametrize("content", [
    "{壊れたJSON",
    "[1, 2, 3]",                                   # 辞書でない
    json.dumps({"active": "yes"}),                 # active が True でない
    json.dumps({"active": True, "until": "あした"}),  # until が数値でない
    "",
])
def test_corrupt_state_file_is_treated_as_inactive(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    state = MaintenanceState.load(path)                # 例外を出さず、通常稼働として扱う
    assert not state.is_active()


def test_inactive_flag_in_file_is_inactive(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"active": False, "until": NOW + 99999}, f)
    assert not MaintenanceState.load(path, now=NOW).is_active(now=NOW)


def test_save_failure_keeps_in_memory_state_and_reports_it(tmp_path):
    bad_path = str(tmp_path / "no_such_dir" / "maint.json")   # 保存先のディレクトリが無い
    state = MaintenanceState.load(bad_path)
    state.start(3600, now=NOW)
    assert state.last_save_ok is False                 # 呼び出し側が利用者に伝えられる
    assert state.is_active(now=NOW)                    # メモリ上の状態は有効（再起動までは効く）
