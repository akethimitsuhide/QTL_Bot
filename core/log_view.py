"""
core/log_view.py
=================
ログを「見る」ための処理（Web ダッシュボードの /logs と、Discord の /qtl_logs が共用する）。

- list_log_files()  : 現在のログ・圧縮済み（.gz）・圧縮待ちの一覧（新しい順）
- find_log_file()   : 画面から渡された名前を、一覧にある実在のファイルに対応づける。
                      パスは入力から組み立てず、「一覧にある名前との完全一致」だけで解決する
                      （../ などのパストラバーサルを構造的に防ぐ。core/web_static.py と同じ考え方）。
- read_entries()    : 末尾 N 件（レベル・キーワードで絞り込み）。.gz も同じように読める。
                      表示するテキストは必ず core/log_mask.py で秘密情報を伏せる。

1件 = 「[時刻] [レベル] 名前: 本文」の行と、それに続く行（トレースバックなど）のまとまり。
"""
from __future__ import annotations

import logging
import os
import re
from collections import deque
from dataclasses import dataclass

from core.config import LOG_FILE_PATH
from core.log_archive import iter_log_lines, list_rotated_paths
from core.log_mask import mask_secrets

logger = logging.getLogger("QTLBot")

_HEADER_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[([A-Z]+)\] ")
LEVELS: dict[str, int] = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

MAX_TAIL = 1000                   # 1回に返す最大件数
MAX_SCAN_ENTRIES = 300_000        # 1回の読み込みで走査する最大件数（巨大ファイルでの占有防止）
MAX_ENTRY_CHARS = 4000            # 1件の表示文字数の上限（長大なトレースバック対策）
MAX_KEYWORD_LEN = 100


@dataclass(frozen=True)
class LogFile:
    name: str            # ファイル名（画面・API で使う識別子）
    kind: str            # current / archive / pending / legacy
    path: str
    size: int
    mtime: float

    @property
    def compressed(self) -> bool:
        return self.kind == "archive"


def _stat(kind: str, path: str) -> LogFile | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return LogFile(os.path.basename(path), kind, path, st.st_size, st.st_mtime)


def list_log_files(base_path: str | None = None) -> list[LogFile]:
    """現在のログを先頭に、新しい順で返す。"""
    base = base_path or LOG_FILE_PATH
    files: list[LogFile] = []
    current = _stat("current", base)
    if current:
        files.append(current)
    rotated = [f for f in (_stat(k, p) for k, p in list_rotated_paths(base)) if f]
    files.extend(reversed(rotated))                       # list_rotated_paths は古い順
    return files


def find_log_file(name: str, base_path: str | None = None) -> LogFile | None:
    """name が一覧にあるファイル名と完全一致するときだけ、そのファイルを返す。"""
    if not isinstance(name, str) or not name or len(name) > 255:
        return None
    for f in list_log_files(base_path):
        if f.name == name:
            return f
    return None


def parse_level(name: str | None) -> int | None:
    """レベル名（大文字小文字不問）を数値へ。空・不明は None（絞り込まない）。"""
    if not name:
        return None
    return LEVELS.get(str(name).strip().upper())


def _iter_entries(path: str):
    """ログを (時刻, レベル, 行リスト) の1件ずつにまとめる。先頭が続き行なら時刻・レベルは空。"""
    cur_time = cur_level = ""
    cur_lines: list[str] = []
    for raw in iter_log_lines(path):
        line = raw.rstrip("\r\n")
        m = _HEADER_RE.match(line)
        if m:
            if cur_lines:
                yield cur_time, cur_level, cur_lines
            cur_time, cur_level, cur_lines = m.group(1), m.group(2), [line]
        else:
            cur_lines.append(line)
    if cur_lines:
        yield cur_time, cur_level, cur_lines


def read_entries(
    path: str,
    tail: int = 200,
    min_level: int | None = None,
    keyword: str | None = None,
    mask: bool = True,
) -> dict:
    """
    path のログの末尾 tail 件（古い→新しい順）を返す。
    min_level: このレベル以上だけ。keyword: 大文字小文字を区別しない部分一致（伏せ字にした後の
    文字列で判定する。伏せた部分を検索して中身を探る使い方ができないようにするため）。

    戻り値: {"entries": [{"time","level","text"}], "matched": 条件に合った件数（走査分）,
             "scanned": 走査した件数, "truncated": 走査の上限で打ち切ったか, "masked": 伏せた箇所の数}
    """
    tail = max(1, min(int(tail), MAX_TAIL))
    kw = (keyword or "").strip()[:MAX_KEYWORD_LEN].lower()
    kept: deque = deque(maxlen=tail)
    matched = scanned = 0
    truncated = False

    for t, lv, lines in _iter_entries(path):
        scanned += 1
        if scanned > MAX_SCAN_ENTRIES:
            truncated = True
            break
        if min_level is not None and LEVELS.get(lv, 0) < min_level:
            continue
        text = "\n".join(lines)
        if kw and kw not in text.lower():            # 先に安い判定（伏せ字処理は一致したものだけ）
            continue
        matched += 1
        kept.append((t, lv, text))

    entries = []
    masked_total = 0
    for t, lv, text in kept:
        if len(text) > MAX_ENTRY_CHARS:
            text = text[:MAX_ENTRY_CHARS] + " …（長いため省略）"
        if mask:
            text, n = mask_secrets(text)
            masked_total += n
        if kw and kw not in text.lower():            # 伏せた後でもう一度判定（中身の推測を防ぐ）
            continue
        entries.append({"time": t, "level": lv, "text": text})
    return {
        "entries": entries,
        "matched": matched,
        "scanned": min(scanned, MAX_SCAN_ENTRIES),
        "truncated": truncated,
        "masked": masked_total,
    }


def entries_to_text(entries: list[dict]) -> str:
    """read_entries() の entries を、ログと同じ見た目の複数行テキストへ。"""
    return "\n".join(e["text"] for e in entries)
