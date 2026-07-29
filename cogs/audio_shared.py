"""
cogs/audio_shared.py
=====================
音声読み上げ（AquesTalkPi）・MP3再生の「実体」を1つに集約した Cog。

【新設の経緯】
令和8年熊本地震では、緊急地震速報（EEW）と地震情報（震度速報・各地の震度等）が
ほぼ同時に、かつ短時間に大量発生した。従来は EEW と地震情報が同じ
QuakeEewCog 内の同じ speech_queue / mp3_queue を共有していたため、
「片方の情報を読み上げている最中にもう片方の重要な情報が来ても、
キューの末尾に積まれて再生が遅れる」という問題があった。

そこで、EEW を扱う EewCog と 地震情報を扱う QuakeInfoCog を分離すると同時に、
両者が読み上げようとする音声を一本化して矛盾なく処理できるよう、
音声キューの実体をこの AudioCog に集約した。EewCog・QuakeInfoCog はいずれも
自分自身の speech_queue/mp3_queue を持たず、
`self.bot.get_cog("AudioCog").speak_local(...)` のように呼び出す。

【対象範囲（Step8時点）】
このリファクタリングは EewCog / QuakeInfoCog の2つのみを対象とする。
tsunami.py / volcano.py / usgs.py / other.py は引き続き従来の AudioMixin
（Cogごとに独立したキュー）を使用しており、このAudioCogとは別系統のまま。
将来的に全Cogをこちらに統合する場合は、各Cogの self.speech_queue 等を
削除し、AudioMixin の代わりにこのCogを参照する形に置き換える。

【提供する属性・メソッド】
    speak_local(text, priority=2)  : 読み上げキューに追加
    play_mp3(key)                  : 効果音（MP3）キューに追加
    speech_queue / mp3_queue       : 実体のキュー
    audio_files                    : キー→ファイル名の対応表
      （EEW用・地震情報用の両方のキーをここに統合する）
"""
import os
import time
import asyncio
import logging

from discord.ext import commands

from core.config import (
    AQUESTALK_PATH, AQUESTALK_SPEED, AUDIO_PLAYER,
    SPEECH_QUEUE_MAXSIZE, MP3_QUEUE_MAXSIZE,
)
from core.audio import AudioMixin

logger = logging.getLogger("QTLBot")


class AudioCog(commands.Cog, AudioMixin):
    """音声読み上げ・MP3再生の実体を保持する共有 Cog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.speech_queue = asyncio.PriorityQueue(maxsize=SPEECH_QUEUE_MAXSIZE)
        self.speech_task: asyncio.Task | None = None
        self.mp3_queue = asyncio.Queue(maxsize=MP3_QUEUE_MAXSIZE)
        self.mp3_task: asyncio.Task | None = None

        # EEW用・地震情報用の両方のMP3キーをここに統合する。
        self.audio_files = {
            # EEW系
            "low_alert":  "low_alert.mp3",
            "high_alert": "high_alert.mp3",
            "eewC":       "eewC.mp3",
            "eew3":       "eew3.mp3",
            "koushin":    "koushin.mp3",
            "saisyu":     "saisyu.mp3",
            # 地震情報系
            "vxse51":     "vxse51.mp3",
            "vxse52":     "vxse52.mp3",
            "vxse53":     "vxse53.mp3",
            "vxse5c":     "vxse5c.mp3",
            "zencyu":     "zencyu.mp3",
            # 強震モニタ・振動レベル系
            "lv100":      "lv100.mp3",
            "lv1000":     "lv1000.mp3",
            "lv2000":     "lv2000.mp3",
        }

    async def cog_load(self):
        self.speech_task = asyncio.create_task(self.speech_worker())
        self.mp3_task = asyncio.create_task(self.mp3_worker())
        logger.info("AudioCog: 音声読み上げ・MP3再生ワーカーを起動しました")

    async def cog_unload(self):
        for bg_task in (self.speech_task, self.mp3_task):
            if bg_task and not bg_task.done():
                bg_task.cancel()
