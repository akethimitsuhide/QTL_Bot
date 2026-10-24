"""
core/yomiage_test.py - 読み上げテンプレートのテスト再生（python3 bot.py --test_yomiage）

.env.yomiage に書いたテンプレートが、実際にどう読み上げられるかを、Discord に接続せず
（＝通知を送らず）手元で確認する。

【使い方】
  python3 bot.py --test_yomiage                      # 全項目の展開例を一覧表示（再生しない）
  python3 bot.py --test_yomiage EEW_FIRST_WARN       # その項目を展開して、実際に音声を再生する
  python3 bot.py --test_yomiage all                  # 全項目を順に再生する
  python3 bot.py --test_yomiage EEW_FIRST_WARN --blank 警報地域
                                                     # 「警報地域」が空のとき（[ ] が省略されるとき）の読み上げを確認する
  python3 bot.py --test_yomiage EEW_FIRST_WARN --no-play
                                                     # 再生せず、文だけ表示する
  項目名は YOMI_ を付けても付けなくてもよい（大文字小文字も区別しない）。
  一意に決まる範囲で、後ろ半分だけの指定（例: FIRST_WARN）も使える。

【仕組み】
  - 展開に使う値は、--doctor の「展開例」と同じサンプル値（core/yomiage.py の TEMPLATE_REGISTRY）。
  - ユーザー設定（.env・.env.yomiage）は Bot 起動時と同じ読み込み方で反映する。
    書式ミスのある項目は、Bot と同じく従来の文言に戻る（その旨も表示する）。
  - 再生は Bot と同じ設定（.env の TTS_ENGINE・AQUESTALK_PATH・AUDIO_PLAYER 等）と同じ
    音声合成・プレーヤーを使う。再生には .env の必須項目（BOT_TOKEN 等）が必要。
    一覧表示・--no-play は設定が不完全でも使える。
  - Discord へは一切接続しない。実際の通知（Embed）は送られない。
"""

from __future__ import annotations

import asyncio
import os
from typing import Callable, Optional

from core.env_loader import load_env_files
from core.yomiage import TEMPLATE_REGISTRY, TemplateSet

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_USAGE = """使い方:
  python3 bot.py --test_yomiage                  全項目の展開例を一覧表示（再生しない）
  python3 bot.py --test_yomiage <項目名>          その項目を展開して音声を再生する（例: EEW_FIRST_WARN）
  python3 bot.py --test_yomiage all              全項目を順に再生する
  オプション:
    --blank <タグ,タグ>   指定したタグを空にして展開する（[ ] の省略を確認できる。例: --blank 警報地域）
    --no-play             再生せず、文だけ表示する"""


def resolve_key(name: str) -> tuple[Optional[str], list[str]]:
    """
    ユーザー指定の名前を YOMI_* の項目名に解決する。戻り値: (一意に決まった項目名 or None, 候補のリスト)。
    "EEW_FIRST_WARN" / "yomi_eew_first_warn" / 一意に決まる後半（"FIRST_WARN"）を受け付ける。
    """
    wanted = name.strip().upper()
    if not wanted.startswith("YOMI_"):
        wanted = "YOMI_" + wanted
    if wanted in TEMPLATE_REGISTRY:
        return wanted, [wanted]
    bare = wanted[len("YOMI_"):]
    candidates = [k for k in TEMPLATE_REGISTRY if k.endswith("_" + bare) or bare in k]
    return (candidates[0], candidates) if len(candidates) == 1 else (None, candidates)


def expand(key: str, env: dict[str, str], blank: tuple[str, ...] = ()) -> tuple[str, bool, Optional[str]]:
    """
    key のテンプレートを、ユーザー設定を反映して展開する。
    戻り値: (展開した文, ユーザー設定が使われたか, 設定ミスの説明 or None)
    """
    default, tags, samples = TEMPLATE_REGISTRY[key]
    warnings: list[str] = []
    template_set = TemplateSet({key: default}, tags, {key: env.get(key)}, warn=warnings.append)
    values = dict(samples)
    for tag in blank:
        values[tag] = ""
    text = template_set.render(key, values)
    return text, template_set.is_custom(key), (warnings[0] if warnings else None)


