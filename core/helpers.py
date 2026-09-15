"""
core/helpers.py
================
複数のCog（quake, tsunami, volcano, usgs 等）から共通で呼ばれる、
「状態を持たない」純粋ヘルパー関数を集約するモジュール。

【設計方針】
元 bot.py ではこれらは QuakeTsunamiCog のメソッド（self.safe_int(...) 等）
として定義されていたが、実体は self の状態を一切使わない純粋関数だった。
Cog分割にあたり、単純な module-level 関数として独立させる。

これにより各 Cog からは
    from core.helpers import safe_int, safe_float, safe_bool, truncate_embed_description, format_jma_time
のように import するだけで良く、Mixin継承やCog間参照を考える必要がない
（最もシンプルな共有方法）。

【呼び出し側の変更点】
  旧: self.safe_int(x)                          → 新: safe_int(x)
  旧: self._truncate_embed_description(text)     → 新: truncate_embed_description(text)
  旧: self.format_jma_time(raw)                  → 新: format_jma_time(raw)

【bot.py からの移行元】
元 bot.py の safe_float() 〜 format_jma_time() 定義（旧 1513〜1591行目付近）。
"""
import logging
from datetime import datetime

from core.constants import INT_MAP

logger = logging.getLogger("QTLBot")


def safe_float(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def safe_int(value) -> int:
    try:
        return int(str(value).replace("km", "").strip())
    except Exception:
        return 0


def safe_bool(value) -> bool:
    """bool / "true" / "1" / 1 など複数形式の真偽値を統一的に bool に変換する"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value).lower() in ("true", "1", "yes")


def truncate_embed_description(
    text: str,
    max_chars: int = 4096,
    suffix: str = "\n\n（長すぎるため一部省略）"
) -> str:
    """
    Discord Embed の description フィールドを正確に切り詰める。

    Discord API は Embed.description で最大 4096 文字をサポート。
    Unicode マルチバイト文字の途中で切られてエラーになることを防ぐ。

    Parameters
    ----------
    text : str
        切り詰め対象のテキスト
    max_chars : int
        最大文字数（デフォルト4096）。Discord API 制限に合わせて設定
    suffix : str
        切り詰め時に追加するサフィックス

    Returns
    -------
    str : 切り詰めされたテキスト（suffix込みで max_chars 以内）
    """
    if len(text) <= max_chars:
        return text

    available = max_chars - len(suffix)

    if available <= 0:
        logger.warning(
            f"truncate_embed_description: suffix が長すぎます "
            f"(suffix={len(suffix)} chars, max={max_chars}). "
            f"suffix なしで切り詰めます"
        )
        return text[:max_chars]

    return text[:available] + suffix


def format_jma_time(raw: str) -> str:
    """
    気象庁 JSON の各種時刻文字列を「YYYY年M月D日H時MM分頃」形式に変換する。

    対応フォーマット:
      2026/04/18 13:20    (P2P 形式)
      2026-04-18T13:20:00 (ISO 8601)
      2026-04-18 13:20    (スペース区切り)
    変換できない場合は入力をそのまま返す。
    """
    if not raw or raw in ("不明", "調査中"):
        return raw
    try:
        normalized = raw[:16].replace("T", " ").replace("-", "/")
        dt = datetime.strptime(normalized, "%Y/%m/%d %H:%M")
        return (
            f"{dt.year}年{dt.month}月{dt.day}日"
            f"{dt.hour}時{dt.minute:02d}分頃"
        )
    except Exception:
        return raw


def format_latlon(latitude: float, longitude: float) -> str:
    """
    10進数の緯度経度を「度分秒（小数第1位までの秒）」形式の文字列に変換する。

    例: 32.561056, 130.378667 -> "32°33′39.8、130°22′43.2"

    震源情報が存在しない場合（-200）等、変換できない入力の場合は
    空文字列を返す。
    """
    def _to_dms(value: float) -> str:
        deg = int(abs(value))
        minutes_full = (abs(value) - deg) * 60
        minutes = int(minutes_full)
        seconds = (minutes_full - minutes) * 60
        return f"{deg}°{minutes:02d}′{seconds:.1f}"

    try:
        if latitude is None or longitude is None:
            return ""
        if latitude == -200 or longitude == -200:
            return ""
        return f"{_to_dms(latitude)}、{_to_dms(longitude)}"
    except Exception:
        return ""


# core.constants.INT_MAP のキー（10,20,...,70）→ 表示ラベル（"1","2",...,"7"）の
# 逆引き。core/gis_render.py（GIS地図描画、2026-09-13追加）で、地図上の
# 観測点マーカーに入れる短いラベル（"1"〜"7"、5弱/5強は"5-"/"5+"、
# 6弱/6強は"6-"/"6+"）を作るために使う。
_SHINDO_SHORT_LABELS: dict[int, str] = {
    10: "1", 20: "2", 30: "3", 40: "4",
    45: "5-", 50: "5+", 55: "6-", 60: "6+", 70: "7",
}

# INT_MAP の値（"1","5弱"等の表示用文字列）→ キー（10,45等の数値コード）の
# 逆引き。EEWのWarnArea（Shindo1/Shindo2）は数値コードではなく表示用
# 文字列で震度を表すため、SHINDO_COLORS 等の数値キー辞書と対応付ける際に
# 必要になる。
_SHINDO_LABEL_TO_CODE: dict[str, int] = {v: k for k, v in INT_MAP.items()}


def shindo_code_from_label(label: str) -> int:
    """
    "1" "5弱" "6強" "不明" 等の震度表示文字列を、core.constants.INT_MAP /
    SHINDO_COLORS で使われている数値コード（10, 45, 60, -1 等）に変換する。
    該当なし（未知の文字列）の場合は -1（不明）を返す。
    """
    return _SHINDO_LABEL_TO_CODE.get(label, -1)


def shindo_short_label(code: int) -> str:
    """
    震度コード（core.constants.INT_MAP のキー。10, 45, 60 等）を、
    GIS地図の観測点マーカーに入れる短い表示ラベルに変換する
    （例: 10→"1", 45→"5-", 50→"5+", 60→"6+"）。該当なしの場合は "?"。
    """
    return _SHINDO_SHORT_LABELS.get(code, "?")
