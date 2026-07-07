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
