"""
tests/test_wave_table_model.py
===============================
core/wave_table_model.py（予報円の走時表モデル選択: JMA2001A / JMA2020A・B・C）のテスト。

【重要】ここで使う表は「合成データ」（tests/_wave_table.py。走時=震源距離÷一定速度）であり、
気象庁の実際の JMA2020 走時表ではない。実ファイルが無くても、形式の読み取り・モデルの切り分け・
キャッシュ・失敗時の挙動を確認するためのもの。実データでの確認は README の手順（python3 -m core.travel_time）で行う。
"""
import logging
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest  # noqa: E402

from _wave_table import DEPTHS, DISTS, write_table  # noqa: E402
from core import travel_time as tt  # noqa: E402
from core import wave_table_model as wtm  # noqa: E402
from core.travel_time import WAVE_P, WAVE_S, TravelTimeTable  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    tt.reset_for_tests()
    wtm.reset_for_tests()
    yield
    tt.reset_for_tests()
    wtm.reset_for_tests()


def _place(base, model_dir, name=None, vp_scale=1.0, suffix=".-00000"):
    """base/<model_dir>/<name or model_dir><suffix> に合成表を置く（実ファイルの命名は tjma2020a.-00000）。vp_scale で走時を変えて表の違いを作る。"""
    folder = base / model_dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ((name or model_dir) + suffix)
    lines = []
    for d in DEPTHS:
        for x in DISTS:
            r = math.hypot(d, x) * vp_scale
            lines.append(f"P {r / 6.0:8.3f} S {r / 3.5:8.3f} {d:3d} {x:6d}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# ---------- モデル名 ----------
@pytest.mark.parametrize("raw,expected", [
    ("jma2020a", "jma2020a"), (" JMA2020B ", "jma2020b"), ("Jma2020C", "jma2020c"),
    ("jma2001a", "jma2001a"), ("", "jma2001a"), (None, "jma2001a"), ("jma2020d", "jma2001a"), ("2020a", "jma2001a"),
])
def test_normalize_model(raw, expected):
    assert wtm.normalize_model(raw) == expected


def test_labels_only_for_jma2020():
    assert wtm.model_label("jma2001a") == ""
    assert wtm.model_label("jma2020a") == "JMA2020A"
    assert wtm.model_label("jma2020b") == "JMA2020B"
    assert wtm.model_label("jma2020c") == "JMA2020C"


# ---------- 気象庁公開仕様（tttfmt）どおりの固定桁の行を読める ----------
def test_parser_reads_official_fixed_width_line(tmp_path):
    # tttfmt: 1 相名P / 3-10 F8.3 / 12 相名S / 14-21 F8.3 / 23-25 I3 深さ / 28-32 I5 震央距離
    lines = [
        "P    0.416 S    0.703   0     0",
        "P    1.249 S    2.109   0     5",
        "P  123.456 S  223.456 100  2000",
    ]
    assert lines[0][0] == "P" and lines[0][11] == "S"
    f = tmp_path / "t"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tbl = TravelTimeTable.from_file(str(f))
    assert tbl.depth_range == (0.0, 100.0)
    assert tbl.travel_time(0, 5, WAVE_P) == pytest.approx(1.249)
    assert tbl.travel_time(100, 2000, WAVE_S) == pytest.approx(223.456)


# ---------- 実機の tjma2020a.-00000 の末尾行（実データの一部）を読める ----------
def test_parser_reads_real_jma2020a_tail_lines(tmp_path):
    real = [
        "P  198.065 S  356.373 700   1810",
        "P  212.000 S  381.533 700   1980",
        "P  212.817 S  383.008 700   1990",
        "P  213.633 S  384.481 700   2000",
    ]
    f = tmp_path / "tjma2020a.-00000"
    f.write_text("\n".join(real) + "\n", encoding="utf-8")
    tbl = TravelTimeTable.from_file(str(f))
    assert tbl.depth_range == (700.0, 700.0)
    assert tbl.distance_range() == (1810.0, 2000.0)
    assert tbl.travel_time(700, 2000, WAVE_P) == pytest.approx(213.633)
    assert tbl.travel_time(700, 1995, WAVE_S) == pytest.approx((383.008 + 384.481) / 2)


# ---------- パス解決 ----------
def test_resolve_prefers_default_name(tmp_path):
    p = _place(tmp_path, "tjma2020a")
    assert wtm.resolve_jma2020_path("jma2020a", str(tmp_path)) == p


def test_resolve_falls_back_to_single_elev0_file(tmp_path):
    p = _place(tmp_path, "tjma2020b", name="other_name")
    assert wtm.resolve_jma2020_path("jma2020b", str(tmp_path)) == p


def test_resolve_ignores_other_elevation_files(tmp_path):
    """実フォルダには .-00000〜.-08000 が並ぶ。標高0m（.-00000）だけを選び、他の標高は選ばない。"""
    p = _place(tmp_path, "tjma2020a")
    for n in ("-00100", "-02700", "-08000"):
        _place(tmp_path, "tjma2020a", suffix="." + n.lstrip("."))
    assert wtm.resolve_jma2020_path("jma2020a", str(tmp_path)) == p
    # 標高0mのファイルが無く他の標高だけがあるときは、取り違えを避けて None
    os.remove(p)
    assert wtm.resolve_jma2020_path("jma2020a", str(tmp_path)) is None


def test_resolve_accepts_plus_suffix(tmp_path):
    p = _place(tmp_path, "tjma2020c", suffix=".+00000")
    assert wtm.resolve_jma2020_path("jma2020c", str(tmp_path)) == p


def test_resolve_prefers_minus_over_plus(tmp_path):
    minus = _place(tmp_path, "tjma2020a", suffix=".-00000")
    _place(tmp_path, "tjma2020a", suffix=".+00000")
    assert wtm.resolve_jma2020_path("jma2020a", str(tmp_path)) == minus


def test_resolve_refuses_ambiguous_files(tmp_path):
    _place(tmp_path, "tjma2020c", name="x1")
    _place(tmp_path, "tjma2020c", name="x2")
    assert wtm.resolve_jma2020_path("jma2020c", str(tmp_path)) is None


def test_resolve_missing_and_non_2020_models(tmp_path):
    assert wtm.resolve_jma2020_path("jma2020a", str(tmp_path)) is None
    assert wtm.resolve_jma2020_path("jma2001a", str(tmp_path)) is None


# ---------- モデルごとに別の表を使い、互いに混ざらない ----------
def test_each_model_loads_its_own_table(tmp_path):
    f2001 = write_table(tmp_path / "t2001")
    _place(tmp_path, "tjma2020a", vp_scale=1.0)
    _place(tmp_path, "tjma2020b", vp_scale=2.0)   # 走時が2倍 = 同じ秒数での到達距離が約半分
    a = wtm.get_wave_table("jma2020a", f2001, str(tmp_path))
    b = wtm.get_wave_table("jma2020b", f2001, str(tmp_path))
    assert a is not None and b is not None and a is not b
    ra = a.radius_km(10, 20, WAVE_P)
    rb = b.radius_km(10, 20, WAVE_P)
    assert ra is not None and rb is not None and rb < ra * 0.6


def test_jma2020_does_not_disturb_jma2001_cache(tmp_path):
    f2001 = write_table(tmp_path / "t2001")
    _place(tmp_path, "tjma2020a", vp_scale=2.0)
    t2001 = wtm.get_wave_table("jma2001a", f2001, str(tmp_path))
    assert t2001 is tt.get_table(f2001)
    wtm.get_wave_table("jma2020a", f2001, str(tmp_path))
    # JMA2020 を読んだ後も、JMA2001A のキャッシュは同じ表のまま（再読み込み・上書きなし）
    assert tt.get_table(f2001) is t2001
    assert wtm.get_wave_table("jma2001a", f2001, str(tmp_path)) is t2001


def test_jma2001a_ignores_dir_and_matches_legacy_path(tmp_path):
    f2001 = write_table(tmp_path / "t2001")
    legacy = tt.get_table(f2001)
    assert wtm.get_wave_table("jma2001a", f2001, "/nonexistent") is legacy
    assert legacy.radius_km(10, 20, WAVE_S) == pytest.approx(wtm.get_wave_table("jma2001a", f2001, "/x").radius_km(10, 20, WAVE_S))


def test_jma2020_table_is_cached(tmp_path):
    _place(tmp_path, "tjma2020a")
    first = wtm.get_wave_table("jma2020a", "unused", str(tmp_path))
    assert first is not None and wtm.get_wave_table("jma2020a", "unused", str(tmp_path)) is first


# ---------- 失敗時 ----------
def test_missing_jma2020_warns_once_and_returns_none(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="QTLBot"):
        assert wtm.get_wave_table("jma2020a", "unused", str(tmp_path)) is None
        assert wtm.get_wave_table("jma2020a", "unused", str(tmp_path)) is None
    warns = [r for r in caplog.records if "JMA2020A" in r.getMessage()]
    assert len(warns) == 1


def test_unreadable_jma2020_file_returns_none(tmp_path):
    folder = tmp_path / "tjma2020c"
    folder.mkdir()
    (folder / "tjma2020c.+00000").write_text("not a travel time table\n", encoding="utf-8")
    assert wtm.get_wave_table("jma2020c", "unused", str(tmp_path)) is None


def test_failure_of_one_model_does_not_affect_another(tmp_path):
    _place(tmp_path, "tjma2020b")
    assert wtm.get_wave_table("jma2020a", "unused", str(tmp_path)) is None
    assert wtm.get_wave_table("jma2020b", "unused", str(tmp_path)) is not None


# ---------- build_wave_overlay へのラベル ----------
def test_overlay_caption_label_is_optional(tmp_path):
    from datetime import timedelta
    from core import eew_wave as ew
    table = TravelTimeTable.from_file(write_table(tmp_path / "t"))
    origin = ew.parse_eew_time("2026/10/16 12:00:00")
    data = {"Longitude": 142.0, "Latitude": 38.0, "Depth": 10, "OriginTime": "2026/10/16 12:00:00"}
    now = origin + timedelta(seconds=20)
    base = ew.build_wave_overlay(data, table, now=now)
    labeled = ew.build_wave_overlay(data, table, now=now, label="JMA2020A")
    assert base.caption == "12:00:20 JST  +20s"           # 既定は従来どおり
    assert labeled.caption == base.caption + "  JMA2020A"
    assert labeled.circles == base.circles                # ラベルは円の計算に影響しない
