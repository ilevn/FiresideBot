from collections import namedtuple
from typing import Optional

import discord
from discord.ext import commands

from cogs.utils import db, is_mod
from cogs.utils.cache import cache
from cogs.utils.meta_cog import Cog


class PollConfig(db.Table, table_name="polls"):
    id = db.PrimaryKeyColumn()
    # The guild this poll belongs to.
    guild_id = db.DiscordIDColumn()
    # Channel id of the poll.
    channel_id = db.DiscordIDColumn(unique=True)
    # Whether the poll should be strictly moderated, true by default.
    is_strict = db.Column(db.Boolean, default=True)


Poll = namedtuple("Poll", "channel_id is_strict")


def to_emoji(c):
    return chr(0x1f1e5 + c)


class Polls(Cog):
    """Poll voting system."""

    def __init__(self, bot):
        super().__init__(bot)
        self.poll_emotes = ("\N{THUMBS UP SIGN}", "\N{THUMBS DOWN SIGN}", "\N{SHRUG}")

    @cache()
    async def get_guild_polls(self, guild_id):
        query = """SELECT channel_id, is_strict FROM polls WHERE guild_id = $1"""
        records = await self.bot.pool.fetch(query, guild_id)
        return records and {channel_id: Poll(channel_id, is_strict) for channel_id, is_strict in records}

    @Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.author.bot:
            return

        polls = await self.get_guild_polls(message.guild.id)
        if not polls:
            return

        poll = polls.get(message.channel.id)
        if not poll:
            return

        if message.content.lower().startswith("poll: "):
            await message.delete()
            # Make a new poll
            embed = discord.Embed(title="Poll")
            embed.set_author(name=message.author.name, icon_url=message.author.avatar_url)
            embed.colour = discord.Colour.blurple()
            content = message.clean_content
            embed.description = content[6:]
            new_message = await message.channel.send(embed=embed)

            for emote in self.poll_emotes:
                await new_message.add_reaction(emote)
            return

        if poll.is_strict:
            fmt = 'Wrong poll format. Please type "Poll: poll message here"'
            await message.channel.send(fmt, delete_after=14)
            await message.delete(delay=12)

    @commands.group(name="polls")
    @is_mod()
    async def _polls(self, ctx):
        """Handles poll channel configuration for the server."""
        pass

    @_polls.command(name="add")
    async def _polls_add(self, ctx, is_strict: Optional[bool] = False, *, channel: discord.TextChannel):
        """Adds a poll channel which is either strict or non strict."""
        query = """INSERT INTO polls (guild_id, channel_id, is_strict) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING"""
        status = await ctx.db.execute(query, ctx.guild.id, channel.id, is_strict)
        if status[-1] == '0':
            await ctx.send('\N{CROSS MARK} This channel is already a configured poll channel! >:(')
            return

        await ctx.send('\N{WHITE HEAVY CHECK MARK} Success :) Poll entry added')
        self.get_guild_polls.invalidate(self, ctx.guild.id)

    @_polls.command(name="remove")
    async def _polls_remove(self, ctx, *, channel: discord.TextChannel):
        """Removes a poll channel."""
        query = """DELETE FROM polls WHERE guild_id = $1 AND channel_id = $2"""
        await ctx.db.execute(query, ctx.guild.id, channel.id)
        await ctx.send('\N{OK HAND SIGN}')
        self.get_guild_polls.invalidate(self, ctx.guild.id)

    @commands.command()
    @is_mod()
    async def quickpoll(self, ctx, *questions_and_choices: str):
        """
        Makes a poll quickly.
        The first argument is the question and the rest
        are the choices.
        """

        if len(questions_and_choices) < 2:
            return await ctx.send('Need at least 1 question with 1 choice.')
        elif len(questions_and_choices) > 21:
            return await ctx.send('You can only have up to 20 choices.')

        perms = ctx.channel.permissions_for(ctx.guild.me)
        if not (perms.read_message_history or perms.add_reactions):
            return await ctx.send('Need Read Message History and Add Reactions permissions.')

        question = questions_and_choices[0]
        choices = [(to_emoji(e), v) for e, v in enumerate(questions_and_choices[1:], 1)]

        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

        fmt = '\n'.join(f'{key}: {question}' for key, question in choices)
        poll = await ctx.send(f'{ctx.author} asks: {question}\n\n{fmt}')
        for emoji, _ in choices:
            await poll.add_reaction(emoji)


setup = Polls.setup
