"""
core/tts_engines.py
====================
TTS_ENGINE の設定値に応じて、テキストから再生可能な音声バイト列（wav/mp3等、
指定した AUDIO_PLAYER がそのまま再生できる形式）を生成する。

現在サポートするエンジン:
    - "aquestalk"  : ローカルの AquesTalkPi バイナリ（raw wav 出力）
    - "scratchtts" : Scratch の音声合成API（https://synthesis-service.scratch.mit.edu/synth）
                     を HTTP 経由で呼び出す。取得した音声は ffmpeg で
                     約3セミトーン（2^(3/12)倍）ピッチアップして返す
                     （ScratchTTSの声が低めに聞こえるための調整。
                     テンポ・再生時間は変えずピッチだけを上げる）。

いずれのエンジンも、生成に失敗した場合は None を返す
（呼び出し元の speech_worker が「生成失敗」としてログを出し、
 再生をスキップする）。

【ClientSession のシングルトン化について】
ScratchTTS用の aiohttp.ClientSession はモジュールレベルで1つだけ保持し、
使い回す（_get_scratchtts_session()）。TCP接続確立・TLSハンドシェイクの
コストを毎回払わないようにするための最適化。Bot終了時は
close_scratchtts_session() を呼び出して明示的にクローズすること
（bot.py の finally 節で呼び出し済み）。
"""
import asyncio
import logging
import math
import urllib.parse

import aiohttp

from core.config import (
    AQUESTALK_PATH, AQUESTALK_SPEED,
    SCRATCHTTS_URL, SCRATCHTTS_LOCALE, SCRATCHTTS_GENDER, SCRATCHTTS_TIMEOUT_SEC,
)

logger = logging.getLogger("QTLBot")

# ScratchTTS の音声を上げるセミトーン数。
# 周波数比 = 2^(semitones/12)。3セミトーンで約1.1892倍。
SCRATCHTTS_PITCH_SEMITONES = 3.0
_PITCH_RATIO = 2 ** (SCRATCHTTS_PITCH_SEMITONES / 12)

# ── ScratchTTS 用 ClientSession シングルトン ──
# 以前は synthesize_scratchtts() 呼び出しのたびに aiohttp.ClientSession を
# 新規生成していたため、TCP接続確立・TLSハンドシェイクのコストが読み上げの
# たびに発生していた（ScratchTTSは仕様上「重複読み上げOK」で呼び出し頻度が
# 高くなりうるため、影響が大きい）。モジュールレベルで1つのセッションを
# 保持し、初回呼び出し時にのみ生成して使い回す。
# ClientSession はイベントループに紐づくため、生成は必ず実行中の
# イベントループ内（= 非同期関数の中）で行う。二重生成を防ぐため
# asyncio.Lock で保護する。
_scratchtts_session: aiohttp.ClientSession | None = None
_scratchtts_session_lock = asyncio.Lock()


async def _get_scratchtts_session() -> aiohttp.ClientSession:
    """ScratchTTS用のClientSessionをシングルトンとして取得する（なければ生成）。"""
    global _scratchtts_session
    if _scratchtts_session is None or _scratchtts_session.closed:
        async with _scratchtts_session_lock:
            # ロック待ちの間に他のタスクが生成済みの可能性があるため再チェック
            if _scratchtts_session is None or _scratchtts_session.closed:
                _scratchtts_session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=SCRATCHTTS_TIMEOUT_SEC)
                )
                logger.info("ScratchTTS: ClientSession を新規生成しました")
    return _scratchtts_session


async def close_scratchtts_session() -> None:
    """Bot終了時に呼び出し、ScratchTTS用のClientSessionを明示的に閉じる。"""
    global _scratchtts_session
    if _scratchtts_session is not None and not _scratchtts_session.closed:
        await _scratchtts_session.close()
        logger.info("ScratchTTS: ClientSession を閉じました")
    _scratchtts_session = None


