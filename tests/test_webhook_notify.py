"""
Webhook 通知（core/webhook_notify.py と core/discord_send.py への組み込み）のテスト（2026-10-12追加）。

確認すること:
1. WEBHOOK_URLS の検証（https・Discord/Slack のホストのみ。URL をログ・repr に出さない）
2. WEBHOOK_EVENTS の解釈（all・種別指定・不正値は「すべて」に倒す）
3. Embed の変換（Discord: 添付画像の除去・空 Embed の除外・メンション無効化 / Slack: 変換とエスケープ）
4. 送信（実際の HTTP サーバーで）: 成功・429（Retry-After）・5xx の再送・4xx は再送しない・リダイレクト非追従
5. 複数宛先の独立性（1つが失敗しても他へ届く）、種別フィルター、送信待ちの上限
6. send_with_retry への組み込み: Discord の送信が失敗しても Webhook は配信される
"""
import asyncio
import logging
import os

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import discord  # noqa: E402
import pytest  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from core import discord_send, webhook_notify as wn  # noqa: E402
from core.webhook_notify import (  # noqa: E402
    WebhookNotifier, WebhookTarget, parse_event_filter, parse_webhook_urls,
    to_discord_payload, to_slack_payload,
)

DISCORD_URL = "https://discord.com/api/webhooks/123456/SECRET-TOKEN-abc"
SLACK_URL = "https://hooks.slack.com/services/T000/B000/SECRET-xyz"


# ---- URL の検証 ------------------------------------------------------------------

def test_parse_urls_accepts_discord_and_slack_and_dedupes():
    targets, rejected = parse_webhook_urls(f"{DISCORD_URL}, {SLACK_URL} ,{DISCORD_URL}")
    assert [(t.kind, t.index) for t in targets] == [("discord", 1), ("slack", 2)]
    assert len(rejected) == 1 and "重複" in rejected[0]


@pytest.mark.parametrize("bad", [
    "http://discord.com/api/webhooks/1/x",              # http
    "https://evil.example/api/webhooks/1/x",            # 他ホスト
    "https://discord.com.evil.example/api/webhooks/1/x",  # 似せたホスト
    "https://user:pass@discord.com/api/webhooks/1/x",   # 認証情報付き
    "https://discord.com:8443/api/webhooks/1/x",        # ポート指定
    "https://discord.com/other/path",                   # Webhook の形式ではない
    "https://hooks.slack.com/other/path",
    "ftp://discord.com/api/webhooks/1/x",
    "not a url",
])
def test_parse_urls_rejects(bad):
    targets, rejected = parse_webhook_urls(bad)
    assert targets == [] and len(rejected) == 1


def test_secrets_never_appear_in_repr_or_rejection_text():
    targets, _ = parse_webhook_urls(DISCORD_URL)
    assert "SECRET-TOKEN" not in repr(targets[0]) and "SECRET-TOKEN" not in targets[0].describe()
    _, rejected = parse_webhook_urls("http://discord.com/api/webhooks/1/SECRET-TOKEN")
    assert all("SECRET-TOKEN" not in r for r in rejected)


def test_parse_empty():
    assert parse_webhook_urls("") == ([], [])
    assert parse_webhook_urls(None) == ([], [])


# ---- 種別フィルター ----------------------------------------------------------------

def test_event_filter():
    assert parse_event_filter("all") == (None, [])
    assert parse_event_filter("") == (None, [])
    assert parse_event_filter("EEW, tsunami") == (frozenset({"eew", "tsunami"}), [])
    ev, unknown = parse_event_filter("eew,bogus")
    assert ev == frozenset({"eew"}) and unknown == ["bogus"]
    assert parse_event_filter("bogus") == (None, ["bogus"])      # 有効な語が無ければ「すべて」（取りこぼし防止）
    assert parse_event_filter("eew,all") == (None, [])


def test_event_key_mapping():
    assert wn.event_key("EEW") == "eew" and wn.event_key("地震情報") == "quake"
    assert wn.event_key("気象庁その他") == "other" and wn.event_key("") == "other"
    assert wn.EVENT_KEYS == {"eew", "quake", "tsunami", "volcano", "usgs", "kanchi", "other"}


