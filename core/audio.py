"""
core/audio.py
=============
音声読み上げ（AquesTalkPi / ScratchTTS）・MP3再生を提供する Mixin クラス。

【TTS_ENGINE による読み上げエンジンの切り替え】
.env の TTS_ENGINE で読み上げエンジンを選択できる：
    TTS_ENGINE=aquestalk  （デフォルト）ローカルの AquesTalkPi バイナリを使用
    TTS_ENGINE=scratchtts Scratch の音声合成APIをネットワーク経由で使用
実際の音声合成は core/tts_engines.py の synthesize() に委譲している。
ScratchTTS を選んだ場合、取得した音声は ffmpeg で約3セミトーン
（core.tts_engines.SCRATCHTTS_PITCH_SEMITONES）ピッチアップしてから再生する
（テンポ・再生時間は変えずピッチだけを上げる。詳細は tts_engines.py 参照）。

【設計方針: なぜ Mixin なのか】
speak_local() や play_mp3() は notify_quake / notify_tsunami / notify_volcano
など、ほぼ全ての Cog から呼ばれる。しかし Cog を分割すると、これらのメソッドは
物理的に別クラスへ移ってしまう。

解決策として、このファイルは「メソッドの実装だけ」を提供する AudioMixin を定義する。
各 Cog は commands.Cog と AudioMixin を多重継承することで、
自分自身の self.speech_queue / self.mp3_queue / self.audio_files を使って
これらのメソッドをそのまま呼び出せるようになる。

    class TsunamiCog(commands.Cog, AudioMixin):
        def __init__(self, bot):
            self.bot = bot
            self.speech_queue = asyncio.PriorityQueue(maxsize=SPEECH_QUEUE_MAXSIZE)
            self.mp3_queue    = asyncio.Queue(maxsize=MP3_QUEUE_MAXSIZE)
            self.audio_files  = {...}
            ...

【Step8時点: 音声キューの実体を持つCogと、それを参照するだけのCogが混在】
cogs/audio_shared.py の AudioCog が「音声の実体」を持つ唯一の Cog になった
（EewCog・QuakeInfoCog がこれを使う）。この2Cogは自分自身のキューを持たず、
AudioMixin ではなく本ファイルの AudioClientMixin を継承し、
`self.bot.get_cog("AudioCog").speak_local(...)` に相当する処理を
簡潔に呼び出せるようにしている。

一方 tsunami.py / volcano.py / usgs.py / other.py は今なお AudioMixin を
直接継承し、自分自身の speech_queue / mp3_queue を保持したままである
（Step8ではEEW・地震情報の2Cogのみを対象とした）。将来的に全Cogを
AudioCogへ統合する場合は、これらのCogも AudioClientMixin に置き換える。

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
    TTS_ENGINE, SCRATCHTTS_LOCALE, SCRATCHTTS_GENDER,
)
from core.tts_engines import synthesize, SCRATCHTTS_PITCH_SEMITONES
from core.ews_signal import SAMPLE_RATE as EWS_PCM_SAMPLE_RATE

logger = logging.getLogger("QTLBot")

try:
    import pygame
    # 【2026-08-04 修正】frequency未指定だとpygame.mixerはデフォルトの
    # 22050Hzでミキサーを初期化する。pygame.mixer.music（MP3再生）は
    # ファイルを都度デコードして再生するため、ミキサー周波数と実ファイルの
    # サンプルレートが異なっていてもSDL_mixerが自動リサンプリングして
    # 正しいピッチで再生されるが、pygame.mixer.Sound(buffer=...)
    # （core.ews_signal で生成する生PCMバッファの再生に使用）は
    # 自動リサンプリングされず、バッファの中身をミキサー周波数・
    # チャンネル数のデータとしてそのまま解釈してしまう。
    #
    # 実機検証で以下2点のズレが同時に起きていたことを確認した:
    #   1. サンプルレート: ews_signal.SAMPLE_RATE=44100Hz に対し、
    #      ミキサーはデフォルトの22050Hzで初期化されていた
    #   2. チャンネル数: ews_signal が生成するPCMはモノラル(1ch)だが、
    #      ミキサーはデフォルトのステレオ(2ch)で初期化されていた
    #      （モノラルデータをステレオのインターリーブとして誤読し、
    #      実際のサンプル数が半分に解釈されてしまう）
    # どちらも「実際より短い時間で再生し切ってしまう＝ピッチが上がる」
    # 方向にズレるため、両方が重なると影響が増幅する。
    # ews_signal.SAMPLE_RATE・生成PCMのチャンネル数（モノラル）と
    # 一致させるため、frequency・channels を明示的に指定する。
    pygame.mixer.init(frequency=44100, channels=1)
    _PYGAME_AVAILABLE = True
except Exception as e:
    _PYGAME_AVAILABLE = False
    print(f"[WARNING] pygame.mixer の初期化に失敗しました。MP3再生は無効です: {e}")


class AudioClientMixin:
    """
    自分自身のキューを持たず、共通の AudioCog（cogs/audio_shared.py）に
    処理を委譲するための薄い Mixin。

    EewCog・QuakeInfoCog のように「音声の実体は持たないが speak_local /
    play_mp3 を呼びたい」Cogがこれを継承する。継承先は self.bot を
    持っている必要がある（commands.Cog は通常持っている）。

    AudioCog が何らかの理由でまだ登録されていない場合は、警告ログを
    出すだけで例外にはしない（起動順序の問題で通知自体が失われるのを防ぐ）。
    """

    def _get_audio_cog(self):
        audio_cog = self.bot.get_cog("AudioCog")
        if audio_cog is None:
            logger.warning(
                "AudioCog が見つかりません。音声再生をスキップします "
                "（bot.py での登録順序を確認してください）"
            )
        return audio_cog

    async def speak_local(self, text: str, priority: int = 2):
        audio_cog = self._get_audio_cog()
        if audio_cog is not None:
            await audio_cog.speak_local(text, priority)

    async def play_mp3(self, key: str):
        audio_cog = self._get_audio_cog()
        if audio_cog is not None:
            await audio_cog.play_mp3(key)

    async def play_ews_pcm(self, pcm_bytes: bytes, label: str = "ews"):
        audio_cog = self._get_audio_cog()
        if audio_cog is not None:
            await audio_cog.play_ews_pcm(pcm_bytes, label)


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
        if TTS_ENGINE == "aquestalk" and not AQUESTALK_PATH:
            logger.info("AQUESTALK_PATH 未設定のため音声読み上げ機能は無効です")
            return
        logger.info(
            f"音声読み上げ開始: engine={TTS_ENGINE} "
            f"/ player={AUDIO_PLAYER}"
            + (f" / aquestalk={AQUESTALK_PATH} speed={AQUESTALK_SPEED}" if TTS_ENGINE == "aquestalk" else
               f" / scratchtts locale={SCRATCHTTS_LOCALE} gender={SCRATCHTTS_GENDER} "
               f"pitch=+{SCRATCHTTS_PITCH_SEMITONES}semitones")
        )
        queue_warn_threshold = max(SPEECH_QUEUE_MAXSIZE * 0.8, 1)

        while not self.bot.is_closed():
            try:
                priority, text = await self.speech_queue.get()
                queue_size = self.speech_queue.qsize()

                if queue_size >= queue_warn_threshold:
                    logger.warning(f"音声キュー圧力高 (深さ: {queue_size}/{SPEECH_QUEUE_MAXSIZE})")

                logger.info(f"音声再生開始 [優先度{priority}] (キュー深さ: {queue_size}): {text[:60]}")

                audio_out = await synthesize(TTS_ENGINE, text)

                if audio_out:
                    play_proc = await asyncio.create_subprocess_exec(
                        AUDIO_PLAYER, "-",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, play_err = await play_proc.communicate(input=audio_out)
                    logger.info(f"{AUDIO_PLAYER} 終了コード={play_proc.returncode}")
                    if play_err and play_proc.returncode != 0:
                        logger.warning(f"{AUDIO_PLAYER} stderr: {play_err.decode(errors='replace')[:200]}")
                else:
                    logger.warning(f"音声生成失敗（出力なし, engine={TTS_ENGINE}）: {text[:60]}")

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

    def _play_pcm_blocking(self, pcm_bytes: bytes, label: str = "ews"):
        """
        16bit/モノラル/44100Hz の生PCMバイト列を、外部ファイルへ書き出さずに
        直接再生する（pygame.mixer.Sound の buffer 引数を使用）。
        _play_mp3_blocking と同様、pygame自体がブロッキングAPIのため
        run_in_executor から呼ばれる想定。
        """
        try:
            sound = pygame.mixer.Sound(buffer=pcm_bytes)
            channel = sound.play()
            while channel is not None and channel.get_busy():
                time.sleep(0.1)
        except Exception as e:
            logger.error(f"play_ews_pcm: 再生エラー label={label}: {e}")

    async def play_ews_pcm(self, pcm_bytes: bytes, label: str = "ews") -> None:
        """
        EWS（緊急警報放送）信号音等、core.ews_signal で生成した生PCM
        バイト列を、外部ファイルへ一切出力せずに再生する。

        既存の mp3_queue（ファイルパスベース）とは異なる経路のため、
        専用のキューは持たずその場で再生する。津波警報・大津波警報の
        発表・更新という緊急性の高い通知であり、他の音声キューの順番を
        待たせず即座に鳴らすことを優先する設計とした。
        pygame.mixer.Sound.play() 自体はブロッキングしないが、
        再生完了を待つ処理（get_busy() ポーリング）はブロッキングする
        ため、_play_mp3_blocking と同様 run_in_executor 経由で呼ぶ。
        """
        if not _PYGAME_AVAILABLE:
            logger.warning("play_ews_pcm: pygame.mixer が利用できないため再生をスキップします")
            return
        if not pcm_bytes:
            logger.warning(f"play_ews_pcm: PCMデータが空のため再生をスキップします label={label}")
            return

        # 【2026-08-04 追加】ミキサーの実際の初期化周波数と、
        # core.ews_signal が生成したPCMのサンプルレート（44100Hz固定）が
        # 一致しているかを検証する。一致していない場合、
        # pygame.mixer.Sound(buffer=...) はリサンプリングせずバッファを
        # そのまま解釈するため、意図しないピッチ・再生速度で鳴ってしまう
        # （実機で「音が高くなる」不具合として発現した根本原因）。
        # ここでは強制的に補正はせず、設定不一致に気づけるよう警告のみ
        # 出す（pygame.mixer.init の呼び出し側=モジュールロード時点で
        # frequency=44100 を明示指定することを主対策としている）。
        try:
            mixer_freq, _fmt, mixer_channels = pygame.mixer.get_init()
            if mixer_freq != EWS_PCM_SAMPLE_RATE:
                logger.warning(
                    f"play_ews_pcm: pygame.mixer の初期化周波数（{mixer_freq}Hz）が "
                    f"EWS PCMのサンプルレート（{EWS_PCM_SAMPLE_RATE}Hz）と一致していません。"
                    f"再生速度・ピッチが意図しない値になる可能性があります。"
                )
            if mixer_channels != 1:
                logger.warning(
                    f"play_ews_pcm: pygame.mixer のチャンネル数（{mixer_channels}ch）が "
                    f"EWS PCMのチャンネル数（1ch, モノラル）と一致していません。"
                    f"再生速度・ピッチが意図しない値になる可能性があります。"
                )
        except Exception:
            pass  # get_init() 自体の失敗は再生継続を優先し無視する

        logger.info(f"play_ews_pcm: 再生開始 label={label} ({len(pcm_bytes)} bytes)")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._play_pcm_blocking, pcm_bytes, label)
        logger.info(f"play_ews_pcm: 再生完了 label={label}")
