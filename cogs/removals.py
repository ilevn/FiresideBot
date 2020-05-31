import argparse
import asyncio
import io
import itertools
import re
import shlex
import textwrap
import weakref
from collections import Counter, namedtuple
from datetime import datetime, timedelta
from enum import IntEnum
from typing import Union, Dict

import discord
from discord.ext import commands

from cogs.utils import db, Plural, human_timedelta, is_mod
from cogs.utils.converters import FetchedUser, entry_id
from cogs.utils.meta_cog import Cog
from cogs.utils.paginators import CannotPaginate, Pages
from cogs.utils.punishment import Punishment, ActionType


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


class RemovalType(IntEnum):
    UNBAN = 0
    KICK = 1
    BAN = 2

    @property
    def audit_log_action(self):
        return getattr(discord.AuditLogAction, self.name.lower())

    @property
    def action_type(self):
        return getattr(ActionType, self.name.upper())

    @property
    def punishment_type(self):
        return "punishment_add" if self in (RemovalType.KICK, RemovalType.BAN) else "punishment_remove"

    @property
    def colour(self):
        return 0x40E0D0 if self is RemovalType.UNBAN else 0xe57373


class RemovalsTable(db.Table, table_name='removals'):
    id = db.PrimaryKeyColumn()
    # Affected user.
    user_id = db.Column(db.Integer(big=True), nullable=False, index=True)
    # Responsible mod.
    moderator_id = db.Column(db.Integer(big=True), index=True)
    # Message id of the notification embed.
    message_id = db.Column(db.Integer(big=True), index=True)
    # Guild id.
    guild_id = db.Column(db.Integer(big=True), nullable=False)
    # Punishment reason, if available.
    reason = db.Column(db.String)
    # Creation time of the entry.
    created_at = db.Column(db.Datetime, default="now() at time zone 'utc'")
    # User name, used for displays.
    name = db.Column(db.String, index=True)
    # Whether this is a kick, ban or unban.
    type = db.Column(db.Integer(small=True), default=0)
    # Message id in the punishment channel.
    punish_message_id = db.Column(db.Integer(big=True))


class ActionReason(commands.Converter):
    async def convert(self, ctx, argument):
        if len(argument) > 512:
            raise commands.BadArgument(f'reason is too long ({len(argument)}/512)')
        return argument


_RemovalEntry = namedtuple("RemovalEntry", "user moderator reason")

BAN_ADD = "\U00002620 Ban"
KICK_ADD = "\U0001f462 Kick"
BAN_REMOVE = "\U0001f33b Unban"


class RemovalPages(Pages):
    def __init__(self, ctx, entries, *, per_page=4):
        super().__init__(ctx, entries=entries, per_page=per_page)
        self.total = len(entries)

    @classmethod
    async def from_all(cls, ctx):
        query = """SELECT id, user_id, name, reason, created_at, type, moderator_id
                   FROM removals WHERE guild_id = $1
                   ORDER BY created_at DESC"""

        records = await ctx.db.fetch(query, ctx.guild.id)
        if not records:
            raise CannotPaginate("This server doesn't have any removals in its database yet.")

        nested_pages = []
        per_page = 8

        def key(record):
            return record["created_at"].date()

        def get_mod(id_):
            return getattr(ctx.guild.get_member(id_), "name", f"Unknown mod (ID: {id_})")

        for date, info in itertools.groupby(records, key=key):
            needed_info = [r[:6] + (get_mod(r[6]),) for r in info]

            # Small stats about active mods.
            mod, num = Counter([x[-1] for x in needed_info]).most_common()[0]
            desc = f"{Plural(len(needed_info)):removal}" + \
                   ('' if len(needed_info) < 2 else f", most by {mod} ({Plural(num):removal})")

            nested_pages.extend(
                (date, desc,
                 needed_info[i:i + per_page]) for i in range(0, len(needed_info), per_page)
            )

        self = cls(ctx, nested_pages, per_page=1)
        self.get_page = self.get_removal_page
        self.total = sum(len(o) for _, _, o in nested_pages)
        return self

    def get_removal_page(self, page):
        date, description, info = self.entries[page - 1]
        self.title = f'Removals on {date:%d/%m/%Y}'
        self.description = description
        return info

    def prepare_embed(self, entries, page, *, first=False):
        self.embed.clear_fields()
        self.embed.description = self.description
        self.embed.title = self.title

        self.embed.set_footer(text=f'Removal log')

        for entry in entries:
            id_, user_id, name, reason, created_at, type_, moderator = entry
            delta = human_timedelta(created_at)
            # Format the actual entry.
            type_ = RemovalType(type_).name.title()
            fmt = f"[{id_}] {delta} - {reason or 'No reason specified.'} [*{moderator}*]"
            banned_user = f"{name or 'Unknown member'} (ID: {user_id})"
            self.embed.add_field(name=f"[{type_}] {banned_user}", value=fmt, inline=False)

        if self.maximum_pages:
            self.embed.set_author(name=f'Page {page}/{self.maximum_pages} ({self.total} DB entries)')


