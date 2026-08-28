"""
core/env_loader.py
====================
.env の読み込みを一元管理するモジュール（2026-08-27 追加、.env整理案⑩）。

【背景】
.env.example が全機能を1ファイルに集約していたため119個の環境変数が
並ぶ巨大なファイルになり、「何がどこに書いてあるか分からない」状態に
なっていた。特に強震モニタの誤検知対策アルゴリズム調整値
（KYOSHIN_RISE_THRESHOLD 等）は、実機ログを見ながらチューニングする
上級者向けの設定であり、通常運用では一切触らない。

そこで、本体 .env とは別に「カテゴリ別 .env ファイル」を用意し、
存在すれば自動的に追加読み込みする仕組みを導入した。現時点では
Kyoshin（強震モニタ）のアルゴリズム調整値のみをこの仕組みで
.env.kyoshin に分離している（詳細は .env.kyoshin.example 参照）。

【設計方針】
- 後方互換を最優先する。カテゴリ別ファイルが存在しなくても、旧来通り
  1つの .env に全部書いてあれば何も変わらずそのまま動作する
  （python-dotenv は宣言されていないキーを単に無視するだけであり、
  値そのものは各設定の core.config 側の _env_int 等が持つデフォルト値
  にフォールバックする）。
- 優先順位は「.env に書かれた値が常に勝つ」。カテゴリ別ファイルは
  override=False で読み込むため、.env で既に設定済みの変数は
  上書きしない。同じ変数を .env とカテゴリ別ファイルの両方に書いて
  しまっても、.env 側が優先されるので事故にならない。
- 新しいカテゴリを追加したくなったら、_CATEGORY_FILES に
  ("kyoshin", "強震モニタのアルゴリズム調整値") のようなタプルを
  1行追加するだけでよい。

【使い方】
core/config.py の先頭で `load_dotenv()` を直接呼ぶ代わりに、
`from core.env_loader import load_env_files; load_env_files()` を呼ぶ。
"""
import os
import logging

from dotenv import load_dotenv

logger = logging.getLogger("QTLBot")

# 追加読み込み対象のカテゴリ別 .env ファイル。
# ファイル名は ".env.<category>"。存在しない場合は単にスキップされる
# （エラーにはならない）ので、ユーザーは必要な機能を使う場合だけ
# 該当ファイルを用意すればよい。
_CATEGORY_FILES = (
    ("kyoshin", "強震モニタ（Kyoshin）のアルゴリズム調整値。.env.kyoshin.example 参照"),
)


def load_env_files(base_dir: str | None = None) -> list[str]:
    """
    .env 本体、続いてカテゴリ別 .env ファイル（存在するもののみ）を
    この順で読み込む。戻り値は実際に読み込んだファイルパスのリスト
    （--starter 等でユーザーに「どのファイルが読まれたか」を
    表示する用途にも使える）。

    base_dir を省略した場合は、このファイル（core/env_loader.py）から
    見た1つ上のディレクトリ（リポジトリ直下）を使う。
    """
    if base_dir is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    loaded: list[str] = []

    main_path = os.path.join(base_dir, ".env")
    if os.path.isfile(main_path):
        load_dotenv(main_path)
        loaded.append(main_path)
    else:
        # .env が無い場合でも load_dotenv() 自体は例外を出さないため、
        # 明示的に何もしない（BOT_TOKEN 等の必須チェックは
        # core.config._require_env 側が別途エラーにする）。
        load_dotenv()

    for category, _description in _CATEGORY_FILES:
        path = os.path.join(base_dir, f".env.{category}")
        if os.path.isfile(path):
            load_dotenv(path, override=False)
            loaded.append(path)

    return loaded


def list_known_categories() -> tuple:
    """
    core/env_starter.py 等、他モジュールから「どんなカテゴリ別
    ファイルが存在しうるか」を参照するためのアクセサ。
    """
    return _CATEGORY_FILES
