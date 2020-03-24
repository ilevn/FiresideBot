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


Poll = namedtuple("Poll", "id guild_id channel_id is_strict")


class GuildPollConfig:
    __slots__ = ("bot", "polls")

    @classmethod
    async def from_record(cls, bot, records):
        self = cls()
        self.bot = bot

        self.polls = {channel_id: Poll(id_, guild_id, channel_id, is_strict) for id_, guild_id, channel_id, is_strict in
                      records}
        return self


class Polls(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        self.poll_emotes = ("\N{THUMBS UP SIGN}", "\N{THUMBS DOWN SIGN}", "\N{SHRUG}")

    @cache()
    async def get_guild_polls(self, guild_id):
        query = """SELECT * FROM polls WHERE guild_id = $1"""
        records = await self.bot.pool.fetch(query, guild_id)
        return records and await GuildPollConfig.from_record(self.bot, records)

    @Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.author.bot:
            return

        config = await self.get_guild_polls(message.guild.id)

        if not config:
            return

        poll = config.polls.get(message.channel.id)

        if not poll:
            return

        if poll.is_strict:
            if message.content.lower().startswith('poll:'):
                for emote in self.poll_emotes:
                    await message.add_reaction(emote)
            else:
                await message.channel.send('xsoihjfdus', delete_after=20)
                await message.delete(delay=18)
        else:
            if message.content.lower().startswith('poll:'):
                for emote in self.poll_emotes:
                    await message.add_reaction(emote)

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
        if status == 'INSERT 0':
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


setup = Polls.setup
