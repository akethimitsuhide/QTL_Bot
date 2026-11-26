"""
tests/test_status_latency.py
=============================
discord.py の Bot.latency が inf / nan（ゲートウェイ切断中）でも、
/status・!status が落ちないこと（cogs.system._latency_ms）を確認する。
"""
import math

import pytest

from cogs.system import _latency_ms


def test_normal_latency_is_rounded_to_ms():
    assert _latency_ms(0.0456) == 46
    assert _latency_ms(0.0) == 0


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, None, "x"])
def test_disconnected_latency_returns_none(value):
    assert _latency_ms(value) is None


def test_old_expression_would_have_crashed():
    # 回帰確認: 従来の round(latency * 1000) は inf で OverflowError、nan で ValueError
    with pytest.raises(OverflowError):
        round(math.inf * 1000)
    with pytest.raises(ValueError):
        round(math.nan * 1000)
