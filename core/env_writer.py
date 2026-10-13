"""
core/env_writer.py
===================
.env の指定キーだけを安全に書き換える（ランタイム設定変更の永続化用）。

【方針】
- 変更するキー以外の行（コメント・空行・他のキー・順序）は一切変更しない。
- 既存行は値だけを置き換え、行末のインラインコメント（`KEY=1  # 説明`）は残す。
  同じキーが複数あれば、すべて同じ値にそろえる（python-dotenv は後の行が勝つため）。
- キーが無ければ末尾に `KEY=value` を追記する。
- 書き込みは「同じディレクトリの一時ファイル → os.replace」でアトミックに行い、
  途中で失敗しても元の .env を壊さない。元ファイルのパーミッション（トークンを含むため
  通常 600）は引き継ぐ。
- 書き込み前に `.env.bak`（1世代・上書き）へバックアップする。`.env.bak*` は .gitignore 済み。
- 値は英数字と `. _ -` のみ許可する（改行・空白・引用符・# の混入による .env 破損や
  別キーの注入を防ぐ）。呼び出し側が渡すのは数値・true/false・テーマ名など単純な値のみ。
"""
import os
import re
import shutil
import stat
import tempfile

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._-]*$")
_KEY_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def update_env_text(text: str, updates: dict[str, str]) -> str:
    """text 内の updates のキーの値を置き換えた新しいテキストを返す（純粋関数）。"""
    for key, value in updates.items():
        if not _KEY_OK.match(key):
            raise ValueError(f"不正なキー名です: {key!r}")
        if not _SAFE_VALUE.match(value):
            raise ValueError(f"{key} の値に使えない文字が含まれています: {value!r}")

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    done: set[str] = set()

    for i, raw in enumerate(lines):
        body = raw.rstrip("\r\n")
        eol = raw[len(body):]
        if body.lstrip().startswith("#"):
            continue
        for key, value in updates.items():
            m = re.match(rf"^(\s*(?:export\s+)?{re.escape(key)}\s*=)(.*)$", body)
            if not m:
                continue
            old_value = m.group(2)
            cm = re.match(r"^.*?(\s+#.*)$", old_value)
            comment = cm.group(1) if cm else ""           # 「  # 説明」を空白ごと保持
            lines[i] = f"{m.group(1)}{value}{comment}{eol}"
            done.add(key)
            break

    missing = [k for k in updates if k not in done]
    if missing:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        for key in missing:
            lines.append(f"{key}={updates[key]}{newline}")
    return "".join(lines)


def update_env_file(path: str, updates: dict[str, str], backup: bool = True) -> None:
    """
    path の .env を updates の内容で更新する。失敗した場合は例外を送出し、元のファイルは変更されない。
    """
    exists = os.path.exists(path)
    text = ""
    mode = 0o600
    if exists:
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        mode = stat.S_IMODE(os.stat(path).st_mode)
    new_text = update_env_text(text, updates)

    if exists and backup:
        shutil.copy2(path, path + ".bak")

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix=".env.tmp.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
