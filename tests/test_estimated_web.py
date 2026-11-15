"""
tests/test_estimated_web.py
============================
ダッシュボードの推計震度分布図 API（core/estimated_web.py）のテスト。
実際に aiohttp サーバーを起動し、本番と同じセキュリティヘッダー middleware を通して確認する。
"""
import asyncio
import os
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core import estimated_intensity as ei  # noqa: E402
from core import estimated_web  # noqa: E402
from core.web_static import security_headers_middleware  # noqa: E402
from tests.test_estimated_intensity import RAW  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


class _FakeCog:
    def __init__(self, entries, png=PNG, delay=0.0, boom=False):
        self.entries, self.png, self.delay, self.boom = entries, png, delay, boom
        self.png_calls = []
        self.tile_calls = []

    async def get_estimated_entries(self):
        return self.entries

    async def get_estimated_tile(self, entry, mesh):
        self.tile_calls.append((entry.id, mesh))
        if self.boom:
            raise RuntimeError("secret internal detail")
        await asyncio.sleep(self.delay)
        return self.png

    async def get_estimated_png(self, entry):
        self.png_calls.append(entry.id)
        if self.boom:
            raise RuntimeError("secret internal detail")
        await asyncio.sleep(self.delay)
        return self.png


def _entries(n=3):
    return [ei.parse_entries([dict(RAW, url=f"2026110{i+1}0104_44{i}")])[0] for i in range(n)]


def _run(cog, map_enabled, coro_fn):
    async def go():
        app = web.Application(middlewares=[security_headers_middleware])
        estimated_web.register_routes(app, lambda: cog, map_enabled)
        async with TestClient(TestServer(app)) as client:
            return await coro_fn(client)
    return asyncio.run(go())


def test_list_returns_entries_and_security_headers():
    cog = _FakeCog(_entries(3))

    async def check(c):
        r = await c.get("/status/estimated_list")
        body = await r.json()
        assert r.status == 200 and body["count"] == 3 and body["map_enabled"] is True
        e = body["entries"][0]
        assert e["id"] == "202611010104_440" and e["epicenter"] == "静岡県西部" and e["has_map"] is True
        assert e["ranks"][0] == {"label": "5強", "count": 85} and e["occurred_at"] == "2026-11-08 01:04"
        assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Cache-Control"] == "no-store"
        small = await (await c.get("/status/estimated_list?limit=1")).json()
        assert small["count"] == 1
        assert (await c.get("/status/estimated_list?limit=abc")).status == 400
    _run(cog, True, check)


def test_list_503_when_cog_missing():
    async def check(c):
        assert (await c.get("/status/estimated_list")).status == 503
        assert (await c.get("/status/estimated_map?id=202611010104_440")).status == 503
    _run(None, True, check)


def test_map_returns_png_with_private_cache_and_headers():
    cog = _FakeCog(_entries(2))

    async def check(c):
        r = await c.get("/status/estimated_map?id=202611020104_441")
        assert r.status == 200 and r.content_type == "image/png" and await r.read() == PNG
        assert r.headers["Cache-Control"] == "private, max-age=3600"
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert cog.png_calls == ["202611020104_441"]
    _run(cog, True, check)


@pytest.mark.parametrize("bad", ["", "../../etc/passwd", "202611020104_441/../x", "%00", "a" * 300, "20261102_441"])
def test_map_rejects_malformed_ids_without_touching_the_cog(bad):
    cog = _FakeCog(_entries(2))

    async def check(c):
        r = await c.get("/status/estimated_map", params={"id": bad})
        assert r.status == 400
        assert cog.png_calls == []
    _run(cog, True, check)


def test_map_unknown_but_wellformed_id_is_404():
    cog = _FakeCog(_entries(2))

    async def check(c):
        assert (await c.get("/status/estimated_map?id=209901010000_999")).status == 404
        assert cog.png_calls == []
    _run(cog, True, check)


def test_map_disabled_is_404_and_does_not_render():
    cog = _FakeCog(_entries(1))

    async def check(c):
        r = await c.get("/status/estimated_map?id=202611010104_440")
        assert r.status == 404 and cog.png_calls == []
        assert (await (await c.get("/status/estimated_list")).json())["map_enabled"] is False
    _run(cog, False, check)


