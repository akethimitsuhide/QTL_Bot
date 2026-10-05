"""
tests/test_kyoshin_notify.py
=============================
強震モニタ・長周期地震動モニタ・振動レベルの受信/通知の集約（cogs/kyoshin_monitor.py、
2026-10-05）のテスト。通知間隔の決定、EEW通知と検知通知の切り替え（EEW中は検知通知を
中断し、検知は継続）、振動レベルによる検知を確認する。
"""
import asyncio
import os
import sys
import time
from types import SimpleNamespace


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import cogs.kyoshin_monitor as km  # noqa: E402
from core.kyoshin_detector import SeismicEvent  # noqa: E402


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, embed=None, **kw):
        self.sent.append(embed)


def make_cog(monkeypatch, eew_active=None, **cfg):
    for k, v in cfg.items():
        monkeypatch.setattr(km, k, v)
    eew = SimpleNamespace(monitored_event_id=eew_active)
    bot = SimpleNamespace(get_cog=lambda n: eew if n == "EewCog" else None, is_closed=lambda: False)
    cog = km.KyoshinMonitorCog(bot)
    cog.kyoshin_channel = FakeChannel()
    cog.sounds = []

    async def fake_lmoni(session):
        return "https://example/lmoni.gif"

    async def fake_play(key):
        cog.sounds.append(key)
    cog._dual_image_fetcher.fetch_lmoni_url = fake_lmoni
    cog.play_mp3 = fake_play
    cog._last_image_url = "https://example/jma_s.gif"
    cog._last_image_at = time.monotonic()
    return cog, eew


def add_event(cog, n_stations=5, shindo=1.5):
    ev = SeismicEvent(event_id="abcdef123456", created_at=0.0, member_station_ids={str(i) for i in range(n_stations)},
                      max_shindo=shindo, phase="Medium")
    cog.monitor.event_manager.events[ev.event_id] = ev
    return ev


def set_vib(cog, level):
    cog._vib_level, cog._vib_at = level, time.monotonic()


def run(coro):
    return asyncio.run(coro)


def test_notify_interval_is_shortest_enabled_receive_interval(monkeypatch):
    cog, _ = make_cog(monkeypatch)
    assert cog._notify_interval_sec == 1.0                      # 既定: 画像1秒・振動レベル2秒
    cog, _ = make_cog(monkeypatch, KYOSHIN_RECV_JMA_S_ENABLE=False, KYOSHIN_RECV_LMONI_ENABLE=False)
    assert cog._notify_interval_sec == 2.0                      # 振動レベルのみ
    cog, _ = make_cog(monkeypatch, KYOSHIN_RECV_JMA_S_ENABLE=True, KYOSHIN_RECV_VIBRATION_ENABLE=False,
                      KYOSHIN_RECV_IMAGE_INTERVAL_SEC=3.0)
    assert cog._notify_interval_sec == 3.0
    cog, _ = make_cog(monkeypatch, KYOSHIN_RECV_JMA_S_ENABLE=False, KYOSHIN_RECV_LMONI_ENABLE=False,
                      KYOSHIN_RECV_VIBRATION_ENABLE=False)
    assert cog._notify_interval_sec is None                     # 受信がすべて無効


def test_config_floors():
    from core.config import KYOSHIN_RECV_IMAGE_INTERVAL_SEC, KYOSHIN_RECV_VIBRATION_INTERVAL_SEC
    assert KYOSHIN_RECV_IMAGE_INTERVAL_SEC >= 1.0 and KYOSHIN_RECV_VIBRATION_INTERVAL_SEC >= 2.0


def test_vibration_level_over_threshold_is_detection(monkeypatch):
    cog, _ = make_cog(monkeypatch)
    set_vib(cog, 99)
    run(cog._notify_tick())
    assert cog.kyoshin_channel.sent == []                       # 100未満は検知ではない
    set_vib(cog, 100)
    run(cog._notify_tick())
    assert len(cog.kyoshin_channel.sent) == 1
    assert "振動レベル検知" in cog.kyoshin_channel.sent[0].title
    assert cog._recv_count["kyoshin"] == 1 and cog._recv_count["long_period_monitor"] == 0
    assert cog.sounds == ["lv100"]


def test_stale_vibration_level_is_not_detection(monkeypatch):
    cog, _ = make_cog(monkeypatch)
    cog._vib_level, cog._vib_at = 5000, time.monotonic() - 60
    run(cog._notify_tick())
    assert cog.kyoshin_channel.sent == []


def test_eew_suspends_detection_notification_and_resumes(monkeypatch):
    cog, eew = make_cog(monkeypatch)
    add_event(cog)
    set_vib(cog, 500)
    run(cog._notify_tick())                                     # 検知通知
    assert "画像解析検知" in cog.kyoshin_channel.sent[-1].title
    eew.monitored_event_id = "E1"                               # EEW第一報
    run(cog._notify_tick())
    assert cog.kyoshin_channel.sent[-1].title == "強震モニタ"     # EEW通知のみ（検知通知は中断）
    assert cog._detect_suspended is True
    assert cog._recv_count["long_period_monitor"] == 1
    run(cog._notify_tick())
    assert all(e.title == "強震モニタ" for e in cog.kyoshin_channel.sent[1:])
    assert len(cog.monitor.event_manager.events) == 1           # 検知自体は続いている
    eew.monitored_event_id = None                               # 最終報/キャンセル報
    run(cog._notify_tick())
    assert "画像解析検知" in cog.kyoshin_channel.sent[-1].title   # 検知がまだ続いていれば再開
    assert cog._detect_suspended is False


def test_no_notification_after_eew_ends_when_detection_ended(monkeypatch):
    cog, eew = make_cog(monkeypatch, eew_active="E1")
    run(cog._notify_tick())
    n = len(cog.kyoshin_channel.sent)
    assert n == 1
    eew.monitored_event_id = None
    run(cog._notify_tick())
    assert len(cog.kyoshin_channel.sent) == n                   # 検知なし → 通知終了


def test_eew_notification_disabled_keeps_detection_notifications(monkeypatch):
    cog, _ = make_cog(monkeypatch, eew_active="E1", KYOSHIN_NOTIFY_ON_EEW=False)
    set_vib(cog, 300)
    run(cog._notify_tick())
    assert "振動レベル検知" in cog.kyoshin_channel.sent[-1].title   # 中断しない


def test_detect_notification_disabled_still_notifies_eew(monkeypatch):
    cog, eew = make_cog(monkeypatch, eew_active="E1", KYOSHIN_NOTIFY_ON_DETECT=False)
    run(cog._notify_tick())
    assert cog.kyoshin_channel.sent[-1].title == "強震モニタ"
    eew.monitored_event_id = None
    set_vib(cog, 300)
    run(cog._notify_tick())
    assert len(cog.kyoshin_channel.sent) == 1                   # 検知時の通知は無効


def test_event_gates_still_apply(monkeypatch):
    cog, _ = make_cog(monkeypatch)
    add_event(cog, n_stations=1, shindo=1.5)                    # 観測点数が閾値未満
    run(cog._notify_tick())
    assert cog.kyoshin_channel.sent == []


def test_disabled_receivers_are_not_included(monkeypatch):
    cog, _ = make_cog(monkeypatch, eew_active="E1", KYOSHIN_RECV_LMONI_ENABLE=False,
                      KYOSHIN_RECV_VIBRATION_ENABLE=False)
    run(cog._notify_tick())
    e = cog.kyoshin_channel.sent[-1]
    assert e.image.url == "https://example/jma_s.gif" and e.thumbnail.url is None
    assert "振動レベル" not in e.description
