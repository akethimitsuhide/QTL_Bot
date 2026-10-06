"""
tests/test_quake_failover.py
=============================
P2P WebSocket 障害時の代替取得（core/quake_failover.py, core/p2p_ws_hub.py）の
単体テスト。

【注意】JMA JSON/XML の固定データは、公開仕様（list.json の項目名、詳細JSONの
Head/Body/Earthquake/Intensity 構造）から作った合成データであり、気象庁の
実データそのものではない。実データでの確認は別途行うこと。
"""
import asyncio
import json
import os
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core.quake_failover import (  # noqa: E402
    JmaJsonSource, JmaXmlSource, QuakeContentDedupe, QuakeFailoverController,
    SourceItem, _xml_to_dict, fingerprint_from_p2p, fingerprints_match,
    jma_quake_to_p2p, parse_iso6709_coordinate,
    QuakeOneSource, quake_one_to_p2p, MAX_DETAIL_ATTEMPTS,
)
from core.p2p_ws_hub import P2PWebSocketHub  # noqa: E402


# ---------- 合成データ ----------
def _head(title, serial="1"):
    return {"Title": title, "ReportDateTime": "2026-10-01T21:35:00+09:00",
            "EventID": "20261001213100", "InfoType": "発表", "Serial": serial}


SHINDO_SOKUHO = {  # 震度速報（震源なし・区域別震度）
    "Control": {"Title": "震度速報"}, "Head": _head("震度速報"),
    "Body": {"Earthquake": [{"OriginTime": "2026-10-01T21:31:00+09:00",
                             "ArrivalTime": "2026-10-01T21:31:00+09:00"}],
             "Intensity": {"Observation": {"MaxInt": "4", "Pref": [
                 {"Name": "千葉県", "Code": "12", "MaxInt": "4", "Area": [
                     {"Name": "千葉県北東部", "Code": "230", "MaxInt": "4"},
                     {"Name": "千葉県南部", "Code": "231", "MaxInt": "2"}]},
                 {"Name": "茨城県", "Code": "08", "MaxInt": "3", "Area": [
                     {"Name": "茨城県南部", "Code": "221", "MaxInt": "3"}]}]}},
             "Comments": {"ForecastComment": {"Text": "この地震による津波の心配はありません。"}}},
}

SHINGEN = {  # 震源に関する情報（震源あり・震度なし）
    "Control": {"Title": "震源に関する情報"}, "Head": _head("震源に関する情報"),
    "Body": {"Earthquake": [{"OriginTime": "2026-10-01T21:31:00+09:00",
                             "Hypocenter": {"Area": {"Name": "千葉県北東部", "Code": "230",
                                                     "Coordinate": "+35.7+140.7-50000/"}},
                             "Magnitude": "5.4"}],
             "Comments": {"ForecastComment": {"Text": "この地震による津波の心配はありません。"}}},
}

SHINGEN_SHINDO = {  # 震源・震度に関する情報（観測点別震度）
    "Control": {"Title": "震源・震度に関する情報"}, "Head": _head("震源・震度に関する情報"),
    "Body": {"Earthquake": [{"OriginTime": "2026-10-01T21:31:00+09:00",
                             "Hypocenter": {"Area": {"Name": "千葉県北東部", "Code": "230",
                                                     "Coordinate": "+35.7+140.7-50000/"}},
                             "Magnitude": "5.4"}],
             "Intensity": {"Observation": {"MaxInt": "4", "Pref": [
                 {"Name": "千葉県", "Code": "12", "MaxInt": "4", "Area": [
                     {"Name": "千葉県北東部", "Code": "230", "MaxInt": "4", "City": [
                         {"Name": "香取市", "Code": "1223", "MaxInt": "4", "IntensityStation": [
                             {"Name": "香取市佐原諏訪台", "Code": "1", "Int": "4"},
                             {"Name": "香取市役所", "Code": "2", "Int": "3"}]}]}]},
                 {"Name": "茨城県", "Code": "08", "MaxInt": "3", "Area": [
                     {"Name": "茨城県南部", "Code": "221", "MaxInt": "3", "City": [
                         {"Name": "取手市", "Code": "0821", "MaxInt": "3", "IntensityStation": [
                             {"Name": "取手市寺田", "Code": "3", "Int": "3"}]}]}]}]}},
             "Comments": {"ForecastComment": {"Text": "この地震による津波の心配はありません。"}}},
}


# ---------- 変換 ----------
def test_parse_coordinate():
    assert parse_iso6709_coordinate("+35.7+140.7-50000/") == (35.7, 140.7, 50)
    assert parse_iso6709_coordinate("+23.8+121.7+0/") == (23.8, 121.7, 0)
    assert parse_iso6709_coordinate("+35.7+140.7/") == (35.7, 140.7, -1)
    assert parse_iso6709_coordinate("") == (-200.0, -200.0, -1)
    assert parse_iso6709_coordinate(None) == (-200.0, -200.0, -1)


def test_convert_scale_prompt():
    d = jma_quake_to_p2p(SHINDO_SOKUHO, item_key="a.json")
    assert d["issue"]["type"] == "ScalePrompt" and d["issue"]["source"] == "気象庁"
    assert d["earthquake"]["maxScale"] == 40
    assert d["earthquake"]["time"] == "2026/10/01 21:31:00"
    assert d["earthquake"]["hypocenter"]["name"] == "" and d["earthquake"]["hypocenter"]["magnitude"] == -1.0
    assert d["earthquake"]["domesticTsunami"] == "None"
    assert {(p["pref"], p["addr"], p["scale"], p["isArea"]) for p in d["points"]} == {
        ("千葉県", "千葉県北東部", 40, True), ("千葉県", "千葉県南部", 20, True),
        ("茨城県", "茨城県南部", 30, True)}


