"""core/test_runner.py の入力JSON正規化・引数解析のテスト（Discord接続不要）。"""
import json
import os

import pytest

# core.config は import 時に BOT_TOKEN / CHANNEL_ID を要求するためダミー値を与える
os.environ.setdefault("BOT_TOKEN", "dummy-token-for-tests")
os.environ.setdefault("CHANNEL_ID", "1")

from core import test_runner as tr  # noqa: E402

# P2P地震情報 /v2/history の生レスポンス（code=556）と同じ構造（配列で1件）
P2P_EEW_RAW = {
    "areas": [
        {"arrivalTime": None, "kindCode": "11", "name": "宮崎県南部平野部",
         "pref": "宮崎", "scaleFrom": 55, "scaleTo": 60},
        {"arrivalTime": "2024/08/08 16:43:17", "kindCode": "10", "name": "宮崎県北部山沿い",
         "pref": "宮崎", "scaleFrom": 50, "scaleTo": 50},
    ],
    "cancelled": False,
    "code": 556,
    "earthquake": {
        "arrivalTime": "2024/08/08 16:43:03", "condition": "",
        "hypocenter": {"depth": 20, "latitude": 31.8, "longitude": 131.7,
                       "magnitude": 7.2, "name": "日向灘", "reduceName": "日向灘"},
        "originTime": "2024/08/08 16:42:56",
    },
    "id": "66b4770dd616be440743d38b",
    "issue": {"eventId": "20240808164303", "serial": "1", "time": "2024/08/08 16:43:09"},
    "time": "2024/08/08 16:43:09.538",
}


def _write(tmp_path, obj, name="x.json"):
    p = tmp_path / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_single_element_list_is_unwrapped(tmp_path):
    data = tr.load_test_json(_write(tmp_path, [P2P_EEW_RAW]))
    assert isinstance(data, dict) and data["issue"]["eventId"] == "20240808164303"


def test_sniff_detects_raw_p2p_eew_from_list_fixture(tmp_path):
    """実機で「判定不能」になった eew_sample7.json と同形式（配列1件）。"""
    data = tr.load_test_json(_write(tmp_path, [P2P_EEW_RAW]))
    assert tr.sniff_test_target(data) == ["eew_p2p"]


def test_dict_fixture_is_unchanged(tmp_path):
    assert tr.load_test_json(_write(tmp_path, P2P_EEW_RAW)) == P2P_EEW_RAW


def test_multi_element_list_uses_first(tmp_path, capsys):
    other = dict(P2P_EEW_RAW, id="zzz")
    data = tr.load_test_json(_write(tmp_path, [P2P_EEW_RAW, other]))
    assert data["id"] == P2P_EEW_RAW["id"]
    assert "2件" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [[], [1, 2], "str", 5, None])
def test_invalid_top_level_raises_value_error(tmp_path, bad):
    with pytest.raises(ValueError):
        tr.load_test_json(_write(tmp_path, bad), exit_on_error=False)


def test_invalid_top_level_exits_when_exit_on_error(tmp_path):
    with pytest.raises(SystemExit) as e:
        tr.load_test_json(_write(tmp_path, []), exit_on_error=True)
    assert e.value.code == 1


def test_broken_json_is_value_error_when_not_exiting(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):  # JSONDecodeError は ValueError のサブクラス
        tr.load_test_json(str(p), exit_on_error=False)


def test_raw_p2p_eew_converter_works_after_unwrap(tmp_path):
    data = tr.load_test_json(_write(tmp_path, [P2P_EEW_RAW]))
    wolfx = tr._convert_raw_p2p_eew_for_test(data)
    assert wolfx["EventID"] == "20240808164303" and wolfx["Hypocenter"] == "日向灘"


# ── parse_test_args ──
def test_parse_exact_options():
    assert tr.parse_test_args(["--test_auto", "a.json"]) == ("__auto__", "a.json")
    assert tr.parse_test_args(["--test_eew_p2p", "a.json"]) == ("eew_p2p", "a.json")
    assert tr.parse_test_args(["--test_ews"]) == ("ews", "")
    assert tr.parse_test_args([]) is None


@pytest.mark.parametrize("argv", [
    ["--test_"], ["--test_aut", "a.json"], ["--test_foo", "x"],
])
def test_typo_in_test_option_exits_instead_of_starting_production(argv):
    with pytest.raises(SystemExit) as e:
        tr.parse_test_args(argv)
    assert e.value.code == 2


def test_unrelated_args_are_still_ignored():
    assert tr.parse_test_args(["--merge_env", "--yes"]) is None
