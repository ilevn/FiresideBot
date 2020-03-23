import asyncio
import itertools
import textwrap
import weakref
from collections import Counter, namedtuple
from datetime import datetime
from enum import IntEnum
from typing import Union

import discord
from discord.ext import commands

from cogs.utils import db, Plural, human_timedelta, is_mod
from cogs.utils.meta_cog import Cog
from cogs.utils.paginators import CannotPaginate, Pages
from cogs.utils.punishment import Punishment, ActionType


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

    def punishment_type(self):
        return "punishment_add" if self in (RemovalType.KICK, RemovalType.BAN) else "punishment_remove"


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
    type = db.Column(db.Integer, default=0)


class FetchedUser(commands.Converter):
    async def convert(self, ctx, argument):
        if not argument.isdigit():
            raise commands.BadArgument('Not a valid user ID.')
        try:
            return await ctx.bot.fetch_user(argument)
        except discord.NotFound:
            raise commands.BadArgument('User not found.') from None
        except discord.HTTPException:
            raise commands.BadArgument('An error occurred while fetching the user.') from None


class ActionReason(commands.Converter):
    async def convert(self, ctx, argument):
        if len(argument) > 512:
            raise commands.BadArgument(f'reason is too long ({len(argument)}/512)')
        return argument


_RemovalEntry = namedtuple("RemovalEntry", "user moderator reason")


def entry_id(arg):
    try:
        arg = int(arg)
    except ValueError:
        raise commands.BadArgument("Please supply a valid entry ID.")

    # PSQL ints are capped at 2147483647.
    # < capped -> entry id
    if not 0 < arg < 2147483647:
        raise commands.BadArgument("This looks like an entry ID...")

    return arg


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


