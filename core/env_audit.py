"""
core/env_audit.py
====================
.env.example 系ファイル（.env.example, .env.kyoshin.example、将来追加
されるカテゴリ別ファイル）とコード内の実際の参照箇所との整合性を
機械的にチェックするモジュール（2026-08-27 追加、.env整理案⑦）。

【背景】
これまでにも「.env.example に書かれているデフォルト値と、コード側の
実際のデフォルト値が食い違っている」（例: WEB_DASHBOARD_ENABLED の
true/false 不一致）、「コード側で仕様変更したのに .env.example の
コメントが古い挙動の説明のまま残っている」（例:
JISHIN_KANCHI_SPEECH_COUNT_STEP）といった問題が複数回見つかっている。
その都度手作業で grep して突き合わせるのではなく、
`python3 bot.py --check_env` でいつでも同じチェックを再現できるよう
本モジュールとして常設した。

【チェック内容】
1. .env.example 系ファイルで宣言されているが、コード中に文字列
   リテラルとして一切登場しない変数
   （＝ドキュメントに書かれているが実際には読まれていない可能性）
2. コード中で _env_int() / _env_bool() / _getenv_nonempty() /
   _require_env() / os.getenv() に渡されている変数名のうち、
   .env.example 系ファイルのどこにも宣言がない変数
   （＝コードでは使われているのにドキュメント化されていない可能性）

【制約】
このチェックは「変数名という文字列が両側に存在するか」だけを見る
単純な仕組みであり、デフォルト値の内容が一致しているかまでは検証
できない。あくまで「消し忘れ・書き忘れ」を早期発見するための一次
スクリーニングであり、これに通っても内容が正しい保証にはならない
点に注意すること（WEB_DASHBOARD_ENABLEDのtrue/false不一致のような
「両側に存在するが値が違う」ケースは検出できない）。
"""
import os
import re
import glob

# .env.example 本体に加えて、追加でチェック対象にするカテゴリ別
# テンプレートファイル。新しいカテゴリ別ファイル
# （.env.<category>.example）を追加したら、ここにも追記すること
# （core/env_loader.py の _CATEGORY_FILES とは別管理。あちらは
# 「実行時に読み込むファイル」、こちらは「ドキュメントとして
# チェックするファイル」なので、リポジトリのファイル名が対応関係に
# あるだけで、コード上で自動的には連動しない）。
_ENV_EXAMPLE_FILES = (".env.example", ".env.kyoshin.example")

# コード側の走査対象（bot.py と cogs/*.py, core/*.py の全 .py ファイル）
_CODE_GLOB_PATTERNS = ("bot.py", "cogs/*.py", "core/*.py")

# 環境変数を読み込むために実際に使われている関数名（core/config.py の
# _env_int 等のヘルパー、cogs/system.py 等が直接呼ぶ os.getenv、
# core/config.py の _require_env）。新しいヘルパー関数を増やしたら
# ここにも追記すること。
_GETENV_FUNC_NAMES = ("_env_int", "_env_bool", "_getenv_nonempty", "_require_env", "getenv")

# 意図的にドキュメント化していない、または意図的にコード側で
# 参照していない変数名（誤検知として除外したいものがあればここに追加）。
_IGNORE_KEYS: set = set()


def _collect_declared_keys(base_dir: str) -> dict:
    """.env.example 系ファイルで宣言されている変数名 → ファイル名 の dict。"""
    declared: dict = {}
    for filename in _ENV_EXAMPLE_FILES:
        path = os.path.join(base_dir, filename)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^([A-Z][A-Z0-9_]*)=", line.strip())
                if m:
                    declared[m.group(1)] = filename
    return declared


def _collect_code_text(base_dir: str) -> str:
    """走査対象の全 .py ファイルのソースコードを1つの文字列に連結して返す。"""
    chunks = []
    for pattern in _CODE_GLOB_PATTERNS:
        for path in glob.glob(os.path.join(base_dir, pattern)):
            try:
                with open(path, encoding="utf-8") as f:
                    chunks.append(f.read())
            except (OSError, UnicodeDecodeError):
                continue
    return "\n".join(chunks)


def audit_env_files(base_dir: str | None = None) -> dict:
    """
    整合性チェックを実行し、結果を dict で返す（テスト・他モジュールから
    プログラム的に使う場合はこちらを呼ぶ）。

    戻り値:
        {
            "declared_count": int,
            "unused_in_code": [(key, filename), ...],   # ドキュメントのみ
            "undocumented": [key, ...],                 # コードのみ
        }
    """
    if base_dir is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    declared = _collect_declared_keys(base_dir)
    code_text = _collect_code_text(base_dir)

    unused_in_code = []
    for key, filename in sorted(declared.items()):
        if key in _IGNORE_KEYS:
            continue
        if f'"{key}"' not in code_text and f"'{key}'" not in code_text:
            unused_in_code.append((key, filename))

    func_group = "|".join(re.escape(name) for name in _GETENV_FUNC_NAMES)
    code_key_pattern = re.compile(
        rf'(?:{func_group})\(\s*["\']([A-Z][A-Z0-9_]*)["\']'
    )
    referenced_keys = sorted(set(code_key_pattern.findall(code_text)))

    undocumented = [
        key for key in referenced_keys
        if key not in _IGNORE_KEYS and key not in declared
    ]

    return {
        "declared_count": len(declared),
        "unused_in_code": unused_in_code,
        "undocumented": undocumented,
    }


def format_audit_report(result: dict) -> str:
    """audit_env_files() の戻り値を、人間が読めるレポート文字列に整形する。"""
    lines = []
    lines.append("=" * 60)
    lines.append("QTL_Bot .env 整合性チェック（python3 bot.py --check_env）")
    lines.append("=" * 60)
    lines.append(f"チェック対象: {', '.join(_ENV_EXAMPLE_FILES)}")
    lines.append(f"宣言されている変数数: {result['declared_count']}")
    lines.append("")

    unused_in_code = result["unused_in_code"]
    if unused_in_code:
        lines.append(f"[!] ドキュメントに書かれているが、コード中に一切参照が見つからない変数 ({len(unused_in_code)}個):")
        for key, filename in unused_in_code:
            lines.append(f"    - {key}  ({filename})")
        lines.append("    → 廃止された設定の消し忘れの可能性があります。")
    else:
        lines.append("[OK] ドキュメントに書かれている変数は、すべてコード中で参照が見つかりました。")
    lines.append("")

    undocumented = result["undocumented"]
    if undocumented:
        lines.append(f"[!] コードで参照されているが、どの .env.example にも宣言がない変数 ({len(undocumented)}個):")
        for key in undocumented:
            lines.append(f"    - {key}")
        lines.append("    → .env.example への追記漏れの可能性があります。")
    else:
        lines.append("[OK] コードで参照されている変数は、すべて .env.example 系ファイルに宣言が見つかりました。")
    lines.append("")

    total = len(unused_in_code) + len(undocumented)
    if total == 0:
        lines.append("結果: 問題は見つかりませんでした。")
        lines.append("")
        lines.append("※ このチェックは変数名の存在有無のみを見る簡易チェックです。")
        lines.append("   デフォルト値の内容そのものが正しいかまでは検証していません。")
    else:
        lines.append(f"結果: {total}件の要確認項目があります。")

    return "\n".join(lines)


def run_env_check() -> None:
    """`python3 bot.py --check_env` のエントリーポイント。標準出力にレポートを表示する。"""
    result = audit_env_files()
    print(format_audit_report(result))
