"""
起動時の「既存情報」判定（core/startup_guard.py と各ポーラーの初期化）のテスト（2026-10-09追加）。

主に次の2つの取りこぼしの回帰テスト:
1. 平常時に list.json 等が空リストを返すと初期化されず、その後の最初の本物の発表が
   「起動時の既存情報」として握りつぶされる（噴火速報・噴火警報・火山情報・長周期地震動 等）
2. 起動直後の取得失敗で初期化が遅れると、その間に発表された情報が握りつぶされる
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core import startup_guard as sg  # noqa: E402

JST = timezone(timedelta(hours=9))
START = 1_800_000_000.0  # 固定の「起動時刻」（テスト用）


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, JST).isoformat()


@pytest.fixture(autouse=True)
def fixed_start(monkeypatch):
    monkeypatch.setattr(sg, "BOT_START_EPOCH", START)


OLD = iso(START - 3600)   # 起動の1時間前に発表（既存情報）
NEW = iso(START + 30)     # 起動の30秒後に発表（起動後の新規情報）


# ── 純関数 ──
def test_parse_and_compare():
    assert sg.reported_since_start(NEW) is True
    assert sg.reported_since_start(OLD) is False
    assert sg.reported_since_start("2026-09-30T14:08:00+09:00") is False  # 過去
    # タイムゾーン無しはJST扱い
    naive = datetime.fromtimestamp(START + 30, JST).replace(tzinfo=None).isoformat()
    assert sg.reported_since_start(naive) is True


@pytest.mark.parametrize("bad", [None, "", "not a date", 123, "20260930140837"[:3]])
def test_unparseable_is_treated_as_existing(bad):
    assert sg.reported_since_start(bad) is False


# ── 偽のHTTPセッション ──
class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, **kw):
        return self.payload


class FakeSession:
    """routes: {URL部分文字列: payload または payload を返す関数}。呼び出しごとに先頭を消費するリストも可。"""

    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **kw):
        for key, val in self.routes.items():
            if key in url:
                payload = val() if callable(val) else val
                return _Resp(payload)
        raise AssertionError(f"想定外のURL: {url}")


def run(coro):
    return asyncio.run(coro)


# ── 噴火速報・噴火警報（cogs/volcano.py） ──
def _volcano_cog():
    from cogs.volcano import VolcanoCog
    c = VolcanoCog.__new__(VolcanoCog)
    c._last_eruption_id = None
    c._eruption_initialized = False
    c._last_warning_id = None
    c._warning_initialized = False
    c._last_recv, c._recv_count = {}, {}
    c._last_eruption_recv_time = c._last_warning_recv_time = None
    c._eruption_recv_count = c._warning_recv_count = 0
    c._notify_eruption = AsyncMock()
    c._notify_warning = AsyncMock()
    return c


def _item(event_id, report):
    return {"eventId": event_id, "reportDatetime": report}


@pytest.mark.parametrize("kind", ["eruption", "warning"])
def test_volcano_first_real_alert_after_empty_list_is_notified(kind):
    """平常時は空リスト → その後に最初の本物の発表。従来は握りつぶされていた。"""
    c = _volcano_cog()
    payloads = [[], [_item("E1", OLD)]]   # 発表時刻は過去でも、「初期化後に現れた」なら通知する
    c.session = FakeSession({f"/{kind}.json": lambda: payloads.pop(0)})
    fetch = getattr(c, f"fetch_{kind}_info")
    run(fetch())
    notify = getattr(c, f"_notify_{kind}")
    notify.assert_not_awaited()
    run(fetch())
    notify.assert_awaited_once()


@pytest.mark.parametrize("kind", ["eruption", "warning"])
def test_volcano_existing_info_at_startup_is_not_notified(kind):
    c = _volcano_cog()
    c.session = FakeSession({f"/{kind}.json": [_item("E1", OLD)]})
    fetch = getattr(c, f"fetch_{kind}_info")
    run(fetch())
    run(fetch())  # 2回目も同じ → 通知なし
    getattr(c, f"_notify_{kind}").assert_not_awaited()


@pytest.mark.parametrize("kind", ["eruption", "warning"])
def test_volcano_alert_issued_after_start_is_notified_on_first_poll(kind):
    """起動直後の取得失敗で初期化が遅れた場合に相当（発表が起動後）。"""
    c = _volcano_cog()
    c.session = FakeSession({f"/{kind}.json": [_item("E1", NEW)]})
    run(getattr(c, f"fetch_{kind}_info")())
    getattr(c, f"_notify_{kind}").assert_awaited_once()


# ── 火山情報（info.json） ──
def _volcano_info_cog():
    from cogs.volcano import VolcanoCog
    c = VolcanoCog.__new__(VolcanoCog)
    c._last_volcano_info_map = {}
    c._volcano_info_initialized = False
    c._last_recv, c._recv_count = {}, {}
    c._last_volcano_recv_time = None
    c._volcano_recv_count = 0
    c._last_volcano_event_id = None
    c._notify_volcano = AsyncMock()
    return c


def test_volcano_info_first_after_empty_is_notified():
    c = _volcano_info_cog()
    payloads = [[], [{"eventId": "V1", "reportDatetime": OLD}]]
    c.session = FakeSession({"info.json": lambda: payloads.pop(0), "info/V1.json": {"x": 1}})
    run(c.fetch_volcano_info())
    c._notify_volcano.assert_not_awaited()
    run(c.fetch_volcano_info())
    c._notify_volcano.assert_awaited_once()


def test_volcano_info_existing_not_notified_but_post_start_is():
    c = _volcano_info_cog()
    c.session = FakeSession({"info.json": [
        {"eventId": "OLD1", "reportDatetime": OLD},
        {"eventId": "NEW1", "reportDatetime": NEW},
    ], "info/NEW1.json": {"x": 1}})
    run(c.fetch_volcano_info())
    assert c._notify_volcano.await_count == 1
    assert c._notify_volcano.await_args.args[1] == "NEW1"


# ── 長周期地震動（cogs/other.py） ──
def _other_cog():
    from cogs.other import OtherInfoCog
    c = OtherInfoCog.__new__(OtherInfoCog)
    c.last_long_period_id = None
    c._long_period_initialized = False
    c.notify_long_period = AsyncMock()
    return c


def test_long_period_first_after_empty_list_is_notified():
    from cogs.other import OtherInfoCog
    c = _other_cog()
    payloads = [[], [{"eid": "L1", "rdt": OLD}]]
    c.session = FakeSession({"ltpgm/data/list.json": lambda: payloads.pop(0)})
    run(OtherInfoCog.fetch_long_period.coro(c))
    c.notify_long_period.assert_not_awaited()
    run(OtherInfoCog.fetch_long_period.coro(c))
    c.notify_long_period.assert_awaited_once()


def test_long_period_existing_not_notified_post_start_is():
    from cogs.other import OtherInfoCog
    c = _other_cog()
    c.session = FakeSession({"ltpgm/data/list.json": [{"eid": "L1", "rdt": OLD}]})
    run(OtherInfoCog.fetch_long_period.coro(c))
    run(OtherInfoCog.fetch_long_period.coro(c))
    c.notify_long_period.assert_not_awaited()

    c2 = _other_cog()
    c2.session = FakeSession({"ltpgm/data/list.json": [{"eid": "L2", "rdt": NEW}]})
    run(OtherInfoCog.fetch_long_period.coro(c2))
    c2.notify_long_period.assert_awaited_once()


# ── 津波観測情報（cogs/tsunami.py） ──
def _tsunami_cog():
    from cogs.tsunami import TsunamiCog
    c = TsunamiCog.__new__(TsunamiCog)
    c._tsunami_observation_initialized = False
    c.last_tsunami_observation_id = None
    c.notify_tsunami_observation_coastal = AsyncMock()
    return c


@pytest.mark.parametrize("rdt,expect_notify", [(OLD, False), (NEW, True)])
def test_tsunami_observation_first_poll_uses_report_time(rdt, expect_notify):
    from cogs.tsunami import TsunamiCog
    c = _tsunami_cog()
    c.session = FakeSession({
        "tsunami/data/list.json": [{"ttl": "津波観測に関する情報", "eid": "T1", "rdt": rdt, "json": "x.json"}],
        "tsunami/data/x.json": {},
    })
    run(TsunamiCog.fetch_tsunami_observation.coro(c))
    assert c.notify_tsunami_observation_coastal.await_count == (1 if expect_notify else 0)
