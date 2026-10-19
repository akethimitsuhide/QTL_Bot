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
