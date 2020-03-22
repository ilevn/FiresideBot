import asyncio
import textwrap
import weakref
from collections import namedtuple
from typing import Optional

import discord
from discord.ext import commands
from discord.ext.commands import TextChannelConverter, BadArgument, RoleConverter, VoiceChannelConverter

from cogs.utils import db, is_maintainer, Plural
from cogs.utils.command_lock import CLock, CommandIsLocked
from cogs.utils.meta_cog import Cog
from cogs.utils.paginators import Pages


class GuildConfig(db.Table, table_name='guild_config'):
    # The guild id.
    id = db.Column(db.Integer(big=True), primary_key=True)
    # Default log channel for message related events, etc.
    modlog_channel_id = db.DiscordIDColumn()
    # Admin channel.
    mod_channel_id = db.DiscordIDColumn()
    # Default channel on the server. Probably #general in most cases.
    default_channel_id = db.DiscordIDColumn()
    # Bot greeting for ON_MEMBER_ADD.
    greeting = db.Column(db.String)
    # Sentinel to check whether the bot is properly set up.
    is_configured = db.Column(db.Boolean, default=False)
    # Member tracker channel.
    tracker_channel_id = db.DiscordIDColumn(nullable=True)
    # The default poll channel.
    poll_channel_id = db.DiscordIDColumn()
    # The verification role for the server.
    verification_role_id = db.DiscordIDColumn()
    # The verification channel for the server.
    verification_channel_id = db.DiscordIDColumn()
    # The message ID of the verification disclaimer.
    verification_message_id = db.DiscordIDColumn()


class PunishmentConfig(db.Table, table_name='punishment_config'):
    # The guild id.
    id = db.Column(db.Integer(big=True), primary_key=True)
    # Jailed role id.
    jailed_role_id = db.DiscordIDColumn()
    # Shitpost role id.
    shitpost_role_id = db.DiscordIDColumn()
    # Jailed channel.
    jailed_channel_id = db.DiscordIDColumn()
    # Shitpost channel.
    shitpost_channel_id = db.DiscordIDColumn()
    # Punishment channel.
    punishment_channel_id = db.DiscordIDColumn()


class VCChannelConfig(db.Table, table_name='vc_channel_config'):
    # The guild id.
    id = db.Column(db.Integer(big=True), primary_key=True)
    # The voice channel id.
    vc_channel_id = db.DiscordIDColumn()
    # The corresponding voice room id.
    channel_id = db.DiscordIDColumn()


async def get_arg_or_return(question, ctx, messages):
    def check(m):
        return len(m.content) <= 100 and m.channel == ctx.channel and m.author == ctx.author

    messages.append(await ctx.send(question))
    result = await ctx.bot.wait_for('message', timeout=60.0, check=check)

    if not result:
        await ctx.send("Aborting....", delete_after=3)
        await ctx.channel.delete_messages(messages)
    else:
        messages.append(result)
        return result.content


async def manually_create_role(ctx, name, messages):
    messages.append(await ctx.send("Manually creating role..."))
    return await ctx.guild.create_role(name=name, reason="Bot setup")


async def parse_vc_mapping(ctx, arg):
    channel_arg, _, role_arg = arg.partition(":")
    if not channel_arg or not role_arg:
        return

    try:
        vc_channel = await VoiceChannelConverter().convert(ctx, channel_arg)
        channel = await TextChannelConverter().convert(ctx, role_arg)
    except BadArgument:
        await ctx.send("Doesn't look like a valid specifier...", delete_after=3)
        return

    return vc_channel, channel


async def convert_channel(ctx, messages, question):
    channel_arg = await get_arg_or_return(question, ctx, messages)

    default_channel = await _try_convert_channel(ctx, channel_arg)
    if not default_channel:
        default_channel_arg = await get_arg_or_return(f"I could not find {channel_arg},"
                                                      " please try again: ", ctx, messages)
        default_channel = await _try_convert_channel(ctx, default_channel_arg)
        if not default_channel:
            messages.append(await ctx.send("Parsing ultimately failed. Aborting setup..."))
            await ctx.channel.delete_messages(messages)
            return

    return default_channel


async def has_role_flow_create(ctx, messages, role_name):
    question = "Great! Would you mind telling me which role that is?" \
               " Either specify the role ID, mention or name."
    role_arg = await get_arg_or_return(question, ctx, messages)
    if not role_arg:
        role = await manually_create_role(ctx, role_name, messages)
    else:
        role = await _try_convert_role(ctx, role_arg)
        if not role:
            role = await manually_create_role(ctx, role_name, messages)

    return role


