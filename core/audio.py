"""
core/audio.py
=============
音声読み上げ（AquesTalkPi）・MP3再生を提供する Mixin クラス。

【設計方針: なぜ Mixin なのか】
speak_local() や play_mp3() は notify_quake / notify_tsunami / notify_volcano
など、ほぼ全ての Cog から呼ばれる。しかし Cog を分割すると、これらのメソッドは
物理的に別クラスへ移ってしまう。

解決策として、このファイルは「メソッドの実装だけ」を提供する AudioMixin を定義する。
各 Cog は commands.Cog と AudioMixin を多重継承することで、
自分自身の self.speech_queue / self.mp3_queue / self.audio_files を使って
これらのメソッドをそのまま呼び出せるようになる。

    class QuakeEewCog(commands.Cog, AudioMixin):
        def __init__(self, bot):
            self.bot = bot
            self.speech_queue = asyncio.PriorityQueue(maxsize=SPEECH_QUEUE_MAXSIZE)
            self.mp3_queue    = asyncio.Queue(maxsize=MP3_QUEUE_MAXSIZE)
            self.audio_files  = {...}
            ...

【注意: Step1時点では暫定共有】
現時点では quake.py 以外のCogがまだ分割されていないため、
speech_queue / mp3_queue は「メインCog（旧 QuakeTsunamiCog）」が
実質的に保持し続ける。全Cog分割が完了した段階で、
音声再生を専用の1つの AudioCog に集約し、他のCogは
`self.bot.get_cog("AudioCog").play_mp3(...)` のように参照する
形へ移行するのが最終形（Step2以降の課題）。

【bot.py からの移行元】
元 bot.py の speak_local() 〜 play_mp3() 定義（旧 1602〜1728行目付近）。
"""
import os
import time
import asyncio
import logging

from core.config import (
    AQUESTALK_PATH, AQUESTALK_SPEED, AUDIO_PLAYER,
    SPEECH_QUEUE_MAXSIZE, MP3_QUEUE_MAXSIZE,
)

logger = logging.getLogger("QTLBot")

try:
    import pygame
    pygame.mixer.init()
    _PYGAME_AVAILABLE = True
except Exception as e:
    _PYGAME_AVAILABLE = False
    print(f"[WARNING] pygame.mixer の初期化に失敗しました。MP3再生は無効です: {e}")


class AudioMixin:
    """
    音声読み上げ・MP3再生のメソッド群を提供する Mixin。

    要求する self の属性（継承先のCogが __init__ で用意する必要がある）:
        self.bot            : commands.Bot インスタンス
        self.speech_queue    : asyncio.PriorityQueue
        self.mp3_queue       : asyncio.Queue
        self.audio_files     : dict[str, str]  # キー → ファイル名
    """

    async def speak_local(self, text: str, priority: int = 2):
        if not text or not text.strip():
            return
        try:
            self.speech_queue.put_nowait((priority, text))
            logger.info(
                f"音声キュー追加 [優先度{priority}] "
                f"(深さ: {self.speech_queue.qsize()}/{SPEECH_QUEUE_MAXSIZE}): {text[:60]}"
            )
        except asyncio.QueueFull:
            logger.warning(
                f"音声キューが満杯です (深さ: {self.speech_queue.qsize()}): {text[:60]} はスキップされました"
            )

    async def speech_worker(self):
        if not AQUESTALK_PATH:
            logger.info("AQUESTALK_PATH 未設定のため音声読み上げ機能は無効です")
            return
        logger.info(f"音声読み上げ開始: {AQUESTALK_PATH} / player={AUDIO_PLAYER} / speed={AQUESTALK_SPEED}")
        queue_warn_threshold = max(SPEECH_QUEUE_MAXSIZE * 0.8, 1)

        while not self.bot.is_closed():
            try:
                priority, text = await self.speech_queue.get()
                queue_size = self.speech_queue.qsize()

                if queue_size >= queue_warn_threshold:
                    logger.warning(f"音声キュー圧力高 (深さ: {queue_size}/{SPEECH_QUEUE_MAXSIZE})")

                logger.info(f"音声再生開始 [優先度{priority}] (キュー深さ: {queue_size}): {text[:60]}")
                escaped = text.replace('"', '\\"')

                tts_proc = await asyncio.create_subprocess_exec(
                    AQUESTALK_PATH, "-s", str(AQUESTALK_SPEED), escaped,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                tts_out, tts_err = await tts_proc.communicate()
                logger.info(f"AquesTalkPi 終了コード={tts_proc.returncode} 出力バイト数={len(tts_out)}")
                if tts_err:
                    logger.warning(f"AquesTalkPi stderr: {tts_err.decode(errors='replace')[:200]}")

                if tts_out:
                    play_proc = await asyncio.create_subprocess_exec(
                        AUDIO_PLAYER, "-",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, play_err = await play_proc.communicate(input=tts_out)
                    logger.info(f"{AUDIO_PLAYER} 終了コード={play_proc.returncode}")
                    if play_err and play_proc.returncode != 0:
                        logger.warning(f"{AUDIO_PLAYER} stderr: {play_err.decode(errors='replace')[:200]}")
                else:
                    logger.warning(f"音声生成失敗（出力なし）: {text[:60]}")

                self.speech_queue.task_done()
                await asyncio.sleep(0.8)
            except Exception as e:
                logger.error(f"音声再生エラー: {e}")

    async def mp3_worker(self):
        if not _PYGAME_AVAILABLE:
            logger.info("MP3再生機能は無効です")
            return

        logger.info("MP3再生ワーカーを起動します")
        queue_warn_threshold = max(MP3_QUEUE_MAXSIZE * 0.8, 1)

        while not self.bot.is_closed():
            try:
                key, path = await self.mp3_queue.get()
                queue_size = self.mp3_queue.qsize()

                if queue_size >= queue_warn_threshold:
                    logger.warning(f"MP3キュー圧力高 (深さ: {queue_size}/{MP3_QUEUE_MAXSIZE})")

                logger.info(f"play_mp3: 再生開始 key={key} (キュー深さ: {queue_size})")
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._play_mp3_blocking, path, key)
                logger.info(f"play_mp3: 再生完了 key={key}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"MP3再生ワーカーでエラー key={key}: {e}")
            finally:
                try:
                    self.mp3_queue.task_done()
                except Exception:
                    pass

    def _play_mp3_blocking(self, path: str, key: str):
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.1)
        except Exception as e:
            logger.error(f"play_mp3: 再生エラー key={key}: {e}")

    async def play_mp3(self, key: str):
        if not _PYGAME_AVAILABLE:
            logger.warning("play_mp3: pygame.mixer が利用できないため再生をスキップします")
            return

        filename = self.audio_files.get(key)
        if not filename:
            logger.warning(f"play_mp3: キー '{key}' が audio_files に存在しません")
            return

        # プロジェクトルート（core/ の1階層上）を基準にファイルを探す
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base_dir, filename)
        if not os.path.exists(path):
            logger.warning(f"play_mp3: ファイルが見つかりません → {path}")
            return

        try:
            self.mp3_queue.put_nowait((key, path))
            logger.info(f"play_mp3: キューに追加 key={key} (深さ: {self.mp3_queue.qsize()}/{MP3_QUEUE_MAXSIZE})")
        except asyncio.QueueFull:
            logger.warning(
                f"play_mp3: MP3キューが満杯です (深さ: {self.mp3_queue.qsize()}) → キー '{key}' はスキップされました"
            )
