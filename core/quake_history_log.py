"""
core/quake_history_log.py
==========================
P2P地震情報「地震情報」（code=551）の通知内容を、qtlbot.log（ローテーション
込み）へ機械可読な形で記録・復元するためのモジュール。

【背景・設計方針】
core/notification_log.py の record_notification は「直近の通知履歴」を
Web Dashboard で素早く確認するための仕組みだが、メモリ上のリング
バッファ（最大50件）のみで、Bot再起動で消える。また緯度経度等の
座標情報は保持していない。

一方 qtlbot.log は RotatingFileHandler により LOG_BACKUP_COUNT 世代
（既定7、1世代あたり LOG_MAX_BYTES=10MB）が既に永続化されている。
新たにDB等の永続化レイヤーを追加するのではなく、この既存のログ基盤を
そのまま「地震情報の履歴ストア」としても使う方針とした（QTL_Botの
軽量・低依存という設計方針に合わせるため）。

そのためには、ログに人間向けの自由文だけでなく、後から機械的に
パースできる形の1行を追加で出力しておく必要がある。本モジュールは
その「構造化ログ1行」の組み立て（build/format）と、qtlbot.log*を
読み戻してPythonのdictへ復元する（parse/load）の両方を担う。

【フォーマット】
既存のログ行フォーマット（core/logging_setup.py）:
    [YYYY-MM-DD HH:MM:SS] [INFO] QTLBot: <message>
の <message> 部分に、以下の固定タグ+半角スペース+JSON1行を出力する:
    QUAKE_RECORD_V1 {"id": "...", "time": "...", ...}

タグにバージョン番号を含めているのは、将来フィールド構成を変える際に
新旧フォーマットが混在するログファイルを安全に読み分けられるように
するため（現時点ではV1のみ対応）。

JSONは1行に収まる形（indentなし、ensure_ascii=False）で出力する。
複数行にまたがると行単位のログ処理（tail -f 等の一般的な運用ツール）
と相性が悪くなるため。

【qtlbot.log を直接パースする設計のトレードオフ】
- 利点: 新たな永続化先を増やさずに済む（軽量・低依存の方針に合致）。
        既存のログローテーション・世代管理をそのまま再利用できる。
- 欠点: ログファイル全体を毎回スキャンする必要があり、ファイルサイズが
        大きい場合は読み込みに時間がかかる（呼び出し元でキャッシュ
        すること。cogs/system.py の quake_history_handler 参照）。
        また、この記録形式を追加する前に発生した地震（旧ログ）には
        本レコードが存在しないため、遡って復元することはできない
        （そのための一括バックフィル手段は core/quake_history_backfill.py
        を参照）。
"""
import json
import logging
from typing import Iterator

from core.helpers import format_jma_time
from core.qtlbot_log_files import discover_qtlbot_log_paths

logger = logging.getLogger("QTLBot")

# ログ1行内でこのレコードを識別するための固定タグ。
# 末尾に半角スペースを含めた形でそのままログ行に埋め込む。
QUAKE_RECORD_TAG = "QUAKE_RECORD_V1"


def build_quake_record(data: dict, title: str) -> dict | None:
    """
    notify_quake() が受け取る P2P地震情報（code=551）形式の dict から、
    ログ記録・地図表示に必要な最小限のフィールドを抽出する。

    cogs/quake.py の notify_quake() 本文と同じフィールドパスを使う
    （両者が食い違うと「通知本文の内容」と「ログに残る内容」がずれる
    ため、抽出ロジックは意図的に単純な .get() の羅列にとどめている）。

    id が取得できない場合は、後から一意に識別できないレコードのため
    None を返す（呼び出し元はログ出力自体をスキップすること）。
    """
    quake_id = data.get("id") or data.get("_id")
    if not quake_id:
        return None

    eq = data.get("earthquake", {}) or {}
    hypo = eq.get("hypocenter", {}) or {}
    issue = data.get("issue", {}) or {}

    latitude = hypo.get("latitude", -200)
    longitude = hypo.get("longitude", -200)

    return {
        "id": quake_id,
        "title": title,
        "issue_type": issue.get("type", "Other"),
        "source": issue.get("source", "P2P地震情報"),
        # occur_time: 気象庁形式の生時刻文字列をそのまま保持する
        # （表示用に整形済みの文字列はformat_jma_timeで別途復元できるが、
        #  ソート・再計算のためISO変換前の生値も残しておく）。
        "time_raw": eq.get("time", "不明"),
        "hypocenter_name": hypo.get("name") or "調査中",
        "latitude": latitude if latitude != -200 else None,
        "longitude": longitude if longitude != -200 else None,
        "magnitude": hypo.get("magnitude", -1),
        "depth": hypo.get("depth", -1),
        "max_scale": eq.get("maxScale", -1),
        "domestic_tsunami": eq.get("domesticTsunami", "None"),
    }


def format_quake_record_log_line(record: dict) -> str:
    """build_quake_record() の戻り値を、ログへそのまま渡せる1行文字列に変換する。"""
    return f"{QUAKE_RECORD_TAG} " + json.dumps(record, ensure_ascii=False)


def parse_quake_record_log_line(line: str) -> dict | None:
    """
    qtlbot.log の1行から QUAKE_RECORD_TAG に該当するJSONペイロードを
    抽出する。該当しない行、またはJSONとして壊れている行は None を返す
    （例外を送出しない。ログファイルの途中切れ・書き込み中の行末破損等、
    実運用で普通に起こりうるため、呼び出し元でのtry/exceptを不要にする）。
    """
    idx = line.find(QUAKE_RECORD_TAG)
    if idx == -1:
        return None
    payload = line[idx + len(QUAKE_RECORD_TAG):].strip()
    if not payload:
        return None
    try:
        record = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or "id" not in record:
        return None
    return record


def iter_quake_records_from_file(path: str) -> Iterator[dict]:
    """指定したログファイルを1行ずつ読み、地震情報レコードのみを順に返す。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                record = parse_quake_record_log_line(line)
                if record is not None:
                    yield record
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning(f"quake_history_log: ログファイル読み込みに失敗しました ({path}): {e}")
        return


def load_quake_history(limit: int | None = None, base_path: str | None = None) -> list[dict]:
    """
    qtlbot.log* 全体をスキャンし、地震情報の履歴を新しい順（発生時刻降順）
    で返す。

    同一 id が複数回記録されている場合（バックフィルの再実行等）は、
    古い世代→新しい世代の順に処理して後勝ち（新しい世代の内容で上書き）
    とする。呼び出しコストが高いため、頻繁に呼ぶ場合は呼び出し元で
    短時間キャッシュすること（cogs/system.py 側で実施）。
    """
    by_id: dict[str, dict] = {}
    for path in discover_qtlbot_log_paths(base_path):
        for record in iter_quake_records_from_file(path):
            by_id[record["id"]] = record

    def _sort_key(r: dict):
        # ISO 8601 / "YYYY/MM/DD HH:MM:SS" 系の文字列であれば辞書順ソートで
        # おおむね時系列順になる。パース不能な値は最も古い扱いにする。
        return r.get("time_raw") or ""

    records = sorted(by_id.values(), key=_sort_key, reverse=True)
    if limit is not None:
        records = records[:limit]
    return records


def display_record(record: dict) -> dict:
    """
    load_quake_history() が返す生レコードに、表示用の整形済みフィールド
    （発生時刻の日本語表示等）を追加したコピーを返す。
    Web Dashboard 側（JSON API）・地図ページの両方で共通利用する。
    """
    out = dict(record)
    out["time_display"] = format_jma_time(record.get("time_raw", "不明"))
    return out
