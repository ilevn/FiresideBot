from collections import defaultdict

import discord
from discord.ext import commands

from cogs.polls import to_emoji
from cogs.utils import is_maintainer, db, cache, is_mod
from cogs.utils.meta_cog import Cog


class ReactionTable(db.Table, table_name="reaction_table"):
    # Guilds are only supposed to have one reaction channel for now.
    guild_id = db.DiscordIDColumn(nullable=False)
    role_id = db.DiscordIDColumn(unique=True)
    emote = db.Column(db.String, nullable=False)


class Verification(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        # guild -> dict(emoji, role_id)
        self.mappings = defaultdict(dict)

    @cache.cache()
    async def get_role_emote_mapping(self, guild_id):
        query = "SELECT emote, role_id FROM reaction_table WHERE guild_id = $1"
        async with self.bot.pool.acquire() as conn:
            records = await conn.fetch(query, guild_id)

            return records and {e: r_id for (e, r_id) in records}

    @commands.group()
    @is_maintainer()
    async def disclaimer(self, ctx):
        """Manages the disclaimer for the verification channel."""
        pass

    @disclaimer.command()
    async def set(self, ctx, *, content):
        """Sets the disclaimer for this channel.
        Note: This will overwrite the old disclaimer, if present.
        """

        guild_id = ctx.guild.id
        event_cog = ctx.bot.get_cog("Event")
        config = event_cog and await event_cog.get_guild_config(guild_id)
        if not config:
            await ctx.send("Could not find the guild config for this server.")
            return

        # Check if an old disclaimer exists.
        old_id = await ctx.db.fetchval("SELECT verification_message_id FROM guild_config WHERE id = $1", guild_id)
        if old_id:
            message = await config.verification_channel.fetch_message(old_id)
            if message:
                try:
                    await message.edit(content=content)
                except discord.Forbidden:
                    await ctx.send("Sorry I cannot edit this message anymore. Delete the post and remake it.")
                    query = "UPDATE guild_config SET verification_message_id = NULL WHERE id = $1"
                    await ctx.db.execute(query, guild_id)
                else:
                    await ctx.send("\N{THUMBS UP SIGN}")
                    return

        # Message not found.
        message = await config.verification_channel.send(content)
        await message.add_reaction("\N{SQUARED OK}")
        query = "UPDATE guild_config SET verification_message_id = $1 WHERE id = $2"
        await ctx.db.execute(query, message.id, guild_id)
        # Also invalidate our config.
        event_cog.get_guild_config.invalidate(event_cog, guild_id)
        await ctx.send("\N{THUMBS UP SIGN}")

    @Cog.listener()
    async def on_raw_reaction_add(self, payload):
        event_cog = self.bot.get_cog("Event")
        guild_id = payload.guild_id
        config = event_cog and await event_cog.get_guild_config(guild_id)
        if not config:
            return

        if config.verification_channel.id != payload.channel_id:
            return

        mappings = await self.get_role_emote_mapping(guild_id)
        if not mappings:
            return

        try:
            role_id = mappings[str(payload.emoji)]
        except KeyError:
            return

        guild = self.bot.get_guild(guild_id)
        if (role := guild and guild.get_role(role_id)) is None:
            return

        user = self.bot.get_user(payload.user_id)
        if user is None or user.bot:
            return

        # Upgrade our user
        member = guild.get_member(user.id)
        await member.edit(roles=[role])
        # Use internal method to get around having to fetch the message object.
        await self.bot.http.remove_reaction(payload.channel_id, payload.message_id, payload.emoji, user.id)

    @commands.command()
    @is_mod()
    async def reaction_roles(self, ctx, *roles: discord.Role):
        """Allows you to set the reaction roles for this server.
        Up to 20 roles are allowed."""

        guild_id = ctx.guild.id
        event_cog = ctx.bot.get_cog("Event")
        config = event_cog and await event_cog.get_guild_config(guild_id)
        if not config:
            await ctx.send("Could not find the guild config for this server.")
            return

        # Check if this server already has reaction roles set up.
        has_roles = await ctx.db.fetchval("SELECT 1 FROM reaction_table WHERE guild_id = $1", guild_id)
        if has_roles:
            should_delete = await ctx.prompt("It looks like this server already has reaction roles. "
                                             "Should they be deleted?")
            if should_delete:
                await ctx.db.execute("DELETE FROM reaction_table WHERE guild_id = $1", guild_id)
                await ctx.send("Deleted! Please make sure to also delete the old reaction message later.")

        react_roles = [(to_emoji(i), role) for i, role in enumerate(roles, 1)]
        fmt = "\n".join(f"{key}: {role.name}" for key, role in react_roles)
        message = await config.verification_channel.send(fmt)

        for emoji, _ in react_roles:
            await message.add_reaction(emoji)

        to_insert = ((e, r.id, guild_id) for (e, r) in react_roles)
        await ctx.send(":ok_hand:")
        await ctx.db.copy_records_to_table("reaction_table", columns=("emote", "role_id", "guild_id"),
                                           records=to_insert)


setup = Verification.setup
