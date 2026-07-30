"""
core/test_runner.py
====================
`python3 bot.py --test_<cog名> <JSONファイルパス>` によるCLIテスト実行機構。

【背景】
QTL_Botには自動テストが存在せず、EEW・地震情報・津波・火山等の重大な
通知ロジックが実際の災害発生まで動作確認されないという問題があった
（令和8年熊本地震で発覚した複数のバグも、実運用で初めて顕在化した）。

このモジュールは、実際にDiscordへ接続した状態で「サンプルJSONデータを
使って特定Cogの通知ロジックだけを動かし、结果を実チャンネルで目視確認する」
簡易テスト実行機構を提供する。単体テストフレームワーク（pytest等）の
代替ではなく、「本番相当のBotプロセス上で、実際の通知文言・Embed・
読み上げ・音声を人間の目と耳で確認する」ための統合テストに近い位置づけ。

【使い方】
    python3 bot.py --test_eew sample_eew.json
    python3 bot.py --test_quake sample_quake.json
    python3 bot.py --test_tsunami sample_tsunami.json
    python3 bot.py --test_volcano sample_volcano.json
    python3 bot.py --test_usgs sample_usgs.json
    python3 bot.py --test_other sample_other.json

対応する引数と、内部で呼び出す Cog / メソッドの対応は TEST_TARGETS を参照。

【通知内容の「テストである」明記】
各 notify_* 関数は is_test=True を渡された場合、既存の実装で
埋め込みタイトルの先頭に「【テスト】」を付与し、フッターに
「※これはテスト通知です。」を表示するようになっている
（本モジュールが新設したものではなく、既存の仕組みをそのまま利用する）。
加えて、テスト実行の開始・終了時にコンソールとログの両方に
「これはテスト実行です」という趣旨のメッセージを明示的に出力する。

【JSONファイルの中身】
各 --test_<cog> が要求するJSON構造は、対応する notify_* 関数が
受け取る `data` 引数とそのまま同じ形式（P2P地震情報APIやWolfx API、
気象庁XMLから変換された辞書のJSON表現）。サンプルは
tests/fixtures/ 以下に配置する運用を想定。
"""
import argparse
import asyncio
import json
import logging
import sys

logger = logging.getLogger("QTLBot")


def parse_test_args(argv: list[str]) -> tuple[str, str] | None:
    """
    コマンドライン引数から --test_<cog> <jsonパス> を検出する。

    通常起動（テスト引数なし）の場合は None を返す。
    見つかった場合は (cog_key, json_path) のタプルを返す。
    """
    parser = argparse.ArgumentParser(add_help=False)
    for cog_key in TEST_TARGETS:
        parser.add_argument(f"--test_{cog_key}", metavar="JSON_PATH", default=None)

    # 未知の引数（Botの他オプション等）があってもエラーにしない
    known_args, _ = parser.parse_known_args(argv)

    for cog_key in TEST_TARGETS:
        json_path = getattr(known_args, f"test_{cog_key}", None)
        if json_path:
            return cog_key, json_path

    return None


