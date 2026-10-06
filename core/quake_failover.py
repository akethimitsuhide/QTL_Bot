"""
core/quake_failover.py
=======================
P2P地震情報 WebSocket が使えない間、地震情報（震度速報・震源に関する情報・
各地の震度に関する情報・遠地地震に関する情報）を気象庁の公開データの
定期取得で代替するためのモジュール（2026-10-01 追加）。

【経緯】
2026-10-01 21:27 の千葉県北東部の地震（M5前後）で、P2P地震情報 WebSocket の
切断・接続タイムアウトが続き（ping timeout → opening handshake タイムアウト
が約4分間）、震度速報・震源に関する情報・各地の震度に関する情報が通知
されなかった。単一の取得元に依存していたための欠落であり、取得元の
冗長化を行う。

【構成】
- P2PWebSocketHub（core/p2p_ws_hub.py）が連続接続失敗を数え、閾値
  （既定5回）に達したら add_failover_listener のコールバックへ active=True
  を通知する。接続が安定したら active=False を通知する。
- QuakeFailoverController（本モジュール）が active の間だけ、設定された
  取得元（QuakeFallbackSource）を順に試して新着の地震情報を取得し、
  P2P地震情報の JMAQuake（code=551）互換の dict へ変換して
  QuakeInfoCog.handle_failover_quake へ渡す。変換後の形式が同じなので、
  通知処理（notify_quake）は P2P 経由と共通のものをそのまま使う。
- 取得元（優先順。QUAKE_FAILOVER_SOURCES で変更可）:
    jma_json : 気象庁HP のJSON（bosai/quake/data/list.json と各詳細JSON。
               cogs/other.py の後発地震注意情報等の取得にも使っているもの）
    jma_xml  : 気象庁防災情報XML（随時フィード eqvol.xml と各電文XML）
    quake_one: Quake.One の Static API（http://files.quake.one/ の list.json・
               :EventID/info.json・:EventID/smallScalePoints.json。2026-10-02〜。
               暗号化されない http のみの第三者配信データで、1つの地震につき
               発表の最終形（震源・震度に関する情報相当）が1件ずつ得られる。
               震度速報・震源に関する情報の段階は含まず、発表から数分遅れる
               ため、既定では優先順の最後に置く）
  新しい取得元は QuakeFallbackSource を継承して SOURCE_FACTORIES へ
  登録すれば追加できる。

【重複通知の防止（QuakeContentDedupe）】
取得元をまたいで（P2P ⇔ 気象庁JSON ⇔ 気象庁XML）同じ内容の地震情報が
何度も通知されないよう、通知前に「震源要素」（発生時刻・震央地名・M・
深さ）と「震度の分布」（最大震度と都道府県別の最大震度）で照合する。
情報の種別名（ScalePrompt 等）は取得元ごとの呼称の差が出やすいため
照合に使わず、内容そのものだけを見る。震度速報（震源なし・震度あり）・
震源に関する情報（震源あり・震度なし）・各地の震度（両方あり）は内容が
自然に異なるので、別の情報として扱われる。内容が更新（訂正）された
場合も内容が変わるため再通知される。

【起動時の既知情報（baseline）】
Bot 起動時に一度だけ各取得元の現在の一覧を「既知」として記録し、通知は
しない。以後のフォールバック中は、この既知集合に無い（＝起動後に発表
された）情報のうち、内容が既に通知済みでないものだけを通知する。
起動時に取得できなかった場合は、起動時刻より古い情報は対象外にする。

【注意（実データでの検証について）】
気象庁JSON/XMLの構造は公開仕様に沿った防御的な実装（欠損・型の揺れを
許容し、読めない項目は既定値にする）だが、実装時点では気象庁サーバーへ
接続できる環境での実データ検証を行っていない（tests/test_quake_failover.py
の固定データは仕様から作った合成データ）。実際の list.json・各詳細JSON・
XML電文を jma_quake_to_p2p に通して、震源・震度の変換結果が期待どおりか
確認すること。
"""
import asyncio
import json
import logging
import re
import time
import traceback
import xml.etree.ElementTree as ET
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from core.constants import PREFECTURE_MAP

logger = logging.getLogger("QTLBot")

JST = timezone(timedelta(hours=9))

JMA_QUAKE_LIST_URL = "https://www.jma.go.jp/bosai/quake/data/list.json"
JMA_QUAKE_DETAIL_BASE = "https://www.jma.go.jp/bosai/quake/data/"
JMA_XML_FEED_URL = "https://www.data.jma.go.jp/developer/xml/feed/eqvol.xml"

# 代替取得の対象にする情報の題名（list.json の ttl / XMLフィードの title）。
# 後発地震注意情報・南海トラフ臨時情報・震源要素更新のお知らせ等は
# cogs/other.py が別途取得するため対象外。
QUAKE_INFO_TITLES = frozenset({
    "震度速報",
    "震源に関する情報",
    "震源・震度情報",
    "震源・震度に関する情報",
    "各地の震度に関する情報",
    "遠地地震に関する情報",
})

# 気象庁の震度表記 → P2P地震情報の scale 値
JMA_INT_TO_P2P_SCALE = {
    "1": 10, "2": 20, "3": 30, "4": 40,
    "5-": 45, "5+": 50, "6-": 55, "6+": 60, "7": 70,
    "!5-": 46,   # 震度5弱以上未入電（推定）
}

