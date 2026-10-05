"""
core/env_merge.py
==================
`python3 bot.py --merge_env` 用の、.env への不足項目の対話式追加ツール（2026-10-05 追加）。

【背景】
機能追加のたびに .env.example へ設定項目が増えるため、すでに .env を運用している人が
新しい項目を .env へ反映しきれなくなりやすい。`cp .env.example .env` をやり直すと
既存の設定（トークン等）が失われ、手作業で差分を探すのも大変なので、
「.env.example にあって .env に無い項目だけを、1つずつ確認しながら追加する」ツールとした
（cp と git merge の中間のような動き）。

【動作】
- .env.example → .env、.env.kyoshin.example → .env.kyoshin の順に処理する
  （.env.kyoshin が無い場合は、通常運用では不要なファイルのためスキップする）。
- .env.example で `KEY=値` の形で宣言されている項目のうち、対象ファイルに
  `KEY=` も `# KEY=`（コメントアウト）も無いものを「不足」とみなす。
  コメントアウトされた行は不足に含めない（.env.example 側のコメントアウト項目は
  追加の候補にならず、.env 側でコメントアウトしている項目は「意図的に無効化している」
  とみなして触らない）。
- 不足項目ごとに、.env.example の直前の説明コメントと既定値を表示して選ばせる:
    Enter / y : 既定値のまま追加
    e         : 値を入力して追加（トークン等の秘密情報らしい項目は入力を表示しない）
    n         : スキップ（今回は追加しない）
    a         : 残りすべてを既定値で追加
    q         : ここで終了（ここまでの選択は反映する）
- 追加は対象ファイルの末尾に、説明コメントごと追記する。既存の行は一切変更・並べ替え
  しない。書き込み前に `<ファイル名>.bak.<タイムスタンプ>` へバックアップする。
- 対象ファイルにあって .env.example に無い項目（廃止された項目の可能性）は、項目名のみ
  一覧表示する（変更はしない）。
- 既存の値は画面に表示しない。Ctrl+C で中断した場合、ファイルは一切変更しない。
- `--yes` を付けると、確認せずにすべて既定値で追加する（自動化用）。

【設計方針】
discord.py や core.config に依存せず、標準ライブラリのみを使う（.env が不完全な状態でも
実行できるよう、bot.py 側で core.config を import する前に呼ぶ）。

【使い方】
    python3 bot.py --merge_env          # 対話式
    python3 bot.py --merge_env --yes    # すべて既定値で追加
"""
import os
import re
import shutil
import getpass
import sys
from dataclasses import dataclass, field
from datetime import datetime

# (参照元の .env.example 系ファイル, 追加先)
_FILE_PAIRS = (
    (".env.example", ".env"),
    (".env.kyoshin.example", ".env.kyoshin"),
)

_DECLARED_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_COMMENTED_RE = re.compile(r"^\s*#+\s*([A-Z][A-Z0-9_]*)=")
_SECRET_HINTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "PRIVATE_KEY")
_MAX_COMMENT_LINES = 12


@dataclass
class ExampleEntry:
    key: str
    default: str                                  # "=" の右側（そのまま）
    comments: list = field(default_factory=list)  # 直前の連続したコメント行（"#" 付きのまま）


def parse_example(text: str) -> list[ExampleEntry]:
    """
    .env.example 系のテキストから、`KEY=値` で宣言されている項目（コメントアウトされていない
    もの）を出現順に返す。各項目の直前にある空行なしで連続したコメント行を説明として付ける
    （区切り線だけの行は除く）。
    """
    entries: list[ExampleEntry] = []
    block: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _DECLARED_RE.match(line.strip())
        if m:
            key = m.group(1)
            if key not in seen:
                seen.add(key)
                entries.append(ExampleEntry(key, m.group(2), block[-_MAX_COMMENT_LINES:]))
            block = []
        elif line.strip().startswith("#"):
            if not re.fullmatch(r"#\s*[-=─━]{3,}\s*", line.strip()):   # 区切り線は説明に含めない
                block.append(line)
        else:
            block = []   # 空行などでコメントのまとまりを区切る
    return entries


def existing_keys(text: str) -> tuple[set, set]:
    """対象ファイルの (`KEY=` で宣言済みのキー, `# KEY=` でコメントアウト済みのキー)。"""
    declared: set = set()
    commented: set = set()
    for line in text.splitlines():
        m = _DECLARED_RE.match(line.strip())
        if m:
            declared.add(m.group(1))
            continue
        c = _COMMENTED_RE.match(line)
        if c:
            commented.add(c.group(1))
    return declared, commented


def find_missing(example_text: str, target_text: str) -> list[ExampleEntry]:
    """.env.example にあって対象ファイルに無い（宣言もコメントアウトも無い）項目。"""
    declared, commented = existing_keys(target_text)
    return [e for e in parse_example(example_text) if e.key not in declared and e.key not in commented]


def find_unknown(example_text: str, target_text: str) -> list[str]:
    """対象ファイルにあるが .env.example に無い項目名（廃止された可能性がある項目）。"""
    known = {e.key for e in parse_example(example_text)}
    known |= set(existing_keys(example_text)[1])          # example 側でコメントアウトされている項目も既知扱い
    declared, _ = existing_keys(target_text)
    return sorted(declared - known)


