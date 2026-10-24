"""core/doctor.py（python3 bot.py --doctor）のテスト。"""
import os
import socket

import pytest

from core import doctor
from core.doctor import NG, OK, WARN, INFO


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key in ("BOT_TOKEN", "CHANNEL_ID", "TTS_ENGINE", "AQUESTALK_PATH", "AUDIO_PLAYER", "GIS_MAP_ENABLE",
                   "GIS_MAP_DATA_DIR", "WEB_DASHBOARD_ENABLED", "WEB_DASHBOARD_PORT", "WEB_DASHBOARD_HOST",
                   "WEB_DASHBOARD_TOKEN", "WEB_DASHBOARD_ALLOWED_IPS") or key.startswith("YOMI_"):
            monkeypatch.delenv(key, raising=False)


def _levels(checks):
    return [c.level for c in checks]


def _find(checks, fragment):
    return [c for c in checks if fragment in c.title]


def _write(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")


# ---------- 設定ファイル ----------
def test_missing_env_is_ng(tmp_path):
    checks = doctor.check_env_files(str(tmp_path), doctor._read_env_values(str(tmp_path)))
    assert NG in _levels(_find(checks, ".env がありません"))
    assert NG in _levels(_find(checks, "BOT_TOKEN"))
    assert NG in _levels(_find(checks, "CHANNEL_ID"))


def test_valid_env_and_token_never_printed(tmp_path):
    _write(tmp_path, ".env", "BOT_TOKEN=aaaa.bbbb.cccc\nCHANNEL_ID=123\n")
    os.chmod(tmp_path / ".env", 0o600)
    checks = doctor.check_env_files(str(tmp_path), doctor._read_env_values(str(tmp_path)))
    assert NG not in _levels(checks) and WARN not in _levels(checks)
    assert "aaaa.bbbb.cccc" not in doctor.format_report([("x", checks)])


def test_placeholder_token_channel_and_permissions(tmp_path):
    _write(tmp_path, ".env", "BOT_TOKEN=your_bot_token_here\nCHANNEL_ID=abc\nEEW_CHANNEL_ID=xyz\n")
    os.chmod(tmp_path / ".env", 0o644)
    checks = doctor.check_env_files(str(tmp_path), doctor._read_env_values(str(tmp_path)))
    assert _find(checks, "サンプルのまま")[0].level == NG
    assert _find(checks, "CHANNEL_ID は数値")[0].level == NG
    assert _find(checks, "EEW_CHANNEL_ID")[0].level == WARN
    assert _find(checks, "権限が緩すぎ")[0].level == WARN


def test_missing_items_vs_example_is_info(tmp_path):
    _write(tmp_path, ".env", "BOT_TOKEN=a.b.c\nCHANNEL_ID=1\n")
    _write(tmp_path, ".env.example", "BOT_TOKEN=\nCHANNEL_ID=\nNEW_ITEM=1\n")
    checks = doctor.check_env_files(str(tmp_path), doctor._read_env_values(str(tmp_path)))
    found = _find(checks, "新しい項目が 1 件")
    assert found and found[0].level == INFO and "NEW_ITEM" in found[0].detail


# ---------- 読み上げテンプレート ----------
def test_yomiage_none_configured(tmp_path):
    checks = doctor.check_yomiage(str(tmp_path))
    assert _levels(checks) == [INFO]


def test_yomiage_detects_all_kinds_of_mistakes(tmp_path):
    _write(tmp_path, ".env.yomiage", "\n".join([
        'YOMI_EEW_FIRST_WARN="{未知}で地震"',                       # 未知のタグ
        'YOMI_QUAKE_FORIGN="タイプミス"',                           # 項目名の打ち間違い
        'YOMI_VOLCANO_ERUPTION_FLASH="[{火山名}が噴火"',             # [ ] の対応ミス
        'YOMI_USGS_QUAKE={場所}でM{マグニチュード} #地震',           # 引用符なしの #
        'YOMI_TSUNAMI_JMA_CANCEL="${HOME}{解除文}"',                 # ${ } 展開
        'YOMI_EEW_UPDATE_INTENSITY="震度が{最大震度}に変わりました"',  # 正常
        "# YOMI_OTHER_KANCHI=コメントアウト行は無視",
    ]) + "\n")
    checks = doctor.check_yomiage(str(tmp_path))
    titles = "\n".join(c.title + c.detail for c in checks)
    assert "未知のタグ: {未知}" in titles
    assert "もしかして YOMI_QUAKE_FOREIGN" in titles
    assert "[ ] の対応が取れていません" in titles
    assert "コメントとして切り捨てられます" in titles
    assert "${" in titles and "展開" in titles
    ok = [c for c in checks if c.level == OK and c.title == "YOMI_EEW_UPDATE_INTENSITY"]
    assert ok and ok[0].detail == "展開例: 震度が5弱に変わりました"
    assert "OTHER_KANCHI" not in titles


def test_yomiage_line_numbers_and_quoted_hash_is_fine(tmp_path):
    _write(tmp_path, ".env.yomiage", '\n\nYOMI_USGS_QUAKE="{場所}でM{マグニチュード} #地震"\n')
    checks = doctor.check_yomiage(str(tmp_path))
    assert not [c for c in checks if "切り捨て" in c.title]
    assert _find(checks, "書式の問題はありません")


def test_yomiage_same_as_default_and_blank(tmp_path):
    from core.yomiage import TEMPLATE_REGISTRY
    default = TEMPLATE_REGISTRY["YOMI_EEW_UPDATE_INTENSITY"][0]
    _write(tmp_path, ".env.yomiage", f'YOMI_EEW_UPDATE_INTENSITY="{default}"\nYOMI_EEW_UPDATE_HYPO=\n')
    checks = doctor.check_yomiage(str(tmp_path))
    assert _find(checks, "既定と同じ内容です")
    assert not _find(checks, "YOMI_EEW_UPDATE_HYPO")


def test_yomiage_keys_in_dot_env_are_checked_too(tmp_path):
    _write(tmp_path, ".env", 'YOMI_EEW_UPDATE_HYPO="{不明なタグ}"\n')
    assert _find(doctor.check_yomiage(str(tmp_path)), "未知のタグ")


def test_registry_covers_every_default_and_sample_covers_every_tag():
    from core import yomiage
    keys = {}
    for d in (yomiage.EEW_DEFAULTS, yomiage.QUAKE_DEFAULTS, yomiage.TSUNAMI_DEFAULTS,
              yomiage.OTHER_DEFAULTS, yomiage.USGS_DEFAULTS, yomiage.VOLCANO_DEFAULTS):
        keys.update(d)
    assert set(yomiage.TEMPLATE_REGISTRY) == {"YOMI_" + k for k in keys}
    for key, (default, tags, samples) in yomiage.TEMPLATE_REGISTRY.items():
        assert set(samples) == set(tags), key
        assert yomiage.validate_template(default, tags) is None


# ---------- 機能の前提条件 ----------
def test_gis_files_list_matches_gis_data():
    os.environ.setdefault("BOT_TOKEN", "t")
    os.environ.setdefault("CHANNEL_ID", "1")
    from core import gis_data
    assert set(doctor._GIS_FILES) == {fn for _, fn in gis_data._FILES.values()}


def test_gis_data_missing_and_present(tmp_path):
    env = {"GIS_MAP_ENABLE": "true", "GIS_MAP_DATA_DIR": str(tmp_path)}
    assert _find(doctor.check_features(str(tmp_path), env), "データが未取得")[0].level == INFO
    for name in doctor._GIS_FILES:
        (tmp_path / name).write_text("[]", encoding="utf-8")
    assert _find(doctor.check_features(str(tmp_path), env), "データがそろっています")


def test_dashboard_port_in_use_and_free_and_exposure_warning(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        used = doctor.check_features(str(tmp_path), {"WEB_DASHBOARD_ENABLED": "true", "WEB_DASHBOARD_PORT": str(port)})
        assert _find(used, "使用中")[0].level == INFO
    free = doctor.check_features(str(tmp_path), {"WEB_DASHBOARD_ENABLED": "true", "WEB_DASHBOARD_PORT": str(port)})
    assert _find(free, "が使えます")[0].level == OK
    exposed = doctor.check_features(str(tmp_path), {"WEB_DASHBOARD_ENABLED": "true", "WEB_DASHBOARD_PORT": str(port),
                                                    "WEB_DASHBOARD_HOST": "0.0.0.0"})
    assert _find(exposed, "認証も接続元制限もありません")[0].level == WARN
    guarded = doctor.check_features(str(tmp_path), {"WEB_DASHBOARD_ENABLED": "true", "WEB_DASHBOARD_PORT": str(port),
                                                    "WEB_DASHBOARD_HOST": "0.0.0.0", "WEB_DASHBOARD_TOKEN": "x"})
    assert not _find(guarded, "認証も接続元制限もありません")
    bad = doctor.check_features(str(tmp_path), {"WEB_DASHBOARD_ENABLED": "true", "WEB_DASHBOARD_PORT": "99999"})
    assert _find(bad, "WEB_DASHBOARD_PORT が不正")[0].level == NG


def test_aquestalk_path_checks(tmp_path):
    assert _find(doctor.check_features(str(tmp_path), {}), "AQUESTALK_PATH が未設定")[0].level == INFO
    missing = doctor.check_features(str(tmp_path), {"AQUESTALK_PATH": str(tmp_path / "none")})
    assert _find(missing, "見つかりません")[0].level == WARN
    exe = tmp_path / "AquesTalkPi"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(exe, 0o644)
    assert _find(doctor.check_features(str(tmp_path), {"AQUESTALK_PATH": str(tmp_path)}), "実行権限がありません")
    os.chmod(exe, 0o755)
    assert _find(doctor.check_features(str(tmp_path), {"AQUESTALK_PATH": str(tmp_path)}), "AquesTalkPi が使えます")


# ---------- 実行状態・全体 ----------
def test_runtime_pid_states(tmp_path):
    assert doctor.check_runtime(str(tmp_path))[0].level == OK
    _write(tmp_path, "bot.pid", "4242")
    assert doctor.check_runtime(str(tmp_path), pid_alive=lambda p: True)[0].level == WARN
    assert doctor.check_runtime(str(tmp_path), pid_alive=lambda p: False)[0].level == INFO
    _write(tmp_path, "bot.pid", "broken")
    assert doctor.check_runtime(str(tmp_path))[0].level == WARN


def test_run_doctor_exit_code_and_report(tmp_path, capsys):
    assert doctor.run_doctor(str(tmp_path)) == 1                      # .env も BOT_TOKEN も無い → NG
    out = capsys.readouterr().out
    assert "[ NG ]" in out and "--starter" in out and "結果: NG" in out
    _write(tmp_path, ".env", "BOT_TOKEN=a.b.c\nCHANNEL_ID=1\n")
    os.chmod(tmp_path / ".env", 0o600)
    assert doctor.run_doctor(str(tmp_path)) == 0
    assert "読み取り専用" in capsys.readouterr().out


def test_multiple_channel_ids_are_accepted(tmp_path):
    _write(tmp_path, ".env", "BOT_TOKEN=a.b.c\nCHANNEL_ID=111, 222\nEEW_CHANNEL_ID=333;444\nQUAKE_CHANNEL_ID=555,abc\n")
    os.chmod(tmp_path / ".env", 0o600)
    checks = doctor.check_env_files(str(tmp_path), doctor._read_env_values(str(tmp_path)))
    assert _find(checks, "CHANNEL_ID が設定されています（2チャンネル）")[0].level == OK
    assert not _find(checks, "EEW_CHANNEL_ID")                     # 正常な複数指定は警告しない
    assert _find(checks, "QUAKE_CHANNEL_ID")[0].level == WARN       # 一部に数値以外があれば警告


def test_dashboard_web_files_check(tmp_path, monkeypatch):
    from core import web_static
    env = {"WEB_DASHBOARD_ENABLED": "true", "WEB_DASHBOARD_PORT": "59123"}
    assert _find(doctor.check_features(str(tmp_path), env), "画面ファイル（web/）がそろっています")
    monkeypatch.setattr(web_static, "WEB_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setattr(web_static.StaticFiles, "__init__", lambda self, web_dir=str(tmp_path / "nowhere"): (
        setattr(self, "_web_dir", web_dir), setattr(self, "_cache", {}), setattr(self, "_warned", set()))[0])
    found = _find(doctor.check_features(str(tmp_path), env), "画面ファイルが不足しています")
    assert found and found[0].level == WARN and "git pull" in found[0].detail
