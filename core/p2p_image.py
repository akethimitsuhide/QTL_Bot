"""
core/p2p_image.py
==================
P2P地震情報 API の地図画像URL生成・CDN反映遅延対策のリトライ埋め込みを
提供するモジュール。

【設計方針: なぜ独立モジュールなのか】
notify_quake（地震情報）と notify_tsunami（P2P津波情報）の両方が
同じ画像添付ロジックを必要とする。Cog分割前は同じクラス内の
メソッドとして共有できていたが、cogs/quake.py と cogs/tsunami.py に
分かれた今、このロジックを両方に複製すると保守性が落ちる
（CDN側の挙動が変わったときに2箇所を直す必要が出る）。

そのため core/audio.py の AudioMixin と同じパターンで
P2PImageMixin を提供し、両方のCogがこれを多重継承する。

    class QuakeInfoCog(commands.Cog, AudioClientMixin, P2PImageMixin):
        ...

【2026-08-02: リトライ埋め込み方式の廃止 → 再実装の経緯】
以前の _attach_p2p_image() は「HTTPステータスが200であること」のみを
成功判定基準にしていた。しかし実運用で、CDNが200を返した時点では
まだ画像の実体（バイナリ）が生成し切っておらず、Discord側が実際に
その画像をフェッチした瞬間には失敗し、壊れたリンクとして恒久的に
表示されてしまう不具合が繰り返し発生した。この問題を受けて、一時的に
リトライ埋め込み自体を廃止し、画像URLを本文にテキストとして含める
方式（build_p2p_image_url_text）に切り替えていた。

その後、実運用でテキストURL方式は問題なく動作することを確認できたが、
見た目としては embed 画像として綺麗に埋め込まれた状態が望ましいという
判断により、以下の検証を追加した安定版として _attach_p2p_image を
再実装した:

  1. HTTPステータス200だけでなく、レスポンスボディの実サイズを検証する
     （MIN_VALID_IMAGE_BYTES 未満は「まだ生成中」とみなし、成功と
     判定しない）。
  2. レスポンスボディの先頭がPNGファイルのマジックバイト
     （\\x89PNG\\r\\n\\x1a\\n）で始まっていることを検証する。
     HTTPステータスが200でも、CDNがまだ生成中のプレースホルダー
     （空ボディやエラーページのHTML等）を返している場合はこれで
     確実に弾ける。

  これらの検証を両方通過した場合のみ「画像が実際に利用可能」と
  判定し、embedへの埋め込みを行う。どちらか一方でも失敗した場合は
  「まだCDN反映が完了していない」として通常通りリトライを続ける。

【フォールバック】
全リトライ失敗時は on_failure コールバックが呼ばれる。呼び出し元は
これを使って、画像が結局表示できなかった場合の代替表示
（本文へのURLテキスト追記等、build_p2p_image_url_text 参照）を
行うことができる。

【P2P_IMAGE_ATTACH_ENABLED フラグについて】
原因切り分けのため、core.config.P2P_IMAGE_ATTACH_ENABLED で
画像埋め込み処理自体を一時的に無効化できるようにしている。
無効化時（False）は _attach_p2p_image() がリトライ処理を一切行わず、
即座に return する。この場合、呼び出し元は build_p2p_image_url_text()
で得られる画像URLの文字列を通知本文（description）に含めることで、
Discord自体のリンクプレビュー機能によって画像を自動展開させる
（Bot側でのCDNポーリング・embed編集を伴わない、より単純な経路）。
"""
import asyncio
import logging

import aiohttp
import discord

from core.config import P2P_IMAGE_ATTACH_ENABLED

logger = logging.getLogger("QTLBot")