async def _play_async(text: str) -> bool:
    """Bot と同じ音声合成・プレーヤーで再生する。成功したら True。"""
    try:
        from core.config import AUDIO_PLAYER, TTS_ENGINE          # 必須項目が無いと SystemExit
        from core.tts_engines import synthesize
    except SystemExit:
        print("  ! 再生に必要な .env の設定が不足しています（BOT_TOKEN・CHANNEL_ID 等）。"
              "--no-play なら設定なしでも使えます")
        return False
    audio = await synthesize(TTS_ENGINE, text)
    if not audio:
        print(f"  ! 音声を生成できませんでした（TTS_ENGINE={TTS_ENGINE}）。"
              "python3 bot.py --doctor で音声の設定を確認できます")
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            AUDIO_PLAYER, "-",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate(input=audio)
    except FileNotFoundError:
        print(f"  ! 音声プレーヤー {AUDIO_PLAYER!r} が見つかりません（AUDIO_PLAYER で指定できます）")
        return False
    if proc.returncode != 0:
        print(f"  ! プレーヤーがエラー終了しました（コード {proc.returncode}）: {err.decode(errors='replace')[:200]}")
        return False
    return True


def _parse_args(args: list[str]) -> tuple[Optional[str], tuple[str, ...], bool, Optional[str]]:
    """戻り値: (対象, 空にするタグ, 再生しないか, エラー文)。"""
    target: Optional[str] = None
    blank: tuple[str, ...] = ()
    no_play = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--no-play":
            no_play = True
        elif a == "--blank":
            if i + 1 >= len(args):
                return None, (), False, "--blank にはタグ名が必要です（例: --blank 警報地域）"
            blank = tuple(t for t in args[i + 1].replace("、", ",").split(",") if t.strip())
            blank = tuple(t.strip() for t in blank)
            i += 1
        elif a.startswith("--"):
            return None, (), False, f"不明なオプションです: {a}"
        elif target is None:
            target = a
        else:
            return None, (), False, f"項目名は1つだけ指定してください（余分な指定: {a}）"
        i += 1
    return target, blank, no_play, None


def run_test_yomiage(args: list[str], base_dir: Optional[str] = None,
                     play: Optional[Callable[[str], bool]] = None, out: Callable[[str], None] = print) -> int:
    """`python3 bot.py --test_yomiage ...` のエントリーポイント。終了コードを返す。"""
    base_dir = base_dir or _BASE_DIR
    target, blank, no_play, error = _parse_args(args)
    if error:
        out(error)
        out(_USAGE)
        return 2
    load_env_files(base_dir)                                   # Bot 起動時と同じ読み込み（.env 優先）
    env = {k: v for k, v in os.environ.items() if k.startswith("YOMI_")}
    do_play = play or (lambda text: asyncio.run(_play_async(text)))

    # --- 一覧 ---
    if target is None:
        out("読み上げテンプレートの展開例（サンプル値で展開。再生はしません）")
        out("  ※ 実際に聞くには: python3 bot.py --test_yomiage <項目名>")
        for key in TEMPLATE_REGISTRY:
            text, custom, warning = expand(key, env, blank)
            mark = "★設定済み" if custom else "  既定  "
            out(f"[{mark}] {key}")
            out(f"           {text}")
            if warning:
                out(f"           ! {warning}")
        return 0

    # --- 対象の決定 ---
    if target.lower() == "all":
        keys = list(TEMPLATE_REGISTRY)
    else:
        key, candidates = resolve_key(target)
        if key is None:
            if candidates:
                out(f"項目名が複数に当てはまります: {target}")
                out("  候補: " + "、".join(candidates))
            else:
                out(f"項目が見つかりません: {target}")
                out("  python3 bot.py --test_yomiage で全項目の一覧を確認できます")
            return 2
        keys = [key]

    out("=== 読み上げテンプレートのテスト再生（Discord へは通知しません）===")
    failures = 0
    for key in keys:
        text, custom, warning = expand(key, env, blank)
        out(f"\n{key}" + ("（.env.yomiage の設定を使用）" if custom else "（既定の文言）"))
        if blank:
            out(f"  空にしたタグ: {'、'.join(blank)}")
        if warning:
            out(f"  ! {warning}")
        out(f"  読み上げ: {text}")
        if not text.strip():
            out("  （空の文のため、実際の通知では読み上げられません）")
            continue
        if not no_play and not do_play(text):
            failures += 1
    if failures:
        out(f"\n再生に失敗した項目が {failures} 件ありました。")
        return 1
    return 0
