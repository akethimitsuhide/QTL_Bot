"""
core/quake_summary.py
=====================
日次・週次の「地震サマリー」の集計・スケジュール計算・Embed 組み立て（2026-10-27〜）。

【データ元】
core/quake_history_log.py が qtlbot.log に残す QUAKE_RECORD_V1（P2P地震情報 code=551 の通知内容）を
そのまま使う。新しい永続化先は作らない（軽量・低依存の方針）。したがって集計対象は
「QTL_Bot が通知した地震」のみで、QUAKE_MIN_SCALE 等のフィルターで通知しなかった地震や、
qtlbot.log のローテーションで消えた期間は含まれない（後者は Embed に注記する）。

【既存の週間/月間ダイジェスト（cogs/system.py）との違い】
ダイジェストは「通知種別ごとの件数」と配信成功率が中心。こちらは地震の中身
（最大震度・主な地震・震度別件数・日別の推移）を要約する。互いに独立して動く。

【同じ地震の統合】
1つの地震は、震度速報・震源に関する情報・各地の震度に関する情報の順に複数回通知され、
QUAKE_RECORD_V1 も通知ごとに1件ずつ残る。ここでは発生時刻（time_raw）が同じレコードを
1つの地震にまとめる。同じ分に別の地震が起きた場合は1件に統合される（実運用上まれ。許容）。
"""
import re
from collections import Counter
from datetime import datetime, timedelta

import discord

from core.constants import INT_MAP, SHINDO_COLORS
from core.qtlbot_log_files import discover_qtlbot_log_paths

WEEKDAY_NAMES = "月火水木金土日"

_TSUNAMI_ALERT_VALUES = ("Watch", "Warning", "MajorWarning")
_LOG_TIME_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


# ===============================
# スケジュール
# ===============================
def parse_summary_time(raw: str, default: tuple[int, int] = (0, 5)) -> tuple[int, int]:
    """'HH:MM' を (時, 分) に変換する。形式不正・範囲外は default。"""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(raw))
    if not m:
        return default
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return hour, minute


def next_run_time(now: datetime, hour: int, minute: int, weekday: int | None = None) -> datetime:
    """
    now より後で最初の投稿時刻を返す。
    weekday=None なら毎日 hour:minute、0〜6（月〜日。datetime.weekday() 準拠）ならその曜日のみ。
    """
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is None:
        return candidate if candidate > now else candidate + timedelta(days=1)
    candidate += timedelta(days=(weekday - now.weekday()) % 7)
    return candidate if candidate > now else candidate + timedelta(days=7)


def summary_period(kind: str, run_at: datetime) -> tuple[datetime, datetime]:
    """
    集計期間 [start, end) を返す。投稿日の0時を end とし、daily は前日1日分、
    weekly は直前の7日分（常に暦日の区切り）。
    """
    end = run_at.replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=7 if kind == "weekly" else 1), end


# ===============================
# 集計
# ===============================
def _parse_time(raw) -> datetime | None:
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(str(raw), fmt)
        except ValueError:
            continue
    return None


