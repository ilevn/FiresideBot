import textwrap
from typing import Union

import discord
from discord.ext import commands
from discord.ext.commands import clean_content

from cogs.utils import db, is_mod
from cogs.utils.converters import FetchedUser
from cogs.utils.meta_cog import Cog
from cogs.utils.paginators import FieldPages, CannotPaginate


class QuoteConfig(db.Table, table_name="quotes"):
    id = db.PrimaryKeyColumn()
    # The guild this quote belongs to.
    guild_id = db.DiscordIDColumn()
    # The keyword/user who said the quote.
    user_id = db.DiscordIDColumn()
    # The quote itself.
    quote = db.Column(db.String)


class Quotes(Cog):
    @commands.group(name="quote")
    async def _quote(self, ctx):
        """Allows for the creation and viewing of quotes."""
        pass

    @_quote.command(name="add")
    @is_mod()
    async def _quote_add(self, ctx, member: Union[discord.Member, FetchedUser], *, quote: clean_content):
        """Adds a quote for a specific user in this guild."""
        query = """INSERT INTO quotes (guild_id, user_id, quote) VALUES ($1, $2, $3)"""
        await ctx.db.execute(query, ctx.guild.id, member.id, quote)
        await ctx.send('\N{WHITE HEAVY CHECK MARK} Success :) Quote entry added')

    @_quote.command(name="random")
    async def _quote_random(self, ctx, *, member: Union[discord.Member, FetchedUser]):
        """Displays a random quote for a member."""
        query = """SELECT quote FROM quotes WHERE guild_id = $1 AND user_id = $2 ORDER BY random() LIMIT 1"""
        quote = await ctx.db.fetchval(query, ctx.guild.id, member.id)
        if not quote:
            return

        member_name = getattr(member, "display_name", str(member))
        embed = discord.Embed(title=f"Here's a quote from {member_name}", colour=discord.Colour.light_grey())
        embed.description = textwrap.shorten(f"\N{PUBLIC ADDRESS LOUDSPEAKER}\n{quote}", width=2000)
        await ctx.send(embed=embed)

    @_quote.command(name="all")
    @commands.cooldown(1, 30.0, type=commands.BucketType.member)
    async def quotes_all(self, ctx, *, member: discord.Member = None):
        """Gets all quotes of a server member or the whole guild, if
        no member is specified.
        """
        guild = ctx.guild
        if not member:
            subcheck = ""
            args = (guild.id,)
        else:
            subcheck = "AND user_id = $2 ORDER BY user_id"
            args = (guild.id, member.id)

        query = f"SELECT quote, user_id FROM quotes WHERE guild_id = $1 {subcheck}"
        records = await ctx.db.fetch(query, *args)

        if not records:
            await ctx.send("Could not find any quotes for this server...")
            return

        def get_member():
            mem = guild.get_member(user_id)
            return getattr(mem, "display_name", f"ID ({user_id})")

        entries = []
        for content, user_id in records:
            header = get_member() if not member else "Content"
            entries.append((header, content[:1048]))

        pages = FieldPages(ctx, entries=entries)
        try:
            await pages.paginate()
        except CannotPaginate as e:
            await ctx.send(e)


setup = Quotes.setup
