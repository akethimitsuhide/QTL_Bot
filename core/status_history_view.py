"""
core/status_history_view.py
============================
/qtl_status の履歴オプション用に、履歴レコードを Discord 表示用の行へ
整形する純粋関数群。

入力は core.quake_history_log.display_record / core.eew_history_log.display_record
で time_display を付与済みのレコード。Discord や設定には依存しない
（単体テストを環境変数なしで実行できる）。
"""
from typing import Mapping

# Embed description の上限（4096）に対する余裕を持たせた上限
_MAX_DESCRIPTION_CHARS = 3900


def _scale_label(code, int_map: Mapping[int, str]) -> str:
    if code is None:
        return "不明"
    return int_map.get(code, "不明")


def format_quake_line(record: dict, int_map: Mapping[int, str]) -> str:
    """地震情報レコード1件を1行にする。"""
    mag = record.get("magnitude", -1)
    depth = record.get("depth", -1)
    mag_str = f"M{mag}" if mag not in (None, -1) else "M調査中"
    depth_str = f"約{depth}km" if depth not in (None, -1) else "調査中"
    title = record.get("title") or "地震情報"
    return (
        f"**{record.get('time_display', '不明')}**　{record.get('hypocenter_name', '調査中')}"
        f"　最大震度{_scale_label(record.get('max_scale'), int_map)}"
        f"　{mag_str}　深さ{depth_str}　（{title}）"
    )


def format_eew_line(record: dict) -> str:
    """EEW レコード1件を1行にする（PLUM法は震源要素が無いためその旨を表示）。"""
    serial = "最終報" if record.get("is_final") else f"第{record.get('serial', 1)}報"
    intensity = record.get("max_intensity_label") or "不明"
    if record.get("is_assumption"):
        source_part = "PLUM法（震源要素なし）"
    else:
        mag = record.get("magnitude")
        depth = record.get("depth")
        mag_str = f"M{mag}" if mag is not None else "M不明"
        depth_str = f"約{depth}km" if depth is not None else "不明"
        source_part = f"{mag_str}　深さ{depth_str}"
    return (
        f"**{record.get('time_display', '不明')}**　{record.get('hypocenter_name', '不明')}"
        f"　予想最大震度{intensity}　{source_part}　（{serial}）"
    )


def join_lines(lines: list[str]) -> str:
    """行を改行で連結し、Embed の上限を超える場合は末尾を省略する。"""
    if not lines:
        return "履歴がありません。"
    text = "\n".join(lines)
    if len(text) <= _MAX_DESCRIPTION_CHARS:
        return text
    return text[:_MAX_DESCRIPTION_CHARS] + "\n（長いため一部省略）"
