"""
core/gis_discord.py
====================
GIS地図描画機能（core/gis_render.py, core/gis_tile_render.py）が生成した
PNGバイト列を、Discordの複数埋め込み画像として送信する際の共通処理
（2026-09-18追加）。

1件の通知に複数のGIS地図画像（例: 震度3以上の観測点周辺の拡大表示＋
全観測点を含めた通常表示の2枚）を添付したいケースが出てきたため、
各Cogでの重複実装を避けるためにここへ切り出した。

【Discordの制約】
1つのEmbedにつき画像（set_image）は1枚までしか表示できないため、
複数枚の画像を1つのメッセージで見せるには、画像だけを持つ追加の
Embedをメッセージに複数含める必要がある（discord.py の
channel.send(embeds=[...]) はEmbedのリストを渡せる）。
"""
import io

import discord


def build_gis_message_kwargs(image_list: list, base_embed: discord.Embed) -> dict:
    """
    PNGバイト列のリスト（None/空バイト列は無視される）から、
    channel.send(**kwargs) にそのまま渡せる引数辞書を組み立てる。

    - 画像が1枚も無い場合: {"embed": base_embed}（画像添付なしの
      通常通知と同じ）
    - 画像が1枚の場合: base_embed に画像を設定し、{"embed": base_embed,
      "files": [...]}
    - 画像が2枚以上の場合: 1枚目は base_embed に設定し、2枚目以降は
      画像のみの追加Embedとして {"embeds": [...], "files": [...]} を返す
      （呼び出し側はこれを channel.send(**kwargs) するだけでよい）。

    base_embed は呼び出し元が組み立てた本体の埋め込み（タイトル・
    説明文等を設定済みのもの）を渡すこと。本関数が set_image() を
    呼び出すため、呼び出し側で先に set_image() している場合は上書き
    される点に注意。
    """
    images = [b for b in (image_list or []) if b]
    if not images:
        return {"embed": base_embed}

    files = []
    embeds = [base_embed]
    for i, img_bytes in enumerate(images):
        filename = f"gis_map_{i}.png"
        files.append(discord.File(io.BytesIO(img_bytes), filename=filename))
        if i == 0:
            base_embed.set_image(url=f"attachment://{filename}")
        else:
            extra_embed = discord.Embed()
            extra_embed.set_image(url=f"attachment://{filename}")
            embeds.append(extra_embed)

    if len(embeds) == 1:
        return {"embed": embeds[0], "files": files}
    return {"embeds": embeds, "files": files}
