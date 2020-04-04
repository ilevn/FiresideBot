import abc
import argparse
import datetime
import shlex
import textwrap
import unicodedata
from collections import defaultdict
from enum import IntFlag, Enum, _decompose
from io import StringIO
from typing import Optional

import discord
import regex as re
from discord.ext import commands

from cogs.events import EventConfig
from cogs.utils import db, embed_paginate, human_join, Plural, is_mod
from cogs.utils.cache import cache
from cogs.utils.converters import entry_id
from cogs.utils.meta_cog import Cog
from cogs.utils.paginators import FieldPages
from cogs.utils.punishment import Punishment, ActionType


class ActionFailed(Exception):
    pass


class Args(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


_TEST_STRING = ("", " ", "\t", "\n", "\b", "\r", "word", "hi!", "Robot overlord Adam", "chameleon")


def verify_regex(pattern, matcher=re.findall, tester=_TEST_STRING):
    try:
        if all(matcher(pattern, x) for x in tester):
            raise RuntimeError("This regex matches everything.")
        return pattern
    except re.error as exc:
        raise RuntimeError(f"Invalid regex passed: {exc}")


class StoreRegex(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        regex = r" ".join(values)
        verify_regex(regex)
        setattr(namespace, self.dest, regex)


class EntityType(Enum):
    MEMBER = "member"
    GUILD = "guild"
    CHANNEL = "channel"


class SpamFilter(db.Table):
    id = db.PrimaryKeyColumn()
    # The associated guild_id.
    guild_id = db.DiscordIDColumn(nullable=False)
    # This can either be a channel, member or guild.
    entity_id = db.DiscordIDColumn(nullable=False, index=True)
    # The entity type. Speeds up conversion.
    entity_type = db.Column(db.String, nullable=False)
    # The applied filter.
    regex = db.Column(db.String, nullable=False, index=True)
    # Meta-info about creation time.
    created = db.Column(db.Datetime, default="now() at time zone 'utc'")
    # Action that will be taken.
    action = db.Column(db.Integer(small=True), default=0, index=True)
    # Extra information pertaining an action
    extra = db.Column(db.JSON, default="'{}'::jsonb")


def wrap_exception(func):
    async def wrap(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            # Re-raise
            raise ActionFailed(e)

    return wrap


class BaseAction(metaclass=abc.ABCMeta):
    __slots__ = ("entity", "message", "config")

    def __init__(self, message, entity, config):
        self.message = message
        self.entity = entity
        self.config: EventConfig = config

    @abc.abstractmethod
    async def apply(self, **kwargs):
        raise NotImplementedError


class NotifyAction(BaseAction):
    __slots__ = ()

    async def apply(self, **kwargs):
        if not self.config.mod_channel:
            raise ActionFailed("No mod channel found.")

        e: FilterEntity = self.entity
        embed = discord.Embed(title=f"\U000026a0 Filter triggered for `{e.regex}`")
        instance = e.representation

        if e.entity_type != "guild":
            instance_formatter = f"{e.entity_type.title()}: {instance.mention} (ID: `{instance.id}`)"
        else:
            instance_formatter = "This server"

        embed.add_field(name="Affected instance", value=instance_formatter, inline=False)
        msg = self.message
        if e.entity_type != "member":
            embed.add_field(name="Triggered by", value=f"{msg.author.mention} (ID`{msg.author.id}`)", inline=False)
        elif e.entity_type != "channel":
            embed.add_field(name="In", value=f"{msg.channel.mention}", inline=False)

        flags = e.action_type.all_flags
        actions_taken = ", ".join(flag.name.title() for flag in flags)
        embed.add_field(name="Actions taken", value=actions_taken, inline=False)

        if ActionEnum.DELETE not in flags:
            embed.description = f"[Review incident]({self.message.jump_url})"
        else:
            embed_paginate(embed, "Message Content", msg.clean_content)

        msg = await self.config.mod_channel.send(content="" if kwargs.get("silent") else "@here", embed=embed)
        # Add clickable check mark for other mods.
        await msg.add_reaction("\N{WHITE HEAVY CHECK MARK}")


class DeleteAction(BaseAction):
    __slots__ = ()

    async def send_mod_log_info(self):
        embed = discord.Embed(colour=discord.Colour.orange())
        embed.title = "\U00002757 Automatic deletion"
        embed.timestamp = datetime.datetime.utcnow()
        await self.config.modlog.send(embed=embed)

    @wrap_exception
    async def apply(self, **kwargs):
        await self.message.delete()
        await self.send_mod_log_info()


class MuteAction(BaseAction):
    __slots__ = ()

    @wrap_exception
    async def apply(self, **kwargs):
        author = self.message.author
        await author.add_roles(discord.Object(id=self.config.shitpost_role_id))
        # Dispatch as well.
        punishment = Punishment(author.guild, author, self.config.bot.user, ActionType.SHITPOST, "Automatic mute")
        self.config.bot.dispatch("punishment_add", punishment)


class JailAction(BaseAction):
    __slots__ = ()

    @wrap_exception
    async def apply(self, **kwargs):
        author = self.message.author
        await author.add_roles(discord.Object(id=self.config.jailed_role_id))
        # Dispatch as well.
        punishment = Punishment(author.guild, author, self.config.bot.user, ActionType.JAIL, "Automatic jail")
        self.config.bot.dispatch("punishment_add", punishment)


class RespondAction(BaseAction):
    __slots__ = ()

    def process_template(self, content):
        _count = self.entity.counter
        key_mapping = {
            "name": self.message.author.display_name,
            "name_mention": self.message.author.mention,
            "channel": self.message.channel.mention,
            "count_raw": str(_count),
            "count": 'once' if _count == 1 else format(Plural(_count), 'time')
        }

        def repl(m):
            return key_mapping.get(m[1], m[0])

        return re.sub(r"\$(\w+)", repl, content)

    async def apply(self, **kwargs):
        response = kwargs.get("response")
        if not response:
            raise ActionFailed("No response set")

        content = self.process_template(response)
        await self.message.channel.send(content, delete_after=kwargs.get("delete_after"))


class ActionEnum(IntFlag):
    NOTHING = 0
    DELETE = 1
    NOTIFY = 2
    MUTE = 4
    JAIL = 8
    RESPOND = 16

    ALL = DELETE | NOTIFY | MUTE | JAIL | RESPOND

    @property
    def all_flags(self):
        flags, _ = _decompose(self.__class__, self._value_)
        return flags

    def get_name(self):
        if self._name_ is not None:
            return self._name_

        members = self.all_flags
        if len(members) == 1 and members[0]._name_ is None:
            return repr(members[0]._value_)
        else:
            return ", ".join(str(m._name_ or m._value_) for m in members)


lookup = {
    ActionEnum.NOTIFY: NotifyAction,
    ActionEnum.DELETE: DeleteAction,
    ActionEnum.MUTE: MuteAction,
    ActionEnum.JAIL: JailAction,
    ActionEnum.RESPOND: RespondAction
}


class GuildFilter:
    __slots__ = ("bot", "guild", "guild_only", "channels", "users", "_cs_all_entities")

    def __init__(self, data, guild_id, bot):
        self.bot = bot
        self.guild = self.bot.get_guild(guild_id)
        self.guild_only = defaultdict(set)
        self.channels = defaultdict(set)
        self.users = defaultdict(set)
        self.group_entities(data)

    async def fetch_mod_config(self):
        cog = self.bot.get_cog("Event")
        return cog and await cog.get_guild_config(self.guild.id)

    def group_entities(self, data):
        resolver = {
            "guild": self.guild_only,
            "member": self.users,
            "channel": self.channels
        }

        for record in data:
            bucket = resolver[record["entity_type"]]
            entity = FilterEntity.from_record(record, self.bot)
            bucket[entity.entity_id].add(entity)

    @staticmethod
    def normaliser(string):
        return ''.join(c for c in unicodedata.normalize('NFKD', string) if unicodedata.category(c) != 'Mn')

    @discord.utils.cached_slot_property("_cs_all_entities")
    def all_entities(self):
        all_ = {e for e_set in {**self.guild_only, **self.channels, **self.users}.values() for e in e_set}
        return sorted(all_, key=lambda e: e.id)

    async def feed(self, message):
        async def process_regexps(*, entities):
            for entity in entities:
                if re.search(entity.regex, content):
                    # Apply actions.
                    await entity.apply_all(message, config=config)

        config = await self.fetch_mod_config()
        # Strip accents and other junk.
        content = self.normaliser(message.content)

        if self.guild_only:
            await process_regexps(entities=self.guild_only.get(self.guild.id))

        if self.channels:
            if channel := self.channels.get(message.channel.id):
                await process_regexps(entities=channel)

        if self.users:
            if user := self.users.get(message.author.id):
                await process_regexps(entities=user)


class FilterEntity:
    __slots__ = ("id", "guild", "actions", "action_type", "entity_type", "entity_id",
                 "regex", "created", "bot", "kwargs", "_meta_cache", "counter", "_cs_meta")

    @classmethod
    def from_record(cls, record, bot):
        self = cls()

        self.id = record["id"]
        self.action_type = ActionEnum(record["action"])
        self.actions = tuple(lookup[action] for action in self.action_type.all_flags)
        self.regex = record["regex"]
        self.created = record["created"]
        self.entity_id = record["entity_id"]
        self.entity_type = record["entity_type"]
        self.kwargs = record["extra"] or {}

        self.bot = bot
        self.guild = record["guild_id"] and self.bot.get_guild(record["guild_id"])
        self.counter = 0
        return self

    @property
    def representation(self):
        guild = self.guild
        if self.entity_type == "guild":
            return guild

        return getattr(guild, f"get_{self.entity_type}")(self.entity_id)

    async def apply_all(self, message, *, config):
        if not self.actions:
            self.bot.logger.warn(f"No actions defined for entity {self!r}")
            return

        self.counter += 1
        for klass in self.actions:
            action = klass(message, self, config)
            try:
                await action.apply(**self.kwargs)
            except ActionFailed as e:
                self.bot.logger.warn(f"Filter trigger failed for {action.__class__.__name__}: {e}")

    @discord.utils.cached_slot_property("_cs_meta")
    def meta(self):
        items = (f"{' '.join(map(str.title, attr.split('_')))}: {val}" for attr, val in self.kwargs.items())
        extra = "\n".join(items)
        return str(self), f"Action: {self.action_type.get_name()}\nRegex: `{self.regex}`\n{extra}"

    def __str__(self):
        return f"[{self.id}] {self.created:%d/%m/%Y} - {self.representation}"

    def __repr__(self):
        fmt = "<FilterEntity id=<{0.id} type='{0.entity_type}' action='{0.action_type}' regex='{0.regex}'>"
        return fmt.format(self)


class Scope(commands.Converter):
    async def convert(self, ctx, argument):
        val = None

        argument = argument.lower()
        if argument in ("guild", "g", "server"):
            # Short-cut
            return "guild", [ctx.guild]
        elif argument in ("user", "u", "member"):
            val = "Member"
        elif argument in ("channel", "c"):
            val = "TextChannel"

        if not val:
            raise commands.BadArgument(f'Unknown scope "{argument}".')

        def check(m):
            return len(m.content) <= 100 and m.channel == ctx.channel and m.author == ctx.author

        messages = [await ctx.send(f"It looks like you want to change the scope to `{val.title()}`.")]
        needs_conversion = []

        # Hard-cap at 20
        for i in range(1, 21):
            messages.append(await ctx.send(f'Provide up to {21 - i} instances of your selected entity'
                                           f' or cancel with `{ctx.prefix}cancel`.'))

            entry = await ctx.bot.wait_for('message', timeout=60.0, check=check)
            if entry is None:
                break

            messages.append(entry)
            if entry.clean_content.startswith(f'{ctx.prefix}cancel'):
                break

            needs_conversion.append(entry.content)

        try:
            await ctx.channel.delete_messages(messages)
        except discord.HTTPException:
            pass

        # Try to convert all entities to our type.
        converted = []
        converter = getattr(commands, f"{val}Converter")()
        for entry in needs_conversion:
            try:
                converted.append(await converter.convert(ctx, entry))
            except discord.ext.commands.ConversionError:
                pass

        return val, converted


class Filtering(Cog):
    @cache()
    async def get_active_filters(self, guild_id):
        query = "SELECT * FROM spamfilter WHERE guild_id = $1 ORDER BY id"
        async with self.bot.pool.acquire() as con:
            records = await con.fetch(query, guild_id)
            return records and GuildFilter(records, guild_id, self.bot)

    async def filter_message(self, message):
        if isinstance(message.author, discord.User):
            return

        if message.guild is None or message.author.bot:
            return

        if message.author.guild_permissions.manage_guild:
            # Mod, we don't care about them.
            return

        spam_filter = await self.get_active_filters(message.guild.id)
        if not spam_filter:
            return

        await spam_filter.feed(message)

    @Cog.listener()
    async def on_message(self, message):
        await self.filter_message(message)

    @Cog.listener()
    async def on_message_edit(self, _, after):
        await self.filter_message(after)

    @commands.group(invoke_without_command=True, ignore_extra=False)
    @is_mod()
    async def filter(self, ctx):
        """Shows active filter measures."""
        spam_filter = await self.get_active_filters(ctx.guild.id)
        if not spam_filter:
            return await ctx.send("No active filters are in place.")

        entries = [e.meta for e in spam_filter.all_entities]
        pages = FieldPages(ctx, entries=entries)
        await pages.paginate()

    @filter.error
    async def filter_error(self, ctx, error):
        if isinstance(error, commands.TooManyArguments):
            await ctx.message.add_reaction("\U00002754")

    @filter.command(name="add")
    @is_mod()
    async def filter_add(self, ctx, *, args):
        """Adds a pattern to the list of filters for this guild..

        This command has a powerful "command line" syntax.

        The following options are valid:

        `--regex` or `-r`: Regex that messages should match. (required)
        `--user` or `-u`: Users who should be filtered.
        `--channel` or `-c`: Channels that should be filtered
        `--guild` or `-g`: Whether this filter should be applied to the whole guild.

        Note: You cannot specify other entities if `--guild` was used.

        **Action types** (these fire whenever a filter gets triggered):

        `--notify`: Whether the bot should notify mods in the mod-channel.
        `--delete`: Whether the affected message should get deleted.
        `--mute`: Whether the affected member should get muted.
        `--respond`: Whether the bot should respond with something in the context channel.

        `--jail`: Whether the affected member should be jailed.
        (This automatically notifies the mod-team)

        Note: You need to specify ___at least___ one action type.

        **Optionals** (these work only in combination with preceding action types):

        [NOTIFY]:
        `--silent`: Whether the bot should refrain from pinging when a notify action is triggered.

        [RESPOND]:
        `--respond_delete`: Whether the bot should delete its response afterwards (in seconds, if specified).

        The following string templates are available:
        - `$count` - A pluralised counter that displays how often this filter event was triggered.
        The format is `once | X times`.
        - `$count_raw` - The raw integer value of the counter.
        - `$name` - A formatted string of the user who triggered it.
        - `$name_mention` - A mention of the user who triggered it.
        - `$channel` - A mention of the context channel.
        """

        parser = Args(add_help=False, allow_abbrev=False)
        parser.add_argument("--user", "-u", nargs="+")
        parser.add_argument("--channel", "-c", nargs="+")
        parser.add_argument("--regex", "-r", nargs="+", required=True, action=StoreRegex)
        parser.add_argument("--notify", action="store_true")
        parser.add_argument("--delete", action="store_true")
        parser.add_argument("--silent", action="store_true")
        parser.add_argument("--mute", action="store_true")
        parser.add_argument("--jail", action="store_true")
        parser.add_argument("--respond", nargs="+")
        parser.add_argument("--respond_delete", type=float)
        parser.add_argument("--guild", "-g", action="store_true")

        def split(s):
            lex = shlex.shlex(s, posix=True)
            lex.whitespace_split = True
            lex.escape = ""
            lex.commenters = ''
            return list(lex)

        try:
            args = parser.parse_args(split(args))
        except Exception as e:
            await ctx.send(str(e))
            return

        async def work_entities(converter, list_, t):
            for entity in list_:
                try:
                    en = await converter.convert(ctx, entity)
                    entities.append((t, en))
                except Exception as exc:
                    self.logger.warn(f"Could not parse entity: {exc}")
                    raise

        entities = []
        if args.guild:
            entities.append((EntityType.GUILD, ctx.guild))
        else:
            if args.user:
                conv = commands.MemberConverter()
                await work_entities(conv, args.user, EntityType.MEMBER)

            if args.channel:
                conv = commands.TextChannelConverter()
                await work_entities(conv, args.channel, EntityType.CHANNEL)

        if not entities:
            # Nothing supplied.
            raise commands.BadArgument("You need to specify at least one entity option.")

        extra = {}
        value = 0

        if args.notify:
            value += 2

            if args.silent:
                extra["silent"] = True

        if args.delete:
            value += 1

        if args.mute:
            value += 4

        if args.jail:
            value += 10 if not args.notify else 8

        if args.respond is not None:
            value += 16
            extra["response"] = ' '.join(args.respond)
            if args.respond_delete:
                extra["delete_after"] = args.respond_delete

        if value == 0:
            options = human_join([f'`--{fl.get_name().lower()}`' for fl in ActionEnum.ALL.all_flags])
            raise commands.BadArgument(f"Please provide at least one of {options}.")

        async with ctx.db.transaction():
            # Asyncpg why.
            query = """
                    INSERT INTO spamfilter (guild_id, entity_id, entity_type, regex, action, extra) 
                    SELECT x.guild_id, x.entity_id, x.entity_type, x.regex, x.action, x.extra
                    FROM jsonb_to_recordset($1::jsonb) AS
                    x(guild_id BIGINT, entity_id BIGINT, entity_type TEXT, regex TEXT, action INTEGER, extra JSONB)
                    """

            to_insert = ((ctx.guild.id, e.id, t.name.lower(), args.regex, value, extra) for (t, e) in entities)
            keys = ("guild_id", "entity_id", "entity_type", "regex", "action", "extra")
            await ctx.db.execute(query, [dict(zip(keys, elem)) for elem in to_insert])

        self.get_active_filters.invalidate(self, ctx.guild.id)
        await ctx.send("Successfully added new filter entry.")

    @filter.command(name="remove")
    @is_mod()
    async def filter_remove(self, ctx, id: entry_id):
        """Removes a filter."""
        query = "DELETE FROM spamfilter WHERE id = $1 AND guild_id = $2"
        status = await ctx.db.execute(query, id, ctx.guild.id)
        if status == 'DELETE 0':
            return await ctx.send('Could not delete any filters with that ID.')

        await ctx.send("Successfully deleted filter entry.")
        self.get_active_filters.invalidate(self, ctx.guild.id)

    @filter.command(name="update")
    @is_mod()
    async def filter_update(self, ctx, id: entry_id, *, new_regex):
        """Allows you to update the regex of a filter entry."""
        try:
            to_insert = verify_regex(new_regex)
        except RuntimeError as e:
            return await ctx.send(e)

        query = "UPDATE spamfilter SET regex = $1 WHERE id = $2 AND guild_id = $3"
        status = await ctx.db.execute(query, to_insert, id, ctx.guild.id)
        if status == "UPDATE 0":
            return await ctx.send("Could not update entry. Are you sure it exists?")

        await ctx.send(f"Successfully updated entry. New regex set to `{to_insert}`.")
        self.get_active_filters.invalidate(self, ctx.guild.id)

    @staticmethod
    def analyse_chars(chars):
        def to_string(c):
            digit = f'{ord(c):x}'
            name = unicodedata.name(c, 'Name not found.')
            return f'`\\U{digit:>08}`: {name} - {c} \N{EM DASH} <http://www.fileformat.info/info/unicode/char/{digit}>'

        return '\n'.join(map(to_string, set(chars)))

    @filter.command(name="analyse")
    @is_mod()
    async def filter_analyse(self, ctx, user: Optional[discord.Member] = None, *, message: discord.Message):
        """Analyse user patterns.
        You can provide a message by either using the direct jump link
        or in the format of `{channel-id}-{message-id}`."""
        user = user or message.author
        filters = await self.get_active_filters(message.guild.id)
        if not filters:
            return await ctx.send("Could not find any filters.")

        user_filter = filters.users.get(user.id)
        if not user_filter:
            return await ctx.send(f"Could not find filters for {user}")

        chars_analysed = self.analyse_chars(message.content)
        regex_results = []
        for entity in user_filter:
            regex_results.append((entity.id, re.search(entity.regex, message.content)))

        embed = discord.Embed(title=f"Analysis for message `{message.id}` with user {user}")
        embed.description = "\n".join(f"[{id_}] - {result or 'No match'}" for id_, result in regex_results)

        if len(chars_analysed) < 1019:
            embed.add_field(name="Chars", value=chars_analysed)
        else:
            async with ctx.session.post("http://0x0.st", data={"file": StringIO(chars_analysed)}) as post:
                embed.description = f"Chars [view]({await post.text()})\n" + embed.description
        await ctx.send(embed=embed)

    @filter.command(name="append")
    @is_mod()
    async def filter_append(self, ctx, id: entry_id, *, to_append):
        """Appends a regex to an existing entry.
        Both entries are joined with OR."""
        query = "SELECT regex FROM spamfilter WHERE id = $1 AND guild_id = $2"
        original = await ctx.db.fetchval(query, id, ctx.guild.id)
        if not original:
            return await ctx.send("Could not find filter entry.")

        new_regex = f"{original}|{to_append}"
        try:
            to_insert = verify_regex(new_regex)
        except RuntimeError as e:
            return await ctx.send(e)

        query = "UPDATE spamfilter SET regex = $1 WHERE id = $2 AND guild_id = $3"
        await ctx.db.execute(query, to_insert, id, ctx.guild.id)
        await ctx.send(f"New regex set to `{to_insert}`.")
        self.get_active_filters.invalidate(self, ctx.guild.id)

    @filter.command(name="scope")
    @is_mod()
    async def filter_scope(self, ctx, id: entry_id, *, new_scope: Scope):
        """Changes the scope of a filter entry.
        Available options:
        - guild/server
        - user/member
        - channel"""
        type_, entities = new_scope
        # Oh boy, conversion time. This is a bit more complicated than originally anticipated.
        # We first fetch all information from the existing entry and then delete it.
        query = """DELETE FROM spamfilter WHERE id = $1 AND guild_id = $2 RETURNING regex, action, extra"""
        old_record = await ctx.db.fetchrow(query, id, ctx.guild.id)
        if not old_record:
            return await ctx.send("Could not find an entry with that ID.")

        async with ctx.db.transaction():
            # Asyncpg why.
            query = """
                    INSERT INTO spamfilter (guild_id, entity_id, entity_type, regex, action, extra) 
                    SELECT x.guild_id, x.entity_id, x.entity_type, x.regex, x.action, x.extra
                    FROM jsonb_to_recordset($1::jsonb) AS
                    x(guild_id BIGINT, entity_id BIGINT, entity_type TEXT, regex TEXT, action INTEGER, extra JSONB)
                    """

            to_insert = ((ctx.guild.id, e.id, type_, *old_record) for e in entities)
            keys = ("guild_id", "entity_id", "entity_type", "regex", "action", "extra")
            await ctx.db.execute(query, [dict(zip(keys, elem)) for elem in to_insert])

        await ctx.send(f"Successfully changed scope to `{type_.title()}` for entry {id}.")
        self.get_active_filters.invalidate(self, ctx.guild.id)

    @filter.command(name="debug")
    @is_mod()
    async def filter_debug(self, ctx, id: entry_id, *, string):
        """Debugs a filter"""

        query = "SELECT regex FROM spamfilter WHERE id = $1 AND guild_id = $2"
        regex = await ctx.db.fetchval(query, id, ctx.guild.id)
        if not regex:
            return await ctx.send("Could not find entry.")

        payload = {
            "regex": regex,
            "testString": string,
            "flavor": "python",
            "delimiter": '"',
            "flags": "gm"
        }

        async with ctx.session.post("https://regex101.com/api/regex", data=payload) as resp:
            if resp.status != 200:
                return await ctx.send(f"Could not communicate with Regex101: {resp.status}")
            fragment = (await resp.json())["permalinkFragment"]

        embed = discord.Embed(title="Regex debug result", colour=discord.Colour.blurple())
        embed.description = f"[View on Regex101](https://regex101.com/r/{fragment}/1)"
        embed.add_field(name="Match", value=re.search(regex, string) or "Didn't match")
        await ctx.send(embed=embed)

    @filter.command(name="action")
    @is_mod()
    async def filter_action(self, ctx, id: entry_id):
        """Changes the action types of a filter."""

        # Check whether the filter exists.
        query = "SELECT 1 FROM spamfilter WHERE guild_id = $1 AND id = $2"
        exists = await ctx.db.fetchval(query, ctx.guild.id, id)
        if not exists:
            return await ctx.send("Could not find an entry with this ID.")

        all_ = ActionEnum.ALL
        mapped = sorted([(e.name.title(), e.value) for e in all_.all_flags], key=lambda e: e[1])
        pages = FieldPages(ctx, entries=mapped)
        pages.embed.title = "Action types"

        to_delete = [ctx.message, await ctx.send("Please select action types by adding up the values.")]
        await pages.paginate()
        to_delete.append(pages.message)

        def check(m):
            return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

        async def clean(*, msg=None, delete_after=None):
            if msg:
                await ctx.send(msg, delete_after=delete_after)

            await ctx.channel.delete_messages(to_delete)

        answer = await ctx.bot.wait_for("message", check=check)
        to_delete.append(answer)
        try:
            action = ActionEnum(int(answer.content))
        except ValueError:
            await clean(msg="This doesn't look like an int. Aborting...", delete_after=3)
            return

        if not 1 <= action.value <= all_.value:
            await clean(msg="This value is not within the accepted int range.", delete_after=3)
            return

        extra = {}
        # These are special because they require user interaction
        if ActionEnum.NOTIFY in action:
            is_silent = await ctx.prompt("It looks like you selected `Notify`"
                                         " as one of the new action types for this filter."
                                         " Should the bot notify mods without pinging?")
            if is_silent:
                extra["silent"] = True

        if ActionEnum.RESPOND in action:
            to_delete.append(await ctx.send("`Response` seems to be part of your"
                                            " new actions for this instance."
                                            " Please specify a response now."))

            message = await ctx.bot.wait_for("message", check=check)
            to_delete.append(message)

            if message.attachments:
                response = message.attachments[0].url
            else:
                response = message.content

            extra["response"] = response
            should_delete = await ctx.prompt(f"Alright, got it. Should this response get deleted after"
                                             f" a certain amount of time?")

            if should_delete:
                to_delete.append(await ctx.send("Please specify a float now."))
                message = await ctx.bot.wait_for("message", check=check)
                to_delete.append(message)
                try:
                    delay = float(message.content)
                except ValueError:
                    retry = await ctx.prompt("Hm, this doesn't look like a float. Would you like to retry?")
                    delay = 5.0

                    if retry:
                        message = await ctx.bot.wait_for("message", check=check)
                        to_delete.append(message)
                        try:
                            delay = float(message.content)
                        except ValueError:
                            to_delete.append(await ctx.send("Parsing failed again. Setting default to 5.0 seconds"))
                    else:
                        to_delete.append(await ctx.send("Alright, using 5.0 as default instead."))

                extra["delete_after"] = max(min(delay, 60.0), 5.0)

        embed = discord.Embed(title="Summary of new action types")
        embed.add_field(name="New actions", value="\n".join(e.name.title() for e in action.all_flags))

        def shorten(m):
            return textwrap.shorten(str(m), width=100)

        if extra:
            embed.add_field(name="Extras", value="\n".join(f'{k}: {shorten(v)}' for k, v in extra.items()))

        to_delete.append(await ctx.send(embed=embed))
        confirm = await ctx.prompt("Alright, here's a summary of all new action types."
                                   " Please confirm whether these are correct.")

        if not confirm:
            await clean(msg="Alright, aborting...", delete_after=3)
            return

        query = "UPDATE spamfilter SET action = $1, extra = $2 WHERE id = $3"
        await ctx.db.execute(query, action.value, extra, id)
        self.get_active_filters.invalidate(self, ctx.guild.id)
        await ctx.send(f"Successfully changed action types for entry {id}.")
        await clean()

    @filter.command(name="modifycounter")
    @is_mod()
    async def filter_modify_counter(self, ctx, id: entry_id, *, value: int):
        """Modifies the internal in-memory counter for a filter entity."""
        entries = await self.get_active_filters(ctx.guild.id)
        if not entries:
            return await ctx.send("Could not find filters for this guild. Weird...")

        entity = discord.utils.get(entries.all_entities, id=id)
        if not entity:
            return await ctx.send("Could not find an entity with that ID.")

        entity.counter = value
        await ctx.send(f"Set counter for entity {id} to `{value}`.")


setup = Filtering.setup
