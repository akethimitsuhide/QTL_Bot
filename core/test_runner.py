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
    python3 bot.py --test_auto sample_unknown.json  # 形式を自動判定して実行（2026-08-30追加）

対応する引数と、内部で呼び出す Cog / メソッドの対応は TEST_TARGETS を参照。

【--test_auto による自動判定実行】
    python3 bot.py --test_auto path/to/downloaded.json

情報ソースによってJSONの形式が細かく異なり、どの --test_<cog> を
使えばよいか一見して分かりにくい（特に気象庁XML由来のControl/Head/
Body形式は、津波観測情報・津波予報・南海トラフ情報等で外側の形を
共有している）。実際に、津波警報・注意報・予報（VTSE41）形式の
JSONを --test_tsunami（P2P地震情報API形式用）に渡してしまい、
正しく読み込めなかった事例があったため追加した。
sniff_test_target() がJSONの構造（Body配下の具体的なキー等）から
候補を判定し、1件に絞れた場合のみ自動実行する。0件（未知の形式）
または2件以上（区別不能）の場合は自動実行せず、その旨を表示して
ユーザーに判断を委ねる。

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

from core.eew_convert import convert_p2p_eew_to_wolfx

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

    【--test_auto <json_path>】
    【2026-08-30 追加】どの --test_<cog> を使えばよいか判断が難しい
    JSONファイル（気象庁から実際にダウンロードしたXML変換JSON等）を
    渡すと、中身の構造（フィンガープリント）から対象を自動判定して
    実行する。実際に「津波警報・注意報・予報（VTSE41）形式のJSONを
    --test_tsunami に渡してしまい、正しく読み込めなかった」という
    事例があったための追加（sniff_test_target 参照）。
    検出時は特別な cog_key "__auto__" を使う。
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--test_all", metavar="FIXTURES_DIR", default=None)
    parser.add_argument("--test_auto", metavar="JSON_PATH", default=None)
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

    if known_args.test_auto is not None:
        CLI_TEST_MODE = True
        return "__auto__", known_args.test_auto

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
def _convert_raw_p2p_eew_for_test(data: dict) -> dict:
    """
    【2026-08-30 追加】--test_eew_p2p 用のデータ変換フック。

    経緯: P2P地震情報の緊急地震速報（生の code=556 形式。トップレベルに
    "issue"（"eventId"・"serial"を持つ）・"earthquake"・"areas" を持つ）を
    実機でダウンロードした過去データ（例: KUMAMOTO_7_2.json）で
    --test_eew を実行したところ、フィールドが一致せず「不明の緊急地震
    速報」という結果になってしまう事例が発生した。

    本番のWebSocketハンドラ（cogs/eew.py の P2P EEW 受信処理）は、
    受信した生データを core.eew_convert.convert_p2p_eew_to_wolfx() で
    Wolfx形式（notify_eewが直接期待するフラットな "EventID"/
    "MaxIntensity" 等の形式）へ変換してから notify_eew() に渡している。
    --test_eew はこの変換ステップを経ないまま生データを直接
    notify_eew() に渡していたため、正しく処理できなかった。

    本関数は、TEST_TARGETS["eew_p2p"] の "data_converter" として
    _invoke_test_target() から呼び出され、本番と全く同じ変換関数を
    使って生データを変換してから notify_eew() へ渡すことで、この
    ギャップを解消する。

    【今後の拡張について】
    今後、別の情報ソースで「本番では生データ→変換してから通知関数に
    渡している」ケースが見つかった場合も、同様の変換ラッパー関数を
    作って対応する TEST_TARGETS エントリの "data_converter" に指定
    すればよい（汎用的な拡張ポイントとして設計している。
    _invoke_test_target 参照）。

    変換に失敗した場合（convert_p2p_eew_to_wolfx が None を返す、＝
    想定した構造ではない）は ValueError を送出し、_invoke_test_target
    側で捕捉して分かりやすいエラーメッセージを表示する
    （変換失敗を握りつぶして「不明」な結果を出さないようにするため）。
    """
    converted = convert_p2p_eew_to_wolfx(data)
    if converted is None:
        raise ValueError(
            "convert_p2p_eew_to_wolfx() が None を返しました。"
            "入力JSONがP2P地震情報の緊急地震速報（code=556）の生形式と"
            "一致しない可能性があります（issue.eventId / earthquake / "
            "areas 等のフィールドを確認してください）"
        )
    return converted


