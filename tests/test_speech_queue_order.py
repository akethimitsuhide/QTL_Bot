"""
tests/test_speech_queue_order.py
================================
読み上げキュー（core/audio.py の AudioMixin）の取り出し順のテスト（2026-11追加）。

不具合: (priority, text) をそのまま PriorityQueue に積んでいたため、同じ優先度では読み上げ文の
文字列順で取り出されていた。実機の EEW で、先に積んだ「推定最大震度6弱」より後に積んだ
「推定最大震度5強」が先に読み上げられ、最後に聞こえるのが古い情報になった。
同じ優先度は到着順（FIFO）、優先度が違うときは従来どおり小さい方が先。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHANNEL_ID", "1")

import core.audio as audio  # noqa: E402


class _Bot:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed


class _Speaker(audio.AudioMixin):
    def __init__(self):
        self.bot = _Bot()
        self.speech_queue = asyncio.PriorityQueue(maxsize=200)


async def _no_sleep(*a, **kw):
    return None


def _drain_order(texts_with_priority, monkeypatch):
    """speak_local で積み、speech_worker が実際に取り出して合成に渡す順序を返す。"""
    monkeypatch.setattr(audio, "TTS_ENGINE", "scratchtts")   # AQUESTALK_PATH 未設定による無効化を避ける
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    spoken = []

    async def fake_synthesize(engine, text):
        spoken.append(text)
        if len(spoken) >= len(texts_with_priority):
            sp.bot.closed = True    # 全件取り出したらワーカーのループを終える
        return None                 # 音声なし → プレーヤーは起動しない

    monkeypatch.setattr(audio, "synthesize", fake_synthesize)
    sp = _Speaker()

    async def run():
        for text, priority in texts_with_priority:
            await sp.speak_local(text, priority)
        await asyncio.wait_for(sp.speech_worker(), timeout=5)

    asyncio.run(run())
    return spoken


def test_same_priority_is_fifo_not_alphabetical(monkeypatch):
    """実機の再現: 先に積んだ「6弱」が、後に積んだ「5強」より先に読まれる。"""
    order = _drain_order([("推定最大震度6弱", 2), ("推定最大震度5強", 2)], monkeypatch)
    assert order == ["推定最大震度6弱", "推定最大震度5強"]


def test_same_priority_keeps_arrival_order_for_many_items(monkeypatch):
    texts = ["z最後", "a最初", "m中間", "b二番目", "震度速報", "各地の震度に関する情報"]
    order = _drain_order([(t, 2) for t in texts], monkeypatch)
    assert order == texts


def test_lower_priority_number_still_goes_first(monkeypatch):
    order = _drain_order([("通常A", 3), ("通常B", 2), ("緊急", 1), ("通常C", 2)], monkeypatch)
    assert order == ["緊急", "通常B", "通常C", "通常A"]


def test_empty_text_is_ignored():
    sp = _Speaker()
    asyncio.run(sp.speak_local("   ", 2))
    assert sp.speech_queue.qsize() == 0


def test_queue_full_does_not_raise():
    sp = _Speaker()
    sp.speech_queue = asyncio.PriorityQueue(maxsize=1)

    async def run():
        await sp.speak_local("1件目", 2)
        await sp.speak_local("2件目（満杯でスキップ）", 2)

    asyncio.run(run())
    assert sp.speech_queue.qsize() == 1