# PNGファイルのマジックバイト（シグネチャ）。
# CDNが返すレスポンスボディがこれで始まっていない場合、
# HTTPステータスが200であっても有効な画像とはみなさない。
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# CDNが返す画像として妥当とみなす最小バイト数。
# トリミング済みの小さい画像でも、実際のP2P地震情報の地図画像は
# 通常これより大幅に大きい（数KB〜数十KB）。空ボディや極端に
# 小さいプレースホルダーを「生成中」として弾くための閾値。
MIN_VALID_IMAGE_BYTES = 1024


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
        通知本文（description）へ追記するための、画像URLのみのプレーン
        テキストを返す。P2P_IMAGE_ATTACH_ENABLED=False のときの代替表示、
        または _attach_p2p_image が全リトライ失敗した場合の
        on_failure コールバックから使うことを想定している。
        image_id が空、またはURLを生成できない場合は空文字列を返す。
        """
        url = self.p2p_image_url(image_id)
        return url or ""

    @staticmethod
    def _is_valid_image_response(body: bytes) -> bool:
        """
        CDNから取得したレスポンスボディが、実際に利用可能な画像として
        妥当かどうかを検証する。
        - 先頭がPNGマジックバイトで始まっているか
        - 最小バイト数（MIN_VALID_IMAGE_BYTES）以上あるか
        の両方を満たす場合のみ True を返す。
        """
        if len(body) < MIN_VALID_IMAGE_BYTES:
            return False
        if not body.startswith(_PNG_SIGNATURE):
            return False
        return True

    async def _attach_p2p_image(self, message: discord.Message, image_id: str,
                                 on_failure=None) -> None:
        """
        P2P CDN への画像アップロード遅延対策。
        通知直後は画像が未生成のことがあるため、最大 MAX_RETRY 回リトライして
        画像が実際に利用可能になったタイミングでメッセージを編集して
        画像を追加する。

        単純な HTTPステータス200判定ではなく、レスポンスボディの内容
        （PNGマジックバイト・最小サイズ）を検証することで、CDNが
        「200は返すがまだ実体が生成し切っていない」状態を確実に
        検出し、Discord側のフェッチが壊れたリンクとして固定表示
        されてしまう不具合を防ぐ（詳細はモジュールdocstring参照）。

        P2P_IMAGE_ATTACH_ENABLED=False の場合、このメソッドは何もせず
        即座に return する。

        on_failure : 全リトライ失敗時に呼ばれるコールバック（省略可）。
            `await on_failure(message, url)` の形で1回だけ呼び出される。
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

        MAX_RETRY = 20   # 最大 20 回 × 約 6 秒 = 約 2 分
        INTERVAL  = 6    # 各試行前の待機秒数

        for attempt in range(MAX_RETRY):
            await asyncio.sleep(INTERVAL)
            try:
                async with self.session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        logger.debug(
                            f"P2P画像まだなし: HTTP {resp.status} "
                            f"attempt={attempt+1}/{MAX_RETRY}"
                        )
                        continue
                    body = await resp.read()

                if not self._is_valid_image_response(body):
                    logger.debug(
                        f"P2P画像: HTTP 200だが内容が不正（生成中の可能性）"
                        f" size={len(body)}bytes attempt={attempt+1}/{MAX_RETRY}"
                    )
                    continue

                # ここまで到達した場合、有効なPNG画像として確認済み
                try:
                    # message.embeds[0] はキャッシュ済みオブジェクトのため .copy() が必要
                    embed = message.embeds[0].copy()
                    embed.set_image(url=url)
                    await message.edit(embed=embed)
                    logger.info(
                        f"P2P画像を追加しました: id={image_id} "
                        f"(attempt={attempt+1}, size={len(body)}bytes)"
                    )
                except Exception as e:
                    logger.warning(f"P2P画像メッセージ編集失敗: {e}")
                return

            except Exception as e:
                logger.debug(f"P2P画像確認エラー: {e} attempt={attempt+1}")

        logger.info(f"P2P画像: {MAX_RETRY}回リトライ後も取得できませんでした id={image_id}")
        if on_failure:
            try:
                await on_failure(message, url)
            except Exception as e:
                logger.error(f"P2P画像取得失敗時のフォールバック処理でエラー: {e}")
