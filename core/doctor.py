"""
core/doctor.py - 起動前の自己診断（python3 bot.py --doctor）

Bot を起動する前に「動かない原因・動いても期待どおりにならない原因」を一覧する。

【方針】
  - 読み取り専用。.env などの設定ファイルは変更せず、ネットワークにも接続しない
    （ポートの使用状況の確認のみ、手元のマシン内で行う）。
  - core.config を import しない（BOT_TOKEN が未設定でも診断できるようにするため。
    --check_env と同じ方針で、bot.py が core.config より前に呼び出す）。
  - トークン等の秘密の値は画面に表示しない。
  - 終了コード: 1つでも [ NG ] があれば 1、なければ 0（systemd の ExecStartPre 等でも使える）。
    [WARN] は「動くが確認してほしい」ものなので終了コードには影響しない。

【診断項目】
  1. 基本環境        : Python の版、依存パッケージ、空き容量、書き込み権限
  2. 設定ファイル    : .env の有無・必須項目・チャンネルID・権限、.env.example との差分
  3. 読み上げテンプレート : .env.yomiage（および .env）の YOMI_* の書式ミス・未知の項目名・
                          設定済みテンプレートのプレビュー
  4. 音声・地図・ダッシュボード : 使う設定になっている機能の前提条件
  5. 実行状態        : すでに Bot が稼働していないか（PID ファイル）
"""

from __future__ import annotations

import difflib
import errno
import importlib.util
import os
import re
import shutil
import socket
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from dotenv import dotenv_values

from core import env_merge
from core.channels import parse_channel_ids
from core.yomiage import TEMPLATE_REGISTRY, render, validate_template

OK, INFO, WARN, NG = "OK", "INFO", "WARN", "NG"
_MARK = {OK: "[ OK ]", INFO: "[INFO]", WARN: "[WARN]", NG: "[ NG ]"}

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MIN_PYTHON = (3, 11)
_MIN_FREE_MB = 500

# GIS 地図機能が使うデータ（core/gis_data.py の _FILES と同じ。テストで一致を確認している）
_GIS_FILES = ("eew_areas.geojson", "local_areas.geojson", "tsunami_areas.geojson",
              "countries.geojson", "stations.json")

# 環境変数が書かれているファイル（優先順位の高い順。実際の読み込みは core/env_loader.py）
_ENV_FILES = (".env", ".env.kyoshin", ".env.yomiage")


@dataclass
class Check:
    level: str
    title: str
    detail: str = ""


Section = tuple[str, list[Check]]


# ===============================
# 環境変数の収集
# ===============================
def _read_env_values(base_dir: str) -> dict[str, str]:
    """
    Bot 起動時と同じ優先順位（プロセスの環境変数 > .env > .env.kyoshin > .env.yomiage）で
    有効な値を集める。os.environ は変更しない。
    """
    values: dict[str, str] = {}
    for name in _ENV_FILES:
        path = os.path.join(base_dir, name)
        if os.path.isfile(path):
            for key, value in dotenv_values(path).items():
                if value is not None:
                    values.setdefault(key, value)
    for key, value in os.environ.items():
        values[key] = value
    return values


