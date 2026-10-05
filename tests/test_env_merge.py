"""
tests/test_env_merge.py
========================
.env への不足項目の対話式追加（core/env_merge.py、2026-10-05追加）のテスト。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import env_merge as em  # noqa: E402

EXAMPLE = """\
# ============================
# 基本
# ============================

# ----------------------------
# Bot のトークン
BOT_TOKEN=

# 通知間隔（秒）
# デフォルト: 1.0
NOTIFY_SEC=1.0
# COMMENTED_IN_EXAMPLE=1

# 新機能
NEW_FEATURE=true
NEW_SECRET_TOKEN=
"""
TARGET = "BOT_TOKEN=abc123\n# NOTIFY_SEC=5\nOLD_KEY=1"


def test_parse_example_ignores_commented_and_keeps_comments():
    es = em.parse_example(EXAMPLE)
    assert [e.key for e in es] == ["BOT_TOKEN", "NOTIFY_SEC", "NEW_FEATURE", "NEW_SECRET_TOKEN"]
    assert es[1].comments == ["# 通知間隔（秒）", "# デフォルト: 1.0"] and es[1].default == "1.0"
    assert es[0].comments == ["# Bot のトークン"]            # 見出しは空行で切れ、区切り線は説明に含めない


def test_missing_excludes_commented_out_keys_and_unknown_is_reported():
    assert [e.key for e in em.find_missing(EXAMPLE, TARGET)] == ["NEW_FEATURE", "NEW_SECRET_TOKEN"]
    assert em.find_unknown(EXAMPLE, TARGET) == ["OLD_KEY"]


def test_append_keeps_existing_lines_untouched():
    missing = em.find_missing(EXAMPLE, TARGET)
    new = em.append_entries(TARGET, [(missing[0], "false")], "2026-10-05")
    assert new.startswith(TARGET + "\n")
    assert "# 新機能\nNEW_FEATURE=false\n" in new and "NEW_SECRET_TOKEN" not in new
    assert em.append_entries(TARGET, [], "x") == TARGET


def _setup(tmp_path):
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    (tmp_path / ".env").write_text(TARGET, encoding="utf-8")


def _answers(*a):
    it = iter(a)
    return lambda prompt="": next(it)


def test_interactive_flow_with_backup(tmp_path):
    _setup(tmp_path)
    n = em.run_env_merge(str(tmp_path), assume_yes=False, input_fn=_answers("e", "false", "n"),
                         secret_fn=lambda p: "s", out=lambda *_: None)
    assert n == 1
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "NEW_FEATURE=false" in text and "NEW_SECRET_TOKEN" not in text and "BOT_TOKEN=abc123" in text
    assert any(f.startswith(".env.bak.") for f in os.listdir(tmp_path))      # バックアップ
    assert em.find_missing(EXAMPLE, text)[0].key == "NEW_SECRET_TOKEN"        # スキップした項目はまだ不足


def test_secret_value_uses_hidden_input_and_all_defaults(tmp_path):
    _setup(tmp_path)
    em.run_env_merge(str(tmp_path), assume_yes=False, input_fn=_answers("y", "e"),
                     secret_fn=lambda p: "hidden", out=lambda *_: None)
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "NEW_FEATURE=true" in text and "NEW_SECRET_TOKEN=hidden" in text


def test_add_all_quit_and_yes_flag(tmp_path):
    _setup(tmp_path)
    assert em.run_env_merge(str(tmp_path), False, _answers("a"), out=lambda *_: None) == 2
    (tmp_path / ".env").write_text(TARGET, encoding="utf-8")
    assert em.run_env_merge(str(tmp_path), False, _answers("q"), out=lambda *_: None) == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") == TARGET          # 何も追加しなければ変更なし
    assert em.run_env_merge(str(tmp_path), True, out=lambda *_: None) == 2
    assert em.run_env_merge(str(tmp_path), True, out=lambda *_: None) == 0    # 2回目は不足なし


def test_ctrl_c_changes_nothing_and_missing_env_is_skipped(tmp_path):
    _setup(tmp_path)

    def boom(prompt=""):
        raise KeyboardInterrupt
    assert em.run_env_merge(str(tmp_path), False, boom, out=lambda *_: None) == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") == TARGET
    (tmp_path / ".env").unlink()
    assert em.run_env_merge(str(tmp_path), True, out=lambda *_: None) == 0    # .env が無ければ何もしない
