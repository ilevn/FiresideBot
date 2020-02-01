import time

from discord.ext import commands
from discord.ext.commands import is_owner

from cogs.utils.meta_cog import Cog


class Admin(Cog):
    @commands.command()
    async def ping(self, ctx):
        before = time.monotonic()
        msg = await ctx.send(":ping_pong: Pong!")
        after = time.monotonic()
        await msg.edit(content=f":ping_pong: Pong! | {round((after - before) * 1000, 2)}ms")

    @commands.command()
    @is_owner()
    async def load(self, ctx, *, extension):
        ctx.bot.load_extension(f"cogs.{extension}")
        await ctx.send(f"{extension} loaded")

    @commands.command()
    @is_owner()
    async def unload(self, ctx, extension):
        ctx.bot.unload_extension(f"cogs.{extension}")
        await ctx.send(f"{extension} unloaded")

    @commands.command()
    @is_owner()
    async def reload(self, ctx, *, extension):
        try:
            ctx.bot.reload_extension(extension)
        except commands.ExtensionError as e:
            await ctx.send(f"{e.__class__.__name__}: {e}")
        else:
            await ctx.send("\N{OK HAND SIGN}")


setup = Admin.setup
