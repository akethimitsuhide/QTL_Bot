"""
tests/test_log_archive.py
==========================
ログの圧縮保存（core/log_archive.py）と、履歴モジュールが .gz を読めることの確認（2026-10-29）。
Discord 接続や環境変数なしで動く（一時ディレクトリだけを使う）。
"""
import gzip
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

from core import log_archive as la  # noqa: E402
from core.qtlbot_log_files import discover_qtlbot_log_paths  # noqa: E402
from core.quake_history_log import load_quake_history, format_quake_record_log_line  # noqa: E402


def _write(path, text, mtime=None):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _read_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return f.read()


def test_startup_archive_compresses_all_and_removes_originals(tmp_path):
    base = str(tmp_path / "qtlbot.log")
    t0 = time.time() - 3000
    _write(base + ".2", "oldest\n", t0)
    _write(base + ".1", "older\n", t0 + 1000)
    _write(base, "current\n", t0 + 2000)

    assert la.stage_existing_logs(base) == 3
    assert not os.path.exists(base)                      # 退避後は新しいファイルから始まる
    assert la.compress_pending(base) == 3

    left = sorted(os.listdir(tmp_path))
    assert all(name.endswith(".gz") for name in left), left   # 元ファイルは残らない
    paths = discover_qtlbot_log_paths(base)
    assert [_read_gz(p) for p in paths] == ["oldest\n", "older\n", "current\n"]   # 古い→新しい
    assert abs(os.path.getmtime(paths[0]) - t0) < 2          # 元の更新時刻を引き継ぐ


def test_empty_log_is_removed_not_archived(tmp_path):
    base = str(tmp_path / "qtlbot.log")
    _write(base, "")
    assert la.stage_existing_logs(base) == 0
    assert os.listdir(tmp_path) == []


def test_same_second_files_do_not_overwrite_each_other(tmp_path):
    base = str(tmp_path / "qtlbot.log")
    t = time.time() - 100
    _write(base + ".1", "a\n", t)
    _write(base, "b\n", t)
    assert la.stage_existing_logs(base) == 2
    assert la.compress_pending(base) == 2
    texts = sorted(_read_gz(p) for p in discover_qtlbot_log_paths(base))
    assert texts == ["a\n", "b\n"]


def test_pending_is_listed_until_its_gz_exists(tmp_path):
    base = str(tmp_path / "qtlbot.log")
    _write(base, "x\n")
    la.stage_existing_logs(base)
    kinds = [k for k, _ in la.list_rotated_paths(base)]
    assert kinds == ["pending"]
    # 圧縮の途中（.gz はできたが .pending がまだ残っている）は二重に数えない
    pending = la.list_rotated_paths(base)[0][1]
    with gzip.open(pending[:-len(".pending")] + ".gz", "wb") as f:
        f.write(b"x\n")
    assert [k for k, _ in la.list_rotated_paths(base)] == ["archive"]


def test_retention_by_count_and_by_days(tmp_path):
    base = str(tmp_path / "qtlbot.log")
    now = time.time()
    for i, age_days in enumerate((40, 20, 5, 1)):
        p = f"{base}.2026010{i + 1}-000000.gz"
        with gzip.open(p, "wb") as f:
            f.write(b"x")
        os.utime(p, (now - age_days * 86400,) * 2)

    assert la.apply_retention(base, keep_count=0, keep_days=0) == 0        # 0 は「消さない」
    assert la.apply_retention(base, keep_count=3, keep_days=0) == 1        # 世代数: 最も古い1件
    assert la.apply_retention(base, keep_count=99, keep_days=10) == 1      # 日数: 20日前のもの（世代数は見ない）
    left = [os.path.basename(p) for _, p in la.list_rotated_paths(base)]
    assert len(left) == 2


def test_iter_log_lines_reads_plain_gz_and_survives_truncated_gz(tmp_path):
    plain = tmp_path / "a.log"
    plain.write_text("l1\nl2\n", encoding="utf-8")
    assert list(la.iter_log_lines(str(plain))) == ["l1\n", "l2\n"]

    good = tmp_path / "b.gz"
    with gzip.open(good, "wt", encoding="utf-8") as f:
        f.write("".join(f"line{i}\n" for i in range(20000)))
    data = good.read_bytes()
    bad = tmp_path / "c.gz"
    bad.write_bytes(data[: len(data) // 2])                    # 途中で切れた .gz
    got = list(la.iter_log_lines(str(bad)))                    # 例外にならず、読めた分だけ返る
    assert 0 < len(got) < 20000


def test_history_is_loaded_from_archived_logs(tmp_path):
    base = str(tmp_path / "qtlbot.log")
    rec = {"id": "abc", "time_raw": "2026/10/28 23:54:00", "hypocenter_name": "熊本県天草・芦北地方"}
    _write(base, "[2026-10-28 23:57:32] [INFO] QTLBot: " + format_quake_record_log_line(rec) + "\n")
    la.stage_existing_logs(base)
    la.compress_pending(base)
    assert not os.path.exists(base)
    got = load_quake_history(base_path=base)
    assert [r["id"] for r in got] == ["abc"]


def test_rollover_hands_compression_to_a_thread(tmp_path):
    base = str(tmp_path / "qtlbot.log")
    h = la.ArchivingRotatingFileHandler(base, maxBytes=200, keep_count=0, keep_days=0)   # 0=世代数では消さない
    h.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("test_log_archive_rollover")
    lg.propagate = False
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        for i in range(30):
            lg.info("x" * 50 + str(i))
    finally:
        lg.removeHandler(h)
        h.close()
    for t in __import__("threading").enumerate():
        if t.name == "qtlbot-log-archive":
            t.join(timeout=10)
    la.compress_pending(base)                                   # スレッドが間に合わなかった分の保険
    names = os.listdir(tmp_path)
    assert any(n.endswith(".gz") for n in names)
    assert not any(n.endswith(".pending") for n in names)
    total = "".join(_read_gz(p) for p in discover_qtlbot_log_paths(base) if p.endswith(".gz"))
    assert "x" * 50 + "0" in total