XML_MAX_BYTES = 2 * 1024 * 1024      # 電文XMLの最大サイズ（想定外の巨大レスポンス対策）
# Quake.One の list.json の取得間隔の下限（秒）。10回/分（6秒間隔）を超える頻度では
# 取得しない（2026-10-05追加）。詳細（info.json 等）の取得は数えない。
QUAKE_ONE_MIN_POLL_SECONDS = 6.0
MAX_NOTIFY_PER_CYCLE = 10            # 1回のポーリングで通知する最大件数（大量発生時の過剰通知防止）
MAX_DETAIL_ATTEMPTS = 5              # 詳細の取得を試す最大回数（恒久的な失敗の無限再試行防止）
REQUEST_TIMEOUT_SEC = 10             # 1リクエストあたりのタイムアウト


# ===============================
# 小さなユーティリティ
# ===============================
def _as_list(v) -> list:
    """JSON/XML変換結果の「単一要素なら dict、複数なら list」の揺れを list に統一する。"""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _get(d, *path, default=None):
    """dict/list が None や空でも例外にならない安全な入れ子取得（listは先頭要素を見る）。"""
    cur = d
    for key in path:
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    if isinstance(cur, list):
        cur = cur[0] if cur else default
    return cur if cur is not None else default


def _text(v) -> str:
    """文字列化（dict の場合は '#text' / 'value' があればそれを使う）。"""
    if v is None:
        return ""
    if isinstance(v, dict):
        return str(v.get("#text") or v.get("value") or "").strip()
    return str(v).strip()


def _parse_dt(raw) -> datetime | None:
    """ISO8601（+09:00 / Z 付き）・'YYYY/MM/DD HH:MM[:SS]' を aware な datetime にする。"""
    s = _text(raw)
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00").replace("/", "-")
        if " " in s2 and "T" not in s2:
            s2 = s2.replace(" ", "T", 1)
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        return dt
    except (ValueError, TypeError):
        return None


def _fmt_p2p_time(dt: datetime | None) -> str:
    """P2P地震情報の time 表記 'YYYY/MM/DD HH:MM:SS'（JST）。"""
    if dt is None:
        return ""
    return dt.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S")


_COORD_RE = re.compile(r"^\s*([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:([+-]\d+(?:\.\d+)?))?\s*/?\s*$")


def parse_iso6709_coordinate(raw) -> tuple[float, float, int]:
    """
    気象庁の座標表記 '+35.7+140.7-40000/'（緯度・経度・高さ[m]）を
    (緯度, 経度, 深さkm) にする。解釈できない場合は P2P の「不明」値
    (-200, -200, -1)。高さが無い場合の深さは -1、深さ0は 0（ごく浅い）。
    """
    m = _COORD_RE.match(_text(raw))
    if not m:
        return -200.0, -200.0, -1
    lat, lon = float(m.group(1)), float(m.group(2))
    depth = -1
    if m.group(3) is not None:
        depth = int(round(abs(float(m.group(3))) / 1000))
    return lat, lon, depth


def _parse_magnitude(raw) -> float:
    try:
        v = float(_text(raw))
        return v if v == v else -1.0      # NaN（不明）は -1
    except (TypeError, ValueError):
        return -1.0


# ===============================
# 気象庁の電文（JSON/XMLをdict化したもの）→ P2P地震情報 JMAQuake 互換dict
# ===============================
def _issue_type(title: str, has_stations: bool, has_hypocenter: bool,
                has_intensity: bool) -> str:
    """題名と内容から P2P の issue.type 相当を決める。"""
    if "震度速報" in title:
        return "ScalePrompt"
    if "遠地地震" in title:
        return "Foreign"
    if "各地の震度" in title:
        return "DetailScale"
    if "震源・震度" in title:
        return "DetailScale" if has_stations else "ScaleAndDestination"
    if "震源に関する情報" in title:
        return "Destination"
    # 題名で判別できない場合は内容から推定
    if has_intensity and has_hypocenter:
        return "DetailScale" if has_stations else "ScaleAndDestination"
    if has_intensity:
        return "ScalePrompt"
    if has_hypocenter:
        return "Destination"
    return "Other"


def _domestic_tsunami(comments: dict) -> str:
    """
    Body.Comments.ForecastComment の文面から P2P の domesticTsunami 値を推定する。
    該当する記述が無い／判別できない場合は誤って「津波の心配なし」と断定しない
    よう "Unknown"（津波の有無は不明）にする。
    """
    return _tsunami_from_text(_text(_get(comments, "ForecastComment", "Text")))


def _tsunami_from_text(text: str) -> str:
    """津波に関する文面から P2P の domesticTsunami 値を推定する（判別不能は "Unknown"）。"""
    if not text:
        return "Unknown"
    if "津波の心配はありません" in text or "津波の心配なし" in text:
        return "None"
    if "大津波警報" in text:
        return "MajorWarning"
    if "津波警報" in text:
        return "Warning"
    if "津波注意報" in text:
        return "Watch"
    if "若干の海面変動" in text:
        return "NonEffective"
    if "調査中" in text:
        return "Checking"
    return "Unknown"