async def _try_convert_channel(ctx, arg):
    try:
        return await TextChannelConverter().convert(ctx, arg)
    except BadArgument:
        return


async def _try_convert_role(ctx, arg):
    try:
        return await RoleConverter().convert(ctx, arg)
    except BadArgument:
        return


class RoleRange(commands.Converter):
    async def convert(self, ctx, argument):
        first, _, second = argument.partition("..")
        try:
            # arg == idx_a..idx_b
            first_pos = int(first)
            second_pos = int(second)
        except ValueError:
            # arg == role_a..role_b
            first_pos = await _try_convert_role(ctx, first)
            if not first_pos:
                raise BadArgument(f"Invalid first argument {first}")

            second_pos = await _try_convert_role(ctx, second)
            if not second_pos:
                raise BadArgument(f"Invalid second argument {second}")

            first_pos = first_pos.position
            second_pos = second_pos.position

        # At this point we're pretty sure valid role args were provided.
        # Nevertheless, lets validate some stuff.
        # We don't check for pos == -1 for now since that allows clever shortcuts.
        bot_highest = ctx.guild.me.top_role.position
        if any((bad_pos := pos) >= bot_highest for pos in (first_pos, second_pos)):
            raise BadArgument(f"Bad range provided. I cannot assign role at idx {bad_pos}"
                              " due to role hierarchy conflicts.")
        if first_pos <= second_pos:
            raise BadArgument("Bad range provided. Make sure that `first_role.pos > second_role.pos`.")

        return ctx.guild.roles[second_pos:first_pos + 1]


_Channels = namedtuple("_Channels",
                       "with_tracker greeting default_channel admin_channel "
                       "log_channel punishment_channel shitpost_channel jailed_channel verification_channel")


