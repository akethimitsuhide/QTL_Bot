"""
core/notification_log.py
=========================
Web Dashboard の「直近の通知履歴」表示用に、実際にDiscordへ送信した
通知（種別・タイトル・時刻）を記録する共有モジュール。

【設計方針: なぜモジュールレベルの状態なのか】
各Cog（EewCog, QuakeInfoCog, TsunamiCog, VolcanoCog, UsgsCog,
OtherInfoCog）はそれぞれ独立したCogインスタンスであり、共通の基底
クラスを持たない（音声再生は core.audio.AudioMixin 経由で共有して
いるが、これは別の関心事）。全Cog横断で「直近の通知」を1つの
リストとして集約するには、AudioMixin と同様にモジュールレベルの
共有状態を使うのが最もシンプルで、Bot全体の構成を変更せずに済む。

status_history（core/system.py内、システムリソースの推移）とは
別物で、こちらは「実際に何が通知されたか」という内容そのものを
保持する。

【永続化（2026-09-09追加）】
以前はメモリ上のリングバッファ（collections.deque、最大50件）のみで
保持し、Bot再起動でリセットされる設計だった。
core/quake_history_log.py・core/eew_history_log.py と同じ考え方で、
record_notification() の呼び出しごとに NOTIF_RECORD_V1 形式の
機械可読な1行をqtlbot.logへも記録するようにし、load_notification_history()
でqtlbot.log*（ローテーション含む）から復元できるようにした。
新たなDB等は追加せず、既存のログ基盤をそのまま再利用する（軽量・
低依存の方針に合わせるため）。

インメモリのリングバッファ（_notification_log / get_recent_notifications）
自体は、ファイルI/Oを伴わない最速の直近参照用途向けに残してある
（現時点でこの関数を直接呼ぶ既存コードは無いが、後方互換のため関数
自体は維持している）。Web Dashboard（/status/notifications）は
2026-09-09以降、永続化された load_notification_history() を使う。
"""
import json
import logging
from collections import deque
from datetime import datetime
from typing import Iterator

from core.qtlbot_log_files import discover_qtlbot_log_paths

logger = logging.getLogger("QTLBot")

# 保持件数の上限。Web Dashboardでの一覧表示に十分な件数として50件とした
# （status_historyのSTATUS_HISTORY_MAXLENとは独立した値）。
NOTIFICATION_LOG_MAXLEN = 50

_notification_log: deque = deque(maxlen=NOTIFICATION_LOG_MAXLEN)

NOTIF_RECORD_TAG = "NOTIF_RECORD_V1"


def record_notification(kind: str, title: str, detail: str = "") -> None:
    """
    通知1件を記録する（インメモリのリングバッファ + qtlbot.logへの
    構造化ログ出力の両方）。

    Parameters
    ----------
    kind : 通知種別を表す短いラベル（例: "EEW", "地震情報", "津波情報",
        "火山情報", "噴火速報", "噴火警報", "USGS", "長周期地震動",
        "気象庁その他"）。Web Dashboard側でのフィルタ・色分けに使う。
    title : 通知のタイトル（Discord Embedのtitleと同じ文言を渡す想定）。
    detail : 補足情報（震源地・マグニチュード等、任意）。省略可。

    呼び出し元（各Cogのnotify_*メソッド）でのテスト通知
    （is_test=True）は記録対象に含めない設計とする
    （呼び出し元側でis_testの場合はそもそも呼ばないこと）。

    例外を送出しない設計とする。この関数の失敗が本来の通知処理
    （Discordへの送信）に影響を与えてはならないため。
    """
    try:
        record = {
            "timestamp": datetime.now().isoformat(),
            "kind": kind,
            "title": title,
            "detail": detail,
        }
        _notification_log.append(record)
        logger.info(f"{NOTIF_RECORD_TAG} " + json.dumps(record, ensure_ascii=False))
    except Exception:
        # 通知記録の失敗で本来の通知フローを妨げないよう、ここで握りつぶす。
        # （ロガーの呼び出し自体も失敗しうる状況を想定し、あえて何もしない）
        pass


def get_recent_notifications(limit: int | None = None) -> list[dict]:
    """
    インメモリのリングバッファから、直近の通知履歴を新しい順（降順）で
    返す（ファイルI/Oなしの最速パス。Bot再起動でリセットされる）。

    永続化された履歴が必要な場合は load_notification_history() を使う
    こと（Web Dashboardの /status/notifications はこちらを使う）。

    limit を指定した場合はその件数までに絞る（省略時は全件、
    最大でも NOTIFICATION_LOG_MAXLEN 件）。
    """
    items = list(reversed(_notification_log))
    if limit is not None:
        return items[:limit]
    return items


def parse_notification_record_log_line(line: str) -> dict | None:
    """qtlbot.log の1行からNOTIF_RECORD_TAGに該当するJSONペイロードを抽出する。"""
    idx = line.find(NOTIF_RECORD_TAG)
    if idx == -1:
        return None
    payload = line[idx + len(NOTIF_RECORD_TAG):].strip()
    if not payload:
        return None
    try:
        record = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or "timestamp" not in record:
        return None
    return record


def _iter_notification_records_from_file(path: str) -> Iterator[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                record = parse_notification_record_log_line(line)
                if record is not None:
                    yield record
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning(f"notification_log: ログファイル読み込みに失敗しました ({path}): {e}")
        return


def load_notification_history(limit: int | None = None, base_path: str | None = None) -> list[dict]:
    """
    qtlbot.log*（ローテーション込み全世代）をスキャンし、通知履歴を
    新しい順（timestamp降順）で返す。Bot再起動をまたいで永続化された
    履歴を取得できる（core.quake_history_log.load_quake_history と
    同じ考え方）。

    通知には（quake/eewの構造化履歴と異なり）自然な一意キーが無い
    ため重複排除は行わない。呼び出しコストが高いため、頻繁に呼ぶ場合は
    呼び出し元で短時間キャッシュすること
    （cogs/system.py._get_notification_history_cached 参照）。
    """
    records: list[dict] = []
    for path in discover_qtlbot_log_paths(base_path):
        records.extend(_iter_notification_records_from_file(path))

    records.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    if limit is not None:
        records = records[:limit]
    return records
