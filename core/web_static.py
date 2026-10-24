"""
core/web_static.py - Web ダッシュボードの静的ファイル（web/ フォルダ）の配信とセキュリティヘッダー

【なぜこの作りか（セキュリティ設計）】
  ダッシュボードの HTML・CSS・JS を web/ フォルダの別ファイルにしたが、「フォルダの中身をそのまま
  公開する」方式（aiohttp の add_static や、リクエストのパスからファイルを探す実装）は使わない。
  そうした方式は、パスの組み立てを 1 か所誤るだけで、.env（トークン）などフォルダ外のファイルや
  設定ファイルを読み出される脆弱性（パストラバーサル・シンボリックリンク経由の漏えい・
  ディレクトリ一覧の露出）になりうるため。代わりに次の方針にしている。

  1. 許可リスト方式: 配信できるのは、このファイルの PAGES / ASSETS に「書いてあるものだけ」。
     リクエストのパスは辞書の「キー」と文字列として一致するかを調べるだけで、ファイルパスの
     組み立てには一切使わない（`..`・`%2e%2e`・二重エンコード・NUL 文字・バックスラッシュ等を
     送られても、辞書に無いので 404 になるだけ）。ディレクトリ一覧もない。
  2. 読み出しは「許可リストの値（定数）」から作ったパスだけ。さらに、実体が web/ の直下にある
     通常ファイルで、シンボリックリンクを経由していないことを確認する（読めたとしても大きさの
     上限を設ける）。
  3. 実行できるものは何も配信しない: サーバー側で解釈・実行される形式（PHP 等のスクリプト）は
     存在せず、Python の Bot が読み出して返すのはバイト列だけ。アップロード機能もない。
  4. 全レスポンスにセキュリティヘッダーを付ける（security_headers_middleware）:
       - Content-Security-Policy: script-src 'self'（インラインスクリプト・外部スクリプト禁止）、
         外部への接続禁止（connect-src 'self'）、フレーム埋め込み禁止 など。万一、表示するデータに
         悪意あるスクリプトが混ざっても実行されない。
       - X-Content-Type-Options: nosniff（ブラウザによる型の推測実行を禁止）
       - X-Frame-Options: DENY、Referrer-Policy: no-referrer、Cross-Origin-* など。
       - Server ヘッダーの既定値（aiohttp・Python のバージョン）は隠す。
  5. 外部の CDN には依存しない（Chart.js・Leaflet は web/vendor/ に固定の版を同梱。
     web/vendor/VENDORED.md 参照）。CDN の改ざん・停止・追跡の影響を受けず、LAN 内でも動く。
  6. 認証（WEB_DASHBOARD_TOKEN）・IP 許可リストは、既存のミドルウェアが静的ファイルにも
     同じように適用される（このモジュールは認証を迂回する経路を持たない）。

  ※ このダッシュボードが返す内容に .env・トークン・Webhook URL は含まれない（API は稼働状況・
     履歴・地図データのみ。tests/test_web_static.py で、実際にサーバーを起動して確認している）。
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Optional

from aiohttp import web

logger = logging.getLogger("QTLBot")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(_PROJECT_ROOT, "web")

_MAX_FILE_BYTES = 5 * 1024 * 1024      # 想定外に大きなファイルは配信しない

# ===============================
# 許可リスト（ここに無いものは、web/ に置いてあっても配信されない）
# ===============================
# 画面（URL パス → web/ 内のファイル名）
PAGES: dict[str, str] = {
    "/dashboard": "dashboard.html",
    "/quake_map": "quake_map.html",
}
_HTML = "text/html; charset=utf-8"
_CSS = "text/css; charset=utf-8"
_JS = "text/javascript; charset=utf-8"

# /assets/<キー> で配信するファイル（キー → Content-Type）。キーが web/ 内の相対パスでもある。
ASSETS: dict[str, str] = {
    "css/common.css": _CSS,
    "css/dashboard.css": _CSS,
    "css/quake_map.css": _CSS,
    "js/theme.js": _JS,
    "js/common.js": _JS,
    "js/dashboard.js": _JS,
    "js/quake_map.js": _JS,
    "vendor/chart.umd.min.js": _JS,
    "vendor/leaflet.js": _JS,
    "vendor/leaflet.css": _CSS,
}

# ===============================
# セキュリティヘッダー
# ===============================
# 画面（HTML）用 CSP。スクリプト・スタイルは自分のサイトのファイルのみ。インライン不可。
# 画像は data:（Leaflet 等）を許可。データの取得も自分のサイトのみ。
_PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"
)
# /login（core/dashboard_auth.py）は単純な HTML フォームで、スタイルをページ内に持つ。スクリプトは一切不可。
_LOGIN_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'; object-src 'none'"
)
# JSON・CSV など（画面以外）。ブラウザで直接開かれても何も実行・読み込みしない。
_DATA_CSP = "default-src 'none'; frame-ancestors 'none'"

_COMMON_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
    "Server": "QTL_Bot",                      # 既定の「Python/x.y aiohttp/z」（版が分かる）を出さない
}


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    """全レスポンス（エラー・認証失敗の応答を含む）にセキュリティヘッダーを付ける。"""
    try:
        response = await handler(request)
    except web.HTTPException as exc:          # 404 等の例外レスポンスにも付ける
        _apply_headers(request, exc)
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        # ハンドラ内の想定外の例外。aiohttp 任せにすると、セキュリティヘッダーが付かず、Server ヘッダーに
        # ライブラリの版（aiohttp/Python）も出てしまう。詳細はログにだけ残し、利用者には汎用の文言のみ返す。
        logger.exception(f"ダッシュボードの処理で想定外のエラー: {request.method} {request.path}")
        response = web.Response(status=500, text="Internal Server Error", content_type="text/plain")
    _apply_headers(request, response)
    return response


def _apply_headers(request: web.Request, response: web.StreamResponse) -> None:
    headers = response.headers
    for key, value in _COMMON_HEADERS.items():
        headers[key] = value
    if "Content-Security-Policy" not in headers:
        content_type = headers.get("Content-Type", "")
        if request.path in ("/login", "/logout") and content_type.startswith("text/html"):
            headers["Content-Security-Policy"] = _LOGIN_CSP
        elif content_type.startswith("text/html") or content_type.startswith("text/javascript") \
                or content_type.startswith("text/css"):
            headers["Content-Security-Policy"] = _PAGE_CSP
        else:
            headers["Content-Security-Policy"] = _DATA_CSP
    # 認証が絡む可能性のある応答は共有キャッシュ（プロキシ等）に保存させない
    headers.setdefault("Cache-Control", "no-store")


# ===============================
# ファイルの読み出しとキャッシュ
# ===============================
@dataclass(frozen=True)
class _Entry:
    body: bytes
    gz: Optional[bytes]
    etag: str
    content_type: str


class StaticFiles:
    """許可リストのファイルを読み出してプロセス内にキャッシュする（再起動で更新）。"""

    def __init__(self, web_dir: str = WEB_DIR):
        self._web_dir = web_dir
        self._cache: dict[str, Optional[_Entry]] = {}
        self._warned: set[str] = set()

    def _read(self, rel: str, content_type: str) -> Optional[_Entry]:
        """web/ 直下の通常ファイルのみ読む。見つからない・不正なら None（理由はログに1回だけ出す）。"""
        base_real = os.path.realpath(self._web_dir)
        expected = os.path.join(base_real, *rel.split("/"))
        candidate = os.path.join(self._web_dir, *rel.split("/"))
        try:
            real = os.path.realpath(candidate)
            if real != expected:                                   # シンボリックリンク等で実体が別の場所
                return self._warn_once(rel, "シンボリックリンクなど、web/ 内の想定した場所にありません")
            if not os.path.isfile(real):
                return self._warn_once(rel, "ファイルが見つかりません")
            if os.path.getsize(real) > _MAX_FILE_BYTES:
                return self._warn_once(rel, "ファイルが大きすぎます")
            with open(real, "rb") as f:
                body = f.read()
        except OSError as e:
            return self._warn_once(rel, f"読み込めません（{type(e).__name__}）")
        gz = gzip.compress(body, compresslevel=6, mtime=0) if len(body) > 1024 else None
        return _Entry(body=body, gz=gz if gz and len(gz) < len(body) else None,
                      etag='"' + hashlib.sha256(body).hexdigest()[:20] + '"', content_type=content_type)

    def _warn_once(self, rel: str, reason: str) -> None:
        if rel not in self._warned:
            self._warned.add(rel)
            logger.error(f"ダッシュボードのファイル web/{rel}: {reason}")
        return None

    def get(self, rel: str, content_type: str) -> Optional[_Entry]:
        if rel not in self._cache:
            self._cache[rel] = self._read(rel, content_type)
        return self._cache[rel]

    def missing(self) -> list[str]:
        """許可リストのうち、読み出せないもの（起動時の点検用）。"""
        out = [name for name in PAGES.values() if self.get(name, _HTML) is None]
        out += [rel for rel, ctype in ASSETS.items() if self.get(rel, ctype) is None]
        return out


def _respond(request: web.Request, entry: Optional[_Entry], cache_control: str) -> web.Response:
    if entry is None:
        return web.Response(status=503, text="dashboard files are not available", content_type="text/plain")
    # CSP と Content-Type は 304（変更なし）の応答にも必ず付ける。304 のヘッダーは、ブラウザが保存している
    # 前回の応答のヘッダーを「上書き」するため、ここで付けないと、ミドルウェアが既定の CSP（データ用の
    # default-src 'none'）を付けてしまい、2回目以降の表示でスクリプト・CSS がすべてブロックされる
    # （実ブラウザで確認して見つけた不具合。tests/test_web_static.py に回帰テストあり）。
    headers = {"ETag": entry.etag, "Cache-Control": cache_control, "Vary": "Accept-Encoding",
               "Content-Security-Policy": _PAGE_CSP, "Content-Type": entry.content_type}
    if request.headers.get("If-None-Match") == entry.etag:
        return web.Response(status=304, headers=headers)
    if entry.gz is not None and "gzip" in request.headers.get("Accept-Encoding", ""):
        headers["Content-Encoding"] = "gzip"
        body = entry.gz
    else:
        body = entry.body
    return web.Response(body=body, headers=headers)


def register_routes(app: web.Application, files: Optional[StaticFiles] = None) -> StaticFiles:
    """画面（/dashboard・/quake_map）と /assets/<許可リストのキー> のルートを登録する。"""
    files = files or StaticFiles()

    def make_page_handler(filename: str):
        async def handler(request: web.Request) -> web.Response:
            # 画面は常に再検証させる（ETag で変更が無ければ 304）。認証が絡むため共有キャッシュには載せない。
            return _respond(request, files.get(filename, _HTML), "private, no-cache")
        return handler

    for url_path, filename in PAGES.items():
        app.router.add_get(url_path, make_page_handler(filename))

    async def asset_handler(request: web.Request) -> web.Response:
        key = request.match_info.get("asset", "")
        content_type = ASSETS.get(key)                 # 辞書のキーとの文字列一致のみ。パスの組み立てには使わない
        if content_type is None:
            raise web.HTTPNotFound()
        return _respond(request, files.get(key, content_type), "private, max-age=3600")

    app.router.add_get("/assets/{asset:.+}", asset_handler)
    return files
