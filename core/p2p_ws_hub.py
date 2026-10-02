"""
core/p2p_ws_hub.py
===================
P2P地震情報 WebSocket API v2（wss://api.p2pquake.net/v2/ws）への接続を
アプリケーション全体で厳密に1つだけ保持し、受信メッセージを各Cogへ
配信する共有ハブ。

【なぜこのモジュールが必要か】
P2P地震情報のWebSocket APIは「IPアドレスあたり最大2接続」というレート
制限がある。移行前は EewCog が code=556（緊急地震速報）専用の接続を
1つ保持していたが、今回の移行で code=551（地震情報）・552（津波）も
WebSocketで受信するようにした際、各Cog（EewCog/QuakeInfoCog/TsunamiCog）
がそれぞれ個別に `wss://api.p2pquake.net/v2/ws` へ接続すると、
Bot全体で3接続になってしまいレート制限に抵触する。

そのため、接続そのものはこのモジュールが1つだけ保持し、受信した
メッセージを code に応じて各Cogが登録したディスパッチャ（コールバック）
へ配る「ハブ」として振る舞う。

【フィルタリング対象】
  - 551（地震情報）              → quake ディスパッチャへ
  - 552（津波予報・警報・注意報） → tsunami ディスパッチャへ
  - 556（緊急地震速報（警報））   → eew ディスパッチャへ
  - 9611（地震感知情報）          → jishin_kanchi ディスパッチャへ（2026-08〜）
  - 555（ピア数情報）等、上記以外のコードは無視・破棄する。

【重複排除（デデュープ）】
P2P地震情報の仕様上、同一内容のメッセージが複数回配信されることがある
（再接続直後の直近データ再送や、サーバー側の仕様など）。メッセージ内の
`id`（551/552で使用）または `_id`（内部管理用IDが別途あるケース。
556のEEWでは EventID+Serial の組み合わせで別途デデュープ済みのため
ここでは扱わない）を記憶し、既知のIDは各ディスパッチャへ渡さず破棄する。

デデュープキャッシュは無制限に増え続けないよう、最大保持件数を超えたら
古いものから破棄する（FIFO、collections.OrderedDict使用）。

【使い方】
    from core.p2p_ws_hub import P2PWebSocketHub

    hub = P2PWebSocketHub(bot.is_closed)
    hub.register("eew", eew_cog.handle_p2p_eew)
    hub.register("quake", quake_cog.handle_p2p_quake)
    hub.register("tsunami", tsunami_cog.handle_p2p_tsunami)
    bot.loop.create_task(hub.run())

登録するハンドラは `async def handler(data: dict) -> None` のシグネチャを
持つコルーチン関数であること。ハンドラ内で例外が発生してもハブ自体は
落ちず、ログに記録して次のメッセージ処理を継続する。

【障害時フォールバックの判定（2026-10-01 追加）】
WebSocket への接続・再接続が failover_threshold 回（既定5回）連続で
失敗（切断・接続タイムアウト）した時点で「フォールバック状態」に入り、
add_failover_listener で登録されたコールバックへ active=True を通知する
（QuakeInfoCog が地震情報の代替取得を開始する。cogs/quake.py・
core/quake_failover.py 参照）。WebSocket の再接続はその後も並行して
続け、接続が recovery_seconds 秒以上安定したらフォールバック状態を解除
（active=False を通知）し、失敗カウントをリセットする。接続しても
すぐ切れることを繰り返す場合は安定とみなさず、失敗が積み上がる。
"""
import asyncio
import json
import logging
import time
import traceback
import hashlib
from collections import OrderedDict

from core.ws_helpers import ws_connect_loop
from core.config import P2P_WSS, P2P_FAILOVER_THRESHOLD, P2P_FAILOVER_RECOVERY_SECONDS

logger = logging.getLogger("QTLBot")

