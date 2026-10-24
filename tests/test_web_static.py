"""core/web_static.py（ダッシュボードの静的ファイル配信・セキュリティヘッダー）のテスト。

重点: .env（トークン）など許可していないファイルを、どんなパスで要求されても返さないこと。
"""
import asyncio
import os
import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

from core import web_static
from core.web_static import ASSETS, PAGES, StaticFiles, register_routes, security_headers_middleware

SECRET = "SUPER-SECRET-BOT-TOKEN-0123456789"
_REAL_WEB = web_static.WEB_DIR


def _run(coro):
    return asyncio.run(coro)


async def _client(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _app(files):
    app = web.Application(middlewares=[security_headers_middleware])
    register_routes(app, files)
    return app


def _make_site(tmp_path):
    """tmp/.env（秘密）・tmp/web/（許可リストのファイル＋許可リスト外の秘密ファイル）を作る。"""
    (tmp_path / ".env").write_text(f"BOT_TOKEN={SECRET}\n", encoding="utf-8")
    web_dir = tmp_path / "web"
    for name in list(PAGES.values()) + list(ASSETS):
        target = web_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"/* {name} */ " + ("x" * 2000), encoding="utf-8")
    (web_dir / "notes-secret.txt").write_text(SECRET, encoding="utf-8")        # 許可リストに無いファイル
    (web_dir / "vendor" / "leaflet.LICENSE").write_text(SECRET, encoding="utf-8")
    return str(web_dir)


# ---------- 許可リストと実ファイルの整合 ----------
def test_every_allowlisted_file_exists_in_repo():
    files = StaticFiles(_REAL_WEB)
    assert files.missing() == []


def test_nothing_unlisted_under_web_is_served():
    """web/ にあるファイルのうち、配信されるのは許可リストのものだけ（LICENSE 等は配信しない）。"""
    listed = set(PAGES.values()) | set(ASSETS)
    present = set()
    for root, _, names in os.walk(_REAL_WEB):
        for n in names:
            present.add(os.path.relpath(os.path.join(root, n), _REAL_WEB).replace(os.sep, "/"))
    unlisted = present - listed
    assert unlisted <= {"vendor/chart.js.LICENSE.md", "vendor/leaflet.LICENSE", "vendor/VENDORED.md"}, unlisted


# ---------- 正常系 ----------
def test_pages_and_assets_served_with_headers(tmp_path):
    files = StaticFiles(_make_site(tmp_path))

    async def run():
        c = await _client(_app(files))
        try:
            r = await c.get("/dashboard")
            assert r.status == 200 and r.headers["Content-Type"].startswith("text/html")
            csp = r.headers["Content-Security-Policy"]
            assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp and "frame-ancestors 'none'" in csp
            assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "DENY"
            assert r.headers["Referrer-Policy"] == "no-referrer" and r.headers["Server"] == "QTL_Bot"
            js = await c.get("/assets/js/common.js")
            assert js.status == 200 and js.headers["Content-Type"].startswith("text/javascript")
            css = await c.get("/assets/vendor/leaflet.css")
            assert css.headers["Content-Type"].startswith("text/css")
            head = await c.head("/quake_map")
            assert head.status == 200
        finally:
            await c.close()

    _run(run())


