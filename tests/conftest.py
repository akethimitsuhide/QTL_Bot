"""
tests/conftest.py
=================
テスト全体で共通の設定（2026-11追加）。

メンテナンスモード（/qtl_maintenance）の状態ファイル（既定: リポジトリ直下の
kyoshin_maintenance_state.json）は、実運用の Bot と同じ場所を指す。実機でメンテナンスモードに
している最中に pytest を実行しても、テストが実運用の状態を読んで挙動が変わったり、書き換えたり
しないよう、テスト実行中だけ保存先を一時ディレクトリに向ける。
core.config が import される前（各テストモジュールの import より前）に設定する必要があるため、
conftest.py のモジュール先頭で環境変数を設定する。
"""
import os
import tempfile

os.environ["KYOSHIN_MAINTENANCE_STATE_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="qtl_test_maintenance_"), "kyoshin_maintenance_state.json"
)