def test_convert_destination_and_detail():
    d = jma_quake_to_p2p(SHINGEN)
    assert d["issue"]["type"] == "Destination" and d["points"] == []
    h = d["earthquake"]["hypocenter"]
    assert (h["name"], h["magnitude"], h["depth"], h["latitude"], h["longitude"]) == ("千葉県北東部", 5.4, 50, 35.7, 140.7)
    assert d["earthquake"]["maxScale"] == -1
    d = jma_quake_to_p2p(SHINGEN_SHINDO)
    assert d["issue"]["type"] == "DetailScale"
    assert all(not p["isArea"] for p in d["points"]) and len(d["points"]) == 3


def test_convert_scale_notation_and_unknown_tsunami():
    d = jma_quake_to_p2p({"Head": _head("震度速報"), "Body": {"Intensity": {"Observation": {"MaxInt": "5-", "Pref": [
        {"Name": "A県", "Area": {"Name": "A県中部", "MaxInt": "5-"}},      # 単一要素（list でない）
        {"Name": "B県", "Area": [{"Name": "B県北部", "MaxInt": "!5-"}]}]}}}})
    assert d["earthquake"]["maxScale"] == 45
    assert sorted(p["scale"] for p in d["points"]) == [45, 46]
    assert d["earthquake"]["domesticTsunami"] == "Unknown"    # 断定しない


def test_convert_rejects_cancel_and_empty():
    assert jma_quake_to_p2p({"Head": {**_head("震度速報"), "InfoType": "取消"}, "Body": {}}) is None
    assert jma_quake_to_p2p({"Head": _head("震度速報"), "Body": {}}) is None
    assert jma_quake_to_p2p(None) is None and jma_quake_to_p2p({}) is None
    assert jma_quake_to_p2p({"Head": _head("震源に関する情報"), "Body": {"Earthquake": None, "Intensity": None}}) is None


