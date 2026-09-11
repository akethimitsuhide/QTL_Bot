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

【2026-08-19: 「震源に関する情報」のみ地図画像が表示されないバグの調査・修正】
実運用で、地震情報の中でも issue.type == "Destination"（震源に関する
情報）の通知だけ、地図画像が一貫して表示されない（20回リトライ後に
「取得できませんでした」ログが出て諦める）症状が確認された。

原因を調査したところ、旧実装の _is_valid_image_response() は
「PNGマジックバイトで始まっているか」と「MIN_VALID_IMAGE_BYTES
（1024バイト）以上あるか」の両方を必須としていた。しかし
「震源に関する情報」は観測された震度分布を持たない発表種別で、
地図上には震源位置のマーカー程度しか描画されない。そのため生成
される画像は単色領域が大半を占める非常にシンプルな内容になり、
PNG圧縮の効率が高く、結果として完全に生成し終えた正当な画像でも
1024バイトを下回るケースがあった。つまり「まだ生成中の未完成画像」
と「生成済みだが単に軽量な画像」を、サイズという間接的な指標だけで
区別しようとしたことが誤判定の原因だった。

対策として、「サイズが十分大きいか」という間接的な指標ではなく、
PNGファイル構造そのものの完全性を直接検証する方式に変更した。
PNGファイルは仕様上、必ず末尾が IEND チャンク
（\\x00\\x00\\x00\\x00IEND\\xaeB\\x60\\x82、12バイト固定）で終端する。
CDNがまだ書き込み中の未完成ファイルは、先頭のマジックバイトは
正常でもこの終端チャンクを欠いた状態になるため、「先頭マジック
バイト」と「末尾IENDチャンク」の両方を確認することで、ファイル
サイズの大小に関わらず「生成が完了した正当なPNGかどうか」を直接
判定できる。MIN_VALID_IMAGE_BYTESは、空レスポンス等の明らかに
異常なケースを弾く最低限の安全網としてごく小さい値に引き下げて残す。

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

from core.config import P2P_IMAGE_ATTACH_ENABLED, P2P_IMAGE_CDN_CONCURRENCY

logger = logging.getLogger("QTLBot")

# P2P地震情報CDN（cdn.p2pquake.net）への同時アクセス数を、
# QuakeInfoCog/TsunamiCog/EewCog/JishinKanchiCog等、P2PImageMixinを
# 使う全Cogを横断して制限する共有セマフォ。
#
# 【2026-08-23 追加の経緯】
# 大規模地震で短時間に複数のP2P地震情報レポート（震度速報→各地の
# 震度に関する情報等）が連続発表されると、それぞれが独立した
# _attach_p2p_image タスクとして並行実行され、同一CDNへ同時多発的に
# リクエストが飛ぶ。この自己誘発的な負荷集中がCDN側のレート制限や
# 過負荷を招き、「地図画像の埋め込みが軒並み失敗する」不具合の
# 実害（実際のログで5件中3件が20回リトライ後も失敗、残り2件も
# 15〜17回目でようやく成功）につながっていた。
#
# セマフォは「実際にCDNへHTTPリクエストを送っている瞬間」だけを
# 制限する設計とする（各タスクの6秒間隔でのsleep中はセマフォを
# 保持しない）。ポーリングのライフサイクル全体（最大2分）を1つの
# タスクが占有し続けると、後続のタスクが長時間ブロックされてしまう
# ため、実際の通信区間のみを絞ることで、CDN側の瞬間的な同時リクエスト
# 数を減らしつつ、各タスクの2分間のリトライ猶予自体は維持する。
_cdn_semaphore = asyncio.Semaphore(P2P_IMAGE_CDN_CONCURRENCY)

