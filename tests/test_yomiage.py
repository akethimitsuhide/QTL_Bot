"""読み上げテンプレート（core/yomiage.py）と .env.yomiage まわりのテスト。"""
import os

import pytest
from dotenv import dotenv_values

from core import yomiage
from core.yomiage import (
    EEW_DEFAULTS, EEW_TAGS, QUAKE_DEFAULTS, QUAKE_TAGS, TemplateSet, build_eew_values, render,
    validate_template,
)

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---- 導入前の読み上げ文（cogs/eew.py の旧実装）を再現し、既定テンプレートと一致することを確認する ----
def _old_text(case: str, data: dict, areas: list[str]) -> str:
    hypo = data.get("Hypocenter", "不明")
    max_int = data.get("MaxIntensity", "")
    warn_area_text = "、".join(areas) + "では" if areas else ""
    if case == "FIRST_WARN":
        return f"緊急地震速報。{warn_area_text}強い揺れに警戒してください。{hypo}で地震。推定最大震度{max_int}。"
    if case == "FIRST_FORECAST":
        text = f"{hypo}で地震。推定最大震度{max_int}。"
        if data.get("isAssumption", False):
            text += "（PLUM法）"
        return text
    if case == "UPDATE_WARN":
        return f"緊急地震速報。{warn_area_text}強い揺れに警戒してください。"
    if case == "UPDATE_INTENSITY":
        return f"推定最大震度{max_int}"
    if case == "UPDATE_HYPO":
        return f"{hypo}で地震。推定最大震度{max_int}。"
    raise AssertionError(case)


_SAMPLES = [
    ({"Hypocenter": "三陸沖", "MaxIntensity": "5弱", "Serial": 1, "isWarn": True}, ["岩手県", "宮城県"]),
    ({"Hypocenter": "三陸沖", "MaxIntensity": "5弱", "Serial": 2, "isWarn": True}, []),
    ({"Hypocenter": "石川県能登地方", "MaxIntensity": "3", "isAssumption": True}, []),
    ({"Hypocenter": "不明", "MaxIntensity": ""}, ["北海道", "東北", "関東"]),
]


@pytest.mark.parametrize("case", ["FIRST_WARN", "FIRST_FORECAST", "UPDATE_WARN", "UPDATE_INTENSITY", "UPDATE_HYPO"])
@pytest.mark.parametrize("data,areas", _SAMPLES)
def test_default_templates_match_previous_wording(case, data, areas):
    ts = TemplateSet(EEW_DEFAULTS, EEW_TAGS, {})
    assert ts.render("EEW_" + case, build_eew_values(data, areas)) == _old_text(case, data, areas)


# ---- 書式 ----
def test_tag_replacement_and_missing_tag_is_empty():
    assert render("{a}と{b}", {"a": "X"}) == "Xと"


def test_bracket_section_dropped_when_any_tag_empty():
    assert render("A[{x}では]B", {"x": ""}) == "AB"
    assert render("A[{x}では]B", {"x": "東京"}) == "A東京ではB"
    assert render("[{x}と{y}]", {"x": "1", "y": ""}) == ""          # 1つでも空なら全体を省略


def test_bracket_without_tag_is_kept():
    assert render("A[固定文]B", {}) == "A固定文B"


# ---- 検証 ----
@pytest.mark.parametrize("template,fragment", [
    ("{未知}で地震", "未知のタグ"),
    ("{震源で地震", "{ }"),
    ("震源}で地震", "{ }"),
    ("[{震源}", "[ ]"),
    ("{震源}]", "[ ]"),
    ("[[{震源}]]", "入れ子"),
])
def test_validate_rejects_bad_templates(template, fragment):
    problem = validate_template(template, EEW_TAGS)
    assert problem and fragment in problem


def test_validate_accepts_defaults():
    for t in EEW_DEFAULTS.values():
        assert validate_template(t, EEW_TAGS) is None
    for t in QUAKE_DEFAULTS.values():
        assert validate_template(t, QUAKE_TAGS) is None


# ---- TemplateSet ----
def test_custom_template_is_used():
    ts = TemplateSet(EEW_DEFAULTS, EEW_TAGS, {"EEW_UPDATE_INTENSITY": "震度が{最大震度}に変わりました"})
    assert ts.is_custom("EEW_UPDATE_INTENSITY")
    assert ts.render("EEW_UPDATE_INTENSITY", {"最大震度": "6弱"}) == "震度が6弱に変わりました"


def test_invalid_template_falls_back_and_warns_once():
    warnings = []
    ts = TemplateSet(EEW_DEFAULTS, EEW_TAGS, {"EEW_UPDATE_INTENSITY": "{ぞんざい}"}, warn=warnings.append)
    assert not ts.is_custom("EEW_UPDATE_INTENSITY")
    assert ts.render("EEW_UPDATE_INTENSITY", {"最大震度": "4"}) == "推定最大震度4"
    assert len(warnings) == 1 and "YOMI_EEW_UPDATE_INTENSITY" in warnings[0] and "ぞんざい" in warnings[0]


def test_blank_override_means_default():
    ts = TemplateSet(EEW_DEFAULTS, EEW_TAGS, {"EEW_UPDATE_INTENSITY": "   ", "EEW_UPDATE_HYPO": None})
    assert not ts.is_custom("EEW_UPDATE_INTENSITY") and not ts.is_custom("EEW_UPDATE_HYPO")


# ---- タグ値 ----
def test_build_eew_values():
    v = build_eew_values({"Hypocenter": "福島県沖", "MaxIntensity": "5強", "Magnitude": 7.0, "Depth": "10km",
                          "Serial": 3, "isWarn": True, "isFinal": True, "isTraining": True}, ["東北"])
    assert v == {"震源": "福島県沖", "最大震度": "5強", "マグニチュード": "7", "深さ": "10", "警報地域": "東北",
                 "報数": "3", "種別": "警報", "最終報": "最終報", "訓練": "訓練報。", "PLUM法": ""}
    assert set(v) == set(EEW_TAGS)


