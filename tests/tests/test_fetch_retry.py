"""
tests/test_fetch_retry.py
=========================
気象庁・USGS ポーリングの「取得/通知に失敗したときの再試行」まわりのテスト（2026-11追加）。

- 火山情報: 詳細取得（info/{eventId}.json）に失敗した eventId を、次の周期で再検知する
  （従来は常に最新の一覧を保存していたため、一時的な失敗で通知を取りこぼしていた）。
- 津波観測・予報: 最新項目の詳細取得に失敗したとき、より古い項目を先に通知せず、
  失敗した項目を上限付きで再試行する（恒久的な失敗で毎分リトライし続けない）。
- USGS: Circuit Breaker（連続失敗 → バックオフ）が実際に取得ループへ効くこと。
"""
import asyncio
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import aiohttp  # noqa: E402
import pytest  # noqa: E402

import cogs.tsunami as tsunami  # noqa: E402
import cogs.usgs as usgs  # noqa: E402
import cogs.volcano as volcano  # noqa: E402


class _Resp:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """URL の末尾に応じて応答を返す fake セッション。routes: {末尾の文字列: _Resp | 例外}"""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        for tail, resp in self.routes.items():
            if url.endswith(tail):
                if isinstance(resp, BaseException):
                    raise resp
                return resp
        raise AssertionError(f"想定外のURL: {url}")


BOT = SimpleNamespace(is_closed=lambda: False)


# ===============================
# 火山情報
# ===============================
def _volcano_cog(monkeypatch):
    cog = volcano.VolcanoCog(BOT)
    cog.session = _Session()
    cog._volcano_info_initialized = True       # 初回ポーリング済みとして、差分検知から検証する
    cog.notified = []

    async def fake_notify(detail, event_id):
        cog.notified.append(event_id)
    monkeypatch.setattr(cog, "_notify_volcano", fake_notify)
    return cog


def _volcano_poll(cog, items, detail_status=200):
    cog.session.routes = {
        "info.json": _Resp(200, items),
        "/e1.json": _Resp(detail_status, {"headTitle": "x"}),
    }
    asyncio.run(cog.fetch_volcano_info())


def _item(report="2026-11-01T00:00:00+09:00"):
    return {"eventId": "e1", "reportDatetime": report}