def test_xml_converts_to_same_result_as_json():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Report xmlns="http://xml.kishou.go.jp/jmaxml1/" xmlns:jmx_eb="http://xml.kishou.go.jp/jmaxml1/elementBasis1/">
 <Control><Title>震源に関する情報</Title></Control>
 <Head xmlns="http://xml.kishou.go.jp/jmaxml1/informationBasis1/">
  <Title>震源に関する情報</Title><ReportDateTime>2026-10-01T21:35:00+09:00</ReportDateTime>
  <EventID>20261001213100</EventID><InfoType>発表</InfoType><Serial>1</Serial></Head>
 <Body xmlns="http://xml.kishou.go.jp/jmaxml1/body/seismology1/">
  <Earthquake><OriginTime>2026-10-01T21:31:00+09:00</OriginTime>
   <Hypocenter><Area><Name>千葉県北東部</Name><Code type="震央地名">230</Code>
    <jmx_eb:Coordinate description="x">+35.7+140.7-50000/</jmx_eb:Coordinate></Area></Hypocenter>
   <jmx_eb:Magnitude type="Mj" description="M5.4">5.4</jmx_eb:Magnitude></Earthquake>
  <Comments><ForecastComment><Text>この地震による津波の心配はありません。</Text></ForecastComment></Comments>
 </Body></Report>"""
    from_xml = jma_quake_to_p2p(_xml_to_dict(ET.fromstring(xml)))
    from_json = jma_quake_to_p2p(SHINGEN)
    assert from_xml["earthquake"] == from_json["earthquake"]
    assert fingerprints_match(fingerprint_from_p2p(from_xml), fingerprint_from_p2p(from_json))


# ---------- 内容による重複判定 ----------
def _p2p_like(kind):
    """P2P地震情報側が配信しうる形（取得元が違っても内容は同じ）。"""
    if kind == "scale":
        return {"earthquake": {"time": "2026/10/01 21:30:00", "maxScale": 40, "hypocenter": {"name": "", "magnitude": -1, "depth": -1}},
                "points": [{"pref": "千葉県", "addr": "千葉県北東部", "scale": 40}, {"pref": "千葉県", "addr": "千葉県南部", "scale": 20},
                           {"pref": "茨城県", "addr": "茨城県南部", "scale": 30}]}
    if kind == "dest":
        return {"earthquake": {"time": "2026/10/01 21:31", "maxScale": -1, "hypocenter": {"name": "千葉県北東部", "magnitude": 5.4, "depth": 50}}, "points": []}
    if kind == "detail":   # 観測点名の表記がJMAと違っても、都道府県別最大震度が同じなら同一
        return {"earthquake": {"time": "2026/10/01 21:31:00", "maxScale": 40, "hypocenter": {"name": "千葉県北東部", "magnitude": 5.4, "depth": 50}},
                "points": [{"pref": "千葉県", "addr": "千葉香取市佐原", "scale": 40}, {"pref": "茨城県", "addr": "茨城取手市寺田", "scale": 30}]}


def test_same_content_across_sources_is_duplicate():
    fp = fingerprint_from_p2p
    assert fingerprints_match(fp(_p2p_like("scale")), fp(jma_quake_to_p2p(SHINDO_SOKUHO)))   # 発生時刻が1分ずれても同一
    assert fingerprints_match(fp(_p2p_like("dest")), fp(jma_quake_to_p2p(SHINGEN)))
    assert fingerprints_match(fp(_p2p_like("detail")), fp(jma_quake_to_p2p(SHINGEN_SHINDO)))


def test_different_content_is_not_duplicate():
    fp = fingerprint_from_p2p
    sok, shi, det = fp(jma_quake_to_p2p(SHINDO_SOKUHO)), fp(jma_quake_to_p2p(SHINGEN)), fp(jma_quake_to_p2p(SHINGEN_SHINDO))
    assert not fingerprints_match(sok, shi) and not fingerprints_match(shi, det) and not fingerprints_match(sok, det)
    # 内容が更新（震度分布・規模の変化）された情報は別物
    upd = jma_quake_to_p2p(SHINGEN_SHINDO)
    upd["points"].append({"pref": "東京都", "addr": "x", "isArea": False, "scale": 30})
    assert not fingerprints_match(det, fp(upd))
    upd2 = jma_quake_to_p2p(SHINGEN)
    upd2["earthquake"]["hypocenter"]["magnitude"] = 5.6
    assert not fingerprints_match(shi, fp(upd2))
    # 別の地震（時刻が離れている）
    other = jma_quake_to_p2p(SHINGEN)
    other["earthquake"]["time"] = "2026/10/01 22:10:00"
    assert not fingerprints_match(shi, fp(other))


def test_pref_missing_falls_back_to_max_scale_only():
    a = fingerprint_from_p2p({"earthquake": {"time": "2026/10/01 21:31", "maxScale": 40, "hypocenter": {"name": "X", "magnitude": 5.0, "depth": 10}},
                              "points": [{"pref": "", "addr": "a", "scale": 40}]})
    b = fingerprint_from_p2p({"earthquake": {"time": "2026/10/01 21:31", "maxScale": 40, "hypocenter": {"name": "X", "magnitude": 5.0, "depth": 10}},
                              "points": [{"pref": "千葉県", "addr": "a", "scale": 40}]})
    assert fingerprints_match(a, b)


def test_dedupe_store_ttl_and_size():
    st = QuakeContentDedupe(max_items=2, ttl_sec=100)
    fps = [fingerprint_from_p2p({"earthquake": {"time": f"2026/10/01 {h}:00", "maxScale": 10, "hypocenter": {"name": f"N{h}", "magnitude": 4, "depth": 10}}, "points": []})
           for h in (10, 11, 12)]
    st.record(fps[0], 0); st.record(fps[1], 1); st.record(fps[2], 2)
    assert not st.is_duplicate(fps[0], 3)            # 件数上限で追い出された
    assert st.is_duplicate(fps[2], 3)
    assert not st.is_duplicate(fps[2], 1000)         # TTL切れ


# ---------- ハブの切り替え判定 ----------
def test_hub_failover_after_threshold_and_recovery():
    async def run():
        hub = P2PWebSocketHub(lambda: False, failover_threshold=5, recovery_seconds=0.05, disconnect_seconds=1000)
        events = []
        hub.add_failover_listener(events.append)
        for i in range(4):
            hub._on_ws_failure()
        assert not hub.failover_active and events == []
        hub._on_ws_failure()                        # 5回目
        assert hub.failover_active and events == [True]
        hub._on_ws_failure()
        assert events == [True]                     # 重複通知しない
        hub._on_ws_connect()                        # 接続（まだ安定とはみなさない）
        assert hub.failover_active
        await asyncio.sleep(0.1)                    # 安定
        assert not hub.failover_active and events == [True, False]
        assert hub.get_stats()["failover"]["consecutive_failures"] == 0
    asyncio.run(run())


def test_hub_flapping_connection_is_not_stable():
    async def run():
        hub = P2PWebSocketHub(lambda: False, failover_threshold=3, recovery_seconds=0.2, disconnect_seconds=1000)
        events = []
        hub.add_failover_listener(events.append)
        for _ in range(3):                          # 接続してもすぐ切れることを繰り返す
            hub._on_ws_connect()
            await asyncio.sleep(0.02)
            hub._on_ws_failure()
        await asyncio.sleep(0.3)
        assert hub.failover_active and events == [True]
    asyncio.run(run())


def test_hub_listener_exception_does_not_break_hub():
    hub = P2PWebSocketHub(lambda: False, failover_threshold=1, disconnect_seconds=1000)
    hub.add_failover_listener(lambda a: 1 / 0)
    got = []
    hub.add_failover_listener(got.append)
    hub._on_ws_failure()
    assert got == [True]


# ---------- コントローラ ----------
class FakeSource:
    name, label = "fake", "テスト取得元"

    def __init__(self, items, details, fail=False):
        self.items, self.details, self.fail = items, details, fail
        self.detail_calls = []

    async def list_items(self, session):
        if self.fail:
            raise RuntimeError("down")
        return list(self.items)

    async def fetch_detail(self, session, item):
        self.detail_calls.append(item.key)
        return self.details[item.key]


def _item(key, minute):
    from datetime import datetime
    from core.quake_failover import JST
    return SourceItem(key, "x", datetime(2026, 10, 1, 21, minute, tzinfo=JST), "u")


def test_controller_baseline_excludes_existing_and_notifies_new_in_order():
    async def run():
        got = []

        async def handler(data, label):
            got.append((data["id"], label))
        old = _item("old", 0)
        src = FakeSource([old], {})
        c = QuakeFailoverController(lambda: object(), handler, [src], 1, lambda: False, catchup_minutes=10**7)
        await c.baseline()
        assert await c.poll_once() == 0 and src.detail_calls == []        # 既存は通知しない
        src.items = [_item("c", 33), _item("b", 32), _item("a", 31), old]  # 新着3件（新しい順に並んでいる）
        src.details = {k: {"id": k} for k in "abc"}
        assert await c.poll_once() == 3
        assert [g[0] for g in got] == ["a", "b", "c"]                       # 発表順
        assert await c.poll_once() == 0                                     # 二重処理しない
    asyncio.run(run())


def test_controller_source_failover_chain_and_detail_retry():
    async def run():
        got = []

        async def handler(data, label):
            got.append((data["id"], label))
        bad = FakeSource([], {}, fail=True)
        bad.name = "bad"
        good = FakeSource([_item("n", 40)], {"n": {"id": "n"}})
        c = QuakeFailoverController(lambda: object(), handler, [bad, good], 1, lambda: False, catchup_minutes=10**7)
        c._baseline_done = True
        assert await c.poll_once() == 1 and got == [("n", "テスト取得元")]  # 1つ目が落ちても2つ目へ

        flaky = FakeSource([_item("r", 41)], {})
        c2 = QuakeFailoverController(lambda: object(), handler, [flaky], 1, lambda: False, catchup_minutes=10**7)
        c2._baseline_done = True
        assert await c2.poll_once() == 0                                    # 詳細取得失敗(KeyError)
        flaky.details = {"r": {"id": "r"}}
        assert await c2.poll_once() == 1                                    # 次回再試行で通知される
    asyncio.run(run())


def test_controller_without_baseline_ignores_items_older_than_startup():
    async def run():
        got = []

        async def handler(data, label):
            got.append(data["id"])
        from datetime import datetime, timedelta
        from core.quake_failover import JST
        now = datetime.now(JST)
        old = SourceItem("old", "x", now - timedelta(hours=3), "u")
        new = SourceItem("new", "x", now + timedelta(seconds=5), "u")
        src = FakeSource([old, new], {"old": {"id": "old"}, "new": {"id": "new"}})
        c = QuakeFailoverController(lambda: object(), handler, [src], 1, lambda: False, catchup_minutes=10**7)
        assert await c.poll_once() == 1 and got == ["new"]
    asyncio.run(run())


def test_controller_active_flag():
    c = QuakeFailoverController(lambda: None, None, [], 1, lambda: False, catchup_minutes=10**7)
    assert not c.is_active
    c.set_active(True); assert c.is_active
    c.set_active(False); assert not c.is_active


# ---------- 取得元の一覧解析（HTTPをモック） ----------
class _Resp:
    def __init__(self, payload=None, raw=b"", status=200):
        self.payload, self.status = payload, status
        self.content = self
        self._raw = raw

    async def json(self, content_type=None):
        return self.payload

    async def read(self, n=-1):
        return self._raw

    async def iter_chunked(self, n):
        # 本文を小さなチャンクに分けて返す（2026-10-06: 本番コードは iter_chunked で読む）
        for i in range(0, len(self._raw), 7):
            yield self._raw[i:i + 7]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, url, **k):
        return self.mapping[url]


def test_json_source_lists_only_quake_titles():
    from core.quake_failover import JMA_QUAKE_LIST_URL
    lst = [
        {"ttl": "震度速報", "json": "a.json", "rdt": "2026-10-01T21:28:00+09:00"},
        {"ttl": "震源・震度情報", "json": "b.json", "rdt": "2026-10-01T21:35:00+09:00"},
        {"ttl": "南海トラフ地震臨時情報", "json": "c.json"},         # 別Cogの担当
        {"ttl": "震度速報"},                                          # json名なし
    ]
    items = asyncio.run(JmaJsonSource().list_items(_Session({JMA_QUAKE_LIST_URL: _Resp(lst)})))
    assert [i.key for i in items] == ["a.json", "b.json"]
    assert items[0].url.endswith("/bosai/quake/data/a.json")


def test_xml_source_parses_atom_feed_and_rejects_oversize():
    from core.quake_failover import JMA_XML_FEED_URL, XML_MAX_BYTES
    feed = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>震度速報</title><id>urn:a</id><updated>2026-10-01T12:28:00Z</updated>
        <link type="application/xml" href="https://www.data.jma.go.jp/developer/xml/data/a.xml"/></entry>
      <entry><title>気象警報・注意報</title><id>urn:b</id><updated>2026-10-01T12:29:00Z</updated>
        <link type="application/xml" href="https://example/b.xml"/></entry></feed>""".encode()
    items = asyncio.run(JmaXmlSource().list_items(_Session({JMA_XML_FEED_URL: _Resp(raw=feed)})))
    assert [i.key for i in items] == ["urn:a"] and items[0].url.endswith("/a.xml")
    from core.quake_failover import JST
    assert items[0].reported_at.astimezone(JST).hour == 21                   # 12:28Z = 21:28 JST（同一の瞬間）
    import pytest
    with pytest.raises(RuntimeError):
        asyncio.run(JmaXmlSource().list_items(_Session({JMA_XML_FEED_URL: _Resp(raw=b"x" * (XML_MAX_BYTES + 1))})))


