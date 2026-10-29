"""
core/log_mask.py
=================
ログを画面（Web ダッシュボード・/qtl_logs）に表示するとき、Webhook URL・トークンなどの
秘密情報らしき文字列を伏せ字にする（純関数。Discord・ファイルには依存しない）。

【方針】
- 表示時にだけ伏せる。ログファイル自体は書き換えない。
- 「伏せすぎ」より「漏らさない」を優先するが、イベントID・P2P の id（24桁の16進数）・
  UUID などの一般的な識別子は伏せない（履歴の突き合わせに使うため）。
  そのため「長いランダム文字列を無条件に伏せる」規則は置かず、次の形だけを対象にする。
    1. Discord / Slack の Webhook URL
    2. Discord Bot トークンの形（<ID>.<時刻>.<署名>）
    3. Authorization: Bearer ...
    4. token / secret / password / api_key などを名前に含む `名前=値`・`名前: 値`・JSON `"名前": "値"`
    5. URL クエリの access_token / api_key など
- 伏せた件数を返す。ログに秘密情報が残っていないかの確認にも使える。
"""
from __future__ import annotations

import re

MASK = "***"

_SECRET_NAME = r"(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|apikey|authorization|cookie|session[_-]?id|credential)"

# 1) Webhook URL（URL 全体を置き換える）
_DISCORD_WEBHOOK = re.compile(
    r"https?://(?:[\w-]+\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
_SLACK_WEBHOOK = re.compile(r"https?://hooks\.slack\.com/services/[\w/-]+", re.IGNORECASE)

# 2) Discord Bot トークン（base64 の ID . 時刻 . 署名）
_DISCORD_TOKEN = re.compile(r"(?<![\w.-])[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,40}(?![\w-])")

# 3) Bearer トークン
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")

# 4) 名前に秘密らしい語を含む 名前=値 / 名前: 値（値は空白・区切り文字まで）
_KEY_VALUE = re.compile(
    rf"(?i)(\b[\w.-]*{_SECRET_NAME}[\w.-]*\s*[=:]\s*)(?:Bearer\s+)?(?![\"']|\*\*\*)([^\s,;&)\]}}]+)"
)
# 4') JSON / 引用符つき: "token": "xxxx"  /  token='xxxx'
_KEY_VALUE_QUOTED = re.compile(
    rf"(?i)((?:[\"'])[\w.-]*{_SECRET_NAME}[\w.-]*(?:[\"'])\s*[:=]\s*[\"'])([^\"']+)([\"'])"
)

# 5) URL クエリ
_QUERY = re.compile(rf"(?i)([?&][\w.-]*{_SECRET_NAME}[\w.-]*=)(?!\*\*\*)[^&\s\"'<>]+")


def mask_secrets(text: str) -> tuple[str, int]:
    """text の秘密情報らしき部分を伏せ字にして (伏せた後の文字列, 伏せた箇所の数) を返す。"""
    if not text:
        return text, 0
    count = 0

    def _sub(pattern: re.Pattern, repl, s: str) -> str:
        nonlocal count
        s, n = pattern.subn(repl, s)
        count += n
        return s

    text = _sub(_DISCORD_WEBHOOK, "[Webhook URL]", text)
    text = _sub(_SLACK_WEBHOOK, "[Webhook URL]", text)
    text = _sub(_BEARER, lambda m: m.group(1) + MASK, text)
    text = _sub(_KEY_VALUE_QUOTED, lambda m: m.group(1) + MASK + m.group(3), text)
    text = _sub(_KEY_VALUE, lambda m: m.group(1) + MASK, text)
    text = _sub(_QUERY, lambda m: m.group(1) + MASK, text)
    text = _sub(_DISCORD_TOKEN, "[Discord Token]", text)
    return text, count
