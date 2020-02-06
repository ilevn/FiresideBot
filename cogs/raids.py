import asyncio
import datetime
from collections import defaultdict
from enum import IntEnum
from typing import Optional

import discord
from discord.ext import commands

from cogs.utils import human_timedelta, is_mod, db
from cogs.utils.cache import cache
from cogs.utils.meta_cog import Cog


class RaidMode(IntEnum):
    off = 0
    on = 1
    strict = 2

    def __str__(self):
        return self.name


class GuildRaidConfig(db.Table, table_name="guild_raid_config"):
    id = db.Column(db.Integer(big=True), primary_key=True)
    # The current raid mode the server is in.
    raid_mode = db.Column(db.Integer(small=True))
    # Broadcast channel for raid messages.
    broadcast_channel = db.Column(db.Integer(big=True))
    # Amount of mentions a member is allowed to use before
    # being punished in raid mode.
    mention_count = db.Column(db.Integer(small=True))
    # Channels excluded from mention bans.
    safe_mention_channel_ids = db.Column(db.Array(db.Integer(big=True)))


class CooldownByContent(commands.CooldownMapping):
    def _bucket_key(self, message):
        return message.channel.id, message.content


def is_new(member):
    now = datetime.datetime.utcnow()
    seven_days_ago = now - datetime.timedelta(days=7)
    ninety_days_ago = now - datetime.timedelta(days=90)
    return member.created_at > ninety_days_ago and member.joined_at > seven_days_ago


class SpamChecker:
    def __init__(self):
        self.by_content = CooldownByContent.from_cooldown(15, 17.0, commands.BucketType.member)
        self.by_user = commands.CooldownMapping.from_cooldown(10, 12.0, commands.BucketType.user)
        self.last_join = None
        self.new_user = commands.CooldownMapping.from_cooldown(30, 35.0, commands.BucketType.channel)

    def is_spamming(self, message):
        if message.guild is None:
            return False

        current = message.created_at.replace(tzinfo=datetime.timezone.utc).timestamp()

        if is_new(message.author):
            new_bucket = self.new_user.get_bucket(message)
            if new_bucket.update_rate_limit(current):
                return True

        user_bucket = self.by_user.get_bucket(message)
        if user_bucket.update_rate_limit(current):
            return True

        content_bucket = self.by_content.get_bucket(message)
        if content_bucket.update_rate_limit(current):
            return True

        return False

    def is_fast_join(self, member):
        joined = member.joined_at or datetime.datetime.utcnow()
        if self.last_join is None:
            self.last_join = joined
            return False
        is_fast = (joined - self.last_join).total_seconds() <= 2.0
        self.last_join = joined
        return is_fast


class RaidConfig:
    __slots__ = ("bot", "id", "raid_mode", "broadcast_channel_id",
                 "mention_count", "safe_mention_channel_ids")

    @classmethod
    async def from_record(cls, record, bot):
        self = cls()

        self.bot = bot
        self.id = record["id"]
        self.raid_mode = record["raid_mode"]
        self.broadcast_channel_id = record["broadcast_channel"]
        self.mention_count = record["mention_count"]
        self.safe_mention_channel_ids = set(record["safe_mention_channel_ids"] or [])
        return self

    @property
    def broadcast_channel(self):
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(self.broadcast_channel_id)


