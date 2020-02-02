import discord
from discord.ext import commands
from discord.ext.commands import TextChannelConverter, BadArgument, RoleConverter, VoiceChannelConverter

from cogs.utils import db
from cogs.utils.checks import is_maintainer
from cogs.utils.formatting import Plural
from cogs.utils.meta_cog import Cog


class GuildConfig(db.Table, table_name='guild_config'):
    # The guild id.
    id = db.Column(db.Integer(big=True), primary_key=True)
    # Default log channel for message related events, etc.
    modlog_channel_id = db.DiscordIDColumn()
    # Admin channel.
    mod_channel_id = db.DiscordIDColumn()
    # Default channel on the server. Probably #general in most cases.
    default_channel_id = db.DiscordIDColumn()
    # Bot greeting for ON_MEMBER_ADD
    greeting = db.Column(db.String)
    # Sentinel to check whether the bot is properly set up.
    is_configured = db.Column(db.Boolean, default=False)
    # Member tracker channel.
    tracker_channel_id = db.DiscordIDColumn(nullable=True)


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


class VCChannelConfig(db.Table, table_name='vc_channel_config'):
    id = db.PrimaryKeyColumn()
    # The guild id.
    guild_id = db.Column(db.Integer(big=True))
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


class Config(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        # guild -> bool
        self.currently_configuring = {}

    @commands.group()
    @is_maintainer()
    async def config(self, ctx):
        """The central configuration system of the bot."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help('config')

    @config.command(name="setup")
    @is_maintainer()
    async def config_setup(self, ctx):
        """Sets up the bot to ensure events are properly handled."""

        guild_id = ctx.guild.id
        # First, check if we even to configure the bot.
        is_setup = await ctx.db.fetchval("SELECT is_configured FROM guild_config WHERE id = $1", guild_id)
        if is_setup:
            await ctx.send("This bot is already fully set up. "
                           "If you believe this is a mistake,"
                           " manually clear `is_configured` in `guild_config`.")
            return

        if self.currently_configuring.get(guild_id):
            await ctx.send("The bot is currently being configured...")
            return

        # Lock command.
        self.currently_configuring[guild_id] = True
        # Let's kick things off with basic server information.
        messages = [ctx.message, await ctx.send("Let's start by configuring the basics:")]

        create_tracker_channel = await ctx.prompt("Should we create a tracker channel for server members?")
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

        messages.append(await ctx.send(f"The default channel is going to be {default_channel.mention}."))

        # Admin channel
        question = "Now, please provide the admin channel of the server in the same fashion as before."
        admin_channel = await convert_channel(ctx, messages, question)
        if not admin_channel:
            return

        messages.append(await ctx.send(f"The admin channel is going to be {admin_channel.mention}."))

        # Log channel.
        question = "Where should messages be logged?"
        log_channel = await convert_channel(ctx, messages, question)
        if not log_channel:
            return

        # Shitpost channel.
        fmt = f"The log channel is going to be {log_channel.mention}." \
              "\nAwesome! Now we just need to configure the punishment channels, roles and VC mappings :)\n" \
              "Let's start with punishments. What is the default shitpost channel?"
        shitpost_channel = await convert_channel(ctx, messages, fmt)
        if not shitpost_channel:
            return

        # Shitpost role.
        messages.append(await ctx.send(f"Right, shitposters will be sent to {shitpost_channel.mention}."))
        has_role = await ctx.prompt("Do you already have a shitposter role?")
        if has_role:
            shitpost_role = await has_role_flow_create(ctx, messages, "Shitposter")
        else:
            # Can't be arsed to add another prompt flow lol.
            shitpost_role = await manually_create_role(ctx, "Shitposter", messages)

        # Jailed channel.
        fmt = f"Alright. Shitposters will get {shitpost_role.mention}.\n" \
              "What about jailed people, where will they go?"

        jailed_channel = await convert_channel(ctx, messages, fmt)
        if not jailed_channel:
            return

        # Jailed role.
        messages.append(await ctx.send(f"Right, jailed people will be sent to {jailed_channel.mention}."))
        has_role = await ctx.prompt("Do you already have a jailed role?")
        if has_role:
            jailed_role = await has_role_flow_create(ctx, messages, "Jailed")
        else:
            jailed_role = await manually_create_role(ctx, "Jailed", messages)

        # Configure VC channels.
        # TODO: Add paginator in case the server has more channels.
        channel_names = [vc.name for vc in ctx.guild.voice_channels]
        formatted_vc_channels = "\n".join(f"- {vc}" for vc in channel_names)

        response = "Neat, that's done. Now we need to configure VC channel mappings.\n" \
                   f"The following channels *can* be configured:\n{formatted_vc_channels}"
        messages.append(await ctx.send(response))

        # vc channel -> role id
        vc_mapping = []
        len_channels = len(channel_names)
        for chan_i in range(len_channels):
            formatter = f"Up to {Plural(len_channels - chan_i):channel} left to configure" \
                        f" or cancel with `{ctx.prefix}cancel`." \
                        "\nPlease follow the following format: " \
                        "`<vc channel id or name>:<channel id or name or mention>`"

            messages.append(await ctx.send(formatter))

            entry = await ctx.bot.wait_for("message", timeout=60.0,
                                           check=lambda m: m.author == ctx.author and m.channel == ctx.channel)

            if entry is None:
                break

            messages.append(entry)

            if entry.clean_content.startswith(f"{ctx.prefix}cancel"):
                break

            parsed = await parse_vc_mapping(ctx, entry.content)
            if not parsed:
                # Silently ignore for now.
                continue

            vc_mapping.append(parsed)

        messages.append(await ctx.send("Looks like that was it.\nStarting db transaction..."))

        # Okay, we have a lot we need to commit now.
        exc = ctx.db.execute
        # Create a tracker channel, if wanted.
        channel_id = None
        if create_tracker_channel:
            try:
                # No connecting allowed.
                overwrites = {ctx.guild.default_role: discord.PermissionOverwrite(connect=False)}
                channel = await ctx.guild.create_voice_channel(name=f"Members: {len(ctx.guild.members)}",
                                                               position=0, overwrites=overwrites)
                channel_id = channel.id
            except discord.HTTPException:
                messages.append(await ctx.send("Hmm, could not create a text channel, sorry :("))

        # First, start with basic guild information
        query = """INSERT INTO guild_config 
                   (id, modlog_channel_id, mod_channel_id, default_channel_id, greeting, tracker_channel_id)
                   VALUES ($1, $2, $3, $4, $5, $6)"""

        await exc(query, guild_id, log_channel.id, admin_channel.id,
                  default_channel.id, default_greeting, channel_id)

        # Next, punishments.
        query = """INSERT INTO punishment_config 
                   (id, jailed_role_id, shitpost_role_id, jailed_channel_id, shitpost_channel_id)
                   VALUES ($1, $2, $3, $4, $5)"""
        await exc(query, guild_id, jailed_role.id, shitpost_role.id, jailed_channel.id, shitpost_channel.id)

        # Vc mappings. Simple BULK COPY.
        to_insert = [(guild_id, vc.id, ch.id) for vc, ch in vc_mapping]
        await ctx.db.copy_records_to_table("vc_channel_config", columns=("guild_id", "vc_channel_id", "channel_id"),
                                           records=to_insert)

        # Lastly, set our sentinel.
        await exc("UPDATE guild_config SET is_configured = TRUE WHERE id = $1", guild_id)
        await ctx.channel.delete_messages(messages)
        await ctx.send("Done! Everything should work fine now :)")
        # Could be a potential dead-lock. Maybe consider using a sophomore instead.
        self.currently_configuring[guild_id] = False


setup = Config.setup