def test_xml_source_rejects_entity_declaration():
    from core.quake_failover import JMA_XML_FEED_URL
    import pytest
    evil = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa">]><feed>&a;</feed>'
    with pytest.raises(RuntimeError):
        asyncio.run(JmaXmlSource().list_items(_Session({JMA_XML_FEED_URL: _Resp(raw=evil)})))


# ---------- QuakeInfoCog との結合（P2Pと代替取得の重複通知防止） ----------
def test_cog_dedupes_across_p2p_and_failover():
    from unittest.mock import MagicMock
    from cogs.quake import QuakeInfoCog

    async def run():
        bot = MagicMock()
        bot.loop = asyncio.get_running_loop()
        bot.is_closed = lambda: False
        bot.get_channel = lambda i: None
        hub = P2PWebSocketHub(lambda: False, failover_threshold=2, recovery_seconds=60, disconnect_seconds=1000)
        bot.p2p_hub = hub
        cog = QuakeInfoCog(bot)
        cog.session = object()
        sent = []

        async def fake_notify(data, is_test=False, extra_note=None, skip_speech=False):
            sent.append((data.get("id") or data.get("_id"), extra_note))
        cog.notify_quake = fake_notify

        async def no_init():
            cog.last_quake_id = "baseline"
        cog._init_last_quake_id = no_init
        await cog.on_ready()
        assert cog._failover is not None and cog._failover_task is not None
        await cog.on_ready()                                   # 再発火しても二重起動しない
        assert hub._failover_listeners.count(cog._failover.set_active) == 1
        # フォールバック前: P2P経由の震源情報を通知
        p2p_dest = {"id": "p2p1", "earthquake": {"time": "2026/10/01 21:31:00", "maxScale": -1, "hypocenter": {
            "name": "千葉県北東部", "magnitude": 5.4, "depth": 50}}, "points": []}
        await cog.handle_p2p_quake(p2p_dest)
        # 障害 → フォールバック開始。気象庁側の同一内容(震源)は通知せず、新しい内容(震度)のみ通知
        hub._on_ws_failure(); hub._on_ws_failure()
        assert cog._failover.is_active
        await cog.handle_failover_quake(jma_quake_to_p2p(SHINGEN, item_key="s.json"), "気象庁HP（JSON）")
        await cog.handle_failover_quake(jma_quake_to_p2p(SHINGEN_SHINDO, item_key="d.json"), "気象庁HP（JSON）")
        await cog.handle_failover_quake(jma_quake_to_p2p(SHINGEN_SHINDO, item_key="d2.json"), "気象庁XML")  # 別取得元の同内容
        assert [s[0] for s in sent] == ["p2p1", "jma:d.json_1"]
        assert "障害" in sent[1][1] and "気象庁HP（JSON）" in sent[1][1]
        # 復旧後にP2Pが同じ内容を遅れて配信しても通知しない
        p2p_detail = {"id": "p2p2", "earthquake": {"time": "2026/10/01 21:31:00", "maxScale": 40, "hypocenter": {
            "name": "千葉県北東部", "magnitude": 5.4, "depth": 50}},
            "points": [{"pref": "千葉県", "addr": "香取市佐原", "scale": 40}, {"pref": "茨城県", "addr": "取手市寺田", "scale": 30}]}
        await cog.handle_p2p_quake(p2p_detail)
        assert len(sent) == 2
        cog._failover_task.cancel()
    asyncio.run(run())