def _truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# ===============================
# 1. 基本環境
# ===============================
def check_system(base_dir: str, env: dict[str, str]) -> list[Check]:
    checks: list[Check] = []
    ver = sys.version_info
    py = f"{ver.major}.{ver.minor}.{ver.micro}"
    if (ver.major, ver.minor) >= _MIN_PYTHON:
        checks.append(Check(OK, f"Python {py}"))
    else:
        checks.append(Check(NG, f"Python {py}", f"Python {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]} 以上が必要です"))

    gis_on = _truthy(env.get("GIS_MAP_ENABLE"))
    packages = (
        # (import名, pip名, 必須か, 無い場合の影響)
        ("discord", "discord.py", True, "Bot の基盤"),
        ("aiohttp", "aiohttp", True, "HTTP通信・ダッシュボード"),
        ("websockets", "websockets", True, "EEW の受信（Wolfx / P2P地震情報）"),
        ("dotenv", "python-dotenv", True, ".env の読み込み"),
        ("psutil", "psutil", False, "メモリ・CPU 表示（!status・ダッシュボード・リソース監視）"),
        ("PIL", "Pillow", gis_on, "GIS 地図の描画（GIS_MAP_ENABLE=true の場合は必須）"),
        ("pygame", "pygame", False, "MP3 の効果音（無くても Bot は動きます）"),
    )
    for module, pip_name, required, effect in packages:
        if importlib.util.find_spec(module) is not None:
            checks.append(Check(OK, f"パッケージ {pip_name}"))
        elif required:
            checks.append(Check(NG, f"パッケージ {pip_name} が未インストール",
                                f"{effect}。pip install {pip_name}（または pip install -r requirements.txt）"))
        else:
            checks.append(Check(WARN, f"パッケージ {pip_name} が未インストール", f"{effect}。"))

    try:
        free_mb = shutil.disk_usage(base_dir).free // (1024 * 1024)
        if free_mb < _MIN_FREE_MB:
            checks.append(Check(WARN, f"ディスクの空きが少なくなっています（{free_mb} MB）",
                                "ログやキャッシュを書き込めなくなる前に空きを確保してください"))
        else:
            checks.append(Check(OK, f"ディスクの空き {free_mb} MB"))
    except OSError as e:
        checks.append(Check(WARN, "ディスクの空き容量を確認できませんでした", str(e)))

    if os.access(base_dir, os.W_OK):
        checks.append(Check(OK, "Bot のフォルダに書き込めます（ログ・PIDファイル・キャッシュ用）"))
    else:
        checks.append(Check(NG, "Bot のフォルダに書き込めません", "ログや PID ファイルを作れず、起動に失敗します"))
    return checks


# ===============================
# 2. 設定ファイル
# ===============================
def check_env_files(base_dir: str, env: dict[str, str]) -> list[Check]:
    checks: list[Check] = []
    env_path = os.path.join(base_dir, ".env")
    if not os.path.isfile(env_path):
        checks.append(Check(NG, ".env がありません", "python3 bot.py --starter で対話式に作成できます"))
    else:
        checks.append(Check(OK, ".env があります"))
        if os.name == "posix":
            mode = os.stat(env_path).st_mode & 0o777
            if mode & 0o077:
                checks.append(Check(WARN, f".env の権限が緩すぎます（{mode:o}）",
                                    "トークンが他のユーザーから読めます。chmod 600 .env を推奨します"))

    token = (env.get("BOT_TOKEN") or "").strip()
    if not token:
        checks.append(Check(NG, "BOT_TOKEN が設定されていません", "Discord Developer Portal で取得したトークンを .env に設定してください"))
    elif "your_" in token.lower() or "token_here" in token.lower():
        checks.append(Check(NG, "BOT_TOKEN がサンプルのままです"))
    elif token.count(".") != 2:
        checks.append(Check(WARN, "BOT_TOKEN の形式が通常と異なります",
                            "Discord のトークンは「.」で区切られた3つの部分からなります（値は表示しません）"))
    else:
        checks.append(Check(OK, "BOT_TOKEN が設定されています（値は表示しません）"))

    channel = (env.get("CHANNEL_ID") or "").strip()
    ids, rejected = parse_channel_ids(channel)
    if not channel:
        checks.append(Check(NG, "CHANNEL_ID が設定されていません"))
    elif rejected or not ids:
        checks.append(Check(NG, f"CHANNEL_ID は数値（複数ならカンマ区切り）で指定してください（現在: {channel!r}）"))
    else:
        suffix = f"（{len(ids)}チャンネル）" if len(ids) > 1 else ""
        checks.append(Check(OK, f"CHANNEL_ID が設定されています{suffix}"))

    bad = sorted(k for k, v in env.items()
                 if k.endswith("_CHANNEL_ID") and k != "CHANNEL_ID" and v.strip()
                 and (parse_channel_ids(v)[1] or not parse_channel_ids(v)[0]))
    for key in bad:
        checks.append(Check(WARN, f"{key} に数値以外の値があります（現在: {env[key].strip()!r}）",
                            "Discord のチャンネルIDは数字だけです（複数指定はカンマ区切り）。無効な値は無視されます"))

    # .env.example との差分
    for example_name, target_name in env_merge._FILE_PAIRS:
        example_path = os.path.join(base_dir, example_name)
        target_path = os.path.join(base_dir, target_name)
        if not (os.path.isfile(example_path) and os.path.isfile(target_path)):
            continue
        example_text = env_merge._read(example_path)
        target_text = env_merge._read(target_path)
        missing = env_merge.find_missing(example_text, target_text)
        # .env.yomiage の未知の項目名は「3. 読み上げテンプレート」で詳しく診断する
        unknown = [] if target_name == ".env.yomiage" else [
            k for k in env_merge.find_unknown(example_text, target_text)
            if k not in TEMPLATE_REGISTRY]       # .env に書いた YOMI_* は正当な設定（名前の誤りは3.で診断）
        if missing:
            names = "、".join(e.key for e in missing[:5]) + ("…" if len(missing) > 5 else "")
            checks.append(Check(INFO, f"{target_name} に {example_name} の新しい項目が {len(missing)} 件あります",
                                f"{names}。python3 bot.py --merge_env で追加できます（未設定でも既定値で動作します）"))
        if unknown:
            names = "、".join(unknown[:5]) + ("…" if len(unknown) > 5 else "")
            checks.append(Check(INFO, f"{target_name} に {example_name} に無い項目が {len(unknown)} 件あります",
                                f"{names}。廃止された項目か、名前の打ち間違いの可能性があります"))
    return checks


