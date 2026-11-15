"""
tests/test_estimated_intensity.py
==================================
推計震度分布図（core/estimated_intensity.py と render_estimated_intensity_map、2026-11-14追加）のテスト。
一覧JSONの形は、気象庁HPの実データ（2026-11-08 静岡県西部 M5.7）を元にした合成データ。
"""
import asyncio
import io
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from PIL import Image  # noqa: E402

from core import estimated_intensity as ei  # noqa: E402
from core import gis_render as g  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

RAW = {
    "hypo": {"it": "2026-11-08T01:13:42", "at": "2026-11-08T01:04:00", "lat": 34.93, "lon": 137.66,
             "dep": 40, "mag": 5.7, "epi": "静岡県西部", "kun": 0, "maxi": 5.0},
    "comment": "震度５弱のところでは、物が倒れたり、ガラスが割れるなどの被害が発生している可能性があります。",
    "url": "202611080104_443",
    "rank_cnt": {"i9": 0, "i8": 0, "i7": 0, "i6": 85, "i5": 1920, "i4": 107281, "i3": 221968},
    "bounds": [[34.6, 136.57], [35.9, 138.24]],
    "mesh_num": ["5137", "5138", "5236"],
    "datum": 2,
}


def _png(color=(255, 230, 0, 255), size=(80, 80)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


# ---------- 解析 ----------
def test_parse_entries_reads_real_shape():
    (e,) = ei.parse_entries([RAW])
    assert e.id == "202611080104_443" and e.epicenter == "静岡県西部"
    assert (e.lat, e.lon, e.depth, e.magnitude) == (34.93, 137.66, 40.0, "5.7")
    assert e.meshes == ["5137", "5138", "5236"] and e.datum == 2
    assert e.bounds == ((34.6, 136.57), (35.9, 138.24))


def test_parse_entries_skips_malformed_and_unsafe():
    bad_id = dict(RAW, url="../../etc/passwd")
    bad_mesh = dict(RAW, mesh_num=["5137", "../x", "12"])
    out = ei.parse_entries([bad_id, "x", None, bad_mesh])
    assert [e.id for e in out] == [RAW["url"]] and out[0].meshes == ["5137"]
    assert ei.parse_entries({"a": 1}) == []


def test_rank_summary_orders_large_first_and_drops_zero():
    (e,) = ei.parse_entries([RAW])
    assert ei.rank_summary(e) == [("5強", 85), ("5弱", 1920), ("4", 107281)]


# ---------- メッシュの範囲 ----------
def test_mesh_bounds_modern_datum_has_no_correction():
    (lat0, lon0), (lat1, lon1) = ei.mesh_bounds("5137", 2)
    assert (lat0, lon0) == pytest.approx((34.0, 137.0))
    assert (lat1, lon1) == pytest.approx((34.0 + 40 / 60, 138.0))


def test_mesh_bounds_old_datum_is_shifted_slightly():
    new = ei.mesh_bounds("5337", 2)
    old = ei.mesh_bounds("5337", None)
    assert old[0][0] != new[0][0]
    # 日本測地系→世界測地系の補正は緯度で約0.005度、経度で約0.01度
    assert abs(old[0][0] - new[0][0]) < 0.01 and abs(old[0][1] - new[0][1]) < 0.02


def test_tile_url_is_fixed_host():
    (e,) = ei.parse_entries([RAW])
    assert ei.tile_url(e, "5236") == (
        "https://www.jma.go.jp/bosai/estimated_intensity_map/data/202611080104_443/5236.png")


# ---------- 取得（条件付きGET・サイズ上限） ----------
class _Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status, self._body, self.headers = status, body, headers or {}
        self.content = SimpleNamespace(iter_chunked=self._iter)

    async def _iter(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, resps):
        self.resps, self.calls = list(resps), []

    def get(self, url, headers=None):
        self.calls.append((url, headers or {}))
        return self.resps.pop(0)


def test_fetch_entries_uses_etag_and_handles_304():
    import json
    body = json.dumps([RAW]).encode()
    sess = _Session([_Resp(200, body, {"ETag": 'W/"abc"', "Last-Modified": "x"}), _Resp(304)])
    state = {}
    first = asyncio.run(ei.fetch_entries(sess, state))
    assert [e.id for e in first] == [RAW["url"]] and state["etag"] == 'W/"abc"'
    assert asyncio.run(ei.fetch_entries(sess, state)) is None
    assert sess.calls[1][1]["If-None-Match"] == 'W/"abc"'


def test_fetch_entries_rejects_oversize_and_http_error(monkeypatch):
    monkeypatch.setattr(ei, "MAX_LIST_BYTES", 10)
    with pytest.raises(Exception):
        asyncio.run(ei.fetch_entries(_Session([_Resp(200, b"x" * 100)]), {}))
    with pytest.raises(Exception):
        asyncio.run(ei.fetch_entries(_Session([_Resp(503)]), {}))


def test_fetch_tiles_returns_only_successful_ones():
    (e,) = ei.parse_entries([RAW])
    ok = _png()
    sess = _Session([_Resp(200, ok), _Resp(404), _Resp(200, ok)])
    tiles = asyncio.run(ei.fetch_tiles(sess, e))
    assert len(tiles) == 2 and set(tiles) <= {"5137", "5138", "5236"}


# ---------- 描画 ----------
class _FakeAreaSet:
    def get(self):
        ring = [(136.0, 34.0), (139.0, 34.0), (139.0, 36.0), (136.0, 36.0)]
        shape = SimpleNamespace(code="", name="", rings=[ring], bbox=g._rings_bbox([ring]))
        return {"区域": shape}


def _setup(monkeypatch):
    monkeypatch.setattr(g, "_local_areas", _FakeAreaSet())
    monkeypatch.setattr(g, "_ready", lambda: True)


def test_render_returns_png(monkeypatch):
    _setup(monkeypatch)
    tiles = [(ei.mesh_bounds("5137", 2), _png()), (ei.mesh_bounds("5236", 2), _png((255, 40, 0, 255)))]
    out = g.render_estimated_intensity_map(tiles, ((34.6, 136.57), (35.9, 138.24)), (137.66, 34.93), ei.LEGEND)
    assert out is not None and out.startswith(PNG_MAGIC)


def test_render_skips_broken_tiles_and_returns_none_if_nothing_pasted(monkeypatch):
    _setup(monkeypatch)
    good = (ei.mesh_bounds("5137", 2), _png())
    bad = (ei.mesh_bounds("5138", 2), b"not a png")
    assert g.render_estimated_intensity_map([bad, good], None, None) is not None
    assert g.render_estimated_intensity_map([bad], None, None) is None


def test_render_none_when_not_ready_or_no_tiles(monkeypatch):
    _setup(monkeypatch)
    assert g.render_estimated_intensity_map([], None, None) is None
    monkeypatch.setattr(g, "_ready", lambda: False)
    assert g.render_estimated_intensity_map([(ei.mesh_bounds("5137", 2), _png())], None, None) is None


# ---------- /qtl_estimated（過去分の選択・閲覧） ----------
def _entries(n=3):
    out = []
    for i in range(n):
        raw = dict(RAW, url=f"2026110{i+1}0104_44{i}",
                   hypo=dict(RAW["hypo"], at=f"2026-11-0{i+1}T01:04:00", epi=["静岡県西部", "千葉県北東部", "石川県能登地方"][i % 3]))
        out.extend(ei.parse_entries([raw]))
    return out


def _cog(entries):
    from cogs.other import OtherInfoCog
    cog = object.__new__(OtherInfoCog)
    cog._ei_entries = entries
    return OtherInfoCog, cog


class _User:
    def __init__(self, admin=False, roles=()):
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.roles = [SimpleNamespace(id=r) for r in roles]


def test_choice_label_format():
    cls, _ = _cog([])
    (e,) = ei.parse_entries([RAW])
    assert cls._ei_choice_label(e) == "2026-11-08 01:04 静岡県西部 M5.7 最大5強"


def test_autocomplete_filters_and_limits(monkeypatch):
    import cogs.other as other
    monkeypatch.setattr(other, "QTL_ESTIMATED_ACCESS", "all")
    cls, cog = _cog(_entries(3))
    inter = SimpleNamespace(user=_User())
    all_choices = asyncio.run(cls._qtl_estimated_autocomplete(cog, inter, ""))
    assert [c.value for c in all_choices] == [e.id for e in cog._ei_entries]
    only = asyncio.run(cls._qtl_estimated_autocomplete(cog, inter, "千葉"))
    assert len(only) == 1 and "千葉県北東部" in only[0].name
    cog._ei_entries = _entries(3) * 20                     # 60件でも候補は25件まで
    assert len(asyncio.run(cls._qtl_estimated_autocomplete(cog, inter, ""))) == 25


def test_autocomplete_empty_for_unauthorized(monkeypatch):
    import cogs.other as other
    monkeypatch.setattr(other, "QTL_ESTIMATED_ACCESS", "admin")
    cls, cog = _cog(_entries(2))
    assert asyncio.run(cls._qtl_estimated_autocomplete(cog, SimpleNamespace(user=_User()), "")) == []
    assert asyncio.run(cls._qtl_estimated_autocomplete(cog, SimpleNamespace(user=_User(admin=True)), ""))


def test_access_modes(monkeypatch):
    import cogs.other as other
    cls, cog = _cog([])
    monkeypatch.setattr(other, "QTL_ESTIMATED_ROLE_IDS", frozenset({7}))
    monkeypatch.setattr(other, "QTL_ESTIMATED_ACCESS", "role")
    assert cog._can_use_estimated_command(_User(roles=[7])) and not cog._can_use_estimated_command(_User(roles=[8]))
    assert cog._can_use_estimated_command(_User(admin=True))
    monkeypatch.setattr(other, "QTL_ESTIMATED_ACCESS", "admin")
    assert not cog._can_use_estimated_command(_User(roles=[7]))
    monkeypatch.setattr(other, "QTL_ESTIMATED_ACCESS", "all")
    assert cog._can_use_estimated_command(_User())


class _Inter:
    """/qtl_estimated の応答を記録するだけの Interaction もどき。"""
    def __init__(self, user):
        self.user = user
        self.sent = []
        self.response = SimpleNamespace(
            defer=self._defer, is_done=lambda: False, send_message=self._send_message)
        self.followup = SimpleNamespace(send=self._followup)

    async def _defer(self, **kw):
        self.sent.append(("defer", kw))

    async def _send_message(self, content=None, **kw):
        self.sent.append(("send", content, kw))

    async def _followup(self, content=None, **kw):
        self.sent.append(("followup", content, kw))


def test_command_unauthorized_unknown_and_success(monkeypatch):
    import discord
    import cogs.other as other
    cls, cog = _cog(_entries(2))
    cog._ei_command_lock = asyncio.Lock()
    callback = cls.slash_qtl_estimated.callback

    monkeypatch.setattr(other, "QTL_ESTIMATED_ACCESS", "admin")
    denied = _Inter(_User())
    asyncio.run(callback(cog, denied, event=cog._ei_entries[0].id))
    assert denied.sent[0][0] == "send" and "権限" in denied.sent[0][1] and denied.sent[0][2]["ephemeral"]

    monkeypatch.setattr(other, "QTL_ESTIMATED_ACCESS", "all")
    unknown = _Inter(_User())
    asyncio.run(callback(cog, unknown, event="202601010000_999"))
    assert unknown.sent[-1][0] == "followup" and "見つかりません" in unknown.sent[-1][1]

    async def fake_prepare(entry, final_attempt=True):
        assert final_attempt is True
        return discord.Embed(title="推計震度分布図"), []
    cog._prepare_estimated_intensity = fake_prepare
    ok = _Inter(_User())
    asyncio.run(callback(cog, ok, event=cog._ei_entries[1].id))
    kind, _content, kw = ok.sent[-1]
    assert kind == "followup" and kw["ephemeral"] is True and kw["embed"].title == "推計震度分布図"


# ---------- 地図生成の共通メソッド（通知・/qtl_estimated・ダッシュボード共用）----------
class _TileSession:
    """メッシュ画像の取得だけを再現するセッション（失敗させるメッシュを指定できる）。"""
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def get(self, url, headers=None):
        self.calls.append(url)
        mesh = url.rsplit("/", 1)[1].split(".")[0]
        return _Resp(404) if mesh in self.fail else _Resp(200, _png())


def _real_cog(monkeypatch, session):
    from collections import OrderedDict
    import cogs.other as other
    cls, cog = _cog([])
    cog.session = session
    cog._ei_image_cache = OrderedDict()
    cog._ei_command_lock = asyncio.Lock()
    monkeypatch.setattr(g, "_local_areas", _FakeAreaSet())
    monkeypatch.setattr(g, "_ready", lambda: True)
    monkeypatch.setattr(other, "render_estimated_intensity_map", g.render_estimated_intensity_map)
    return cog


def test_render_estimated_caches_complete_maps(monkeypatch):
    sess = _TileSession()
    cog = _real_cog(monkeypatch, sess)
    (entry,) = ei.parse_entries([RAW])

    async def go():
        first = await cog.get_estimated_png(entry)
        calls_after_first = len(sess.calls)
        second = await cog.get_estimated_png(entry)
        return first, second, calls_after_first
    first, second, n = asyncio.run(go())
    assert first.startswith(PNG_MAGIC) and second == first
    assert n == len(entry.meshes) and len(sess.calls) == n          # 2回目はメッシュ画像を取りにいかない


def test_render_estimated_cache_is_bounded(monkeypatch):
    cog = _real_cog(monkeypatch, _TileSession())
    cog._EI_IMAGE_CACHE_MAX = 2
    entries = [ei.parse_entries([dict(RAW, url=f"2026110{i}0104_44{i}")])[0] for i in range(1, 5)]

    async def go():
        for e in entries:
            await cog.get_estimated_png(e)
    asyncio.run(go())
    assert list(cog._ei_image_cache) == [entries[2].id, entries[3].id]


def test_render_estimated_incomplete_tiles_retry_or_partial(monkeypatch):
    sess = _TileSession(fail={"5236"})
    cog = _real_cog(monkeypatch, sess)
    (entry,) = ei.parse_entries([RAW])

    async def go():
        retry = await cog._render_estimated(entry, final_attempt=False)      # 欠けている間は再試行（None）
        final = await cog._render_estimated(entry, final_attempt=True)       # 最終回は取れた分だけ
        return retry, final
    retry, final = asyncio.run(go())
    assert retry is None
    image, partial = final
    assert image.startswith(PNG_MAGIC) and partial is True
    assert entry.id not in cog._ei_image_cache                               # 欠けた地図はキャッシュしない
