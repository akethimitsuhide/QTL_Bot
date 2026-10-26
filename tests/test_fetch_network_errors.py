"""JMA ポーリング（長周期・気象庁その他・津波観測）のネットワークエラー処理のテスト（2026-10-26追加）。

不具合: 回線の一時断で接続タイムアウトが起きると、汎用 except に落ちて ERROR（全文トレース）と
なり、管理者チャンネルにも誤エラー通知が出ていた。想定内のネットワークエラーは WARNING に留める。
"""
import asyncio
import logging
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import aiohttp  # noqa: E402
import pytest  # noqa: E402
from aiohttp.client_exceptions import ConnectionTimeoutError  # noqa: E402

from cogs.other import OtherInfoCog  # noqa: E402
from cogs.tsunami import TsunamiCog  # noqa: E402

LOOPS = [
    (OtherInfoCog, "fetch_long_period"),
    (OtherInfoCog, "fetch_quake_advisory"),
    (TsunamiCog, "fetch_tsunami_observation"),
]
ERRORS = [
    ConnectionTimeoutError("Connection timeout to host https://www.jma.go.jp/x.json"),
    asyncio.TimeoutError(),
    aiohttp.ClientConnectionError("Cannot connect"),
]


def _run(cls, name, exc, caplog):
    cog = cls.__new__(cls)
    cog.session = MagicMock()
    cog.session.get = MagicMock(side_effect=exc)
    with caplog.at_level(logging.DEBUG, logger="QTLBot"):
        asyncio.run(getattr(cls, name).coro(cog))


@pytest.mark.parametrize("cls,name", LOOPS)
@pytest.mark.parametrize("exc", ERRORS, ids=lambda e: type(e).__name__)
def test_network_error_is_warning_not_error(cls, name, exc, caplog):
    _run(cls, name, exc, caplog)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1 and "ネットワークエラー" in warns[0].getMessage()
    assert warns[0].exc_info is None            # トレースバックは出さない


@pytest.mark.parametrize("cls,name", LOOPS)
def test_unexpected_error_is_still_error(cls, name, caplog):
    _run(cls, name, ValueError("想定外"), caplog)
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]
