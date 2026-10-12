"""
依存パッケージの版ずれ確認・脆弱性照合（core/dependency_audit.py、requirements.lock）のテスト
（2026-10-12追加）。

確認すること:
1. ロックファイルのパース（コメント・空行・環境マーカー・不正行）
2. 版ずれの検出（一致・不一致・未インストール・マーカー非該当）
3. pip-audit の有無・終了コードに応じた戻り値（0/1/2）
4. リポジトリの requirements.lock 自体が形式として正しく、requirements.txt の依存を含むこと
"""
import re
import subprocess
from pathlib import Path

import pytest

from core import dependency_audit as da

ROOT = Path(__file__).resolve().parent.parent


def test_parse_lock_basic():
    text = """
# コメント
aiohttp==3.13.3
Pillow == 11.1.0
audioop-lts==0.2.2 ; python_version >= "3.13"

typing_extensions==4.15.0
"""
    assert da.parse_lock(text) == [
        ("aiohttp", "3.13.3", None),
        ("Pillow", "11.1.0", None),
        ("audioop-lts", "0.2.2", 'python_version >= "3.13"'),
        ("typing_extensions", "4.15.0", None),
    ]


@pytest.mark.parametrize("bad", ["aiohttp>=3.8", "aiohttp", "aiohttp==", "-r other.txt", "git+https://x/y"])
def test_parse_lock_rejects_unpinned_lines(bad):
    with pytest.raises(ValueError):
        da.parse_lock(bad)


def test_find_drift_cases():
    pins = [("a", "1.0", None), ("b", "2.0", None), ("c", "3.0", None)]
    versions = {"a": "1.0", "b": "2.1"}  # c は未インストール

    def get(name):
        if name in versions:
            return versions[name]
        raise da.importlib_metadata.PackageNotFoundError(name)

    drift, skipped = da.find_drift(pins, get_version=get)
    assert drift == [("b", "2.0", "2.1"), ("c", "3.0", None)]
    assert skipped == 0


def test_find_drift_skips_inapplicable_marker():
    pins = [("only-future", "1.0", 'python_version >= "99.0"'), ("x", "1.0", None)]
    drift, skipped = da.find_drift(pins, get_version=lambda n: "1.0")
    assert drift == [] and skipped == 0   # マーカー非該当は無視（未インストールでも差分にしない）


def test_find_drift_marker_applies():
    pins = [("m", "1.0", 'python_version >= "3.0"')]
    drift, _ = da.find_drift(pins, get_version=lambda n: "9.9")
    assert drift == [("m", "1.0", "9.9")]


def _write_lock(tmp_path, text="a==1.0\n"):
    p = tmp_path / "requirements.lock"
    p.write_text(text, encoding="utf-8")
    return p


def test_run_returns_2_when_lock_missing(tmp_path, capsys):
    assert da.run_dependency_audit(tmp_path / "nope.lock") == 2
    assert "見つかりません" in capsys.readouterr().out


def test_run_returns_2_when_lock_malformed(tmp_path, capsys):
    assert da.run_dependency_audit(_write_lock(tmp_path, "aiohttp>=3.8\n")) == 2
    assert "形式が不正" in capsys.readouterr().out


def test_run_returns_2_when_pip_audit_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(da, "_pip_audit_available", lambda: False)
    assert da.run_dependency_audit(_write_lock(tmp_path)) == 2
    assert "pip install pip-audit" in capsys.readouterr().out


@pytest.mark.parametrize("rc,expected", [(0, 0), (1, 1), (2, 2), (137, 2)])
def test_run_maps_pip_audit_exit_codes(tmp_path, monkeypatch, rc, expected):
    monkeypatch.setattr(da, "_pip_audit_available", lambda: True)
    captured = {}

    def fake_run(cmd, timeout):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(da.subprocess, "run", fake_run)
    assert da.run_dependency_audit(_write_lock(tmp_path)) == expected
    # 依存解決用の venv を作らない・pip を使わない構成で呼ぶこと
    assert "--no-deps" in captured["cmd"] and "--disable-pip" in captured["cmd"]


def test_run_handles_timeout_and_oserror(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "_pip_audit_available", lambda: True)

    def boom_timeout(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(da.subprocess, "run", boom_timeout)
    assert da.run_dependency_audit(_write_lock(tmp_path)) == 2

    def boom_os(cmd, timeout):
        raise OSError("no exec")

    monkeypatch.setattr(da.subprocess, "run", boom_os)
    assert da.run_dependency_audit(_write_lock(tmp_path)) == 2


# ---- リポジトリの requirements.lock 自体の検証 --------------------------------------

def test_repo_lock_is_well_formed_and_covers_requirements():
    pins = da.parse_lock((ROOT / "requirements.lock").read_text(encoding="utf-8"))
    names = {da._norm(n) for n, _, _ in pins}
    assert len(names) == len(pins), "同じパッケージが重複して固定されている"

    # requirements.txt の（開発用を除く）直接依存がすべてロックに含まれる
    dev_tools = {"pytest", "pytest-asyncio", "flake8", "black", "isort"}
    direct = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*>=", line)
        if m:
            direct.add(da._norm(m.group(1)))
    direct -= dev_tools
    assert direct, "requirements.txt から直接依存を読み取れていない"
    assert direct <= names, f"ロックに無い直接依存: {sorted(direct - names)}"
