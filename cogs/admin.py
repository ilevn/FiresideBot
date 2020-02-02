import time
import traceback

from discord.ext import commands
from discord.ext.commands import is_owner

from cogs.utils.meta_cog import Cog


class Admin(Cog):
    @commands.command()
    @is_owner()
    async def load(self, ctx, *, extension):
        """Load an extension"""
        try:
            ctx.bot.load_extension(f"cogs.{extension}")
        except Exception as e:
            traceback.print_exc()
            await ctx.send(f"Could not load `{extension}` -> `{e}`")
        else:
            await ctx.send("\N{OK HAND SIGN}")

    @commands.command()
    @is_owner()
    async def unload(self, ctx, extension):
        """Unload an extension."""
        try:
            ctx.bot.unload_extension(f"cogs.{extension}")
        except commands.ExtensionError as e:
            await ctx.send(f"{e.__class__.__name__}: {e}")
        else:
            await ctx.send("\N{OK HAND SIGN}")

    @commands.command()
    @is_owner()
    async def reload(self, ctx, *, extension):
        """Reload an extension. """
        try:
            ctx.bot.reload_extension(extension)
        except commands.ExtensionError as e:
            await ctx.send(f"{e.__class__.__name__}: {e}")
        else:
            await ctx.send("\N{OK HAND SIGN}")


setup = Admin.setup