# ---------- Quake.One（2026-10-02追加。実データ: 2026-10-02 06:01 茨城県南部 M3.9） ----------
QO_INFO = {"EventID": "20261002060147", "ReportDateTime": "2026-10-02T06:05:00+09:00",
           "OriginDateTime": "2026-10-02T06:01:00+09:00", "MaxInt": "2", "Magnitude": "3.9",
           "Hypocenter": {"Name": "茨城県南部", "Coordinate": [139.9, 36.1], "Depth": -50000,
                          "Japanese": "北緯３６．１度　東経１３９．９度　深さ　５０ｋｍ"},
           "Comments": "この地震による津波の心配はありません。"}


def _qo_small():
    def f(cls, name, lon=140.0, lat=36.0):
        return {"type": "Feature", "properties": {"class": cls, "name": name},
                "geometry": {"type": "Point", "coordinates": [lon, lat]}}
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"class": "epicenter", "area": "茨城県南部", "coord_jp": "x"},
         "geometry": {"type": "Point", "coordinates": [139.9, 36.1]}},
        f("2", "茨城県南部"), f("1", "茨城県北部"), f("2", "栃木県南部"), f("2", "埼玉県南部"),
        f("1", "千葉県北東部"), f("2", "東京都２３区"), f("1", "神奈川県東部")]}


QO_LIST = {"objects": [
    {"ReportDateTime": "2026-10-02T06:05:00+09:00", "OriginDateTime": "2026-10-02T06:01:00+09:00", "MaxInt": "2",
     "Magnitude": "3.9", "Hypocenter": "茨城県南部", "EventID": "20261002060147"},
    {"ReportDateTime": "2026-10-01T21:32:00+09:00", "OriginDateTime": "2026-10-01T21:27:00+09:00", "MaxInt": "3",
     "Magnitude": "5.1", "Hypocenter": "千葉県北東部", "EventID": "20261001212719"},
    {"ReportDateTime": "x", "Hypocenter": "不正", "EventID": "../../etc/passwd"}],
    "next_cursor": "abc"}


