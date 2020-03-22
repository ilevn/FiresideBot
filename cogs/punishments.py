from typing import Optional

import discord
from discord.ext import commands

from cogs.events import EventConfig
from cogs.reminders import Timer
from cogs.utils import FutureTime, human_timedelta, is_mod
from cogs.utils.meta_cog import Cog
from cogs.utils.punishment import Punishment, ActionType


async def attempt_notification(member: discord.Member, text):
    try:
        await member.send(text)
    except discord.HTTPException:
        # Member has DMs off.
        raise RuntimeError(f"Could not contact {member}.")


class Punishments(Cog):
    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False

        return True

    async def get_config(self, sendable) -> Optional[EventConfig]:
        if (events := self.bot.get_cog("Event")) is None:
            await sendable.send("Sorry, the event cog is currently not loaded...")
            return

        config = await events.get_guild_config(sendable.guild.id)
        if config:
            return config
        else:
            await sendable.send("Could not find config for some reason.")

    async def punish_and_get_channel(self, ctx, duration: FutureTime, reason, member: discord.Member, type_):
        if ctx.message.channel.permissions_for(member).view_audit_log:
            # Moderator or admin. Let's avoid flashbacks of the great purge.
            await ctx.send("\U0000274c I cannot punish a moderator or admin!")
            return

        if (reminder := self.bot.get_cog("Reminder")) is None:
            await ctx.send("Sorry, this is currently unavailable. Try again later?")
            return

        config = await self.get_config(ctx)
        if not config:
            return

        role_id = config.shitpost_role_id if type_ == "shitpost" else config.jailed_role_id

        # Check if they're already punished.
        # The bot doesn't differentiate between a shitpost and a jailed punishment
        # because that would lead to undefined behaviour (e.g role state clash, double punishment).
        query = "SELECT 1 FROM reminders WHERE event = 'punish' AND extra #>> '{args, 2}' = $1"
        record = await ctx.db.fetchrow(query, str(member.id))

        if record:
            await ctx.send(f"{member} is already punished.")
            return

        # Save their role-state.
        managed_roles = set(r.id for r in member.roles if r.managed)
        roles = list(set(r.id for r in member.roles) - managed_roles)

        await reminder.create_timer(duration.dt, 'punish', ctx.guild.id, ctx.author.id,
                                    member.id, roles, type_, connection=ctx.db)
        duration_delta = human_timedelta(duration.dt)

        # This is a work-around for discord's awful "nitrobooster" feature.
        still_apply = managed_roles.union({role_id})
        try:
            await member.edit(roles=[discord.Object(id=id_) for id_ in still_apply])
        except discord.Forbidden:
            await ctx.send("\N{CROSS MARK} I do not have permission to edit this member!")
            return
        else:
            await ctx.send(f"Punished {member.name} for {duration_delta}.")

        channel_id = config.shitpost_channel_id if type_ == "shitpost" else config.jailed_channel_id
        action_type = ActionType.SHITPOST if type_ == "shitpost" else ActionType.JAIL
        # Dispatch our custom punishment event.
        punishment = Punishment(ctx.guild, member, ctx.author, action_type, reason, duration_delta)
        self.bot.dispatch("punishment_add", punishment)

        return ctx.guild.get_channel(channel_id)

    # NOTE: The mod check defaults to `manage_guild == True` atm.
    # We probably want to change that in the future.
    @commands.command(aliases=["jail"])
    @is_mod()
    async def shitpost(self, ctx, duration: FutureTime, member: discord.Member, *, reason=None):
        """Temporarily locks a user out of every single channel this guild has.

        The duration can be a short time form, e.g. 12d or a more human
        duration such as "until monday at 2PM" or a more concrete time
        such as "2018-12-31".

        All times are in UTC.
        """

        type_ = ctx.invoked_with
        channel = await self.punish_and_get_channel(ctx, duration, reason, member, type_)
        if not channel:
            return

        # Good enough for now.
        await channel.send(f"{member.mention} You were {type_}ed for {human_timedelta(duration.dt)}.")

    @Cog.listener()
    async def on_punish_timer_complete(self, timer):
        guild_id, mod_id, member_id, roles, type_ = timer.args

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        member = guild.get_member(member_id)
        if member is None:
            return

        # Get around discord's memey managed roles.
        to_assign = set(r.id for r in member.roles if r.managed).union(roles)
        await member.edit(roles=filter(None, (guild.get_role(r_id) for r_id in to_assign)))

        mod = guild.get_member(mod_id)
        if mod is None:
            return

        config = await self.get_config(mod)
        try:
            await mod.send("Automatic punishment release from timer created "
                           f"{timer.human_delta} for {member}.")
        except discord.Forbidden:
            if config:
                await config.mod_channel.send(f"{mod.mention} {member}'s punishment is now over!")

        action_type = ActionType.SHITPOST if type_ == "shitpost" else ActionType.JAIL
        punishment = Punishment(guild, member, mod, action_type)
        self.bot.dispatch("punishment_remove", punishment)

        try:
            await attempt_notification(member, f"Your punishment on {guild.name} expired.")
        except RuntimeError:
            pass

    @commands.command(aliases=["cleanpost", "cleanjail"])
    @is_mod()
    async def free(self, ctx, *, member: discord.Member):
        """Cancels a punishment of a member."""

        reminder = self.bot.get_cog("Reminder")
        if not reminder:
            return await ctx.send("Sorry, this is currently unavailable. Load the `Reminder` cog.")

        query = "SELECT * FROM reminders WHERE event ='punish' AND extra #>> '{args, 2}' = $1"
        record = await ctx.db.fetchrow(query, str(member.id))

        timer = Timer(record=record) if record else None
        if not timer:
            return await ctx.send(f"{member} is not currently punished.")

        # Dispatch timer.
        await reminder.call_timer(timer)
        if reminder._current_timer and reminder._current_timer.id == timer.id:
            # Cancel and re-run.
            reminder._task.cancel()
            reminder._task = self.bot.loop.create_task(reminder.dispatch_timers())

        await ctx.send(f"Punishment for {member} was successfully cancelled.")


"""
Punishments:
- Ban -> user, mod, reason, duration
- Kick -> see above
- Shitposted -> see above
- Jailed -> see above

== Type, target, mod, reason, duration
"""

setup = Punishments.setup