# This is inspired by Danny"s raid handling and
# includes partial source code licenced under MIT.
class RaidControl(Cog):
    """Central raid control of the bot."""

    def __init__(self, bot):
        super().__init__(bot)
        self._spam_checker = defaultdict(SpamChecker)
        self._disable_lock = asyncio.Lock(loop=bot.loop)

    @cache()
    async def get_raid_config(self, guild_id):
        query = """SELECT * FROM guild_raid_config WHERE id=$1;"""
        async with self.bot.pool.acquire() as con:
            record = await con.fetchrow(query, guild_id)
            return record and await RaidConfig.from_record(record, self.bot)

    async def check_raid(self, config, guild_id, member, message):
        if config.raid_mode != RaidMode.strict.value:
            return

        checker = self._spam_checker[guild_id]
        if not checker.is_spamming(message):
            return

        try:
            await member.ban(reason="Auto-ban from spam (strict raid mode ban)")
        except discord.HTTPException:
            self.logger.info(
                f"[Raid Mode] Failed to ban {member} (ID: {member.id}) "
                f"from server {member.guild} via strict mode.")
        else:
            self.logger.info(f"[Raid Mode] Banned {member} (ID: {member.id})"
                             f" from server {member.guild} via strict mode.")

    @Cog.listener()
    async def on_message(self, message):
        author = message.author
        if author.id in (self.bot.user.id, self.bot.owner_id):
            return

        if message.guild is None:
            return

        if not isinstance(author, discord.Member):
            return

        if author.bot:
            return

        # This only applies to members without roles for now.
        if len(author.roles) > 0:
            return

        guild_id = message.guild.id
        if (config := await self.get_raid_config(guild_id)) is None:
            return

        if not config.raid_mode:
            return

        await self.check_raid(config, guild_id, author, message)

        if len(message.mentions) <= 3:
            return

        if not config.mention_count:
            return

        # Check if it meets the thresholds required.
        mention_count = sum(not m.bot and m.id != author.id for m in message.mentions)
        if mention_count < config.mention_count:
            return

        if message.channel.id in config.safe_mention_channel_ids:
            return

        try:
            await author.ban(reason=f"Spamming mentions ({mention_count} mentions)")
        except Exception:
            self.logger.info(f"Failed to autoban member {author} (ID: {author.id}) in guild ID {guild_id}")
        else:
            await message.channel.send(f"Banned {author} (ID: {author.id}) for spamming {mention_count} mentions.")
            self.logger.info(f"Member {author} (ID: {author.id}) has been autobanned from guild ID {guild_id}")

    async def on_member_join(self, member):
        guild_id = member.guild.id
        config = await self.get_raid_config(guild_id)
        if config is None:
            return

        if not config.raid_mode:
            return

        now = datetime.datetime.utcnow()

        new_mem = member.created_at > (now - datetime.timedelta(days=7))
        checker = self._spam_checker[guild_id]

        # Broadcast message.
        title = "Member Joined"
        if checker.is_fast_join(member):
            # Red
            colour = 0xdd5f53
            if new_mem:
                title = "Member Joined (Very New Member)"
        else:
            # Green.
            colour = 0x53dda4

            if new_mem:
                # Yellow.
                colour = 0xdda453
                title = "Member Joined (Very New Member)"

        e = discord.Embed(title=title, colour=colour)
        e.set_author(name=str(member), icon_url=member.avatar_url).timestamp = now
        e.add_field(name="ID", value=member.id)
        e.add_field(name="Joined", value=member.joined_at)
        e.add_field(name="Created", value=human_timedelta(member.created_at), inline=False)

        if not config.broadcast_channel:
            return
        try:
            await config.broadcast_channel.send(embed=e)
        except discord.Forbidden:
            async with self._disable_lock:
                await self.disable_raid_mode(guild_id)

    @commands.group(aliases=["raids"], invoke_without_command=True)
    @is_mod()
    async def raid(self, ctx):
        """Controls raid mode on the server.
        Calling this command with no arguments will show the current raid
        mode information.
        You must have Manage Server permissions to use this command or
        its subcommands.
        """

        query = "SELECT raid_mode, broadcast_channel FROM guild_raid_config WHERE id=$1;"

        row = await ctx.db.fetchrow(query, ctx.guild.id)
        if row is None:
            fmt = "Raid Mode: off\nBroadcast Channel: None"
        else:
            ch = f"<#{row[1]}>" if row[1] else None
            mode = RaidMode(row[0]) if row[0] is not None else RaidMode.off
            fmt = f"Raid Mode: {mode}\nBroadcast Channel: {ch}"

        await ctx.send(fmt)

    @raid.command(name="on", aliases=["enable", "enabled"])
    @is_mod()
    async def raid_on(self, ctx, *, channel: discord.TextChannel = None):
        """Enables basic raid mode on the server.
        When enabled, server verification level is set to double table flip
        levels and allows the bot to broadcast new members joining
        to a specified channel.
        If no channel is given, the bot will broadcast join
        messages on the channel this command was used in.
        """

        channel = channel or ctx.channel
        await self.enable_raid_mode(ctx, channel, RaidMode.on)

    @raid.command(name="strict")
    @is_mod()
    async def raid_strict(self, ctx, disable_invites: Optional[bool] = False, *, channel: discord.TextChannel = None):
        """Enables strict raid mode on the server.
        Strict mode is similar to regular enabled raid mode, with the added
        benefit of auto-banning members that are spamming. The threshold for
        spamming depends on a per-content basis and also on a per-user basis
        of 15 messages per 17 seconds.
        """
        channel = channel or ctx.channel

        if disable_invites:
            await ctx.send("Purging all server invites...")
            # Purge time.
            invites = (inv for inv in await ctx.guild.invites() if not inv.revoked)
            for inv in invites:
                await inv.delete(reason="Raid prevention")

        perms = ctx.me.guild_permissions
        if not (perms.kick_members and perms.ban_members):
            return await ctx.send("\N{NO ENTRY SIGN} I do not have permissions to kick and ban members.")

        await self.enable_raid_mode(ctx, channel, RaidMode.strict)

    async def enable_raid_mode(self, ctx, channel, mode: RaidMode):
        try:
            await ctx.guild.edit(verification_level=discord.VerificationLevel.double_table_flip)
        except discord.HTTPException:
            await ctx.send("\N{WARNING SIGN} Could not set verification level.")

        query = """INSERT INTO guild_raid_config (id, raid_mode, broadcast_channel)
                       VALUES ($1, $2, $3) ON CONFLICT (id)
                       DO UPDATE SET
                            raid_mode = EXCLUDED.raid_mode,
                            broadcast_channel = EXCLUDED.broadcast_channel;
                    """

        await ctx.db.execute(query, ctx.guild.id, mode.value, channel.id)
        self.get_raid_config.invalidate(self, ctx.guild.id)
        await ctx.send(f"Raid mode enabled. Broadcasting join messages to {channel.mention}.")

    async def disable_raid_mode(self, guild_id):
        query = """INSERT INTO guild_raid_config (id, raid_mode, broadcast_channel)
                   VALUES ($1, $2, NULL) ON CONFLICT (id)
                   DO UPDATE SET
                        raid_mode = EXCLUDED.raid_mode,
                        broadcast_channel = NULL;
                """

        await self.bot.pool.execute(query, guild_id, RaidMode.off.value)
        self._spam_checker.pop(guild_id, None)
        self.get_raid_config.invalidate(self, guild_id)

    @raid.command(name="off", aliases=["disable", "disabled"])
    @is_mod()
    async def raid_off(self, ctx):
        """Disables raid mode on the server.
        When disabled, the server verification levels are set
        back to Low levels and the bot will stop broadcasting
        join messages.
        """

        try:
            await ctx.guild.edit(verification_level=discord.VerificationLevel.low)
        except discord.HTTPException:
            await ctx.send("\N{WARNING SIGN} Could not set verification level.")

        await self.disable_raid_mode(ctx.guild.id)
        await ctx.send("Raid mode disabled. No longer broadcasting join messages.")

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    @is_mod()
    async def mentionspam(self, ctx, count: int = None):
        """Enables auto-banning accounts that spam mentions.
        If a message contains `count` or more mentions, then the
        bot will automatically attempt to auto-ban the member.
        The `count` must be greater than 3. If the `count` is 0,
        then this is disabled.
        This only applies for user mentions. Everyone or Role
        mentions are not included.
        To use this command you must have the Ban Members permission.
        """

        if count is None:
            query = """SELECT mention_count, COALESCE(safe_mention_channel_ids, '{}') AS channel_ids
                       FROM guild_raid_config
                       WHERE id=$1;
                    """

            row = await ctx.db.fetchrow(query, ctx.guild.id)
            if row is None or not row["mention_count"]:
                return await ctx.send("This server has not set up mention spam banning.")

            ignores = ", ".join(f"<#{e}>" for e in row["channel_ids"]) or "None"
            return await ctx.send(f"- Threshold: {row['mention_count']} mentions\n- Ignored Channels: {ignores}")

        if count == 0:
            query = """UPDATE guild_raid_config SET mention_count = NULL WHERE id=$1;"""
            await ctx.db.execute(query, ctx.guild.id)
            self.get_raid_config.invalidate(self, ctx.guild.id)
            return await ctx.send("Auto-banning members has been disabled.")

        if count <= 3:
            await ctx.send("\N{NO ENTRY SIGN} Auto-ban threshold must be greater than three.")
            return

        query = """INSERT INTO guild_raid_config (id, mention_count, safe_mention_channel_ids)
                   VALUES ($1, $2, '{}')
                   ON CONFLICT (id) DO UPDATE SET
                       mention_count = $2;
                """
        await ctx.db.execute(query, ctx.guild.id, count)
        self.get_raid_config.invalidate(self, ctx.guild.id)
        await ctx.send(f"Now auto-banning members that mention more than {count} users.")

    @mentionspam.command(name="ignore", aliases=["bypass"])
    @commands.guild_only()
    @is_mod()
    async def mentionspam_ignore(self, ctx, *channels: discord.TextChannel):
        """Specifies what channels ignore mentionspam auto-bans.
        If a channel is given then that channel will no longer be protected
        by auto-banning from mention spammers.
        To use this command you must have the Ban Members permission.
        """

        query = """UPDATE guild_raid_config
                   SET safe_mention_channel_ids =
                       ARRAY(SELECT DISTINCT * FROM unnest(COALESCE(safe_mention_channel_ids, '{}') || $2::bigint[]))
                   WHERE id = $1;
                """

        if not channels:
            return await ctx.send("Missing channels to ignore.")

        channel_ids = [c.id for c in channels]
        await ctx.db.execute(query, ctx.guild.id, channel_ids)
        self.get_raid_config.invalidate(self, ctx.guild.id)
        await ctx.send(f"Mentions are now ignored on {', '.join(c.mention for c in channels)}.")

    @mentionspam.command(name="unignore", aliases=["protect"])
    @commands.guild_only()
    @is_mod()
    async def mentionspam_unignore(self, ctx, *channels: discord.TextChannel):
        """Specifies what channels to take off the ignore list.
        To use this command you must have the Ban Members permission.
        """

        if not channels:
            return await ctx.send("Missing channels to protect.")

        query = """UPDATE guild_raid_config
                   SET safe_mention_channel_ids =
                       ARRAY(SELECT element FROM unnest(safe_mention_channel_ids) AS element
                             WHERE NOT(element = ANY($2::bigint[])))
                   WHERE id = $1;
                """

        await ctx.db.execute(query, ctx.guild.id, [c.id for c in channels])
        self.get_raid_config.invalidate(self, ctx.guild.id)
        await ctx.send("Updated mentionspam ignore list.")


setup = RaidControl.setup
