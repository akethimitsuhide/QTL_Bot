"""
core/env_writer.py（.env の指定キーだけを安全に書き換える）のテスト（2026-10-12追加）。

確認すること:
1. 変更キー以外の行（コメント・空行・他キー・順序）が一切変わらない
2. インラインコメント・export・CRLF・末尾改行なしの扱い
3. 重複キーはすべて同じ値に、コメントアウトされた行は触らず末尾に追記
4. 危険な値（改行・空白・#・引用符）とキー名は拒否
5. ファイル更新: バックアップ・パーミッション維持・一時ファイルを残さない・失敗時に元ファイルを壊さない
6. 書いた結果を python-dotenv が正しく読める
"""
import os
import stat

import pytest
from dotenv import dotenv_values

from core import env_writer
from core.env_writer import update_env_file, update_env_text

BASE = """# ヘッダーコメント
BOT_TOKEN=secret-token-xyz
CHANNEL_ID=123

# 地震フィルター
QUAKE_MIN_SCALE=0   # 0=全て
TSUNAMI_ENABLE=true
"""


def test_only_target_line_changes():
    out = update_env_text(BASE, {"QUAKE_MIN_SCALE": "40"})
    assert out == BASE.replace("QUAKE_MIN_SCALE=0   # 0=全て", "QUAKE_MIN_SCALE=40   # 0=全て")


def test_missing_key_is_appended_and_missing_trailing_newline_handled():
    out = update_env_text("A=1", {"B": "2"})
    assert out == "A=1\nB=2\n"
    out = update_env_text(BASE, {"NEW_KEY": "x"})
    assert out == BASE + "NEW_KEY=x\n"


def test_commented_out_key_is_not_touched_but_appended():
    text = "# QUAKE_MIN_MAG=3.0\nA=1\n"
    out = update_env_text(text, {"QUAKE_MIN_MAG": "4.5"})
    assert out == text + "QUAKE_MIN_MAG=4.5\n"


def test_duplicate_keys_all_updated_and_export_prefix_kept():
    text = "X=1\nexport X=2\nY=9\n"
    out = update_env_text(text, {"X": "7"})
    assert out == "X=7\nexport X=7\nY=9\n"


def test_prefix_key_is_not_confused():
    text = "QUAKE_MIN_SCALE=0\nQUAKE_MIN_SCALE_EXTRA=5\n"
    out = update_env_text(text, {"QUAKE_MIN_SCALE": "10"})
    assert out == "QUAKE_MIN_SCALE=10\nQUAKE_MIN_SCALE_EXTRA=5\n"


def test_crlf_is_preserved():
    text = "A=1\r\nB=2\r\n"
    assert update_env_text(text, {"A": "5", "C": "3"}) == "A=5\r\nB=2\r\nC=3\r\n"


@pytest.mark.parametrize("bad", ["a b", "x\ny", "x#y", "'q'", '"q"', "a=b", "x;y", "$(rm)"])
def test_unsafe_values_rejected(bad):
    with pytest.raises(ValueError):
        update_env_text("A=1\n", {"A": bad})


@pytest.mark.parametrize("bad_key", ["", "1A", "A B", "A=B", "A\nB"])
def test_bad_keys_rejected(bad_key):
    with pytest.raises(ValueError):
        update_env_text("A=1\n", {bad_key: "1"})


def test_result_is_readable_by_dotenv(tmp_path):
    p = tmp_path / ".env"
    p.write_text(BASE, encoding="utf-8")
    update_env_file(str(p), {"QUAKE_MIN_SCALE": "40", "TSUNAMI_ENABLE": "false"})
    vals = dotenv_values(p)
    assert vals["QUAKE_MIN_SCALE"] == "40" and vals["TSUNAMI_ENABLE"] == "false"
    assert vals["BOT_TOKEN"] == "secret-token-xyz" and vals["CHANNEL_ID"] == "123"


def test_file_update_backup_mode_and_no_leftovers(tmp_path):
    p = tmp_path / ".env"
    p.write_text(BASE, encoding="utf-8")
    os.chmod(p, 0o600)
    update_env_file(str(p), {"QUAKE_MIN_SCALE": "40"})
    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == BASE        # 変更前の内容
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600                           # トークンを含むため維持
    assert sorted(x.name for x in tmp_path.iterdir()) == [".env", ".env.bak"]  # 一時ファイルが残らない


def test_new_file_gets_private_mode(tmp_path):
    p = tmp_path / ".env"
    update_env_file(str(p), {"A": "1"})
    assert p.read_text(encoding="utf-8") == "A=1\n"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert not (tmp_path / ".env.bak").exists()


def test_failure_leaves_original_intact(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text(BASE, encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(env_writer.os, "replace", boom)
    with pytest.raises(OSError):
        update_env_file(str(p), {"QUAKE_MIN_SCALE": "40"})
    assert p.read_text(encoding="utf-8") == BASE
    assert sorted(x.name for x in tmp_path.iterdir()) == [".env", ".env.bak"]   # 一時ファイルは掃除される