def jma_quake_to_p2p(detail: dict, list_title: str = "", item_key: str = "") -> dict | None:
    """
    気象庁の地震情報電文（JSON、または _xml_to_dict でdict化したXML）を
    P2P地震情報 JMAQuake（code=551）互換の dict へ変換する。
    取消報・地震情報でない電文・内容を全く読み取れない場合は None。

    返すdictは cogs/quake.py の notify_quake が参照する項目
    （issue / earthquake / points / comments）を持つ。
    """
    if not isinstance(detail, dict):
        return None
    head = detail.get("Head") or {}
    body = detail.get("Body") or {}
    title = _text(head.get("Title")) or _text(_get(detail, "Control", "Title")) or list_title
    info_type = _text(head.get("InfoType"))
    if info_type == "取消":
        return None

    eq = _get(body, "Earthquake")
    eq = eq if isinstance(eq, dict) else {}
    hypo_area = _get(eq, "Hypocenter", "Area")
    hypo_area = hypo_area if isinstance(hypo_area, dict) else {}
    lat, lon, depth = parse_iso6709_coordinate(hypo_area.get("Coordinate"))
    name = _text(hypo_area.get("Name"))
    mag = _parse_magnitude(eq.get("Magnitude"))
    origin = _parse_dt(eq.get("OriginTime")) or _parse_dt(eq.get("ArrivalTime"))
    has_hypocenter = bool(name)

    # 震度（Observation: MaxInt / Pref[] / Area[] / City[] / IntensityStation[]）
    obs = _get(body, "Intensity", "Observation")
    obs = obs if isinstance(obs, dict) else {}
    station_points: list[dict] = []
    area_points: list[dict] = []
    for pref in _as_list(obs.get("Pref")):
        if not isinstance(pref, dict):
            continue
        pref_name = _text(pref.get("Name"))
        for area in _as_list(pref.get("Area")):
            if not isinstance(area, dict):
                continue
            a_scale = JMA_INT_TO_P2P_SCALE.get(_text(area.get("MaxInt")))
            a_name = _text(area.get("Name"))
            if a_scale is not None and a_name:
                area_points.append({"pref": pref_name, "addr": a_name,
                                    "isArea": True, "scale": a_scale})
            for city in _as_list(area.get("City")):
                if not isinstance(city, dict):
                    continue
                for st in _as_list(city.get("IntensityStation")):
                    if not isinstance(st, dict):
                        continue
                    s_scale = JMA_INT_TO_P2P_SCALE.get(_text(st.get("Int")))
                    s_name = _text(st.get("Name"))
                    if s_scale is not None and s_name:
                        station_points.append({"pref": pref_name, "addr": s_name,
                                               "isArea": False, "scale": s_scale})

    has_stations = bool(station_points)
    issue_type = _issue_type(title, has_stations, has_hypocenter,
                             has_intensity=bool(area_points or station_points
                                                or _text(obs.get("MaxInt"))))
    if issue_type == "ScalePrompt" or not has_stations:
        points = area_points
    else:
        points = station_points

    max_scale = JMA_INT_TO_P2P_SCALE.get(_text(obs.get("MaxInt")))
    if max_scale is None:
        scales = [p["scale"] for p in points]
        max_scale = max(scales) if scales else -1

    if not has_hypocenter and not points and issue_type != "Foreign":
        return None      # 読み取れる内容が何も無い電文は通知しない

    comments = _get(body, "Comments")
    comments = comments if isinstance(comments, dict) else {}
    # 自由付加文は P2P でも「遠地地震（噴火による地震の識別）」でのみ使われる
    # 項目のため、それ以外の種別では空にして P2P 経由の通知と表示を揃える。
    free_form = _text(comments.get("FreeFormComment")) if issue_type == "Foreign" else ""

    report_dt = _parse_dt(head.get("ReportDateTime"))
    return {
        "id": f"jma:{item_key or _text(head.get('EventID'))}_{_text(head.get('Serial'))}",
        "code": 551,
        "issue": {
            "source": "気象庁",
            "time": _fmt_p2p_time(report_dt),
            "type": issue_type,
            "correct": "None",
        },
        "earthquake": {
            "time": _fmt_p2p_time(origin),
            "hypocenter": {
                "name": name,
                "latitude": lat,
                "longitude": lon,
                "depth": depth,
                "magnitude": mag,
            },
            "maxScale": max_scale,
            "domesticTsunami": _domestic_tsunami(comments),
            "foreignTsunami": "Unknown",
        },
        "points": points,
        "comments": {"freeFormComment": free_form},
    }


def _xml_to_dict(elem: ET.Element):
    """
    XML要素を、気象庁JSONと同じ形（タグ名＝キー、同名の複数要素＝list、
    子の無い要素＝文字列）のdictにする。名前空間・属性は無視する。
    """
    children = list(elem)
    if not children:
        return (elem.text or "").strip()
    out: dict = {}
    for ch in children:
        tag = ch.tag.rsplit("}", 1)[-1] if isinstance(ch.tag, str) else str(ch.tag)
        val = _xml_to_dict(ch)
        if tag in out:
            if not isinstance(out[tag], list):
                out[tag] = [out[tag]]
            out[tag].append(val)
        else:
            out[tag] = val
    return out


# ===============================
# Quake.One（Static API）→ P2P地震情報 JMAQuake 互換dict
# ===============================
QUAKE_ONE_BASE = "http://files.quake.one"
QUAKE_ONE_MAX_BYTES = 1024 * 1024          # JSON1件あたりの最大サイズ
_QUAKE_ONE_EVENT_ID_RE = re.compile(r"^[0-9]{10,20}$")   # URLに埋め込むため数字のみ許可

