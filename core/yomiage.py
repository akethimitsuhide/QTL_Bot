"""
core/yomiage.py - 読み上げ文テンプレート（.env.yomiage）の展開・検証（純関数）

【設計方針】
  - Discord・aiohttp・core.config に一切依存しない（環境変数なしで単体テスト
    できるようにするため。core/tsunami_speech.py と同じ方針）。
  - テンプレートの既定値（EEW_DEFAULTS）は、テンプレート導入前にコードへ直接
    書かれていた読み上げ文と同じ結果になるように書いてある。.env.yomiage が
    無い・空の場合は、これまでと全く同じ文言を読み上げる。
  - 設定ミス（未知のタグ・[ ] の対応ミス）は例外にせず、警告ログを出して
    そのキーだけ既定値に戻す（Bot は止めない）。

【書式】（Zero Quake の読み上げテンプレートと同じ考え方）
  {タグ}   : 値に置き換える。値が無い場合は空文字になる。
  [ ... ]  : 中に含まれる {タグ} のうち1つでも空なら、[ ] ごと（中の文章も）
             省略する。全て値があれば [ ] だけ外して中身を読み上げる。
             入れ子にはできない。
  例: 「緊急地震速報。[{警報地域}では]強い揺れに警戒してください。」
      → 警報地域がある時だけ「○○では」が付く。
  ※ { } [ ] は書式用の記号のため、文章としては使えない。
"""

from __future__ import annotations

import re
from typing import Mapping, Optional

_TAG_RE = re.compile(r"\{([^{}]*)\}")
_SECTION_RE = re.compile(r"\[([^\[\]]*)\]")

# ===============================
# EEW（緊急地震速報）
# ===============================
# キー名は .env.yomiage の変数名から "YOMI_" を除いたもの。
EEW_TAGS = (
    "震源",          # 震央地名（不明なら「不明」）
    "最大震度",      # 予想最大震度（「5弱」など。不明なら空）
    "マグニチュード",  # 数値（PLUM法・不明なら空）
    "深さ",          # km の数値のみ（PLUM法・不明なら空）
    "警報地域",      # 強い揺れが予想される地域を「、」で連結（無ければ空）
    "報数",          # 第N報の N
    "種別",          # 「警報」または「予報」
    "最終報",        # 最終報なら「最終報」、それ以外は空
    "訓練",          # 訓練報なら「訓練報。」、それ以外は空
    "PLUM法",        # PLUM法なら「（PLUM法）」、それ以外は空
)

# 導入前のコード（cogs/eew.py の generate_and_speak_eew）の文言と同一。
EEW_DEFAULTS: dict[str, str] = {
    # 第1報（警報）
    "EEW_FIRST_WARN":
        "緊急地震速報。[{警報地域}では]強い揺れに警戒してください。"
        "{震源}で地震。推定最大震度{最大震度}。",
    # 第1報（予報・PLUM法含む）
    "EEW_FIRST_FORECAST":
        "{震源}で地震。推定最大震度{最大震度}。{PLUM法}",
    # 続報: 警報の対象地域が増えた時
    "EEW_UPDATE_WARN":
        "緊急地震速報。[{警報地域}では]強い揺れに警戒してください。",
    # 続報: 予想最大震度が変わった時
    "EEW_UPDATE_INTENSITY":
        "推定最大震度{最大震度}",
    # 続報: 震源（震央地名）が変わった時
    "EEW_UPDATE_HYPO":
        "{震源}で地震。推定最大震度{最大震度}。",
}


# ===============================
# 地震情報（P2P地震情報・気象庁の地震情報。cogs/quake.py）
# ===============================
QUAKE_TAGS = (
    "情報名",          # 「震度速報」「各地の震度に関する情報」など（テスト時は先頭に「【テスト】」）
    "発生時刻",        # 「8時20分頃」の形（時刻が分からない場合は空）
    "震源",            # 震央地名（不明なら「調査中」）
    "最大震度",        # 「4」「5弱」など（不明なら「不明」）
    "深さ",            # 「約10km」「ごく浅い」（不明なら「調査中」）。単位込み
    "マグニチュード",  # 「M6.1」（不明なら「調査中」）。M込み
    "津波",            # 国内への津波についての案内文（案内が無い場合は空）
    "観測都道府県",    # 震度速報で最大震度を観測した都道府県を「、」連結（不明なら「調査中」）
    "最大震度観測地点",  # 最大震度を観測した地点を「、」連結（不明なら「調査中」）
    "遠地地震文",      # 遠地地震の導入文（通常「海外で規模の大きな地震がありました。」、大規模噴火時は「遠地地震がありました。」）
)