def load_test_json(json_path: str):
    """テスト用JSONファイルを読み込む。失敗時は分かりやすいエラーで終了する。"""
    try:
        with open(json_path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[TEST] エラー: JSONファイルが見つかりません: {json_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[TEST] エラー: JSONの構文が不正です: {json_path}\n  {e}")
        sys.exit(1)


# ── テスト対象の定義 ──
# cog_key: {
#   cog_name: Cogクラス名
#   method: 呼び出すメソッド名
#   data_kwarg: dataを渡すキーワード引数名（省略時 "data"）
#   supports_is_test: メソッドが is_test 引数を受け取れるか
#   extra_kwargs: 追加で渡す固定キーワード引数（例: event_id）
# }
# 呼び出し方法: getattr(cog_instance, method_name)(**{data_kwarg: data}, is_test=True, **extra_kwargs)
#
# 【is_test 非対応メソッドについて】
# _notify_volcano / _notify_eruption / _notify_warning / notify_usgs_quake は
# is_test 引数を持たない（既存の実装のまま）。これらのテスト実行時は、
# 代わりに JSON 側に見出しテキストが含まれるフィールド（headTitle 等）へ
# 「【テスト】」を手動で付与しておくか、_inject_test_marker() で
# 実行時に動的に付与する。
TEST_TARGETS = {
    "eew": {
        "cog_name": "EewCog",
        "method": "notify_eew",
    },
    "quake": {
        "cog_name": "QuakeInfoCog",
        "method": "notify_quake",
    },
    "tsunami": {
        "cog_name": "TsunamiCog",
        "method": "notify_tsunami",
    },
    "tsunami_observation": {
        "cog_name": "TsunamiCog",
        "method": "notify_tsunami_observation",
        "data_kwarg": "detail",
    },
    "tsunami_forecast": {
        "cog_name": "TsunamiCog",
        "method": "notify_tsunami_forecast",
        "data_kwarg": "detail",
    },
    "volcano": {
        "cog_name": "VolcanoCog",
        "method": "_notify_volcano",
        "data_kwarg": "detail",
        "supports_is_test": False,
        "extra_kwargs": {"event_id": "TEST-0000000000"},
        "test_marker_field": "headTitle",
    },
    "volcano_eruption": {
        "cog_name": "VolcanoCog",
        "method": "_notify_eruption",
        "data_kwarg": "item",
        "supports_is_test": False,
        "extra_kwargs": {"event_id": "TEST-0000000000"},
        "test_marker_field": "headTitle",
    },
    "volcano_warning": {
        "cog_name": "VolcanoCog",
        "method": "_notify_warning",
        "data_kwarg": "item",
        "supports_is_test": False,
        "extra_kwargs": {"event_id": "TEST-0000000000"},
        "test_marker_field": "headTitle",
    },
    "usgs": {
        "cog_name": "UsgsCog",
        "method": "notify_usgs_quake",
        "data_kwarg": "feature",
        "supports_is_test": False,
        "extra_kwargs": {"extra_note": "【テスト】これはテスト通知です"},
    },
    "other_long_period": {
        "cog_name": "OtherInfoCog",
        "method": "notify_long_period",
        "data_kwarg": "list_item",
    },
    "other_quake_advisory": {
        "cog_name": "OtherInfoCog",
        "method": "notify_quake_advisory",
        "data_kwarg": "list_item",
    },
}


def _inject_test_marker(data, test_marker_field: str | None):
    """
    is_test 引数を持たないメソッド向けに、JSONデータの指定フィールドへ
    「【テスト】」の接頭辞を動的に付与する（元データは書き換えず複製する）。
    """
    if not test_marker_field or not isinstance(data, dict):
        return data
    patched = dict(data)
    if test_marker_field in patched and isinstance(patched[test_marker_field], str):
        if not patched[test_marker_field].startswith("【テスト】"):
            patched[test_marker_field] = "【テスト】" + patched[test_marker_field]
    return patched


async def run_cli_test(bot, cog_key: str, json_path: str) -> None:
    """
    --test_<cog> で指定されたテストを実行する。

    Bot が on_ready 済み（Discordへの接続とCog登録が完了した状態）で
    呼び出すこと。実行後は明示的にプロセスを終了する
    （通常の常駐ループには入らない）。
    """
    target = TEST_TARGETS.get(cog_key)
    if target is None:
        print(f"[TEST] エラー: 不明なテスト対象です: {cog_key}")
        print(f"[TEST] 使用可能なテスト対象: {', '.join(TEST_TARGETS.keys())}")
        await bot.close()
        sys.exit(1)

    banner = "=" * 60
    print(banner)
    print(f"[TEST] これはテスト実行です — 対象: {cog_key} ({target['cog_name']}.{target['method']})")
    print(f"[TEST] 入力ファイル: {json_path}")
    print(banner)
    logger.warning(
        f"★★★ CLIテストモードで実行中 ★★★ "
        f"対象: {cog_key} ({target['cog_name']}.{target['method']}) / 入力: {json_path}"
    )

    data = load_test_json(json_path)

    cog = bot.get_cog(target["cog_name"])
    if cog is None:
        print(f"[TEST] エラー: Cog '{target['cog_name']}' が登録されていません")
        logger.error(f"CLIテスト失敗: Cog '{target['cog_name']}' が見つかりません")
        await bot.close()
        sys.exit(1)

    method = getattr(cog, target["method"], None)
    if method is None:
        print(f"[TEST] エラー: メソッド '{target['method']}' が {target['cog_name']} に存在しません")
        logger.error(f"CLIテスト失敗: メソッド '{target['method']}' が見つかりません")
        await bot.close()
        sys.exit(1)

    data_kwarg = target.get("data_kwarg", "data")
    supports_is_test = target.get("supports_is_test", True)
    extra_kwargs = target.get("extra_kwargs", {})
    test_marker_field = target.get("test_marker_field")

    if not supports_is_test:
        data = _inject_test_marker(data, test_marker_field)
        if test_marker_field:
            print(f"[TEST] 注意: {target['method']} は is_test 引数に対応していないため、"
                  f"フィールド '{test_marker_field}' へ手動でテスト表記を付与しています")
        elif not extra_kwargs:
            print(f"[TEST] 注意: {target['method']} は is_test 引数に対応しておらず、"
                  f"テスト表記を自動付与する設定もありません。通知内容に「テスト」の"
                  f"明記が含まれるかは入力JSON側の内容に依存します")

    kwargs = {data_kwarg: data, **extra_kwargs}
    if supports_is_test:
        kwargs["is_test"] = True

    try:
        await method(**kwargs)
        print(banner)
        print(f"[TEST] 完了しました。Discordの該当チャンネルで通知内容を確認してください。")
        print(f"[TEST] （タイトル先頭の「【テスト】」表記・フッターの「※これはテスト通知です。」で識別できます）")
        print(banner)
        logger.warning(f"★★★ CLIテスト完了 ★★★ 対象: {cog_key}")
    except Exception as e:
        print(banner)
        print(f"[TEST] 実行中にエラーが発生しました: {type(e).__name__}: {e}")
        print(banner)
        logger.error(f"CLIテスト実行エラー: {e}", exc_info=True)
    finally:
        # 音声キュー等の非同期処理が積まれている場合に備え、少し待ってから終了する
        await asyncio.sleep(3)
        await bot.close()