# Quake.One の震度表記（class / MaxInt）→ P2P の scale 値。
# 実データで確認できた表記は "1"〜"2" のみのため、気象庁形式（"5-" "5+"）と
# 日本語表記（"5弱" "5強"）の両方を許容する。未知の表記は点を読み飛ばす。
_QUAKE_ONE_INT = {
    "1": 10, "2": 20, "3": 30, "4": 40, "7": 70,
    "5-": 45, "5+": 50, "6-": 55, "6+": 60,
    "5弱": 45, "5強": 50, "6弱": 55, "6強": 60,
}

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_text(v, max_len: int) -> str:
    """外部入力の文字列を通知に使えるよう整える（制御文字除去・長さ制限）。"""
    s = _CONTROL_CHARS_RE.sub("", _text(v))
    return s[:max_len]


def _quake_one_scale(v) -> int | None:
    s = _text(v).replace("−", "-").replace("－", "-").replace("＋", "+")
    return _QUAKE_ONE_INT.get(s)


def quake_one_to_p2p(info: dict, small_points: dict | None, item_key: str = "") -> dict | None:
    """
    Quake.One の info.json と smallScalePoints.json（細分区域別の震度と重心）を
    P2P地震情報 JMAQuake（code=551）互換の dict へ変換する。

    Quake.One は1つの地震につき最終形の情報のみを返し、情報種別の区別が無いため、
    震源と区域別震度が得られれば "ScaleAndDestination"（震度・震源に関する
    情報）、震源のみなら "Destination" として扱う。points は細分区域単位
    （isArea=True）で、都道府県名は prefecture_map.json（PREFECTURE_MAP）から
    補う（無い区域は空文字＝重複判定では最大震度のみで照合する）。
    市町村別の largeScalePoints は P2P の観測点名と一致せず地図描画に使えない
    ため、現状は使わない。

    外部（暗号化なしの http）から取得した値のため、型・範囲を検証し、
    不正な値は「不明」の値に置き換える（EventIDは数字のみ許可）。
    """
    if not isinstance(info, dict):
        return None
    event_id = _text(info.get("EventID"))
    if not _QUAKE_ONE_EVENT_ID_RE.match(event_id):
        return None

    hypo = info.get("Hypocenter")
    hypo = hypo if isinstance(hypo, dict) else {}
    name = _clean_text(hypo.get("Name"), 40)

    lat = lon = -200.0
    coord = hypo.get("Coordinate")
    if isinstance(coord, (list, tuple)) and len(coord) >= 2:
        try:
            lo, la = float(coord[0]), float(coord[1])
            if -180 <= lo <= 180 and -90 <= la <= 90:
                lon, lat = lo, la
        except (TypeError, ValueError):
            pass

    depth = -1
    try:
        d = int(round(abs(float(hypo.get("Depth"))) / 1000))
        if 0 <= d <= 1000:
            depth = d
    except (TypeError, ValueError):
        pass

    mag = _parse_magnitude(info.get("Magnitude"))
    if not (0 <= mag <= 10):
        mag = -1.0

    points: list[dict] = []
    # 注意: _get() は list の場合に先頭要素へ潰すため、features は直接取り出す
    features = small_points.get("features") if isinstance(small_points, dict) else None
    for f in _as_list(features):
        props = f.get("properties") if isinstance(f, dict) else None
        if not isinstance(props, dict) or props.get("class") == "epicenter":
            continue
        scale = _quake_one_scale(props.get("class"))
        pname = _clean_text(props.get("name"), 40)
        if scale is None or not pname:
            continue
        points.append({"pref": PREFECTURE_MAP.get(pname) or "", "addr": pname,
                       "isArea": True, "scale": scale})

    max_scale = _quake_one_scale(info.get("MaxInt"))
    if max_scale is None:
        max_scale = max((p["scale"] for p in points), default=-1)

    if not name and not points:
        return None
    if name and points:
        issue_type = "ScaleAndDestination"
    elif name:
        issue_type = "Destination"
    else:
        issue_type = "ScalePrompt"

    origin = _parse_dt(info.get("OriginDateTime"))
    report = _parse_dt(info.get("ReportDateTime"))
    return {
        "id": f"quakeone:{item_key or event_id}",
        "code": 551,
        "issue": {"source": "Quake.One", "time": _fmt_p2p_time(report),
                  "type": issue_type, "correct": "None"},
        "earthquake": {
            "time": _fmt_p2p_time(origin),
            "hypocenter": {"name": name, "latitude": lat, "longitude": lon,
                           "depth": depth, "magnitude": mag},
            "maxScale": max_scale,
            "domesticTsunami": _tsunami_from_text(_clean_text(info.get("Comments"), 500)),
            "foreignTsunami": "Unknown",
        },
        "points": points,
        "comments": {"freeFormComment": ""},
    }


# ===============================
# 内容による重複判定
# ===============================
@dataclass(frozen=True)
class QuakeFingerprint:
    """地震情報の「内容」を表す指紋。種別名は含めない（取得元ごとの呼称差を避ける）。"""
    time_epoch: float | None             # 地震の発生時刻（秒、分単位の精度）
    hypocenter: str                      # 震央地名
    magnitude: float                     # M（不明は -1）
    depth: int                           # 深さkm（不明は -1）
    max_scale: int                       # 最大震度（P2P scale値、無しは -1）
    pref_scales: frozenset               # {(都道府県名, その都道府県の最大震度)}


