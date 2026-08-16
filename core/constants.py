"""
core/constants.py
==================
複数のCogで共有される「辞書定数」と「状態を持たない純粋関数」を集約するモジュール。

【設計方針】
- ここに置くのは「震度→色」のような固定マッピングと、
  引数だけで結果が決まる純粋関数（_tsunami_height_key 等）のみ。
- Bot の状態（self.xxx）に依存する関数は置かない
  （それらは各 Cog または core/audio.py の Mixin に置く）。

【bot.py からの移行元】
元 bot.py の region_map 読み込み〜 _tsunami_height_key() 定義まで
（Bot初期化ブロックの直前）に相当。
"""
import os
import json
import logging
from collections import defaultdict

logger = logging.getLogger("QTLBot")


def load_region_map() -> dict:
    """region_map.json を読み込む（緊急地震速報の警報地域名 → 表示用地域名マッピング）。"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # core/ の1階層上（プロジェクトルート）を見る
    path = os.path.join(base_dir, "..", "region_map.json")

    if not os.path.exists(path):
        logger.warning("region_map.json が存在しません")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.error(f"region_map.json 読み込みエラー: {e}")

    return {}


REGION_MAP = load_region_map()


def load_prefecture_map() -> dict:
    """
    prefecture_map.json を読み込む（緊急地震速報や震度情報で用いる区域名 →
    都道府県名 マッピング）。

    震度速報（ScalePrompt）は points[].addr に区域名（例: "熊本県天草・芦北"）
    ではなく気象庁の「緊急地震速報や震度情報で用いる区域の名称」が入るため、
    このマップで都道府県名に変換したうえで表示・読み上げに用いる。
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, "..", "prefecture_map.json")

    if not os.path.exists(path):
        logger.warning("prefecture_map.json が存在しません")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.error(f"prefecture_map.json 読み込みエラー: {e}")

    return {}


PREFECTURE_MAP = load_prefecture_map()


