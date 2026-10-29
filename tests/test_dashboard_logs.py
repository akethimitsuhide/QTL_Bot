"""
ダッシュボードのログ閲覧（/logs・/status/logs*・/logs/download）の結合テスト（2026-10-29）。

実際の SystemCog.start_web_dashboard を起動して確認する（tests/test_dashboard_security.py と同じ方式）。
- 一覧・末尾N件・レベル/キーワード絞り込み・秘密情報の伏せ字
- 名前の指定でログ以外のファイルを読ませられない（パストラバーサル）
- ダウンロードは圧縮済み（.gz）だけ
"""
import asyncio
import gzip
import os
import socket
import sys
import time

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

import cogs.system as system_mod  # noqa: E402
from core import log_view  # noqa: E402

# シークレット検知の誤検知を避けるため、ダミーの Webhook URL は実行時に組み立てる
WEBHOOK = "https://discord.com/api/webhooks/" + "123456789012345678" + "/" + "SECRETwebhookTOKEN-abc_123"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _serve(monkeypatch, token=""):
    port = _free_port()
    monkeypatch.setattr(system_mod, "WEB_DASHBOARD_PORT", port)
    monkeypatch.setattr(system_mod, "WEB_DASHBOARD_HOST", "127.0.0.1")
    monkeypatch.setattr(system_mod, "WEB_DASHBOARD_ALLOWED_IPS", [])
    monkeypatch.setattr(system_mod, "WEB_DASHBOARD_TOKEN", token)
    cog = object.__new__(system_mod.SystemCog)
    cog._web_app = None
    cog._web_runner = None
    await cog.start_web_dashboard()
    return cog, f"http://127.0.0.1:{port}"


def _make_logs(tmp_path, monkeypatch):
    base = str(tmp_path / "qtlbot.log")
    monkeypatch.setattr(log_view, "LOG_FILE_PATH", base)
    with open(base, "w", encoding="utf-8") as f:
        f.write("[2026-10-29 00:00:01] [INFO] QTLBot: 起動しました\n")
        f.write(f"[2026-10-29 00:00:02] [WARNING] QTLBot: 送信失敗 {WEBHOOK}\n")
        f.write("[2026-10-29 00:00:03] [ERROR] QTLBot: 描画エラー\nTraceback (most recent call last):\n  KeyError: 'x'\n")
        f.write("[2026-10-29 00:00:04] [INFO] QTLBot: token=abc123secret\n")
    arc = str(tmp_path / "qtlbot.log.20261028-235959.gz")
    with gzip.open(arc, "wt", encoding="utf-8") as f:
        f.write("[2026-10-28 23:59:59] [INFO] QTLBot: 前回のログ\n")
    old = time.time() - 3600
    os.utime(arc, (old, old))
    return base, arc


def test_list_view_filter_mask_and_download(monkeypatch, tmp_path):
    base, arc = _make_logs(tmp_path, monkeypatch)

    async def run():
        cog, url = await _serve(monkeypatch)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url + "/status/logs") as r:
                    data = await r.json()
                names = [f["name"] for f in data["files"]]
                assert names == ["qtlbot.log", "qtlbot.log.20261028-235959.gz"]     # 現在が先頭
                assert [f["downloadable"] for f in data["files"]] == [False, True]

                # 現在のログ: 伏せ字・トレースバックが1件にまとまる
                async with s.get(url + "/status/logs/view", params={"file": "qtlbot.log"}) as r:
                    v = await r.json()
                text = "\n".join(e["text"] for e in v["entries"])
                assert "SECRETwebhook" not in text and "abc123secret" not in text
                assert v["masked"] == 2 and len(v["entries"]) == 4
                assert any("KeyError" in e["text"] and e["level"] == "ERROR" for e in v["entries"])

                # レベル絞り込み
                async with s.get(url + "/status/logs/view", params={"file": "qtlbot.log", "level": "WARNING"}) as r:
                    v = await r.json()
                assert [e["level"] for e in v["entries"]] == ["WARNING", "ERROR"]

                # キーワード（伏せた部分では探れない）
                async with s.get(url + "/status/logs/view", params={"file": "qtlbot.log", "q": "SECRETwebhook"}) as r:
                    assert (await r.json())["entries"] == []
                async with s.get(url + "/status/logs/view", params={"file": "qtlbot.log", "q": "描画"}) as r:
                    assert len((await r.json())["entries"]) == 1

                # 末尾 N 件
                async with s.get(url + "/status/logs/view", params={"file": "qtlbot.log", "tail": "1"}) as r:
                    assert [e["time"] for e in (await r.json())["entries"]] == ["2026-10-29 00:00:04"]

                # 圧縮済みも読める
                async with s.get(url + "/status/logs/view", params={"file": os.path.basename(arc)}) as r:
                    assert "前回のログ" in (await r.json())["entries"][0]["text"]

                # ダウンロードは .gz のみ（中身は元のファイルそのもの）
                async with s.get(url + "/logs/download", params={"file": os.path.basename(arc)}) as r:
                    assert r.status == 200
                    assert "attachment" in r.headers["Content-Disposition"]
                    assert await r.read() == open(arc, "rb").read()
                async with s.get(url + "/logs/download", params={"file": "qtlbot.log"}) as r:
                    assert r.status == 404

                # 不正な指定
                async with s.get(url + "/status/logs/view", params={"file": "qtlbot.log", "tail": "abc"}) as r:
                    assert r.status == 400
                async with s.get(url + "/status/logs/view", params={"file": "qtlbot.log", "level": "LOUD"}) as r:
                    assert r.status == 400
        finally:
            await cog._web_runner.cleanup()

    asyncio.run(run())


def test_file_name_cannot_escape_the_log_directory(monkeypatch, tmp_path):
    _make_logs(tmp_path, monkeypatch)
    (tmp_path / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")

    async def run():
        cog, url = await _serve(monkeypatch)
        try:
            async with aiohttp.ClientSession() as s:
                for bad in ("../secret.txt", "secret.txt", str(tmp_path / "secret.txt"), "qtlbot.log/../secret.txt",
                            "/etc/passwd", "..%2fsecret.txt", "qtlbot.log\x00", ""):
                    for path in ("/status/logs/view", "/logs/download"):
                        async with s.get(url + path, params={"file": bad}) as r:
                            body = await r.text()
                            assert r.status == 404 and "TOP-SECRET" not in body, (path, bad, r.status)
        finally:
            await cog._web_runner.cleanup()

    asyncio.run(run())


def test_log_endpoints_and_page_require_the_dashboard_token(monkeypatch, tmp_path):
    _make_logs(tmp_path, monkeypatch)

    async def run():
        cog, url = await _serve(monkeypatch, token="A-LONG-RANDOM-DASHBOARD-TOKEN-0123456789")
        try:
            async with aiohttp.ClientSession() as s:
                for path in ("/logs", "/status/logs", "/status/logs/view?file=qtlbot.log",
                             "/logs/download?file=qtlbot.log.20261028-235959.gz", "/assets/js/logs.js"):
                    async with s.get(url + path, allow_redirects=False) as r:
                        assert r.status in (302, 303, 401), (path, r.status)
        finally:
            await cog._web_runner.cleanup()

    asyncio.run(run())


def test_logs_page_is_served(monkeypatch, tmp_path):
    _make_logs(tmp_path, monkeypatch)

    async def run():
        cog, url = await _serve(monkeypatch)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url + "/logs") as r:
                    assert r.status == 200 and "QTL_Bot ログ" in await r.text()
                    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
        finally:
            await cog._web_runner.cleanup()

    asyncio.run(run())
