"""
core/webhook_notify.py
=======================
Discord / Slack の Incoming Webhook へ、通知を追加で送る（複数宛先に同時送信可）。

【位置づけ】
Discord チャンネルへの通知（core/discord_send.py の send_with_retry）に相乗りする形で、
同じ内容を Webhook にも送る。各 Cog の個別の変更は不要。Discord 側の送信の成否とは
独立しており、Discord が不調でも Webhook には届く（別経路としての冗長化）。
Webhook の送信は別タスクで行い、失敗・遅延しても Discord への通知や EEW の音声には影響しない。

【設定（.env）】
  WEBHOOK_URLS   : Webhook URL（カンマ区切りで複数可）。空なら無効。
                   許可するのは https の Discord / Slack の Webhook URL のみ。
  WEBHOOK_EVENTS : 送る通知の種別。all（既定）または eew,quake,tsunami,volcano,usgs,kanchi,other
                   のカンマ区切り。

【送信形式】
  宛先のホストで自動判別する。
  - Discord（discord.com 等）: Embed をそのまま `{"embeds": [...]}` で送る
  - Slack（hooks.slack.com）  : Embed を attachment 形式へ変換（Slack は embeds を受け付けないため）
  添付画像（attachment://...）は Webhook では参照できないため送らない。

【セキュリティ】
- Webhook URL は「知っていれば誰でも投稿できる」秘密情報。ログ・例外メッセージ・
  表示には出さず、宛先は「discord#1」「slack#2」のように番号で表す。
- https かつ許可したホストの URL だけを受け付ける（認証情報付き URL・他ホストは拒否）。
- リダイレクトは追従しない（意図しない宛先への転送を防ぐ）。
- Slack 向けは & < > をエスケープし、外部由来のテキストによる <!channel> 等の
  意図しないメンション発火を防ぐ。Discord 向けは allowed_mentions で一切のメンションを無効化する。

【再送】
  429 / 5xx / ネットワークエラーは最大3回（2秒→4秒。429 は Retry-After を尊重、上限10秒）。
  それ以外の 4xx は再送しない。最終的に失敗しても WARNING ログのみ（Bot の動作は継続）。
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

import aiohttp

from core.config import WEBHOOK_URLS, WEBHOOK_EVENTS

logger = logging.getLogger("QTLBot")

_DISCORD_HOSTS = frozenset({"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"})
_SLACK_HOSTS = frozenset({"hooks.slack.com"})

# send_with_retry の _label → 種別キー（WEBHOOK_EVENTS で指定する ASCII の名前）
_LABEL_TO_EVENT = {
    "EEW": "eew",
    "地震情報": "quake",
    "津波": "tsunami",
    "火山": "volcano",
    "USGS": "usgs",
    "地震感知情報": "kanchi",
    "気象庁その他": "other",
}
EVENT_KEYS = frozenset(_LABEL_TO_EVENT.values())

_ATTEMPTS = 3
_BASE_DELAY_SEC = 2.0
_MAX_DELAY_SEC = 10.0
_REQUEST_TIMEOUT_SEC = 10
_MAX_PENDING = 50           # 受信側が止まっているときに送信タスクが溜まり続けないための上限
_MAX_DISCORD_EMBEDS = 10    # Discord の Webhook の上限
_MAX_SLACK_ATTACHMENTS = 20
_USER_AGENT = "QTL_Bot (https://github.com/akethimitsuhide/QTL_Bot)"


@dataclass(frozen=True)
class WebhookTarget:
    kind: str                                   # "discord" | "slack"
    index: int                                  # 設定内の番号（1始まり）
    url: str = field(repr=False, compare=False)  # repr に出さない（ログへの混入防止）

    def describe(self) -> str:
        return f"{self.kind}#{self.index}"


# ───────────────────────── 設定のパース ─────────────────────────

def parse_webhook_urls(raw: str | None) -> tuple[list[WebhookTarget], list[str]]:
    """
    WEBHOOK_URLS を (有効な宛先, 却下した項目の説明) に分ける。
    却下の説明に URL は含めない（番号と理由のみ）。同じ URL の重複は1つにまとめる。
    """
    targets: list[WebhookTarget] = []
    rejected: list[str] = []
    seen: set[str] = set()
    items = [p.strip() for p in (raw or "").split(",") if p.strip()]
    for pos, item in enumerate(items, 1):
        parts = urlsplit(item)
        reason = None
        host = (parts.hostname or "").lower()
        if parts.scheme != "https":
            reason = "https の URL ではありません"
        elif parts.username or parts.password or "@" in parts.netloc:
            reason = "認証情報付きの URL は使えません"
        elif parts.port not in (None, 443):
            reason = "ポート指定は使えません"
        elif host in _DISCORD_HOSTS:
            kind = "discord"
            if not parts.path.startswith("/api/webhooks/"):
                reason = "Discord の Webhook URL の形式ではありません"
        elif host in _SLACK_HOSTS:
            kind = "slack"
            if not parts.path.startswith("/services/"):
                reason = "Slack の Webhook URL の形式ではありません"
        else:
            reason = "Discord / Slack 以外のホストです"
        if reason:
            rejected.append(f"{pos}番目: {reason}")
            continue
        if item in seen:
            rejected.append(f"{pos}番目: 重複しているため無視しました")
            continue
        seen.add(item)
        targets.append(WebhookTarget(kind=kind, index=len(targets) + 1, url=item))
    return targets, rejected


def parse_event_filter(raw: str | None) -> tuple[frozenset[str] | None, list[str]]:
    """
    WEBHOOK_EVENTS を (許可する種別の集合 or None=すべて, 無視した語のリスト) にする。
    有効な語が1つも無い場合は、アラートの取りこぼしを避けるため「すべて」に倒す。
    """
    tokens = [t.strip().lower() for t in (raw or "").split(",") if t.strip()]
    if not tokens or "all" in tokens:
        return None, []
    valid = frozenset(t for t in tokens if t in EVENT_KEYS)
    unknown = [t for t in tokens if t not in EVENT_KEYS]
    return (valid or None), unknown


def event_key(label: str) -> str:
    return _LABEL_TO_EVENT.get(label, "other")


# ───────────────────────── Embed の変換 ─────────────────────────

def _is_attachment_url(url) -> bool:
    return isinstance(url, str) and url.startswith("attachment://")


def _clean_embed(embed: dict) -> dict:
    """Discord 用: Webhook 先からは参照できない添付画像の参照などを除いた Embed。"""
    out = {k: v for k, v in embed.items() if k not in ("type", "video", "provider")}
    for key in ("image", "thumbnail"):
        if key in out and _is_attachment_url((out[key] or {}).get("url")):
            del out[key]
    footer = out.get("footer")
    if isinstance(footer, dict) and _is_attachment_url(footer.get("icon_url")):
        out["footer"] = {k: v for k, v in footer.items() if k != "icon_url"}
    author = out.get("author")
    if isinstance(author, dict) and _is_attachment_url(author.get("icon_url")):
        out["author"] = {k: v for k, v in author.items() if k != "icon_url"}
    return out


def _is_empty_embed(embed: dict) -> bool:
    return not any(embed.get(k) for k in ("title", "description", "fields", "footer", "image", "thumbnail", "author"))


def to_discord_payload(embeds: list[dict]) -> dict | None:
    cleaned = [e for e in (_clean_embed(x) for x in embeds) if not _is_empty_embed(e)]
    if not cleaned:
        return None
    return {"embeds": cleaned[:_MAX_DISCORD_EMBEDS], "allowed_mentions": {"parse": []}}


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _discord_md_to_slack(text: str) -> str:
    """Discord の Markdown（太字・リンク）を Slack の mrkdwn へ。先にエスケープしてから変換する。"""
    text = _slack_escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)
    return text


def to_slack_payload(embeds: list[dict]) -> dict | None:
    attachments = []
    for emb in (_clean_embed(x) for x in embeds):
        if _is_empty_embed(emb):
            continue
        att: dict = {}
        title = emb.get("title") or ""
        description = emb.get("description") or ""
        att["fallback"] = _slack_escape(title or description[:100] or "QTL_Bot")
        if title:
            att["title"] = _slack_escape(title)
            if emb.get("url"):
                att["title_link"] = emb["url"]
        if description:
            att["text"] = _discord_md_to_slack(description)
            att["mrkdwn_in"] = ["text", "fields"]
        if isinstance(emb.get("color"), int):
            att["color"] = f"#{emb['color']:06x}"
        fields = [
            {"title": _slack_escape(f.get("name", "")), "value": _discord_md_to_slack(f.get("value", "")),
             "short": bool(f.get("inline"))}
            for f in emb.get("fields", []) or []
        ]
        if fields:
            att["fields"] = fields
        footer = emb.get("footer")
        if isinstance(footer, dict) and footer.get("text"):
            att["footer"] = _slack_escape(footer["text"])
        image = emb.get("image")
        if isinstance(image, dict) and image.get("url"):
            att["image_url"] = image["url"]
        ts = emb.get("timestamp")
        if ts:
            try:
                att["ts"] = int(datetime.fromisoformat(ts).timestamp())
            except (ValueError, TypeError):
                pass
        attachments.append(att)
    if not attachments:
        return None
    return {"attachments": attachments[:_MAX_SLACK_ATTACHMENTS]}


# ───────────────────────── 送信 ─────────────────────────

def _retry_after_sec(resp: aiohttp.ClientResponse, attempt: int) -> float:
    header = resp.headers.get("Retry-After")
    try:
        if header is not None:
            return min(max(float(header), 0.0), _MAX_DELAY_SEC)
    except ValueError:
        pass
    return min(_BASE_DELAY_SEC * (2 ** (attempt - 1)), _MAX_DELAY_SEC)


class WebhookNotifier:
    def __init__(self, targets: list[WebhookTarget], events: frozenset[str] | None,
                 attempts: int = _ATTEMPTS):
        self.targets = list(targets)
        self.events = events                    # None = すべて
        self._attempts = max(1, attempts)
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.targets)

    def should_send(self, label: str) -> bool:
        return self.enabled and (self.events is None or event_key(label) in self.events)

    def dispatch(self, label: str, embeds: list[dict]) -> None:
        """送信タスクを起動して即座に戻る（呼び出し元を待たせない）。イベントループ内から呼ぶこと。"""
        if not self.should_send(label):
            return
        if len(self._tasks) >= _MAX_PENDING:
            logger.warning(f"Webhook の送信待ちが{_MAX_PENDING}件に達したため、この通知は送りません（宛先が応答していない可能性）")
            return
        task = asyncio.get_running_loop().create_task(self._run(label, embeds))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, label: str, embeds: list[dict]) -> None:
        payloads = {"discord": to_discord_payload(embeds), "slack": to_slack_payload(embeds)}
        jobs = [(t, payloads[t.kind]) for t in self.targets if payloads[t.kind] is not None]
        if not jobs:
            return
        try:
            timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SEC)
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": _USER_AGENT}) as session:
                await asyncio.gather(
                    *(self._deliver(session, t, p, label) for t, p in jobs),
                    return_exceptions=True,
                )
        except Exception as e:  # 想定外でも Bot 本体へは波及させない
            logger.warning(f"Webhook 送信処理で予期しないエラー: {type(e).__name__}")

    async def _deliver(self, session: aiohttp.ClientSession, target: WebhookTarget,
                       payload: dict, label: str) -> bool:
        tag = f"[{label}] {target.describe()}"
        reason = ""
        for attempt in range(1, self._attempts + 1):
            delay = min(_BASE_DELAY_SEC * (2 ** (attempt - 1)), _MAX_DELAY_SEC)
            try:
                async with session.post(target.url, json=payload, allow_redirects=False) as resp:
                    status = resp.status
                    if 200 <= status < 300:
                        return True
                    if status == 429 or status >= 500:
                        reason = f"HTTP {status}"
                        delay = _retry_after_sec(resp, attempt) if status == 429 else delay
                    else:
                        body = (await resp.text())[:100].replace("\n", " ")
                        logger.warning(f"Webhook 送信に失敗（再送しません）: {tag} HTTP {status} {body}")
                        return False
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # 例外の文字列には URL が含まれうるため、型名だけを記録する
                reason = type(e).__name__
            if attempt < self._attempts:
                await asyncio.sleep(delay)
        logger.warning(f"Webhook 送信に失敗しました（{self._attempts}回試行）: {tag} {reason}")
        return False

    def describe_for_log(self) -> str:
        if not self.enabled:
            return "Webhook 通知: 無効（WEBHOOK_URLS 未設定）"
        kinds = ", ".join(t.describe() for t in self.targets)
        events = "all" if self.events is None else ",".join(sorted(self.events))
        return f"Webhook 通知: 有効（宛先{len(self.targets)}件: {kinds} / 種別: {events}）"


# ───────────────────────── 既定のインスタンスと呼び出し口 ─────────────────────────

_targets, rejected_urls = parse_webhook_urls(WEBHOOK_URLS)
_events, unknown_events = parse_event_filter(WEBHOOK_EVENTS)
notifier = WebhookNotifier(_targets, _events)


def log_startup_status() -> None:
    """起動時に設定の状況（却下した項目・無視した種別名を含む）をログに出す。URL は出さない。"""
    for r in rejected_urls:
        logger.warning(f"WEBHOOK_URLS: {r}")
    for t in unknown_events:
        logger.warning(f"WEBHOOK_EVENTS: 未知の種別 {t!r} を無視しました（{', '.join(sorted(EVENT_KEYS))} / all）")
    logger.info(notifier.describe_for_log())


def dispatch_from_send(send_kwargs: dict, label: str) -> None:
    """
    channel.send に渡す引数から Embed を取り出して Webhook へ配信する。
    例外は絶対に呼び出し元（Discord 通知）へ伝えない。
    """
    n = notifier
    if not n.should_send(label):
        return
    try:
        embeds = []
        if send_kwargs.get("embed") is not None:
            embeds.append(send_kwargs["embed"])
        embeds.extend(send_kwargs.get("embeds") or [])
        dicts = [e.to_dict() for e in embeds if hasattr(e, "to_dict")]
        if dicts:
            n.dispatch(label, dicts)
    except Exception as e:
        logger.warning(f"Webhook への配信準備に失敗: {type(e).__name__}")
