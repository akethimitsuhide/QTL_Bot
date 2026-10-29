"""
core/qtlbot_log_files.py
==========================
qtlbot.log（ローテーション・圧縮済みを含む）のファイルパス一覧を、古い→新しいの順で
返す小さなヘルパー。

core/quake_history_log.py・core/eew_history_log.py・core/notification_log.py・
core/quake_summary.py が同じ探索ロジックを必要とするため（履歴モジュールを互いに独立
させておくため）、ここに切り出した。ファイルの種類と読み方は core/log_archive.py を参照。
"""
import os

from core.config import LOG_FILE_PATH
from core.log_archive import list_rotated_paths


def discover_qtlbot_log_paths(base_path: str | None = None) -> list[str]:
    """
    ログのパス一覧を、古い→新しい（qtlbot.log が最新）の順で返す。
    対象: 圧縮済み（qtlbot.log.<日時>.gz）・圧縮待ち（.pending。同名の .gz がまだ無いもの）・
    旧方式の連番（qtlbot.log.<N>）・現在のログ。読むときは
    core.log_archive.iter_log_lines() を使うと .gz も同じように読める。
    """
    base = base_path or LOG_FILE_PATH
    paths = [p for _kind, p in list_rotated_paths(base)]
    if os.path.exists(base):
        paths.append(base)
    return paths
