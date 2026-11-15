"""
core/estimated_web.py - ダッシュボードの推計震度分布図 API（2026-11-15追加）

  GET /status/estimated_list           一覧（新しい順）。JSON
  GET /status/estimated_detail?id=<ID> 1件分の地図の部品（メッシュの範囲・震源・凡例）。JSON
  GET /status/estimated_tile?id=<ID>&mesh=<メッシュ>  メッシュ画像1枚（PNG。ブラウザの地図に重ねる）
  GET /status/estimated_map?id=<ID>    指定した1件の地図（PNG。Discord 通知と同じ、サーバーで描いた1枚絵）

一覧・地図の取得は OtherInfoCog（cogs/other.py）が行う（Discord の /qtl_estimated と同じ処理・同じ
キャッシュ・同じロックを共用する）。このモジュールは HTTP の入口だけを担当し、次を守る。
  - 認証・IP許可リスト・セキュリティヘッダーは、他の /status/* と同じ middleware が適用する
    （このモジュールは認証を迂回する経路を持たない）。
  - 地図は「一覧にあるデータIDとの完全一致」でしか作らない（入力から URL・ファイルパスを組み立てない）。
  - 作成は同時に1件・90秒で打ち切り。失敗時は内部の例外内容を返さず、決まった短いエラーだけを返す。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from aiohttp import web

from core.estimated_intensity import LEGEND, LEGEND_LABELS, is_valid_id, is_valid_mesh, mesh_bounds, rank_summary

logger = logging.getLogger("QTLBot")

LIST_MAX = 400                 # 一覧で返す最大件数（気象庁の一覧は約340件）
MAP_TIMEOUT_SECONDS = 90       # メッシュ画像の取得＋描画の上限
TILE_TIMEOUT_SECONDS = 30      # メッシュ画像1枚の取得の上限


def register_routes(app: web.Application, get_cog: Callable[[], Optional[object]], map_enabled: bool) -> None:
    """
    app に2つの GET ルートを登録する。
    get_cog    : OtherInfoCog を返す関数（未ロードなら None）。get_estimated_entries() /
                 get_estimated_png(entry) を持つこと。
    map_enabled: GIS_MAP_ENABLE。False のとき地図 API は 404（一覧は返す）。
    """

    async def list_handler(request: web.Request) -> web.StreamResponse:
        cog = get_cog()
        if cog is None:
            return web.json_response({"error": "OtherInfoCog is not loaded"}, status=503)
        try:
            limit = int(request.query.get("limit", "200"))
        except ValueError:
            return web.json_response({"error": "limit must be an integer"}, status=400)
        limit = max(1, min(limit, LIST_MAX))
        entries = (await cog.get_estimated_entries())[:limit]
        return web.json_response({
            "map_enabled": bool(map_enabled),
            "count": len(entries),
            "entries": [
                {
                    "id": e.id,
                    "occurred_at": e.occurred_at.replace("T", " ")[:16],
                    "epicenter": e.epicenter,
                    "magnitude": e.magnitude,
                    "depth": e.depth,
                    "ranks": [{"label": lab, "count": cnt} for lab, cnt in rank_summary(e)],
                    "comment": e.comment,
                    "has_map": bool(e.meshes),
                }
                for e in entries
            ],
        }, headers={"Cache-Control": "no-store"})

    async def _find_entry(request: web.Request):
        """id を検証して一覧の1件を返す。(entry, エラー応答) のどちらか。入力から URL・パスは組み立てない。"""
        cog = get_cog()
        if cog is None:
            return None, web.json_response({"error": "OtherInfoCog is not loaded"}, status=503)
        event_id = request.query.get("id", "")
        if not is_valid_id(event_id):
            return None, web.json_response({"error": "invalid id"}, status=400)
        entry = next((e for e in await cog.get_estimated_entries() if e.id == event_id), None)
        if entry is None:
            return None, web.json_response({"error": "not found"}, status=404)
        return (cog, entry), None

    async def detail_handler(request: web.Request) -> web.StreamResponse:
        found, error = await _find_entry(request)
        if error is not None:
            return error
        _cog, entry = found
        tiles = [
            {"mesh": m, "bounds": [list(corner) for corner in mesh_bounds(m, entry.datum)]}
            for m in entry.meshes
        ]
        return web.json_response({
            "id": entry.id,
            "lat": entry.lat, "lon": entry.lon, "depth": entry.depth,
            "bounds": [list(corner) for corner in entry.bounds] if entry.bounds else None,
            "tiles": tiles,
            "legend": [{"label": lab, "color": "#%02x%02x%02x" % rgb} for lab, (_code, rgb) in zip(LEGEND_LABELS, LEGEND)],
        }, headers={"Cache-Control": "no-store"})

    async def tile_handler(request: web.Request) -> web.StreamResponse:
        found, error = await _find_entry(request)
        if error is not None:
            return error
        cog, entry = found
        mesh = request.query.get("mesh", "")
        if not is_valid_mesh(mesh):
            return web.json_response({"error": "invalid mesh"}, status=400)
        if mesh not in entry.meshes:
            return web.json_response({"error": "not found"}, status=404)
        try:
            png = await asyncio.wait_for(cog.get_estimated_tile(entry, mesh), timeout=TILE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return web.json_response({"error": "timeout"}, status=504)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"/status/estimated_tile エラー: {type(e).__name__}: {e}")
            return web.json_response({"error": "failed to fetch tile"}, status=500)
        if not png:
            return web.json_response({"error": "tile unavailable"}, status=503)
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    async def map_handler(request: web.Request) -> web.StreamResponse:
        if not map_enabled:
            return web.json_response({"error": "GIS_MAP_ENABLE is false"}, status=404)
        cog = get_cog()
        if cog is None:
            return web.json_response({"error": "OtherInfoCog is not loaded"}, status=503)
        event_id = request.query.get("id", "")
        if not is_valid_id(event_id):
            return web.json_response({"error": "invalid id"}, status=400)
        entry = next((e for e in await cog.get_estimated_entries() if e.id == event_id), None)
        if entry is None:
            return web.json_response({"error": "not found"}, status=404)
        try:
            png = await asyncio.wait_for(cog.get_estimated_png(entry), timeout=MAP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return web.json_response({"error": "timeout"}, status=504)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"/status/estimated_map エラー: {type(e).__name__}: {e}")
            return web.json_response({"error": "failed to build map"}, status=500)
        if not png:
            return web.json_response({"error": "map unavailable"}, status=503)
        # データは確定後に変わらないため、認証済み本人のブラウザに1時間キャッシュさせる（private）
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    app.router.add_get("/status/estimated_list", list_handler)
    app.router.add_get("/status/estimated_detail", detail_handler)
    app.router.add_get("/status/estimated_tile", tile_handler)
    app.router.add_get("/status/estimated_map", map_handler)
