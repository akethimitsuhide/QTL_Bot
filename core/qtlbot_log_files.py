"""
core/qtlbot_log_files.py
==========================
qtlbot.log（ローテーション込み）のファイルパス一覧を、古い世代→新しい
世代の順で返す小さなヘルパー。

core/quake_history_log.py・core/eew_history_log.py の両方が同じ
ローテーション探索ロジックを必要とするため、どちらか一方に依存させず
（通知種別ごとの履歴モジュールを互いに独立させておくため）、ここに
切り出した。
"""
import glob
import os

from core.config import LOG_FILE_PATH


def discover_qtlbot_log_paths(base_path: str | None = None) -> list[str]:
    """
    qtlbot.log 本体 + ローテーション済みファイル（qtlbot.log.1 ...
    qtlbot.log.<LOG_BACKUP_COUNT>）のパス一覧を、古い世代→新しい世代
    （qtlbot.log が最新）の順で返す。

    RotatingFileHandler の仕様上、番号が大きいほど古い世代である点に
    注意（qtlbot.log.1 が直近の世代、qtlbot.log.7 が最古）。
    """
    base = base_path or LOG_FILE_PATH
    numbered = sorted(
        glob.glob(f"{base}.*"),
        key=lambda p: _rotation_index(p, base),
        reverse=True,  # 番号が大きい（古い）ものを先頭に
    )
    paths = [p for p in numbered if _rotation_index(p, base) is not None]
    if os.path.exists(base):
        paths.append(base)
    return paths


def _rotation_index(path: str, base: str) -> int | None:
    suffix = path[len(base) + 1:] if path.startswith(base + ".") else ""
    return int(suffix) if suffix.isdigit() else None