# ---- Embed の変換 ------------------------------------------------------------------

def _embed():
    e = discord.Embed(title="緊急地震速報 (第1報)", description="**震源地： 能登半島** [詳細](https://example.com/a) <!channel> &", color=0xE52A18)
    e.add_field(name="最大震度", value="**5弱**", inline=True)
    e.set_footer(text="気象庁")
    return e.to_dict()


def test_discord_payload_strips_attachment_images_and_disables_mentions():
    d = _embed()
    d["image"] = {"url": "attachment://gis_map_0.png"}
    d["thumbnail"] = {"url": "https://example.com/t.png"}
    extra = {"image": {"url": "attachment://gis_map_1.png"}}          # 画像だけの追加 Embed → 空になる
    p = to_discord_payload([d, extra])
    assert p["allowed_mentions"] == {"parse": []}
    assert len(p["embeds"]) == 1
    emb = p["embeds"][0]
    assert "image" not in emb and emb["thumbnail"]["url"] == "https://example.com/t.png"
    assert emb["title"] == d["title"] and emb["color"] == 0xE52A18 and "type" not in emb


def test_discord_payload_none_when_nothing_to_send():
    assert to_discord_payload([{"image": {"url": "attachment://a.png"}}]) is None
    assert to_discord_payload([]) is None


def test_discord_payload_caps_embed_count():
    assert len(to_discord_payload([{"title": str(i)} for i in range(15)])["embeds"]) == 10


def test_slack_payload_conversion_and_escaping():
    p = to_slack_payload([_embed()])
    att = p["attachments"][0]
    assert att["color"] == "#e52a18" and att["title"] == "緊急地震速報 (第1報)"
    assert "*震源地： 能登半島*" in att["text"]                       # **太字** → *太字*
    assert "<https://example.com/a|詳細>" in att["text"]            # [text](url) → <url|text>
    assert "&lt;!channel&gt;" in att["text"] and "&amp;" in att["text"]   # 意図しないメンションの無効化
    assert "<!channel>" not in att["text"]
    assert att["fields"] == [{"title": "最大震度", "value": "*5弱*", "short": True}]
    assert att["footer"] == "気象庁"


def test_slack_payload_timestamp_and_empty():
    e = discord.Embed(title="t", timestamp=__import__("datetime").datetime(2026, 10, 12, 9, 0, tzinfo=__import__("datetime").timezone.utc))
    assert to_slack_payload([e.to_dict()])["attachments"][0]["ts"] == 1791795600
    assert to_slack_payload([{}]) is None


# ---- 送信（実際の HTTP サーバー）------------------------------------------------------

class Hook:
    """テスト用の受信側。responses の順に応答し、受信した JSON を記録する。"""

    def __init__(self, *responses):
        self.responses = list(responses) or [(204, {})]
        self.received: list[dict] = []
        self.headers: list[dict] = []

    async def handle(self, request):
        self.received.append(await request.json())
        self.headers.append(dict(request.headers))
        status, hdrs = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if status in (301, 302):
            return web.Response(status=status, headers=hdrs)
        return web.Response(status=status, headers=hdrs, text="" if status < 400 else "bad_request_detail")


async def _serve(*hooks):
    """hooks ごとにサーバーを起動し (servers, [WebhookTarget...]) を返す。"""
    servers, targets = [], []
    for i, (hook, kind) in enumerate(hooks, 1):
        app = web.Application()
        app.router.add_post("/hook", hook.handle)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        targets.append(WebhookTarget(kind=kind, index=i, url=str(server.make_url("/hook"))))
    return servers, targets


@pytest.fixture
def sleeps(monkeypatch):
    rec = []
    real_sleep = asyncio.sleep

    async def fake(sec):
        if sec:                       # aiohttp 内部の sleep(0) は記録しない
            rec.append(sec)
        await real_sleep(0)

    monkeypatch.setattr(wn.asyncio, "sleep", fake)
    return rec