def append_entries(target_text: str, chosen: list[tuple[ExampleEntry, str]], stamp: str) -> str:
    """選ばれた項目（説明コメント付き）を対象テキストの末尾へ追記した全文を返す。既存の行は変更しない。"""
    if not chosen:
        return target_text
    out = target_text
    if out and not out.endswith("\n"):
        out += "\n"
    out += f"\n# --- merge_env による追加 ({stamp}) ---\n"
    for entry, value in chosen:
        out += "\n"
        for c in entry.comments:
            out += c + "\n"
        out += f"{entry.key}={value}\n"
    return out


def _is_secret(key: str) -> bool:
    return any(h in key for h in _SECRET_HINTS)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _choose_interactive(entries: list[ExampleEntry], input_fn=input, secret_fn=getpass.getpass,
                        out=print) -> list[tuple[ExampleEntry, str]]:
    """不足項目を1つずつ確認し、追加するもの [(項目, 値)] を返す。"""
    chosen: list[tuple[ExampleEntry, str]] = []
    add_all = False
    total = len(entries)
    for i, entry in enumerate(entries, start=1):
        if add_all:
            chosen.append((entry, entry.default))
            continue
        out(f"\n[{i}/{total}] {entry.key}")
        for c in entry.comments:
            out(f"  {c}")
        out(f"  既定値: {entry.default if entry.default else '（空）'}")
        while True:
            ans = input_fn("  追加しますか？ [Enter/y]既定値で追加 / [e]値を入力 / [n]スキップ / "
                           "[a]残りすべて既定値で追加 / [q]ここで終了: ").strip().lower()
            if ans in ("", "y", "yes"):
                chosen.append((entry, entry.default))
            elif ans == "e":
                if _is_secret(entry.key):
                    value = secret_fn(f"  {entry.key} の値（入力は表示されません）: ").strip()
                else:
                    value = input_fn(f"  {entry.key} の値: ").strip()
                chosen.append((entry, value))
            elif ans in ("n", "no"):
                pass
            elif ans == "a":
                chosen.append((entry, entry.default))
                add_all = True
            elif ans == "q":
                return chosen
            else:
                out("  ※ Enter / y / e / n / a / q のいずれかを入力してください。")
                continue
            break
    return chosen


def run_env_merge(base_dir: str | None = None, assume_yes: bool | None = None,
                  input_fn=input, secret_fn=getpass.getpass, out=print) -> int:
    """
    不足項目の追加を実行する。戻り値は追加した項目の総数。
    assume_yes が None のときは、コマンドラインに --yes があるかで決める。
    """
    if base_dir is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if assume_yes is None:
        assume_yes = "--yes" in sys.argv[1:]

    out("=" * 60)
    out("QTL_Bot .env 不足項目の追加（python3 bot.py --merge_env）")
    out("=" * 60)

    plans = []   # (example名, target名, target本文, [(項目, 値)])
    try:
        for example_name, target_name in _FILE_PAIRS:
            example_path = os.path.join(base_dir, example_name)
            target_path = os.path.join(base_dir, target_name)
            if not os.path.isfile(example_path):
                out(f"\n[{target_name}] {example_name} が見つからないためスキップします。")
                continue
            if not os.path.isfile(target_path):
                if target_name == ".env":
                    out(f"\n[{target_name}] がありません。先に `python3 bot.py --starter` か "
                        f"`cp {example_name} {target_name}` で作成してください。")
                else:
                    out(f"\n[{target_name}] は作成されていません（通常運用では不要なためスキップします）。")
                continue

            example_text, target_text = _read(example_path), _read(target_path)
            out(f"\n■ {target_name}（{example_name} と比較）")
            unknown = find_unknown(example_text, target_text)
            if unknown:
                out(f"  参考: {example_name} に無い項目（廃止された可能性があります。変更はしません）: "
                    + ", ".join(unknown))
            missing = find_missing(example_text, target_text)
            if not missing:
                out("  不足している項目はありません。")
                continue
            out(f"  不足している項目: {len(missing)} 件")
            chosen = ([(e, e.default) for e in missing] if assume_yes
                      else _choose_interactive(missing, input_fn, secret_fn, out))
            plans.append((target_name, target_path, target_text, chosen))
    except (KeyboardInterrupt, EOFError):
        out("\n中断しました。ファイルは一切変更していません。")
        return 0

    total = 0
    stamp = datetime.now().strftime("%Y-%m-%d")
    backup_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for target_name, target_path, target_text, chosen in plans:
        if not chosen:
            continue
        shutil.copy2(target_path, f"{target_path}.bak.{backup_stamp}")
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(append_entries(target_text, chosen, stamp))
        total += len(chosen)
        out(f"\n[{target_name}] に {len(chosen)} 件を追加しました"
            f"（バックアップ: {target_name}.bak.{backup_stamp}）。")
        out("  追加した項目: " + ", ".join(e.key for e, _ in chosen))
    if total == 0:
        out("\n変更はありません。")
    else:
        out("\n完了しました。Bot を再起動すると反映されます。値の確認は `python3 bot.py --check_env` も使えます。")
    return total
