"""
core/p2p_image.py
==================
P2P地震情報 API の地図画像URL生成を提供するモジュール。

【設計方針: なぜ独立モジュールなのか】
notify_quake（地震情報）と notify_tsunami（P2P津波情報）の両方が
同じ画像URL生成ロジックを必要とする。Cog分割前は同じクラス内の
メソッドとして共有できていたが、cogs/quake.py と cogs/tsunami.py に
分かれた今、このロジックを両方に複製すると保守性が落ちる
（CDN側のURL形式が変わったときに2箇所を直す必要が出る）。

そのため core/audio.py の AudioMixin と同じパターンで
P2PImageMixin を提供し、両方のCogがこれを多重継承する。

    class QuakeInfoCog(commands.Cog, AudioClientMixin, P2PImageMixin):
        ...

【2026-08-02: CDNリトライ埋め込み方式の廃止】
このモジュールには以前、_attach_p2p_image() という「通知メッセージ送信後、
CDNへの画像反映を最大2分間ポーリングし、反映され次第 message.edit() で
embedに画像を追加する」処理が実装されていた。しかし実運用で以下の不具合が
繰り返し発生した:

  Bot側がCDNへの疎通確認（HTTP 200）に成功した時点（早ければ attempt=1、
  つまり最短5秒後）で embed に画像URLをセットし直しても、その直後に
  Discord側が実際にその画像をフェッチした瞬間には、CDN側で画像の実体
  （バイナリ）がまだ生成し切っていないことがあった。この場合、Discordは
  「取得失敗」を内部的にキャッシュしてしまい、以降そのメッセージの画像は
  ずっと壊れたリンクのまま表示される。Bot側のリトライ回数や待機秒数を
  増やしても、Discord側のフェッチタイミングそのものは制御できないため、
  この問題を確実に解消することはできなかった。

  根本原因が「CDN反映を待って埋め込む」という設計自体にあると判断し、
  CDNの反映状況をポーリングする処理を完全に廃止した。代わりに、通知本文
  （embed.description）に画像URLをテキストとしてそのまま含める方式に
  統一した。Discordはメッセージ送信時にリンクプレビューとして自動で
  画像を展開する機能を持っており、この展開はDiscord自身のタイミングで
  行われる（必要なら再フェッチも行われる）ため、CDN側の生成タイミングに
  Bot側が依存する必要がなくなる。

【使い方】
    quake_id = data.get("id")
    image_url = self.build_p2p_image_url_text(quake_id) if quake_id else ""
    if image_url:
        description += f"\\n\\n{image_url}"
    embed.description = description
    await channel.send(embed=embed)

【bot.py からの移行元】
元 bot.py の p2p_image_url() 定義
（Cog分割前の旧ファイルで cogs/quake.py に一時的に複製されていたものを統合）。
"""
import logging

logger = logging.getLogger("QTLBot")


class P2PImageMixin:
    """
    P2P地震情報の地図画像URLを生成するためのメソッド群を提供する Mixin。
    """

    @staticmethod
    def p2p_image_url(image_id: str) -> str | None:
        if not image_id:
            return None
        return f"https://cdn.p2pquake.net/app/images/{image_id}_trim_big.png"

    def build_p2p_image_url_text(self, image_id: str) -> str:
        """
        通知本文（description）へ追記するための、画像URLのみのプレーン
        テキストを返す。Discordはメッセージ本文中の画像URL（拡張子が
        画像形式のリンク）を自動的にプレビュー展開するため、Bot側で
        CDNの反映を待ってembedを編集するリトライ処理を行わなくても
        同等の見た目を得られる。image_id が空、またはURLを生成できない
        場合は空文字列を返す。
        """
        url = self.p2p_image_url(image_id)
        return url or ""