# P2Pメッセージの code と、対応するディスパッチャ登録キーの対応。
# 555（ピア数情報）等、ここに定義されていない code は無視・破棄する。
CODE_TO_KEY = {
    551: "quake",
    552: "tsunami",
    556: "eew",
    9611: "jishin_kanchi",
}

# デデュープキャッシュの最大保持件数（FIFOで古いものから破棄）。
# 551/552/556 それぞれ独立してこの件数を保持する。
DEDUPE_CACHE_MAX_SIZE = 200


class P2PWebSocketHub:
    """
    P2P地震情報 WebSocket への接続を1つだけ保持し、受信メッセージを
    code に応じて登録済みディスパッチャへ配信する共有ハブ。
    """

    def __init__(self, is_closed_fn,
                 failover_threshold: int = P2P_FAILOVER_THRESHOLD,
                 recovery_seconds: float = P2P_FAILOVER_RECOVERY_SECONDS):
        """
        Parameters
        ----------
        is_closed_fn : Bot が終了処理中かどうかを返す呼び出し可能オブジェクト
                       （例: lambda: bot.is_closed()）。ws_connect_loop に
                       そのまま渡し、Bot終了時にWebSocket再接続ループも
                       停止させるために使う。
        failover_threshold : 何回連続で接続に失敗したらフォールバック状態に
                       入るか（既定: P2P_FAILOVER_THRESHOLD=5）。
        recovery_seconds : 接続が何秒以上続いたら安定とみなして
                       フォールバック状態を解除するか
                       （既定: P2P_FAILOVER_RECOVERY_SECONDS=60）。
        """
        self._is_closed_fn = is_closed_fn
        # -- 障害時フォールバック判定用の状態 --
        self._failover_threshold = failover_threshold
        self._recovery_seconds = recovery_seconds
        self._ws_connected = False
        self._connect_seq = 0                       # 接続のたびに増やす世代番号
        self._consecutive_failures = 0
        self._first_failure_monotonic: float | None = None
        self._failover_active = False
        self._failover_listeners: list = []         # callback(active: bool)
        # key ("eew" / "quake" / "tsunami" / "jishin_kanchi" 等) -> async def handler(data) -> None
        # 【2026-08-19 修正】以前はキー一覧を {"eew": [], "quake": [], "tsunami": []}
        # と直接ハードコードしていたため、CODE_TO_KEY に新しいcode（例:
        # 9611→jishin_kanchi）を追加しても、ここが追従しておらず
        # register("jishin_kanchi", ...) が「未知のディスパッチャキー」
        # としてValueErrorを送出してしまうバグがあった。CODE_TO_KEY の
        # 値（ディスパッチャキー）から動的に生成することで、今後
        # 新しいcodeを追加する際にこの初期化を書き換える必要がなくなる。
        self._dispatchers: dict[str, list] = {key: [] for key in CODE_TO_KEY.values()}
        # code (551/552/556/9611等) ごとの直近受信ID（OrderedDictをFIFOキャッシュとして使用）
        self._seen_ids: dict[int, OrderedDict] = {
            code: OrderedDict() for code in CODE_TO_KEY
        }
        self._recv_count: dict[str, int] = {key: 0 for key in CODE_TO_KEY.values()}
        self._ignored_count = 0

    def register(self, key: str, handler) -> None:
        """
        指定した key（"eew" / "quake" / "tsunami"）に対するディスパッチ先
        ハンドラを登録する。同じ key に複数のハンドラを登録することも
        可能（その場合、受信メッセージ1件につき全ハンドラが呼ばれる）。

        handler : `async def handler(data: dict) -> None` のコルーチン関数。
        """
        if key not in self._dispatchers:
            raise ValueError(f"未知のディスパッチャキーです: {key!r}")
        self._dispatchers[key].append(handler)
        logger.info(f"P2PWebSocketHub: '{key}' ディスパッチャを登録しました")

    # ===============================
    # 障害時フォールバックの判定（2026-10-01 追加）
    # ===============================
    @property
    def failover_active(self) -> bool:
        """連続接続失敗が閾値に達し、フォールバック状態にあるか。"""
        return self._failover_active

    def add_failover_listener(self, callback) -> None:
        """
        フォールバック状態の変化（True=開始 / False=解除）を受け取る同期
        コールバック callback(active: bool) を登録する。
        コールバック内の例外はログに記録するだけで、ハブの動作には影響しない。
        """
        self._failover_listeners.append(callback)

    def _notify_failover(self, active: bool) -> None:
        for cb in self._failover_listeners:
            try:
                cb(active)
            except Exception:
                logger.error(
                    f"P2PWebSocketHub: フォールバック通知コールバックでエラー:\n{traceback.format_exc()}"
                )

    def _on_ws_connect(self) -> None:
        """WebSocket の接続確立時（ws_connect_loop の on_connect）。"""
        self._ws_connected = True
        self._connect_seq += 1
        seq = self._connect_seq
        try:
            asyncio.get_running_loop().create_task(self._recovery_check(seq))
        except RuntimeError:
            # イベントループ外から呼ばれた場合（単体テスト等）は何もしない
            pass

    async def _recovery_check(self, seq: int) -> None:
        """接続が recovery_seconds 秒以上続いていれば「安定」とみなして復旧させる。"""
        await asyncio.sleep(self._recovery_seconds)
        if self._ws_connected and self._connect_seq == seq:
            self._mark_stable()

    def _mark_stable(self) -> None:
        had_failures = self._consecutive_failures > 0
        self._consecutive_failures = 0
        self._first_failure_monotonic = None
        if self._failover_active:
            self._failover_active = False
            logger.warning(
                f"P2PWebSocketHub: WebSocket が {self._recovery_seconds:g} 秒以上安定したため、"
                "代替取得（フォールバック）を停止します"
            )
            self._notify_failover(False)
        elif had_failures:
            logger.info("P2PWebSocketHub: WebSocket が安定したため接続失敗カウントをリセットしました")

    def _on_ws_failure(self) -> None:
        """
        WebSocket の切断・接続失敗時（ws_connect_loop の on_disconnect、または
        正常クローズ）。連続失敗が閾値に達したらフォールバック状態に入る。
        """
        self._ws_connected = False
        self._connect_seq += 1          # 進行中の recovery_check を無効化
        self._consecutive_failures += 1
        if self._first_failure_monotonic is None:
            self._first_failure_monotonic = time.monotonic()
        if (not self._failover_active
                and self._consecutive_failures >= self._failover_threshold):
            self._failover_active = True
            elapsed = time.monotonic() - self._first_failure_monotonic
            logger.warning(
                f"P2PWebSocketHub: WebSocket の連続接続失敗が {self._consecutive_failures} 回"
                f"（約{elapsed:.0f}秒）に達したため、気象庁データによる代替取得"
                "（フォールバック）を開始します。WebSocketの再接続は並行して続けます"
            )
            self._notify_failover(True)

    def _is_duplicate(self, code: int, data_id, raw: str | None = None) -> bool:
        """
        code・data_id の組み合わせが既知（重複）かどうかを判定し、
        未知であればキャッシュに記録する。

        【2026-08-01/02 修正: id=None フォールバック】
        当初は data_id が None（P2P地震情報のメッセージから id/_id を
        取得できないケース）の場合、重複判定不能として常に False
        （＝重複ではない）を返し、フィルタリングせず素通しさせていた。
        しかし実運用で、同一またはid欠落のcode=551（地震情報）メッセージが
        WebSocketから複数回配信されるケースが実際に発生し、素通し設計の
        せいで同一内容が繰り返しディスパッチされるリスクがあった。

        そのため、data_id が None の場合は raw（メッセージの生JSON文字列）
        のハッシュ値をフォールバックの重複判定キーとして使う。これにより
        「id が取得できない」ケースでも、内容が完全に同一のメッセージが
        短時間に繰り返し届いた場合は確実に弾ける。
        raw も渡されない（呼び出し側の実装漏れ等）場合のみ、従来通り
        判定不能として False を返す。
        """
        if data_id is None:
            if raw is None:
                return False
            data_id = ("_hash_", hashlib.sha256(raw.encode("utf-8")).hexdigest())

        cache = self._seen_ids.get(code)
        if cache is None:
            return False

        if data_id in cache:
            return True

        cache[data_id] = True
        # FIFOで古いものから破棄
        while len(cache) > DEDUPE_CACHE_MAX_SIZE:
            cache.popitem(last=False)
        return False

    async def _dispatch(self, key: str, data: dict) -> None:
        handlers = self._dispatchers.get(key, [])
        if not handlers:
            logger.debug(
                f"P2PWebSocketHub: '{key}' 用のハンドラが登録されていないため破棄します"
            )
            return
        self._recv_count[key] = self._recv_count.get(key, 0) + 1
        for handler in handlers:
            try:
                await handler(data)
            except Exception:
                logger.error(
                    f"P2PWebSocketHub: '{key}' ハンドラでエラー発生:\n{traceback.format_exc()}"
                )

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            logger.warning(f"P2PWebSocketHub: JSONパース失敗: {raw[:200]!r}")
            return

        code = data.get("code")
        key = CODE_TO_KEY.get(code)
        if key is None:
            # 555（ピア数情報）等、対象外のcodeは無視・破棄
            self._ignored_count += 1
            return

        data_id = data.get("id") or data.get("_id")
        if data_id is None:
            # 本来 P2P地震情報 API の JMAQuake（551）/JMATsunami（552）は
            # id が必須項目のはずだが、実運用で id が取得できない
            # メッセージが実際に届いたことがある
            # （2026-08-01/02, 地図画像非表示・二重通知インシデント）。
            # 原因調査のため、生データ全体を警告ログに残す
            # （info/debugではローテーションで消えやすく、原因調査の
            # ためには warning レベルで確実に残すほうが望ましい）。
            logger.warning(
                f"P2PWebSocketHub: code={code} のメッセージに id/_id が"
                f"見つかりません。生データ: {raw!r}"
            )
        if self._is_duplicate(code, data_id, raw=raw):
            logger.debug(
                f"P2PWebSocketHub: 重複メッセージのためスキップ code={code} id={data_id}"
            )
            return

        await self._dispatch(key, data)

    async def run(self) -> None:
        """
        WebSocket接続を開始する。切断時は core.ws_helpers.ws_connect_loop
        による自動再接続（指数バックオフ）に従う。接続はアプリケーション
        全体でこの1本のみとすること（複数箇所から呼び出さない）。
        """
        async def _handle(ws):
            async for raw in ws:
                await self._handle_message(raw)
            # 例外なしでループを抜けた＝サーバー側からの正常クローズ。
            # 例外による切断は ws_connect_loop の on_disconnect 側で数えるため、
            # ここで数えるのは正常クローズの場合のみ（二重カウントしない）。
            self._on_ws_failure()

        await ws_connect_loop(
            self._is_closed_fn, P2P_WSS, "P2P地震情報 統合", _handle,
            on_disconnect=self._on_ws_failure,
            on_connect=self._on_ws_connect,
        )

    def get_stats(self) -> dict:
        """!status 等から参照するための簡易統計情報。"""
        return {
            "recv_count": dict(self._recv_count),
            "ignored_count": self._ignored_count,
            "dedupe_cache_sizes": {
                CODE_TO_KEY[code]: len(cache)
                for code, cache in self._seen_ids.items()
            },
            "failover": {
                "active": self._failover_active,
                "ws_connected": self._ws_connected,
                "consecutive_failures": self._consecutive_failures,
                "threshold": self._failover_threshold,
            },
        }