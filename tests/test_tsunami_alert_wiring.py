"""津波の警報・注意報・予報の読み上げ／EWS が、情報源（P2P・気象庁）をまたいで二重にならないことのテスト。

notify_tsunami（P2P）と notify_tsunami_forecast（気象庁JSON）を、本番と同じ非テスト経路で実際に通す。
判定の本体は core/tsunami_speech.AlertGate（判定ロジック自体は test_tsunami_speech.py）。
"""
import asyncio
import os

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest

import cogs.tsunami as tsunami_mod
from core.tsunami_speech import AlertGate


class _Channel:
    def __init__(self):
        self.sent = 0

    async def send(self, *args, **kwargs):
        self.sent += 1


def _cog():
    cog = object.__new__(tsunami_mod.TsunamiCog)
    cog.tsunami_channel = cog.channel = _Channel()
    cog.spoken = []
    cog.ews = []
    cog._alert_gate = AlertGate()

    async def speak_local(text, priority=2):
        cog.spoken.append(text)

    async def ews(grade):
        cog.ews.append(grade)

    cog.speak_local = speak_local
    cog._play_ews_signal = ews
    cog.session = None
    return cog


@pytest.fixture(autouse=True)
def _ews_on(monkeypatch):
    monkeypatch.setattr(tsunami_mod, "EWS_ENABLE", True)


def _p2p(cancelled=False, grade="Warning", names=("岩手県",), mid="t1"):
    return {"id": mid, "cancelled": cancelled, "issue": {"time": "2026/04/20 17:00:00", "source": "気象庁"},
            "areas": [{"name": n, "grade": grade, "immediate": False} for n in names]}


def _jma(items, headline=""):
    """items: [(区域名, Kind.Code, Kind.Name)]"""
    return {"Control": {"PublishingOffice": "気象庁"},
            "Head": {"Title": "津波警報・注意報・予報", "ReportDateTime": "2026-04-20T17:00:00+09:00",
                     "EventID": "E", "Headline": {"Text": headline}},
            "Body": {"Tsunami": {"Forecast": {"Item": [
                {"Area": {"Name": n}, "Category": {"Kind": {"Name": kn, "Code": c}}} for n, c, kn in items]}}}}


def _run(coro):
    asyncio.run(coro)


def test_p2p_then_jma_same_warning_speaks_and_rings_once():
    cog = _cog()
    _run(cog.notify_tsunami(_p2p()))
    _run(cog.notify_tsunami_forecast(_jma([("岩手県", "51", "津波警報")])))
    assert cog.spoken == ["岩手県に津波警報が発表されました"]
    assert cog.ews == ["Warning"]
    assert cog.channel.sent == 2          # Embed 通知は両方送る（抑止するのは読み上げ・EWSだけ）


def test_jma_then_p2p_same_warning_once():
    cog = _cog()
    _run(cog.notify_tsunami_forecast(_jma([("岩手県", "51", "津波警報")])))
    _run(cog.notify_tsunami(_p2p()))
    assert cog.spoken == ["津波警報が発表されました"]
    assert cog.ews == ["Warning"]


def test_followup_without_change_is_silent_but_escalation_and_added_areas_speak():
    cog = _cog()
    _run(cog.notify_tsunami_forecast(_jma([("岩手県", "51", "津波警報")])))
    _run(cog.notify_tsunami_forecast(_jma([("岩手県", "51", "津波警報")])))          # 続報（変化なし）
    assert len(cog.spoken) == 1 and cog.ews == ["Warning"]
    _run(cog.notify_tsunami_forecast(_jma([("岩手県", "51", "津波警報"),
                                           ("宮城県", "62", "津波注意報")])))         # 注意報の区域が増えた
    assert len(cog.spoken) == 2 and cog.ews == ["Warning"]
    _run(cog.notify_tsunami_forecast(_jma([("岩手県", "52", "大津波警報")])))         # 切り上げ
    assert len(cog.spoken) == 3 and cog.ews == ["Warning", "MajorWarning"]


def test_cancel_spoken_once_across_sources():
    cog = _cog()
    _run(cog.notify_tsunami(_p2p()))
    _run(cog.notify_tsunami(_p2p(cancelled=True, mid="t2")))
    _run(cog.notify_tsunami_forecast(_jma([("岩手県", "50", "津波警報解除")])))
    assert cog.spoken == ["岩手県に津波警報が発表されました", "津波情報が解除されました"]


def test_cancel_after_restart_is_still_spoken_once():
    """Bot 再起動後（状態なし）に届いた解除を、無音にしない。続けて別経路から来た同じ解除は読まない。"""
    cog = _cog()
    _run(cog.notify_tsunami_forecast(_jma([("岩手県", "50", "津波警報解除")])))
    _run(cog.notify_tsunami(_p2p(cancelled=True)))
    assert cog.spoken == ["津波警報が解除されました"]


def test_warning_to_forecast_code_73_is_not_treated_as_cancel():
    cog = _cog()
    _run(cog.notify_tsunami_forecast(_jma([("岩手県", "51", "津波警報")])))
    _run(cog.notify_tsunami_forecast(_jma([("岩手県", "73", "津波警報解除・津波予報")])))
    assert len(cog.spoken) == 2            # 警報→予報への切替（downgrade）として読む。解除扱いで無音にしない


def test_test_notifications_bypass_gate_and_do_not_pollute_state():
    cog = _cog()
    for _ in range(2):
        _run(cog.notify_tsunami(_p2p(), is_test=True))
    assert len(cog.spoken) == 2 and cog.ews == ["Warning", "Warning"]
    _run(cog.notify_tsunami(_p2p()))       # テスト通知は状態に残らないので、本番の第一報として読む
    assert len(cog.spoken) == 3 and len(cog.ews) == 3
