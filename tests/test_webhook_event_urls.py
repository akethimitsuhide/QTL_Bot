"""Webhook の種別別 URL（WEBHOOK_URLS_<種別>）と柔軟な区切りのテスト。"""
import asyncio
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

from core import webhook_notify as wn
from core.webhook_notify import WebhookNotifier, parse_targets

D1 = "https://discord.com/api/webhooks/111/SECRET-ONE"
D2 = "https://discord.com/api/webhooks/222/SECRET-TWO"
S1 = "https://hooks.slack.com/services/T/B/SECRET-SLACK"


def test_separators_comma_semicolon_whitespace():
    targets, _, rejected = parse_targets(f"{D1};{S1}  {D2},", None)
    assert [t.kind for t in targets] == ["discord", "slack", "discord"] and rejected == []


def test_same_url_in_several_fields_is_one_target():
    global_t, ev, rejected = parse_targets(D1, {"eew": f"{D1},{S1}", "tsunami": S1})
    assert [t.index for t in global_t] == [1]
    assert ev["eew"][0] is global_t[0] and ev["eew"][1].index == 2 and ev["tsunami"][0] is ev["eew"][1]
    assert rejected == []


def test_rejections_name_the_variable_and_never_contain_urls():
    _, _, rejected = parse_targets("", {"eew": "http://discord.com/api/webhooks/1/SECRET-TOKEN", "quake": f"{D1},{D1}"})
    text = "\n".join(rejected)
    assert "WEBHOOK_URLS_EEW の1番目" in text and "WEBHOOK_URLS_QUAKE の2番目" in text and "重複" in text
    assert "SECRET" not in text


def test_event_specific_urls_ignore_the_global_event_filter():
    g, ev, _ = parse_targets(D1, {"eew": S1})
    n = WebhookNotifier(g, frozenset({"quake"}), event_targets=ev)      # 全種別向けは quake だけ
    assert [t.describe() for t in n.targets_for("EEW")] == ["slack#2"]     # eew は専用の宛先のみ
    assert [t.describe() for t in n.targets_for("地震情報")] == ["discord#1"]
    assert n.targets_for("津波") == [] and not n.should_send("津波")


def test_overlap_between_global_and_event_is_sent_once():
    g, ev, _ = parse_targets(f"{D1},{D2}", {"tsunami": D1})
    n = WebhookNotifier(g, None, event_targets=ev)
    assert [t.describe() for t in n.targets_for("津波")] == ["discord#1", "discord#2"]


def test_only_event_urls_enables_notifier():
    _, ev, _ = parse_targets("", {"volcano": D1})
    n = WebhookNotifier([], None, event_targets=ev)
    assert n.enabled and n.should_send("火山") and not n.should_send("EEW")
    assert not WebhookNotifier([], None).enabled


def test_describe_for_log_lists_event_targets_without_urls():
    g, ev, _ = parse_targets(D1, {"eew": S1})
    text = WebhookNotifier(g, None, event_targets=ev).describe_for_log()
    assert "全種別向け1件" in text and "eew専用1件" in text and "slack#2" in text
    assert "SECRET" not in text and "discord.com" not in text


def test_dispatch_delivers_to_chosen_targets_only(monkeypatch):
    g, ev, _ = parse_targets(D1, {"eew": S1})
    n = WebhookNotifier(g, frozenset({"quake"}), event_targets=ev)
    sent = []

    async def fake_deliver(self, session, target, payload, label):
        sent.append((label, target.describe(), sorted(payload)))
        return True

    monkeypatch.setattr(WebhookNotifier, "_deliver", fake_deliver)
    embeds = [{"title": "t", "description": "d"}]

    async def run():
        await n._run("EEW", embeds)
        await n._run("地震情報", embeds)
        await n._run("津波", embeds)

    asyncio.run(run())
    assert sent == [("EEW", "slack#2", ["attachments"]), ("地震情報", "discord#1", ["allowed_mentions", "embeds"])]
    assert wn.EVENT_KEYS == {"eew", "quake", "tsunami", "volcano", "usgs", "kanchi", "other"}