def fingerprint_from_p2p(data: dict) -> QuakeFingerprint:
    """P2P形式の地震情報 dict から内容の指紋を作る。"""
    eq = data.get("earthquake") or {}
    hypo = eq.get("hypocenter") or {}
    ts = None
    dt = _parse_dt(eq.get("time"))
    if dt is not None:
        ts = dt.replace(second=0, microsecond=0).timestamp()
    by_pref: dict[str, int] = {}
    for p in data.get("points") or []:
        scale = p.get("scale")
        if scale is None:
            continue
        pref = (p.get("pref") or "").strip()
        if scale > by_pref.get(pref, -1):
            by_pref[pref] = scale
    try:
        mag = float(hypo.get("magnitude", -1))
    except (TypeError, ValueError):
        mag = -1.0
    try:
        depth = int(hypo.get("depth", -1))
    except (TypeError, ValueError):
        depth = -1
    try:
        max_scale = int(eq.get("maxScale", -1))
    except (TypeError, ValueError):
        max_scale = -1
    return QuakeFingerprint(
        time_epoch=ts,
        hypocenter=(hypo.get("name") or "").strip(),
        magnitude=mag,
        depth=depth,
        max_scale=max_scale,
        pref_scales=frozenset(by_pref.items()),
    )


# 発生時刻の許容差（秒）。震度速報の time が発生時刻ではなく検知時刻に
# なる等、取得元による数十秒〜1分程度のずれを同一とみなす。
TIME_TOLERANCE_SEC = 180


def fingerprints_match(a: QuakeFingerprint, b: QuakeFingerprint) -> bool:
    """2つの指紋が「同じ内容の地震情報」を指すか。"""
    if a.time_epoch is not None and b.time_epoch is not None:
        if abs(a.time_epoch - b.time_epoch) > TIME_TOLERANCE_SEC:
            return False
    if a.hypocenter != b.hypocenter:
        return False
    if abs(a.magnitude - b.magnitude) >= 0.05:
        return False
    if a.depth != b.depth or a.max_scale != b.max_scale:
        return False
    # 都道府県名が取得元から得られていない（空キー）場合は、震度分布の
    # 照合を最大震度のみにとどめる（取得元の欠落で重複判定を外さないため）。
    if any(pref == "" for pref, _ in a.pref_scales | b.pref_scales):
        return True
    return a.pref_scales == b.pref_scales


class QuakeContentDedupe:
    """
    通知済みの地震情報の内容指紋を保持し、同じ内容の再通知を判定する。
    保持は件数・時間とも有限（max_items件、ttl_sec秒）。
    """

    def __init__(self, max_items: int = 200, ttl_sec: float = 6 * 3600):
        self._max_items = max_items
        self._ttl_sec = ttl_sec
        self._items: deque = deque()          # (QuakeFingerprint, recorded_monotonic)

    def _purge(self, now: float) -> None:
        while self._items and (
            now - self._items[0][1] > self._ttl_sec or len(self._items) > self._max_items
        ):
            self._items.popleft()

    def is_duplicate(self, fp: QuakeFingerprint, now: float) -> bool:
        self._purge(now)
        return any(fingerprints_match(fp, old) for old, _ in self._items)

    def record(self, fp: QuakeFingerprint, now: float) -> None:
        self._purge(now)
        self._items.append((fp, now))


# ===============================
# 取得元
# ===============================
@dataclass
class SourceItem:
    """取得元の一覧の1件（詳細は fetch_detail で取得する）。"""
    key: str                      # 取得元内で一意な識別子（既知判定に使う）
    title: str                    # 情報の題名
    reported_at: datetime | None  # 発表時刻
    url: str                      # 詳細の取得先


class QuakeFallbackSource:
    """代替取得元の基底クラス。list_items と fetch_detail を実装する。"""
    name = "base"
    label = "base"          # 通知フッター等に出す表示名
    # この取得元の一覧取得の最小間隔（秒）。None ならコントローラの poll_seconds を使う。
    # 設定した場合、コントローラは一覧取得の間隔がこの値を下回らないよう待つ。
    poll_seconds: float | None = None

    async def list_items(self, session) -> list[SourceItem]:
        raise NotImplementedError

    async def fetch_detail(self, session, item: SourceItem) -> dict | None:
        """P2P形式のdictを返す。取得・変換できなければ None。"""
        raise NotImplementedError


