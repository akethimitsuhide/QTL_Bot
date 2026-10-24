"""実際の SystemCog.start_web_dashboard を起動して、ダッシュボード全体のセキュリティを確認するテスト。

- どの経路でも .env・トークン・Webhook URL が返らない（秘密の値を環境変数に仕込んで確認）
- 全レスポンスにセキュリティヘッダー（CSP 等）が付く
- トークン認証が静的ファイル（/assets/*）にも効く
"""
import asyncio
import os
import socket

import aiohttp

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

import cogs.system as system_mod

SECRETS = {
    "BOT_TOKEN": "AAAA.BBBB-SECRET-DISCORD-BOT-TOKEN",
    "WEB_DASHBOARD_TOKEN": "SECRET-DASHBOARD-TOKEN-0123456789-ABCDEF-LONG",
    "WEBHOOK_URLS": "https://discord.com/api/webhooks/123/SECRET-WEBHOOK-TOKEN",
}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stub_cog():
    cog = object.__new__(system_mod.SystemCog)
    cog._web_app = None
    cog._web_runner = None
    return cog


async def _serve(monkeypatch, token=""):
    port = _free_port()
    monkeypatch.setattr(system_mod, "WEB_DASHBOARD_PORT", port)
    monkeypatch.setattr(system_mod, "WEB_DASHBOARD_HOST", "127.0.0.1")
    monkeypatch.setattr(system_mod, "WEB_DASHBOARD_ALLOWED_IPS", [])
    monkeypatch.setattr(system_mod, "WEB_DASHBOARD_TOKEN", token)
    cog = _stub_cog()
    await cog.start_web_dashboard()
    return cog, f"http://127.0.0.1:{port}"


async def _stop(cog):
    if cog._web_runner is not None:
        await cog._web_runner.cleanup()


_PATHS = [
    "/dashboard", "/quake_map", "/assets/js/common.js", "/assets/css/common.css", "/assets/vendor/leaflet.js",
    "/health", "/status", "/status/history", "/status/notifications", "/status/quake_history",
    "/status/eew_history", "/status/gis_countries", "/status/gis_local_areas",
    "/.env", "/.env.yomiage", "/assets/../.env", "/assets/%2e%2e/.env", "/bot.py", "/web/", "/nothing",
]


def test_no_route_returns_secrets_and_all_responses_carry_security_headers(monkeypatch):
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)

    async def run():
        cog, base = await _serve(monkeypatch)
        try:
            async with aiohttp.ClientSession() as session:
                for path in _PATHS:
                    async with session.get(base + path, allow_redirects=False) as r:
                        body = await r.text(errors="replace")
                        for name, secret in SECRETS.items():
                            assert secret not in body, f"{path} が {name} を返した"
                        for header in ("X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy",
                                       "Referrer-Policy"):
                            assert header in r.headers, f"{path}: {header} なし（status {r.status}）"
                        assert "aiohttp" not in r.headers.get("Server", "") and "Python" not in r.headers.get("Server", "")
                        if path.startswith("/.") or "%2e" in path or path in ("/bot.py", "/web/", "/nothing"):
                            assert r.status == 404, (path, r.status)
        finally:
            await _stop(cog)

    asyncio.run(run())


def test_pages_are_served_with_strict_csp(monkeypatch):
    async def run():
        cog, base = await _serve(monkeypatch)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(base + "/dashboard") as r:
                    assert r.status == 200 and "</html>" in await r.text()
                    csp = r.headers["Content-Security-Policy"]
                    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
                async with session.get(base + "/status") as r:
                    # JSON は画面用 CSP ではなく、何も読み込まない CSP
                    assert r.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
        finally:
            await _stop(cog)

    asyncio.run(run())


def test_token_auth_also_protects_static_files(monkeypatch):
    token = SECRETS["WEB_DASHBOARD_TOKEN"]

    async def run():
        cog, base = await _serve(monkeypatch, token=token)
        try:
            async with aiohttp.ClientSession() as session:
                for path in ("/dashboard", "/quake_map", "/assets/js/common.js", "/assets/vendor/chart.umd.min.js",
                             "/status", "/status/gis_countries"):
                    async with session.get(base + path, allow_redirects=False,
                                           headers={"Accept": "application/json"}) as r:
                        assert r.status == 401, (path, r.status)          # 認証なしでは何も返さない
                        assert token not in await r.text()
                        assert "Content-Security-Policy" in r.headers      # 拒否応答にもヘッダーが付く
                async with session.get(base + "/login", allow_redirects=False) as r:
                    assert r.status == 200
                    csp = r.headers["Content-Security-Policy"]
                    assert "script" not in csp and "form-action 'self'" in csp
        finally:
            await _stop(cog)

    asyncio.run(run())
