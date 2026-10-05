"""
tests/test_apm_expected_errors.py
==================================
core/apm.py の _mark_expected_error（aiohttp計装の response_hook）のテスト
（2026-10-03追加）。想定内の TimeoutError / CancelledError はスパンをエラー扱いにせず、
HTTP 5xx・接続拒否は従来どおりエラーのままであることを、実際のaiohttp計装で確認する。
OpenTelemetry未インストールの環境ではスキップする。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("opentelemetry.instrumentation.aiohttp_client")

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from core.apm import _mark_expected_error, is_expected_error  # noqa: E402


def test_is_expected_error():
    assert is_expected_error(asyncio.TimeoutError())
    assert is_expected_error(TimeoutError())
    assert is_expected_error(asyncio.CancelledError())
    assert not is_expected_error(aiohttp.ClientConnectorError.__new__(aiohttp.ClientConnectorError))
    assert not is_expected_error(ValueError("x")) and not is_expected_error(None)


def test_hook_never_raises_on_broken_inputs():
    _mark_expected_error(object(), object())          # span/params が想定外でも例外を出さない
    _mark_expected_error(None, None)


def test_instrumented_requests_only_expected_errors_become_ok():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = AioHttpClientInstrumentor()
    instrumentor.instrument(tracer_provider=provider, response_hook=_mark_expected_error)

    async def slow(request):
        await asyncio.sleep(1)
        return web.Response(text="x")

    async def server_error(request):
        return web.Response(status=500, text="x")

    async def run():
        app = web.Application()
        app.router.add_get("/slow", slow)
        app.router.add_post("/slow", slow)
        app.router.add_get("/500", server_error)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession() as s:
                with pytest.raises(asyncio.TimeoutError):                       # 1) タイムアウト
                    async with s.get(f"{base}/slow", timeout=aiohttp.ClientTimeout(total=0.2)):
                        pass

                async def post():
                    async with s.post(f"{base}/slow"):
                        pass
                t = asyncio.create_task(post())                                  # 2) 送信中のキャンセル
                await asyncio.sleep(0.2)
                t.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await t

                async with s.get(f"{base}/500"):                                 # 3) HTTP 500
                    pass
                with pytest.raises(aiohttp.ClientConnectorError):                # 4) 接続拒否
                    async with s.get("http://127.0.0.1:1/x", timeout=aiohttp.ClientTimeout(total=2)):
                        pass
        finally:
            await runner.cleanup()

    try:
        asyncio.run(run())
        spans = exporter.get_finished_spans()
    finally:
        instrumentor.uninstrument()

    by_kind = {}
    for sp in spans:
        exc_types = [e.attributes.get("exception.type") for e in sp.events]
        by_kind[(sp.name, tuple(exc_types))] = sp
    timeout = next(sp for sp in spans if any("TimeoutError" in str(e.attributes.get("exception.type")) for e in sp.events))
    cancelled = next(sp for sp in spans if any("CancelledError" in str(e.attributes.get("exception.type")) for e in sp.events))
    http500 = next(sp for sp in spans if sp.attributes.get("http.status_code") == 500)
    refused = next(sp for sp in spans if any("ClientConnectorError" in str(e.attributes.get("exception.type")) for e in sp.events))
    assert timeout.status.status_code.name == "OK" and timeout.attributes["qtl.expected_error"] is True
    assert cancelled.status.status_code.name == "OK" and cancelled.attributes["qtl.expected_error"] is True
    assert http500.status.status_code.name == "ERROR"
    assert refused.status.status_code.name == "ERROR" and "qtl.expected_error" not in refused.attributes
    assert timeout.events and cancelled.events           # 例外の記録は残っている（発生自体はトレースで確認できる）
