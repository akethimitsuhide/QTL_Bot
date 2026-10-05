"""
core/apm.py
===========
Mackerel の APM（アプリケーションパフォーマンスモニタリング）連携を提供するモジュール。
OpenTelemetry (OTLP) 経由でトレース情報を Mackerel に送信する。

【実装の根拠】
Mackerel公式ドキュメント
(https://mackerel.io/ja/docs/entry/tracing/installations/python) の
「ゼロコード計装」（opentelemetry-instrument 経由でアプリを起動する方式）を
参考に、本プロジェクトでは discord.py の Cog ライフサイクル
（cog_load/cog_unload）に組み込む「コードベース計装」として実装している。
公式のゼロコード計装（起動コマンドを opentelemetry-instrument でラップする方式）
とは実行方式が異なるが、送信先エンドポイント・ヘッダー形式は公式ドキュメント
記載の値に合わせている。

送信先: https://otlp-vaxila.mackerelio.com/v1/traces
必須ヘッダー: Mackerel-Api-Key（APIキー）, Accept: */*

【デフォルトは無効（APM_ENABLED=false）】
これはBotの必須機能ではなく、運用者（Mackerel等でBotを監視したい人）向けの
オプション機能のため。無効時はこのモジュールの処理は一切実行されず、
opentelemetry 関連パッケージが未インストールでも動作に影響しない。

【対応する計装（instrumentation）】
- aiohttp クライアント: 各Cogが行うJMA/USGS/P2P等へのHTTPリクエストを
  自動的にトレース対象にする（レイテンシ・失敗の可視化に有用）。

【使い方】
    from core.apm import setup_apm, shutdown_apm

    # Bot起動時、他のCogより先に呼ぶ（aiohttp計装が後続のセッション作成にも効くよう）
    setup_apm()
    ...
    # Bot終了時
    shutdown_apm()
"""
import asyncio
import logging

from core.config import (
    APM_ENABLED, APM_SERVICE_NAME, APM_MACKEREL_API_KEY,
    APM_OTLP_ENDPOINT, APM_OTLP_API_KEY_HEADER,
)

logger = logging.getLogger("QTLBot")

_tracer_provider = None
_apm_active = False


# 【2026-10-03追加】Mackerel APM に「エラー」として課題化されていた、想定内の失敗。
# aiohttp クライアントの自動計装は、例外が出たリクエストのスパンを一律に
# ステータス ERROR にして例外を記録する。本Botでは次の2つが通常運用で頻繁に起き、
# どちらもコード側で捕捉・処理済みのため、障害ではない。
#   - asyncio.TimeoutError（Python 3.11以降は組み込みの TimeoutError と同一）:
#       強震モニタ等の外部サーバーの応答が設定秒（例: 5秒）を超えた。失敗時は
#       次の周期で再試行する設計。
#   - asyncio.CancelledError: Discord への送信（discord.py の POST）等の途中で、
#       EEW最終報での強震モニタ監視の終了（task.cancel()）やBot終了により
#       タスクがキャンセルされた。
EXPECTED_ERRORS = (asyncio.TimeoutError, asyncio.CancelledError)


def is_expected_error(exc) -> bool:
    """HTTPクライアントのスパンで、障害ではなく想定内とみなす例外か。"""
    return isinstance(exc, EXPECTED_ERRORS)


def _mark_expected_error(span, params) -> None:
    """
    aiohttp 計装の response_hook（例外時にも、エラー状態を設定した後に呼ばれる）。
    想定内の例外（is_expected_error）の場合のみ、スパンのステータスを OK に
    上書きし、属性 qtl.expected_error=true を付ける。例外の記録（exception イベント）は
    残るため、発生自体は Mackerel のトレース上で確認できる。
    HTTP 5xx・接続拒否（ClientConnectorError）・DNS失敗等はエラーのままにする。
    フックの失敗が通常のリクエスト処理に影響しないよう、例外は握りつぶす。
    """
    try:
        if not is_expected_error(getattr(params, "exception", None)):
            return
        from opentelemetry.trace import Status, StatusCode
        span.set_attribute("qtl.expected_error", True)
        span.set_status(Status(StatusCode.OK))
    except Exception:
        logger.debug("APM: _mark_expected_error でエラー", exc_info=True)


