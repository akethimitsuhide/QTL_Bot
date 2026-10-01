"""
core/tsunami_speech.py - 津波情報の読み上げ文生成・状態判定・重複防止（純関数）

【設計方針】
  - Discord・aiohttp・core.config に一切依存しない（環境変数なしで単体テスト
    できるようにするため。core/constants.py は core.config をimportするので使わない）。
  - 時刻は引数（now: 秒）で受け取り、内部で time.time() を呼ばない（テスト容易性）。
  - 各ハンドラ（P2P形式・気象庁形式）は、受け取ったデータをまず AlertState に
    変換してからここの関数に渡す。

【二重読み上げ防止の考え方】
  P2P地震情報と気象庁では津波イベントのIDが異なるため、IDではなく
  「（予報区名, 警報区分）の状態」そのものを比較する（AlertGate）。
  状態が前回読み上げた内容から変わっていなければ、情報源が違っても読み上げない。
  読み上げとEWS信号音は同じ判定結果（Decision）で動かす。

【区分キー】 "MajorWarning"(大津波警報) / "Warning"(津波警報) /
             "Watch"(津波注意報) / "Forecast"(津波予報)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

GRADE_ORDER = ["MajorWarning", "Warning", "Watch", "Forecast"]  # 先頭ほど深刻
GRADE_LABEL = {
    "MajorWarning": "大津波警報",
    "Warning": "津波警報",
    "Watch": "津波注意報",
    "Forecast": "津波予報",
}
# 読み上げ優先度（PriorityQueue: 小さいほど優先）
GRADE_PRIORITY = {"MajorWarning": 1, "Warning": 1, "Watch": 2, "Forecast": 3}

ALERT_STATE_TTL_SEC = 6 * 3600        # 状態を保持する時間
CROSS_SOURCE_WINDOW_SEC = 90          # 別情報源から同一最上位区分が来た場合に重複とみなす時間
DEFAULT_MAX_AREAS = 3                 # 読み上げる地域名の最大数（超過分は「等」）
EVENT_GATE_TTL_SEC = 6 * 3600
DEFAULT_COOLDOWN_SEC = 120


def _rank(grade: str) -> int:
    try:
        return GRADE_ORDER.index(grade)
    except ValueError:
        return len(GRADE_ORDER)


# ===============================
# 区分の正規化
# ===============================
def grade_from_kind_name(kind_name: str, kind_code: str = "") -> Optional[str]:
    """
    気象庁JSONの Category.Kind.Name から区分キーを返す。
    津波なし・解除（Code "00" 等）は None（＝警報等が出ていない）。
    「大津波警報」は「津波警報」を部分文字列に含むため、必ず先に判定する。
    """
    name = kind_name or ""
    if kind_code == "00" or "津波なし" in name or "解除" in name:
        return None
    if "大津波警報" in name:
        return "MajorWarning"
    if "津波警報" in name:
        return "Warning"
    if "津波注意報" in name:
        return "Watch"
    if "津波予報" in name:
        return "Forecast"
    return None


def grade_from_p2p(grade: str) -> Optional[str]:
    """P2P地震情報(code=552)の grade から区分キーを返す。Unknown は津波予報扱い。"""
    if grade in ("MajorWarning", "Warning", "Watch"):
        return grade
    if grade == "Unknown":
        return "Forecast"
    return None


# ===============================
# 状態
# ===============================
@dataclass
class AlertState:
    """ある時点の津波警報・注意報・予報の状態（情報源を問わない共通表現）。"""
    areas: dict[str, str] = field(default_factory=dict)     # 予報区名 → 区分キー
    heights: dict[str, str] = field(default_factory=dict)   # 予報区名 → 予想高さ（生値）
    arrival: dict[str, str] = field(default_factory=dict)   # 予報区名 → 到達予想時刻（生値）
    immediate: set[str] = field(default_factory=set)        # 「ただちに来襲」の予報区
    source: str = ""

    @property
    def empty(self) -> bool:
        return not self.areas

    @property
    def top_grade(self) -> Optional[str]:
        if not self.areas:
            return None
        return min(self.areas.values(), key=_rank)

    def areas_of(self, grade: str) -> list[str]:
        """指定区分の予報区を、到達予想が早い順（時刻不明は後ろ）→名前順で返す。"""
        names = [n for n, g in self.areas.items() if g == grade]
        return sorted(names, key=lambda n: (self.arrival.get(n) or "~", n))


def state_from_jma_forecast(detail: dict) -> AlertState:
    """気象庁の津波予報（Body.Tsunami.Forecast.Item[]）を AlertState に変換する。"""
    state = AlertState(source="jma")
    body = (detail or {}).get("Body", {}) or {}
    items = ((body.get("Tsunami", {}) or {}).get("Forecast", {}) or {}).get("Item", []) or []
    for it in items:
        name = (it.get("Area", {}) or {}).get("Name")
        kind = (it.get("Category", {}) or {}).get("Kind", {}) or {}
        grade = grade_from_kind_name(kind.get("Name", ""), str(kind.get("Code", "")))
        if not name or grade is None:
            continue
        state.areas[name] = grade
        first = it.get("FirstHeight", {}) or {}
        if first.get("ArrivalTime"):
            state.arrival[name] = first["ArrivalTime"]
        if "ただちに" in (first.get("Condition") or ""):
            state.immediate.add(name)
        h = (it.get("MaxHeight", {}) or {}).get("TsunamiHeight")
        if h:
            state.heights[name] = str(h)
    return state


def state_from_p2p_tsunami(data: dict) -> AlertState:
    """P2P地震情報 津波予報(code=552)を AlertState に変換する。cancelled は空の状態。"""
    state = AlertState(source="p2p")
    if (data or {}).get("cancelled"):
        return state
    for a in (data or {}).get("areas", []) or []:
        name = a.get("name")
        grade = grade_from_p2p(a.get("grade", ""))
        if not name or grade is None:
            continue
        state.areas[name] = grade
        first = a.get("firstHeight", {}) or {}
        if first.get("arrivalTime"):
            state.arrival[name] = first["arrivalTime"]
        if a.get("immediate") or "ただちに" in (first.get("condition") or ""):
            state.immediate.add(name)
        desc = (a.get("maxHeight", {}) or {}).get("description")
        if desc:
            state.heights[name] = str(desc)
    return state


# ===============================
# 状態変化の分類
# ===============================
@dataclass
class Change:
    kind: str                       # none / new / added / escalation / downgrade / cancel
    prev_top: Optional[str] = None
    added: list[str] = field(default_factory=list)


def classify_change(prev: Optional[AlertState], curr: AlertState) -> Change:
    """
    前回読み上げ時の状態 prev と今回の状態 curr を比較して変化を分類する。
    高さの修正や、最上位区分が変わらない区分の入れ替えだけの続報は "none"（無音）。
    """
    prev_empty = prev is None or prev.empty
    if prev_empty and curr.empty:
        return Change("none")
    if prev_empty:
        return Change("new")
    if curr.empty:
        return Change("cancel", prev_top=prev.top_grade)
    pt, ct = prev.top_grade, curr.top_grade
    if _rank(ct) < _rank(pt):
        return Change("escalation", prev_top=pt)
    if _rank(ct) > _rank(pt):
        return Change("downgrade", prev_top=pt)
    # 最上位区分は同じ: 注意報以上の予報区が新たに加わったか
    added = [n for n, g in curr.areas.items()
             if n not in prev.areas and _rank(g) <= _rank("Watch")]
    if added:
        return Change("added", prev_top=pt, added=sorted(added))
    return Change("none", prev_top=pt)


# ===============================
# 読み上げ用の整形
# ===============================
def format_height_for_speech(raw: str) -> str:
    """
    予想高さ・観測高さの生値を読み上げ用に整形する（読まない場合は空文字列）。
      "3" / "3m" → "3メートル", "10m超" → "10メートル超", ">10" → "10メートル以上",
      "巨大"/"高い" → そのまま, "<0.2"・"若干"・"微弱"・不明 → 空文字列
    """
    s = (raw or "").strip()
    if not s or s.startswith("<") or s in ("若干", "微弱", "不明", "欠測", "観測中"):
        return ""
    if s in ("巨大", "高い"):
        return s
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return ""
    suffix = "以上" if (s.startswith((">", "≧", "≥")) or "以上" in s) else ("超" if "超" in s else "")
    return f"{m.group()}メートル{suffix}"


def _height_sort_value(raw: str) -> float:
    s = (raw or "").strip()
    if s in ("巨大",):
        return 100.0
    if s in ("高い",):
        return 5.0
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m or s.startswith("<"):
        return 0.0
    v = float(m.group())
    return v + (0.001 if ("以上" in s or "超" in s or s.startswith((">", "≧", "≥"))) else 0.0)


def format_areas_for_speech(names: list[str], max_areas: int = DEFAULT_MAX_AREAS) -> str:
    if not names:
        return ""
    head = "、".join(names[:max_areas])
    return head + ("等" if len(names) > max_areas else "")


def format_time_for_speech(iso: str) -> str:
    """ISO8601（JST表記）の時刻文字列から「H時MM分頃」を作る。解釈できなければ空文字列。"""
    m = re.search(r"(?:T|\s)(\d{2}):(\d{2})", iso or "") or re.fullmatch(r"(\d{1,2}):(\d{2})", (iso or "").strip())
    if not m:
        return ""
    return f"{int(m.group(1))}時{m.group(2)}分頃"


def _top_height(state: AlertState, grade: str) -> str:
    hs = [state.heights[n] for n in state.areas_of(grade) if n in state.heights]
    if not hs:
        return ""
    return format_height_for_speech(max(hs, key=_height_sort_value))


def build_alert_speech(curr: AlertState, change: Change,
                       max_areas: int = DEFAULT_MAX_AREAS) -> tuple[str, int]:
    """
    警報・注意報・予報の読み上げ文と優先度を返す（change.kind が none の場合は ("", 0)）。
    """
    if change.kind == "none":
        return "", 0

    if change.kind == "cancel":
        pt = change.prev_top
        if pt in ("MajorWarning", "Warning"):
            return f"{GRADE_LABEL[pt]}が解除されました。", 2
        if pt == "Watch":
            return "津波注意報が解除されました。", 2
        return "津波の心配はなくなりました。", 3

    top = curr.top_grade
    label = GRADE_LABEL[top]
    areas = format_areas_for_speech(curr.areas_of(top), max_areas)

    if change.kind == "added":
        added_top = [n for n in change.added if curr.areas[n] == top] or change.added
        return (f"{label}の対象地域が追加されました。{format_areas_for_speech(added_top, max_areas)}。",
                GRADE_PRIORITY[top])

    if change.kind == "escalation":
        lead = f"{GRADE_LABEL[change.prev_top]}が{label}に切り替わりました。"
        priority = 1
    elif change.kind == "downgrade":
        return f"{GRADE_LABEL[change.prev_top]}は{label}に切り替わりました。", 2
    else:  # new
        lead = f"{label}が発表されました。"
        priority = GRADE_PRIORITY[top]

    text = f"{lead}{areas}。"
    height = _top_height(curr, top)
    if height:
        text += f"予想される高さは最大{height}です。"
    imm = [n for n in curr.areas_of(top) if n in curr.immediate]
    if imm:
        text += f"{imm[0]}ではただちに津波が来襲すると予測されています。"
    if top in ("MajorWarning", "Warning"):
        text += "ただちに避難してください。"
    return text, priority


# ===============================
# 二重読み上げ防止ゲート（警報・注意報・予報）
# ===============================
@dataclass
class Decision:
    speak: bool
    change: Change
    ews: bool = False               # EWS信号音を鳴らすべきか
    text: str = ""
    priority: int = 0


class AlertGate:
    """
    警報・注意報・予報の読み上げ/EWSを、情報源（P2P・気象庁）をまたいで
    一元的に判定する。状態は1系統のみ保持する（津波イベントは同時に1件を前提）。
    """

    def __init__(self, max_areas: int = DEFAULT_MAX_AREAS):
        self._state: Optional[AlertState] = None
        self._ts: float = 0.0
        self._max_areas = max_areas

    def decide(self, curr: AlertState, now: float) -> Decision:
        prev = self._state
        if prev is not None and now - self._ts > ALERT_STATE_TTL_SEC:
            prev = None
        change = classify_change(prev, curr)

        if change.kind == "none":
            self._merge(prev, curr, now)
            return Decision(False, change)

        # 補助ルール: 別情報源から短時間内に同じ最上位区分が来た場合は、予報区名の
        # 表記ゆれ等で状態が食い違っていても二重に読まない（状態は統合して保持）
        if (change.kind in ("new", "added") and prev is not None and not prev.empty
                and prev.source != curr.source and now - self._ts <= CROSS_SOURCE_WINDOW_SEC
                and prev.top_grade == curr.top_grade):
            self._merge(prev, curr, self._ts)
            return Decision(False, change)

        text, priority = build_alert_speech(curr, change, self._max_areas)
        ews = change.kind in ("new", "escalation") and curr.top_grade in ("Warning", "MajorWarning")
        self._state, self._ts = curr, now
        return Decision(True, change, ews=ews, text=text, priority=priority)

    def _merge(self, prev: Optional[AlertState], curr: AlertState, ts: float) -> None:
        """prev と curr の予報区を統合して保持する（区分は深刻な方を優先）。"""
        if prev is None or prev.empty:
            self._state, self._ts = curr, ts
            return
        merged = AlertState(source=prev.source)
        for src in (prev, curr):
            for n, g in src.areas.items():
                if n not in merged.areas or _rank(g) < _rank(merged.areas[n]):
                    merged.areas[n] = g
            merged.heights.update(src.heights)
            merged.arrival.update(src.arrival)
            merged.immediate |= src.immediate
        self._state, self._ts = merged, ts


# ===============================
# イベント単位のゲート（観測・沖合・満潮時刻情報）
# ===============================
class EventGate:
    """
    イベントID単位で「読み上げるべきか」を判定する。
      mode="rise"  : 初回、または値が上昇した時のみ（沿岸観測の最大階級）
      mode="change": 初回、または値が変化し、かつcooldown経過後（満潮時刻情報）
    値の上昇（rise）はcooldownの対象外。
    """

    def __init__(self, mode: str = "rise", cooldown_sec: float = DEFAULT_COOLDOWN_SEC):
        if mode not in ("rise", "change"):
            raise ValueError(mode)
        self._mode = mode
        self._cooldown = cooldown_sec
        self._last: dict[str, tuple[object, float]] = {}

    def decide(self, key: str, value, now: float, rising: Optional[bool] = None) -> bool:
        self._last = {k: v for k, v in self._last.items() if now - v[1] <= EVENT_GATE_TTL_SEC}
        prev = self._last.get(key)
        if prev is None:
            self._last[key] = (value, now)
            return True
        prev_value, prev_ts = prev
        if self._mode == "rise":
            if rising if rising is not None else value > prev_value:
                self._last[key] = (value, now)
                return True
            return False
        # change
        if value != prev_value and now - prev_ts >= self._cooldown:
            self._last[key] = (value, now)
            return True
        return False


# ===============================
# 観測情報の判定・読み上げ文
# ===============================
# 沿岸観測点の階級（値が大きいほど深刻）。gis_render の色分けと同じ区分。
OBS_TIER = {"unknown": 0, "forecast": 1, "watch": 2, "warning": 3, "major_warning": 4}
_OBS_TIER_PRIORITY = {"major_warning": 1, "warning": 1, "watch": 2, "forecast": 3, "unknown": 3}


def obs_grade_from_height(raw_height: str) -> str:
    """
    津波観測点の観測値文字列から階級を判定する（地図の色分けと通知の読み上げで共用）。
      5m以上 → major_warning / 1m以上5m未満 → warning / 0.1m以上1m未満 → watch /
      微弱・弱・低い・観測中・"<0.2"等 → forecast / 欠測・不明・空 → unknown
    """
    if not raw_height:
        return "unknown"
    s = str(raw_height).strip()
    if s in ("微弱", "弱", "低い", "観測中"):
        return "forecast"
    if s in ("欠測", "不明"):
        return "unknown"
    if s.startswith("<"):
        return "forecast"
    try:
        value = float(s)
    except (TypeError, ValueError):
        return "unknown"
    if value >= 5:
        return "major_warning"
    if value >= 1:
        return "warning"
    if value >= 0.1:
        return "watch"
    return "forecast"


def obs_height_level(raw_height: str) -> tuple:
    """
    観測値の生値を「階級＋実測値」の比較可能なタプルに変換する
    （2026-09-29追加）。タプルの大小比較がそのまま「値の大小」に対応する
    （辞書式比較: まず階級 OBS_TIER、次に実測値）。StationHeightTracker
    が前報との比較（上昇判定）に使うほか、同じ階級判定基準を再利用する
    場所（cogs/tsunami.py の _obs_sort_key 等）からも参照できるよう
    公開関数にしている。実測値が無い（微弱・観測中・欠測等）場合は
    同一階級内で最も低い値（-1.0）として扱う。
    """
    tier = OBS_TIER.get(obs_grade_from_height(raw_height), 0)
    try:
        value = float(raw_height)
    except (TypeError, ValueError):
        value = -1.0
    return (tier, value)


class StationHeightTracker:
    """
    観測点ごとの直近（前報）の観測値を保持し、今回の値が前回発表時より
    上昇しているかどうかを判定する（2026-09-29追加）。

    【背景】沿岸観測点の通知文に付ける「↑」（上昇中を示す）マークを、
    以前は MaxHeight.Condition == "観測中" であれば無条件に付けていたが、
    「観測中」は「まだピークが確定していない」ことを示すだけで、実際に
    上昇中かどうかまでは分からない。前報の同じ観測点の値と比較すること
    で、より正確に「上昇中」を判定する。

    【判定方法】obs_height_level() で階級＋実測値のタプルに変換し、
    前回保持していた値より大きければ「上昇」とみなす。前報のデータが
    無い（このトラッカーで初めて見る観測点＝新規追加観測点や、初回の
    発表）場合は、比較のしようが無いため False（上昇と判定しない）。
    値が同じ・下降した場合も False。

    【キーの単位】呼び出し側が任意のキー文字列を渡す。津波イベントを
    またいで別の津波の観測点と混同しないよう、cogs/tsunami.py 側では
    「EventID:観測点コード」を渡す想定（EventIDが無い場合は観測点名に
    フォールバック）。
    """

    def __init__(self, ttl_sec: float = EVENT_GATE_TTL_SEC):
        self._ttl = ttl_sec
        self._last: dict[str, tuple[tuple, float]] = {}  # key -> (level, timestamp)

    def check_and_update(self, key: str, raw_height: str, now: float) -> bool:
        """
        key の前回レベルと raw_height のレベルを比較し、上昇していれば
        True を返す。呼び出しのたびに、比較結果によらず今回の値で
        状態を更新する（＝常に「直前の1報」と比較する。EventGate の
        mode="rise" のように「最後に上昇した時点」とは比較しない）。
        """
        self._last = {k: v for k, v in self._last.items() if now - v[1] <= self._ttl}
        level = obs_height_level(raw_height)
        prev = self._last.get(key)
        self._last[key] = (level, now)
        if prev is None:
            return False
        return level > prev[0]


def station_raw_height(station: dict) -> str:
    """Station.MaxHeight から生の観測値文字列を取り出す（無ければ「欠測」）。"""
    max_h = station.get("MaxHeight", {}) or {}
    for key in ("Condition", "TsunamiHeight"):
        if max_h.get(key):
            return str(max_h[key])
    if max_h.get("value") is not None:
        return str(max_h["value"])
    return "欠測"


def build_coastal_observation_speech(detail: dict) -> Optional[tuple[str, int, int]]:
    """
    「津波観測に関する情報」（VTSE51）の読み上げ。
    戻り値: (読み上げ文, 優先度, 最大階級 OBS_TIER値)。観測点が無ければ None。
    最大階級の観測点（同階級なら高さが大きい方）を1つ選んで読む。
    """
    body = (detail or {}).get("Body", {}) or {}
    items = ((body.get("Tsunami", {}) or {}).get("Observation", {}) or {}).get("Item", []) or []
    best = None  # (tier, height_value, area, station_name, raw)
    for it in items:
        area = (it.get("Area", {}) or {}).get("Name") or ""
        for st in it.get("Station", []) or []:
            raw = station_raw_height(st)
            grade = obs_grade_from_height(raw)
            key = (OBS_TIER[grade], _height_sort_value(raw))
            if best is None or key > best[0]:
                best = (key, grade, area, st.get("Name", ""), raw)
    if best is None or best[1] == "unknown":
        return None
    _, grade, area, name, raw = best
    where = f"{area}の{name}" if area else name
    h = format_height_for_speech(raw)
    if h:
        text = f"津波観測に関する情報。{where}で、{h}を観測しました。"
    else:
        text = f"津波観測に関する情報。{where}で、津波を観測しています。"
    return text, _OBS_TIER_PRIORITY[grade], OBS_TIER[grade]


def _offshore_station_name_for_speech(name: str) -> str:
    """
    沖合観測点名を読み上げ用に整える（2026-10-01追加）。
    全角英数字を半角にし（例: 「岩手沖６０ｋｍＡ」→「岩手沖60kmA」）、
    同じ距離の観測点を区別する末尾の識別記号（A/B）を除き、「km」は
    TTSが確実に読める「キロメートル」にする（例: 「岩手沖60キロメートル」）。
    """
    n = unicodedata.normalize("NFKC", name or "").strip()
    n = re.sub(r"(?<=km)[A-Za-z]$", "", n)
    return n.replace("km", "キロメートル")


def build_offshore_observation_speech(detail: dict,
                                      max_areas: int = DEFAULT_MAX_AREAS) -> Optional[tuple[str, int]]:
    """
    「沖合の津波観測に関する情報」（VTSE52）の読み上げ。観測点が無ければ None。

    形式: 「沖合の津波観測に関する情報。{H時MM分頃}、次の沖合で津波を観測しました。
    {観測点名}。」。時刻は発表時刻（Head.ReportDateTime、無ければTargetDateTime）。
    観測点が max_areas を超える場合は、今回新たに追加された観測点（FirstHeight/
    MaxHeight の Revise が「追加」）を優先して max_areas 件までとし、超過分は「等」。
    """
    detail = detail or {}
    body = detail.get("Body", {}) or {}
    items = ((body.get("Tsunami", {}) or {}).get("Observation", {}) or {}).get("Item", []) or []
    stations = [st for it in items for st in (it.get("Station", []) or [])]
    if not stations:
        return None

    def _is_new(st: dict) -> bool:
        return any((st.get(k, {}) or {}).get("Revise") == "追加" for k in ("FirstHeight", "MaxHeight"))

    ordered = [st for st in stations if _is_new(st)] + [st for st in stations if not _is_new(st)]
    names: list[str] = []
    for st in ordered:
        n = _offshore_station_name_for_speech(st.get("Name", ""))
        if n and n not in names:
            names.append(n)

    head = detail.get("Head", {}) or {}
    t = format_time_for_speech(head.get("ReportDateTime") or head.get("TargetDateTime") or "")
    text = "沖合の津波観測に関する情報。"
    if t:
        text += f"{t}、"
    text += "次の沖合で津波を観測しました。"
    if names:
        text += f"{format_areas_for_speech(names, max_areas)}。"
    return text, 2


def offshore_station_keys(detail: dict) -> frozenset:
    """
    「沖合の津波観測に関する情報」（VTSE52）に含まれる観測点の集合を返す
    （EventGate mode="change" の変化検知用の値。2026-10-01追加）。
    観測点コード（Station.Code）を使い、無ければ観測点名で代用する。同じ
    津波の続報で観測点が追加された場合だけ集合が変わり、既存観測点の値の
    修正・更新（高さの訂正等）では変わらない。
    """
    body = (detail or {}).get("Body", {}) or {}
    items = ((body.get("Tsunami", {}) or {}).get("Observation", {}) or {}).get("Item", []) or []
    keys = set()
    for it in items:
        for st in it.get("Station", []) or []:
            key = st.get("Code") or st.get("Name")
            if key:
                keys.add(str(key))
    return frozenset(keys)


def build_tide_time_speech(detail: dict) -> Optional[tuple[str, int, tuple[str, str]]]:
    """
    「各地の満潮時刻・津波到達予想時刻に関する情報」の読み上げ。
    戻り値: (読み上げ文, 優先度, 変化検知用の値=(予報区名, 到達予想時刻))。
    津波注意報以上の予報区のうち、到達予想が最も早い予報区を読む。該当が無ければ None。
    """
    state = state_from_jma_forecast(detail)
    cands = [n for n, g in state.areas.items()
             if _rank(g) <= _rank("Watch") and state.arrival.get(n)]
    if not cands:
        return None
    earliest = min(cands, key=lambda n: (state.arrival[n], _rank(state.areas[n]), n))
    when = format_time_for_speech(state.arrival[earliest])
    if not when:
        return None
    text = f"津波の到達予想時刻を発表しました。最も早い到達は、{when}、{earliest}です。"
    if earliest in state.immediate:
        text += f"{earliest}ではただちに津波が来襲すると予測されています。"
    top = state.top_grade
    priority = 2 if top in ("MajorWarning", "Warning") else 3
    return text, priority, (earliest, state.arrival[earliest])