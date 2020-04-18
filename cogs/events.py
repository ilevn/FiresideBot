import json
import re
import typing
from collections import namedtuple, defaultdict
from datetime import datetime
from difflib import Differ

import discord
from discord import Message, Member

from cogs.utils import human_timedelta, Plural, embed_paginate
from cogs.utils.cache import cache, ExpiringCache
from cogs.utils.meta_cog import Cog
from cogs.utils.paginators import BulkDeletePaginator
from cogs.utils.punishment import Punishment, ActionType


def is_outside_voice(state):
    return state.channel is None


def is_inside_voice(state):
    return state.channel is not None


StateInformation = namedtuple("StateInformation", "roles muted")


def get_diff(before, after):
    dif = list(Differ().compare(before.split(' '), after.split(' ')))
    return " ".join((f'__{i[2:]}__' if i[0] == '+' else i[2:]) for i in dif if not i[0] in '-?')


_SENTINEL = object()


class EventConfig:
    # These slots match with our db columns, which is why the assignment works.
    __slots__ = ('bot', 'id', 'modlog_channel_id', 'mod_channel_id', 'default_channel_id',
                 'greeting', 'shitpost_channel_id', 'jailed_channel_id', 'shitpost_role_id',
                 'jailed_role_id', 'mappings', 'tracker_channel_id', 'poll_channel_id',
                 'punishment_channel_id', 'verification_channel_id', 'verification_role_id',
                 'verification_message_id')

    @classmethod
    async def from_record(cls, record, bot, vc_mappings):
        self = cls()

        self.bot = bot
        # vc channel -> txt channel
        self.mappings = dict(vc_mappings)
        for val in EventConfig.__slots__:
            actual_val = record.get(val, _SENTINEL)
            if actual_val is _SENTINEL:
                continue

            setattr(self, val, actual_val)

        return self

    def _resolve_channel(self, channel_id):
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(channel_id)

    @property
    def modlog(self):
        return self._resolve_channel(self.modlog_channel_id)

    @property
    def mod_channel(self):
        return self._resolve_channel(self.mod_channel_id)

    @property
    def default_channel(self):
        return self._resolve_channel(self.default_channel_id)

    @property
    def tracker_channel(self):
        if not self.tracker_channel_id:
            return

        return self._resolve_channel(self.tracker_channel_id)

    @property
    def punishment_channel(self):
        return self._resolve_channel(self.punishment_channel_id)

    @property
    def verification_channel(self):
        return self._resolve_channel(self.verification_channel_id)


