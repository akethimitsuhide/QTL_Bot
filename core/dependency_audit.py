"""
core/dependency_audit.py
=========================
`python3 bot.py --audit_deps` の実体。requirements.lock（固定版）について、
  1. 実際にインストールされている版とのずれ（ドリフト）を表示し、
  2. pip-audit で既知の脆弱性を照合する。

【方針】
- Bot 本体（Discord 接続・.env）には一切依存しない。--check_env 等と同様、.env が無くても実行できる。
- pip-audit は任意の外部ツール（requirements には入れない）。未導入なら導入方法を案内して終了する。
- pip-audit は --no-deps --disable-pip で実行する。ロックファイルは推移的依存まで固定済みのため、
  依存解決のための一時 venv 作成・インストールを行わず、PyPI の脆弱性情報の照会だけで済む
  （Raspberry Pi でも軽い）。
- 終了コード: 0=問題なし / 1=脆弱性あり / 2=pip-audit を実行できなかった。
  版ずれは情報として表示するのみ（終了コードには影響しない）。cron などから定期実行できる。
- 環境マーカー（; python_version >= "3.13" など）の評価には packaging を使う。
  packaging が無い環境ではマーカー付きの行を版ずれ確認から除外する。
"""
import importlib.metadata as importlib_metadata
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

DEFAULT_LOCK_PATH = Path(__file__).resolve().parent.parent / "requirements.lock"
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;]+)\s*(?:;\s*(.+))?$")
_AUDIT_TIMEOUT_SEC = 300


def _norm(name: str) -> str:
    """PEP 503 の名前の正規化（大文字小文字・区切り文字の違いを吸収）。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> list[tuple[str, str, str | None]]:
    """
    ロックファイルを (名前, 版, マーカー or None) のリストにする。
    空行・コメント行は無視する。`name==version` 形式以外の行は ValueError（固定の保証が崩れるため）。
    """
    pins: list[tuple[str, str, str | None]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split(" #", 1)[0].strip() if " #" in raw else raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN_RE.match(line)
        if not m:
            raise ValueError(f"{lineno}行目: 'name==version' 形式ではありません: {raw!r}")
        pins.append((m.group(1), m.group(2), (m.group(3) or "").strip() or None))
    return pins


def _marker_applies(marker: str | None) -> bool | None:
    """マーカーが現在の環境に該当するか。評価できない（packaging が無い）場合は None。"""
    if not marker:
        return True
    try:
        from packaging.markers import Marker
    except ImportError:
        return None
    try:
        return bool(Marker(marker).evaluate())
    except Exception:
        return None


def find_drift(
    pins: list[tuple[str, str, str | None]],
    get_version: Callable[[str], str] = importlib_metadata.version,
) -> tuple[list[tuple[str, str, str | None]], int]:
    """
    ロックの版と、実際にインストールされている版のずれを返す。
    戻り値: ([(名前, ロックの版, インストール済みの版 or None), ...], 確認をスキップした行数)
    - None は「未インストール」。
    - マーカーが現在の環境に該当しない行、評価できない行はスキップする。
    """
    drift: list[tuple[str, str, str | None]] = []
    skipped = 0
    for name, locked, marker in pins:
        applies = _marker_applies(marker)
        if applies is None:
            skipped += 1
            continue
        if not applies:
            continue
        try:
            installed: str | None = get_version(name)
        except importlib_metadata.PackageNotFoundError:
            installed = None
        if installed != locked:
            drift.append((name, locked, installed))
    return drift, skipped


def _pip_audit_available() -> bool:
    return importlib.util.find_spec("pip_audit") is not None


def run_dependency_audit(lock_path: Path | str = DEFAULT_LOCK_PATH) -> int:
    """版ずれの表示と pip-audit による脆弱性照合を行い、終了コードを返す。"""
    lock_path = Path(lock_path)
    print(f"[Audit] ロックファイル: {lock_path}")
    try:
        pins = parse_lock(lock_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("[Audit] requirements.lock が見つかりません。")
        return 2
    except ValueError as e:
        print(f"[Audit] requirements.lock の形式が不正です: {e}")
        return 2
    print(f"[Audit] 固定されたパッケージ: {len(pins)}件")

    # ── 1. 版ずれ（情報表示のみ）
    drift, skipped = find_drift(pins)
    if drift:
        print(f"[Audit] ロックと実際の版が異なるパッケージ: {len(drift)}件")
        for name, locked, installed in drift:
            print(f"  - {name}: ロック={locked} / インストール済み={installed or '未インストール'}")
    else:
        print("[Audit] インストール済みの版はロックと一致しています。")
    if skipped:
        print(f"[Audit] （packaging が無いため、環境マーカー付きの{skipped}行は版ずれ確認から除外しました）")

    # ── 2. 脆弱性照合
    if not _pip_audit_available():
        print("[Audit] pip-audit が未導入のため、脆弱性の照合はできません。")
        print("[Audit]   導入: pip install pip-audit")
        return 2

    print("[Audit] pip-audit で既知の脆弱性を照合します（PyPI へ問い合わせるため、少し時間がかかります）...")
    cmd = [
        sys.executable, "-m", "pip_audit",
        "-r", str(lock_path), "--no-deps", "--disable-pip", "--progress-spinner", "off",
    ]
    try:
        result = subprocess.run(cmd, timeout=_AUDIT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        print(f"[Audit] pip-audit が{_AUDIT_TIMEOUT_SEC}秒でタイムアウトしました。")
        return 2
    except OSError as e:
        print(f"[Audit] pip-audit を実行できませんでした: {e}")
        return 2

    if result.returncode == 0:
        print("[Audit] 既知の脆弱性は見つかりませんでした。")
        return 0
    if result.returncode == 1:
        print("[Audit] 上記の脆弱性が見つかりました。修正版への更新を検討してください"
              "（更新手順は requirements.lock の冒頭を参照）。")
        return 1
    print(f"[Audit] pip-audit が異常終了しました（終了コード {result.returncode}）。")
    return 2
