"""
core/eew_history_log.py
==========================
緊急地震速報（EEW、Wolfx/P2P由来を問わない）の通知内容を、
core/quake_history_log.py と同じ考え方でqtlbot.log（ローテーション込み）
へ機械可読な形で記録・復元するためのモジュール。

【quake_history_log.py との違い】
- 地震情報（P2P code=551）は1つの地震につき通知1回・idも1つだが、
  EEWは同一の地震（EventID）について第1報〜最終報まで複数回
  （Serialが増加しながら）更新通知される。地図・表での履歴閲覧では
  「同じ地震の連続更新」を1行にまとめたいため、レコードの一意キーは
  EventIDのみとし、Serialを重ねるたびに同じキーで上書きする
  （core.quake_history_log.load_quake_history と同じ「新しい世代が
  古い世代を上書きする」dedupe機構が、この用途にもそのまま使える）。
- キャンセル報（isCancel=True）は震源情報を持たないため、地図表示に
  適さないケースがある。build_eew_record はそれらの通知に対しては
  None を返し、呼び出し側（cogs/eew.py）でログ出力自体をスキップする
  設計とした。
- 最大震度はWolfx形式では文字列（例: "5弱"）で渡ってくるため、
  core.constants.INT_MAP を使って地震情報と同じ数値コード（max_scale）
  に正規化して保存する。Web Dashboard側の色分け・フィルタ表示を
  地震情報の履歴機能とロジック共通化できるようにするため。

その他の設計方針（qtlbot.logを直接パースする理由・トレードオフ等）は
core/quake_history_log.py のモジュールdocstringを参照。
"""
import json
import logging
from typing import Iterator

from core.constants import INT_MAP
from core.helpers import format_jma_time, safe_float, safe_int
from core.qtlbot_log_files import discover_qtlbot_log_paths

logger = logging.getLogger("QTLBot")

EEW_RECORD_TAG = "EEW_RECORD_V1"


def _shindo_label_to_code(label: str) -> int | None:
    """"5弱" のような震度ラベル文字列を INT_MAP の数値コードへ変換する。"""
    return next((k for k, v in INT_MAP.items() if v == label), None)


def build_eew_record(data: dict, title: str) -> dict | None:
    """
    notify_eew() が受け取るWolfx形式（P2P由来をこの形式へ変換した
    ものも含む）の dict から、ログ記録・地図表示に必要な最小限の
    フィールドを抽出する。

    以下のいずれかに該当する場合は None を返す（呼び出し元はログ出力
    自体をスキップすること）:
    - EventID が取得できない（一意に識別できない）
    - isCancel=True（キャンセル報。震源情報を伴わないため地図表示に
      適さない）
    """
    event_id = data.get("EventID")
    if not event_id:
        return None
    if data.get("isCancel"):
        return None

    is_assumption = data.get("isAssumption", False)  # PLUM法

    latitude = data.get("Latitude", -200)
    longitude = data.get("Longitude", -200)

    magnitude = None
    depth = None
    if not is_assumption:
        raw_mag = data.get("Magnitude") or data.get("Magunitude")
        mag_val = safe_float(raw_mag) if raw_mag is not None else -1.0
        magnitude = mag_val if mag_val > 0 else None

        raw_depth = str(data.get("Depth", "-1")).replace("km", "").strip()
        depth_val = safe_int(raw_depth)
        depth = depth_val if depth_val >= 0 else None

    max_intensity_label = data.get("MaxIntensity", "不明")

    return {
        "id": event_id,
        "title": title,
        "serial": safe_int(data.get("Serial", 1)) or 1,
        "is_final": bool(data.get("isFinal", False)),
        "is_warn": bool(data.get("isWarn", False)),
        "is_assumption": is_assumption,
        "time_raw": data.get("OriginTime", "不明"),
        "hypocenter_name": data.get("Hypocenter") or "不明",
        "latitude": latitude if latitude != -200 else None,
        "longitude": longitude if longitude != -200 else None,
        "magnitude": magnitude,
        "depth": depth,
        "max_scale": _shindo_label_to_code(max_intensity_label),
        "max_intensity_label": max_intensity_label,
    }


def format_eew_record_log_line(record: dict) -> str:
    """build_eew_record() の戻り値を、ログへそのまま渡せる1行文字列に変換する。"""
    return f"{EEW_RECORD_TAG} " + json.dumps(record, ensure_ascii=False)


def parse_eew_record_log_line(line: str) -> dict | None:
    """qtlbot.log の1行からEEW_RECORD_TAGに該当するJSONペイロードを抽出する。"""
    idx = line.find(EEW_RECORD_TAG)
    if idx == -1:
        return None
    payload = line[idx + len(EEW_RECORD_TAG):].strip()
    if not payload:
        return None
    try:
        record = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or "id" not in record:
        return None
    return record


def iter_eew_records_from_file(path: str) -> Iterator[dict]:
    """指定したログファイルを1行ずつ読み、EEWレコードのみを順に返す。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                record = parse_eew_record_log_line(line)
                if record is not None:
                    yield record
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning(f"eew_history_log: ログファイル読み込みに失敗しました ({path}): {e}")
        return


def load_eew_history(limit: int | None = None, base_path: str | None = None) -> list[dict]:
    """
    qtlbot.log* 全体をスキャンし、EEWの履歴（EventIDごとに最新の報を
    採用）を新しい順（発生時刻降順）で返す。

    同一EventIDについて複数のSerialが記録されている場合、古い世代→
    新しい世代の順に処理して後勝ちとする（Serialが大きい＝新しい報が
    自然に残る。古い世代のログの方がSerialが大きいことは通常起こらない
    ため、単純な「後勝ち」でも実用上問題ない）。
    """
    by_id: dict[str, dict] = {}
    for path in discover_qtlbot_log_paths(base_path):
        for record in iter_eew_records_from_file(path):
            by_id[record["id"]] = record

    def _sort_key(r: dict):
        return r.get("time_raw") or ""

    records = sorted(by_id.values(), key=_sort_key, reverse=True)
    if limit is not None:
        records = records[:limit]
    return records


def display_record(record: dict) -> dict:
    """load_eew_history() が返す生レコードに、表示用の整形済みフィールドを追加したコピーを返す。"""
    out = dict(record)
    out["time_display"] = format_jma_time(record.get("time_raw", "不明"))
    return out