class Config(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        self.currently_configuring = weakref.WeakValueDictionary()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False

        return True

    @staticmethod
    def invalidate_guild_config(ctx):
        # Invalidate to ensure cache integrity.
        event_cog = ctx.bot.get_cog("Event")
        if event_cog:
            return event_cog.get_guild_config.invalidate(event_cog, ctx.guild.id)

    @commands.group()
    @is_maintainer()
    async def config(self, ctx):
        """The central configuration system of the bot."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help('config')

    @staticmethod
    async def _setup_channels(ctx, messages):
        create_tracker_channel = await ctx.prompt("Should we create a tracker channel for server members?",
                                                  timeout=210)
        if create_tracker_channel:
            messages.append(await ctx.send("Alrighty. Consider it done."))

        default_greeting = await get_arg_or_return("What should the default greeting be?", ctx, messages)
        if not default_greeting:
            return

        # Try to convert the arg into a regular channel.
        question = "Lovely. Where should I post the greeting? Either specify a channel ID, mention or name."
        default_channel = await convert_channel(ctx, messages, question)
        if not default_channel:
            return

        messages.append(await ctx.send(f"The default greeting channel is going to be {default_channel.mention}."))

        question = "Where should users be prompted for verification?"
        verification_channel = await convert_channel(ctx, messages, question)
        if not verification_channel:
            return

        messages.append(await ctx.send(f"The default verification is going to be {verification_channel.mention}."))

        # Admin channel
        question = "Now, please provide the admin channel of the server in the same fashion as before."
        admin_channel = await convert_channel(ctx, messages, question)
        if not admin_channel:
            return

        messages.append(await ctx.send(f"The admin channel is going to be {admin_channel.mention}."))

        # Punishment channel.
        question = "Where should punishments be logged?"
        punishment_channel = await convert_channel(ctx, messages, question)
        if not punishment_channel:
            return

        messages.append(await ctx.send(f"The punishments channel is going to be {punishment_channel.mention}."))

        # Log channel.
        question = "Where should messages be logged?"
        log_channel = await convert_channel(ctx, messages, question)
        if not log_channel:
            return

        messages.append(await ctx.send(f"The log channel is going to be {log_channel.mention}."))

        # Shitpost channel.
        shitpost_channel = await convert_channel(ctx, messages, "What is the default shitpost channel?")
        if not shitpost_channel:
            return

        messages.append(await ctx.send(f"Right, shitposters will be sent to {shitpost_channel.mention}."))

        fmt = "Where are jailed people going to be?"
        jailed_channel = await convert_channel(ctx, messages, fmt)
        if not jailed_channel:
            return

        messages.append(await ctx.send(f"Okay, jailed people will be sent to {jailed_channel.mention}."))

        return _Channels(create_tracker_channel, default_greeting, default_channel.id, admin_channel.id,
                         log_channel.id, punishment_channel.id, shitpost_channel.id, jailed_channel.id,
                         verification_channel.id)

    @staticmethod
    async def _setup_roles(ctx, messages):
        # Shitpost role.
        has_role = await ctx.prompt("Do you already have a shitposter role?")
        if has_role:
            shitpost_role = await has_role_flow_create(ctx, messages, "Shitposter")
        else:
            # Can't be arsed to add another prompt flow lol.
            shitpost_role = await manually_create_role(ctx, "Shitposter", messages)

        # Jailed role.
        has_role = await ctx.prompt("Do you already have a jailed role?")
        if has_role:
            jailed_role = await has_role_flow_create(ctx, messages, "Jailed")
        else:
            jailed_role = await manually_create_role(ctx, "Jailed", messages)

        # Jailed role.
        has_role = await ctx.prompt("Do you already have a verification role?")
        if has_role:
            verification_role = await has_role_flow_create(ctx, messages, "Unverified")
        else:
            verification_role = await manually_create_role(ctx, "Unverified", messages)

        return shitpost_role, jailed_role, verification_role

    @staticmethod
    async def _setup_vc_mappings(ctx, messages):
        messages.append(await ctx.send("Now we need to configure VC channel mappings."))
        # Setup paginator.
        channel_names = [vc.name for vc in ctx.guild.voice_channels]
        pages = Pages(ctx, entries=channel_names, use_index=False)
        pages.embed.title = "The following channels can be configured"
        await pages.paginate()
        messages.append(pages.message)

        # vc channel -> role id
        vc_mapping = []
        len_channels = len(channel_names)
        for chan_i in range(len_channels):
            formatter = f"Up to {Plural(len_channels - chan_i):channel} left to configure" \
                        f" or cancel with `{ctx.prefix}cancel`." \
                        "\nPlease follow the following format: " \
                        "`<vc channel id or name>:<channel id or name or mention>`"

            messages.append(await ctx.send(formatter))

            def check(m):
                return m.author == ctx.author and m.channel == ctx.channel

            try:
                entry = await ctx.bot.wait_for("message", timeout=60.0, check=check)
            except asyncio.TimeoutError:
                break

            messages.append(entry)

            if entry.clean_content.startswith(f"{ctx.prefix}cancel"):
                break

            parsed = await parse_vc_mapping(ctx, entry.content)
            if not parsed:
                continue

            vc_mapping.append(parsed)

        return vc_mapping

    # noinspection PyDunderSlots,PyUnresolvedReferences,PyTypeChecker
    @staticmethod
    async def _setup_channel_overwrites(guild, punish_channel_id, shitpost_channel_id,
                                        jailed_channel_id, shitpost_role, jailed_role,
                                        verification_channel_id, verification_role):

        success, skipped, failure = 0, 0, 0
        reason = "Automatic server setup."

        def deny_send_and_react(overwrites):
            overwrites.send_messages = False
            overwrites.add_reactions = False

        async def edit_perms(role, to_edit):
            if not to_edit.is_empty():
                await channel.set_permissions(role, overwrite=to_edit, reason=reason)

        # Jailed -> R:Jailed & Punishments
        # Shitpost -> R:All; W:Shitpost
        # No one has reactions.
        # Verification -> RW:Verification
        # @everyone -> R:everything except Jailed & Verification
        for channel in guild.channels:
            if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
                continue

            perms = channel.permissions_for(guild.me)
            if perms.manage_roles:
                jailed_perms = channel.overwrites_for(jailed_role)
                shitpost_perms = channel.overwrites_for(shitpost_role)
                everyone_perms = channel.overwrites_for(guild.default_role)
                verification_perms = channel.overwrites_for(verification_role)

                if (channel_id := channel.id) == punish_channel_id:
                    deny_send_and_react(everyone_perms)
                    verification_perms.read_messages = False
                elif channel_id == shitpost_channel_id:
                    shitpost_perms.send_messages = True
                    jailed_perms.read_messages = False
                    verification_perms.read_messages = False
                elif channel_id == jailed_channel_id:
                    jailed_perms.read_messages = True
                    everyone_perms.read_messages = False
                elif channel_id == verification_channel_id:
                    verification_perms.read_messages = True
                    everyone_perms.read_messages = False
                else:
                    # View channel or Read channel.
                    jailed_perms.read_messages = False
                    verification_perms.read_messages = False
                    if isinstance(channel, discord.VoiceChannel):
                        shitpost_perms.connect = False
                    else:
                        # Regular channel
                        deny_send_and_react(shitpost_perms)

                try:
                    # This is pretty awful but discord won't let us bulk edit channel perms.
                    await edit_perms(shitpost_role, shitpost_perms)
                    await edit_perms(jailed_role, jailed_perms)
                    await edit_perms(verification_role, verification_perms)
                    await edit_perms(guild.default_role, everyone_perms)
                except discord.HTTPException:
                    failure += 1
                else:
                    success += 1
            else:
                skipped += 1

        return success, failure, skipped

    async def _config_setup(self, ctx):
        """Sets up the bot to ensure events are properly handled."""

        # First, check if we even to configure the bot.
        guild_id = ctx.guild.id
        is_setup = await ctx.db.fetchval("SELECT is_configured FROM guild_config WHERE id = $1", guild_id)
        if is_setup:
            await ctx.send("This bot is already fully set up. "
                           "If you believe this is a mistake,"
                           " manually clear `is_configured` in `guild_config`"
                           " and reload the Event cog.")
            return

        # Release connection since we're going to wait for a bunch of input before actually committing.
        await ctx.release()

        # Let's kick things off with basic server information.
        fmt = """
        Let's start by configuring the basics. First things first: all channel, role and member
        names are case sensitive. If you have a role named `Jailed` and another one named `jailed` 
        and you specified 'jailed', the bot will try to resolve the latter. Keep this in mind while 
        the bot is being set up. :)
        """

        acknowledgement = await ctx.prompt(textwrap.dedent(fmt))
        if not acknowledgement:
            await ctx.send("Aborting...", delete_after=3)
            await ctx.message.delete()
            return

        messages = [ctx.message]
        # Set up basic channel functions.
        ch_cfg = await self._setup_channels(ctx, messages)
        if not ch_cfg:
            await ctx.send("Aborting...", delete_after=3)
            await ctx.channel.delete_messages(messages)
            return

        # Special case a few channels because they're needed later.
        shitpost_chan = ch_cfg.shitpost_channel
        jailed_chan = ch_cfg.jailed_channel
        punish_chan = ch_cfg.punishment_channel
        verif_chan = ch_cfg.verification_channel

        messages.append(await ctx.send("Awesome, you're making great progress. Next step, server roles!"))
        shitpost_role, jailed_role, verification_role = await self._setup_roles(ctx, messages)
        # Configure all permission overwrites for the punishment role.
        messages.append(await ctx.send("Roles, check! Setting up permission overwrites for punishment roles..."
                                       " (This might take a while)"))

        async with ctx.typing():
            args = (ctx.guild, punish_chan, shitpost_chan, jailed_chan, shitpost_role, jailed_role,
                    verif_chan, verification_role)
            success, failure, skipped = await self._setup_channel_overwrites(*args)
            total = success + failure + skipped
            messages.append(await ctx.send(f"Attempted to update {total} channel permissions. "
                                           f"[Updated: {success}, Failed: {failure}, Skipped: {skipped}]"))

        # Configure VC channels.
        vc_mapping = await self._setup_vc_mappings(ctx, messages)
        messages.append(await ctx.send("Looks like that was it.\nStarting db transaction..."))
        # Create a tracker channel, if wanted.
        channel_id = None
        if ch_cfg.with_tracker:
            try:
                # No connecting allowed.
                overwrites = {ctx.guild.default_role: discord.PermissionOverwrite(connect=False)}
                channel = await ctx.guild.create_voice_channel(name=f"Members: {len(ctx.guild.members)}",
                                                               position=0, overwrites=overwrites)
                channel_id = channel.id
            except discord.HTTPException:
                messages.append(await ctx.send("Hmm, could not create the tracker channel, sorry :("))

        # Reacquire.
        await ctx.acquire()
        exc = ctx.db.execute
        # First, start with basic guild information
        query = """INSERT INTO guild_config 
                   (id, modlog_channel_id, mod_channel_id, default_channel_id, greeting, tracker_channel_id,
                   verification_channel_id, verification_role_id)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"""

        await exc(query, guild_id, ch_cfg.log_channel, ch_cfg.admin_channel, ch_cfg.default_channel,
                  ch_cfg.greeting, channel_id, verif_chan, verification_role.id)

        # Next, punishments.
        query = """INSERT INTO punishment_config 
                   (id, jailed_role_id, shitpost_role_id, jailed_channel_id,
                    shitpost_channel_id, punishment_channel_id)
                   VALUES ($1, $2, $3, $4, $5, $6)"""

        await exc(query, guild_id, jailed_role.id, shitpost_role.id, jailed_chan, shitpost_chan, punish_chan)

        # Vc mappings. Simple BULK COPY.
        to_insert = [(guild_id, vc.id, ch.id) for vc, ch in vc_mapping]
        await ctx.db.copy_records_to_table("vc_channel_config", columns=("id", "vc_channel_id", "channel_id"),
                                           records=to_insert)

        # Lastly, set our sentinel.
        await exc("UPDATE guild_config SET is_configured = TRUE WHERE id = $1", guild_id)
        await ctx.channel.delete_messages(messages)
        await ctx.send("Done! Everything should work fine now :)")
        # Refresh guild config.
        self.invalidate_guild_config(ctx)

    @config.command(name="setup")
    @is_maintainer()
    async def config_setup(self, ctx):
        if (lock := self.currently_configuring.get(ctx.guild.id)) is None:
            self.currently_configuring[ctx.guild.id] = lock = CLock()
        try:
            with lock:
                await self._config_setup(ctx)
        except CommandIsLocked:
            await ctx.send(f"This command is currently in use.")

    async def _bulk_add_roles(self, ctx, roles, category=None):
        async with ctx.db.transaction():
            query = "SELECT role_id FROM roles WHERE guild_id = $1;"
            records = await ctx.db.fetch(query, ctx.guild.id)

            # Don't insert duplicate roles.
            current_roles = {r[0] for r in records}
            guild_id = ctx.guild.id
            to_insert = [(guild_id, r.id, category) for r in roles if r.id not in current_roles]

            # BULK COPY.
            await ctx.db.copy_records_to_table('roles', columns=('guild_id', 'role_id', 'category'),
                                               records=to_insert)

            # Invalidate cache, if applicable.
            if cog := self.bot.get_cog("Community"):
                cog.get_pool_roles.invalidate_containing(f"{ctx.guild.id!r}")

    # Maybe change perm system.
    @config.group(name="roles")
    @is_maintainer()
    async def _roles(self, ctx):
        """Handles assignable roles for the server """
        pass

    @_roles.command(name="enable", aliases=["asar", "add"])
    async def roles_enable(self, ctx, category: Optional[str] = None, *roles: discord.Role):
        """Add roles to the central rolepool of the server.
        This will append roles to the existing rolepool, if any are found.
        """

        if not roles:
            return await ctx.send("Missing roles to add.")

        if category:
            category = category.lower()

        await self._bulk_add_roles(ctx, roles, category)
        await ctx.send("Updated rolepool.")

    @_roles.command(name="batchadd", usage="<category> <first_pos|role_a>..<second_pos|role_b>")
    async def roles_add_batch(self, ctx, category, role_range: RoleRange):
        """Batch inserts new roles into the available rolepool with a given category.
        Role ranges are inclusive and should be provided in descending order.
        """
        await self._bulk_add_roles(ctx, role_range, category)
        await ctx.send("Updated rolepool.")

    @_roles.command(name="disable")
    async def roles_disable(self, ctx, *roles: discord.Role):
        """Remove roles from the central rolepool of the server.
        This will remove roles from the existing rolepool, if any are found.
        """

        if not roles:
            return await ctx.send("Missing roles to remove.")

        query = "DELETE FROM roles WHERE guild_id = $1 AND role_id = ANY($2::bigint[])"
        await ctx.db.execute(query, ctx.guild.id, [c.id for c in roles])

        if cog := ctx.bot.get_cog("Community"):
            # Flush cache, if cog is loaded.
            cog.get_pool_roles.invalidate_containing(f"{ctx.guild.id!r}")

        await ctx.send("Updated rolepool.")

    async def set_config(self, ctx, key, value):
        query = f"UPDATE guild_config SET {key} = $1 WHERE id = $2 AND is_configured = TRUE"
        async with ctx.db.transaction():
            status = await ctx.db.execute(query, value, ctx.guild.id)
            if status == "UPDATE 0":
                raise RuntimeError("No configured guild with this ID was found."
                                   " Please use `config setup` to configure the database properly.")

            self.invalidate_guild_config(ctx)

    @config.command(name="greeting")
    async def config_greeting(self, ctx, *, greeting):
        """Set the default greeting for this server.
        This commands supports the following templates:
        - `$name` - A formatted string of the member that joined.
        - `$name_mention` - A mention of the member that joined.
        - `$server` - The server name.
        """

        try:
            await self.set_config(ctx, "greeting", greeting)
            await ctx.send("\N{THUMBS UP SIGN}")
        except RuntimeError as e:
            await ctx.send(e)


setup = Config.setup
