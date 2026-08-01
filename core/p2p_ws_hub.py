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
"""
import json
import logging
import traceback
from collections import OrderedDict

from core.ws_helpers import ws_connect_loop
from core.config import P2P_WSS

logger = logging.getLogger("QTLBot")

# P2Pメッセージの code と、対応するディスパッチャ登録キーの対応。
# 555（ピア数情報）等、ここに定義されていない code は無視・破棄する。
CODE_TO_KEY = {
    551: "quake",
    552: "tsunami",
    556: "eew",
}

# デデュープキャッシュの最大保持件数（FIFOで古いものから破棄）。
# 551/552/556 それぞれ独立してこの件数を保持する。
DEDUPE_CACHE_MAX_SIZE = 200


class P2PWebSocketHub:
    """
    P2P地震情報 WebSocket への接続を1つだけ保持し、受信メッセージを
    code に応じて登録済みディスパッチャへ配信する共有ハブ。
    """

    def __init__(self, is_closed_fn):
        """
        Parameters
        ----------
        is_closed_fn : Bot が終了処理中かどうかを返す呼び出し可能オブジェクト
                       （例: lambda: bot.is_closed()）。ws_connect_loop に
                       そのまま渡し、Bot終了時にWebSocket再接続ループも
                       停止させるために使う。
        """
        self._is_closed_fn = is_closed_fn
        # key ("eew" / "quake" / "tsunami") -> async def handler(data) -> None
        self._dispatchers: dict[str, list] = {"eew": [], "quake": [], "tsunami": []}
        # code (551/552/556) ごとの直近受信ID（OrderedDictをFIFOキャッシュとして使用）
        self._seen_ids: dict[int, OrderedDict] = {
            code: OrderedDict() for code in CODE_TO_KEY
        }
        self._recv_count: dict[str, int] = {"eew": 0, "quake": 0, "tsunami": 0}
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

    def _is_duplicate(self, code: int, data_id) -> bool:
        """
        code・data_id の組み合わせが既知（重複）かどうかを判定し、
        未知であればキャッシュに記録する。data_id が取得できない
        メッセージ（None）は重複判定できないため常に False を返す
        （フィルタリングせず素通しする）。
        """
        if data_id is None:
            return False

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
        if self._is_duplicate(code, data_id):
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

        await ws_connect_loop(self._is_closed_fn, P2P_WSS, "P2P地震情報 統合", _handle)

    def get_stats(self) -> dict:
        """!status 等から参照するための簡易統計情報。"""
        return {
            "recv_count": dict(self._recv_count),
            "ignored_count": self._ignored_count,
            "dedupe_cache_sizes": {
                CODE_TO_KEY[code]: len(cache)
                for code, cache in self._seen_ids.items()
            },
        }