# ===============================
# 3. 読み上げテンプレート
# ===============================
_KEY_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def check_yomiage(base_dir: str) -> list[Check]:
    checks: list[Check] = []
    path = os.path.join(base_dir, ".env.yomiage")
    has_file = os.path.isfile(path)

    entries = []
    for name in (".env", ".env.yomiage"):
        p = os.path.join(base_dir, name)
        if not os.path.isfile(p):
            continue
        parsed = dotenv_values(p)
        with open(p, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                m = _KEY_LINE_RE.match(line)
                if m and m.group(1).startswith("YOMI_"):
                    entries.append((m.group(1), parsed.get(m.group(1)) or "", lineno, name, m.group(2).strip()))

    if not entries:
        if has_file:
            checks.append(Check(OK, ".env.yomiage に設定された項目はありません（すべて従来の文言を使用）"))
        else:
            checks.append(Check(INFO, "読み上げテンプレートは未設定です（.env.yomiage なし。すべて従来の文言を使用）",
                                "変えたい場合は cp .env.yomiage.example .env.yomiage"))
        return checks

    custom = problems = 0
    previews: list[Check] = []
    for key, value, lineno, fname, raw in entries:
        where = f"{fname} {lineno}行目 {key}"
        if key not in TEMPLATE_REGISTRY:
            hint = difflib.get_close_matches(key, TEMPLATE_REGISTRY, n=1, cutoff=0.6)
            suggest = f"もしかして {hint[0]} ですか？" if hint else ".env.yomiage.example にある項目名を確認してください"
            checks.append(Check(WARN, f"{where}: 未知の項目名です（無視されます）", suggest))
            problems += 1
            continue
        if not value.strip():
            continue                                                 # 空＝既定の文言を使う
        default, tags, samples = TEMPLATE_REGISTRY[key]

        # python-dotenv に黙って変形されやすい書き方
        quoted = raw[:1] in ('"', "'")
        if not quoted and re.search(r"\s#", raw):
            checks.append(Check(WARN, f"{where}: 引用符で囲まれていないため、「 #」以降がコメントとして切り捨てられます",
                                '値全体を "..." で囲んでください'))
            problems += 1
        if "${" in raw:
            checks.append(Check(WARN, f"{where}: 「${{」は環境変数の展開として扱われ、意図しない文言になります",
                                "$ は使わないでください"))
            problems += 1

        problem = validate_template(value.strip(), tags)
        if problem:
            checks.append(Check(WARN, f"{where}: {problem}（この項目は無視され、従来の文言で読み上げます）",
                                f"設定値: {value.strip()}"))
            problems += 1
            continue
        custom += 1
        if value.strip() == default:
            previews.append(Check(INFO, f"{key}: 既定と同じ内容です"))
        else:
            previews.append(Check(OK, f"{key}", "展開例: " + render(value.strip(), samples)))

    if problems:
        checks.append(Check(WARN, f"読み上げテンプレート: {problems} 件の問題があります（Bot は止まらず、該当項目は従来の文言になります）"))
    else:
        checks.append(Check(OK, f"読み上げテンプレート: 書式の問題はありません（設定済み {custom} 件）"))
    return checks + previews


# ===============================
# 4. 音声・地図・ダッシュボード
# ===============================
def check_features(base_dir: str, env: dict[str, str]) -> list[Check]:
    checks: list[Check] = []

    # --- 音声読み上げ ---
    engine = (env.get("TTS_ENGINE") or "aquestalk").strip().lower()
    player = (env.get("AUDIO_PLAYER") or "aplay").strip()
    if engine == "scratchtts":
        if shutil.which("ffmpeg"):
            checks.append(Check(OK, "読み上げ: ScratchTTS（ffmpeg が見つかりました）"))
        else:
            checks.append(Check(WARN, "読み上げ: TTS_ENGINE=scratchtts ですが ffmpeg が見つかりません",
                                "ピッチ変換に ffmpeg が必要です（sudo apt install ffmpeg）"))
        if not shutil.which(player):
            checks.append(Check(WARN, f"音声プレーヤー {player} が見つかりません"))
    else:
        raw = (env.get("AQUESTALK_PATH") or "").strip().rstrip("/")
        if not raw:
            checks.append(Check(INFO, "読み上げ: AQUESTALK_PATH が未設定のため無効です（通知は通常どおり届きます）"))
        else:
            if os.path.isdir(raw):
                raw = os.path.join(raw, "AquesTalkPi")
            if not os.path.isfile(raw):
                checks.append(Check(WARN, f"読み上げ: AQUESTALK_PATH のファイルが見つかりません: {raw}", "読み上げは無効になります"))
            elif not os.access(raw, os.X_OK):
                checks.append(Check(WARN, f"読み上げ: AQUESTALK_PATH に実行権限がありません: {raw}", f"chmod +x {raw}"))
            else:
                checks.append(Check(OK, "読み上げ: AquesTalkPi が使えます"))
            if not shutil.which(player):
                checks.append(Check(WARN, f"音声プレーヤー {player} が見つかりません", "AUDIO_PLAYER を確認してください"))

    # --- GIS 地図 ---
    if _truthy(env.get("GIS_MAP_ENABLE")):
        gis_dir = (env.get("GIS_MAP_DATA_DIR") or "").strip() or os.path.join(base_dir, "data", "gis")
        missing = [n for n in _GIS_FILES
                   if not (os.path.isfile(os.path.join(gis_dir, n)) and os.path.getsize(os.path.join(gis_dir, n)) > 0)]
        if not missing:
            checks.append(Check(OK, "GIS 地図: データがそろっています"))
        else:
            checks.append(Check(INFO, f"GIS 地図: データが未取得です（{', '.join(missing)}）",
                                "初回起動時に自動で取得します（python3 bot.py --refresh_gis_data で手動取得もできます）"))
    else:
        checks.append(Check(INFO, "GIS 地図: 無効です（GIS_MAP_ENABLE=false）"))

    # --- Web ダッシュボード ---
    if _truthy(env.get("WEB_DASHBOARD_ENABLED"), default=False):
        host = (env.get("WEB_DASHBOARD_HOST") or "127.0.0.1").strip()
        port_raw = (env.get("WEB_DASHBOARD_PORT") or "8080").strip()
        if not port_raw.isdigit() or not 1 <= int(port_raw) <= 65535:
            checks.append(Check(NG, f"WEB_DASHBOARD_PORT が不正です（現在: {port_raw!r}）"))
        else:
            checks.append(_check_port(host, int(port_raw)))
        from core.web_static import StaticFiles
        missing_web = StaticFiles().missing()
        if missing_web:
            checks.append(Check(WARN, f"ダッシュボードの画面ファイルが不足しています（web/ の {len(missing_web)} ファイル）",
                                "画面が表示されません。" + "、".join(missing_web[:4]) + ("…" if len(missing_web) > 4 else "")
                                + " ／ git pull で web/ フォルダごと更新してください"))
        else:
            checks.append(Check(OK, "ダッシュボード: 画面ファイル（web/）がそろっています"))
        exposed = host not in ("127.0.0.1", "localhost", "::1")
        if exposed and not (env.get("WEB_DASHBOARD_TOKEN") or "").strip() \
                and not (env.get("WEB_DASHBOARD_ALLOWED_IPS") or "").strip():
            checks.append(Check(WARN, f"ダッシュボードが {host} で公開され、認証も接続元制限もありません",
                                "同じネットワークの誰でも見られます。WEB_DASHBOARD_TOKEN または WEB_DASHBOARD_ALLOWED_IPS を設定してください"))
    else:
        checks.append(Check(INFO, "Web ダッシュボード: 無効です（WEB_DASHBOARD_ENABLED=false）"))
    return checks


def _check_port(host: str, port: int) -> Check:
    """ポートが使えるかを確認する（実際にバインドを試し、すぐに閉じる）。"""
    try:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((host, port))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            return Check(INFO, f"ダッシュボード: ポート {port} は使用中です",
                         "Bot がすでに稼働している場合は正常です。そうでなければ別のポートを指定してください")
        return Check(WARN, f"ダッシュボード: {host}:{port} を使用できません", str(e))
    return Check(OK, f"ダッシュボード: ポート {port} が使えます")


# ===============================
# 5. 実行状態
# ===============================
def check_runtime(base_dir: str, pid_alive: Optional[Callable[[int], bool]] = None) -> list[Check]:
    pid_path = os.path.join(base_dir, "bot.pid")
    if not os.path.isfile(pid_path):
        return [Check(OK, "Bot は稼働していません（PID ファイルなし）")]
    try:
        with open(pid_path, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return [Check(WARN, "bot.pid の内容を読めません", "Bot が停止していることを確認して、bot.pid を削除してください")]
    alive = pid_alive or _pid_is_alive
    if alive(pid):
        return [Check(WARN, f"Bot がすでに稼働中です（PID {pid}）",
                      "二重起動になるため、新しく起動する前に停止してください")]
    return [Check(INFO, f"bot.pid が残っていますが PID {pid} のプロセスはありません",
                  "前回の異常終了の残骸です。次回起動時に自動で無視されます")]


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ===============================
# 実行・表示
# ===============================
def run_checks(base_dir: Optional[str] = None) -> list[Section]:
    base_dir = base_dir or _BASE_DIR
    env = _read_env_values(base_dir)
    return [
        ("1. 基本環境", check_system(base_dir, env)),
        ("2. 設定ファイル", check_env_files(base_dir, env)),
        ("3. 読み上げテンプレート", check_yomiage(base_dir)),
        ("4. 音声・地図・ダッシュボード", check_features(base_dir, env)),
        ("5. 実行状態", check_runtime(base_dir)),
    ]


def format_report(sections: list[Section]) -> str:
    lines = ["=" * 60, "QTL_Bot 自己診断 (--doctor)", "=" * 60]
    counts = {OK: 0, INFO: 0, WARN: 0, NG: 0}
    for title, checks in sections:
        lines += ["", f"■ {title}"]
        for c in checks:
            counts[c.level] += 1
            lines.append(f"{_MARK[c.level]} {c.title}")
            if c.detail:
                lines.append(f"        {c.detail}")
    lines += ["", "-" * 60,
              f"結果: NG {counts[NG]} 件 / WARN {counts[WARN]} 件 / INFO {counts[INFO]} 件 / OK {counts[OK]} 件"]
    if counts[NG]:
        lines.append("[ NG ] を直してから Bot を起動してください。")
    elif counts[WARN]:
        lines.append("起動はできますが、[WARN] の内容を確認してください。")
    else:
        lines.append("問題は見つかりませんでした。")
    lines.append("※ 読み取り専用の診断です。設定ファイルの変更や外部への通信は行っていません。")
    return "\n".join(lines)


def run_doctor(base_dir: Optional[str] = None) -> int:
    """`python3 bot.py --doctor` のエントリーポイント。終了コード（NG があれば 1）を返す。"""
    sections = run_checks(base_dir)
    print(format_report(sections))
    return 1 if any(c.level == NG for _, checks in sections for c in checks) else 0
