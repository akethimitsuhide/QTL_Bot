"""
core/log_archive.py
====================
qtlbot.log の圧縮保存（.gz）・保持期間の管理・圧縮済みログの読み出し。

【ファイル名】
  qtlbot.log                          現在書き込み中のログ（無圧縮）
  qtlbot.log.<YYYYmmdd-HHMMSS>.pending   圧縮待ち（改名しただけ。次の .gz になる）
  qtlbot.log.<YYYYmmdd-HHMMSS>.gz        圧縮済みのアーカイブ
  qtlbot.log.<N>                         旧方式（RotatingFileHandler）の世代。起動時に .gz へ移す
  <YYYYmmdd-HHMMSS> は、そのファイルの最終更新時刻（＝そのログの終わりの時刻）。

【圧縮のタイミング】
  1. Bot の起動時: 起動前に残っていたログ（qtlbot.log と旧方式の世代すべて）を、1ファイルずつ
     .pending に改名し（一瞬で終わる）、圧縮は別スレッドで行う。圧縮が終わったら元ファイル
     （.pending）を消す。起動のたびにログが新しいファイルから始まるため、ローテーションの
     世代が起動のたびにずれていくことがない。
  2. 実行中のローテーション（LOG_MAX_BYTES 到達）: 同じく改名だけ行い、圧縮は別スレッド。
  圧縮の途中で電源が切れても .pending が残るので、次の起動で続きから圧縮される。

【保持】
  LOG_KEEP_DAYS > 0 … 最終更新が日数を超えたアーカイブを消す（世代数は見ない）
  LOG_KEEP_DAYS = 0 … 世代数（LOG_BACKUP_COUNT）を超えた古いアーカイブから消す。
                      LOG_BACKUP_COUNT=0 は「世代数では消さない」。
  .pending（圧縮待ち）は保持の対象にしない。

【読み出し】
  iter_log_lines(path) は .gz・無圧縮のどちらも1行ずつ返す。壊れた/途中で切れた .gz は、
  読めたところまでで止める（例外にしない）。
"""
from __future__ import annotations

import glob
import gzip
import logging
import os
import re
import threading
import time
import zlib
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Iterator

logger = logging.getLogger("QTLBot")

_STAMP_FMT = "%Y%m%d-%H%M%S"
_CHUNK = 1024 * 1024
_THROTTLE_SEC = 0.005          # 圧縮中、1MBごとに少し譲る（Raspberry Pi の負荷対策）
_ARCHIVE_RE = re.compile(r"\.(\d{8}-\d{6}(?:-\d+)?)\.(gz|pending)$")

_compress_lock = threading.Lock()


# ===============================
# 名前・一覧
# ===============================
def _stamp_of(path: str) -> str:
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime(_STAMP_FMT)


def _unique_pending(base: str, stamp: str) -> str:
    """同じ時刻のファイルが既にあっても上書きしないよう、末尾に連番を付ける。"""
    n = 0
    while True:
        suffix = stamp if n == 0 else f"{stamp}-{n}"
        if not any(os.path.exists(f"{base}.{suffix}.{ext}") for ext in ("pending", "gz")):
            return f"{base}.{suffix}.pending"
        n += 1


def _numbered_legacy(base: str) -> list[str]:
    out = []
    for p in glob.glob(f"{base}.*"):
        suffix = p[len(base) + 1:]
        if suffix.isdigit():
            out.append(p)
    return out


def list_rotated_paths(base: str) -> list[tuple[str, str]]:
    """
    現在のログ以外のファイルを (種別, パス) で返す。古い順（最終更新時刻→名前）。
    種別: "archive"（.gz）/ "pending"（圧縮待ち。同名の .gz がまだ無いものだけ）/ "legacy"（旧方式の連番）
    """
    items: list[tuple[str, str]] = []
    gz_stems: set[str] = set()
    for p in glob.glob(f"{base}.*.gz"):
        if _ARCHIVE_RE.search(p):
            items.append(("archive", p))
            gz_stems.add(p[:-3])                      # ".gz" を除いた部分
    for p in glob.glob(f"{base}.*.pending"):
        if _ARCHIVE_RE.search(p) and p[:-8] not in gz_stems:      # ".pending" を除いた部分
            items.append(("pending", p))
    for p in _numbered_legacy(base):
        items.append(("legacy", p))

    def _key(item):
        try:
            return (os.path.getmtime(item[1]), item[1])
        except OSError:
            return (0.0, item[1])

    return sorted(items, key=_key)


