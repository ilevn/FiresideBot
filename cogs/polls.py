from collections import namedtuple
from typing import Optional

import discord
from discord.ext import commands

from cogs.utils import db, is_mod
from cogs.utils.cache import cache
from cogs.utils.converters import entry_id
from cogs.utils.meta_cog import Cog


class PollConfig(db.Table, table_name="polls"):
    id = db.PrimaryKeyColumn()
    # The guild this poll belongs to.
    guild_id = db.DiscordIDColumn()
    # Channel id of the poll.
    channel_id = db.DiscordIDColumn(unique=True)
    # Whether the poll should be strictly moderated, true by default.
    is_strict = db.Column(db.Boolean, default=True)


class PollEntry(db.Table, table_name="poll_entries"):
    id = db.PrimaryKeyColumn()
    # The message ID of the entry.
    message_id = db.DiscordIDColumn()
    # The author of the entry.
    author_id = db.DiscordIDColumn(index=True)
    # The channel this entry was created in.
    channel_id = db.DiscordIDColumn(nullable=False)
    # The guild this poll entry was created in.
    guild_id = db.DiscordIDColumn(nullable=False)


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

    async def create_poll(self, message):
        author = message.author

        embed = discord.Embed(colour=discord.Colour.blurple())
        embed.set_author(name=author.name, icon_url=author.avatar_url)
        content = message.clean_content[6:]
        # Check if a mod mentioned someone.
        mentions = None
        if author.guild_permissions.manage_guild and message.role_mentions:
            mentions = ", ".join(map(lambda r: r.mention, message.role_mentions))

        # Add entry to DB.
        query = "INSERT INTO poll_entries (author_id, channel_id, guild_id) VALUES ($1, $2, $3) RETURNING id"
        entry_id = await self.bot.pool.fetchval(query, author.id, message.channel.id, message.guild.id)

        embed.description = f"__**Poll:**__ {content}"
        embed.set_footer(text=f'Entry ID {entry_id} | Edit this poll with "poll edit {entry_id} <new content>"')

        new_message = await message.channel.send(content=mentions, embed=embed)
        for emote in self.poll_emotes:
            await new_message.add_reaction(emote)

        await self.bot.pool.execute("UPDATE poll_entries SET message_id = $1 WHERE id = $2",
                                    new_message.id, entry_id)

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
            await self.create_poll(message)
            return

        if poll.is_strict:
            fmt = 'Wrong poll format. Please type "Poll: poll message here"'
            await message.channel.send(fmt, delete_after=14)
            await message.delete(delay=12)

    @commands.group(name="polls", aliases=["poll"])
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

    @_polls.command(name="edit")
    async def _polls_edit(self, ctx, id: entry_id, *, new_content: commands.clean_content):
        """Edits a poll entry that you own."""
        query = "SELECT author_id, message_id, channel_id FROM poll_entries WHERE id = $1 AND guild_id = $2"
        entry = await ctx.db.fetchrow(query, id, ctx.guild.id)
        if entry is None:
            return await ctx.send("Could not find a poll entry with that ID.")

        if entry[0] != ctx.author.id:
            return await ctx.send("This poll entry does not belong to you.")

        # Retrieve the channel.
        channel = ctx.guild.get_channel(entry["channel_id"])
        if not channel:
            return await ctx.send("Looks like the channel for this poll entry was deleted.")

        message = await channel.fetch_message(entry["message_id"])
        if not message:
            return await ctx.send("Looks like the message for this poll entry was deleted.")

        embed = message.embeds[0]
        embed.description = new_content
        await message.edit(embed=embed)
        await ctx.send("Successfully edited poll entry.")

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
