"""
core/env_starter.py
====================
`python3 bot.py --starter` 用の対話式 .env セットアップウィザード。

【背景】
.env.example は全設定項目（15セクション）を網羅した完全なリファレンス
だが、初めて QTL_Bot を導入する人がゼロから読んで .env を作るのは
ハードルが高い。本モジュールは「本当に最初に決めれば十分な項目」だけを
対話形式で質問し、.env.example をベースに該当行だけを書き換えた .env を
生成する。それ以外の項目（フィルター閾値・音声設定の細部等）は
.env.example に書かれているデフォルト値のまま残るので、後から
`nano .env` で必要な箇所だけ調整すればよい。

【設計方針】
- discord.py 等の重い依存や core.config（BOT_TOKEN 未設定だと
  即 SystemExit する）には一切依存しない。bot.py 側で
  「--starter が指定されたら core.config を import する前に
  本モジュールだけを呼んで終了する」ようにすることで、.env が
  存在しない状態でも安全に実行できる。
- 標準ライブラリのみ使用（getpass / re / shutil / datetime）。
  軽量・低依存というプロジェクト方針に合わせている。
- 既存の .env を誤って上書きしないよう、上書き前に必ず確認し、
  かつ .env.bak.<タイムスタンプ> として退避してから書き込む。
- 対話中に Ctrl+C で中断された場合は、ファイルを一切変更せずに
  安全に終了する。

【使い方】
    python3 bot.py --starter
"""
import os
import re
import shutil
import getpass
from datetime import datetime


def _ask(prompt: str, default: str | None = None, required: bool = False,
          validator=None, error_msg: str = "入力内容が正しくありません。") -> str:
    """
    1行の入力を受け付ける。default が指定されていれば Enter だけで採用する。
    validator(value) -> bool を渡すと、それが True になるまで再入力を促す。
    """
    suffix = f"（既定値: {default}）" if default is not None else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw:
            if default is not None:
                raw = default
            elif not required:
                return ""
            else:
                print("  ※ 必須項目です。空欄では進めません。")
                continue
        if validator is not None and not validator(raw):
            print(f"  ※ {error_msg}")
            continue
        return raw


def _ask_secret(prompt: str, required: bool = True) -> str:
    """トークン等、画面に表示したくない入力を受け付ける（getpass使用）。"""
    while True:
        raw = getpass.getpass(f"{prompt}: ").strip()
        if raw:
            return raw
        if not required:
            return ""
        print("  ※ 必須項目です。空欄では進めません。")


def _ask_yesno(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt}（{hint}）: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  ※ y または n で答えてください。")


def _is_digits(value: str) -> bool:
    return value.isdigit()


def _set_env_line(content: str, key: str, value: str) -> str:
    """
    content 内の "KEY=..." で始まる行（1箇所のみ想定）を
    "KEY=value" に置き換える。該当行が見つからない場合は変更しない
    （.env.example 側の項目名変更にすぐには追従できないが、その場合も
    ウィザード自体は残りの項目を正常に処理できるよう安全側に倒す）。
    """
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(content) is None:
        print(f"  ⚠ 警告: .env.example 内に {key}= の行が見つからなかったため、"
              f"この項目はスキップしました（手動で .env に追記してください）")
        return content
    return pattern.sub(lambda _m: f"{key}={value}", content, count=1)