def _run_dispatch(hooks_kinds, label="EEW", events=None, embeds=None, attempts=3):
    async def go():
        servers, targets = await _serve(*hooks_kinds)
        try:
            n = WebhookNotifier(targets, events, attempts=attempts)
            n.dispatch(label, embeds if embeds is not None else [_embed()])
            while n._tasks:
                await asyncio.gather(*list(n._tasks))
        finally:
            for s in servers:
                await s.close()
    asyncio.run(go())


def test_delivery_success_and_headers(sleeps):
    hook = Hook()
    _run_dispatch([(hook, "discord")])
    assert len(hook.received) == 1 and "embeds" in hook.received[0]
    assert "QTL_Bot" in hook.headers[0]["User-Agent"]


def test_slack_target_gets_slack_format(sleeps):
    hook = Hook((200, {}))
    _run_dispatch([(hook, "slack")])
    assert "attachments" in hook.received[0] and "embeds" not in hook.received[0]


def test_429_honors_retry_after_then_succeeds(sleeps):
    hook = Hook((429, {"Retry-After": "3"}), (204, {}))
    _run_dispatch([(hook, "discord")])
    assert len(hook.received) == 2 and sleeps == [3.0]


def test_429_retry_after_is_capped(sleeps):
    hook = Hook((429, {"Retry-After": "999"}), (204, {}))
    _run_dispatch([(hook, "discord")])
    assert sleeps == [10.0]


def test_5xx_retried_with_backoff_then_gives_up(sleeps, caplog):
    hook = Hook((503, {}))
    with caplog.at_level(logging.WARNING, logger="QTLBot"):
        _run_dispatch([(hook, "discord")])
    assert len(hook.received) == 3 and sleeps == [2.0, 4.0]
    assert any("3回試行" in r.getMessage() and "discord#1" in r.getMessage() for r in caplog.records)


def test_4xx_not_retried_and_logged_without_url(sleeps, caplog):
    hook = Hook((400, {}))
    with caplog.at_level(logging.WARNING, logger="QTLBot"):
        _run_dispatch([(hook, "slack")])
    assert len(hook.received) == 1 and sleeps == []
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "HTTP 400" in msgs and "bad_request_detail" in msgs and "127.0.0.1" not in msgs


def test_redirect_is_not_followed(sleeps):
    target_hook = Hook()
    redirector = Hook((302, {"Location": "http://127.0.0.1:1/elsewhere"}))
    _run_dispatch([(redirector, "discord")])
    assert len(redirector.received) == 1 and target_hook.received == []


def test_network_error_is_retried_without_leaking_url(sleeps, caplog):
    async def go():
        t = WebhookTarget("discord", 1, url="http://127.0.0.1:9/hook")   # 接続できないポート
        n = WebhookNotifier([t], None)
        n.dispatch("EEW", [_embed()])
        await asyncio.gather(*list(n._tasks))
    with caplog.at_level(logging.WARNING, logger="QTLBot"):
        asyncio.run(go())
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "3回試行" in msgs and "127.0.0.1" not in msgs and sleeps == [2.0, 4.0]


# ---- 複数宛先・フィルター・上限 ----------------------------------------------------

def test_one_target_failing_does_not_affect_the_other(sleeps):
    bad, good = Hook((500, {})), Hook()
    _run_dispatch([(bad, "discord"), (good, "slack")])
    assert len(bad.received) == 3 and len(good.received) == 1


def test_event_filter_applies(sleeps):
    hook = Hook()
    _run_dispatch([(hook, "discord")], label="津波", events=frozenset({"eew"}))
    assert hook.received == []
    _run_dispatch([(hook, "discord")], label="EEW", events=frozenset({"eew"}))
    assert len(hook.received) == 1


def test_nothing_sent_for_empty_embeds(sleeps):
    hook = Hook()
    _run_dispatch([(hook, "discord")], embeds=[{"image": {"url": "attachment://a.png"}}])
    assert hook.received == []


def test_pending_limit_drops_new_dispatches(monkeypatch, caplog):
    async def go():
        n = WebhookNotifier([WebhookTarget("discord", 1, url="http://127.0.0.1:9/h")], None)
        monkeypatch.setattr(wn, "_MAX_PENDING", 2)
        started = []

        async def slow(label, embeds):
            started.append(1)
            await asyncio.sleep(0.05)

        monkeypatch.setattr(n, "_run", slow)
        for _ in range(4):
            n.dispatch("EEW", [{"title": "t"}])
        await asyncio.gather(*list(n._tasks))
        return len(started)
    with caplog.at_level(logging.WARNING, logger="QTLBot"):
        assert asyncio.run(go()) == 2
    assert any("送信待ちが" in r.getMessage() for r in caplog.records)


