"""
core/dashboard_auth.py
=======================
Web ダッシュボードの任意のトークン認証（WEB_DASHBOARD_TOKEN）。

【方針】
- 未設定（空）なら認証は行わない（従来どおり。IP 許可リスト・bind アドレスのみで制御）。
- 認証方法は2通り:
    1. Authorization: Bearer <token> ヘッダー（curl・スクリプト用）
    2. ログインページ（/login）で入力 → セッションクッキー（ブラウザ用）
- クッキーにトークンそのものは入れない。トークンから HMAC で導出した値を使う
  （ステートレス。トークンを変更すると既存のセッションは無効になる）。
- 未認証時: ブラウザの画面遷移（Accept: text/html）は /login へリダイレクト、
  それ以外（fetch / curl）は 401 JSON。
- /health（疎通確認のみ・機密なし）、/login、/logout は認証不要。
  /health/full 以下の詳細情報はすべて認証対象。
- ログインの総当たり対策として、同一IPからの失敗が続いたら一定時間ブロックする。
- クッキーは HttpOnly + SameSite=Lax。HTTPS（request.secure）のときだけ Secure を付ける
  （LAN 内の http 運用でもクッキーが送られるように）。Lax なのは、Discord 等に貼った
  ダッシュボードのリンクを開いたときもログイン状態を保つため。
- aiohttp 以外に依存しない（core.config を読まない）。単体テストを環境変数なしで実行できる。
"""
import hashlib
import hmac
import html
import logging
import time
from collections import deque
from urllib.parse import quote

from aiohttp import web

logger = logging.getLogger("QTLBot")

COOKIE_NAME = "qtl_dash_session"
SESSION_MAX_AGE_SEC = 7 * 24 * 3600     # ログイン状態の保持期間（7日）
PUBLIC_PATHS = frozenset({"/health", "/login", "/logout"})
DEFAULT_LANDING = "/dashboard"
MIN_RECOMMENDED_TOKEN_LENGTH = 16

LOGIN_MAX_FAILURES = 5                   # この回数失敗したら
LOGIN_WINDOW_SEC = 300                   # 5分間は同一IPをブロック
_LIMITER_MAX_TRACKED_IPS = 1000


def session_cookie_value(token: str) -> str:
    """トークンから導出したセッションクッキー値（トークン自体は含めない）。"""
    return hmac.new(token.encode("utf-8"), b"qtl-dashboard-session-v1", hashlib.sha256).hexdigest()


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def verify_token(candidate: str, token: str) -> bool:
    """定数時間比較（長さの違いも漏らさないよう、ハッシュ同士で比較する）。"""
    return hmac.compare_digest(_digest(candidate), _digest(token))


def extract_bearer(header_value: str | None) -> str | None:
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def safe_next(raw: str | None) -> str:
    """
    ログイン後のリダイレクト先を、サイト内パスに限定する（オープンリダイレクト対策）。
    '/' で始まり、'//'・'/\\'（プロトコル相対）・制御文字・/login 自身は拒否して既定に戻す。
    """
    if not raw or len(raw) > 512:
        return DEFAULT_LANDING
    if not raw.startswith("/") or raw.startswith("//") or raw.startswith("/\\"):
        return DEFAULT_LANDING
    if any(ord(c) < 0x20 or c == "\x7f" for c in raw):
        return DEFAULT_LANDING
    if raw == "/login" or raw.startswith("/login?") or raw.startswith("/logout"):
        return DEFAULT_LANDING
    return raw