# 導入前のコード（cogs/quake.py の notify_quake）の文言と同一。
# 津波の案内文は従来「 」（半角スペース）付きで連結していたため [ {津波} ] の形にしている。
QUAKE_DEFAULTS: dict[str, str] = {
    # 震度速報（震源・規模は未確定）
    "QUAKE_SCALE_PROMPT":
        "{情報名}。[{発生時刻}、]最大震度{最大震度}を、{観測都道府県}で観測しました。"
        "震源地・規模は調査中です。今後の情報に注意してください。",
    # 震源に関する情報（震度なし）
    "QUAKE_DESTINATION":
        "{情報名}。[{発生時刻}、]地震がありました。 震源地は {震源}。 深さは {深さ}。"
        " マグニチュードは {マグニチュード}。[ {津波}]",
    # 震度・震源に関する情報／各地の震度に関する情報（最大震度3以上）
    "QUAKE_SCALE_DEST_STRONG":
        "{情報名}。[{発生時刻}、]最大震度{最大震度}を観測する地震がありました。"
        " 震源地は {震源}。 震源の深さは {深さ}。 マグニチュードは {マグニチュード} と推定されます。"
        "この地震で、最大震度{最大震度}を、{最大震度観測地点}で観測しました。[ {津波}]",
    # 震度・震源に関する情報／各地の震度に関する情報（最大震度2以下）
    "QUAKE_SCALE_DEST":
        "{情報名}。[{発生時刻}、]最大震度{最大震度}を観測する地震がありました。[ {津波}]"
        " 震源地は {震源}。 震源の深さは {深さ}。 マグニチュードは {マグニチュード} と推定されます。",
    # 遠地地震に関する情報
    "QUAKE_FOREIGN":
        "{情報名}。[{発生時刻}、]{遠地地震文} 震源地は {震源}。 マグニチュードは {マグニチュード}。[ {津波}]",
    # その他の情報
    "QUAKE_OTHER":
        "{情報名}。[ {津波}] 震源地 {震源}。 最大震度 {最大震度}。 {マグニチュード}。",
}


# ===============================
# 津波（cogs/tsunami.py・core/tsunami_speech.py）
# ===============================
TSUNAMI_TAGS = (
    "情報名",        # 通知のタイトル（テスト通知では先頭に「【テスト】」）
    "区分",          # 「大津波警報」「津波警報」「津波注意報」「津波予報」など
    "地域",          # 対象の地域名（3つまでを「、」連結し、超えたら「等」）
    "解除文",        # 解除の案内文（気象庁の見出しがあればそれ、無ければ解除された区分から作った文）
    "気象庁見出し",   # 気象庁の発表見出し（解除時。無ければ空）
    "解除対象",      # 解除された区分（「津波警報」「津波注意報」。判別できなければ空）
    "発表時刻",      # 「17時43分頃」の形（沖合観測情報。不明なら空）
    "観測点",        # 沖合の観測点名（3つまで、超えたら「等」）
    "到達予想時刻",   # 最も早い到達予想時刻（「17時43分頃」）
    "予報区",        # 最も早く到達する予報区名
    "即時地域",      # 「ただちに津波が来襲する」と予測される地域名（無ければ空）
)

# 導入前のコードの文言と同一。
TSUNAMI_DEFAULTS: dict[str, str] = {
    # P2P地震情報（津波予報）: 対象地域と区分が分かる場合
    "TSUNAMI_P2P_ISSUED": "{地域}に{区分}が発表されました",
    # P2P地震情報（津波予報）: 対象地域・区分が分からない場合
    "TSUNAMI_P2P_ISSUED_PLAIN": "{情報名} が発表されました",
    # P2P地震情報（津波予報）: 解除
    "TSUNAMI_P2P_CANCEL": "津波情報が解除されました",
    # 気象庁（津波警報・注意報・予報）: 発表。{区分}は最大の区分
    "TSUNAMI_JMA_ISSUED": "{区分}が発表されました",
    # 気象庁（津波警報・注意報・予報）: 解除
    "TSUNAMI_JMA_CANCEL": "{解除文}",
    # 沖合の津波観測に関する情報
    "TSUNAMI_OFFSHORE":
        "沖合の津波観測に関する情報。[{発表時刻}、]次の沖合で津波を観測しました。[{観測点}。]",
    # 各地の満潮時刻・津波到達予想時刻に関する情報
    "TSUNAMI_TIDE_TIME":
        "津波の到達予想時刻を発表しました。最も早い到達は、{到達予想時刻}、{予報区}です。"
        "[{即時地域}ではただちに津波が来襲すると予測されています。]",
}