def can_execute_action(ctx, user, target):
    return user.id == ctx.bot.owner_id or \
           user == ctx.guild.owner or \
           user.top_role > target.top_role


class Removals(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        # A set of known removals. Used to short-circuit events.
        self.known_removals = set()
        self.removal_information: Dict[int, _RemovalEntry] = {}
        self._locks = weakref.WeakValueDictionary()

    async def cog_check(self, ctx):
        return bool(ctx.guild)

    @staticmethod
    async def get_potential_removal_entry(guild: discord.Guild, user, type_: RemovalType):
        # Discord is inconsistent.
        await asyncio.sleep(1)

        async for entry in guild.audit_logs(action=type_.audit_log_action, limit=2):
            if entry.target == user:
                return _RemovalEntry(entry.target, entry.user, entry.reason)

        # Log not found, only return partial information in case of ban.
        # As for UNBAN and KICK: If we can't find entries for them,
        # they probably didn't get unbanned or kicked.
        if type_ == RemovalType.BAN:
            info = await guild.fetch_ban(user)
            return _RemovalEntry(info.user, None, info.reason)

    async def get_modlog(self, guild_id):
        """Return the cached guild config for a server."""
        event_cog = self.bot.get_cog("Event")
        config = event_cog and await event_cog.get_guild_config(guild_id)
        return config and config.modlog

    def format_modlog_entry(self, member, moderator, formatter, colour, e_id, reason=None):
        embed = discord.Embed(title=formatter, colour=colour)

        embed.add_field(name="Username", value=member)
        if isinstance(member, discord.Member):
            # Not a cross-ban.
            embed.add_field(name="Nickname", value=member.nick)
            embed.add_field(name="Profile", value=member.mention)

        embed.add_field(name="ID", value=member.id)
        embed.add_field(name="Moderator", value=moderator or "No responsible moderator", inline=False)
        # Create placeholder in case no reason was provided.
        prefix = self.bot.command_prefix
        reason_place_holder = f"No reason set. Provide one with `{prefix}reason {e_id} <reason>`."
        embed.add_field(name="Reason", value=reason or reason_place_holder)
        # Make entry ID available to allow for back-references.
        embed.set_footer(text=f'Entry ID {e_id} ').timestamp = datetime.utcnow()
        return embed

    async def _parse_and_log_event(self, guild, member, type_, formatter):
        if member.id == self.bot.user.id:
            # Bot probably got removed from the guild.
            return

        # First, try to get audit log info.
        try:
            info = self.removal_information.pop(member.id)
        except KeyError:
            info = await self.get_potential_removal_entry(guild, member, type_)

        if info is None:
            return

        query = """
                INSERT INTO removals (user_id, moderator_id, reason, guild_id, name, type)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, user_id, moderator_id, reason
                """

        mod_id = getattr(info.moderator, "id", None)
        record = await self.bot.pool.fetchrow(query, info.user.id, mod_id, info.reason,
                                              guild.id, str(member), type_.value)

        event_cog = self.bot.get_cog("Event")
        config = event_cog and await event_cog.get_guild_config(guild.id)
        if not (config and config.modlog):
            # Don't send, I guess.
            self.bot.logger.warn("Removal event failed to send...")
            return

        responsible_mod = info.moderator
        reason = info.reason
        e_id = record[0]
        embed = self.format_modlog_entry(member, responsible_mod, formatter, type_.colour, e_id, reason)
        # Finally, log the new message to allow mods to edit it later.
        msg = await config.modlog.send(embed=embed)
        query = """UPDATE removals SET message_id = $1 WHERE id = $2"""
        await self.bot.pool.execute(query, msg.id, record[0])

        # Remind mods to provide a reason if none is set.
        if not reason and config.mod_channel:
            prefix = self.bot.command_prefix
            if responsible_mod:
                action = "unbanning" if type_ is RemovalType.UNBAN else "getting rid of"
                fmt = f"Hey {responsible_mod.mention}!" \
                      f" I noticed you haven't specified a reason for {action} {member} yet...\n" \
                      f"Please do so by typing `{prefix}reason {e_id}`, thank you."
            else:
                action = "unbanned" if type_ is RemovalType.UNBAN else "got rid of"
                fmt = f"@here I'm not sure who {action} {member} but their entry is missing a reason...\n" \
                      f"Please specify one by typing `{prefix}reason {e_id}`, thank you."

            await config.mod_channel.send(fmt)

        # Also dispatch to #punishment channel.
        action_type = type_.action_type
        punishment = Punishment(guild, member, responsible_mod, action_type, reason or "No reason provided.", id=e_id)
        self.bot.dispatch(type_.punishment_type, punishment)

        if type_ == RemovalType.BAN:
            # Set marker to avoid event clashing with KICK.
            # The only reason this works is because discord dispatches
            # MEMBER_BAN first.
            self.known_removals.add(member.id)

    async def parse_event_with_lock(self, guild, member, type_, formatter):
        lock = self._locks.get(guild.id)
        if lock is None:
            self._locks[guild.id] = lock = asyncio.Lock(loop=self.bot.loop)

        async with lock:
            if member.id in self.known_removals and type_ != RemovalType.UNBAN:
                # Likely a KICK event.
                self.known_removals.remove(member.id)
                return

            await self._parse_and_log_event(guild, member, type_, formatter)

    @Cog.listener()
    async def on_member_ban(self, guild, member):
        await self.parse_event_with_lock(guild, member, RemovalType.BAN, BAN_ADD)

    @Cog.listener()
    async def on_member_unban(self, guild, user):
        await self.parse_event_with_lock(guild, user, RemovalType.UNBAN, BAN_REMOVE)

    @Cog.listener()
    async def on_member_remove(self, member):
        # This is very much experimental at the moment.
        # The main problem we're facing is that discord delegates kicks to
        # MEMBER_REMOVE, which means there's no reliable way of determining
        # whether someone actually got kicked.
        await self.parse_event_with_lock(member.guild, member, RemovalType.KICK, KICK_ADD)

    @commands.command(name="reason")
    @is_mod()
    async def removal_reason(self, ctx, id: entry_id, *, reason: ActionReason):
        """Allows you to provide a reason for a removal entry."""
        query = "SELECT message_id, reason, punish_message_id FROM removals WHERE id = $1 AND guild_id = $2"
        record = await ctx.db.fetchrow(query, id, ctx.guild.id)
        if not record:
            return await ctx.send("Could not find an entry with that ID.")

        if record[1]:
            fmt = "This entry already has a reason specified. Are you sure you want to overwrite it?"
            if not await ctx.prompt(fmt):
                return await ctx.send("Aborting...", delete_after=3)

        event_cog = self.bot.get_cog("Event")
        config = event_cog and await event_cog.get_guild_config(ctx.guild.id)
        if not (config and config.modlog):
            return await ctx.send("Could not find modlog channel. Aborting...", delete_after=3)

        try:
            message = await config.modlog.fetch_message(record[0])
        except discord.NotFound:
            return await ctx.send("Could not find message in modlog. Aborting...", delete_after=3)

        embed = message.embeds[0]
        embed.set_field_at(-2, name="Moderator", value=ctx.author, inline=False)
        embed.set_field_at(-1, name="Reason", value=reason)
        await message.edit(embed=embed)

        query = "UPDATE removals SET reason = $1, moderator_id = $2 WHERE message_id = $3"
        await ctx.db.execute(query, reason, ctx.author.id, message.id)

        # Update reason in #punishment as well.
        if record[2] and config.punishment_channel:
            try:
                message = await config.punishment_channel.fetch_message(record[2])
            except discord.NotFound:
                # Peculiar.
                self.logger.warn(f"Removal entry {id} is missing a punishment message.")
            else:
                embed = message.embeds[0]
                embed.set_field_at(1, name="Moderator", value=ctx.author, inline=False)
                embed.set_field_at(-2, name="Reason", value=reason)
                await message.edit(embed=embed)

        await ctx.send(f"Successfully set reason for entry {id}.")

    @commands.command(aliases=["xban"])
    @is_mod()
    async def crossban(self, ctx, user_ids: commands.Greedy[int], *, reason: ActionReason = None):
        """
        Bans a member via their ID.
        Useful if you want to ban a member who's
        behaving inappropriately on a different server.
        """

        guild = ctx.guild
        mod = ctx.author
        if any(mem.id in user_ids for mem in guild.members):
            return await ctx.send(":x: Don't use this command to ban server members!")

        # Remove duplicates.
        user_ids = set(user_ids) - set(x.user.id for x in await guild.bans())
        if not user_ids:
            return await ctx.send("It looks like all of these IDs are already banned. Aborting...")

        reason = reason or 'External server ban'
        actual_users = set()

        async with ctx.channel.typing():
            for user_id in user_ids:
                try:
                    user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                except discord.NotFound:
                    pass
                else:
                    actual_users.add(user)

            if not actual_users:
                return await ctx.send("Please provide at least one valid user ID.")

            b_type = RemovalType.BAN
            keys = ('user_id', 'moderator_id', 'guild_id', 'reason', 'name', 'type')
            to_insert = ((m.id, mod.id, guild.id, reason, str(m), b_type.value) for m in actual_users)

            query = """
                    INSERT INTO removals(user_id, moderator_id, guild_id, reason, name, type)
                    SELECT x.user_id, x.moderator_id, x.guild_id, x.reason, x.name, x.type
                    FROM jsonb_to_recordset($1::jsonb)
                    AS x(user_id BIGINT, moderator_id BIGINT, guild_id BIGINT,
                         reason text, name text, type smallint)
                    RETURNING user_id, id
                    """

            records = await ctx.db.fetch(query, [dict(zip(keys, elem)) for elem in to_insert])
            users = {user_id: e_id for user_id, e_id in records}
            for user in actual_users:
                self.known_removals.add(user.id)
                # Dispatch to log channels.
                modlog = await self.get_modlog(guild.id)
                e_id = None
                if modlog:
                    e_id = users[user.id]
                    embed = self.format_modlog_entry(user, mod, BAN_ADD, b_type.colour, e_id, reason)
                    msg = await modlog.send(embed=embed)
                    query = """UPDATE removals SET message_id = $1 WHERE id = $2"""
                    await ctx.db.execute(query, msg.id, e_id)

                punishment = Punishment(guild, user, mod, ActionType.BAN, reason, id=e_id)
                self.bot.dispatch("punishment_add", punishment)
                await guild.ban(user, reason=reason)

            pages = Pages(ctx, entries=[f"{user} - (`{user.id}`)" for user in actual_users])
            pages.embed.title = f"Cross-banned {Plural(len(actual_users)):user}"

            try:
                await pages.paginate()
            except CannotPaginate as e:
                await ctx.send(e)

    @commands.command()
    @is_mod()
    async def ban(self, ctx, member: discord.Member, *, reason: ActionReason):
        """Bans a member from the server."""

        # People with manage_guild won't get banned for now.
        if ctx.channel.permissions_for(member).manage_guild:
            await ctx.send(":x: I cannot ban this member.")
            return

        self.removal_information[member.id] = _RemovalEntry(member, ctx.author, reason)
        await member.ban(delete_message_days=7, reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @is_mod()
    async def kick(self, ctx, member: discord.Member, *, reason: ActionReason):
        """Kicks a member from the server."""

        # People with manage_guild won't get kicked for now.
        if ctx.channel.permissions_for(member).manage_guild:
            await ctx.send(":x: I cannot kick this member.")
            return

        self.removal_information[member.id] = _RemovalEntry(member, ctx.author, reason)
        await member.kick(reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @is_mod()
    async def unban(self, ctx, member: discord.Member, *, reason: ActionReason):
        """Unbans a member from the server."""
        self.removal_information[member.id] = _RemovalEntry(member, ctx.author, reason)
        await member.unban(reason=reason)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.group(aliases=["removal"], invoke_without_command=True)
    @is_mod()
    async def removals(self, ctx):
        """Fetches all guild removals from the central database"""
        try:
            page = await RemovalPages.from_all(ctx)
            await page.paginate()
        except CannotPaginate as e:
            await ctx.send(e)

    @removals.command(name="view")
    @is_mod()
    async def removals_view(self, ctx, *, id: Union[FetchedUser, entry_id]):
        """View a ban entry by member or database ID.
        Note that member based views only show the most recent removal (for now)."""

        args, id_ = ("AND user_id =", id.id) if isinstance(id, discord.User) else ("AND id =", id)
        query = f"""
                SELECT user_id, name, moderator_id, message_id, reason, created_at, type
                FROM removals
                WHERE guild_id = $1 {args} $2
                """

        record = await ctx.db.fetchrow(query, ctx.guild.id, id_)
        if not record:
            return await ctx.send("Could not find an entry with this ID.")

        user_id, name, mod_id, message, reason, created, type_ = record
        type_ = RemovalType(type_).name.title()
        embed = discord.Embed(title=f"[{type_}] {name or 'Unknown name'} ({user_id})",
                              colour=discord.Colour.blurple())

        mod = ctx.guild.get_member(mod_id)
        embed.set_author(name=mod or f'ID: {mod_id}', icon_url=getattr(mod, 'avatar_url', None))

        modlog = await self.get_modlog(ctx.guild.id)
        if modlog and message:
            url = f'https://discordapp.com/channels/{ctx.guild.id}/{modlog.id}/{message}'
            embed.description = f"[Jump to removal]({url})"

        reason = textwrap.shorten(reason, 120) if reason else 'No reason'
        embed.add_field(name="Reason", value=reason, inline=False)

        embed.set_footer(text=f"Entry ID {id}").timestamp = created
        await ctx.send(embed=embed)

    @removals.command(name="stats")
    @is_mod()
    async def removal_stats(self, ctx, *, member: discord.Member = None):
        """Shows ban stats about a moderator or the server."""
        if member is None:
            await self.show_guild_stats(ctx)
        else:
            await self.show_mod_stats(ctx, member)

    async def show_guild_stats(self, ctx):
        lookup = (
            '\N{FIRST PLACE MEDAL}',
            '\N{SECOND PLACE MEDAL}',
            '\N{THIRD PLACE MEDAL}',
            '\N{SPORTS MEDAL}',
            '\N{SPORTS MEDAL}'
        )

        embed = discord.Embed(title='Server Removal Stats', colour=discord.Colour.blurple())
        # Total bans.
        query = "SELECT COUNT(*), MIN(created_at) FROM removals WHERE guild_id=$1;"
        count = await ctx.db.fetchrow(query, ctx.guild.id)

        query = """SELECT COUNT(*)
                   FROM removals
                   WHERE guild_id = $1 AND created_at > (CURRENT_TIMESTAMP - INTERVAL '7 days')"""

        this_week = await ctx.db.fetchval(query, ctx.guild.id)
        embed.description = f'{count[0]} ({this_week} this week) users removed.'
        embed.set_footer(text='Tracking removals since').timestamp = count[1] or datetime.utcnow()

        query = """
                SELECT moderator_id, COUNT(*)
                FROM removals
                WHERE guild_id=$1
                AND moderator_id IS NOT NULL 
                AND type = ANY('{1, 2}')
                GROUP BY moderator_id
                ORDER BY 2 DESC
                LIMIT 5;
                """

        records = await ctx.db.fetch(query, ctx.guild.id)
        value = '\n'.join(f'{lookup[index]}: <@!{author_id}> ({Plural(bans):removal})'
                          for (index, (author_id, bans)) in enumerate(records)) or 'No removals.'

        embed.add_field(name='Top Mods (by removals)', value=value, inline=False)

        query = """
                SELECT moderator_id, COUNT(*)
                FROM removals
                WHERE guild_id=$1
                AND created_at > (CURRENT_TIMESTAMP - INTERVAL '1 day')
                AND moderator_id IS NOT NULL
                GROUP BY moderator_id
                ORDER BY 2 DESC
                LIMIT 5
                """

        records = await ctx.db.fetch(query, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: <@!{author_id}> ({Plural(bans):removal})'
                          for (index, (author_id, bans)) in enumerate(records)) or 'No removals today'

        embed.add_field(name='Top Mods Today (by removals)', value=value, inline=False)
        await ctx.send(embed=embed)

    @staticmethod
    async def show_mod_stats(ctx, member):
        lookup = (
            '\N{FIRST PLACE MEDAL}',
            '\N{SECOND PLACE MEDAL}',
            '\N{THIRD PLACE MEDAL}',
            '\N{SPORTS MEDAL}',
            '\N{SPORTS MEDAL}'
        )

        query = "SELECT COUNT(*), MIN(created_at) FROM removals WHERE moderator_id = $1 AND guild_id = $2"
        mod_count = await ctx.db.fetchrow(query, member.id, ctx.guild.id)

        query = """SELECT COUNT(*)
                           FROM removals
                           WHERE guild_id = $1 
                           AND created_at > (CURRENT_TIMESTAMP - INTERVAL '7 days')
                           AND moderator_id = $2"""

        this_week = await ctx.db.fetchval(query, ctx.guild.id, member.id)

        embed = discord.Embed(title=f'Mod Removal Stats for {member}', colour=discord.Colour.blurple())
        embed.description = f'{mod_count[0]} ({this_week} this week) users removed.'
        embed.set_footer(text='First recorded removal').timestamp = mod_count[1] or datetime.utcnow()

        query = """
                SELECT CASE
                WHEN LOWER(reason) LIKE 'no reason%' THEN 'No reason'
                        ELSE LOWER(reason)
                END AS res, COUNT(*)
                FROM removals
                WHERE moderator_id = $1 AND guild_id = $2 AND reason != 'None'
                GROUP BY res ORDER BY 2 DESC
                LIMIT 5
                """

        records = await ctx.db.fetch(query, member.id, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: {reason} ({Plural(uses):ban})'
                          for (index, (reason, uses)) in enumerate(records)) or 'No reasons provided yet'

        embed.add_field(name="Common ban reasons", value=value, inline=False)

        total = await ctx.db.fetchval("SELECT COUNT(*) FROM removals WHERE guild_id = $1", ctx.guild.id)
        embed.add_field(name="Removal percentage", value=f'{(mod_count[0] / total) * 100:.2f}%')
        await ctx.send(embed=embed)

    @commands.command(name="isbanned")
    async def is_banned(self, ctx, *, user: Union[discord.Member, FetchedUser]):
        """Returns whether a user is banned or not."""

        await ctx.message.delete()
        banned = user.id in (b.user.id for b in await ctx.guild.bans())
        await ctx.send("Yes." if banned else "No.", delete_after=30)

    @commands.command()
    @is_mod()
    async def massban(self, ctx, *, args):
        """Mass bans multiple members from the server.

        This command has a powerful "command line" syntax. **Every option is optional.**

        Users are only banned **if and only if** all conditions are met.

        The following options are valid.

        `--channel` or `-c`: Channel to search for message history.
        `--reason` or `-r`: The reason for the ban.
        `--regex`: Regex that usernames must match.
        `--created`: Matches users whose accounts were created less than specified minutes ago.
        `--joined`: Matches users that joined less than specified minutes ago.
        `--joined-before`: Matches users who joined before the member ID given.
        `--joined-after`: Matches users who joined after the member ID given.
        `--no-avatar`: Matches users who have no avatar. (no arguments)
        `--no-roles`: Matches users that have no role. (no arguments)
        `--show`: Show members instead of banning them. (no arguments)
        Message history filters (Requires `--channel`):
        `--contains`: A substring to search for in the message.
        `--starts`: A substring to search for that the message starts with.
        `--ends`: A substring to search for that the message ends with.
        `--match`: A regex to match the message content to.
        `--search`: How many messages to search. Default 100. Max 2000.
        `--after`: Messages must come after this message ID.
        `--before`: Messages must come before this message ID.
        `--files`: Checks if the message has attachments. (no arguments)
        `--embeds`: Checks if the message has embeds. (no arguments)
        """

        # For some reason there are cases due to caching that ctx.author
        # can be a User even in a guild only context
        # Rather than trying to work out the kink with it
        # just upgrade the member itself.
        if not isinstance(ctx.author, discord.Member):
            try:
                author = await ctx.guild.fetch_member(ctx.author.id)
            except discord.HTTPException:
                return await ctx.send("Somehow, Discord does not seem to think you are in this server.")
        else:
            author = ctx.author

        parser = Arguments(add_help=False, allow_abbrev=False)
        parser.add_argument("--channel", "-c")
        parser.add_argument("--reason", "-r")
        parser.add_argument("--search", type=int, default=100)
        parser.add_argument("--regex")
        parser.add_argument("--no-avatar", action="store_true")
        parser.add_argument("--no-roles", action="store_true")
        parser.add_argument("--created", type=int)
        parser.add_argument("--joined", type=int)
        parser.add_argument("--joined-before", type=int)
        parser.add_argument("--joined-after", type=int)
        parser.add_argument("--contains")
        parser.add_argument("--starts")
        parser.add_argument("--ends")
        parser.add_argument("--match")
        parser.add_argument("--show", action="store_true")
        parser.add_argument("--embeds", action="store_const", const=lambda m: len(m.embeds))
        parser.add_argument("--files", action="store_const", const=lambda m: len(m.attachments))
        parser.add_argument("--after", type=int)
        parser.add_argument("--before", type=int)

        try:
            args = parser.parse_args(shlex.split(args))
        except Exception as e:
            return await ctx.send(e)

        members = []

        if args.channel:
            channel = await commands.TextChannelConverter().convert(ctx, args.channel)
            before = args.before and discord.Object(id=args.before)
            after = args.after and discord.Object(id=args.after)
            predicates = []
            if args.contains:
                predicates.append(lambda m: args.contains in m.content)
            if args.starts:
                predicates.append(lambda m: m.content.startswith(args.starts))
            if args.ends:
                predicates.append(lambda m: m.content.endswith(args.ends))
            if args.match:
                try:
                    _match = re.compile(args.match)
                except re.error as e:
                    return await ctx.send(f"Invalid regex passed to `--match`: {e}")
                else:
                    predicates.append(lambda m, x=_match: x.match(m.content))
            if args.embeds:
                predicates.append(args.embeds)
            if args.files:
                predicates.append(args.files)

            async for message in channel.history(limit=min(max(1, args.search), 2000), before=before, after=after):
                if all(p(message) for p in predicates):
                    members.append(message.author)
        else:
            members = ctx.guild.members

        # Member filters.
        predicates = [
            lambda m: isinstance(m, discord.Member) and can_execute_action(ctx, author, m),  # Only if applicable.
            lambda m: not m.bot,  # No bots.
            lambda m: m.discriminator != '0000',  # No deleted users.
        ]

        async def _resolve_member(member_id):
            if (r := ctx.guild.get_member(member_id)) is None:
                try:
                    return await ctx.guild.fetch_member(member_id)
                except discord.HTTPException as e:
                    raise commands.BadArgument(f"Could not fetch member by ID {member_id}: {e}") from None
            return r

        if args.regex:
            try:
                _regex = re.compile(args.regex)
            except re.error as e:
                return await ctx.send(f"Invalid regex passed to `--regex`: {e}")
            else:
                predicates.append(lambda m, x=_regex: x.match(m.name))

        if args.no_avatar:
            predicates.append(lambda m: m.avatar is None)
        if args.no_roles:
            predicates.append(lambda m: len(getattr(m, "roles", [])) <= 1)

        now = datetime.utcnow()
        if args.created:
            def created(mem, *, offset=now - timedelta(minutes=args.created)):
                return mem.created_at > offset

            predicates.append(created)
        if args.joined:
            def joined(mem, *, offset=now - timedelta(minutes=args.joined)):
                if isinstance(mem, discord.User):
                    # If the member is a user then they left already.
                    return True
                return mem.joined_at and mem.joined_at > offset

            predicates.append(joined)
        if args.joined_after:
            _joined_after_member = await _resolve_member(args.joined_after)

            def joined_after(mem, *, _other=_joined_after_member):
                return mem.joined_at and _other.joined_at and mem.joined_at > _other.joined_at

            predicates.append(joined_after)
        if args.joined_before:
            _joined_before_member = await _resolve_member(args.joined_before)

            def joined_before(mem, *, _other=_joined_before_member):
                return mem.joined_at and _other.joined_at and mem.joined_at < _other.joined_at

            predicates.append(joined_before)

        members = {m for m in members if all(p(m) for p in predicates)}
        if not members:
            return await ctx.send("No members found matching criteria.")

        if args.show:
            members = sorted(members, key=lambda m: m.joined_at or now)
            fmt = "\n".join(f"{m.id}\tJoined: {m.joined_at}\tCreated: {m.created_at}\t{m}" for m in members)
            content = f"Current Time: {datetime.utcnow()}\nTotal members: {len(members)}\n{fmt}"
            file = discord.File(io.BytesIO(content.encode("utf-8")), filename="members.txt")
            return await ctx.send(file=file)

        if args.reason is None:
            return await ctx.send("--reason flag is required.")
        else:
            reason = await ActionReason().convert(ctx, args.reason)

        confirm = await ctx.prompt(f"This will ban **{Plural(len(members)):member}**. Are you sure?")
        if not confirm:
            return await ctx.send("Aborting.")

        count = 0
        for member in members:
            try:
                # Queue them up.
                self.removal_information[member.id] = _RemovalEntry(member, ctx.author, reason)
                await ctx.guild.ban(member, reason=reason)
            except discord.HTTPException:
                pass
            else:
                count += 1

        await ctx.send(f"Banned {count}/{len(members)}")


setup = Removals.setup
