import discord
from discord.ext import commands

from cogs.utils import is_maintainer
from cogs.utils.meta_cog import Cog


class Verification(Cog):
    @commands.group()
    @is_maintainer()
    async def disclaimer(self, ctx):
        """Manages the disclaimer for the verification channel."""
        pass

    @disclaimer.command()
    async def set(self, ctx, *, content):
        """Sets the disclaimer for this channel.
        Note: This will overwrite the old disclaimer, if present..
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
                await message.edit(content=content)
                return

        # Message not found.
        message = await config.verification_channel.send(content)
        await message.add_reaction("\N{SQUARED OK}")
        query = "UPDATE guild_config SET verification_message_id = $1 WHERE id = $2"
        await ctx.db.execute(query, message.id, guild_id)

    @Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if str(payload.emoji) != '\N{SQUARED OK}':
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        user = self.bot.get_user(payload.user_id)
        if user is None or user.bot:
            return

        event_cog = self.bot.get_cog("Event")
        config = event_cog and await event_cog.get_guild_config(payload.guild_id)
        if not config:
            return

        # Upgrade our user
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(user.id)
        await member.remove_roles(discord.Object(id=config.verification_role_id))


setup = Verification.setup

