"""
core/estimated_intensity.py
============================
気象庁HP「推計震度分布図」（250mメッシュ）の一覧・メッシュ画像の取得と解析
（2026-11-14追加）。通知は cogs/other.py（OtherInfoCog）、地図への重ね描きは
core/gis_render.py（render_estimated_intensity_map）が担う。

【データの出どころ（気象庁HPが画面表示に使っている公開ファイル。HAR で確認）】
  一覧   : https://www.jma.go.jp/bosai/estimated_intensity_map/data/list.json
           （新しい順。1件 = 1地震。ETag / Last-Modified / max-age=60 付き。約250KB）
             hypo      : it=発表時刻 / at=発生時刻 / lat / lon / dep / mag / epi=震央地名 / maxi
             comment   : 解説文
             url       : データのフォルダ名（例 "202611080104_443" = 発生の年月日時分_震央地名コード）
             rank_cnt  : 推計震度ごとのメッシュ数 {"i9"…"i0"}（i4=震度4 … i9=震度7）
             bounds    : データのある範囲 [[南緯, 西経], [北緯, 東経]]
             mesh_num  : 画像のある1次メッシュコード（例 ["5137", "5138", …]）
             datum     : 2 なら世界測地系。無い（旧データ）場合は日本測地系→世界測地系の補正をかける
  メッシュ画像 : .../data/<url>/<メッシュコード>.png（800x800 の透過PNG。画像は半透明0.6で重ねて表示される）
  1次メッシュコード "AABB" の範囲: 緯度 AA/1.5 〜 +40分、経度 100+BB 〜 +1度
  （気象庁HPの表示ロジックと同じ。datum が 2 でないときは同じ式で測地系を補正する）。

【注意】気象庁が API として公開しているものではなく、HP の内部ファイルのため、
  告知なく仕様が変わる可能性がある。取得・解析に失敗しても通知側は再試行するだけで、
  他の機能には影響しない。応答の大きさ・URL の形式を検証し、固定ホストにしか接続しない。
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger("QTLBot")

BASE_URL = "https://www.jma.go.jp/bosai/estimated_intensity_map/data/"
LIST_URL = BASE_URL + "list.json"

MAX_LIST_BYTES = 2 * 1024 * 1024      # 一覧（実測 約250KB）の上限
MAX_TILE_BYTES = 1 * 1024 * 1024      # メッシュ画像1枚（実測 最大約100KB）の上限
MAX_TILES = 40                        # 1件で取得するメッシュ画像の上限（異常に多い場合は打ち切る）
_TILE_CONCURRENCY = 4

_ID_RE = re.compile(r"^\d{12}_[0-9A-Za-z]{1,8}$")
_MESH_RE = re.compile(r"^\d{4}$")

# 凡例（気象庁HPの images/int4.svg〜int9.svg の色）。(ラベル用の震度コード, RGB)
# 震度コードは core.constants.INT_MAP のキー（45=5弱 50=5強 55=6弱 60=6強 70=7）。
LEGEND = (
    (40, (250, 230, 150)),
    (45, (255, 230, 0)),
    (50, (255, 153, 0)),
    (55, (255, 40, 0)),
    (60, (165, 0, 33)),
    (70, (180, 0, 104)),
)
# rank_cnt のキー → 表示名
RANK_LABELS = (("i9", "7"), ("i8", "6強"), ("i7", "6弱"), ("i6", "5強"), ("i5", "5弱"), ("i4", "4"))


@dataclass
class Entry:
    """一覧の1件（1地震分の推計震度分布）。"""
    id: str                                   # url フィールド（データフォルダ名）
    reported_at: str = ""                     # hypo.it（発表時刻。タイムゾーン無し・JST）
    occurred_at: str = ""                     # hypo.at（発生時刻）
    lat: Optional[float] = None
    lon: Optional[float] = None
    depth: Optional[float] = None
    magnitude: str = ""
    epicenter: str = ""
    comment: str = ""
    rank_cnt: dict = field(default_factory=dict)
    meshes: list = field(default_factory=list)
    bounds: Optional[tuple] = None            # ((lat_sw, lon_sw), (lat_ne, lon_ne))
    datum: Optional[int] = None


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def is_valid_id(value) -> bool:
    """データID（一覧の url フィールド）の形式か。リクエストの入力を一覧と照合する前の形式チェックに使う。"""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def parse_entries(data) -> list:
    """一覧JSONを Entry のリスト（元の順＝新しい順）にする。形式に合わない件は読み飛ばす。"""
    if not isinstance(data, list):
        return []
    entries = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        eid = raw.get("url")
        if not isinstance(eid, str) or not _ID_RE.match(eid):
            continue
        hypo = raw.get("hypo") if isinstance(raw.get("hypo"), dict) else {}
        meshes = [m for m in (raw.get("mesh_num") or []) if isinstance(m, str) and _MESH_RE.match(m)]
        bounds = None
        b = raw.get("bounds")
        try:
            (a, c), (d, e) = b
            vals = [_num(a), _num(c), _num(d), _num(e)]
            if None not in vals:
                bounds = ((vals[0], vals[1]), (vals[2], vals[3]))
        except (TypeError, ValueError):
            pass
        rank = raw.get("rank_cnt") if isinstance(raw.get("rank_cnt"), dict) else {}
        mag = hypo.get("mag")
        entries.append(Entry(
            id=eid,
            reported_at=str(hypo.get("it") or ""),
            occurred_at=str(hypo.get("at") or ""),
            lat=_num(hypo.get("lat")), lon=_num(hypo.get("lon")), depth=_num(hypo.get("dep")),
            magnitude="" if mag in (None, "") else str(mag),
            epicenter=str(hypo.get("epi") or ""),
            comment=str(raw.get("comment") or ""),
            rank_cnt={k: int(v) for k, v in rank.items() if isinstance(v, (int, float))},
            meshes=meshes, bounds=bounds,
            datum=int(raw["datum"]) if isinstance(raw.get("datum"), (int, float)) else None,
        ))
    return entries


def mesh_bounds(code: str, datum: Optional[int]) -> tuple:
    """
    1次メッシュコード（"AABB"）の範囲 ((緯度_南, 経度_西), (緯度_北, 経度_東)) を返す。
    datum が 2（世界測地系）以外のときは、気象庁HPと同じ式で日本測地系→世界測地系の補正をかける。
    """
    lat = int(code[:2]) / 1.5
    lon = int(code[2:4]) + 100.0
    lat2 = lat + 40 / 60
    lon2 = lon + 1.0
    if datum != 2:
        lat = lat - 10695e-8 * lat + 17464e-9 * lon + 0.0046017
        lon = lon - 46038e-9 * lat - 83043e-9 * lon + 0.01004
        lat2 = lat2 - 10695e-8 * lat2 + 17464e-9 * lon2 + 0.0046017
        lon2 = lon2 - 46038e-9 * lat2 - 83043e-9 * lon2 + 0.01004
    return (lat, lon), (lat2, lon2)


def tile_url(entry: Entry, mesh: str) -> str:
    """メッシュ画像のURL。entry.id・mesh は parse_entries で形式検証済みのものだけを渡すこと。"""
    return f"{BASE_URL}{entry.id}/{mesh}.png"


def rank_summary(entry: Entry) -> list:
    """[(震度の表示名, メッシュ数), …]（震度の大きい順。メッシュ数が0の階級は除く）。"""
    return [(label, entry.rank_cnt[key]) for key, label in RANK_LABELS if entry.rank_cnt.get(key, 0) > 0]


async def _read_limited(resp: aiohttp.ClientResponse, limit: int) -> Optional[bytes]:
    """応答本文を limit バイトまで読む。超えたら None。"""
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        buf.extend(chunk)
        if len(buf) > limit:
            return None
    return bytes(buf)


async def fetch_entries(session: aiohttp.ClientSession, state: dict) -> Optional[list]:
    """
    一覧を取得して Entry のリストを返す。前回から変化なし（304）の場合は None。
    state（呼び出し側が保持する dict）に ETag / Last-Modified を覚え、条件付きGETで通信量を抑える。
    HTTP エラー・形式不正は例外（呼び出し側が WARNING にして次の周期で再試行する）。
    """
    headers = {"Accept-Encoding": "gzip, deflate"}
    if state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    elif state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]
    async with session.get(LIST_URL, headers=headers) as resp:
        if resp.status == 304:
            return None
        if resp.status != 200:
            raise aiohttp.ClientError(f"推計震度分布の一覧の取得に失敗しました（HTTP {resp.status}）")
        body = await _read_limited(resp, MAX_LIST_BYTES)
        if body is None:
            raise aiohttp.ClientError("推計震度分布の一覧が想定より大きいため破棄しました")
        etag, last_mod = resp.headers.get("ETag"), resp.headers.get("Last-Modified")
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("推計震度分布の一覧が配列ではありません")
    state["etag"], state["last_modified"] = etag, last_mod
    return parse_entries(data)


async def fetch_tiles(session: aiohttp.ClientSession, entry: Entry) -> dict:
    """
    entry のメッシュ画像を取得し、{メッシュコード: PNGバイト列} を返す（取得できたものだけ）。
    PNGとして正しいか・大きさは描画側（Pillow）で検証する。
    """
    meshes = entry.meshes[:MAX_TILES]
    sem = asyncio.Semaphore(_TILE_CONCURRENCY)
    out: dict = {}

    async def one(mesh: str) -> None:
        async with sem:
            try:
                async with session.get(tile_url(entry, mesh), headers={"Accept-Encoding": "identity"}) as resp:
                    if resp.status != 200:
                        logger.warning(f"推計震度分布: メッシュ画像 {entry.id}/{mesh} の取得に失敗（HTTP {resp.status}）")
                        return
                    body = await _read_limited(resp, MAX_TILE_BYTES)
                    if body is None:
                        logger.warning(f"推計震度分布: メッシュ画像 {entry.id}/{mesh} が想定より大きいため破棄しました")
                        return
                    out[mesh] = body
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.warning(f"推計震度分布: メッシュ画像 {entry.id}/{mesh} の取得エラー: {type(e).__name__}: {e}")

    await asyncio.gather(*(one(m) for m in meshes))
    return out
