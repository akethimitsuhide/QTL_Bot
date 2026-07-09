"""
core/kyoshin_image_monitor.py
==============================
core.kyoshin_detector.EventManager と連動し、
「イベント発生 → 画像通知関数の継続実行 → イベント終了で停止」
というライフサイクルを制御するメインループ。

【設計方針】
このモジュールは asyncio ベースの非同期ループとして実装し、
実際の「画像解析でリアルタイム震度を取得する処理」は
外部から callback として注入する（このモジュール自体は
画像デコードの実装を持たない）。

呼び出し側（Cog）は以下の3つの callback を渡す:
    get_readings()         : dict[station_id, shindo] を返す非同期関数
                              （実際の画像取得・色相解析はここで行う）
    send_kyoshin_image()   : イベントが継続している間、繰り返し呼ばれる
                              非同期関数（Discord への画像通知処理）
    on_event_ended()       : イベント終了時に1回だけ呼ばれる（任意）

【ライフサイクル】
    1. poll_interval_sec ごとに get_readings() を呼び、
       EventManager に ingest → tick する
    2. tick() が "created" を返したら、そのイベント専用の
       画像通知ループ（send_kyoshin_image を image_interval_sec ごとに
       繰り返し呼ぶ asyncio.Task）を起動する
    3. tick() が "ended" を返したら、対応するイベントの
       画像通知ループを cancel する
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from core.kyoshin_detector import EventManager, DetectorConfig, SeismicEvent

logger = logging.getLogger("QTLBot")

GetReadingsFn = Callable[[], Awaitable[dict[str, float]]]
SendImageFn = Callable[[SeismicEvent], Awaitable[None]]
OnEventEndedFn = Callable[[str], Awaitable[None]]


class KyoshinImageMonitor:
    """
    強震モニタ画像解析による揺れ検知と、画像通知関数の継続実行を制御するクラス。

    使い方（Cog内の例）:
        monitor = KyoshinImageMonitor(
            config=DetectorConfig(),
            get_readings=self._fetch_current_shindo_map,   # 画像解析結果を返す
            send_kyoshin_image=self._send_kyoshin_image,     # Discord通知処理
        )
        for sid, neighbors in region_map.items():
            monitor.event_manager.register_station(sid, neighbors=neighbors)

        self.kyoshin_task = self.bot.loop.create_task(monitor.run())
        ...
        # Cog終了時
        await monitor.stop()
    """

    def __init__(
        self,
        get_readings: GetReadingsFn,
        send_kyoshin_image: SendImageFn,
        config: DetectorConfig | None = None,
        on_event_ended: OnEventEndedFn | None = None,
        poll_interval_sec: float = 1.0,
        image_interval_sec: float = 2.0,
    ):
        self.event_manager = EventManager(config)
        self._get_readings = get_readings
        self._send_kyoshin_image = send_kyoshin_image
        self._on_event_ended = on_event_ended
        self.poll_interval_sec = poll_interval_sec
        self.image_interval_sec = image_interval_sec

        self._running = False
        self._image_tasks: dict[str, asyncio.Task] = {}  # event_id -> 画像通知継続タスク

    # ===============================
    # メインループ
    # ===============================
    async def run(self) -> None:
        """
        poll_interval_sec ごとに観測値を取り込み、イベントのライフサイクルを制御する。
        Bot終了時（CancelledError）まで動き続ける想定。
        """
        self._running = True
        logger.info("KyoshinImageMonitor: メインループを開始します")

        while self._running:
            try:
                readings = await self._get_readings()
                now = asyncio.get_event_loop().time()

                for station_id, shindo in readings.items():
                    self.event_manager.ingest(station_id, shindo, now)

                changes = self.event_manager.tick(now)

                for change in changes:
                    if change.kind == "created":
                        self._start_image_loop(change.event_id, change.event)
                    elif change.kind == "updated":
                        # 最大震度更新等。画像通知ループは既に動いているのでここでは何もしない。
                        pass
                    elif change.kind == "merged":
                        # マージされた側(merged_from)の画像通知ループを停止する
                        # （生き残る側 keep_id は継続）
                        if change.merged_from:
                            await self._stop_image_loop(change.merged_from)
                    elif change.kind == "ended":
                        await self._stop_image_loop(change.event_id)
                        if self._on_event_ended:
                            await self._on_event_ended(change.event_id)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"KyoshinImageMonitor: メインループでエラー: {e}", exc_info=True)

            await asyncio.sleep(self.poll_interval_sec)

    async def stop(self) -> None:
        """Bot終了時に呼ぶ。メインループとすべての画像通知ループを停止する。"""
        self._running = False
        for event_id in list(self._image_tasks.keys()):
            await self._stop_image_loop(event_id)
        logger.info("KyoshinImageMonitor: 停止しました")

    # ===============================
    # 画像通知ループの起動・停止
    # ===============================
    def _start_image_loop(self, event_id: str, event: SeismicEvent) -> None:
        if event_id in self._image_tasks:
            return  # 既に起動済み

        async def _loop():
            logger.info(f"KyoshinImageMonitor: イベント {event_id[:8]} の画像通知ループを開始")
            try:
                while True:
                    current_event = self.event_manager.events.get(event_id)
                    if current_event is None:
                        # tick() 側で既に終了済み（保険）
                        break
                    try:
                        await self._send_kyoshin_image(current_event)
                    except Exception as e:
                        logger.error(f"KyoshinImageMonitor: send_kyoshin_image エラー: {e}", exc_info=True)
                    await asyncio.sleep(self.image_interval_sec)
            except asyncio.CancelledError:
                logger.info(f"KyoshinImageMonitor: イベント {event_id[:8]} の画像通知ループを停止")
                raise

        self._image_tasks[event_id] = asyncio.get_event_loop().create_task(_loop())

    async def _stop_image_loop(self, event_id: str) -> None:
        task = self._image_tasks.pop(event_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass