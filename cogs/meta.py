import io
import time
import unicodedata
from typing import Union

import discord
from discord.ext import commands

from cogs.utils import mod_cooldown
from cogs.utils.converters import FetchedUser
from cogs.utils.meta_cog import Cog
from cogs.utils.paginators import PaginatedHelpCommand
from cogs.utils.paginators.urban_pages import UrbanDictionaryPages


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
        return bool(ctx.guild)

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
    @mod_cooldown(1, 10 * 60, commands.BucketType.member)
    async def ping(self, ctx):
        """Pings the bot."""

        before = time.monotonic()
        msg = await ctx.send(":ping_pong: Pong!")
        after = time.monotonic()
        await msg.edit(content=f":ping_pong: Pong! | {round((after - before) * 1000, 2)}ms")

    @commands.command(name="urban", aliases=["whatis"])
    async def _urban(self, ctx, *, word):
        """Searches urban dictionary."""

        url = "https://api.urbandictionary.com/v0/define"
        async with ctx.session.get(url, params={'term': word}) as resp:
            if resp.status != 200:
                return await ctx.send(f'An error occurred: {resp.status} {resp.reason}')

            js = await resp.json()
            data = js.get('list')
            if not data:
                return await ctx.send('No results found, sorry.')

        try:
            pages = UrbanDictionaryPages(ctx, data)
            await pages.paginate()
        except Exception as e:
            await ctx.send(e)

    @commands.command()
    async def dogfacts(self, ctx):
        """Gives you a random dog fact."""
        async with ctx.session.get("https://dog-api.kinduff.com/api/facts") as resp:
            if resp.status != 200:
                return await ctx.send("No dog facts found :(")

            js = await resp.json()
            await ctx.send(f"\N{DOG FACE} **Random dog fact:**\n{js['facts'][0]}")

    @commands.command()
    async def dog(self, ctx):
        """Gives you a random dog."""
        async with ctx.session.get("https://random.dog/woof") as resp:
            if resp.status != 200:
                return await ctx.send("No dog found :(")

            filename = await resp.text()
            url = f"https://random.dog/{filename}"
            filesize = ctx.guild.filesize_limit if ctx.guild else 8388608
            if filename.endswith((".mp4", ".webm")):
                async with ctx.typing():
                    async with ctx.session.get(url) as other:
                        if other.status != 200:
                            return await ctx.send("Could not download dog video :(")

                        if int(other.headers["Content-Length"]) >= filesize:
                            return await ctx.send(f"Video was too big to upload... Watch it here: {url} instead.")

                        fp = io.BytesIO(await other.read())
                        await ctx.send(file=discord.File(fp, filename=filename))
            else:
                await ctx.send(embed=discord.Embed(title='Random Dog').set_image(url=url))

    @commands.command(hidden=True)
    async def cat(self, ctx):
        """Gives you a random cat."""
        async with ctx.session.get("https://api.thecatapi.com/v1/images/search") as resp:
            if resp.status != 200:
                return await ctx.send('No cat found :(')
            js = await resp.json()
            await ctx.send(embed=discord.Embed(title="Random Cat").set_image(url=js[0]["url"]))

    @commands.command(aliases=["avatar"])
    async def avy(self, ctx, *, user: Union[discord.Member, FetchedUser] = None):
        """Shows the avatar of a user.
        This displays your avatar by default."""
        embed = discord.Embed()
        user = user or ctx.author
        avatar = user.avatar_url_as(static_format='png')
        embed.set_author(name=str(user), url=avatar)
        embed.set_image(url=avatar)
        await ctx.send(embed=embed)

    @avy.error
    async def avy_error(self, ctx, error):
        if isinstance(error, commands.BadUnionArgument):
            await ctx.send("Couldn't find that user...")


setup = Meta.setup
