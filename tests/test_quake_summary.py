"""日次・週次の地震サマリー（core/quake_summary.py・cogs/quake_summary.py）のテスト（2026-10-27追加）。"""
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest  # noqa: E402

import cogs.quake_summary as cog_mod  # noqa: E402
from core.quake_summary import (  # noqa: E402
    build_summary_embed, merge_events, next_run_time, oldest_log_time, parse_summary_time,
    summarize, summary_period,
)


def rec(id_, time_raw, issue="DetailScale", name="調査中", scale=-1, mag=-1, depth=-1, tsunami="None"):
    return {"id": id_, "title": "x", "issue_type": issue, "source": "気象庁", "time_raw": time_raw,
            "hypocenter_name": name, "latitude": None, "longitude": None, "magnitude": mag,
            "depth": depth, "max_scale": scale, "domestic_tsunami": tsunami}


# 実運用ログ（2026-10-26〜27）の地震。1つの地震は震度速報→震源→各地の震度の順に3件記録される
KUMAMOTO = [
    rec("6adf6548", "2026/10/26 23:34:00", "ScalePrompt", "調査中", 30, -1, -1, "Checking"),
    rec("6adf6592", "2026/10/26 23:34:00", "Destination", "熊本県天草・芦北地方", -1, 2.6, 0),
    rec("6adf65d1", "2026/10/26 23:34:00", "DetailScale", "熊本県天草・芦北地方", 30, 2.6, 0),
]
HIROSHIMA = [rec("6adf8b78", "2026/10/27 02:16:00", "DetailScale", "広島県南西部", 10, 2.7, 20)]
KUSHIRO = [rec("6adfab70", "2026/10/27 04:32:00", "DetailScale", "釧路沖", 10, 3.3, 50)]


def test_parse_summary_time():
    assert parse_summary_time("00:05") == (0, 5)
    assert parse_summary_time(" 7:30 ") == (7, 30)
    for bad in ("25:00", "9", "abc", "12:60", ""):
        assert parse_summary_time(bad) == (0, 5)


def test_next_run_time_daily_and_weekly():
    now = datetime(2026, 10, 27, 10, 0)            # 火曜
    assert next_run_time(now, 0, 5) == datetime(2026, 10, 28, 0, 5)
    assert next_run_time(datetime(2026, 10, 27, 0, 1), 0, 5) == datetime(2026, 10, 27, 0, 5)
    assert next_run_time(now, 0, 5, weekday=0) == datetime(2026, 11, 2, 0, 5)    # 次の月曜
    assert next_run_time(datetime(2026, 11, 2, 0, 5), 0, 5, weekday=0) == datetime(2026, 11, 9, 0, 5)  # ちょうどなら翌週


def test_summary_period_uses_whole_days():
    run = datetime(2026, 10, 27, 0, 5)
    assert summary_period("daily", run) == (datetime(2026, 10, 26), datetime(2026, 10, 27))
    assert summary_period("weekly", run) == (datetime(2026, 10, 20), datetime(2026, 10, 27))


def test_merge_three_notifications_into_one_event():
    events = merge_events(KUMAMOTO)
    assert len(events) == 1
    e = events[0]
    assert (e["name"], e["max_scale"], e["magnitude"], e["depth"]) == ("熊本県天草・芦北地方", 30, 2.6, 0)


def test_merge_keeps_known_values_and_flags_tsunami_alert():
    events = merge_events([
        rec("1", "2026/10/27 01:00:00", "ScaleAndDestination", "三陸沖", 55, 7.0, 10, "Warning"),
        rec("2", "2026/10/27 01:00:00", "DetailScale", "調査中", -1, -1, -1, "None"),   # 後の情報が不明でも上書きしない
    ])
    assert len(events) == 1
    e = events[0]
    assert (e["name"], e["max_scale"], e["magnitude"], e["tsunami_alert"]) == ("三陸沖", 55, 7.0, True)


def test_foreign_and_broken_records():
    events = merge_events([
        rec("1", "2026/10/27 01:00:00", "Foreign", "フィリピン", -1, 6.5, 10),
        rec("2", "不明", "DetailScale", "x", 10, 1.0, 0),           # 時刻不明は除外
    ])
    assert len(events) == 1 and events[0]["foreign"] is True


def test_summarize_daily_window_and_counts():
    start, end = datetime(2026, 10, 27), datetime(2026, 10, 28)
    s = summarize(KUMAMOTO + HIROSHIMA + KUSHIRO, start, end)        # 熊本(10/26)は範囲外
    assert s["domestic_total"] == 2
    assert s["by_scale"] == {10: 2} and s["max_scale"] == 10
    assert s["max_magnitude_event"]["name"] == "釧路沖"
    assert [e["name"] for e in s["top"]] == ["釧路沖", "広島県南西部"]      # 同震度ならMの大きい順


def test_embed_daily_content():
    s = summarize(KUMAMOTO + HIROSHIMA + KUSHIRO, datetime(2026, 10, 26), datetime(2026, 10, 27))
    emb = build_summary_embed(s, "daily")
    assert "10月26日（月）" in emb.title
    assert "国内の地震: 1件" in emb.description and "震度3" in emb.description
    assert any("震度3: 1件" in f.value for f in emb.fields)
    assert any("23:34　熊本県天草・芦北地方　震度3　M2.6　ごく浅い" in f.value for f in emb.fields)