def test_quake_one_convert_real_sample():
    d = quake_one_to_p2p(QO_INFO, _qo_small(), item_key="20261002060147@2026-10-02T06:05:00+09:00")
    assert d["issue"]["type"] == "ScaleAndDestination" and d["issue"]["source"] == "Quake.One"
    eq = d["earthquake"]
    assert eq["time"] == "2026/10/02 06:01:00" and eq["maxScale"] == 20 and eq["domesticTsunami"] == "None"
    h = eq["hypocenter"]
    assert (h["name"], h["magnitude"], h["depth"], h["longitude"], h["latitude"]) == ("茨城県南部", 3.9, 50, 139.9, 36.1)
    pts = {(p["addr"], p["pref"], p["scale"], p["isArea"]) for p in d["points"]}
    assert ("茨城県南部", "茨城県", 20, True) in pts and ("東京都２３区", "東京都", 20, True) in pts
    assert len(d["points"]) == 7 and all(p["addr"] != "" for p in d["points"])   # epicenter 行は含めない


def test_quake_one_same_content_as_p2p_is_duplicate_but_stage_differs():
    d = quake_one_to_p2p(QO_INFO, _qo_small())
    fp = fingerprint_from_p2p(d)
    # 同じ地震の「P2P側の各地の震度に関する情報」と、都道府県別の最大震度が一致すれば同一内容
    p2p = {"earthquake": {"time": "2026/10/02 06:01", "maxScale": 20, "hypocenter": {"name": "茨城県南部", "magnitude": 3.9, "depth": 50}},
           "points": [{"pref": "茨城県", "addr": "つくば市", "scale": 20}, {"pref": "栃木県", "addr": "宇都宮市", "scale": 20},
                      {"pref": "埼玉県", "addr": "さいたま市", "scale": 20}, {"pref": "千葉県", "addr": "香取市", "scale": 10},
                      {"pref": "東京都", "addr": "千代田区", "scale": 20}, {"pref": "神奈川県", "addr": "横浜市", "scale": 10}]}
    assert fingerprints_match(fp, fingerprint_from_p2p(p2p))
    # 震源のみ（震度なし）の情報とは別内容なので通知される
    assert not fingerprints_match(fp, fingerprint_from_p2p(jma_quake_to_p2p(SHINGEN)))


def test_quake_one_validation_rejects_bad_input():
    assert quake_one_to_p2p({**QO_INFO, "EventID": "../x"}, _qo_small()) is None
    assert quake_one_to_p2p("x", None) is None and quake_one_to_p2p({}, None) is None
    bad = quake_one_to_p2p({**QO_INFO, "Magnitude": "99", "Hypocenter": {"Name": "A\x00B\n", "Coordinate": [999, 999], "Depth": "abc"}}, None)
    h = bad["earthquake"]["hypocenter"]
    assert h["magnitude"] == -1.0 and (h["longitude"], h["latitude"]) == (-200.0, -200.0) and h["depth"] == -1
    assert "\x00" not in h["name"] and h["name"].startswith("AB")        # 制御文字は除去される
    assert bad["issue"]["type"] == "Destination"                       # 震度が無ければ震源のみ
    long_name = quake_one_to_p2p({**QO_INFO, "Hypocenter": {"Name": "あ" * 500}}, None)
    assert len(long_name["earthquake"]["hypocenter"]["name"]) == 40
    unk = _qo_small(); unk["features"][1]["properties"]["class"] = "9"  # 未知の震度表記は点だけ読み飛ばす
    assert len(quake_one_to_p2p(QO_INFO, unk)["points"]) == 6
    assert quake_one_to_p2p({**QO_INFO, "Hypocenter": {}}, None) is None


def test_quake_one_intensity_notations():
    sm = {"features": [{"properties": {"class": c, "name": "茨城県南部"}} for c in ("5-", "5強", "6＋", "7")]}
    d = quake_one_to_p2p({**QO_INFO, "MaxInt": "6+"}, sm)
    assert sorted(p["scale"] for p in d["points"]) == [45, 50, 60, 70]
    assert d["earthquake"]["maxScale"] == 60


def test_quake_one_source_list_and_detail():
    from core.quake_failover import QUAKE_ONE_BASE

    async def run():
        base = QUAKE_ONE_BASE
        eid = "20261002060147"
        sess = _Session({
            f"{base}/list.json": _Resp(raw=json.dumps(QO_LIST).encode()),
            f"{base}/{eid}/info.json": _Resp(raw=json.dumps(QO_INFO).encode()),
            f"{base}/{eid}/smallScalePoints.json": _Resp(raw=json.dumps(_qo_small()).encode()),
        })
        src = QuakeOneSource()
        items = await src.list_items(sess)
        assert [i.key for i in items] == ["20261002060147@2026-10-02T06:05:00+09:00",
                                          "20261001212719@2026-10-01T21:32:00+09:00"]      # 不正なEventIDは除外
        assert items[0].url == f"{base}/{eid}"
        d = await src.fetch_detail(sess, items[0])
        assert d["issue"]["source"] == "Quake.One" and len(d["points"]) == 7
        # info.json の EventID が食い違う応答は採用しない
        sess2 = _Session({f"{base}/{eid}/info.json": _Resp(raw=json.dumps({**QO_INFO, "EventID": "20260101000000"}).encode()),
                          f"{base}/{eid}/smallScalePoints.json": _Resp(raw=json.dumps(_qo_small()).encode())})
        import pytest
        with pytest.raises(RuntimeError):
            await src.fetch_detail(sess2, items[0])
        # サイズ超過・HTTPエラー
        from core.quake_failover import QUAKE_ONE_MAX_BYTES
        with pytest.raises(RuntimeError):
            await src.list_items(_Session({f"{base}/list.json": _Resp(raw=b"x" * (QUAKE_ONE_MAX_BYTES + 1))}))
        with pytest.raises(RuntimeError):
            await src.list_items(_Session({f"{base}/list.json": _Resp(status=503)}))
    asyncio.run(run())