class LoginRateLimiter:
    """同一IPのログイン失敗回数を数え、上限に達したら一定時間ブロックする（メモリ内）。"""

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES, window_sec: int = LOGIN_WINDOW_SEC):
        self._max = max_failures
        self._window = window_sec
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, ip: str, now: float) -> deque:
        q = self._failures.get(ip)
        if q is None:
            return deque()
        while q and now - q[0] >= self._window:
            q.popleft()
        if not q:
            self._failures.pop(ip, None)
            return deque()
        return q

    def is_blocked(self, ip: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return len(self._prune(ip, now)) >= self._max

    def retry_after(self, ip: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        q = self._prune(ip, now)
        if len(q) < self._max:
            return 0
        return max(1, int(self._window - (now - q[0])) + 1)

    def record_failure(self, ip: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if len(self._failures) >= _LIMITER_MAX_TRACKED_IPS and ip not in self._failures:
            # 追跡数の上限（メモリ保護）。期限切れを掃除し、それでも多ければ古いものから捨てる
            for key in list(self._failures):
                self._prune(key, now)
            while len(self._failures) >= _LIMITER_MAX_TRACKED_IPS:
                self._failures.pop(next(iter(self._failures)))
        q = self._failures.setdefault(ip, deque())
        q.append(now)

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)


def login_page_html(next_path: str, error: str | None = None) -> str:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>QTL_Bot ログイン</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; display: flex; min-height: 100vh;
         align-items: center; justify-content: center; margin: 0; }}
  form {{ width: min(92vw, 340px); padding: 24px; border: 1px solid #8884; border-radius: 10px; }}
  h1 {{ font-size: 1.1rem; margin: 0 0 16px; }}
  input {{ width: 100%; box-sizing: border-box; padding: 10px; font-size: 1rem; margin-bottom: 12px; }}
  button {{ width: 100%; padding: 10px; font-size: 1rem; cursor: pointer; }}
  .err {{ color: #d33; margin: 0 0 12px; font-size: .9rem; }}
</style></head><body>
<form method="post" action="/login">
  <h1>QTL_Bot ダッシュボード</h1>
  {err}
  <input type="password" name="token" placeholder="アクセストークン"
         autocomplete="current-password" autofocus required>
  <input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
  <button type="submit">ログイン</button>
</form></body></html>"""


def _client_ip(request: web.Request) -> str:
    peername = request.transport.get_extra_info("peername") if request.transport else None
    return (peername[0] if peername else request.remote) or "unknown"


class DashboardAuth:
    """トークン認証の middleware と /login・/logout ハンドラーをまとめたもの。"""

    def __init__(self, token: str, limiter: LoginRateLimiter | None = None):
        if not token:
            raise ValueError("token must not be empty")
        self._token = token
        self._cookie_value = session_cookie_value(token)
        self._limiter = limiter or LoginRateLimiter()
        self.middleware = self._build_middleware()

    def _is_authenticated(self, request: web.Request) -> bool:
        bearer = extract_bearer(request.headers.get("Authorization"))
        if bearer is not None and verify_token(bearer, self._token):
            return True
        cookie = request.cookies.get(COOKIE_NAME)
        return bool(cookie) and hmac.compare_digest(cookie, self._cookie_value)

    def _build_middleware(self):
        @web.middleware
        async def auth_middleware(request: web.Request, handler):
            if request.path in PUBLIC_PATHS or self._is_authenticated(request):
                return await handler(request)

            if request.method in ("GET", "HEAD") and "text/html" in request.headers.get("Accept", ""):
                target = f"/login?next={quote(request.path_qs, safe='')}"
                return web.Response(status=302, headers={"Location": target, "Cache-Control": "no-store"})

            return web.json_response(
                {"error": "unauthorized"}, status=401,
                headers={"WWW-Authenticate": 'Bearer realm="qtl-dashboard"'},
            )
        return auth_middleware

    async def handle_login_get(self, request: web.Request) -> web.Response:
        return web.Response(
            text=login_page_html(safe_next(request.query.get("next"))),
            content_type="text/html", headers={"Cache-Control": "no-store"},
        )

    async def handle_login_post(self, request: web.Request) -> web.Response:
        ip = _client_ip(request)
        form = await request.post()
        next_path = safe_next(form.get("next") if isinstance(form.get("next"), str) else None)

        if self._limiter.is_blocked(ip):
            wait = self._limiter.retry_after(ip)
            logger.warning(f"Web Dashboard: ログイン試行が多すぎるためブロック中です: {ip}")
            return web.Response(
                text=login_page_html(next_path, f"試行回数が多すぎます。{wait}秒後にやり直してください。"),
                status=429, content_type="text/html",
                headers={"Retry-After": str(wait), "Cache-Control": "no-store"},
            )

        candidate = form.get("token")
        if not isinstance(candidate, str) or not verify_token(candidate, self._token):
            self._limiter.record_failure(ip)
            logger.warning(f"Web Dashboard: ログインに失敗しました: {ip}")
            return web.Response(
                text=login_page_html(next_path, "トークンが正しくありません。"),
                status=401, content_type="text/html", headers={"Cache-Control": "no-store"},
            )

        self._limiter.reset(ip)
        logger.info(f"Web Dashboard: ログインしました: {ip}")
        resp = web.Response(status=303, headers={"Location": next_path, "Cache-Control": "no-store"})
        resp.set_cookie(
            COOKIE_NAME, self._cookie_value, max_age=SESSION_MAX_AGE_SEC,
            httponly=True, samesite="Lax", secure=request.secure, path="/",
        )
        return resp

    async def handle_logout(self, request: web.Request) -> web.Response:
        resp = web.Response(status=303, headers={"Location": "/login", "Cache-Control": "no-store"})
        resp.del_cookie(COOKIE_NAME, path="/")
        return resp

    def register_routes(self, app: web.Application) -> None:
        app.router.add_get("/login", self.handle_login_get)
        app.router.add_post("/login", self.handle_login_post)
        app.router.add_get("/logout", self.handle_logout)
