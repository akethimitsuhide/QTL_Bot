"""core.config._env_bool の判定と、綴りミスの警告のテスト（2026-10-26追加）。

不具合: JISHIN_KANCHI_ENABLE=ture（綴りミス）が黙って false 扱いになり、地震感知情報が
通知されない原因に気づけなかった。判定結果は従来どおり false のまま、警告だけ出す。
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest  # noqa: E402

from core.config import _env_bool  # noqa: E402


@pytest.mark.parametrize("value", ["true", "TRUE", " 1 ", "yes"])
def test_true_values(monkeypatch, caplog, value):
    monkeypatch.setenv("QTL_T", value)
    with caplog.at_level(logging.WARNING, logger="QTLBot"):
        assert _env_bool("QTL_T", False) is True
    assert not caplog.records


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "  "])
def test_false_values_do_not_warn(monkeypatch, caplog, value):
    monkeypatch.setenv("QTL_T", value)
    with caplog.at_level(logging.WARNING, logger="QTLBot"):
        assert _env_bool("QTL_T", True) is False
    assert not caplog.records


def test_unset_uses_default(monkeypatch):
    monkeypatch.delenv("QTL_T", raising=False)
    assert _env_bool("QTL_T", True) is True
    assert _env_bool("QTL_T", False) is False


def test_typo_is_false_but_warns(monkeypatch, caplog):
    monkeypatch.setenv("QTL_T", "ture")
    with caplog.at_level(logging.WARNING, logger="QTLBot"):
        assert _env_bool("QTL_T", True) is False          # 判定は従来どおり
    assert any("QTL_T" in r.getMessage() and "ture" in r.getMessage() for r in caplog.records)
