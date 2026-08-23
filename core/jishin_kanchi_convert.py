"""
core/jishin_kanchi_convert.py
===============================
P2P地震情報「地震感知情報」（code=9611）の信頼度（confidence）を
表示用のレベル・ラベルへ変換する純粋関数群。

【仕様の出典】
P2P地震情報 API v2 の地震感知情報エンドポイント仕様書（ユーザー提供）:

    confidence*  number
    P2P地震情報 Beta3 における信頼度（0～1）
    0: 非表示、0.97015: レベル1、0.96774: レベル2、
    0.97024: レベル3、0.98052: レベル4。
    値は適合率 (precision) です。

    area_confidences.<*>.confidence  number
    信頼度（0～1） P2P地震情報 Beta3 においては、
    0未満: F、0.0以上0.2未満: E、0.2以上0.4未満: D、
    0.4以上0.6未満: C、0.6以上0.8未満: B、0.8以上: A です。

【全体信頼度（confidence）が単純な範囲判定ではない理由】
地域ごとの信頼度（area_confidences側）は0.0〜0.8の範囲で単調に
A〜Fへ区分されるのに対し、全体の信頼度（confidence）は
0.97015（レベル1）→0.96774（レベル2）→0.97024（レベル3）→
0.98052（レベル4）という、数値の大小がレベル番号と一致しない
離散値の羅列になっている。これは何らかの分類モデルが各レベルごとに
出力する適合率（precision）の実測値をそのまま定数として使っている
ためと考えられ、単純な範囲（大小関係）で判定できない。そのため
本モジュールでは「最も近い既知の定数値」に丸める方式で判定する。
"""

# 全体信頼度（confidence）の既知の定数値 → レベル番号。
# 大小関係とレベル番号が一致しない点に注意（モジュールdocstring参照）。
_CONFIDENCE_LEVEL_VALUES: dict[float, int] = {
    0.97015: 1,
    0.96774: 2,
    0.97024: 3,
    0.98052: 4,
}

# 既知の定数値とみなす許容誤差。API側の浮動小数点表現の丸め誤差を
# 吸収するための小さな値。
#
# 【2026-08 修正】既知の定数値同士の間隔が極めて狭い
# （例: レベル1=0.97015 と レベル3=0.97024 の差はわずか0.00009）ため、
# 「最初に誤差の範囲内で一致した値を採用する」という単純な実装では、
# 本来レベル3であるべき値がレベル1に誤判定されるバグがあった
# （dict の走査順で先に定義されているレベル1と誤って一致してしまう）。
# そのため、判定方式を「全既知値の中で最も差が小さいものを選ぶ」
# 最近傍マッチングに変更し、その最小差が _CONFIDENCE_MATCH_EPSILON
# 以内の場合のみ採用する（許容誤差は浮動小数点の丸め誤差のみを
# 吸収する目的のため、既知値同士の最小間隔0.00009より十分小さい
# 値に設定する）。
_CONFIDENCE_MATCH_EPSILON = 1e-5

# 「0: 非表示」を判定するしきい値。0そのものだけでなく、限りなく0に
# 近い値（浮動小数点の丸め誤差等）も非表示扱いにする。
_CONFIDENCE_HIDDEN_THRESHOLD = 0.01

# レベル番号 → 表示ラベル（仕様書のユーザー指定表記そのまま）。
LEVEL_LABELS: dict[int, str] = {
    1: "レベル1（高い）",
    2: "レベル2（やや高い）",
    3: "レベル3（やや低い）",
    4: "レベル4（低い）",
}


def confidence_to_level(confidence: float | None) -> int | None:
    """
    地震感知情報全体の信頼度（confidence）を、レベル番号（1〜4）へ
    変換する。0に近い値（非表示指定）または未知の値の場合は
    None を返す（呼び出し元はNoneの場合、通知を抑制することを推奨）。
    """
    if confidence is None:
        return None
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return None

    if confidence < _CONFIDENCE_HIDDEN_THRESHOLD:
        return None

    # 最も差が小さい既知値を探す（"最初に許容誤差内で一致したもの"
    # ではなく、全既知値の中の最近傍を選ぶ。理由はモジュール上部の
    # _CONFIDENCE_MATCH_EPSILON のコメント参照）。
    best_level = None
    best_diff = float("inf")
    for known_value, level in _CONFIDENCE_LEVEL_VALUES.items():
        diff = abs(confidence - known_value)
        if diff < best_diff:
            best_diff = diff
            best_level = level

    if best_diff <= _CONFIDENCE_MATCH_EPSILON:
        return best_level

    # 未知の値。安全側に倒してNone（非表示）とはせず、レベル4
    # （最も慎重な扱い＝低い信頼度）として扱う方が実用上望ましいと
    # 判断し、フォールバックする。
    return 4


def confidence_to_level_label(confidence: float | None) -> str | None:
    """confidence_to_level() の結果を、そのまま表示用ラベル文字列に変換する。"""
    level = confidence_to_level(confidence)
    if level is None:
        return None
    return LEVEL_LABELS.get(level, f"レベル{level}")


def area_confidence_display(display: str | None, confidence: float | None) -> str:
    """
    地域ごとの信頼度表示（"A"〜"F"）を返す。

    API仕様上、area_confidences.<code>.display には既に "A"〜"F" の
    文字列が入っているため、通常はそれをそのまま使えば良い。
    display が欠落している場合のみ、confidence の数値から
    フォールバックとして算出する（仕様書の範囲表に基づく）。
    """
    if display:
        return display
    if confidence is None:
        return "F"
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return "F"
    if c < 0:
        return "F"
    elif c < 0.2:
        return "E"
    elif c < 0.4:
        return "D"
    elif c < 0.6:
        return "C"
    elif c < 0.8:
        return "B"
    else:
        return "A"


# 地域信頼度の表示順（高い順）。ソートキーとして使う。
AREA_CONFIDENCE_ORDER = ["A", "B", "C", "D", "E", "F"]


def area_confidence_sort_key(display: str) -> int:
    """area_confidence_displayの結果をソート用の数値キーに変換する（A=0が最優先）。"""
    try:
        return AREA_CONFIDENCE_ORDER.index(display)
    except ValueError:
        return len(AREA_CONFIDENCE_ORDER)  # 未知の表示値は最後尾に


def format_p2p_time(raw: str | None) -> str:
    """
    P2P地震情報APIの時刻文字列（"2026/08/13 06:21:29.999" 形式、
    ミリ秒部分は無い場合もある）を、ミリ秒を除いた
    "2026/08/13 06:21:29" 形式に整形する。

    パースできない場合は入力をそのまま返す（表示を欠落させないため）。
    """
    if not raw:
        return "不明"
    # ミリ秒部分（"."以降）を単純に切り落とす。
    # 例: "2026/08/13 06:21:29.999" -> "2026/08/13 06:21:29"
    return raw.split(".")[0]