def test_describe_for_log_has_no_urls():
    targets, _ = parse_webhook_urls(f"{DISCORD_URL},{SLACK_URL}")
    text = WebhookNotifier(targets, frozenset({"eew", "tsunami"})).describe_for_log()
    assert "discord#1" in text and "slack#2" in text and "eew,tsunami" in text
    assert "SECRET" not in text and "discord.com" not in text
    assert "無効" in WebhookNotifier([], None).describe_for_log()


# ---- send_with_retry への組み込み ---------------------------------------------------

class _FakeNotifier:
    def __init__(self, enabled=True, boom=False):
        self.enabled, self.boom, self.calls = enabled, boom, []

    def should_send(self, label):
        return self.enabled

    def dispatch(self, label, embeds):
        if self.boom:
            raise RuntimeError("dispatch failed")
        self.calls.append((label, embeds))


class _Channel:
    def __init__(self, exc=None):
        self.exc, self.sent = exc, []

    async def send(self, *a, **kw):
        self.sent.append(kw)
        if self.exc:
            raise self.exc
        return "msg"


def test_send_with_retry_dispatches_webhook_even_if_discord_fails(monkeypatch):
    fake = _FakeNotifier()
    monkeypatch.setattr(wn, "notifier", fake)
    resp = type("R", (), {"status": 403, "reason": "x"})()
    ch = _Channel(discord.Forbidden(resp, "no perm"))
    with pytest.raises(discord.Forbidden):
        asyncio.run(discord_send.send_with_retry(ch, _label="津波", embed=discord.Embed(title="津波警報")))
    assert len(fake.calls) == 1 and fake.calls[0][0] == "津波"           # Discord 失敗でも配信される
    assert fake.calls[0][1][0]["title"] == "津波警報"


def test_send_with_retry_dispatches_once_even_with_retries(monkeypatch):
    fake = _FakeNotifier()
    monkeypatch.setattr(wn, "notifier", fake)

    async def fake_sleep(sec):
        pass

    monkeypatch.setattr(discord_send.asyncio, "sleep", fake_sleep)
    resp = type("R", (), {"status": 503, "reason": "x"})()
    ch = _Channel(discord.DiscordServerError(resp, "down"))
    with pytest.raises(discord.DiscordServerError):
        asyncio.run(discord_send.send_with_retry(ch, _label="EEW", embed=discord.Embed(title="t")))
    assert len(ch.sent) == 3 and len(fake.calls) == 1                   # Discord は3回試行、Webhook は1回


def test_webhook_failure_never_breaks_discord_send(monkeypatch):
    monkeypatch.setattr(wn, "notifier", _FakeNotifier(boom=True))
    ch = _Channel()
    assert asyncio.run(discord_send.send_with_retry(ch, _label="EEW", embed=discord.Embed(title="t"))) == "msg"


def test_send_with_retry_handles_embeds_list_and_non_embed(monkeypatch):
    fake = _FakeNotifier()
    monkeypatch.setattr(wn, "notifier", fake)
    ch = _Channel()
    asyncio.run(discord_send.send_with_retry(
        ch, _label="火山", embed=discord.Embed(title="a"), embeds=[discord.Embed(title="b")]))
    assert [e["title"] for e in fake.calls[0][1]] == ["a", "b"]
    fake.calls.clear()
    asyncio.run(discord_send.send_with_retry(ch, _label="火山", embed="not-an-embed"))   # Embed でなければ送らない
    assert fake.calls == []


def test_disabled_notifier_is_noop(monkeypatch):
    fake = _FakeNotifier(enabled=False)
    monkeypatch.setattr(wn, "notifier", fake)
    asyncio.run(discord_send.send_with_retry(_Channel(), _label="EEW", embed=discord.Embed(title="t")))
    assert fake.calls == []