async def synthesize_aquestalk(text: str) -> bytes | None:
    """AquesTalkPi バイナリを実行し、生成された wav バイト列を返す。"""
    if not AQUESTALK_PATH:
        logger.info("AQUESTALK_PATH 未設定のため音声読み上げをスキップします")
        return None

    escaped = text.replace('"', '\\"')
    try:
        proc = await asyncio.create_subprocess_exec(
            AQUESTALK_PATH, "-s", str(AQUESTALK_SPEED), escaped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        logger.info(f"AquesTalkPi 終了コード={proc.returncode} 出力バイト数={len(out)}")
        if err:
            logger.warning(f"AquesTalkPi stderr: {err.decode(errors='replace')[:200]}")
        return out or None
    except Exception as e:
        logger.error(f"AquesTalkPi 実行エラー: {e}")
        return None


async def synthesize_scratchtts(text: str) -> bytes | None:
    """
    ScratchTTS API から音声を取得し、ffmpeg で約3セミトーンピッチアップして返す。

    エンドポイント仕様:
        GET {SCRATCHTTS_URL}?locale=<ロケール>&gender=<female|male>&text=<内容>
    """
    params = {
        "locale": SCRATCHTTS_LOCALE,
        "gender": SCRATCHTTS_GENDER,
        "text": text,
    }
    url = f"{SCRATCHTTS_URL}?{urllib.parse.urlencode(params)}"

    try:
        session = await _get_scratchtts_session()
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning(f"ScratchTTS 取得失敗: HTTP {resp.status} ({text[:30]})")
                return None
            raw_audio = await resp.read()
    except asyncio.TimeoutError:
        logger.warning(f"ScratchTTS タイムアウト（{SCRATCHTTS_TIMEOUT_SEC}秒）: {text[:30]}")
        return None
    except Exception as e:
        logger.warning(f"ScratchTTS 取得エラー: {e} ({text[:30]})")
        return None

    if not raw_audio:
        logger.warning(f"ScratchTTS: 空のレスポンス ({text[:30]})")
        return None

    pitched = await _pitch_shift_ffmpeg(raw_audio, _PITCH_RATIO)
    if pitched is None:
        # ピッチシフトに失敗しても、元音声はそのまま再生できるようフォールバック
        logger.warning("ScratchTTS: ピッチシフトに失敗したため、元の音声をそのまま使用します")
        return raw_audio
    return pitched


async def _probe_sample_rate(audio_bytes: bytes) -> int | None:
    """ffprobe で音声データの実サンプルレート(Hz)を取得する。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate",
            "-of", "csv=p=0",
            "pipe:0",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(input=audio_bytes)
        if proc.returncode != 0 or not out:
            logger.warning(f"ffprobe サンプルレート取得失敗: {err.decode(errors='replace')[:200]}")
            return None
        return int(out.decode().strip())
    except FileNotFoundError:
        logger.warning("ffprobe が見つかりません")
        return None
    except (ValueError, Exception) as e:
        logger.warning(f"ffprobe 実行エラー: {e}")
        return None


async def _pitch_shift_ffmpeg(audio_bytes: bytes, ratio: float) -> bytes | None:
    """
    ffmpeg の asetrate + atempo フィルタで、再生時間を変えずにピッチだけを
    ratio 倍にする。

    asetrate でサンプルレートを ratio 倍にすると、ピッチも再生速度も
    ratio 倍になってしまうため、atempo で速度を 1/ratio 倍に戻すことで
    「ピッチだけが ratio 倍、テンポは変わらない」状態にする。
    出力フォーマットは wav に統一する（pygame.mixer.music.load で
    そのまま再生できるようにするため）。
    """
    # asetrate は「出力サンプルレートをこの値にする」フィルタで、これにより
    # ピッチも再生速度も変化する。ffmpeg のフィルタ式内では入力の実際の
    # サンプルレートを参照する変数は使えないため、事前に ffprobe で
    # 入力音声の実サンプルレートを取得し、それに ratio を掛けた具体的な
    # 数値を asetrate に渡す。その後 atempo で速度を 1/ratio 倍に戻し、
    # テンポ（再生時間）を元に戻す。
    input_sample_rate = await _probe_sample_rate(audio_bytes)
    if input_sample_rate is None:
        logger.warning("ffmpeg: 入力音声のサンプルレート取得に失敗しました")
        return None
    new_sample_rate = int(round(input_sample_rate * ratio))
    filter_chain = f"asetrate={new_sample_rate},aresample=44100,atempo={1/ratio}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i", "pipe:0",
            "-filter:a", filter_chain,
            "-f", "wav",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(input=audio_bytes)
        if proc.returncode != 0 or not out:
            logger.warning(
                f"ffmpeg ピッチシフト失敗 (code={proc.returncode}): "
                f"{err.decode(errors='replace')[:200]}"
            )
            return None
        return out
    except FileNotFoundError:
        logger.warning("ffmpeg が見つかりません。ScratchTTS のピッチシフトをスキップします")
        return None
    except Exception as e:
        logger.error(f"ffmpeg ピッチシフト実行エラー: {e}")
        return None


async def synthesize(engine: str, text: str) -> bytes | None:
    """TTS_ENGINE の値に応じて適切なエンジンで音声を合成する。"""
    if engine == "scratchtts":
        return await synthesize_scratchtts(text)
    return await synthesize_aquestalk(text)