def test_gzip_and_etag(tmp_path):
    files = StaticFiles(_make_site(tmp_path))

    async def run():
        c = await _client(_app(files))
        try:
            r = await c.get("/assets/js/common.js", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
            assert r.headers["Content-Encoding"] == "gzip" and r.headers["Vary"] == "Accept-Encoding"
            etag = r.headers["ETag"]
            again = await c.get("/assets/js/common.js", headers={"If-None-Match": etag})
            assert again.status == 304
            # 304 のヘッダーはブラウザの保存済みヘッダーを上書きするため、CSP・Content-Type も同じ値でなければならない
            assert again.headers["Content-Security-Policy"] == web_static._PAGE_CSP
            assert again.headers["Content-Type"].startswith("text/javascript")
            page = await c.get("/dashboard")
            page304 = await c.get("/dashboard", headers={"If-None-Match": page.headers["ETag"]})
            assert page304.status == 304
            assert page304.headers["Content-Security-Policy"] == page.headers["Content-Security-Policy"]
            assert page304.headers["Content-Type"] == page.headers["Content-Type"]
            plain = await c.get("/assets/js/common.js", headers={"Accept-Encoding": "identity"})
            assert "Content-Encoding" not in plain.headers and "common.js" in await plain.text()
        finally:
            await c.close()

    _run(run())


# ---------- 攻撃パターン: .env を読み出せないこと ----------
_ATTACK_PATHS = [
    "/.env", "/web/../.env", "/assets/../.env", "/assets/../../.env", "/assets/%2e%2e/.env",
    "/assets/%2e%2e/%2e%2e/.env", "/assets/..%2f..%2f.env", "/assets/%252e%252e/.env",
    "/assets/....//....//.env", "/assets/..\\..\\.env", "/assets/js/../../../.env",
    "/assets/%00.env", "/assets/js/common.js%00", "/assets/js/common.js/../../../.env",
    "/assets/notes-secret.txt", "/assets/vendor/leaflet.LICENSE", "/assets/", "/assets", "/assets/js/",
    "/assets/js", "/assets/vendor/", "/assets/JS/COMMON.JS",
    "/assets//etc/passwd", "/assets/file:///etc/passwd", "/dashboard/../.env", "/dashboard.html",
    "/web/dashboard.html", "/quake_map/", "/.git/config", "/bot.py", "/requirements.txt",
]


@pytest.mark.parametrize("path", _ATTACK_PATHS)
def test_attack_paths_never_return_secrets(tmp_path, path):
    files = StaticFiles(_make_site(tmp_path))

    async def run():
        c = await _client(_app(files))
        try:
            r = await c.get(path)
            body = await r.text()
            assert SECRET not in body, path
            assert "BOT_TOKEN" not in body, path
            # 許可リスト外・不正なパスは 4xx（ディレクトリ一覧や 200 にはならない）
            assert r.status in (400, 404, 405), (path, r.status)
            assert r.headers["X-Content-Type-Options"] == "nosniff"        # 404 にもヘッダーが付く
            assert "Content-Security-Policy" in r.headers
        finally:
            await c.close()

    _run(run())


def test_only_get_and_head_allowed(tmp_path):
    files = StaticFiles(_make_site(tmp_path))

    async def run():
        c = await _client(_app(files))
        try:
            for method in ("post", "put", "delete", "patch"):
                r = await getattr(c, method)("/dashboard", data=b"x")
                assert r.status == 405, method
                r = await getattr(c, method)("/assets/js/common.js", data=b"x")
                assert r.status == 405, method
        finally:
            await c.close()

    _run(run())


def test_symlink_to_secret_is_not_followed(tmp_path):
    web_dir = _make_site(tmp_path)
    link = os.path.join(web_dir, "js", "common.js")
    os.remove(link)
    os.symlink(str(tmp_path / ".env"), link)               # 許可リストのファイル名を .env へのリンクにされた場合
    files = StaticFiles(web_dir)

    async def run():
        c = await _client(_app(files))
        try:
            r = await c.get("/assets/js/common.js")
            assert r.status == 503 and SECRET not in await r.text()
        finally:
            await c.close()

    _run(run())


def test_missing_and_oversized_files_give_503_not_crash(tmp_path, monkeypatch):
    web_dir = _make_site(tmp_path)
    os.remove(os.path.join(web_dir, "dashboard.html"))
    monkeypatch.setattr(web_static, "_MAX_FILE_BYTES", 1000)
    files = StaticFiles(web_dir)

    async def run():
        c = await _client(_app(files))
        try:
            assert (await c.get("/dashboard")).status == 503
            assert (await c.get("/assets/js/common.js")).status == 503      # 2000バイト > 上限
        finally:
            await c.close()

    _run(run())
    assert "dashboard.html" in files.missing()


# ---------- ヘッダー ----------
def test_security_headers_on_error_and_data_and_login_responses():
    app = web.Application(middlewares=[security_headers_middleware])

    async def forbidden(request):
        return web.json_response({"error": "forbidden"}, status=403)

    async def login(request):
        return web.Response(text="<form></form>", content_type="text/html")

    async def data(request):
        return web.json_response({"ok": 1}, headers={"Cache-Control": "private, max-age=60"})

    async def boom(request):
        raise web.HTTPUnauthorized()

    app.router.add_get("/forbidden", forbidden)
    app.router.add_get("/login", login)
    app.router.add_get("/data", data)
    app.router.add_get("/boom", boom)

    async def run():
        c = await _client(app)
        try:
            r = await c.get("/forbidden")
            assert r.status == 403 and r.headers["Content-Security-Policy"] == web_static._DATA_CSP
            r = await c.get("/login")
            assert r.headers["Content-Security-Policy"] == web_static._LOGIN_CSP
            assert "script" not in r.headers["Content-Security-Policy"]
            r = await c.get("/data")
            assert r.headers["Cache-Control"] == "private, max-age=60"          # 個別に設定したものは上書きしない
            r = await c.get("/boom")
            assert r.status == 401 and r.headers["X-Frame-Options"] == "DENY"
            r = await c.get("/nonexistent")
            assert r.status == 404 and r.headers["Server"] == "QTL_Bot"
        finally:
            await c.close()

    _run(run())


# ---------- CSP に適合したフロントエンドであること（静的な点検） ----------
def _read(rel):
    with open(os.path.join(_REAL_WEB, rel), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("page", sorted(PAGES.values()))
def test_html_has_no_inline_script_style_or_external_resources(page):
    html = _read(page)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "インラインスクリプト"
    assert not re.search(r"<style", html, re.I), "<style> 要素"
    assert not re.search(r"\sstyle\s*=", html, re.I), "style 属性"
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "on* イベント属性"
    assert not re.search(r"(?:src|href)\s*=\s*[\"']\s*(?:https?:)?//", html, re.I), "外部リソース"
    assert not re.search(r"javascript:", html, re.I)


_OWN_JS = [k for k in ASSETS if k.startswith("js/")]


@pytest.mark.parametrize("rel", _OWN_JS)
def test_own_js_avoids_dangerous_apis(rel):
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", _read(rel), flags=re.S)         # コメントは除く
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                   "setAttribute('style'", 'setAttribute("style"', "javascript:"):
        assert banned not in code, f"{rel}: {banned}"
    assert not re.search(r"""['"]https?://""", code), f"{rel}: 外部URLへの参照"


@pytest.mark.parametrize("rel", [k for k in ASSETS if k.startswith("css/")])
def test_own_css_has_no_external_urls_or_imports(rel):
    css = _read(rel)
    assert "@import" not in css and not re.search(r"url\(\s*['\"]?\s*(?:https?:)?//", css)


def test_every_script_and_link_target_in_html_is_in_the_allowlist():
    for page in PAGES.values():
        html = _read(page)
        for url in re.findall(r"(?:src|href)=\"(/assets/[^\"]+)\"", html):
            assert url[len("/assets/"):] in ASSETS, (page, url)


def test_vendored_versions_are_documented():
    text = _read("vendor/VENDORED.md")
    assert "chart.js" in text.lower() and "leaflet" in text.lower() and "sha256" in text.lower()


def test_unhandled_exception_becomes_generic_500_with_security_headers():
    app = web.Application(middlewares=[security_headers_middleware])

    async def broken(request):
        raise RuntimeError("internal detail: /home/user/.env SECRET")

    app.router.add_get("/broken", broken)

    async def run():
        c = await _client(app)
        try:
            r = await c.get("/broken")
            body = await r.text()
            assert r.status == 500 and body == "Internal Server Error"      # 内部情報（パス・例外文）を出さない
            assert r.headers["Server"] == "QTL_Bot" and r.headers["X-Content-Type-Options"] == "nosniff"
            assert "Content-Security-Policy" in r.headers
        finally:
            await c.close()

    _run(run())