def run_env_starter() -> None:
    """
    対話形式で .env を生成するウィザード本体。
    bot.py から `python3 bot.py --starter` 実行時にのみ呼ばれる。
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    example_path = os.path.join(base_dir, ".env.example")
    target_path = os.path.join(base_dir, ".env")

    print("=" * 60)
    print("QTL_Bot 初期セットアップウィザード（.env 作成）")
    print("=" * 60)
    print("最低限必要な項目だけ質問します。それ以外の詳細設定は")
    print(".env.example に書かれているデフォルト値のまま .env に")
    print("書き込まれるので、後から nano .env で調整できます。")
    print("Ctrl+C でいつでも中断できます（.env は変更されません）。")
    print()

    if not os.path.isfile(example_path):
        print(f"❌ .env.example が見つかりません: {example_path}")
        print("   リポジトリ直下（bot.py と同じ場所）で実行しているか確認してください。")
        return

    if os.path.isfile(target_path):
        print(f"⚠ 既に .env が存在します: {target_path}")
        if not _ask_yesno("上書きしますか？（既存ファイルは自動でバックアップされます）", default=False):
            print("中断しました。.env は変更していません。")
            return
        print()

    try:
        with open(example_path, encoding="utf-8") as f:
            content = f.read()

        # ── 1. Discord 基本設定（必須） ──
        print("── Discord 基本設定 ──")
        bot_token = _ask_secret("Discord Bot のトークン（Developer Portal で取得したもの）")
        channel_id = _ask(
            "デフォルト通知先チャンネル ID（数字のみ）",
            required=True, validator=_is_digits,
            error_msg="チャンネルIDは数字のみで入力してください。",
        )
        content = _set_env_line(content, "BOT_TOKEN", bot_token)
        content = _set_env_line(content, "CHANNEL_ID", channel_id)
        print()

        # ── 2. 通知先チャンネルの分離（任意） ──
        print("── 通知先チャンネルの分離（任意） ──")
        print("地震・津波・火山などを別々のチャンネルに分けたい場合のみ設定します。")
        print("空欄のままにすると、すべて上記のデフォルトチャンネルに送られます。")
        if _ask_yesno("通知の種類ごとにチャンネルを分けますか？", default=False):
            for key, label in [
                ("EEW_CHANNEL_ID", "EEW（緊急地震速報）用チャンネル ID"),
                ("QUAKE_CHANNEL_ID", "地震情報用チャンネル ID"),
                ("TSUNAMI_CHANNEL_ID", "津波情報用チャンネル ID"),
                ("VOLCANO_CHANNEL_ID", "火山情報用チャンネル ID"),
            ]:
                value = _ask(f"{label}（空欄でデフォルトチャンネルを使用）",
                              validator=lambda v: v == "" or v.isdigit(),
                              error_msg="空欄か数字のみで入力してください。")
                if value:
                    content = _set_env_line(content, key, value)
        print()

        # ── 3. Web Dashboard（任意） ──
        print("── Web Dashboard（任意） ──")
        print("稼働状況を http://<Botのアドレス>:<ポート>/status で確認できる機能です。")
        if _ask_yesno("Web Dashboard を有効にしますか？", default=False):
            content = _set_env_line(content, "WEB_DASHBOARD_ENABLED", "true")
            port = _ask("待ち受けポート番号", default="8080", validator=_is_digits,
                        error_msg="ポート番号は数字のみで入力してください。")
            content = _set_env_line(content, "WEB_DASHBOARD_PORT", port)
        print()

        # ── 4. 音声読み上げ（任意） ──
        print("── 音声読み上げ（任意・Raspberry Pi + AquesTalkPi 導入時のみ） ──")
        aquestalk_path = _ask(
            "AquesTalkPi 実行ファイルのパス（未導入・不要なら空欄のままEnter）"
        )
        if aquestalk_path:
            content = _set_env_line(content, "AQUESTALK_PATH", aquestalk_path)
        print()

        # ── 5. ログレベル（任意） ──
        print("── ログレベル（任意） ──")
        log_level = _ask(
            "ログレベル（INFO / DEBUG / WARNING）", default="INFO",
            validator=lambda v: v.upper() in ("INFO", "DEBUG", "WARNING", "ERROR", "CRITICAL"),
            error_msg="INFO / DEBUG / WARNING / ERROR / CRITICAL のいずれかを入力してください。",
        )
        content = _set_env_line(content, "LOG_LEVEL", log_level.upper())
        print()

    except KeyboardInterrupt:
        print("\n\n中断しました。.env は作成・変更していません。")
        return

    # ── 既存 .env のバックアップ ──
    if os.path.isfile(target_path):
        backup_path = f"{target_path}.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(target_path, backup_path)
        print(f"既存の .env を {os.path.basename(backup_path)} にバックアップしました")

    with open(target_path, "w", encoding="utf-8") as f:
        f.write(content)

    print()
    print("=" * 60)
    print(f"✅ .env を作成しました: {target_path}")
    print("=" * 60)
    print("残りの詳細設定（通知フィルター・音声・強震モニタ等）は")
    print(".env.example のデフォルト値のまま反映されています。")
    print("必要に応じて `nano .env` で調整してください（README.md の")
    print("「環境変数リファレンス」に全項目の説明があります）。")
    print()
    print("Bot を起動するには:")
    print("    python3 bot.py")