def test_build_eew_values_plum_and_unknown_numbers():
    v = build_eew_values({"Hypocenter": "X", "isAssumption": True, "Magnitude": 5.5, "Depth": 10}, [])
    assert v["マグニチュード"] == "" and v["深さ"] == "" and v["PLUM法"] == "（PLUM法）" and v["種別"] == "予報"
    v = build_eew_values({"Magnitude": -1, "Depth": -1}, [])
    assert v["マグニチュード"] == "" and v["深さ"] == ""
    assert build_eew_values({"Magnitude": 5.6}, [])["マグニチュード"] == "5.6"


# ---- .env.yomiage.example との整合 ----
def test_example_file_matches_builtin_defaults():
    values = dotenv_values(os.path.join(_BASE, ".env.yomiage.example"))
    assert {k[len("YOMI_"):]: v for k, v in values.items()} == {**EEW_DEFAULTS, **QUAKE_DEFAULTS}


def test_yomiage_file_is_loaded_but_env_wins(tmp_path, monkeypatch):
    from core.env_loader import load_env_files
    (tmp_path / ".env").write_text('YOMI_EEW_UPDATE_HYPO="本体側"\n', encoding="utf-8")
    (tmp_path / ".env.yomiage").write_text(
        'YOMI_EEW_UPDATE_HYPO="別ファイル側"\nYOMI_EEW_UPDATE_INTENSITY="{最大震度}です"\n', encoding="utf-8")
    monkeypatch.delenv("YOMI_EEW_UPDATE_HYPO", raising=False)
    monkeypatch.delenv("YOMI_EEW_UPDATE_INTENSITY", raising=False)
    loaded = load_env_files(str(tmp_path))
    assert any(p.endswith(".env.yomiage") for p in loaded)
    assert os.environ["YOMI_EEW_UPDATE_HYPO"] == "本体側"
    assert os.environ["YOMI_EEW_UPDATE_INTENSITY"] == "{最大震度}です"
    monkeypatch.delenv("YOMI_EEW_UPDATE_HYPO", raising=False)
    monkeypatch.delenv("YOMI_EEW_UPDATE_INTENSITY", raising=False)


def test_module_has_no_discord_or_config_dependency():
    import ast
    tree = ast.parse(open(yomiage.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "re", "typing"}


# ---- 地震情報（導入前の cogs/quake.py の文言を再現して一致を確認） ----
def _old_quake(key, v):
    title, name, mag_str, depth_str = v["情報名"], v["震源"], v["マグニチュード"], v["深さ"]
    max_scale_str = v["最大震度"]
    time_str = v["発生時刻"] + "、" if v["発生時刻"] else ""
    tsunami_speak = " " + v["津波"] if v["津波"] else ""
    if key == "QUAKE_SCALE_PROMPT":
        return (f"{title}。{time_str}最大震度{max_scale_str}を、{v['観測都道府県']}で観測しました。"
                f"震源地・規模は調査中です。今後の情報に注意してください。")
    if key == "QUAKE_DESTINATION":
        return (f"{title}。{time_str}地震がありました。 震源地は {name}。 深さは {depth_str}。"
                f" マグニチュードは {mag_str}。{tsunami_speak}")
    if key == "QUAKE_SCALE_DEST_STRONG":
        return (f"{title}。{time_str}最大震度{max_scale_str}を観測する地震がありました。"
                f" 震源地は {name}。 震源の深さは {depth_str}。 マグニチュードは {mag_str} と推定されます。"
                f"この地震で、最大震度{max_scale_str}を、{v['最大震度観測地点']}で観測しました。{tsunami_speak}")
    if key == "QUAKE_SCALE_DEST":
        return (f"{title}。{time_str}最大震度{max_scale_str}を観測する地震がありました。{tsunami_speak}"
                f" 震源地は {name}。 震源の深さは {depth_str}。 マグニチュードは {mag_str} と推定されます。")
    if key == "QUAKE_FOREIGN":
        return (f"{title}。{time_str}{v['遠地地震文']} 震源地は {name}。"
                f" マグニチュードは {mag_str}。{tsunami_speak}")
    if key == "QUAKE_OTHER":
        return f"{title}。{tsunami_speak} 震源地 {name}。 最大震度 {max_scale_str}。 {mag_str}。"
    raise AssertionError(key)


_QUAKE_SAMPLES = [
    {"情報名": "各地の震度に関する情報", "発生時刻": "8時20分頃", "震源": "能登半島沖", "最大震度": "5弱",
     "深さ": "約10km", "マグニチュード": "M6.1", "津波": "この地震による津波の心配はありません。",
     "観測都道府県": "石川県、富山県", "最大震度観測地点": "石川県志賀町、石川県輪島市", "遠地地震文": "海外で規模の大きな地震がありました。"},
    {"情報名": "【テスト】震度速報", "発生時刻": "", "震源": "調査中", "最大震度": "2",
     "深さ": "調査中", "マグニチュード": "調査中", "津波": "",
     "観測都道府県": "調査中", "最大震度観測地点": "調査中", "遠地地震文": "遠地地震がありました。"},
]


@pytest.mark.parametrize("key", list(QUAKE_DEFAULTS))
@pytest.mark.parametrize("values", _QUAKE_SAMPLES)
def test_quake_default_templates_match_previous_wording(key, values):
    ts = TemplateSet(QUAKE_DEFAULTS, QUAKE_TAGS, {})
    assert ts.render(key, values) == _old_quake(key, values)