def test_controller_gives_up_after_max_detail_attempts():
    async def run():
        got = []

        async def handler(data, label):
            got.append(data)
        src = FakeSource([_item("bad", 50)], {})            # 詳細が常に失敗（KeyError）
        c = QuakeFailoverController(lambda: object(), handler, [src], 1, lambda: False, catchup_minutes=10**7)
        c._baseline_done = True
        for _ in range(MAX_DETAIL_ATTEMPTS):
            assert await c.poll_once() == 0
        calls = len(src.detail_calls)
        assert calls == MAX_DETAIL_ATTEMPTS
        await c.poll_once()
        assert len(src.detail_calls) == calls                # 断念後は取得しにいかない
    asyncio.run(run())


def test_controller_catchup_window_skips_old_items_on_first_activation():
    """障害前にP2Pで通知済みの過去の地震が、初回のフォールバック取得で再通知されない。"""
    async def run():
        from datetime import datetime, timedelta
        from core.quake_failover import JST
        got = []

        async def handler(data, label):
            got.append(data["id"])
        now = datetime(2026, 10, 1, 21, 32, 30, tzinfo=JST)

        def at(key, delta_min):
            return SourceItem(key, "x", now - timedelta(minutes=delta_min), "u")
        items = [at("recent_a", 4), at("recent_b", 2), at("old_1h", 60), at("old_2d", 2880),
                 SourceItem("no_time", "x", None, "u")]
        src = FakeSource(items, {k.key: {"id": k.key} for k in items})
        c = QuakeFailoverController(lambda: object(), handler, [src], 1, lambda: False,
                                    catchup_minutes=30, now_fn=lambda: now)
        c._baseline_done = True
        assert await c.poll_once() == 2
        assert got == ["recent_a", "recent_b"]                      # 古い情報・発表時刻不明は通知しない
        assert src.detail_calls == ["recent_a", "recent_b"]         # 古い情報の詳細は取得しにも行かない
        assert await c.poll_once() == 0
        # 窓を広げれば（障害が長引く場合）古い情報も対象になる
        src2 = FakeSource([at("old_1h", 60)], {"old_1h": {"id": "old_1h"}})
        c2 = QuakeFailoverController(lambda: object(), handler, [src2], 1, lambda: False,
                                     catchup_minutes=120, now_fn=lambda: now)
        c2._baseline_done = True
        assert await c2.poll_once() == 1
    asyncio.run(run())


# ---------- 再接続の待ち時間中のフォールバック（2026-10-02追加） ----------
def test_hub_failover_starts_during_reconnect_wait_without_waiting_for_failure_count():
    """切断後、再接続の待ち時間（失敗が積み上がらない間）でも一定時間でフォールバックに入る。"""
    async def run():
        hub = P2PWebSocketHub(lambda: False, failover_threshold=5, recovery_seconds=0.05, disconnect_seconds=0.05)
        events = []
        hub.add_failover_listener(events.append)
        hub._on_ws_connect()
        hub._on_ws_failure()                       # 切断（失敗は1回だけ。以降は再接続待ち）
        assert not hub.failover_active
        await asyncio.sleep(0.02)
        assert not hub.failover_active             # まだ待機時間内
        await asyncio.sleep(0.1)
        assert hub.failover_active and events == [True]
        assert hub.get_stats()["failover"]["consecutive_failures"] == 1     # 回数条件では入っていない
        hub._on_ws_failure()                       # さらに失敗しても二重通知しない
        assert events == [True]
        hub._on_ws_connect()                       # 復旧は従来どおり安定接続を待つ
        assert hub.failover_active
        await asyncio.sleep(0.1)
        assert not hub.failover_active and events == [True, False]
    asyncio.run(run())


def test_hub_quick_reconnect_does_not_trigger_failover():
    async def run():
        hub = P2PWebSocketHub(lambda: False, failover_threshold=5, disconnect_seconds=0.1)
        events = []
        hub.add_failover_listener(events.append)
        hub._on_ws_connect()
        hub._on_ws_failure()
        await asyncio.sleep(0.03)
        hub._on_ws_connect()                       # 短い切断のうちに再接続
        await asyncio.sleep(0.2)
        assert not hub.failover_active and events == []
        # 再び切断した場合は、新しい切断期間として数え直す
        hub._on_ws_failure()
        await asyncio.sleep(0.15)
        assert hub.failover_active and events == [True]
    asyncio.run(run())