def test_embed_empty_day():
    s = summarize([], datetime(2026, 10, 26), datetime(2026, 10, 27))
    emb = build_summary_embed(s, "daily")
    assert "ありませんでした" in emb.description and not emb.fields


def test_embed_weekly_has_daily_breakdown_and_tsunami_note():
    recs = KUMAMOTO + HIROSHIMA + KUSHIRO + [
        rec("9", "2026/10/22 12:00:00", "ScaleAndDestination", "三陸沖", 45, 6.0, 10, "Watch")]
    s = summarize(recs, datetime(2026, 10, 20), datetime(2026, 10, 27))
    emb = build_summary_embed(s, "weekly")
    assert "週間地震サマリー（10/20〜10/26）" in emb.title
    day = next(f for f in emb.fields if f.name == "日別").value
    assert day.count("\n") == 6                                          # 7日分すべて載る
    assert "10/22(木): 1件（最大震度5弱）" in day and "10/21(水): 0件" in day
    assert any(f.name == "津波注意報以上を伴った地震" and f.value == "1件" for f in emb.fields)


def test_footer_warns_when_log_is_too_short():
    s = summarize(KUMAMOTO, datetime(2026, 10, 20), datetime(2026, 10, 27))
    short = build_summary_embed(s, "weekly", log_oldest=datetime(2026, 10, 24, 3, 0))
    ok = build_summary_embed(s, "weekly", log_oldest=datetime(2026, 10, 1))
    assert "実際より少なく" in short.footer.text and "実際より少なく" not in ok.footer.text


def test_oldest_log_time_reads_oldest_generation(tmp_path):
    base = str(tmp_path / "qtlbot.log")
    (tmp_path / "qtlbot.log.2").write_text("[2026-10-20 03:00:00] [INFO] QTLBot: a\n", encoding="utf-8")
    (tmp_path / "qtlbot.log.1").write_text("[2026-10-24 03:00:00] [INFO] QTLBot: b\n", encoding="utf-8")
    (tmp_path / "qtlbot.log").write_text("[2026-10-26 03:00:00] [INFO] QTLBot: c\n", encoding="utf-8")
    assert oldest_log_time(base) == datetime(2026, 10, 20, 3, 0)
    assert oldest_log_time(str(tmp_path / "none.log")) is None


# ── Cog: 送信の制御 ──
@pytest.fixture
def sender(monkeypatch):
    sent = []

    async def fake_send(channel, **kw):
        sent.append(kw["embed"])

    monkeypatch.setattr(cog_mod, "send_with_retry", fake_send)
    monkeypatch.setattr(cog_mod, "resolve_channels", lambda bot, ids: object())
    monkeypatch.setattr(cog_mod, "load_quake_history", lambda: KUMAMOTO + HIROSHIMA + KUSHIRO)
    monkeypatch.setattr(cog_mod, "oldest_log_time", lambda: None)
    c = cog_mod.QuakeSummaryCog(bot=None)
    return c, sent


def test_cog_sends_daily(sender):
    c, sent = sender
    asyncio.run(c._send_summary("daily", datetime(2026, 10, 27, 0, 5)))
    assert len(sent) == 1 and "10月26日" in sent[0].title


def test_cog_skips_empty_day_but_not_empty_week(sender, monkeypatch):
    c, sent = sender
    monkeypatch.setattr(cog_mod, "load_quake_history", lambda: [])
    asyncio.run(c._send_summary("daily", datetime(2026, 10, 27, 0, 5)))
    assert sent == []                                                    # 日次は0件なら投稿しない
    asyncio.run(c._send_summary("weekly", datetime(2026, 10, 27, 0, 5)))
    assert len(sent) == 1                                                # 週次は0件でも投稿する


def test_cog_does_not_send_without_channel(sender, monkeypatch):
    c, sent = sender
    monkeypatch.setattr(cog_mod, "resolve_channels", lambda bot, ids: None)
    asyncio.run(c._send_summary("daily", datetime(2026, 10, 27, 0, 5)))
    assert sent == []


def test_cog_load_starts_nothing_when_disabled(monkeypatch):
    monkeypatch.setattr(cog_mod, "QUAKE_SUMMARY_DAILY_ENABLE", False)
    monkeypatch.setattr(cog_mod, "QUAKE_SUMMARY_WEEKLY_ENABLE", False)
    c = cog_mod.QuakeSummaryCog(bot=None)
    asyncio.run(c.cog_load())
    assert c._tasks == []


def test_worker_schedules_next_slot_and_never_double_sends(monkeypatch):
    """起床が早めでも同じ投稿時刻を二重に処理せず、次の枠（翌日）へ進む。"""
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 27, 10, 0)

    class FakeBot:
        async def wait_until_ready(self):
            pass

        def is_closed(self):
            return False

    sleeps, runs = [], []

    async def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(cog_mod, "datetime", FixedDatetime)
    monkeypatch.setattr(cog_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(cog_mod, "QUAKE_SUMMARY_TIME", "00:05")
    c = cog_mod.QuakeSummaryCog(bot=FakeBot())

    async def fake_send(kind, run_at):
        runs.append(run_at)

    c._send_summary = fake_send
    asyncio.run(c._worker("daily"))
    assert runs == [datetime(2026, 10, 28, 0, 5), datetime(2026, 10, 29, 0, 5)]   # 同じ枠を繰り返さない
    assert sleeps[0] == pytest.approx(14 * 3600 + 5 * 60)                       # 10:00 → 翌 00:05
