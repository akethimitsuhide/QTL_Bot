"""
tests/test_usgs_dedupe.py
==========================
USGS地震情報の重複通知防止（cogs/usgs.py、2026-10-07修正）と、--test_quake の
入力JSON検証（core/test_runner.py）のテスト。

実機ログで、cooldown（既定300秒）がポーリング間隔（既定600秒）より短いため、
all_day フィード（過去24時間）に載っている同じ地震が10分ごとに再通知されていた。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import cogs.usgs as usgs  # noqa: E402


def _feature(eid, mag=6.0):
    return {"id": eid, "properties": {"mag": mag, "place": f"place {eid}"},
            "geometry": {"coordinates": [140.0, 35.0, 10.0]}}


class _Resp:
    def __init__(self, features):
        self.status = 200
        self._features = features

    async def json(self, content_type=None):
        return {"features": self._features}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self):
        self.features = []

    def get(self, url, **kw):
        return _Resp(self.features)


def _make(monkeypatch, cooldown=300):
    monkeypatch.setattr(usgs, "USGS_ENABLED", True)
    monkeypatch.setattr(usgs, "USGS_NOTIFICATION_COOLDOWN", cooldown)
    for k, v in (("USGS_REGION_LAT_MIN", -90), ("USGS_REGION_LAT_MAX", 90),
                 ("USGS_REGION_LON_MIN", -180), ("USGS_REGION_LON_MAX", 180)):
        monkeypatch.setattr(usgs, k, v)
    cog = usgs.UsgsCog(SimpleNamespace(is_closed=lambda: False))
    cog.session = _Session()
    cog.notified = []

    async def fake_notify(feature, extra_note=""):
        cog.notified.append(feature["id"])
    cog.notify_usgs_quake = fake_notify
    return cog


def _poll(cog):
    asyncio.run(cog.fetch_usgs_quake.coro(cog))


def _age(cog, seconds):
    cog.last_usgs_ids = {k: v - seconds for k, v in cog.last_usgs_ids.items()}


def test_same_quake_in_feed_is_not_renotified_after_cooldown(monkeypatch):
    cog = _make(monkeypatch, cooldown=300)
    cog.session.features = [_feature("a"), _feature("b")]
    _poll(cog)
    assert cog.notified == []                        # 初回ポーリングは記録のみ
    for _ in range(3):                               # 10分ごとのポーリングを想定（cooldown 300秒を超える）
        _age(cog, 600)
        _poll(cog)
    assert cog.notified == []                        # 以前はここで毎回 a, b が再通知されていた


def test_new_quake_is_notified_once(monkeypatch):
    cog = _make(monkeypatch)
    cog.session.features = [_feature("a")]
    _poll(cog)
    cog.session.features = [_feature("a"), _feature("new")]
    _age(cog, 600)
    _poll(cog)
    assert cog.notified == ["new"]
    _age(cog, 600)
    _poll(cog)
    assert cog.notified == ["new"]                   # 2回目以降は通知しない


def test_ids_that_left_the_feed_are_pruned_after_cooldown(monkeypatch):
    cog = _make(monkeypatch, cooldown=300)
    cog.session.features = [_feature("a"), _feature("b")]
    _poll(cog)
    cog.session.features = [_feature("b"), _feature("c")]   # a はフィードから消えた
    _age(cog, 600)
    _poll(cog)
    assert "a" not in cog.last_usgs_ids and "b" in cog.last_usgs_ids   # メモリが増え続けない
    assert cog.notified == ["c"]


def test_test_quake_validation_accepts_p2p_quake_format(capsys):
    from core.test_runner import validate_expected_fields
    p2p = {"issue": {"type": "DetailScale"}, "earthquake": {"maxScale": 45}, "points": []}
    validate_expected_fields("quake", p2p)
    assert "警告" not in capsys.readouterr().out
    validate_expected_fields("quake", {"EventID": "x", "Hypocenter": "y", "MaxIntensity": "4"})  # Wolfx形式は取り違え
    assert "警告" in capsys.readouterr().out
