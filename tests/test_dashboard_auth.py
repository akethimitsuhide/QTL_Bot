"""
Web ダッシュボードのトークン認証（core/dashboard_auth.py）のテスト（2026-10-12追加）。

確認すること:
1. 未認証: fetch/curl は 401 JSON、ブラウザ遷移（Accept: text/html）は /login へリダイレクト
2. /health・/login は認証不要、それ以外（/health/full 含む）は認証必須
3. Bearer ヘッダー認証（正しい値のみ通る）
4. ログイン → セッションクッキー（HttpOnly・SameSite=Lax・トークンを含まない）→ 以降の認証
5. ログイン後のリダイレクト先をサイト内に限定（オープンリダイレクト対策）
6. 失敗が続いたら同一IPをブロック（正しいトークンでも拒否）
7. ログアウトでクッキー削除、トークン変更で既存セッション無効
"""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.dashboard_auth import (
    COOKIE_NAME, DEFAULT_LANDING, DashboardAuth, LoginRateLimiter,
    extract_bearer, safe_next, session_cookie_value, verify_token,
)

TOKEN = "correct-horse-battery-staple"


def _make_app(token=TOKEN, limiter=None):
    auth = DashboardAuth(token, limiter=limiter)

    async def ok_json(request):
        return web.json_response({"ok": True})

    async def page(request):
        return web.Response(text="<html>secret</html>", content_type="text/html")

    app = web.Application(middlewares=[auth.middleware])
    auth.register_routes(app)
    app.router.add_get("/health", ok_json)
    app.router.add_get("/health/full", ok_json)
    app.router.add_get("/status", ok_json)
    app.router.add_get("/dashboard", page)
    return app


def _run(coro_fn, **app_kwargs):
    async def runner():
        async with TestClient(TestServer(_make_app(**app_kwargs))) as client:
            return await coro_fn(client)
    return asyncio.run(runner())


HTML = {"Accept": "text/html,application/xhtml+xml"}


# ---- 純粋関数 -----------------------------------------------------------------

def test_verify_token_and_cookie_value():
    assert verify_token(TOKEN, TOKEN)
    assert not verify_token(TOKEN + "x", TOKEN)
    assert not verify_token("", TOKEN)
    cv = session_cookie_value(TOKEN)
    assert TOKEN not in cv and len(cv) == 64
    assert cv != session_cookie_value(TOKEN + "x")


def test_extract_bearer():
    assert extract_bearer("Bearer abc") == "abc"
    assert extract_bearer("bearer  abc ") == "abc"
    assert extract_bearer("Basic abc") is None
    assert extract_bearer("Bearer") is None
    assert extract_bearer(None) is None


def test_safe_next_blocks_open_redirects():
    assert safe_next("/quake_map?limit=5") == "/quake_map?limit=5"
    for bad in (None, "", "https://evil.example", "//evil.example", "/\\evil.example",
                "evil", "/login", "/login?next=/x", "/logout", "/a\r\nSet-Cookie: x=1", "/" + "a" * 600):
        assert safe_next(bad) == DEFAULT_LANDING, bad


def test_rate_limiter_window():
    lim = LoginRateLimiter(max_failures=3, window_sec=100)
    for t in (0, 1, 2):
        lim.record_failure("1.1.1.1", now=t)
    assert lim.is_blocked("1.1.1.1", now=3)
    assert lim.retry_after("1.1.1.1", now=3) > 0
    assert not lim.is_blocked("2.2.2.2", now=3)          # 他IPには影響しない
    assert not lim.is_blocked("1.1.1.1", now=200)        # 期間経過で解除
    lim.record_failure("3.3.3.3", now=0)
    lim.reset("3.3.3.3")
    assert not lim.is_blocked("3.3.3.3", now=1)


# ---- 未認証 / 公開パス ---------------------------------------------------------

def test_unauthenticated_api_gets_401_json():
    async def go(c):
        r = await c.get("/status")
        assert r.status == 401
        assert (await r.json())["error"] == "unauthorized"
        assert "Bearer" in r.headers["WWW-Authenticate"]
    _run(go)


def test_unauthenticated_browser_redirects_to_login_with_next():
    async def go(c):
        r = await c.get("/dashboard?x=1", headers=HTML, allow_redirects=False)
        assert r.status == 302
        assert r.headers["Location"] == "/login?next=%2Fdashboard%3Fx%3D1"
    _run(go)


def test_health_and_login_are_public_but_health_full_is_not():
    async def go(c):
        assert (await c.get("/health")).status == 200
        assert (await c.get("/login")).status == 200
        assert (await c.get("/health/full")).status == 401
    _run(go)


# ---- Bearer ---------------------------------------------------------------------

def test_bearer_auth():
    async def go(c):
        ok = await c.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status == 200
        bad = await c.get("/status", headers={"Authorization": "Bearer wrong"})
        assert bad.status == 401
    _run(go)


# ---- ログイン / クッキー -------------------------------------------------------

def test_login_flow_sets_safe_cookie_and_authenticates():
    async def go(c):
        r = await c.post("/login", data={"token": TOKEN, "next": "/dashboard"}, allow_redirects=False)
        assert r.status == 303 and r.headers["Location"] == "/dashboard"
        set_cookie = r.headers["Set-Cookie"]
        assert COOKIE_NAME in set_cookie and TOKEN not in set_cookie
        assert "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie
        assert "Secure" not in set_cookie            # http 運用でもクッキーが送られる
        page = await c.get("/dashboard", headers=HTML)   # クッキーで認証される
        assert page.status == 200 and "secret" in await page.text()
        assert (await c.get("/status")).status == 200
    _run(go)


def test_login_wrong_token_no_cookie():
    async def go(c):
        r = await c.post("/login", data={"token": "nope"}, allow_redirects=False)
        assert r.status == 401 and "Set-Cookie" not in r.headers
        assert (await c.get("/status")).status == 401
    _run(go)


def test_login_next_is_restricted_to_same_site():
    async def go(c):
        r = await c.post("/login", data={"token": TOKEN, "next": "https://evil.example/"}, allow_redirects=False)
        assert r.status == 303 and r.headers["Location"] == DEFAULT_LANDING
    _run(go)


def test_login_page_escapes_next():
    async def go(c):
        r = await c.get('/login?next=/a"><script>alert(1)</script>')
        body = await r.text()
        assert "<script>alert(1)</script>" not in body
    _run(go)


def test_brute_force_is_blocked_even_with_correct_token():
    async def go(c):
        for _ in range(3):
            assert (await c.post("/login", data={"token": "bad"})).status == 401
        r = await c.post("/login", data={"token": TOKEN})
        assert r.status == 429 and int(r.headers["Retry-After"]) > 0
        assert (await c.get("/status")).status == 401    # ブロック中はクッキーも発行されない
    _run(go, limiter=LoginRateLimiter(max_failures=3, window_sec=300))


def test_logout_clears_cookie():
    async def go(c):
        await c.post("/login", data={"token": TOKEN})
        assert (await c.get("/status")).status == 200
        r = await c.get("/logout", allow_redirects=False)
        assert r.status == 303 and r.headers["Location"] == "/login"
        assert (await c.get("/status")).status == 401
    _run(go)


def test_cookie_from_old_token_is_rejected():
    async def go(c):
        c.session.cookie_jar.update_cookies({COOKIE_NAME: session_cookie_value("old-token")})
        assert (await c.get("/status")).status == 401
    _run(go)
