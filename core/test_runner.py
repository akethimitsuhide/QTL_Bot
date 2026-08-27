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

【--test_all による一括実行】
    python3 bot.py --test_all tests/fixtures/

TEST_TARGETS 全対象を一括で実行する。個別に --test_<cog> <json> を
指定する代わりに fixture を集めたディレクトリを1つ指定すると、
"<fixtures_dir>/<cog_key>_sample.json" という命名規則のファイルを
cog_key ごとに探索し、存在するものだけ順番に実行する
（例: tests/fixtures/eew_sample.json, tests/fixtures/quake_sample.json）。
JSON を必要としない対象（ews）はそのまま実行される。
fixture が存在しない対象はスキップされ、最後に OK / NG / SKIP の
サマリーが表示される。

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
import os
import sys

logger = logging.getLogger("QTLBot")

# CLIテストモードで起動しているかどうかを、他モジュール（cogs/system.py の
# Web Dashboard起動判定など）から参照できるようにするフラグ。
# bot.py のモジュールロード時に parse_test_args() の結果に応じてセットされる。
CLI_TEST_MODE = False


def parse_test_args(argv: list[str]) -> tuple[str, str] | None:
    """
    コマンドライン引数から --test_<cog> <jsonパス> を検出する。

    通常起動（テスト引数なし）の場合は None を返す。
    見つかった場合は (cog_key, json_path) のタプルを返す。

    TEST_TARGETS[cog_key].get("requires_json", True) が False の対象
    （例: ews）は、JSONファイルを必要としないテストのため、
    `--test_ews` のように値を省略しても検出できるよう
    nargs="?" を指定する（省略時は json_path として空文字列を返す）。

    【--test_all <fixtures_dir>】
    TEST_TARGETS の全対象を一括実行する特殊モード。個別の --test_<cog> と
    異なり、cog_key ごとに JSON パスを指定する必要はなく、fixture を
    まとめて置いたディレクトリを1つだけ指定する。
    検出時は特別な cog_key "__all__" を使い、
    (「__all__」, fixtures_dir) を返す（json_path 部分には
    ディレクトリパスが入る点に注意）。
    実際にどの対象を実行するか（<cog_key>_sample.json の探索・
    存在確認）は run_all_cli_tests() 側で行う。
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--test_all", metavar="FIXTURES_DIR", default=None)
    for cog_key, target in TEST_TARGETS.items():
        if target.get("requires_json", True):
            parser.add_argument(f"--test_{cog_key}", metavar="JSON_PATH", default=None)
        else:
            parser.add_argument(
                f"--test_{cog_key}", metavar="JSON_PATH", nargs="?",
                const="", default=None,
            )

    # 未知の引数（Botの他オプション等）があってもエラーにしない
    known_args, _ = parser.parse_known_args(argv)

    global CLI_TEST_MODE

    # --test_all が指定されていれば最優先で検出する
    # （個別の --test_<cog> と併用された場合も一括実行を優先）
    if known_args.test_all is not None:
        CLI_TEST_MODE = True
        return "__all__", known_args.test_all

    for cog_key, target in TEST_TARGETS.items():
        json_path = getattr(known_args, f"test_{cog_key}", None)
        requires_json = target.get("requires_json", True)
        # requires_json=True: json_path が非空文字列なら検出（従来通り）
        # requires_json=False: json_path が None でなければ検出
        #   （空文字列 "" も「フラグだけ指定された」という有効な検出結果）
        detected = (json_path is not None) if not requires_json else bool(json_path)
        if detected:
            CLI_TEST_MODE = True
            return cog_key, json_path or ""

    return None


def load_test_json(json_path: str, exit_on_error: bool = True):
    """
    テスト用JSONファイルを読み込む。

    exit_on_error=True（デフォルト、単体実行 --test_<cog> 用）:
        失敗時は分かりやすいエラーメッセージを表示してプロセスを終了する
        （従来通りの挙動）。
    exit_on_error=False（--test_all の一括実行用）:
        プロセスを終了させず、例外をそのまま送出する。
        --test_all 実行中に1つのfixtureが壊れていても他の対象の実行を
        止めないよう、呼び出し元（run_all_cli_tests）で対象単位に
        キャッチしてスキップ扱いにするため。
    """
    try:
        with open(json_path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[TEST] エラー: JSONファイルが見つかりません: {json_path}")
        if exit_on_error:
            sys.exit(1)
        raise
    except json.JSONDecodeError as e:
        print(f"[TEST] エラー: JSONの構文が不正です: {json_path}\n  {e}")
        if exit_on_error:
            sys.exit(1)
        raise


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
        "extra_kwargs": {"start_monitor": False},
        "expected_fields": ["EventID", "Hypocenter", "MaxIntensity"],
    },
    "quake": {
        "cog_name": "QuakeInfoCog",
        "method": "notify_quake",
        "expected_fields": ["Hypocenter", "MaxIntensity"],
    },
    "tsunami": {
        "cog_name": "TsunamiCog",
        "method": "notify_tsunami",
        "expected_fields": ["areas", "issue"],
    },
    "tsunami_observation": {
        "cog_name": "TsunamiCog",
        "method": "notify_tsunami_observation",
        "data_kwarg": "detail",
        "expected_fields": ["areas"],
    },
    "tsunami_forecast": {
        "cog_name": "TsunamiCog",
        "method": "notify_tsunami_forecast",
        "data_kwarg": "detail",
        "expected_fields": ["areas"],
    },
    "volcano": {
        "cog_name": "VolcanoCog",
        "method": "_notify_volcano",
        "data_kwarg": "detail",
        "supports_is_test": False,
        "extra_kwargs": {"event_id": "TEST-0000000000"},
        "test_marker_field": "headTitle",
        "expected_fields": ["headTitle"],
    },
    "volcano_eruption": {
        "cog_name": "VolcanoCog",
        "method": "_notify_eruption",
        "data_kwarg": "item",
        "supports_is_test": False,
        "extra_kwargs": {"event_id": "TEST-0000000000"},
        "test_marker_field": "headTitle",
        "expected_fields": ["headTitle"],
    },
    "volcano_warning": {
        "cog_name": "VolcanoCog",
        "method": "_notify_warning",
        "data_kwarg": "item",
        "supports_is_test": False,
        "extra_kwargs": {"event_id": "TEST-0000000000"},
        "test_marker_field": "headTitle",
        "expected_fields": ["headTitle"],
    },
    "usgs": {
        "cog_name": "UsgsCog",
        "method": "notify_usgs_quake",
        "data_kwarg": "feature",
        "supports_is_test": False,
        "extra_kwargs": {"extra_note": "【テスト】これはテスト通知です"},
        "expected_fields": ["properties", "geometry"],
    },
    "other_long_period": {
        "cog_name": "OtherInfoCog",
        "method": "notify_long_period",
        "data_kwarg": "list_item",
        "expected_fields": [],
    },
    "other_quake_advisory": {
        "cog_name": "OtherInfoCog",
        "method": "notify_quake_advisory",
        "data_kwarg": "list_item",
        "expected_fields": [],
    },
    "ews": {
        "cog_name": "QuakeInfoCog",
        "method": "_play_ews_signal",
        # EWS信号音の生成・再生自体は地震データ(JSON)を必要としない
        # （domesticTsunami の値だけをトリガーとして使う）ため、
        # 他のテスト対象と異なりJSONファイルの指定を必須としない。
        "requires_json": False,
        # --test_ews に続けて "Warning" または "MajorWarning" を指定
        # できる（例: --test_ews MajorWarning）。省略時は MajorWarning
        # （最も緊急性の高い信号）を使う。
        "cli_arg_builder": lambda arg: {
            "dom_tsunami": arg if arg in ("Warning", "MajorWarning") else "MajorWarning"
        },
        "expected_fields": [],
    },
}


def validate_expected_fields(cog_key: str, data: dict) -> None:
    """
    指定した cog_key の TEST_TARGETS エントリに定義された expected_fields が
    data 内に存在するか検証する。欠けているフィールドがあれば、コマンド指定
    ミス（例: --test_eew のつもりが quake 用 JSON を渡してしまった等）の
    可能性が高いため、警告のみ出して処理は継続する（テスト用途のため中断は
    しない）。
    """
    target = TEST_TARGETS.get(cog_key)
    if not target:
        return
    expected_fields = target.get("expected_fields", [])
    if not expected_fields:
        return
    missing = [f for f in expected_fields if f not in data]
    if missing:
        print(
            f"[TEST] 警告: --test_{cog_key} に指定したJSONに想定フィールドが"
            f"見つかりません: {missing}"
        )
        print(
            f"[TEST]       (対象JSONファイルを取り違えている可能性があります。"
            f" {cog_key} が期待するフィールド: {expected_fields})"
        )
        logger.warning(
            f"CLIテスト: --test_{cog_key} の入力JSONに想定フィールド不足 "
            f"missing={missing} expected={expected_fields}"
        )


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


def _resolve_cog_and_method(bot, cog_key: str, target: dict):
    """
    テスト対象の Cog インスタンスとメソッドを解決する。

    見つからない場合は (None, None) を返し、エラーメッセージの出力と
    ログ記録のみ行う（プロセス終了・bot.close() は呼び出し元の責務とする。
    run_cli_test は単体実行なので即終了、run_all_cli_tests は一括実行の
    途中なので当該対象をスキップして次へ進む、という違いがあるため）。
    """
    cog = bot.get_cog(target["cog_name"])
    if cog is None:
        print(f"[TEST] エラー: Cog '{target['cog_name']}' が登録されていません")
        logger.error(f"CLIテスト失敗（{cog_key}）: Cog '{target['cog_name']}' が見つかりません")
        return None, None

    method = getattr(cog, target["method"], None)
    if method is None:
        print(f"[TEST] エラー: メソッド '{target['method']}' が {target['cog_name']} に存在しません")
        logger.error(f"CLIテスト失敗（{cog_key}）: メソッド '{target['method']}' が見つかりません")
        return None, None

    return cog, method


async def _invoke_test_target(cog_key: str, target: dict, cog, method, json_path: str) -> bool:
    """
    Cog・メソッドの解決が完了しているテスト対象を実際に1回呼び出す。

    戻り値は成功なら True、失敗（JSON読み込みエラー・実行時例外含む）
    なら False。例外はここで捕捉して print / logger.error するのみで
    再送出しない（--test_all の一括実行が1件の失敗で止まらないように
    するため。run_cli_test / run_all_cli_tests のどちらからも呼べる
    共通処理として設計している）。
    """
    requires_json = target.get("requires_json", True)

    if not requires_json:
        # 【EWS等、JSONファイルを必要としないテスト対象】
        # cli_arg_builder が定義されていればそれを使って呼び出し引数を
        # 組み立てる（現状は ews のみ、domesticTsunami の値を組み立てる）。
        try:
            arg_builder = target.get("cli_arg_builder")
            kwargs = arg_builder(json_path) if arg_builder is not None else {}
            await method(**kwargs)
            print(f"[TEST] 完了しました（{cog_key}）。実際の音声・信号音が再生されたか確認してください。")
            logger.warning(f"★★★ CLIテスト完了 ★★★ 対象: {cog_key}")
            return True
        except Exception as e:
            print(f"[TEST] 実行中にエラーが発生しました（{cog_key}）: {type(e).__name__}: {e}")
            logger.error(f"CLIテスト実行エラー（{cog_key}）: {e}", exc_info=True)
            return False

    try:
        # --test_all からの呼び出し（exit_on_error=False）でも単体実行
        # からの呼び出しでも、ここでは常に例外を送出させて捕捉する
        # （単体実行時の sys.exit(1) は run_cli_test 側で別途保証する）。
        data = load_test_json(json_path, exit_on_error=False)
    except (FileNotFoundError, json.JSONDecodeError):
        return False

    validate_expected_fields(cog_key, data)

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
        print(f"[TEST] 完了しました（{cog_key}）。Discordの該当チャンネルで通知内容を確認してください。")
        print("[TEST]   （タイトル先頭の「【テスト】」表記・フッターの「※これはテスト通知です。」で識別できます）")
        logger.warning(f"★★★ CLIテスト完了 ★★★ 対象: {cog_key}")
        return True
    except Exception as e:
        print(f"[TEST] 実行中にエラーが発生しました（{cog_key}）: {type(e).__name__}: {e}")
        logger.error(f"CLIテスト実行エラー（{cog_key}）: {e}", exc_info=True)
        return False


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

    requires_json = target.get("requires_json", True)

    banner = "=" * 60
    print(banner)
    print(f"[TEST] これはテスト実行です — 対象: {cog_key} ({target['cog_name']}.{target['method']})")
    if requires_json:
        print(f"[TEST] 入力ファイル: {json_path}")
    print(banner)
    logger.warning(
        f"★★★ CLIテストモードで実行中 ★★★ "
        f"対象: {cog_key} ({target['cog_name']}.{target['method']})"
        + (f" / 入力: {json_path}" if requires_json else "")
    )

    cog, method = _resolve_cog_and_method(bot, cog_key, target)
    if cog is None or method is None:
        await bot.close()
        sys.exit(1)

    if requires_json and not os.path.exists(json_path):
        # 単体実行時は従来通り即エラー終了させる
        # （load_test_json(exit_on_error=True) と同じ挙動をここで先に保証する）
        print(f"[TEST] エラー: JSONファイルが見つかりません: {json_path}")
        await bot.close()
        sys.exit(1)

    try:
        await _invoke_test_target(cog_key, target, cog, method, json_path)
    finally:
        print(banner)
        # 音声キュー等の非同期処理が積まれている場合に備え、少し待ってから終了する
        await asyncio.sleep(3)
        await bot.close()


async def run_all_cli_tests(bot, fixtures_dir: str) -> None:
    """
    --test_all <fixtures_dir> で指定された一括テストを実行する。

    TEST_TARGETS の全対象について、requires_json=True の対象は
    "<fixtures_dir>/<cog_key>_sample.json" を探索し、存在するものだけ
    実行する（存在しない対象はスキップし、一覧にまとめて表示する）。
    requires_json=False の対象（ews）は JSON 不要のためそのまま実行する。

    Bot が on_ready 済みで呼び出すこと。全対象の実行が終わったら
    サマリーを表示し、明示的にプロセスを終了する。
    """
    banner = "=" * 60
    print(banner)
    print(f"[TEST] これは一括テスト実行です（--test_all） — fixtures: {fixtures_dir}")
    print(banner)
    logger.warning(f"★★★ CLIテストモード（一括実行）で実行中 ★★★ fixtures={fixtures_dir}")

    if not os.path.isdir(fixtures_dir):
        print(f"[TEST] エラー: fixtures ディレクトリが見つかりません: {fixtures_dir}")
        logger.error(f"CLIテスト（一括）失敗: fixtures ディレクトリが見つかりません: {fixtures_dir}")
        await bot.close()
        sys.exit(1)

    results: dict[str, str] = {}  # cog_key -> "OK" / "NG" / "SKIP"

    for cog_key, target in TEST_TARGETS.items():
        requires_json = target.get("requires_json", True)

        if requires_json:
            json_path = os.path.join(fixtures_dir, f"{cog_key}_sample.json")
            if not os.path.exists(json_path):
                print(f"[TEST] スキップ（{cog_key}）: fixture が見つかりません → {json_path}")
                logger.info(f"CLIテスト（一括）スキップ: {cog_key} ({json_path} が存在しません)")
                results[cog_key] = "SKIP"
                continue
        else:
            json_path = ""

        print(banner)
        print(f"[TEST] 実行中 — 対象: {cog_key} ({target['cog_name']}.{target['method']})")
        if requires_json:
            print(f"[TEST] 入力ファイル: {json_path}")

        cog, method = _resolve_cog_and_method(bot, cog_key, target)
        if cog is None or method is None:
            results[cog_key] = "NG"
            continue

        ok = await _invoke_test_target(cog_key, target, cog, method, json_path)
        results[cog_key] = "OK" if ok else "NG"

        # 各対象の間に少し間隔を空ける
        # （音声キュー・Discordレート制限・通知の見分けやすさへの配慮）
        await asyncio.sleep(2)

    print(banner)
    print("[TEST] 一括テスト完了 — 結果サマリー")
    for cog_key in TEST_TARGETS:
        status = results.get(cog_key, "SKIP")
        icon = {"OK": "✅", "NG": "❌", "SKIP": "⚪"}.get(status, "?")
        print(f"[TEST]   {icon} {status:<4} {cog_key}")
    ok_count = sum(1 for s in results.values() if s == "OK")
    ng_count = sum(1 for s in results.values() if s == "NG")
    skip_count = sum(1 for s in results.values() if s == "SKIP")
    print(f"[TEST] 合計: {len(TEST_TARGETS)} 件 / OK={ok_count} NG={ng_count} SKIP={skip_count}")
    print(banner)
    logger.warning(
        f"★★★ CLIテスト（一括）完了 ★★★ "
        f"OK={ok_count} NG={ng_count} SKIP={skip_count} / {results}"
    )

    await asyncio.sleep(3)
    await bot.close()