# ===============================
# 圧縮
# ===============================
def _gzip_file(src: str, dst_gz: str) -> None:
    tmp = dst_gz + ".tmp"
    mtime = os.path.getmtime(src)
    with open(src, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
        while True:
            chunk = fin.read(_CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            time.sleep(_THROTTLE_SEC)
    os.utime(tmp, (mtime, mtime))                      # 保持日数の判定は元ファイルの時刻で行う
    os.replace(tmp, dst_gz)


def compress_pending(base: str) -> int:
    """圧縮待ち（.pending）をすべて .gz にして元を消す。圧縮した数を返す。"""
    done = 0
    with _compress_lock:
        for kind, path in list_rotated_paths(base):
            if kind != "pending":
                continue
            dst = path[:-len(".pending")] + ".gz"
            try:
                _gzip_file(path, dst)
                os.remove(path)
                done += 1
            except OSError as e:
                logger.warning(f"ログの圧縮に失敗しました（次回の起動で再試行します）: {os.path.basename(path)}: {e}")
    return done


def apply_retention(base: str, keep_count: int, keep_days: int) -> int:
    """保持の設定を超えたアーカイブ（.gz）を消す。消した数を返す。"""
    archives = [p for kind, p in list_rotated_paths(base) if kind == "archive"]   # 古い順
    to_delete: list[str] = []
    if keep_days > 0:
        cutoff = time.time() - keep_days * 86400
        to_delete = [p for p in archives if _safe_mtime(p) < cutoff]
    elif keep_count > 0 and len(archives) > keep_count:
        to_delete = archives[: len(archives) - keep_count]
    removed = 0
    for p in to_delete:
        try:
            os.remove(p)
            removed += 1
        except OSError as e:
            logger.warning(f"古いログの削除に失敗しました: {os.path.basename(p)}: {e}")
    return removed


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return time.time()


def _background_job(base: str, keep_count: int, keep_days: int) -> None:
    try:
        n = compress_pending(base)
        removed = apply_retention(base, keep_count, keep_days)
        if n or removed:
            logger.info(f"ログの整理が完了しました（圧縮 {n} 件・期限切れの削除 {removed} 件）")
    except Exception as e:                      # スレッドの中の想定外エラーで Bot を巻き込まない
        logger.warning(f"ログの整理で想定外のエラー: {type(e).__name__}: {e}")


def start_background_archive(base: str, keep_count: int, keep_days: int) -> threading.Thread:
    """圧縮と保持の整理を別スレッドで始める。"""
    t = threading.Thread(
        target=_background_job, args=(base, keep_count, keep_days),
        name="qtlbot-log-archive", daemon=True,
    )
    t.start()
    return t


# ===============================
# 起動時
# ===============================
def stage_existing_logs(base: str) -> int:
    """
    起動前に残っていたログ（現在のログ＋旧方式の連番世代）を、1つずつ .pending に改名する。
    改名だけなので一瞬で終わる（圧縮は start_background_archive が別スレッドで行う）。
    空のファイルは消す。改名したファイルの数を返す。
    """
    targets = [p for kind, p in list_rotated_paths(base) if kind == "legacy"]
    if os.path.exists(base):
        targets.append(base)
    staged = 0
    for path in targets:
        try:
            if os.path.getsize(path) == 0:
                os.remove(path)
                continue
            os.rename(path, _unique_pending(base, _stamp_of(path)))
            staged += 1
        except OSError as e:
            logger.warning(f"ログの退避に失敗しました: {os.path.basename(path)}: {e}")
            print(f"[LogArchive] ログの退避に失敗しました: {path}: {e}", flush=True)
    return staged


# ===============================
# ローテーション（実行中）
# ===============================
class ArchivingRotatingFileHandler(RotatingFileHandler):
    """
    LOG_MAX_BYTES に達したら、今のログを .pending に改名して新しいファイルに切り替え、
    圧縮は別スレッドで行う（書き込み中のログ処理を圧縮で待たせない）。
    """

    def __init__(self, filename, maxBytes, keep_count: int, keep_days: int, encoding="utf-8"):
        super().__init__(filename, maxBytes=maxBytes, backupCount=0, encoding=encoding)
        self._keep_count = keep_count
        self._keep_days = keep_days

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        try:
            if os.path.exists(self.baseFilename) and os.path.getsize(self.baseFilename) > 0:
                os.rename(self.baseFilename, _unique_pending(self.baseFilename, _stamp_of(self.baseFilename)))
                start_background_archive(self.baseFilename, self._keep_count, self._keep_days)
        except OSError as e:
            print(f"[LogArchive] ローテーションに失敗しました: {e}", flush=True)
        if not self.delay:
            self.stream = self._open()


# ===============================
# 読み出し
# ===============================
def is_gzip_path(path: str) -> bool:
    return path.endswith(".gz")


def iter_log_lines(path: str) -> Iterator[str]:
    """
    ログファイルを1行ずつ返す（.gz も無圧縮も同じように読める）。
    開けない場合は OSError（FileNotFoundError 含む）。壊れた・途中で切れた .gz は、
    読めたところまでで止める。
    """
    if is_gzip_path(path):
        f = gzip.open(path, "rt", encoding="utf-8", errors="replace")
    else:
        f = open(path, "r", encoding="utf-8", errors="replace")
    with f:
        try:
            for line in f:
                yield line
        except (EOFError, zlib.error, gzip.BadGzipFile) as e:
            logger.warning(f"ログの読み込みを途中で止めました（ファイルが壊れている可能性）: {os.path.basename(path)}: {e}")