def test_map_unavailable_timeout_and_internal_error_do_not_leak_details(monkeypatch):
    async def check_none(c):
        assert (await c.get("/status/estimated_map?id=202611010104_440")).status == 503
    _run(_FakeCog(_entries(1), png=None), True, check_none)

    monkeypatch.setattr(estimated_web, "MAP_TIMEOUT_SECONDS", 0.05)

    async def check_timeout(c):
        assert (await c.get("/status/estimated_map?id=202611010104_440")).status == 504
    _run(_FakeCog(_entries(1), delay=1.0), True, check_timeout)

    async def check_boom(c):
        r = await c.get("/status/estimated_map?id=202611010104_440")
        text = await r.text()
        assert r.status == 500 and "secret internal detail" not in text
    _run(_FakeCog(_entries(1), boom=True), True, check_boom)


# ---------- 地図の部品（detail）とメッシュ画像の中継（tile）----------
def test_detail_returns_tiles_bounds_and_legend():
    cog = _FakeCog(_entries(2))

    async def check(c):
        r = await c.get("/status/estimated_detail?id=202611020104_441")
        d = await r.json()
        assert r.status == 200 and d["id"] == "202611020104_441" and (d["lat"], d["lon"]) == (34.93, 137.66)
        assert d["bounds"] == [[34.6, 136.57], [35.9, 138.24]]
        assert [t["mesh"] for t in d["tiles"]] == ["5137", "5138", "5236"]
        (lat0, lon0), (lat1, lon1) = d["tiles"][0]["bounds"]
        assert (lat0, lon0) == pytest.approx((34.0, 137.0)) and (lat1, lon1) == pytest.approx((34.0 + 40 / 60, 138.0))
        assert [x["label"] for x in d["legend"]] == ["4", "5弱", "5強", "6弱", "6強", "7"]
        assert d["legend"][0]["color"] == "#fae696" and d["legend"][5]["color"] == "#b40068"
        assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Content-Type-Options"] == "nosniff"
    _run(cog, True, check)


def test_detail_and_tile_reject_bad_input_without_fetching():
    cog = _FakeCog(_entries(2))

    async def check(c):
        assert (await c.get("/status/estimated_detail?id=../x")).status == 400
        assert (await c.get("/status/estimated_detail?id=209901010000_999")).status == 404
        base = "/status/estimated_tile?id=202611020104_441"
        assert (await c.get("/status/estimated_tile?id=../x&mesh=5137")).status == 400
        assert (await c.get(base)).status == 400                           # mesh なし
        for bad in ["../etc", "51370", "abcd", "5137/../1", ""]:
            assert (await c.get(base, params={"mesh": bad})).status == 400
        assert (await c.get(base + "&mesh=9999")).status == 404            # 形式は正しいが、その地震のメッシュではない
        assert cog.tile_calls == []                                        # どれも取得には進まない
    _run(cog, True, check)


def test_tile_returns_png_with_private_cache():
    cog = _FakeCog(_entries(2))

    async def check(c):
        r = await c.get("/status/estimated_tile?id=202611020104_441&mesh=5236")
        assert r.status == 200 and r.content_type == "image/png" and await r.read() == PNG
        assert r.headers["Cache-Control"] == "private, max-age=3600" and r.headers["X-Content-Type-Options"] == "nosniff"
        assert cog.tile_calls == [("202611020104_441", "5236")]
    _run(cog, True, check)


def test_tile_unavailable_timeout_and_error_do_not_leak(monkeypatch):
    async def check_none(c):
        assert (await c.get("/status/estimated_tile?id=202611010104_440&mesh=5137")).status == 503
    _run(_FakeCog(_entries(1), png=None), True, check_none)

    monkeypatch.setattr(estimated_web, "TILE_TIMEOUT_SECONDS", 0.05)

    async def check_timeout(c):
        assert (await c.get("/status/estimated_tile?id=202611010104_440&mesh=5137")).status == 504
    _run(_FakeCog(_entries(1), delay=1.0), True, check_timeout)

    async def check_boom(c):
        r = await c.get("/status/estimated_tile?id=202611010104_440&mesh=5137")
        assert r.status == 500 and "secret internal detail" not in await r.text()
    _run(_FakeCog(_entries(1), boom=True), True, check_boom)
