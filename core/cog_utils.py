"""
core/cog_utils.py
==================
Cog分割後、あるCogから別のCogの状態を安全に参照するためのヘルパー。

【設計方針】
discord.py の bot.get_cog(name) は、該当Cogが未登録・起動失敗している場合
None を返す。SystemCog はほぼ全てのCogの状態を横断的に集約する必要があるが、
「あるCogだけがまだ起動していない/クラッシュしている」状況でも
!status コマンド自体は落ちてほしくない。

そのため、get_cog_attr() は
  1. 指定した名前の Cog を取得
  2. 存在すれば指定した属性を取得
  3. どちらかが失敗したら default を返す
という安全なアクセスを提供する。

【使用例】
    from core.cog_utils import get_cog_attr

    last_recv = get_cog_attr(bot, "QuakeEewCog", "_last_recv", default={})
    wolfx_hb  = get_cog_attr(bot, "QuakeEewCog", "_wolfx_last_heartbeat")
"""
from discord.ext import commands


def get_cog(bot: commands.Bot, cog_name: str):
    """指定した名前の Cog インスタンスを取得する。存在しなければ None。"""
    return bot.get_cog(cog_name)


def get_cog_attr(bot: commands.Bot, cog_name: str, attr_name: str, default=None):
    """
    指定した Cog の属性を安全に取得する。
    Cog が未登録、または属性が存在しない場合は default を返す。
    """
    cog = bot.get_cog(cog_name)
    if cog is None:
        return default
    return getattr(cog, attr_name, default)


def call_cog_method(bot: commands.Bot, cog_name: str, method_name: str, *args, default=None, **kwargs):
    """
    指定した Cog のメソッドを安全に呼び出す（同期メソッド用）。
    Cog が未登録、またはメソッドが存在しない場合は default を返す。
    """
    cog = bot.get_cog(cog_name)
    if cog is None:
        return default
    method = getattr(cog, method_name, None)
    if method is None:
        return default
    return method(*args, **kwargs)