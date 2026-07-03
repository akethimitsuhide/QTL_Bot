"""
core/p2p_image.py
==================
P2P地震情報 API の地図画像URL生成・CDN反映遅延対策のリトライ添付を提供するモジュール。

【設計方針: なぜ独立モジュールなのか】
notify_quake（地震情報）と notify_tsunami（P2P津波情報）の両方が
同じCDN画像添付ロジックを必要とする。Cog分割前は同じクラス内の
メソッドとして共有できていたが、cogs/quake.py と cogs/tsunami.py に
分かれた今、このロジックを両方に複製すると保守性が落ちる
（CDN側の挙動が変わったときに2箇所を直す必要が出る）。

そのため core/audio.py の AudioMixin と同じパターンで
P2PImageMixin を提供し、両方のCogがこれを多重継承する。

    class QuakeEewCog(commands.Cog, AudioMixin, P2PImageMixin):
        ...

【要求する self の属性】
    self.session : aiohttp.ClientSession（各Cogが cog_load で生成したもの）

【bot.py からの移行元】
元 bot.py の p2p_image_url() / _attach_p2p_image() 定義
（Cog分割前の旧ファイルで cogs/quake.py に一時的に複製されていたものを統合）。
"""
import asyncio
import logging

import aiohttp
import discord

logger = logging.getLogger("QTLBot")


class P2PImageMixin:
    """
    P2P地震情報の地図画像をEmbedに添付するためのメソッド群を提供する Mixin。

    要求する self の属性:
        self.session : aiohttp.ClientSession
    """

    @staticmethod
    def p2p_image_url(image_id: str) -> str | None:
        if not image_id:
            return None
        return f"https://cdn.p2pquake.net/app/images/{image_id}_trim_big.png"

    async def _attach_p2p_image(self, message: discord.Message, image_id: str) -> None:
        """
        P2P CDN への画像アップロード遅延対策。
        通知直後は画像が未生成のことがあるため、最大 MAX_RETRY 回リトライして
        URL が有効になったタイミングでメッセージを編集して画像を追加する。
        """
        if not image_id:
            return
        url = self.p2p_image_url(image_id)
        if not url:
            return

        MAX_RETRY = 15   # 最大 15 回 × 約 8 秒 = 約 2 分
        INTERVAL  = 5    # 各試行前の待機秒数

        for attempt in range(MAX_RETRY):
            await asyncio.sleep(INTERVAL)
            try:
                # Range ヘッダーなしの通常 GET で URL の疎通確認をする。
                # Range: bytes=0-0 を付けると CDN が接続を切断する場合があるため使わない。
                async with self.session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    ok = resp.status == 200
                    # コンテンツを読み捨てて接続を解放
                    await resp.read()
                if ok:
                    try:
                        # message.embeds[0] はキャッシュ済みオブジェクトのため .copy() が必要
                        embed = message.embeds[0].copy()
                        embed.set_image(url=url)
                        await message.edit(embed=embed)
                        logger.info(f"P2P画像を追加しました: id={image_id} (attempt={attempt+1})")
                    except Exception as e:
                        logger.warning(f"P2P画像メッセージ編集失敗: {e}")
                    return
                logger.debug(f"P2P画像まだなし: HTTP {resp.status} attempt={attempt+1}/{MAX_RETRY}")
            except Exception as e:
                logger.debug(f"P2P画像確認エラー: {e} attempt={attempt+1}")

        logger.info(f"P2P画像: {MAX_RETRY}回リトライ後も取得できませんでした id={image_id}")