def test_volcano_new_event_is_retried_after_detail_failure(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    cog = _volcano_cog(monkeypatch)
    _volcano_poll(cog, [_item()], detail_status=500)
    assert cog.notified == []                          # 取得失敗 → 通知なし
    assert "e1" not in cog._last_volcano_info_map      # 記録せず、次回また新規として検知させる
    _volcano_poll(cog, [_item()], detail_status=200)
    assert cog.notified == ["e1"]                      # 次の周期で通知される
    _volcano_poll(cog, [_item()], detail_status=200)
    assert cog.notified == ["e1"]                      # 成功後は重複しない


def test_volcano_updated_event_is_retried_after_detail_failure(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    cog = _volcano_cog(monkeypatch)
    old = _item("2026-11-01T00:00:00+09:00")
    new = _item("2026-11-01T06:00:00+09:00")
    cog._last_volcano_info_map = {"e1": old}
    _volcano_poll(cog, [new], detail_status=503)
    assert cog.notified == []
    assert cog._last_volcano_info_map["e1"] == old     # 前回の項目に戻る → 更新として再検知される
    _volcano_poll(cog, [new], detail_status=200)
    assert cog.notified == ["e1"]


def test_volcano_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    cog = _volcano_cog(monkeypatch)
    for _ in range(volcano.VOLCANO_DETAIL_MAX_ATTEMPTS):
        _volcano_poll(cog, [_item()], detail_status=404)
    assert cog._last_volcano_info_map["e1"] == _item()  # 打ち切り: 記録して再試行を止める
    assert cog._volcano_detail_fail_counts == {}
    calls = len(cog.session.calls)
    _volcano_poll(cog, [_item()], detail_status=200)
    assert cog.notified == []                            # 打ち切った項目は再通知しない
    assert len(cog.session.calls) == calls + 1           # 一覧のみ取得し、詳細は取りに行かない


def test_volcano_network_error_on_detail_is_retried(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    cog = _volcano_cog(monkeypatch)
    cog.session.routes = {
        "info.json": _Resp(200, [_item()]),
        "/e1.json": aiohttp.ClientConnectionError("down"),
    }
    asyncio.run(cog.fetch_volcano_info())
    assert "e1" not in cog._last_volcano_info_map
    _volcano_poll(cog, [_item()], detail_status=200)
    assert cog.notified == ["e1"]


def test_volcano_notify_exception_is_not_retried(monkeypatch):
    """通知処理の例外は再試行しない（途中まで送信済みの重複通知を避ける。従来どおり）。"""
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    cog = _volcano_cog(monkeypatch)

    async def boom(detail, event_id):
        raise ValueError("通知処理の不具合")
    monkeypatch.setattr(cog, "_notify_volcano", boom)
    _volcano_poll(cog, [_item()], detail_status=200)
    assert cog._last_volcano_info_map["e1"] == _item()


async def _no_sleep(*a, **kw):
    return None


# ===============================
# 津波観測・予報
# ===============================
def _tsunami_cog():
    cog = tsunami.TsunamiCog(SimpleNamespace())
    cog.session = _Session()
    cog._tsunami_observation_initialized = True
    cog.notified = []

    async def fake_forecast(detail, list_item=None, is_test=False):
        cog.notified.append(list_item["eid"])
    cog.notify_tsunami_forecast = fake_forecast
    return cog


NEW = {"ttl": "津波警報", "eid": "e2", "rdt": "t2", "json": "new.json"}
OLD = {"ttl": "津波警報", "eid": "e1", "rdt": "t1", "json": "old.json"}


def _tsunami_poll(cog, new_status=200, old_status=200):
    cog.session.routes = {
        "list.json": _Resp(200, [NEW, OLD]),   # 新しい順
        "new.json": _Resp(new_status, {"d": "new"}),
        "old.json": _Resp(old_status, {"d": "old"}),
    }
    asyncio.run(tsunami.TsunamiCog.fetch_tsunami_observation.coro(cog))


def test_tsunami_detail_failure_does_not_notify_older_item_first():
    cog = _tsunami_cog()
    _tsunami_poll(cog, new_status=500)
    assert cog.notified == []                          # 古い項目を先に通知しない（順序の逆転防止）
    assert cog.last_tsunami_observation_id is None
    assert not any(u.endswith("old.json") for u in cog.session.calls)
    _tsunami_poll(cog, new_status=200)
    assert cog.notified == ["e2"]                      # 最新項目が再試行されて通知される
    assert cog.last_tsunami_observation_id == "e2_t2"


def test_tsunami_gives_up_after_max_attempts():
    cog = _tsunami_cog()
    for _ in range(tsunami.TSUNAMI_OBS_MAX_ATTEMPTS):
        _tsunami_poll(cog, new_status=404)
    assert cog.last_tsunami_observation_id == "e2_t2"   # 諦めて処理済み扱い
    calls = len(cog.session.calls)
    _tsunami_poll(cog, new_status=200)
    assert cog.notified == []
    assert len(cog.session.calls) == calls + 1          # 一覧のみ。詳細は取りに行かない


def test_tsunami_notify_exception_is_retried_with_limit():
    cog = _tsunami_cog()
    attempts = []

    async def boom(detail, list_item=None, is_test=False):
        attempts.append(list_item["eid"])
        raise ValueError("通知処理の不具合")
    cog.notify_tsunami_forecast = boom
    for _ in range(tsunami.TSUNAMI_OBS_MAX_ATTEMPTS + 2):
        _tsunami_poll(cog)
    assert len(attempts) == tsunami.TSUNAMI_OBS_MAX_ATTEMPTS   # 上限回数で止まる（無限に再試行しない）
    assert cog.last_tsunami_observation_id == "e2_t2"


def test_tsunami_network_error_on_detail_is_retried_with_limit():
    cog = _tsunami_cog()
    cog.session.routes = {
        "list.json": _Resp(200, [NEW, OLD]),
        "new.json": aiohttp.ClientConnectionError("down"),
    }
    for _ in range(tsunami.TSUNAMI_OBS_MAX_ATTEMPTS):
        asyncio.run(tsunami.TsunamiCog.fetch_tsunami_observation.coro(cog))
    assert cog.last_tsunami_observation_id == "e2_t2"
    assert not any(u.endswith("old.json") for u in cog.session.calls)


def test_tsunami_success_resets_failure_count():
    cog = _tsunami_cog()
    _tsunami_poll(cog, new_status=500)
    _tsunami_poll(cog, new_status=200)
    assert cog.notified == ["e2"]
    assert cog._obs_retry_key is None and cog._obs_retry_count == 0


# ===============================
# USGS Circuit Breaker
# ===============================
def _usgs_cog(monkeypatch):
    monkeypatch.setattr(usgs, "USGS_ENABLED", True)
    monkeypatch.setattr(usgs, "FETCH_FAILURE_THRESHOLD", 3)
    monkeypatch.setattr(usgs, "FETCH_BACKOFF_SECONDS", 60)
    cog = usgs.UsgsCog(BOT)
    cog.session = _Session()
    cog.session.routes = {"all_day.geojson": aiohttp.ClientConnectionError("down")}
    return cog


def _usgs_poll(cog):
    asyncio.run(cog.fetch_usgs_quake.coro(cog))


def test_usgs_failures_accumulate_across_polls_and_trigger_backoff(monkeypatch):
    cog = _usgs_cog(monkeypatch)
    for _ in range(3):
        _usgs_poll(cog)
    assert len(cog.session.calls) == 3
    assert cog._fetch_failures["usgs"] == 3             # 取得ループの先頭で判定しても失敗回数が戻らない
    _usgs_poll(cog)
    assert len(cog.session.calls) == 3                  # バックオフ中は取得しない


def test_usgs_resumes_after_backoff_expires(monkeypatch):
    cog = _usgs_cog(monkeypatch)
    for _ in range(3):
        _usgs_poll(cog)
    cog._fetch_backoff_until["usgs"] = time.monotonic() - 1   # 期限切れ
    _usgs_poll(cog)
    assert len(cog.session.calls) == 4                   # 取得を再開
    assert cog._fetch_failures["usgs"] == 1              # 失敗回数は期限切れで戻ってから数え直し


def test_usgs_success_resets_failures(monkeypatch):
    cog = _usgs_cog(monkeypatch)
    _usgs_poll(cog)
    _usgs_poll(cog)
    feature = {"id": "a", "properties": {"mag": 0.1, "place": "x"}, "geometry": {"coordinates": [0, 0, 0]}}
    cog.session.routes = {"all_day.geojson": _Resp(200, {"features": [feature]})}  # 空フィードは成功扱いにならない
    _usgs_poll(cog)
    cog.session.routes = {"all_day.geojson": aiohttp.ClientConnectionError("down")}
    _usgs_poll(cog)
    _usgs_poll(cog)
    assert len(cog.session.calls) == 5                   # 2回失敗 → 成功でリセット → 2回失敗: バックオフには入らない