# PNGファイルのマジックバイト（シグネチャ、先頭8バイト固定）。
# CDNが返すレスポンスボディがこれで始まっていない場合、
# HTTPステータスが200であっても有効な画像とはみなさない。
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# PNGファイルのIENDチャンク（終端マーカー、末尾12バイト固定）。
# 長さフィールド(4バイト、常に0)+チャンク種別"IEND"(4バイト)+
# CRC(4バイト、IENDは中身が空のため常に同じ値)で構成される、
# 全てのPNGファイルが末尾に持つ固定バイト列。
# CDNがまだ書き込み中の未完成ファイルはこれを欠くため、
# 「生成完了した正当なPNGかどうか」の直接的な判定に使える
# （ファイルサイズという間接的な指標に頼らないための2026-08修正）。
_PNG_IEND_TRAILER = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"

# CDNが返す画像として妥当とみなす最小バイト数。
# 「震源に関する情報」等、内容がシンプルで正当に小さいPNGも
# 存在するため、この閾値はあくまで空レスポンス・極端に短い
# 応答を弾くための最低限の安全網とし、大きくは設定しない
# （2026-08: 主判定はPNG先頭・末尾の構造検証に変更したため、
# 以前の1024バイトから大幅に引き下げた）。
MIN_VALID_IMAGE_BYTES = 64


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

        - 最小バイト数（MIN_VALID_IMAGE_BYTES）以上あるか（空応答等の
          明らかな異常のみを弾く最低限の安全網）
        - 先頭がPNGマジックバイトで始まっているか
        - 末尾がPNGのIENDチャンクで終わっているか（＝生成完了した
          正当なPNGファイルとして完結しているか）

        の全てを満たす場合のみ True を返す。単純な「サイズが大きいか」
        だけでは、内容がシンプルで正当に軽量な画像（例: 震度分布を
        持たない「震源に関する情報」の地図画像）を誤って「まだ生成中」
        と判定してしまうため、末尾チャンクによる構造検証を主とする
        （詳細はモジュールdocstring参照）。
        """
        if len(body) < MIN_VALID_IMAGE_BYTES:
            return False
        if not body.startswith(_PNG_SIGNATURE):
            return False
        if not body.endswith(_PNG_IEND_TRAILER):
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

        # 【2026-09-09 追加】全リトライ失敗時、最終的な失敗理由が
        # DEBUGログ（既定運用ではファイルに出力されない）にしか
        #残らず、「そもそも画像が存在しないURL（恒常的な404等）なのか」
        # 「生成が遅いだけで200は返るが内容が不正な状態が続いているのか」
        # を通常運用のログ（INFO以上）から判別できなかった。
        # JishinKanchiCog（地震感知情報）で「地図画像が表示されない」
        # 事象の切り分け依頼があり、実機ログを確認したところ、疑わしい
        # 画像取得が3件とも20回リトライ後に失敗していたが、具体的な
        # 失敗理由（HTTPステータス等）が記録されておらず原因を特定
        # できなかった。そのため、最終試行の結果をこの変数に保持し、
        # 全リトライ失敗時のサマリーログ（INFO）に含めるようにした。
        last_failure_reason = "不明"

        for attempt in range(MAX_RETRY):
            await asyncio.sleep(INTERVAL)
            try:
                # CDNへの実リクエスト区間のみをセマフォで保護する
                # （sleep中は保持しない。理由はモジュール冒頭の
                # _cdn_semaphore のコメント参照）。
                async with _cdn_semaphore:
                    async with self.session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            last_failure_reason = f"HTTP {resp.status}"
                            logger.debug(
                                f"P2P画像まだなし: HTTP {resp.status} "
                                f"attempt={attempt+1}/{MAX_RETRY}"
                            )
                            continue
                        body = await resp.read()

                if not self._is_valid_image_response(body):
                    last_failure_reason = f"HTTP 200だが内容が不正（size={len(body)}bytes）"
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
                last_failure_reason = f"{type(e).__name__}: {e}"
                logger.debug(f"P2P画像確認エラー: {e} attempt={attempt+1}")

        logger.info(
            f"P2P画像: {MAX_RETRY}回リトライ後も取得できませんでした "
            f"id={image_id} url={url} (最終試行の結果: {last_failure_reason})"
        )
        if on_failure:
            try:
                await on_failure(message, url)
            except Exception as e:
                logger.error(f"P2P画像取得失敗時のフォールバック処理でエラー: {e}")