TEST_TARGETS = {
    "eew": {
        "cog_name": "EewCog",
        "method": "notify_eew",
        "extra_kwargs": {"start_monitor": False},
        "expected_fields": ["EventID", "Hypocenter", "MaxIntensity"],
    },
    "eew_p2p": {
        # 【2026-08-30 追加】P2P地震情報の緊急地震速報（生の code=556
        # 形式）専用のテスト対象。"eew" が期待するWolfx形式（フラットな
        # EventID/MaxIntensity）とは異なり、こちらはトップレベルに
        # "issue"（eventId/serial）・"earthquake"・"areas" を持つ生の
        # P2P形式をそのまま受け取り、本番と同じ
        # core.eew_convert.convert_p2p_eew_to_wolfx() で変換してから
        # notify_eew() を呼び出す（_convert_raw_p2p_eew_for_test 参照）。
        # source="p2p_eew" を指定し、本番のP2P EEW受信時と同じ
        # チャンネル選択（p2p_eew_channel）で通知させる。
        "cog_name": "EewCog",
        "method": "notify_eew",
        "data_converter": _convert_raw_p2p_eew_for_test,
        "extra_kwargs": {"start_monitor": False, "source": "p2p_eew"},
        # expected_fields は変換前（生データ）の構造で検証する
        "expected_fields": ["issue", "earthquake", "areas"],
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
        # 【2026-08-30 修正】以前は ["areas"] だったが、これは
        # notify_tsunami（P2P地震情報APIのcode=552形式）が使う
        # トップレベルキーであり、notify_tsunami_observation が実際に
        # 受け取る気象庁XML由来のControl/Head/Body形式には存在しない。
        # コピペミスと見られ、正しい入力JSONを渡してもこの検証のせいで
        # 「フィールド不足」という誤った警告が出ていた
        # （sniff_test_target のフィンガープリントも参照）。
        "expected_fields": ["Control", "Head", "Body"],
    },
    "tsunami_forecast": {
        "cog_name": "TsunamiCog",
        "method": "notify_tsunami_forecast",
        "data_kwarg": "detail",
        # 【2026-08-30 修正】tsunami_observation と同じ理由で ["areas"] は
        # 誤り。実際にはControl/Head/Body形式（例: VTSE41 津波警報・
        # 注意報・予報）を受け取る。
        "expected_fields": ["Control", "Head", "Body"],
    },
    "jishin_kanchi": {
        # 【2026-08-30 追加】P2P地震感知情報（code=9611）専用のCLIテスト
        # 対象が存在しなかったための追加。cogs/jishin_kanchi.py 側の
        # notify_jishin_kanchi は is_test=True 対応済みだが、これまで
        # --test_jishin_kanchi で個別に動作確認する手段が無かった。
        "cog_name": "JishinKanchiCog",
        "method": "notify_jishin_kanchi",
        "expected_fields": ["confidence", "started_at"],
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
        # list_item形式（quake/data/list.jsonの1エントリ。"json"キーで
        # 詳細JSONのファイル名を指すだけの小さい構造）専用。
        # 本番のポーリングループが受け取る形と同じ。
        "cog_name": "OtherInfoCog",
        "method": "notify_quake_advisory",
        "data_kwarg": "list_item",
        "expected_fields": [],
    },
    "other_quake_advisory_detail": {
        # 【2026-08-31 追加】気象庁HPから直接ダウンロードした完全な
        # 詳細JSON（Control/Head/Body形式）専用。list_item形式とは
        # 異なり "json" キー（ファイル名）を持たないため、
        # notify_quake_advisory の detail_data 引数に直接渡す
        # 必要がある（list_item に渡すと "json" キーが見つからず
        # 即座にreturnしてしまい、何も通知されない）。
        "cog_name": "OtherInfoCog",
        "method": "notify_quake_advisory",
        "data_kwarg": "detail_data",
        "expected_fields": ["Control", "Head", "Body"],
    },
    "nankai_trough": {
        # 【2026-08-30 追加、2026-09-01 移設】南海トラフ地震臨時情報専用の
        # CLIテスト対象。notify_nankai_trough は cogs/tsunami.py から
        # cogs/other.py（OtherInfoCog）へ移設した（tsunami API経由、
        # Body.EarthquakeInfo を含むControl/Head/Body形式であることは
        # 変わらない）。
        "cog_name": "OtherInfoCog",
        "method": "notify_nankai_trough",
        "data_kwarg": "detail",
        "expected_fields": ["Control", "Head", "Body"],
    },
    "hypocenter_update": {
        # 【2026-08-30 追加、2026-09-01 移設】「顕著な地震の震源要素更新の
        # お知らせ」専用のCLIテスト対象。notify_hypocenter_update は
        # cogs/tsunami.py から cogs/other.py（OtherInfoCog）へ移設した。
        "cog_name": "OtherInfoCog",
        "method": "notify_hypocenter_update",
        "data_kwarg": "detail",
        "expected_fields": ["Control", "Head", "Body"],
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


def sniff_test_target(data) -> list[str]:
    """
    【2026-08-30 追加】JSONデータの構造的特徴（フィンガープリント）から、
    どの --test_<cog_key> に対応する形式かを推定する。

    背景: 気象庁からダウンロードした実際のXML変換JSON（例: 津波警報・
    注意報・予報＝VTSE41形式）を、形式の似ている別の --test_<cog>
    （例: P2P地震情報APIのcode=552形式を期待する --test_tsunami）に
    誤って渡してしまい、正しく読み込めない事例が発生した。
    validate_expected_fields によるトップレベルキーの警告だけでは、
    そもそも「どの --test_<cog> を使うべきか」までは教えてくれない
    ため、逆に「このJSONはどの形式か」を判定する本関数を新設した。

    注意点: 気象庁XML由来のControl/Head/Body形式は、津波観測情報・
    津波予報・南海トラフ情報・震源要素更新のお知らせ等、複数の
    --test_<cog> で共有されている外側の形であり、トップレベルの
    キーの有無だけでは区別できない。そのため Body 以下の具体的な
    キー（Body.Tsunami.Observation の有無等）まで見て判定する。
    各ルールの根拠は対応する notify_* 関数の実際のフィールド
    アクセス箇所（cogs/tsunami.py, cogs/quake.py, cogs/volcano.py 等）
    から書き起こしたものであり、推測ではない。

    戻り値: マッチした cog_key のリスト。空リストなら該当なし
    （未知の形式、または今後 --test_ 対象が追加された形式）。
    複数該当する場合は、区別する決め手がない、または本当に複数の
    条件に当てはまる可能性があることを意味する
    （呼び出し元の run_auto_cli_test 側で、1件に絞れない場合は
    自動実行せずユーザーに選択を委ねる）。
    """
    if not isinstance(data, dict):
        return []

    matches: list[str] = []

    # ── EEW（Wolfxの共通フラット形式。P2P地震情報WebSocketから受信した
    #    生データをconvert_p2p_eew_to_wolfxで変換した後の形式でもある）──
    # cogs/eew.py: data.get("EventID"), data.get("MaxIntensity") 等、
    # トップレベルの大文字キーで直接アクセスする独自形式。
    if "EventID" in data and "MaxIntensity" in data:
        matches.append("eew")

    # ── issue オブジェクトのネスト構造まで見て、EEW（P2P生形式）・
    #    地震情報・津波を区別する ──
    # 【2026-08-30 追加の経緯】以前はトップレベルの "issue"+"earthquake"
    # の有無だけで「地震情報（quake）」と判定していたが、P2P地震情報の
    # 緊急地震速報（生の code=556 形式）もトップレベルに "issue"・
    # "earthquake"・"areas" を持つため、実際には地震情報（code=551）と
    # 区別がつかず、EEWの生データを --test_auto に渡すと誤って
    # 「地震情報」と判定してしまう不具合があった（実機ログで確認）。
    #
    # 各コードの "issue" オブジェクトの中身は、実際にそのコードを
    # パースする側のコードが読んでいるキーで区別できる:
    #   - P2P生EEW（code=556）: core/eew_convert.py が
    #     issue.get("eventId"), issue.get("serial") を読む
    #   - 地震情報（code=551）: cogs/quake.py notify_quake が
    #     issue.get("type") を読み、data.get("points") も参照する
    #   - 津波（code=552）: cogs/tsunami.py notify_tsunami が
    #     issue.get("time") を読み、hypocenter/points的な地震固有
    #     フィールドは持たない
    issue = data.get("issue")
    issue = issue if isinstance(issue, dict) else None
    has_earthquake = isinstance(data.get("earthquake"), dict)

    if issue is not None and "eventId" in issue and (
        data.get("cancelled") is not None or (has_earthquake and "areas" in data)
    ):
        # P2P生EEW（code=556）。cancelled（キャンセル報）は
        # earthquake/areasを持たない場合があるためcancelledの有無も
        # 判定条件に含める。
        matches.append("eew_p2p")
    elif issue is not None and "type" in issue and has_earthquake and "points" in data:
        # 地震情報（P2P地震情報APIのcode=551形式）
        matches.append("quake")
    elif issue is not None and "areas" in data and not has_earthquake:
        # 津波（P2P地震情報APIのcode=552形式）。
        # quake/eew_p2pと違い、地震固有のearthquakeオブジェクトを持たない。
        matches.append("tsunami")

    # ── 地震感知情報（P2P地震情報APIのcode=9611形式）──
    # cogs/jishin_kanchi.py notify_jishin_kanchi:
    # data.get("confidence"), data.get("started_at"), data.get("area_confidences")
    if "confidence" in data and "started_at" in data:
        matches.append("jishin_kanchi")

    # ── USGS（GeoJSON Feature形式）──
    # cogs/usgs.py notify_usgs_quake: feature.get("properties"), feature.get("geometry")
    if "properties" in data and "geometry" in data:
        matches.append("usgs")

    # ── 火山情報系（フラットなcamelCase形式。気象庁XMLをVolcanoCog内で
    #    事前に変換したもので、Control/Head/Bodyそのものではない）──
    # volcano: cogs/volcano.py _notify_volcano
    #   headTitle, volcanoActivity, volcanoPrevention 等
    if "headTitle" in data and ("volcanoActivity" in data or "volcanoPrevention" in data):
        matches.append("volcano")
    # volcano_eruption: cogs/volcano.py _notify_eruption（噴火速報。
    #   headTitleは持つがvolcanoActivity等は持たない）
    elif "headTitle" in data and "infoType" in data:
        matches.append("volcano_eruption")
    # volcano_warning: cogs/volcano.py _notify_warning（headTitleを
    #   持たず、代わりにvolcanoInfosを持つ）
    if "volcanoInfos" in data and "headTitle" not in data:
        matches.append("volcano_warning")

    # ── 気象庁XML由来のControl/Head/Body形式（複数の --test_<cog> で
    #    外側の形を共有するため、Body配下の具体的なキーまで見て区別する）──
    body = data.get("Body")
    if isinstance(data.get("Control"), dict) and isinstance(data.get("Head"), dict) and isinstance(body, dict):
        tsunami_body = body.get("Tsunami")
        tsunami_body = tsunami_body if isinstance(tsunami_body, dict) else {}

        if isinstance(tsunami_body.get("Observation"), dict):
            # tsunami_observation: cogs/tsunami.py notify_tsunami_observation
            # （Forecastを併せ持つ場合もあるが、Observationがあれば
            #   優先的にこちらと判定する。実際のVTSE51/52がこの形）
            matches.append("tsunami_observation")
        elif isinstance(tsunami_body.get("Forecast"), (dict, list)):
            # tsunami_forecast: cogs/tsunami.py notify_tsunami_forecast
            # （Observationを持たずForecastのみ＝VTSE41系）
            matches.append("tsunami_forecast")

        # 【2026-08-31 修正】"Body.EarthquakeInfo" 及び "Body.Earthquake"
        # は、複数の情報種別が同じキーを共有しており、キーの有無だけでは
        # 区別できないことが実機ログで判明した。
        #
        # 具体的には、気象庁は「南海トラフ地震臨時情報」「顕著な地震の
        # 震源要素更新のお知らせ」を tsunami/data/list.json と
        # quake/data/list.json の2つのエンドポイントに重複して掲載して
        # おり（cogs/other.py 冒頭のdocstring「Step6時点の設計メモ」
        # 参照）、どちらの経路のJSONも Body の形が同一になる:
        #   - tsunami/list.json経由 → cogs/tsunami.py の
        #     notify_nankai_trough / notify_hypocenter_update が処理
        #     （通知先: tsunami_channel）
        #   - quake/list.json経由   → cogs/other.py の
        #     notify_quake_advisory が処理（通知先: other_channel）
        # さらに「北海道・三陸沖後発地震注意情報」も同じ
        # Body.EarthquakeInfo 形式を使うが、こちらは quake/list.json
        # 経由でのみ配信され、cogs/other.py にしかハンドラが無い
        # （cogs/tsunami.py には対応するメソッドが存在しない）。
        #
        # 以前は「EarthquakeInfoがあれば無条件にnankai_troughと判定」
        # していたため、「北海道・三陸沖後発地震注意情報」のJSONを
        # --test_auto に渡すと誤って nankai_trough（TsunamiCog、
        # tsunami_channel）と判定され、本来 other_channel に届くはずの
        # 通知が津波情報チャンネルに送られてしまう不具合があった。
        #
        # そのため、実際の本番ディスパッチと同じ基準（Head.Title の
        # テキスト内容）でさらに絞り込む。「南海トラフ」「顕著な地震の
        # 震源要素更新」は上記の通り本番でも両方の経路が独立して動作
        # しうる（意図された重複）ため、どちらか一方に決め打ちせず
        # 両方を候補として提示し、ユーザーに選んでもらう
        # （誤った対象で実行してしまうことを避けるため）。
        head_title = ""
        control_title = ""
        if isinstance(data.get("Head"), dict):
            head_title = str(data["Head"].get("Title", ""))
        if isinstance(data.get("Control"), dict):
            control_title = str(data["Control"].get("Title", ""))
        title_text = head_title or control_title

        if isinstance(body.get("EarthquakeInfo"), dict):
            if "北海道" in title_text or "三陸沖" in title_text or "後発地震" in title_text:
                # cogs/tsunami.py にハンドラが無い、
                # other_quake_advisory_detail（OtherInfoCog,
                # other_channel）専用の情報種別
                matches.append("other_quake_advisory_detail")
            elif "南海トラフ" in title_text:
                # 意図された重複配信。どちらのチャンネルで確認したいか
                # ユーザーに委ねる。
                matches.append("nankai_trough")
                matches.append("other_quake_advisory_detail")
            else:
                # タイトルから判別できない場合は、決め打ちせず両方
                # 候補として提示する（誤判定でtsunami_channelに
                # 送ってしまうことを避けるため、nankai_troughを単独で
                # 確定させない）。
                matches.append("nankai_trough")
                matches.append("other_quake_advisory_detail")
        elif isinstance(body.get("Earthquake"), (dict, list)) and not tsunami_body:
            if "顕著な地震の震源要素更新" in title_text:
                # 意図された重複配信（上記と同様の理由で両方提示）
                matches.append("hypocenter_update")
                matches.append("other_quake_advisory_detail")
            else:
                # hypocenter_update: cogs/other.py notify_hypocenter_update
                # （2026-09-01移設。Tsunamiを持たずEarthquakeのみ＝
                #   震源要素更新のお知らせ。タイトルが取得できない/
                #   一致しない場合のフォールバック）
                matches.append("hypocenter_update")

    # ── JMA list.json の1エントリ形式（"json"キーで詳細JSONのファイル名を
    #    指すだけの、上記のどの形式よりも小さい構造）──
    # other_long_period / other_quake_advisory はどちらもこの形式を
    # 共有しており、内容（ttlフィールドのテキスト）でしか区別できない。
    # ttlが無い、または判定できない場合は両方を候補として返す
    # （呼び出し元で自動実行せず、ユーザーに選択を委ねる）。
    if "json" in data and not matches:
        ttl = str(data.get("ttl", ""))
        if "長周期地震動" in ttl:
            matches.append("other_long_period")
        elif any(kw in ttl for kw in ("後発地震注意情報", "南海トラフ", "震源要素更新")):
            matches.append("other_quake_advisory")
        else:
            # ttlで判別できない場合は両方候補として提示する
            matches.append("other_long_period")
            matches.append("other_quake_advisory")

    return matches


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

    # 【2026-08-30 追加】data_converter: 本番コードでは生データを
    # そのまま notify_* に渡すのではなく、事前に変換関数を通す
    # 情報ソース（例: P2P生EEW形式 → Wolfx形式）向けのフック。
    # expected_fields の検証は変換前（生データ）の構造に対して行う
    # （上の validate_expected_fields 呼び出しがそれより先にあるのは
    # そのため）。変換に失敗した場合（ValueError等）はここで捕捉し、
    # 「不明」な通知を作ってしまうことなく、分かりやすいエラーとして
    # 打ち切る。
    data_converter = target.get("data_converter")
    if data_converter is not None:
        try:
            data = data_converter(data)
        except Exception as e:
            print(f"[TEST] エラー: 入力データの変換に失敗しました（{cog_key}）: {type(e).__name__}: {e}")
            logger.error(f"CLIテスト データ変換エラー（{cog_key}）: {e}", exc_info=True)
            return False

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


async def _wait_for_audio_drain(bot, cog=None, max_wait: float = 25.0) -> None:
    """
    【2026-08-31 追加】音声読み上げ・MP3チャイム再生が完了するまで待機する。

    経緯: 従来は各CLIテスト関数の末尾で固定 asyncio.sleep(3) の後に
    bot.close() していたが、実機ログで「play_mp3: 再生開始」のログは
    出るのに「play_mp3: 再生完了」のログが一度も出ないまま数秒後に
    シャットダウンしているケースが確認された（津波警報のチャイム音＝
    「ピロピロ音」が鳴らなかったという報告と一致）。
    AquesTalkPiによる音声合成＋aplay再生や、pygameによるMP3再生は、
    テキストが長い・ファイルが大きいと3秒以上かかることがあり、
    固定3秒の待機では単純に間に合わず、再生の途中でプロセスごと
    強制終了されてしまっていた。

    core/audio.py の speech_worker / mp3_worker はどちらも、実際に
    再生が完了した後で queue.task_done() を呼んでいる
    （speech_queue.task_done() は play_proc.communicate() 完了後、
    mp3_queue.task_done() は _play_mp3_blocking() 完了後）。
    そのため asyncio.Queue.join()（＝put_nowewait したすべての項目に
    ついて task_done() が呼ばれるまで待つ）を使えば、「キューに積んだ
    音声・チャイムの再生が実際に完了するまで」を正確に待てる
    （単に qsize()==0 を見るよりも正確。qsize() はワーカーが
    queue.get() で取り出した瞬間にゼロになるが、そのアイテムの
    実際の再生はまだ完了していないため）。

    対象Cogは2種類ある（core/audio.py 冒頭のdocstring参照）:
      - AudioMixin を直接継承するCog（tsunami/volcano/usgs/other等）
        → cog.speech_queue / cog.mp3_queue を直接持つ
      - AudioClientMixin を継承するCog（eew/quake）
        → 自身のキューは持たず、AudioCog（bot.get_cog("AudioCog")）に
          委譲している
    どちらのケースにも対応できるよう、テスト対象Cog自身とAudioCogの
    両方のキューをチェックする。

    max_wait 秒以内にキューが空にならなかった場合は、待つのを諦めて
    警告ログを出し、通常通りシャットダウンする（無限に待ち続けて
    テストプロセスがハングすることを避けるための安全弁）。
    """
    queues = []
    audio_cog = bot.get_cog("AudioCog")
    for c in (cog, audio_cog):
        if c is None:
            continue
        sq = getattr(c, "speech_queue", None)
        mq = getattr(c, "mp3_queue", None)
        if sq is not None:
            queues.append(sq)
        if mq is not None:
            queues.append(mq)

    if not queues:
        # 対象Cogがそもそも音声機能を持たない（volcano_warning等）場合は
        # 従来通りの短い待機のみ行う
        await asyncio.sleep(3)
        return

    try:
        await asyncio.wait_for(
            asyncio.gather(*(q.join() for q in queues)),
            timeout=max_wait,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"CLIテスト: 音声・MP3再生が{max_wait}秒以内に完了しませんでした。"
            f"再生の途中でプロセスを終了する可能性があります。"
        )


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
        # 【2026-08-31 修正】固定3秒のsleepから、実際の再生完了を
        # queue.join()で確認する方式に変更（_wait_for_audio_drain参照）。
        await _wait_for_audio_drain(bot, cog)
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

    # 【2026-08-31 修正】一括実行では複数のCogが音声・MP3キューを持ちうる
    # ため、AudioCog（eew/quake分）に加え、実行した各対象Cogの
    # キューも合わせて待機する。cog=None を渡すと _wait_for_audio_drain
    # は AudioCog のみをチェックするため、tsunami/volcano/usgs/other等
    # 独自キューを持つCogについては個別に集めて渡す。
    tested_cogs = []
    for cog_key in TEST_TARGETS:
        target = TEST_TARGETS[cog_key]
        c = bot.get_cog(target["cog_name"])
        if c is not None and c not in tested_cogs:
            tested_cogs.append(c)
    for c in tested_cogs:
        await _wait_for_audio_drain(bot, c, max_wait=10.0)
    await bot.close()


async def run_auto_cli_test(bot, json_path: str) -> None:
    """
    --test_auto <json_path> で指定された自動判定テストを実行する。

    【2026-08-30 追加の経緯】
    気象庁からダウンロードした実際のXML変換JSON（例: 津波警報・注意報・
    予報＝VTSE41形式）を、形式の似ている別の --test_<cog>（例:
    P2P地震情報APIのcode=552形式を期待する --test_tsunami）に誤って
    渡してしまい、正しく読み込めない事例が発生した。情報ソースごとに
    形式が異なる中で、どの --test_<cog> を使えばよいかをファイルの
    中身から判断できるようにするための機能。

    sniff_test_target() でJSONの構造から候補となる cog_key を推定し、
    - 候補が0件: 未知の形式。既存のどの --test_<cog> にも一致しない
      旨を表示し、何もせず終了する（誤った対象を推測で実行しない）。
    - 候補が1件: その cog_key で自動的にテストを実行する
      （通常の --test_<cog_key> と同じ処理を内部的に呼び出す）。
    - 候補が2件以上: 区別する決め手がない状態。全候補を表示し、
      どちらの --test_<cog> を明示的に使うべきかをユーザーに委ねる
      （自動実行はしない。誤った対象で実行してしまうことを避ける
      ため）。

    Bot が on_ready 済みで呼び出すこと。実行後は明示的にプロセスを
    終了する。
    """
    banner = "=" * 60
    print(banner)
    print(f"[TEST] これは自動判定テスト実行です（--test_auto） — 入力: {json_path}")
    print(banner)
    logger.warning(f"★★★ CLIテストモード（自動判定）で実行中 ★★★ 入力={json_path}")

    try:
        data = load_test_json(json_path, exit_on_error=False)
    except (FileNotFoundError, json.JSONDecodeError):
        await bot.close()
        sys.exit(1)

    candidates = sniff_test_target(data)

    if not candidates:
        print("[TEST] 判定不能: このJSONの構造に一致する --test_<cog> が見つかりませんでした")
        print("[TEST]   既存のどの形式（EEW/地震情報/津波/地震感知情報/火山/USGS等）にも")
        print("[TEST]   一致しないようです。sniff_test_target() のフィンガープリントに")
        print("[TEST]   該当ルールが無い新しい形式である可能性があります。")
        print(f"[TEST]   トップレベルキー: {list(data.keys()) if isinstance(data, dict) else '（dict以外）'}")
        logger.warning(f"CLIテスト（自動判定）失敗: 候補が見つかりません（トップレベルキー: {list(data.keys()) if isinstance(data, dict) else data}）")
        print(banner)
        await bot.close()
        sys.exit(1)

    if len(candidates) > 1:
        print(f"[TEST] 判定保留: 複数の候補が見つかりました（{', '.join(candidates)}）")
        print("[TEST]   このJSONだけでは一意に絞り込めないため、自動実行はしません。")
        print("[TEST]   以下のいずれかを明示的に指定して実行してください:")
        for c in candidates:
            print(f"[TEST]     python3 bot.py --test_{c} {json_path}")
        logger.warning(f"CLIテスト（自動判定）: 候補複数のため保留 candidates={candidates}")
        print(banner)
        await bot.close()
        sys.exit(1)

    cog_key = candidates[0]
    target = TEST_TARGETS[cog_key]
    print(f"[TEST] 判定結果: --test_{cog_key} 相当と判断しました "
          f"({target['cog_name']}.{target['method']})")
    logger.warning(f"CLIテスト（自動判定）: {cog_key} と判定 ({target['cog_name']}.{target['method']})")

    cog, method = _resolve_cog_and_method(bot, cog_key, target)
    if cog is None or method is None:
        print(banner)
        await bot.close()
        sys.exit(1)

    try:
        await _invoke_test_target(cog_key, target, cog, method, json_path)
    finally:
        print(banner)
        # 【2026-08-31 修正】固定3秒のsleepから、実際の再生完了を
        # queue.join()で確認する方式に変更（_wait_for_audio_drain参照）。
        await _wait_for_audio_drain(bot, cog)
        await bot.close()
