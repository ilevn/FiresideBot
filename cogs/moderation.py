import discord
from discord.ext import commands

from cogs.utils import is_mod, human_timedelta, time
from cogs.utils.meta_cog import Cog


class Moderation(Cog):
    """Cog for moderation actions."""

    @commands.command(aliases=['newmembers', "new"])
    @is_mod()
    async def newusers(self, ctx, *, count: int = 5):
        """List the newest members of the server.
        This is useful to check if any suspicious members have joined.
        The count parameter can only be up to 25.
        """
        count = max(min(int(count), 25), 5)
        members = sorted(ctx.message.guild.members, key=lambda m: m.joined_at, reverse=True)[:count]
        embed = discord.Embed(title='New Members', colour=discord.Colour.green())

        for member in members:
            body = f'joined {human_timedelta(member.joined_at)}, created {human_timedelta(member.created_at)}'
            embed.add_field(name=f'{member} (ID: {member.id})', value=body, inline=False)

        await ctx.send(embed=embed)

    @commands.command()
    @is_mod()
    async def block(self, ctx, *, member: discord.Member):
        """Blocks a user from a channel."""

        reason = f"Block by {ctx.author} (ID: {ctx.author.id})"

        try:
            await ctx.channel.set_permissions(member, send_messages=False, add_reactions=False, reason=reason)
        except:
            await ctx.send("\N{THUMBS DOWN SIGN}")
        else:
            await ctx.send("\N{THUMBS UP SIGN}")

    @commands.command()
    @is_mod()
    async def tempblock(self, ctx, duration: time.FutureTime, *, member: discord.Member):
        """Temporarily blocks a user from a channel.
        The duration can be a a short time form, e.g. 30d or a more human
        duration such as "until thursday at 3PM" or a more concrete time
        such as "2020-12-31" or "21/03/2020".
        Note that times are in UTC.
        """

        reminder = self.bot.get_cog("Reminder")
        if reminder is None:
            return await ctx.send("Sorry, this functionality is currently unavailable. Try again later?")

        timer = await reminder.create_timer(duration.dt, "tempblock", ctx.guild.id, ctx.author.id,
                                            ctx.channel.id, member.id,
                                            connection=ctx.db,
                                            created=ctx.message.created_at)

        reason = f"Tempblock by {ctx.author} (ID: {ctx.author.id}) until {duration.dt}"

        try:
            await ctx.set_permissions(member, send_messages=False, add_reactions=False, reason=reason)
        except:
            await ctx.send("\N{THUMBS DOWN SIGN}")
        else:
            await ctx.send(f"Blocked {member} for {time.human_timedelta(duration.dt, source=timer.created_at)}.")

    @commands.Cog.listener()
    async def on_tempblock_timer_complete(self, timer):
        guild_id, mod_id, channel_id, member_id = timer.args

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            return

        to_unblock = guild.get_member(member_id)
        if to_unblock is None:
            return

        moderator = guild.get_member(mod_id)
        if moderator is None:
            try:
                moderator = await self.bot.fetch_user(mod_id)
            except:
                # Request failed somehow.
                moderator = f"Mod ID {mod_id}"
            else:
                moderator = f"{moderator} (ID: {mod_id})"
        else:
            moderator = f"{moderator} (ID: {mod_id})"

        reason = f"Automatic unblock from timer made on {timer.created_at} by {moderator}."

        try:
            await channel.set_permissions(to_unblock, send_messages=None, add_reactions=None, reason=reason)
        except:
            pass


setup = Moderation.setup