def _num(value) -> float | None:
    """-1（不明）・None・数値以外は None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value == -1:
        return None
    return value


def merge_events(records: list[dict]) -> list[dict]:
    """QUAKE_RECORD_V1 のレコード群を、発生時刻ごとの「1つの地震」に統合する。"""
    groups: dict[tuple[str, bool], list[dict]] = {}
    for r in records:
        if _parse_time(r.get("time_raw")) is None:
            continue
        foreign = r.get("issue_type") == "Foreign"
        groups.setdefault((r["time_raw"], foreign), []).append(r)

    events = []
    for (time_raw, foreign), recs in groups.items():
        # id は時系列順に増える（通知の古い順）。後の通知の値で上書きし、不明(-1)・調査中は上書きしない
        recs.sort(key=lambda r: str(r.get("id", "")))
        ev = {
            "time": _parse_time(time_raw), "foreign": foreign,
            "max_scale": -1, "magnitude": None, "depth": None,
            "name": "調査中", "tsunami_alert": False,
        }
        for r in recs:
            if (v := _num(r.get("max_scale"))) is not None:
                ev["max_scale"] = v
            if (v := _num(r.get("magnitude"))) is not None:
                ev["magnitude"] = v
            if (v := _num(r.get("depth"))) is not None:
                ev["depth"] = v
            if (name := r.get("hypocenter_name")) and name != "調査中":
                ev["name"] = name
            if r.get("domestic_tsunami") in _TSUNAMI_ALERT_VALUES:
                ev["tsunami_alert"] = True
        events.append(ev)
    return events


def summarize(records: list[dict], start: datetime, end: datetime, top_n: int = 5) -> dict:
    """期間 [start, end) の地震を集計する。"""
    events = [e for e in merge_events(records) if start <= e["time"] < end]
    domestic = [e for e in events if not e["foreign"]]

    by_scale = Counter(e["max_scale"] for e in domestic if e["max_scale"] != -1)
    max_scale = max(by_scale) if by_scale else -1

    def _rank(e):
        mag = e["magnitude"] if e["magnitude"] is not None else -1
        return (e["max_scale"], mag, e["time"])

    with_mag = [e for e in domestic if e["magnitude"] is not None]
    by_day: dict = {}
    for e in domestic:
        d = e["time"].date()
        count, day_max = by_day.get(d, (0, -1))
        by_day[d] = (count + 1, max(day_max, e["max_scale"]))

    return {
        "start": start, "end": end,
        "domestic_total": len(domestic),
        "foreign_total": len(events) - len(domestic),
        "by_scale": dict(by_scale),
        "no_scale": sum(1 for e in domestic if e["max_scale"] == -1),
        "max_scale": max_scale,
        "max_magnitude_event": max(with_mag, key=lambda e: e["magnitude"]) if with_mag else None,
        "top": sorted(domestic, key=_rank, reverse=True)[:max(1, top_n)],
        "tsunami_alert_total": sum(1 for e in domestic if e["tsunami_alert"]),
        "by_day": by_day,
    }


# ===============================
# ログの保持期間チェック
# ===============================
def oldest_log_time(base_path: str | None = None) -> datetime | None:
    """qtlbot.log* のうち最も古い世代の先頭行の時刻（ログが残っている最古の時点）。無ければ None。"""
    for path in discover_qtlbot_log_paths(base_path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _LOG_TIME_RE.match(line)
                    if m:
                        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    break
        except OSError:
            continue
        return None   # 最古のファイルが読めた（行が無い）場合はそれ以上遡らない
    return None


# ===============================
# Embed
# ===============================
def _scale_label(scale: float) -> str:
    return f"震度{INT_MAP.get(scale, '不明')}"


def _depth_label(depth) -> str:
    if depth is None:
        return ""
    return "ごく浅い" if depth == 0 else f"{int(depth)}km"


def _event_line(e: dict, with_date: bool) -> str:
    when = f"{e['time'].month}/{e['time'].day} {e['time']:%H:%M}" if with_date else f"{e['time']:%H:%M}"
    parts = [when, e["name"], _scale_label(e["max_scale"]) if e["max_scale"] != -1 else "震度不明"]
    if e["magnitude"] is not None:
        parts.append(f"M{e['magnitude']:.1f}")
    if (d := _depth_label(e["depth"])):
        parts.append(d)
    return "・" + "　".join(parts)


def build_summary_embed(
    stats: dict, kind: str, now: datetime | None = None,
    log_oldest: datetime | None = None,
) -> discord.Embed:
    """summarize() の結果から Embed を作る。kind は "daily" / "weekly"。"""
    start, end = stats["start"], stats["end"]
    last_day = end - timedelta(days=1)
    if kind == "weekly":
        title = f"🗓 週間地震サマリー（{start.month}/{start.day}〜{last_day.month}/{last_day.day}）"
    else:
        title = f"📅 {start.month}月{start.day}日（{WEEKDAY_NAMES[start.weekday()]}）の地震サマリー"

    total = stats["domestic_total"]
    if total == 0:
        description = "この期間に通知した国内の地震はありませんでした。"
        color = discord.Color.blue().value
    else:
        head = [f"**国内の地震: {total}件**"]
        if stats["max_scale"] != -1:
            head.append(f"最大震度: {_scale_label(stats['max_scale'])}")
        ev = stats["max_magnitude_event"]
        if ev is not None:
            head.append(f"最大M: M{ev['magnitude']:.1f}（{ev['name']}）")
        description = "　".join(head)
        color = SHINDO_COLORS.get(stats["max_scale"], SHINDO_COLORS[-1])

    embed = discord.Embed(title=title, description=description, color=color, timestamp=now)

    if total:
        scale_lines = [f"・{_scale_label(s)}: {n}件" for s, n in sorted(stats["by_scale"].items(), reverse=True)]
        if stats["no_scale"]:
            scale_lines.append(f"・震度不明（震源情報のみ）: {stats['no_scale']}件")
        if scale_lines:
            embed.add_field(name="震度別件数", value="\n".join(scale_lines), inline=False)

        embed.add_field(
            name="主な地震（震度・規模の大きい順）",
            value="\n".join(_event_line(e, with_date=(kind == "weekly")) for e in stats["top"])[:1024],
            inline=False,
        )

    if kind == "weekly":
        day_lines = []
        for i in range(7):
            day = (start + timedelta(days=i)).date()
            count, day_max = stats["by_day"].get(day, (0, -1))
            tail = f"（最大{_scale_label(day_max)}）" if day_max != -1 else ""
            day_lines.append(f"・{day.month}/{day.day}({WEEKDAY_NAMES[day.weekday()]}): {count}件{tail}")
        embed.add_field(name="日別", value="\n".join(day_lines), inline=False)

    if stats["tsunami_alert_total"]:
        embed.add_field(name="津波注意報以上を伴った地震", value=f"{stats['tsunami_alert_total']}件", inline=True)
    if stats["foreign_total"]:
        embed.add_field(name="遠地地震の情報", value=f"{stats['foreign_total']}件", inline=True)

    footer = f"集計期間: {start:%Y-%m-%d %H:%M} 〜 {end:%Y-%m-%d %H:%M} ／ QTL_Bot が通知した地震のみ"
    if log_oldest is not None and log_oldest > start:
        footer += (
            f"\n⚠ ログは {log_oldest:%Y-%m-%d %H:%M} 以降しか残っておらず、"
            "実際より少なく集計されている可能性があります"
        )
    embed.set_footer(text=footer)
    return embed