class Removals(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        # A set of known removals. Used to short-circuit events.
        self.known_removals = set()
        self._locks = weakref.WeakValueDictionary()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False

        return True

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

    async def _parse_and_log_event(self, guild, member, type_, formatter):
        # First, try to get audit log info.
        info = await self.get_potential_removal_entry(guild, member, type_)
        if not info:
            # Not removed or member couldn't be found.
            return

        query = """INSERT INTO removals (user_id, moderator_id, reason, guild_id, name, type)
                            VALUES ($1, $2, $3, $4, $5, $6)
                            RETURNING id, user_id, moderator_id, reason"""

        mod_id = getattr(info.moderator, "id", None)
        record = await self.bot.pool.fetchrow(query, info.user.id, mod_id, info.reason,
                                              guild.id, str(member), type_.value)

        modlog = await self.get_modlog(guild.id)
        if not modlog:
            # Don't send, I guess.
            self.bot.logger.warn("Removal event failed to send...")
            return

        entry = _RemovalEntry(*record[1:])
        colour = 0x40E0D0 if type_ == RemovalType.UNBAN else 0xe57373
        embed = discord.Embed(title=formatter, colour=colour)

        embed.add_field(name="Username", value=str(member))
        if isinstance(member, discord.Member):
            # Not a cross-ban.
            embed.add_field(name="Nickname", value=member.nick)
            embed.add_field(name="Profile", value=member.mention)

        embed.add_field(name="ID", value=member.id)
        responsible_mod = guild.get_member(entry.moderator)
        embed.add_field(name="Moderator", value=responsible_mod or "No responsible moderator", inline=False)
        # Create placeholder in case no reason was provided.
        prefix = self.bot.command_prefix
        reason_place_holder = f"No reason yet. Provide one with `{prefix}reason {record[0]} <reason>`."
        embed.add_field(name="Reason", value=entry.reason or reason_place_holder)
        # Make entry ID available to allow for back-references.
        embed.set_footer(text=f'Entry ID {record[0]} ').timestamp = datetime.utcnow()
        # Finally, log the new message to allow mods to edit it later.
        msg = await modlog.send(embed=embed)
        query = """UPDATE removals SET message_id = $1 WHERE id = $2"""
        await self.bot.pool.execute(query, msg.id, record[0])
        # Also dispatch to #punishment channel.
        action_type = type_.action_type
        punishment = Punishment(guild, member, responsible_mod, action_type, entry.reason or "No reason provided.")
        self.bot.dispatch(action_type.punishment_type, punishment)

        if type_ == RemovalType.BAN:
            # Set marker to avoid event clashing with KICK.
            self.known_removals.add(member.id)

    async def parse_event_with_lock(self, guild, member, type_, formatter):
        lock = self._locks.get(guild.id)
        if lock is None:
            self._locks[guild.id] = lock = asyncio.Lock(loop=self.bot.loop)

        async with lock:
            if member.id in self.known_removals and type_ != RemovalType.UNBAN:
                # Banned/unbanned with command. No further action required.
                self.known_removals.remove(member.id)
                return

            await self._parse_and_log_event(guild, member, type_, formatter)

    @Cog.listener()
    async def on_member_ban(self, guild, member: discord.Member):
        await self.parse_event_with_lock(guild, member, RemovalType.BAN, "\U00002620 Ban")

    @Cog.listener()
    async def on_member_unban(self, guild, user: discord.User):
        await self.parse_event_with_lock(guild, user, RemovalType.UNBAN, "\U0001f33b Unban")

    @Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        # This is very much experimental at the moment.
        # The main problem we're facing is that discord delegates kicks to
        # MEMBER_REMOVE, which means there's no reliable way of determining
        # whether someone actually got kicked.
        await self.parse_event_with_lock(member.guild, member, RemovalType.KICK, "\U0001f462 Kick")

    @commands.command(name="reason")
    @is_mod()
    async def removal_reason(self, ctx, id: entry_id, *, reason: ActionReason):
        query = "SELECT message_id, reason FROM removals WHERE id = $1 AND guild_id = $2"
        record = await ctx.db.fetchrow(query, id, ctx.guild.id)
        if not record:
            return await ctx.send("Could not find an entry with that ID.")

        if record[1]:
            confirm = await ctx.prompt("This entry already has a reason specified."
                                       " Are you sure you want to overwrite it?")

            if not confirm:
                return await ctx.send("Aborting...", delete_after=3)

        modlog = await self.get_modlog(ctx.guild.id)
        if not modlog:
            return await ctx.send("Could not find modlog channel. Aborting...", delete_after=3)

        try:
            message = await modlog.fetch_message(record[0])
        except discord.NotFound:
            return await ctx.send("Could not find message in modlog. Aborting...", delete_after=3)

        embed = message.embeds[0]
        embed.set_field_at(-2, name="Moderator", value=ctx.author, inline=False)
        embed.set_field_at(-1, name="Reason", value=reason)
        await message.edit(embed=embed)

        query = "UPDATE removals SET reason = $1, moderator_id = $2 WHERE message_id = $3"
        await ctx.db.execute(query, reason, ctx.author.id, message.id)
        await ctx.send(f"Successfully set reason for entry {id}.")

    @commands.command(aliases=["xban"])
    @is_mod()
    async def crossban(self, ctx, user_ids: commands.Greedy[int], *, reason: ActionReason = None):
        """
        Ban a member via their ID.
        Useful if you want to ban a member who's
        behaving inappropriately on a different server.
        """

        if any(mem.id in user_ids for mem in ctx.guild.members):
            return await ctx.send(":x: Don't use this command to ban server members!")

        # Remove duplicates.
        user_ids = set(user_ids) - set(x.user.id for x in await ctx.guild.bans())
        if not user_ids:
            return await ctx.send("It looks like all of these IDs are already banned. Aborting...")

        reason = reason or 'Cross-ban'
        actual_users = set()

        async with ctx.channel.typing():
            for user_id in user_ids:
                try:
                    user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                except discord.NotFound:
                    continue

                actual_users.add(user)

            if not actual_users:
                return await ctx.send("Please provide at least one valid user ID.")

            # Perform a BULK COPY to insert all additions.
            keys = ('user_id', 'moderator_id', 'guild_id', 'reason', 'name', 'type')
            b_val = RemovalType.BAN.value
            to_insert = [(m.id, ctx.author.id, ctx.guild.id, reason, str(m), b_val) for m in actual_users]
            await ctx.db.copy_records_to_table('removals', columns=keys, records=to_insert)

            # Add banned users to removal list and ban.
            for user in actual_users:
                self.known_removals.add(user.id)
                punishment = Punishment(ctx.guild, user, ctx.author, ActionType.BAN, reason)
                self.bot.dispatch("punishment_add", punishment)
                await ctx.guild.ban(user, reason=reason)

            pages = Pages(ctx, entries=[f"{user} - (`{user.id}`)" for user in actual_users])
            pages.embed.title = f"Cross-banned {Plural(len(actual_users)):user}"

            try:
                await pages.paginate()
            except CannotPaginate as e:
                await ctx.send(e)

    # Maintainer note:
    # Removal commands currently don't get logged. Change later?
    @commands.command()
    @is_mod()
    async def ban(self, ctx, member: discord.Member, *, reason: ActionReason = None):
        """
        Ban a member from the server.
        """

        # People with manage_guild won't get banned for now.
        if ctx.channel.permissions_for(member).manage_guild:
            await ctx.send(":x: I cannot ban this member.")
            return

        b_val = RemovalType.BAN.value
        query = "INSERT INTO removals (user_id, moderator_id, reason, guild_id, type) VALUES ($1, $2, $3, $4, $5)"
        await ctx.db.execute(query, member.id, ctx.author.id, reason, ctx.guild.id, b_val)

        await member.ban(delete_message_days=7, reason=reason)
        punishment = Punishment(ctx.guild, member, ctx.author, ActionType.BAN, reason)
        self.bot.dispatch("punishment_add", punishment)
        self.known_removals.add(member.id)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @is_mod()
    async def kick(self, ctx, member: discord.Member, *, reason: ActionReason = None):
        """
        Kicks a member from the server.
        """

        # People with manage_guild won't get kicked for now.
        if ctx.channel.permissions_for(member).manage_guild:
            await ctx.send(":x: I cannot kick this member.")
            return

        # EZ mode. No fetching required.
        query = "INSERT INTO removals (user_id, moderator_id, reason, guild_id, type) VALUES ($1, $2, $3, $4, $5)"
        await ctx.db.execute(query, member.id, ctx.author.id, reason, ctx.guild.id, RemovalType.KICK.value)

        await member.kick(reason=reason)
        self.known_removals.add(member.id)
        punishment = Punishment(ctx.guild, member, ctx.author, ActionType.KICK, reason)
        self.bot.dispatch("punishment_add", punishment)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.command()
    @is_mod()
    async def unban(self, ctx, member: discord.Member, *, reason: ActionReason = None):
        """
        Unbans a member from the server.
        """

        query = "INSERT INTO removals (user_id, moderator_id, reason, guild_id, type) VALUES ($1, $2, $3, $4, $5)"
        await ctx.db.execute(query, member.id, ctx.author.id, reason, ctx.guild.id, RemovalType.UNBAN.value)

        await member.unban(reason=reason)
        self.known_removals.add(member.id)
        punishment = Punishment(ctx.guild, member, ctx.author, ActionType.UNBAN, reason)
        self.bot.dispatch("punishment_remove", punishment)
        await ctx.send('\N{OK HAND SIGN}')

    @commands.group(aliases=["removal"], invoke_without_command=True)
    @is_mod()
    async def removals(self, ctx):
        """Fetches all guild removals from the central database"""
        page = await RemovalPages.from_all(ctx)

        try:
            await page.paginate()
        except CannotPaginate as e:
            await ctx.send(e)

    @removals.command(name="view")
    @is_mod()
    async def removals_view(self, ctx, *, id: Union[FetchedUser, entry_id]):
        """View a ban entry by member or database ID.
        Note, that member based views only yield the most recent removal (for now)."""

        args, id_ = ("AND user_id = ", id.id) if isinstance(id, discord.User) else ("AND id = ", id)
        query = f"""SELECT user_id, name, moderator_id, message_id, reason, created_at, type
                   FROM removals
                   WHERE guild_id = $1 {args} $2"""

        record = await ctx.db.fetchrow(query, ctx.guild.id, id_)
        if not record:
            return await ctx.send("Could not find an entry with this ID.")

        user_id, name, mod_id, message, reason, created, type_ = record
        embed = discord.Embed(title=f"[{type_}] {name or 'Unknown name'} ({user_id})",
                              colour=discord.Colour.blurple())

        mod = ctx.guild.get_member(mod_id)
        embed.set_author(name=mod or f'ID: {mod_id}', icon_url=getattr(mod, 'avatar_url', None))

        modlog = await self.get_modlog(ctx.guild.id)
        if modlog:
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

    @staticmethod
    async def show_guild_stats(ctx):
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

        # TODO: Maybe exclude UNBAN.
        query = """SELECT moderator_id,
                                  COUNT(*)
                           FROM removals
                           WHERE guild_id=$1
                           AND moderator_id IS NOT NULL 
                           GROUP BY moderator_id
                           ORDER BY 2 DESC
                           LIMIT 5;
                        """

        records = await ctx.db.fetch(query, ctx.guild.id)
        value = '\n'.join(f'{lookup[index]}: <@!{author_id}> ({Plural(bans):ban})'
                          for (index, (author_id, bans)) in enumerate(records)) or 'No removals.'

        embed.add_field(name='Top Mods (by removals)', value=value, inline=False)

        query = """SELECT moderator_id, COUNT(*)
                           FROM removals
                           WHERE guild_id=$1
                           AND created_at > (CURRENT_TIMESTAMP - INTERVAL '1 day')
                           AND moderator_id IS NOT NULL
                           GROUP BY moderator_id
                           ORDER BY 2 DESC
                           LIMIT 5;
                        """

        records = await ctx.db.fetch(query, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: <@!{author_id}> ({Plural(bans):ban})'
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

        query = """SELECT CASE
                            WHEN LOWER(reason) LIKE 'no reason%' THEN 'No reason'
                            ELSE LOWER(reason)
                        END AS res, COUNT(*)
                   FROM removals
                   WHERE moderator_id = $1 AND guild_id = $2 AND reason != 'None'
                   GROUP BY res ORDER BY 2 DESC
                   LIMIT 5"""

        records = await ctx.db.fetch(query, member.id, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: {reason} ({Plural(uses):ban})'
                          for (index, (reason, uses)) in enumerate(records)) or 'No reasons provided yet'

        embed.add_field(name="Common ban reasons", value=value, inline=False)

        total = await ctx.db.fetchval("SELECT COUNT(*) FROM removals WHERE guild_id = $1", ctx.guild.id)
        embed.add_field(name="Removal percentage", value=f'{(mod_count[0] / total) * 100:.2f}%')
        await ctx.send(embed=embed)

    @commands.command()
    async def isbanned(self, ctx, *, user: Union[discord.Member, FetchedUser]):
        """Returns whether a user is banned or not."""

        await ctx.message.delete()
        banned = user.id in (b.user.id for b in await ctx.guild.bans())
        await ctx.send("Yes." if banned else "No.", delete_after=30)


setup = Removals.setup
