import textwrap

import discord
from discord.ext import commands
from discord.ext.commands import clean_content

from cogs.utils import db, is_mod
from cogs.utils.meta_cog import Cog


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
    async def _quote_add(self, ctx, member: discord.Member, *, quote: clean_content):
        """Adds a quote for a specific user in this guild."""
        query = """INSERT INTO quotes (guild_id, user_id, quote) VALUES ($1, $2, $3)"""
        await ctx.db.execute(query, ctx.guild.id, member.id, quote)

        await ctx.send('\N{WHITE HEAVY CHECK MARK} Success :) Quote entry added')

    @_quote.command(name="random")
    async def _quote_random(self, ctx, *, member: discord.Member):
        """Displays a random quote for a member."""
        discord_char_limit = 2000
        query = """SELECT quote FROM quotes WHERE guild_id = $1 AND user_id = $2 ORDER BY random() limit 1"""
        quote = await ctx.db.fetchval(query, ctx.guild.id, member.id)

        if not quote:
            return

        display_quote = textwrap.shorten(f"{member.name} \N{PUBLIC ADDRESS LOUDSPEAKER} {quote}", discord_char_limit)

        await ctx.send(display_quote)


setup = Quotes.setup
