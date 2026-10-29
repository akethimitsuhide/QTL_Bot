"""/qtl_logs（Discord スラッシュコマンド）: 権限・伏せ字・長文のファイル添付（2026-10-29）。"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

import cogs.system as system_mod  # noqa: E402
from core import log_view  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/SECRETwebhookTOKEN-abc_123"


def _interaction(admin: bool):
    user = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=admin), roles=[])
    return SimpleNamespace(
        user=user,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _call(interaction, **kw):
    cog = object.__new__(system_mod.SystemCog)
    args = dict(lines=30, level=None, keyword=None, generation=0)
    args.update(kw)
    asyncio.run(system_mod.SystemCog.slash_qtl_logs.callback(cog, interaction, **args))


def _log(tmp_path, monkeypatch, body):
    base = str(tmp_path / "qtlbot.log")
    monkeypatch.setattr(log_view, "LOG_FILE_PATH", base)
    with open(base, "w", encoding="utf-8") as f:
        f.write(body)


def test_non_admin_is_refused_by_default(tmp_path, monkeypatch):
    _log(tmp_path, monkeypatch, "[2026-10-29 00:00:01] [INFO] QTLBot: x\n")
    it = _interaction(admin=False)
    _call(it)
    it.response.send_message.assert_awaited_once()
    assert "権限" in it.response.send_message.await_args.args[0]
    it.followup.send.assert_not_awaited()


def test_admin_gets_masked_tail_as_ephemeral(tmp_path, monkeypatch):
    _log(tmp_path, monkeypatch,
         "[2026-10-29 00:00:01] [INFO] QTLBot: 起動\n"
         f"[2026-10-29 00:00:02] [WARNING] QTLBot: 送信失敗 {WEBHOOK}\n")
    it = _interaction(admin=True)
    _call(it, level="WARNING")
    sent = it.followup.send.await_args
    assert sent.kwargs["ephemeral"] is True
    text = sent.args[0]
    assert "SECRETwebhook" not in text and "送信失敗" in text and "起動" not in text
    assert "伏せ字" in text


def test_long_output_is_attached_as_a_masked_file(tmp_path, monkeypatch):
    body = "".join(f"[2026-10-29 00:00:{i % 60:02d}] [INFO] QTLBot: {'x' * 80} {i} token=abc{i}\n" for i in range(60))
    _log(tmp_path, monkeypatch, body)
    it = _interaction(admin=True)
    _call(it, lines=60)
    sent = it.followup.send.await_args
    data = sent.kwargs["file"].fp.read().decode("utf-8")
    assert "token=***" in data and "token=abc" not in data


def test_missing_generation_and_empty_result_are_reported(tmp_path, monkeypatch):
    _log(tmp_path, monkeypatch, "[2026-10-29 00:00:01] [INFO] QTLBot: x\n")
    it = _interaction(admin=True)
    _call(it, generation=5)
    assert "ありません" in it.followup.send.await_args.args[0]
    it = _interaction(admin=True)
    _call(it, keyword="存在しない文字列")
    assert "該当するログがありません" in it.followup.send.await_args.args[0]