def load_pref_region_map() -> dict:
    """
    pref_region_map.json を読み込む（府県予報区名 → 地方予報区名 マッピング）。

    緊急地震速報の警報対象地域は REGION_MAP で府県予報区名（例:
    「北海道道南」「青森」）に変換されるが、対象地域が広範囲（多くの
    都道府県）にわたる大規模なEEWでは、府県予報区名を全て列挙すると
    通知・読み上げの文言が過度に長くなる。そのため、対象の府県予報区数が
    一定件数以上の場合、このマップでさらに地方予報区名（例:「北海道」
    「東北」）へ集約する（詳細は cogs/eew.py の該当ロジック参照）。
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, "..", "pref_region_map.json")

    if not os.path.exists(path):
        logger.warning("pref_region_map.json が存在しません")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.error(f"pref_region_map.json 読み込みエラー: {e}")

    return {}


PREF_REGION_MAP = load_pref_region_map()

# 地方予報区の表示順序（北から南）。
# collapse_regions_if_needed() で複数の地方予報区を表示する際、
# 単純な文字列ソートではなく地理的に自然な順序で並べるために使う。
REGION_DISPLAY_ORDER = [
    "北海道", "東北", "関東", "伊豆諸島", "小笠原",
    "北陸", "甲信", "東海", "近畿", "中国", "四国", "九州",
    "奄美群島", "沖縄",
]


def collapse_regions_if_needed(pref_regions: set, threshold: int) -> list:
    """
    府県予報区名の集合（REGION_MAP 変換後、例:「北海道道南」「青森」）を
    受け取り、件数が threshold 件以上の場合は PREF_REGION_MAP でさらに
    地方予報区名（例:「北海道」「東北」）へ集約して返す。
    threshold 未満の場合は、元の府県予報区名を地理的に自然な順序
    （北から南）で返す（変換自体は行わない）。

    戻り値は表示用に整列済みのリスト。
    """
    if not pref_regions:
        return []

    if len(pref_regions) < threshold:
        # 集約せず、元の府県予報区名をそのまま使う。
        # REGION_DISPLAY_ORDER に無い名称（想定外の府県予報区名）は
        # 末尾にアルファベット順で追加する。
        known = [r for r in REGION_DISPLAY_ORDER if r in pref_regions]
        unknown = sorted(pref_regions - set(REGION_DISPLAY_ORDER))
        return known + unknown

    # 地方予報区へ集約する。PREF_REGION_MAP に無いキーは
    # 元の名前をそのまま使う（変換漏れで情報が消えるのを防ぐ）。
    collapsed = {PREF_REGION_MAP.get(r, r) for r in pref_regions}
    known = [r for r in REGION_DISPLAY_ORDER if r in collapsed]
    unknown = sorted(collapsed - set(REGION_DISPLAY_ORDER))
    return known + unknown

# 震度コード → 表示文字列
INT_MAP = {
    -1: "不明",
    10: "1",
    20: "2",
    30: "3",
    40: "4",
    45: "5弱",
    46: "推定5弱以上",
    50: "5強",
    55: "6弱",
    60: "6強",
    70: "7",
}

# 震度コード → Embed 色
SHINDO_COLORS = {
    -1: 0x62626B,
    0:  0x62626B,
    10: 0x3098BD,
    20: 0x4CD0A7,
    30: 0xF6CB51,
    40: 0xFF9939,
    45: 0xE52A18,
    50: 0xC31B1B,
    55: 0xA30A6B,
    60: 0x86046E,
    70: 0x54068E,
}

# 長周期地震動階級 → Embed 色
LG_COLORS = {
    "1":  0xF2CF57,
    "2":  0xD73B15,
    "3":  0xB3091D,
    "4":  0x890076,
    "不明": 0x62626B,
}

# 津波区分コード → 表示文字列
TSUNAMI_MAP = {
    "None":         "津波の心配なし",
    "Unknown":      "津波の有無は不明",
    "Checking":     "津波の有無を調査中",
    "NonEffective": "若干の海面変動（被害の心配なし）",
    "Watch":        "津波注意報",
    "Warning":      "津波警報",
    "MajorWarning": "大津波警報",
}

# 津波区分コードの深刻度順（重い順）。
# 複数エリアの中から最も深刻な区分を選ぶソートキーや、
# 「前回よりエスカレーションしたか」の比較に使う
# （例: cogs/quake.py の EWS 重複再生防止、cogs/tsunami.py の表示順ソート）。
TSUNAMI_GRADE_ORDER = ["MajorWarning", "Warning", "Watch", "Unknown"]

# 地震情報の発表種別 → 表示文字列
QUAKE_TYPE_MAP = {
    "ScalePrompt":          "震度速報",
    "Destination":          "震源に関する情報",
    "ScaleAndDestination":  "震度・震源に関する情報",
    "DetailScale":          "各地の震度に関する情報",
    "Foreign":              "遠地地震に関する情報",
    "Other":                "その他の情報",
}


def _tsunami_height_key(height_str: str) -> float:
    """
    津波予想高さ文字列を数値に変換してソートキーとして返す（降順ソート用）。
    例: "10m以上" → 10.001, "5m" → 5.0, "0.2m未満" → 0.199
    """
    if not height_str:
        return -1.0
    s = height_str.strip()
    if s in ("巨大", "重大な津波"):
        return 100.0
    if s == "高い":
        return 5.0
    if s in ("微弱", "若干"):
        return 0.1
    import re
    m = re.search(r'(\d+(?:\.\d+)?)', s)
    if not m:
        return 0.0
    v = float(m.group(1))
    if "以上" in s:
        v += 0.001
    elif "未満" in s:
        v -= 0.001
    return v


def format_tsunami_height_value(raw: str) -> str:
    """
    JMA tsunami JSON の MaxHeight.TsunamiHeight（文字列）を表示用に整形する。

    2026-08: cogs/tsunami.py（1100行超）の肥大化対策として、TsunamiCogの
    静的メソッド _format_tsunami_height_value から移設（cogs/tsunami.py
    側には後方互換の薄いラッパーを残してある）。_tsunami_height_key
    （ソートキー生成）と同じ「津波高さ文字列」を扱う関数のため、
    同じモジュールにまとめる方が自然という判断もある。

    値の例:
      "<0.2"  → "0.2m未満"
      ">10" / "≧10" → "10m以上"
      "5"     → "5m"
      "巨大" / "高い" / "若干" 等の定性語 → そのまま返す（m を付けない）
    """
    if not raw:
        return ""
    s = raw.strip()

    # 定性的な表現はそのまま（末尾に m を付けると "巨大m" のような誤表記になるため）
    QUALITATIVE = {"巨大", "高い", "若干", "微弱", "不明"}
    if s in QUALITATIVE:
        return s

    if s.startswith("<"):
        return f"{s[1:]}m未満"
    if s.startswith(">") or s.startswith("\u2267") or s.startswith("\u2265"):
        return f"{s[1:]}m以上"

    # 数値のみ（"5", "10" 等）
    import re
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return f"{s}m"

    # それ以外の未知のフォーマットはそのまま返す（mを付けて誤解させない）
    return s


# 震度コード（scale値）の降順リスト。
# 「最大震度から1階級下まで」を抽出する処理や、
# 各地の震度リストを震度の高い順に表示する処理で使用する。
# 46（推定5弱以上）は45（5弱）と70（7）の間の特殊値のため、
# 表示上は45と同じ「階級」として扱う。
SCALE_ORDER = [70, 60, 55, 50, 46, 45, 40, 30, 20, 10]


def scale_one_level_down(scale_val: int) -> int | None:
    """
    SCALE_ORDER 上で scale_val の「1つ下の震度階級」を返す。
    46（推定5弱以上）は45（5弱）と同階級として扱うため、
    46の1つ下は45ではなく40（震度4）を返す。
    見つからない場合は None を返す。
    """
    order_for_step = [70, 60, 55, 50, 45, 40, 30, 20, 10]
    normalized = 45 if scale_val == 46 else scale_val
    if normalized not in order_for_step:
        return None
    idx = order_for_step.index(normalized)
    if idx + 1 >= len(order_for_step):
        return None
    return order_for_step[idx + 1]


def group_points_by_scale(points: list[dict]) -> dict[int, list[str]]:
    """
    P2P地震情報 API の points 配列（震度観測点情報）を、
    scale（震度コード）ごとに addr（観測点名・区域名）のリストへ
    グルーピングする。同一 scale 内での重複addrは排除する。

    戻り値は {scale値: [addr, ...]} の辞書（挿入順は元データ順）。
    """
    grouped: dict[int, list[str]] = defaultdict(list)
    seen: dict[int, set] = defaultdict(set)
    for p in points or []:
        scale_val = p.get("scale")
        addr = p.get("addr")
        if scale_val is None or not addr:
            continue
        if addr in seen[scale_val]:
            continue
        seen[scale_val].add(addr)
        grouped[scale_val].append(addr)
    return grouped