# ===============================
# 長周期地震動・その他（cogs/other.py・cogs/jishin_kanchi.py）
# ===============================
OTHER_TAGS = (
    "情報名",      # 「南海トラフ地震臨時情報」など、気象庁が発表した情報の名称
    "発生時刻",    # 「8時20分頃」（長周期地震動）。震源要素更新では「8時20分」（頃なし。不明なら「先程」）
    "震源",        # 震央地名
    "マグニチュード",  # 数値のみ（「M」は付かない）
    "深さ",        # 震源要素更新のお知らせの深さ（「10km」または「不明」）
    "最大階級",    # 最大長周期地震動階級（1〜4。不明なら「不明」）
    "情報種別",    # 南海トラフ地震臨時情報の種別（「巨大地震警戒」「巨大地震注意」）
    "信頼度",      # 地震感知情報の信頼度（「高」「中」など）
    "件数",        # 地震感知情報の感知報告の件数
)

# 導入前のコードの文言と同一。
OTHER_DEFAULTS: dict[str, str] = {
    # 長周期地震動に関する観測情報
    "OTHER_LONG_PERIOD":
        "長周期地震動に関する観測情報。[{発生時刻}に]発生した{震源}を震源とする"
        " マグニチュード{マグニチュード}の地震により、最大長周期地震動階級{最大階級}を観測しました。",
    # 後発地震注意情報など、気象庁のその他の情報（震源要素更新・南海トラフ以外）
    "OTHER_ADVISORY": "{情報名} が発表されました",
    # 顕著な地震の震源要素更新のお知らせ
    "OTHER_HYPO_UPDATE":
        "{発生時刻}頃の地震について、震源要素を更新しました。"
        " 震源地 {震源} マグニチュード M{マグニチュード} 深さ {深さ}",
    # 南海トラフ地震臨時情報（巨大地震警戒・巨大地震注意のみ読み上げ）
    "OTHER_NANKAI": "南海トラフ地震臨時情報。{情報種別}が発表されました。",
    # 地震感知情報（第一報のみ読み上げ）
    "OTHER_KANCHI": "地震感知情報。信頼度{信頼度}。{件数}件の感知報告があります。",
}

# ===============================
# USGS（cogs/usgs.py）
# ===============================
USGS_TAGS = (
    "発生時刻",      # 「2026年10月20日08時20分」（不明なら「不明」）
    "場所",          # USGSの震源地名
    "マグニチュード",  # 小数1桁の数値のみ（「M」は付かない）
)

USGS_DEFAULTS: dict[str, str] = {
    "USGS_QUAKE":
        "USGS地震情報。{発生時刻}、{場所}でマグニチュード{マグニチュード}の地震が発生しました。",
}

# ===============================
# 火山（cogs/volcano.py）
# ===============================
VOLCANO_TAGS = (
    "情報名",      # 火山情報の見出し
    "火山名",      # 火山の名称
    "警戒レベル",  # 噴火警戒レベル（1〜3）
    "警報名",      # 噴火警報の種別（「噴火警報（火口周辺）」など）
    "状況",        # 「発表」「切替」「解除」など（無い場合は「発表」）
)

VOLCANO_DEFAULTS: dict[str, str] = {
    # 火山情報（噴火警戒レベル1〜3のみ読み上げ）
    "VOLCANO_INFO": "火山情報。{情報名}。警戒レベル{警戒レベル}。",
    # 噴火速報
    "VOLCANO_ERUPTION_FLASH": "噴火速報。{火山名}で噴火が発生しました。",
    # 噴火警報
    "VOLCANO_ERUPTION_WARNING": "噴火警報。{火山名}で{警報名}が{状況}されました。",
}