def test_hub_zero_disconnect_seconds_is_immediate_and_startup_counts_as_down():
    async def run():
        hub = P2PWebSocketHub(lambda: False, failover_threshold=5, disconnect_seconds=0)
        events = []
        hub.add_failover_listener(events.append)
        hub._on_ws_failure()
        assert hub.failover_active and events == [True]
        # 起動直後（一度も接続できていない）も切断期間として扱う
        hub2 = P2PWebSocketHub(lambda: False, failover_threshold=5, disconnect_seconds=0.05)
        ev2 = []
        hub2.add_failover_listener(ev2.append)
        hub2._begin_down_period()
        await asyncio.sleep(0.1)
        assert hub2.failover_active and ev2 == [True]
        # closed 中は開始しない
        hub3 = P2PWebSocketHub(lambda: True, failover_threshold=5, disconnect_seconds=0.05)
        hub3._begin_down_period()
        await asyncio.sleep(0.1)
        assert not hub3.failover_active
    asyncio.run(run())


# ===== Quake.One の取得間隔（2026-10-05追加）=====
def test_quake_one_poll_seconds_has_floor_of_6_seconds():
    from core.quake_failover import QuakeOneSource, build_sources
    assert QuakeOneSource().poll_seconds == 6.0
    assert QuakeOneSource(1).poll_seconds == 6.0          # 10回/分を超える頻度は指定不可
    assert QuakeOneSource(12).poll_seconds == 12.0
    q = [s for s in build_sources(["jma_json", "quake_one"], 3) if s.name == "quake_one"][0]
    assert q.poll_seconds == 6.0
    assert [s for s in build_sources(["jma_json"], 3)][0].poll_seconds is None


def test_controller_never_lists_a_rate_limited_source_faster_than_its_interval():
    import time as _t

    async def run():
        stamps = []

        class Limited(FakeSource):
            poll_seconds = 0.3

            async def list_items(self, session):
                stamps.append(_t.monotonic())
                return []
        src = Limited([], {})
        c = QuakeFailoverController(lambda: object(), None, [src], 0.01, lambda: False, catchup_minutes=10**7)
        for _ in range(3):
            await c.poll_once()
        assert all(b - a >= 0.29 for a, b in zip(stamps, stamps[1:]))
        assert c._next_sleep == 0.3                        # 次回待ちも取得元の間隔
    asyncio.run(run())


# ===== 本文の読み取り（2026-10-06修正）=====
# resp.content.read(n) は届いている分しか返さず、複数チャンクの本文が途中で切れていた
# （jma_xml の「unclosed token」エラーの原因）。
def _serve(body: bytes, chunk: int = 16384):
    from aiohttp import web

    async def handler(request):
        resp = web.StreamResponse()
        await resp.prepare(request)
        for i in range(0, len(body), chunk):
            await resp.write(body[i:i + chunk])
            await asyncio.sleep(0.002)
        await resp.write_eof()
        return resp
    app = web.Application()
    app.router.add_get("/", handler)
    return app


def test_body_split_across_chunks_is_read_completely_for_xml_and_json():
    import json
    import aiohttp
    from aiohttp import web
    from core import quake_failover as qf

    xml = b"<feed>\n" + b"".join(b"<entry><title>t%d</title></entry>\n" % i for i in range(5000)) + b"</feed>\n"
    js = json.dumps({"objects": [{"EventID": "x%d" % i} for i in range(5000)]}).encode()

    async def run():
        for body in (xml, js):
            runner = web.AppRunner(_serve(body))
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"http://127.0.0.1:{port}/") as r:
                        assert await qf.JmaXmlSource._read_limited(r) == body if body is xml else True
                    if body is js:
                        assert await qf._get_json_limited(s, f"http://127.0.0.1:{port}/") == json.loads(js)
            finally:
                await runner.cleanup()
    asyncio.run(run())


def test_oversized_body_is_rejected():
    import aiohttp
    from aiohttp import web
    from core import quake_failover as qf

    async def run():
        runner = web.AppRunner(_serve(b"x" * 100_000))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{port}/") as r:
                    with pytest.raises(RuntimeError):
                        await qf._read_body_limited(r, 50_000)
        finally:
            await runner.cleanup()
    asyncio.run(run())

# ===== Quake.One list.json の実際の形式（2026-10-06）=====
# EventID をキーとする辞書。以前は {"objects": [...]} か配列を想定していて、
# 起動時に「list.json の形式が想定外です」と失敗していた。
def test_quake_one_list_accepts_real_dict_keyed_by_event_id():
    from core.quake_failover import QuakeOneSource
    real = {
        "20261006134727": {"ReportDateTime": "2026-10-06T13:49:00+09:00", "OriginDateTime": "2026-10-06T13:47:00+09:00",
                           "MaxInt": "1", "Magnitude": "2.9", "Hypocenter": "熊本県熊本地方"},
        "20261006104835": {"ReportDateTime": "2026-10-06T10:50:00+09:00", "OriginDateTime": "2026-10-06T10:48:00+09:00",
                           "MaxInt": "1", "Magnitude": "3.6", "Hypocenter": "浦河沖"},
    }

    async def run():
        items = await QuakeOneSource().list_items(_Session({"http://files.quake.one/list.json": _Resp(raw=json.dumps(real).encode())}))
        assert {i.key for i in items} == {"20261006134727@2026-10-06T13:49:00+09:00",
                                          "20261006104835@2026-10-06T10:50:00+09:00"}
        assert all(i.url.startswith("http://files.quake.one/2026") and i.reported_at is not None for i in items)
        for bad in ("[]", "{}", "1", '"x"'):
            try:
                await QuakeOneSource().list_items(_Session({"http://files.quake.one/list.json": _Resp(raw=bad.encode())}))
            except RuntimeError:
                assert bad in ("1", '"x"')       # 辞書でも配列でもない形式だけがエラー
    asyncio.run(run())
