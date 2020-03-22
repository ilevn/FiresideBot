import time
import unicodedata

from discord.ext import commands

from cogs.utils.meta_cog import Cog
from cogs.utils.paginators import PaginatedHelpCommand


class Meta(Cog):
    """Commands for functions related to Discord or the bot itself."""

    def __init__(self, bot):
        super().__init__(bot)
        self.old_help_command = bot.help_command
        bot.help_command = PaginatedHelpCommand()
        bot.help_command.cog = self

    def cog_unload(self):
        self.bot.help_command = self.old_help_command

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False

        return True

    @commands.command()
    async def charinfo(self, ctx, *, characters: str):
        """Shows information about a number of characters."""

        def to_string(c):
            digit = f'{ord(c):x}'
            name = unicodedata.name(c, 'Name not found.')
            return f'`\\U{digit:>08}`: {name} - {c} \N{EM DASH} <http://www.fileformat.info/info/unicode/char/{digit}>'

        msg = '\n'.join(map(to_string, characters))
        if len(msg) > 2000:
            return await ctx.send('Output too long to display.')
        await ctx.send(msg)

    @commands.command()
    async def ping(self, ctx):
        before = time.monotonic()
        msg = await ctx.send(":ping_pong: Pong!")
        after = time.monotonic()
        await msg.edit(content=f":ping_pong: Pong! | {round((after - before) * 1000, 2)}ms")


setup = Meta.setup
