"""core/yomiage_test.py（python3 bot.py --test_yomiage）のテスト。"""
import asyncio
import os

import pytest

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("CHANNEL_ID", "1")

from core import yomiage_test
from core.yomiage import TEMPLATE_REGISTRY
from core.yomiage_test import expand, resolve_key, run_test_yomiage


@pytest.fixture(autouse=True)
def _restore_env():
    before = dict(os.environ)
    for k in [k for k in os.environ if k.startswith("YOMI_")]:
        del os.environ[k]
    yield
    os.environ.clear()
    os.environ.update(before)


def _run(tmp_path, args, play=None):
    lines = []
    code = run_test_yomiage(args, base_dir=str(tmp_path), play=play, out=lines.append)
    return code, "\n".join(lines)


def test_resolve_key_variants():
    assert resolve_key("EEW_FIRST_WARN")[0] == "YOMI_EEW_FIRST_WARN"
    assert resolve_key("yomi_eew_first_warn")[0] == "YOMI_EEW_FIRST_WARN"
    assert resolve_key("first_warn")[0] == "YOMI_EEW_FIRST_WARN"          # 一意に決まる後半だけの指定
    key, cands = resolve_key("EEW_UPDATE")
    assert key is None and len(cands) == 3                                 # 複数に当てはまる
    assert resolve_key("nothing") == (None, [])


def test_list_shows_every_key_without_playing(tmp_path):
    played = []
    code, out = _run(tmp_path, [], play=played.append)
    assert code == 0 and played == []
    for key in TEMPLATE_REGISTRY:
        assert key in out
    assert "★設定済み" not in out


def test_list_marks_custom_and_reports_invalid(tmp_path):
    (tmp_path / ".env.yomiage").write_text(
        'YOMI_EEW_UPDATE_INTENSITY="震度が{最大震度}に変わりました"\nYOMI_USGS_QUAKE="{未知}"\n', encoding="utf-8")
    code, out = _run(tmp_path, [])
    assert "震度が5弱に変わりました" in out
    assert "未知のタグ" in out and "既定の文言" in out
    assert out.count("★設定済み") == 1                                     # 不正な設定は「設定済み」にならない


def test_target_plays_expanded_text(tmp_path):
    played = []
    code, out = _run(tmp_path, ["EEW_FIRST_WARN"], play=lambda t: played.append(t) or True)
    assert code == 0 and played == ["緊急地震速報。岩手県、宮城県では強い揺れに警戒してください。三陸沖で地震。推定最大震度5弱。"]
    assert "Discord へは通知しません" in out


def test_blank_tag_omits_bracket_section(tmp_path):
    played = []
    _run(tmp_path, ["EEW_FIRST_WARN", "--blank", "警報地域"], play=lambda t: played.append(t) or True)
    assert played == ["緊急地震速報。強い揺れに警戒してください。三陸沖で地震。推定最大震度5弱。"]


def test_no_play_and_all(tmp_path):
    played = []
    code, out = _run(tmp_path, ["all", "--no-play"], play=played.append)
    assert code == 0 and played == [] and out.count("読み上げ:") == len(TEMPLATE_REGISTRY)
    played2 = []
    _run(tmp_path, ["all"], play=lambda t: played2.append(t) or True)
    assert len(played2) == len(TEMPLATE_REGISTRY)


def test_failed_playback_gives_exit_code_1(tmp_path):
    code, out = _run(tmp_path, ["EEW_FIRST_WARN"], play=lambda t: False)
    assert code == 1 and "再生に失敗" in out


def test_bad_arguments(tmp_path):
    assert _run(tmp_path, ["--unknown"])[0] == 2
    assert _run(tmp_path, ["--blank"])[0] == 2
    assert _run(tmp_path, ["A", "B"])[0] == 2
    code, out = _run(tmp_path, ["EEW_UPDATE"])
    assert code == 2 and "複数に当てはまります" in out
    code, out = _run(tmp_path, ["nothing"])
    assert code == 2 and "見つかりません" in out


def test_expand_reports_warning_and_falls_back():
    text, custom, warning = expand("YOMI_EEW_UPDATE_INTENSITY", {"YOMI_EEW_UPDATE_INTENSITY": "{x}"})
    assert text == "推定最大震度5弱" and not custom and "未知のタグ" in warning


def test_real_playback_path_uses_synthesize_and_player(monkeypatch, capfd):
    import core.config as cfg
    import core.tts_engines as tts

    got = {}

    async def fake_synthesize(engine, text):
        got["text"] = text
        return b"RIFFfake"

    monkeypatch.setattr(tts, "synthesize", fake_synthesize)
    monkeypatch.setattr(cfg, "AUDIO_PLAYER", "cat")                       # stdin を読んで終了するだけのプレーヤー
    assert asyncio.run(yomiage_test._play_async("テスト")) is True and got["text"] == "テスト"
    monkeypatch.setattr(cfg, "AUDIO_PLAYER", "no-such-player-xyz")
    assert asyncio.run(yomiage_test._play_async("テスト")) is False
    assert "見つかりません" in capfd.readouterr().out

    async def silent(engine, text):
        return None

    monkeypatch.setattr(tts, "synthesize", silent)
    assert asyncio.run(yomiage_test._play_async("テスト")) is False