class JmaJsonSource(QuakeFallbackSource):
    """気象庁HP JSON（bosai/quake/data/list.json と詳細JSON）。"""
    name = "jma_json"
    label = "気象庁HP（JSON）"

    async def list_items(self, session) -> list[SourceItem]:
        async with session.get(
            JMA_QUAKE_LIST_URL, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
        if not isinstance(data, list):
            raise RuntimeError("list.json の形式が想定外です")
        items = []
        for it in data:
            if not isinstance(it, dict):
                continue
            ttl = _text(it.get("ttl"))
            fname = _text(it.get("json"))
            if ttl not in QUAKE_INFO_TITLES or not fname:
                continue
            items.append(SourceItem(
                key=fname, title=ttl,
                reported_at=_parse_dt(it.get("rdt")),
                url=JMA_QUAKE_DETAIL_BASE + fname,
            ))
        return items

    async def fetch_detail(self, session, item: SourceItem) -> dict | None:
        async with session.get(
            item.url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            detail = await resp.json(content_type=None)
        return jma_quake_to_p2p(detail, list_title=item.title, item_key=item.key)


async def _read_body_limited(resp, max_bytes: int) -> bytes:
    """
    レスポンス本文を最大 max_bytes まで読み切って返す。上限を超えたら RuntimeError。

    【2026-10-06 修正】以前は `await resp.content.read(max_bytes + 1)` で読んでいたが、
    aiohttp の StreamReader.read(n) は「その時点で届いている分（最大n）」しか返さないため、
    本文が複数のTCPチャンクに分かれて届くと途中で切れた本文を返していた
    （jma_xml の「unclosed token」エラーの原因）。iter_chunked で最後まで読む。
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise RuntimeError("応答が想定より大きいため破棄しました")
        chunks.append(chunk)
    return b"".join(chunks)


class JmaXmlSource(QuakeFallbackSource):
    """気象庁防災情報XML（随時フィード eqvol.xml と各電文XML）。"""
    name = "jma_xml"
    label = "気象庁XML"

    @staticmethod
    async def _read_limited(resp) -> bytes:
        try:
            body = await _read_body_limited(resp, XML_MAX_BYTES)
        except RuntimeError:
            raise RuntimeError("XMLが想定より大きいため破棄しました")
        # 気象庁の電文XML・フィードは DTD/ENTITY 宣言を使わない。外部からの
        # 入力をパースするため、実体参照の展開による資源枯渇（Billion laughs
        # 等）を避ける目的で、これらを含むXMLは安全のため破棄する。
        head = body[:4096].upper()
        if b"<!DOCTYPE" in head or b"<!ENTITY" in body.upper():
            raise RuntimeError("DTD/ENTITY宣言を含むXMLのため破棄しました")
        return body

    async def list_items(self, session) -> list[SourceItem]:
        async with session.get(
            JMA_XML_FEED_URL, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            raw = await self._read_limited(resp)
        root = ET.fromstring(raw)
        items = []
        for entry in root.iter():
            if not (isinstance(entry.tag, str) and entry.tag.rsplit("}", 1)[-1] == "entry"):
                continue
            fields = {ch.tag.rsplit("}", 1)[-1]: ch for ch in entry if isinstance(ch.tag, str)}
            title = _text(fields["title"].text) if "title" in fields else ""
            if title not in QUAKE_INFO_TITLES:
                continue
            link = fields.get("link")
            url = link.attrib.get("href", "") if link is not None else ""
            key = _text(fields["id"].text) if "id" in fields else url
            if not url or not key:
                continue
            updated = _parse_dt(fields["updated"].text) if "updated" in fields else None
            items.append(SourceItem(key=key, title=title, reported_at=updated, url=url))
        return items

    async def fetch_detail(self, session, item: SourceItem) -> dict | None:
        async with session.get(
            item.url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            raw = await self._read_limited(resp)
        root = ET.fromstring(raw)
        return jma_quake_to_p2p(_xml_to_dict(root), list_title=item.title, item_key=item.key)


async def _get_json_limited(session, url: str, max_bytes: int = QUAKE_ONE_MAX_BYTES):
    """HTTP GET してJSONを返す（サイズ上限つき。想定外に大きい応答は破棄する）。"""
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        raw = await _read_body_limited(resp, max_bytes)
    return json.loads(raw)


class QuakeOneSource(QuakeFallbackSource):
    """
    Quake.One Static API（http://files.quake.one/）。
      list.json                     : 最新の地震（{"objects": [{EventID, ReportDateTime, ...}]}）
      :EventID/info.json            : 震源要素・最大震度・コメント
      :EventID/smallScalePoints.json: 細分区域別の震度と区域の重心（GeoJSON）
    同じ EventID でも続報で ReportDateTime が変わるため、既知判定のキーは
    「EventID@ReportDateTime」にする。
    """
    name = "quake_one"
    label = "Quake.One"

    def __init__(self, poll_seconds: float | None = None):
        # list.json の取得間隔。下限は QUAKE_ONE_MIN_POLL_SECONDS（10回/分）
        self.poll_seconds = max(
            QUAKE_ONE_MIN_POLL_SECONDS,
            float(poll_seconds) if poll_seconds is not None else QUAKE_ONE_MIN_POLL_SECONDS,
        )

    async def list_items(self, session) -> list[SourceItem]:
        data = await _get_json_limited(session, f"{QUAKE_ONE_BASE}/list.json")
        objs = data.get("objects") if isinstance(data, dict) else data
        if not isinstance(objs, list):
            raise RuntimeError("list.json の形式が想定外です")
        items = []
        for o in objs:
            if not isinstance(o, dict):
                continue
            eid = _text(o.get("EventID"))
            rdt = _clean_text(o.get("ReportDateTime"), 40)
            if not _QUAKE_ONE_EVENT_ID_RE.match(eid):
                continue
            items.append(SourceItem(
                key=f"{eid}@{rdt}", title="震源・震度に関する情報",
                reported_at=_parse_dt(rdt), url=f"{QUAKE_ONE_BASE}/{eid}",
            ))
        return items

    async def fetch_detail(self, session, item: SourceItem) -> dict | None:
        info, small = await asyncio.gather(
            _get_json_limited(session, f"{item.url}/info.json"),
            _get_json_limited(session, f"{item.url}/smallScalePoints.json"),
        )
        # list.json の EventID と info.json の EventID が食い違う応答は採用しない
        if _text(info.get("EventID") if isinstance(info, dict) else "") != item.key.split("@", 1)[0]:
            raise RuntimeError("info.json の EventID が list.json と一致しません")
        return quake_one_to_p2p(info, small, item_key=item.key)


SOURCE_FACTORIES = {
    JmaJsonSource.name: JmaJsonSource,
    JmaXmlSource.name: JmaXmlSource,
    QuakeOneSource.name: QuakeOneSource,
}


def build_sources(names: list[str], quake_one_poll_seconds: float | None = None) -> list[QuakeFallbackSource]:
    """
    設定名（QUAKE_FAILOVER_SOURCES）の並び順で取得元を作る。未知の名前は警告して無視。
    quake_one_poll_seconds: Quake.One の list.json の取得間隔（秒。下限6秒＝10回/分）。
    """
    sources: list[QuakeFallbackSource] = []
    for n in names:
        factory = SOURCE_FACTORIES.get(n)
        if factory is None:
            logger.warning(
                f"QUAKE_FAILOVER_SOURCES に未知の取得元 {n!r} が指定されています"
                f"（指定できる値: {', '.join(SOURCE_FACTORIES)}）。無視します"
            )
            continue
        if not any(isinstance(s, factory) for s in sources):
            sources.append(
                factory(quake_one_poll_seconds) if factory is QuakeOneSource else factory()
            )
    return sources


# ===============================
# 制御
# ===============================
class QuakeFailoverController:
    """
    フォールバック状態（set_active）の間だけ、取得元を順に試して新着の
    地震情報を handler(data, source_label) へ渡す。
    """

    def __init__(self, get_session, handler, sources: list[QuakeFallbackSource],
                 poll_seconds: float, is_closed_fn, known_max: int = 500,
                 catchup_minutes: float = 30, now_fn=None):
        """
        get_session  : aiohttp.ClientSession を返す関数（Cogのセッションを使う）
        handler      : async def handler(data: dict, source_label: str) -> None
        sources      : 優先順の取得元
        poll_seconds : フォールバック中のポーリング間隔
        is_closed_fn : Bot終了判定
        catchup_minutes : 発表から何分以内の情報だけを通知対象にするか（既定30分）。
            フォールバックは障害時にしか取得しないため、初回の取得時には取得元の
            一覧に「起動後〜障害発生前に P2P で通知済みの過去の地震」が大量に
            含まれる。これらを新着として再通知しないよう、発表がこの時間より
            古い（または発表時刻が不明な）情報は通知せず既知にする。
            障害が長引いてこれより前の情報が取り残される場合は、この値を増やす
            （P2P_FAILOVER_CATCHUP_MINUTES）。重複照合の保持（6時間）より短くすること。
        now_fn : 現在時刻（JSTのaware datetime）を返す関数。テスト用（既定は実時刻）
        """
        self._get_session = get_session
        self._handler = handler
        self._sources = sources
        self._poll_seconds = poll_seconds
        self._is_closed = is_closed_fn
        self._active = asyncio.Event()
        self._known: OrderedDict = OrderedDict()      # 既知（通知済み・起動時既存・処理済み）の key
        self._detail_failures: dict[str, int] = {}    # 詳細取得に失敗した回数（key単位）
        self._known_max = known_max
        self._baseline_done = False
        self._catchup = timedelta(minutes=catchup_minutes)
        self._now_fn = now_fn or (lambda: datetime.now(JST))
        self._start_time = self._now_fn()
        self._last_list_at: dict[str, float] = {}      # 取得元名 -> 直近の一覧取得の時刻（monotonic）
        self._next_sleep = poll_seconds                # 次のポーリングまでの待ち時間

    async def _list_items(self, src: QuakeFallbackSource, session) -> list[SourceItem]:
        """
        一覧取得。src.poll_seconds が設定されている取得元は、前回の一覧取得から
        その秒数が経過するまで待ってから取得する（Quake.One の 10回/分 の上限を守る）。
        """
        interval = getattr(src, "poll_seconds", None)
        if interval:
            wait = self._last_list_at.get(src.name, float("-inf")) + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_list_at[src.name] = time.monotonic()
        return await src.list_items(session)

    # -- 状態 --
    @property
    def is_active(self) -> bool:
        return self._active.is_set()

    def set_active(self, active: bool) -> None:
        """P2PWebSocketHub のフォールバック状態変化コールバックとして登録する（同期関数）。"""
        if active:
            if not self._active.is_set():
                logger.warning(
                    "QuakeFailover: 代替取得を開始します（取得元: "
                    f"{', '.join(s.name for s in self._sources) or 'なし'}、"
                    f"間隔 {self._poll_seconds:g} 秒）"
                )
            self._active.set()
        else:
            if self._active.is_set():
                logger.info("QuakeFailover: 代替取得を停止します（P2P WebSocket 復旧）")
            self._active.clear()

    def _remember(self, key: str) -> None:
        self._known[key] = True
        while len(self._known) > self._known_max:
            self._known.popitem(last=False)

    # -- 起動時の既知情報の記録 --
    async def baseline(self) -> None:
        """起動時に各取得元の現在の一覧を既知として記録する（通知しない）。"""
        session = self._get_session()
        if session is None:
            return
        done_any = False
        for src in self._sources:
            try:
                for it in await self._list_items(src, session):
                    self._remember(f"{src.name}:{it.key}")
                done_any = True
            except Exception as e:
                logger.warning(f"QuakeFailover: {src.name} の起動時一覧取得に失敗しました: {e}")
        self._baseline_done = done_any
        if done_any:
            logger.info(f"QuakeFailover: 起動時の既知情報を {len(self._known)} 件記録しました")

    # -- 1回分のポーリング --
    async def poll_once(self) -> int:
        """
        最初に一覧を取得できた取得元で新着を処理する。通知（handler呼び出し）した
        件数を返す。全取得元が失敗した場合は 0。
        """
        session = self._get_session()
        if session is None:
            return 0
        for src in self._sources:
            try:
                items = await self._list_items(src, session)
            except Exception as e:
                logger.warning(f"QuakeFailover: {src.name} の一覧取得に失敗（次の取得元を試します）: {e}")
                continue
            # 実際に使った取得元の間隔で次回まで待つ（Quake.One は6秒、他は poll_seconds）
            self._next_sleep = getattr(src, "poll_seconds", None) or self._poll_seconds
            return await self._process(src, items, session)
        self._next_sleep = self._poll_seconds
        logger.warning("QuakeFailover: すべての取得元で一覧取得に失敗しました")
        return 0

    async def _process(self, src: QuakeFallbackSource, items: list[SourceItem], session) -> int:
        fresh = []
        now = self._now_fn()
        skipped_old = 0
        for it in items:
            if f"{src.name}:{it.key}" in self._known:
                continue
            # 発表が古い（または発表時刻が不明な）情報は通知せず既知にする。
            # 通常は障害前に P2P で通知済みの過去の地震であり、再通知を避ける。
            if it.reported_at is None or it.reported_at < now - self._catchup:
                self._remember(f"{src.name}:{it.key}")
                skipped_old += 1
                continue
            # 起動時の既知情報を記録できていない場合は、起動より古い情報を対象外にする
            if not self._baseline_done and it.reported_at < self._start_time - timedelta(minutes=2):
                self._remember(f"{src.name}:{it.key}")
                continue
            fresh.append(it)
        if skipped_old:
            logger.info(
                f"QuakeFailover: {src.name} の発表が{self._catchup.total_seconds() / 60:g}分より古い "
                f"{skipped_old} 件は、通知済みとみなして通知しません"
            )
        # 古い順（震度速報 → 震源 → 各地の震度 の発表順）に処理する
        fresh.sort(key=lambda i: i.reported_at or datetime.min.replace(tzinfo=JST))
        notified = 0
        for it in fresh[:MAX_NOTIFY_PER_CYCLE]:
            try:
                data = await src.fetch_detail(session, it)
            except Exception as e:
                # 詳細の取得失敗は既知にせず、次回のポーリングで再試行する。
                # ただし恒久的な失敗（404等）を毎回リクエストし続けないよう、
                # MAX_DETAIL_ATTEMPTS 回で断念して既知にする。
                fkey = f"{src.name}:{it.key}"
                n = self._detail_failures.get(fkey, 0) + 1
                self._detail_failures[fkey] = n
                if n >= MAX_DETAIL_ATTEMPTS:
                    self._remember(fkey)
                    self._detail_failures.pop(fkey, None)
                    logger.warning(
                        f"QuakeFailover: {src.name} の詳細取得に {n} 回失敗したため断念します ({it.key}): {e}"
                    )
                else:
                    logger.warning(
                        f"QuakeFailover: {src.name} の詳細取得に失敗 ({it.key}, {n}/{MAX_DETAIL_ATTEMPTS}回目): {e}"
                    )
                continue
            self._detail_failures.pop(f"{src.name}:{it.key}", None)
            self._remember(f"{src.name}:{it.key}")
            if data is None:
                logger.info(f"QuakeFailover: {src.name} {it.key} は通知対象の内容がないためスキップしました")
                continue
            try:
                await self._handler(data, src.label)
                notified += 1
            except Exception:
                logger.error(f"QuakeFailover: handler でエラー:\n{traceback.format_exc()}")
        if len(fresh) > MAX_NOTIFY_PER_CYCLE:
            logger.warning(
                f"QuakeFailover: 新着が {len(fresh)} 件あるため、今回は {MAX_NOTIFY_PER_CYCLE} 件のみ処理します"
            )
        return notified

    # -- 常駐ループ --
    async def run(self) -> None:
        """Botの起動中ずっと動かすタスク。フォールバック中のみ poll_once を繰り返す。"""
        try:
            await self.baseline()
        except Exception:
            logger.error(f"QuakeFailover: baseline でエラー:\n{traceback.format_exc()}")
        while not self._is_closed():
            try:
                await self._active.wait()
                while self._active.is_set() and not self._is_closed():
                    await self.poll_once()
                    await asyncio.sleep(self._next_sleep)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"QuakeFailover: ループでエラー:\n{traceback.format_exc()}")
                await asyncio.sleep(self._poll_seconds)