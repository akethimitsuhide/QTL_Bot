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

    class QuakeInfoCog(commands.Cog, AudioClientMixin, P2PImageMixin):
        ...

【要求する self の属性】
    self.session : aiohttp.ClientSession（各Cogが cog_load で生成したもの）

【P2P_IMAGE_ATTACH_ENABLED フラグについて】
過去のWebSocket移行作業で「地図画像が表示されない」バグが発生したことが
あり、原因切り分けのため core.config.P2P_IMAGE_ATTACH_ENABLED で
画像添付処理自体を一時的に無効化できるようにしている。
無効化時（False）は _attach_p2p_image() がリトライ処理を一切行わず、
即座に return する。この場合、呼び出し元は build_p2p_image_url_text()
（下記）で得られる画像URLの文字列を通知本文（description）に含めることで、
Discord自体のリンクプレビュー機能によって画像を自動展開させる
（Bot側でのCDNポーリング・embed編集を伴わない、より単純な経路）。

【bot.py からの移行元】
元 bot.py の p2p_image_url() / _attach_p2p_image() 定義
（Cog分割前の旧ファイルで cogs/quake.py に一時的に複製されていたものを統合）。
"""
import asyncio
import logging

import aiohttp
import discord

from core.config import P2P_IMAGE_ATTACH_ENABLED

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

    def build_p2p_image_url_text(self, image_id: str) -> str:
        """
        P2P_IMAGE_ATTACH_ENABLED=False のときに通知本文へ追記するための、
        画像URLのみのプレーンテキストを返す。Discordはメッセージ本文中の
        画像URL（拡張子が画像形式のリンク）を自動的にプレビュー展開するため、
        Bot側でCDNの反映を待ってembedを編集するリトライ処理を行わなくても
        同等の見た目を得られる。image_id が空、またはURLを生成できない
        場合は空文字列を返す。
        """
        url = self.p2p_image_url(image_id)
        return url or ""

    async def _attach_p2p_image(self, message: discord.Message, image_id: str,
                                 on_failure=None) -> None:
        """
        P2P CDN への画像アップロード遅延対策。
        通知直後は画像が未生成のことがあるため、最大 MAX_RETRY 回リトライして
        URL が有効になったタイミングでメッセージを編集して画像を追加する。

        P2P_IMAGE_ATTACH_ENABLED=False の場合、このメソッドは何もせず
        即座に return する（画像URLをDiscordの自動リンク展開に委ねる方式に
        切り替える場合は、呼び出し元で build_p2p_image_url_text() を使って
        通知本文に画像URLを含めること）。

        on_failure : 全リトライ失敗時に呼ばれるコールバック（省略可）。
            `await on_failure(message, url)` の形で1回だけ呼び出される。
            呼び出し元（notify_quake等）はこれを使って、地図画像が
            結局表示できなかった場合のフォールバック処理
            （本文への震度一覧追記・リンクのみ貼付など）を行える。
            P2P_IMAGE_ATTACH_ENABLED=False のときは on_failure は呼ばれない
            （呼び出し元が build_p2p_image_url_text() で代替表示を
            用意している前提のため）。
        """
        if not P2P_IMAGE_ATTACH_ENABLED:
            logger.debug(
                f"P2P画像添付: P2P_IMAGE_ATTACH_ENABLED=False のためスキップします "
                f"(image_id={image_id})"
            )
            return

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
        if on_failure:
            try:
                await on_failure(message, url)
            except Exception as e:
                logger.error(f"P2P画像取得失敗時のフォールバック処理でエラー: {e}")
