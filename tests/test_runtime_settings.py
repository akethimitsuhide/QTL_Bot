"""
core/runtime_settings.py（再起動不要の設定変更）と /qtl_config コマンドのテスト（2026-10-12追加）。

確認すること:
1. 入力の解釈（on/off・震度の書き方・数値範囲）と不正入力の拒否
2. change: .env へ保存 → 実行時の値を更新。保存に失敗したら実行時の値も変えない
3. 深さの下限 ≤ 上限 の整合
4. .env に書いた文字列が、core.config の読み込み（再起動後）で同じ値になる
5. 対象設定がすべて core.config と .env.example に存在する
6. 地図の配色が実行時の設定に従う
7. /qtl_config のコマンド: 管理者のみ・成功/失敗の応答・.env への保存
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import pytest  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from core import runtime_settings as rs  # noqa: E402
from core.runtime_settings import RuntimeSettings, SettingError, SPEC_BY_KEY  # noqa: E402


def _initial(**over):
    base = {
        "QUAKE_MIN_SCALE": 0, "QUAKE_MIN_MAG": 0.0, "QUAKE_MIN_DEPTH": 0, "QUAKE_MAX_DEPTH": 9999,
        "QUAKE_ENABLE_SCALE_PROMPT": True, "QUAKE_ENABLE_DESTINATION": True,
        "QUAKE_ENABLE_SCALE_AND_DEST": True, "QUAKE_ENABLE_DETAIL_SCALE": True,
        "QUAKE_ENABLE_FOREIGN": True, "QUAKE_ENABLE_OTHER": True,
        "EEW_ENABLE": True, "QUAKE_ENABLE": True, "VOLCANO_ENABLE": True,
        "TSUNAMI_ENABLE": True, "ENABLE_LONG_PERIOD": True, "ENABLE_ADVISORY": True,
        "GIS_MAP_THEME": "light",
    }
    base.update(over)
    return base


@pytest.fixture
def env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("BOT_TOKEN=secret\nQUAKE_MIN_SCALE=0   # 0=全て\n", encoding="utf-8")
    return str(p)


# ---- 入力の解釈 ------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("on", True), ("ON", True), ("true", True), ("有効", True), ("1", True),
    ("off", False), ("False", False), ("無効", False), ("0", False),
])
def test_bool_parse(raw, expected):
    assert SPEC_BY_KEY["TSUNAMI_ENABLE"].parse(raw) is expected


@pytest.mark.parametrize("raw", ["", "maybe", "2", "はい"])
def test_bool_parse_rejects(raw):
    with pytest.raises(SettingError):
        SPEC_BY_KEY["TSUNAMI_ENABLE"].parse(raw)


@pytest.mark.parametrize("raw,expected", [
    ("全て", 0), ("0", 0), ("4", 40), ("震度4", 40), ("震度4以上", 40), ("40", 40),
    ("5弱", 45), ("5強", 50), ("6弱", 55), ("6強", 60), ("7", 70), (" 3 ", 30),
])
def test_scale_parse(raw, expected):
    assert SPEC_BY_KEY["QUAKE_MIN_SCALE"].parse(raw) == expected


@pytest.mark.parametrize("raw", ["5", "8", "強", "100", "abc"])
def test_scale_parse_rejects(raw):
    with pytest.raises(SettingError):
        SPEC_BY_KEY["QUAKE_MIN_SCALE"].parse(raw)


def test_number_ranges():
    assert SPEC_BY_KEY["QUAKE_MIN_MAG"].parse("3.5") == 3.5
    assert SPEC_BY_KEY["QUAKE_MIN_DEPTH"].parse("100") == 100
    for spec, raw in [("QUAKE_MIN_MAG", "-1"), ("QUAKE_MIN_MAG", "11"), ("QUAKE_MIN_MAG", "nan"),
                      ("QUAKE_MIN_MAG", "x"), ("QUAKE_MIN_DEPTH", "1.5"), ("QUAKE_MIN_DEPTH", "-1"),
                      ("QUAKE_MAX_DEPTH", "10000")]:
        with pytest.raises(SettingError):
            SPEC_BY_KEY[spec].parse(raw)


def test_theme_parse():
    assert SPEC_BY_KEY["GIS_MAP_THEME"].parse("Dark") == "dark"
    with pytest.raises(SettingError):
        SPEC_BY_KEY["GIS_MAP_THEME"].parse("neon")


def test_display():
    assert SPEC_BY_KEY["QUAKE_MIN_SCALE"].display(0) == "全て"
    assert SPEC_BY_KEY["QUAKE_MIN_SCALE"].display(45) == "震度5弱以上"
    assert SPEC_BY_KEY["TSUNAMI_ENABLE"].display(False) == "無効"


# ---- change ----------------------------------------------------------------------

def test_change_persists_then_updates(env_file):
    s = RuntimeSettings(_initial())
    old, new = s.change("QUAKE_MIN_SCALE", "震度4", env_path=env_file)
    assert (old, new) == ("全て", "震度4以上")
    assert s.get("QUAKE_MIN_SCALE") == 40
    vals = dotenv_values(env_file)
    assert vals["QUAKE_MIN_SCALE"] == "40" and vals["BOT_TOKEN"] == "secret"
    assert "# 0=全て" in open(env_file, encoding="utf-8").read()       # インラインコメントを保持


def test_change_does_not_touch_runtime_when_persist_fails(env_file, monkeypatch):
    s = RuntimeSettings(_initial())

    def boom(*a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(rs, "update_env_file", boom)
    with pytest.raises(SettingError) as ei:
        s.change("TSUNAMI_ENABLE", "off", env_path=env_file)
    assert "保存できませんでした" in str(ei.value)
    assert s.get("TSUNAMI_ENABLE") is True                              # 実行時の値は変わらない


def test_invalid_input_changes_nothing(env_file):
    s = RuntimeSettings(_initial())
    before = open(env_file, encoding="utf-8").read()
    with pytest.raises(SettingError):
        s.change("QUAKE_MIN_MAG", "abc", env_path=env_file)
    with pytest.raises(SettingError):
        s.change("BOT_TOKEN", "hijack", env_path=env_file)               # 対象外のキーは変更不可
    assert open(env_file, encoding="utf-8").read() == before
    assert s.get("QUAKE_MIN_MAG") == 0.0


def test_depth_order_is_enforced(env_file):
    s = RuntimeSettings(_initial(QUAKE_MIN_DEPTH=10, QUAKE_MAX_DEPTH=100))
    before = open(env_file, encoding="utf-8").read()
    with pytest.raises(SettingError):
        s.change("QUAKE_MIN_DEPTH", "200", env_path=env_file)
    with pytest.raises(SettingError):
        s.change("QUAKE_MAX_DEPTH", "5", env_path=env_file)
    assert open(env_file, encoding="utf-8").read() == before
    s.change("QUAKE_MAX_DEPTH", "300", env_path=env_file)
    s.change("QUAKE_MIN_DEPTH", "200", env_path=env_file)
    assert (s.get("QUAKE_MIN_DEPTH"), s.get("QUAKE_MAX_DEPTH")) == (200, 300)


def test_missing_initial_values_rejected():
    with pytest.raises(KeyError):
        RuntimeSettings({"QUAKE_MIN_SCALE": 0})


# ---- 再起動後に同じ値になるか / 設定の網羅 ------------------------------------------

def test_persisted_text_round_trips_through_config_parsers(monkeypatch):
    """.env に書く文字列を、core.config と同じ読み方で読み直すと元の値に戻る。"""
    from core import config
    cases = {
        "QUAKE_MIN_SCALE": ("震度5強", lambda k: config._env_int(k, -1), 50),
        "QUAKE_MIN_DEPTH": ("30", lambda k: config._env_int(k, -1), 30),
        "TSUNAMI_ENABLE": ("off", lambda k: config._env_bool(k, True), False),
        "ENABLE_ADVISORY": ("on", lambda k: config._env_bool(k, False), True),
        "QUAKE_MIN_MAG": ("3.5", lambda k: float(os.environ[k]), 3.5),
    }
    for key, (raw, reader, expected) in cases.items():
        spec = SPEC_BY_KEY[key]
        monkeypatch.setenv(key, spec.to_env(spec.parse(raw)))
        assert reader(key) == expected, key


def test_all_specs_exist_in_config_and_env_example():
    from core import config
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    example = open(os.path.join(root, ".env.example"), encoding="utf-8").read()
    for key in SPEC_BY_KEY:
        assert hasattr(config, key), f"core.config に {key} がありません"
        assert key in example, f".env.example に {key} の記載がありません"


# ---- 呼び出し側が実行時の値に従う ----------------------------------------------------

def test_gis_theme_follows_runtime_setting(monkeypatch):
    from core import gis_render as g
    monkeypatch.setattr(rs, "settings", RuntimeSettings(_initial(GIS_MAP_THEME="dark")))
    assert g._theme_colors() == g._THEMES["dark"]
    monkeypatch.setattr(rs, "settings", RuntimeSettings(_initial(GIS_MAP_THEME="highlight")))
    assert g._theme_colors() == g._THEMES["highlight"]


# ---- /qtl_config コマンド ----------------------------------------------------------

def _interaction(admin: bool):
    user = SimpleNamespace(
        id=42, guild_permissions=SimpleNamespace(administrator=admin), roles=[],
    )
    return SimpleNamespace(
        user=user,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        namespace=SimpleNamespace(key="TSUNAMI_ENABLE"),
    )


def _cog():
    from cogs import system
    return system.SystemCog.__new__(system.SystemCog)


def test_command_set_denies_non_admin(monkeypatch, env_file):
    from cogs import system
    monkeypatch.setattr(rs, "settings", RuntimeSettings(_initial()))
    monkeypatch.setattr(rs, "ENV_FILE_PATH", env_file)
    it = _interaction(admin=False)
    asyncio.run(system.SystemCog.slash_config_set.callback(_cog(), it, "TSUNAMI_ENABLE", "off"))
    it.response.send_message.assert_awaited_once()
    assert it.response.send_message.await_args.kwargs["ephemeral"] is True
    assert rs.get("TSUNAMI_ENABLE") is True and "TSUNAMI_ENABLE" not in dotenv_values(env_file)


def test_command_set_success_and_failure(monkeypatch, env_file):
    from cogs import system
    monkeypatch.setattr(rs, "settings", RuntimeSettings(_initial()))
    monkeypatch.setattr(rs, "ENV_FILE_PATH", env_file)

    it = _interaction(admin=True)
    asyncio.run(system.SystemCog.slash_config_set.callback(_cog(), it, "TSUNAMI_ENABLE", "off"))
    msg = it.followup.send.await_args.args[0]
    assert msg.startswith("✅") and "有効 → 無効" in msg
    assert rs.get("TSUNAMI_ENABLE") is False and dotenv_values(env_file)["TSUNAMI_ENABLE"] == "false"

    it2 = _interaction(admin=True)
    asyncio.run(system.SystemCog.slash_config_set.callback(_cog(), it2, "QUAKE_MIN_MAG", "abc"))
    assert it2.followup.send.await_args.args[0].startswith("❌")
    assert rs.get("QUAKE_MIN_MAG") == 0.0


def test_command_list_shows_all_settings(monkeypatch):
    from cogs import system
    monkeypatch.setattr(rs, "settings", RuntimeSettings(_initial(QUAKE_MIN_SCALE=40)))
    it = _interaction(admin=True)
    asyncio.run(system.SystemCog.slash_config_list.callback(_cog(), it))
    embed = it.response.send_message.await_args.kwargs["embed"]
    for key in SPEC_BY_KEY:
        assert key in embed.description
    assert "震度4以上" in embed.description


def test_value_autocomplete_follows_selected_key():
    from cogs import system
    it = _interaction(admin=True)
    it.namespace.key = "QUAKE_MIN_SCALE"
    got = asyncio.run(system.SystemCog._config_value_autocomplete.__wrapped__(_cog(), it, "5")
                      if hasattr(system.SystemCog._config_value_autocomplete, "__wrapped__")
                      else system.SystemCog._config_value_autocomplete(_cog(), it, "5"))
    assert {c.value for c in got} == {"5弱", "5強"}
    it.namespace.key = "TSUNAMI_ENABLE"
    got = asyncio.run(system.SystemCog._config_value_autocomplete(_cog(), it, ""))
    assert {c.value for c in got} == {"on", "off"}