def setup_apm() -> bool:
    """
    APM_ENABLED=true の場合のみ、OpenTelemetry SDK を初期化し、
    Mackerel の OTLP エンドポイントへのトレース送信を開始する。

    Returns
    -------
    bool : APMが実際に有効化されたかどうか
    """
    global _tracer_provider, _apm_active

    if not APM_ENABLED:
        logger.info("APM: 無効です（APM_ENABLED=false）")
        return False

    if not APM_MACKEREL_API_KEY:
        logger.warning(
            "APM: APM_ENABLED=true ですが APM_MACKEREL_API_KEY が未設定のため、"
            "APM連携を開始できません。.env を確認してください。"
        )
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError as e:
        logger.warning(
            f"APM: 必要なパッケージがインストールされていません ({e})。"
            f"requirements.txt の 'APM (Mackerel連携)' セクションを"
            f"pip install してから APM_ENABLED=true にしてください。"
        )
        return False

    try:
        resource = Resource.create({"service.name": APM_SERVICE_NAME})
        provider = TracerProvider(resource=resource)

        exporter = OTLPSpanExporter(
            # APM_OTLP_ENDPOINT は /v1/traces を含む完全な URL（config.py 参照）。
            # ここでパスを追加結合すると "/v1/traces/v1/traces" のような
            # 壊れたURLになり、Mackerel側にデータが届かなくなるため注意。
            endpoint=APM_OTLP_ENDPOINT,
            headers={
                APM_OTLP_API_KEY_HEADER: APM_MACKEREL_API_KEY,
                # Mackerel公式ドキュメントの curl 例に倣い Accept ヘッダーを明示する。
                "Accept": "*/*",
            },
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer_provider = provider

        # aiohttp クライアントの自動計装
        # 各Cogの fetch_* が行うHTTPリクエストがすべてトレース対象になる。
        # aiohttp.ClientSession のメソッドをパッチする方式のため、
        # 既に生成済みのセッションにも適用される（インスタンス生成前である必要はない）。
        try:
            from opentelemetry.instrumentation.aiohttp_client import (
                AioHttpClientInstrumentor,
            )
            AioHttpClientInstrumentor().instrument(response_hook=_mark_expected_error)
            logger.info(
                "APM: aiohttp クライアントの自動計装を有効化しました"
                "（想定内のTimeoutError/CancelledErrorはエラー扱いにしません）"
            )
        except ImportError:
            logger.warning(
                "APM: opentelemetry-instrumentation-aiohttp-client が未インストールのため、"
                "HTTPリクエストの自動計装はスキップされます（トレース自体は有効です）"
            )

        _apm_active = True
        logger.info(
            f"APM: Mackerel連携を開始しました "
            f"(service.name={APM_SERVICE_NAME}, endpoint={APM_OTLP_ENDPOINT})"
        )
        return True

    except Exception as e:
        logger.error(f"APM: 初期化中にエラーが発生しました: {e}", exc_info=True)
        return False


def shutdown_apm() -> None:
    """Bot終了時に呼び出し、バッファ内の未送信スパンをフラッシュしてから終了する。"""
    global _apm_active
    if not _apm_active or _tracer_provider is None:
        return
    try:
        _tracer_provider.shutdown()
        logger.info("APM: シャットダウンしました（未送信スパンをフラッシュ済み）")
    except Exception as e:
        logger.warning(f"APM: シャットダウン中にエラー: {e}")
    finally:
        _apm_active = False


def is_apm_active() -> bool:
    """APMが実際に稼働中かどうかを返す（!status 等での表示用）。"""
    return _apm_active