class Event(Cog):
    """
    Event cog for message handling.
    """

    def __init__(self, bot):
        super().__init__(bot)
        # Save their roles for 60 minutes.
        self._recent_bad_nicks = set()
        self._member_state = defaultdict(lambda: ExpiringCache(3600))
        self._pending_mute = defaultdict(set)

    @cache()
    async def get_guild_config(self, guild_id) -> typing.Optional[EventConfig]:
        query = """SELECT * FROM guild_config gc JOIN punishment_config pc ON gc.id = pc.id WHERE pc.id = $1"""

        async with self.bot.pool.acquire() as con:
            record = await con.fetchrow(query, guild_id)
            if not record:
                return

            # Also fetch vc mappings.
            vc_mapping_query = "SELECT vc_channel_id, channel_id FROM vc_channel_config WHERE id = $1"
            mappings = await con.fetch(vc_mapping_query, guild_id)
            return record and await EventConfig.from_record(record, self.bot, mappings)

    @Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        config = await self.get_guild_config(guild.id)
        if not config:
            return

        async def handle_vc_leave():
            if channel_id := config.mappings.get(before.channel.id):
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.set_permissions(member, overwrite=None)

        async def handle_vc_join():
            if channel_id := config.mappings.get(after.channel.id):
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.set_permissions(member, read_messages=True, send_messages=True)

        if is_outside_voice(before) and is_inside_voice(after):
            # Joined channel.
            await handle_vc_join()
        elif is_outside_voice(after) and is_inside_voice(before):
            # Left channel.
            await handle_vc_leave()
        elif is_inside_voice(before) and is_inside_voice(after):
            if after.channel.id != before.channel.id:
                # Possible channel change.
                await handle_vc_leave()
                await handle_vc_join()

    async def update_tracker(self, guild):
        config = await self.get_guild_config(guild.id)
        if not config and not config.tracker_channel:
            return

        # Update the tracker with the latest server size.
        await config.tracker_channel.edit(name=f"Members: {len(guild.members)}")

    async def _handle_rejoin(self, state, embed, member):
        embed.title = f'\U000026a0 User {member.name} re-joined the server!'
        if state.roles:
            try:
                await member.add_roles(*state.roles, atomic=False, reason="Role-state restore")
                self.logger.info(f"Restoring role-state for {member} with {len(state.roles)} previous roles.")
            except (discord.HTTPException, discord.Forbidden):
                pass

        if state.muted:
            self.logger.info(f"Scheduling mute for {member}.")
            self._pending_mute[member.guild.id].add(member.id)

        embed.add_field(name="State information",
                        value=f"Restored roles: {len(state.roles)}\nMuted: {'Yes' if state.muted else 'No'}",
                        inline=False)

        return embed

    @staticmethod
    def format_greeting(member, greeting):
        valid_templates = {
            "name": member.name,
            "name_mention": member.mention,
            "server": member.guild.name
        }

        def sub(m):
            return valid_templates.get(m[1], m[0])

        return re.sub(r"\$(\w+)", sub, greeting)

    @Cog.listener()
    async def on_member_join(self, member: discord.Member):
        config = await self.get_guild_config(member.guild.id)
        if not config:
            return

        # Update member count.
        await self.update_tracker(member.guild)

        embed = discord.Embed(colour=0x81c784)
        embed.set_footer(text=str(member.id))
        embed.timestamp = datetime.utcnow()

        # Try to restore their previous state.
        # This only applies in case they rejoined within an hour.
        state = self._member_state[member.guild.id].fetch(member.id)
        if state:
            embed = await self._handle_rejoin(state, embed, member)
        else:
            # Probably not a rejoin, though that's not certain.
            embed.title = f'\U0001f44b User {member.name} joined the server!'
            # Give them the `Unverified` role.
            await member.add_roles(discord.Object(id=config.verification_role_id))

            delta = (member.joined_at - member.created_at).total_seconds() // 60
            if delta < 10:
                embed.colour = 0xe57373
                embed.add_field(name="\U00002757 Young account",
                                value=f"Created {human_timedelta(member.created_at)}")

            if config.default_channel and config.greeting:
                await config.default_channel.send(self.format_greeting(member, config.greeting))

        if not config.modlog:
            return

        ping = "@here" if state and state.muted else ""
        await config.modlog.send(content=ping, embed=embed)
        # Check their default display name.
        is_bad_name = await self.validate_nickname(member)
        if is_bad_name:
            await config.modlog.send(embed=is_bad_name)

    @Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        config = await self.get_guild_config(member.guild.id)
        if not config:
            return

        # Save their connection state in case they want to bypass a punishment or a mute.
        # This is a workaround for `member.voice.mute` because that state only gets displayed when
        # they're connected to a voice channel.
        _logs = await member.guild.audit_logs(action=discord.AuditLogAction.member_update).flatten()
        potentially_muted = next((x.after.mute for x in _logs if getattr(x.after, "mute", None)
                                  is not None and x.target.id == member.id), False)

        await self.update_tracker(member.guild)

        # Exclude @everyone
        mem_roles = member.roles[1:]
        self._member_state[member.guild.id][member.id] = StateInformation(roles=mem_roles,
                                                                          muted=potentially_muted)

        self.logger.info(f"Member left. Saving role-state for {member}: {Plural(len(mem_roles)):role}."
                         f" They were {'' if potentially_muted else 'not '}previously muted.")

        embed = discord.Embed(colour=0xe57373)
        embed.title = f'\U0001f6aa User {member.name} left the server!'
        embed.set_footer(text=str(member.id))
        embed.timestamp = datetime.utcnow()
        await config.modlog.send(embed=embed)

    @Cog.listener()
    async def on_message_edit(self, before: Message, after: Message):
        config = await self.get_guild_config(after.guild.id)
        if not config:
            return

        if not before.author.bot and before.content != after.content and before.content != "" and after.content != "":
            embed = discord.Embed(colour=0xffcc80)
            embed.title = f'\U0000270f `[{after.channel.name.upper()}]` User {before.author} edited their message'
            embed.timestamp = datetime.utcnow()

            embed_paginate(embed, 'Before', before.clean_content, inline=False)

            changes = get_diff(before.clean_content, after.clean_content)
            if len(changes) > 1024:
                # Find changed line positions.
                match = re.finditer(r'__(.*)__', changes)
                if any(m.start() <= 1024 < m.end() for m in match):
                    # Cover case where we have a long edit that exceeds the limit.
                    changes = changes[:1022] + "____" + changes[1022:]

            embed_paginate(embed, 'After', changes, inline=False)

            await config.modlog.send(embed=embed)

    @Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        config = await self.get_guild_config(message.guild.id)
        if not config:
            return

        if message.author.bot:
            return

        embed = discord.Embed(colour=0xe57373)
        embed.title = f'\U0001f525 `[{message.channel.name.upper()}]` User {message.author} deleted their message'

        if message.attachments:
            image = message.attachments[0].proxy_url
            embed.add_field(name='Proxy URL (This URL might not be valid for long)', value=image)
            embed.set_image(url=image)

        if message.content:
            # Guard against large chunks of text.
            embed_paginate(embed, "Content", message.clean_content, inline=False)

        embed.timestamp = datetime.utcnow()

        await config.modlog.send(embed=embed)

    async def validate_nickname(self, member) -> typing.Optional[discord.Embed]:
        new_nick = member.display_name
        if new_nick.isascii():
            return

        # Maybe .encode in the future.
        ascii_nick = "Ascii nickname needed"
        self._recent_bad_nicks.add(member.id)

        try:
            await member.edit(nick=ascii_nick)
        except discord.Forbidden as e:
            self.logger.warn(e)
            return

        embed = discord.Embed(title=f"\U000026a0 Bad member name fixed")
        embed.description = f"Original nickname '{new_nick}'"
        embed.add_field(name="Member", value=member.name, inline=False)
        embed.add_field(name="New nick", value=ascii_nick, inline=False)
        embed.timestamp = datetime.utcnow()
        return embed

    @Cog.listener()
    async def on_member_update(self, before: Member, after: Member):
        config = await self.get_guild_config(after.guild.id)
        if not config:
            return

        if len(before.roles) != len(after.roles):
            fmt_before = [x.name for x in before.roles[1:]]
            fmt_after = [x.name for x in after.roles[1:]]

            embed = discord.Embed()

            # Role removed
            if len(fmt_before) > len(fmt_after):
                diff = [i for (i, e) in enumerate(fmt_before) if e not in fmt_after]

                for change in diff:
                    fmt_before[change] = f'~~{fmt_before[change]}~~'
                embed.colour = 0xe57373

            # Role added
            elif len(fmt_after) > len(fmt_before):
                diff = [i for (i, e) in enumerate(fmt_after) if e not in fmt_before]

                for change in diff:
                    fmt_after[change] = f'+__{fmt_after[change]}__'
                embed.colour = 0x81c784

            fmt_before = ", ".join(fmt_before)
            fmt_after = ', '.join(fmt_after)

            embed.title = f"\U0001f4cb {after.name}'s roles have changed:"
            embed.add_field(name="Before", value=f'{fmt_before} ')
            embed.add_field(name="After", value=fmt_after)
            embed.timestamp = datetime.utcnow()
            try:
                return await config.modlog.send(embed=embed)
            except discord.HTTPException:
                # No previous roles found.
                # Imo, this is nicer for joins.
                pass

        if before.nick != after.nick:
            if after.bot:
                return

            if after.id in self._recent_bad_nicks:
                # We triggered this event.
                self._recent_bad_nicks.remove(after.id)
                return

            # Check whether the new display name is ascii-only.
            is_bad_nick = await self.validate_nickname(after)
            if is_bad_nick:
                await config.modlog.send(embed=is_bad_nick)
                return

            embed = discord.Embed(colour=0xffcc80)
            embed.title = f"\U0001f4cb {after.name}'s nickname has changed:"
            embed.add_field(name="Before", value=before.nick)
            embed.add_field(name="After", value=after.nick)
            embed.timestamp = datetime.utcnow()
            await config.modlog.send(embed=embed)

    @Cog.listener()
    async def on_bulk_message_delete(self, messages: typing.List[discord.Message]):
        # Filter bot messages.
        actual_messages = [m for m in messages if not m.author.bot]
        if not actual_messages:
            return

        first_message = actual_messages[0]

        # Manually load modlog.
        config = await self.get_guild_config(first_message.guild.id)
        if not (config and config.modlog_channel_id):
            return

        paginator = BulkDeletePaginator(channel=config.modlog, entries=actual_messages,
                                        event_name=f"\U0001f525 Bulk deletion", timestamp=datetime.utcnow())

        try:
            await paginator.paginate()
        except Exception as e:
            with open('bulk_delete_err.log', 'w', encoding='utf-8') as fp:
                for message in [m for m in messages if not m.author.bot]:
                    try:
                        x = json.dumps({"id": message.id, "content": message.content}, ensure_ascii=True, indent=4)
                    except:
                        fp.write(f'{message}\n')
                    else:
                        fp.write(f'{x}\n')

            # Re-raise for on_error
            raise discord.DiscordException("Bulk delete failed") from e

    @Cog.listener()
    async def on_punishment_add(self, punishment: Punishment):
        config = await self.get_guild_config(punishment.guild.id)
        if not config:
            return

        embed = discord.Embed(title=f"\N{WARNING SIGN} New {punishment.type.title} punishment",
                              colour=discord.Colour.red())

        embed.add_field(name="Affected member", value=punishment.target, inline=False)
        moderator = punishment.moderator or "No responsible moderator"
        embed.add_field(name="Responsible moderator", value=moderator, inline=False)
        embed.add_field(name="Reason", value=punishment.reason or "No reason provided.", inline=False)
        embed.add_field(name="Duration", value=punishment.duration, inline=False)
        embed.timestamp = datetime.utcnow()

        msg = await config.punishment_channel.send(embed=embed)
        if punishment.id is not None:
            # Kick or Ban.
            query = "UPDATE removals SET punish_message_id = $1 WHERE id = $2"
            await self.bot.pool.execute(query, msg.id, punishment.id)

    @Cog.listener()
    async def on_punishment_remove(self, punishment: Punishment):
        config = await self.get_guild_config(punishment.guild.id)
        if not config:
            return

        fmt = f"{punishment.type.title} punishment lifted" if punishment.type != ActionType.UNBAN else "New Unban"
        embed = discord.Embed(title=f"\N{SHAMROCK} {fmt}", colour=discord.Colour.green())
        target = getattr(punishment.target, "display_name", punishment.target)
        embed.add_field(name="Affected member", value=target, inline=False)
        embed.add_field(name="Responsible moderator", value=punishment.moderator, inline=False)
        embed.timestamp = datetime.utcnow()

        msg = await config.punishment_channel.send(embed=embed)
        if punishment.id is not None:
            # Unban.
            query = "UPDATE removals SET punish_message_id = $1 WHERE id = $2"
            await self.bot.pool.execute(query, msg.id, punishment.id)


setup = Event.setup