def validate_template(template: str, allowed_tags) -> Optional[str]:
    """
    テンプレートの書式を検査し、問題があればその説明文を、問題なければ None を返す。
    検査内容: 未知のタグ / { } の対応ミス / [ ] の対応ミス・入れ子。
    """
    allowed = set(allowed_tags)
    unknown = sorted({t for t in _TAG_RE.findall(template) if t not in allowed})
    if unknown:
        return "未知のタグ: " + "・".join("{" + t + "}" for t in unknown)

    stripped = _TAG_RE.sub("", template)
    if "{" in stripped or "}" in stripped:
        return "{ } の対応が取れていません"

    depth = 0
    for ch in stripped:
        if ch == "[":
            depth += 1
            if depth > 1:
                return "[ ] は入れ子にできません"
        elif ch == "]":
            depth -= 1
            if depth < 0:
                return "[ ] の対応が取れていません"
    if depth != 0:
        return "[ ] の対応が取れていません"
    return None


def render(template: str, values: Mapping[str, str]) -> str:
    """
    テンプレートを展開する。[ ] 内に空のタグが1つでもあれば、その [ ] 全体を省略する。
    値が無い（values に無い）タグは空文字として扱う。
    """
    def _section(m: "re.Match[str]") -> str:
        inner = m.group(1)
        for tag in _TAG_RE.findall(inner):
            if not str(values.get(tag, "")):
                return ""
        return inner

    text = _SECTION_RE.sub(_section, template)
    return _TAG_RE.sub(lambda m: str(values.get(m.group(1), "")), text)


class TemplateSet:
    """
    キーごとの「既定テンプレート」と「ユーザー設定（.env.yomiage）」を持ち、
    検証済みのものだけを使って文章を作る。

    overrides: {キー: ユーザーが設定したテンプレート（未設定・空は None か ""）}
    warn     : 設定ミスの通知先（logger.warning など。1キーにつき生成時に1回だけ呼ぶ）
    """

    def __init__(self, defaults: Mapping[str, str], allowed_tags,
                 overrides: Mapping[str, Optional[str]], warn=None):
        self._defaults = dict(defaults)
        self._allowed = tuple(allowed_tags)
        self._active: dict[str, str] = {}
        self._custom: set[str] = set()
        for key, default in self._defaults.items():
            user = (overrides.get(key) or "").strip()
            template = default
            if user:
                problem = validate_template(user, self._allowed)
                if problem is None:
                    template = user
                    self._custom.add(key)
                elif warn:
                    warn(f"読み上げテンプレート YOMI_{key} を無視して既定の文言を使います（{problem}）: {user}")
            self._active[key] = template

    def is_custom(self, key: str) -> bool:
        return key in self._custom

    def render(self, key: str, values: Mapping[str, str]) -> str:
        """key のテンプレートを展開する。展開中の想定外エラーは既定の文言で作り直す。"""
        try:
            return render(self._active[key], values)
        except Exception:
            return render(self._defaults[key], values)


# ===============================
# EEW のタグ値の組み立て
# ===============================
def _digits(value) -> str:
    """'10km'・10・'10' → '10'。数値にできない・負の値は空文字。"""
    try:
        n = int(str(value).replace("km", "").strip())
    except (TypeError, ValueError):
        return ""
    return str(n) if n >= 0 else ""


def _magnitude(value) -> str:
    try:
        m = float(value)
    except (TypeError, ValueError):
        return ""
    if m <= 0:
        return ""
    return str(int(m)) if m == int(m) else str(m)


def build_eew_values(data: Mapping, warn_area_names: list[str]) -> dict[str, str]:
    """
    Wolfx形式のEEW辞書から、テンプレートに差し込むタグ値を作る。
    warn_area_names: 読み上げる警報地域名（地方予報区への集約などは呼び出し側で済ませる）
    """
    is_plum = bool(data.get("isAssumption", False))
    return {
        "震源": str(data.get("Hypocenter", "不明")),
        "最大震度": str(data.get("MaxIntensity", "")),
        "マグニチュード": "" if is_plum else _magnitude(data.get("Magnitude") or data.get("Magunitude")),
        "深さ": "" if is_plum else _digits(data.get("Depth")),
        "警報地域": "、".join(warn_area_names),
        "報数": str(data.get("Serial", "")),
        "種別": "警報" if data.get("isWarn", False) else "予報",
        "最終報": "最終報" if data.get("isFinal", False) else "",
        "訓練": "訓練報。" if data.get("isTraining", False) else "",
        "PLUM法": "（PLUM法）" if is_plum else "",
    }
