import traceback

from discord.ext import commands

from cogs.utils import checks
from cogs.utils.meta_cog import Cog


class Admin(Cog):
    async def cog_check(self, ctx):
        return await checks.maintainer_check(ctx)

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            await ctx.send(":x: You're not a maintainer")

    @commands.command()
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
    async def unload(self, ctx, extension):
        """Unload an extension."""
        try:
            ctx.bot.unload_extension(f"cogs.{extension}")
        except commands.ExtensionError as e:
            await ctx.send(f"{e.__class__.__name__}: {e}")
        else:
            await ctx.send("\N{OK HAND SIGN}")

    @commands.command()
    async def reload(self, ctx, *, extension):
        """Reload an extension. """
        try:
            ctx.bot.reload_extension(extension)
        except commands.ExtensionError as e:
            await ctx.send(f"{e.__class__.__name__}: {e}")
        else:
            await ctx.send("\N{OK HAND SIGN}")

    @commands.command()
    async def test_sentry(self, ctx):
        return 0 / 0


setup = Admin.setup
