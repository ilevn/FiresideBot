import typing

from cogs.utils.cache import cache
from cogs.utils.meta_cog import Cog


def is_outside_voice(state):
    return state.channel is None


def is_inside_voice(state):
    return state.channel is not None


class EventConfig:
    __slots__ = ('bot', 'id', 'modlog_channel_id', 'mod_channel_id', 'default_channel_id',
                 'greeting', 'shitpost_channel_id',
                 'jailed_channel_id', 'shitpost_role_id', 'jailed_role_id', 'mappings')

    @classmethod
    async def from_record(cls, record, bot, vc_mappings):
        self = cls()

        self.bot = bot
        self.mappings = dict(vc_mappings)
        # Thanks python for allowing this.
        for val in EventConfig.__slots__:
            actual_val = record.get(val)
            if not actual_val:
                continue

            setattr(self, val, actual_val)

        return self

    @property
    def modlog(self):
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(self.modlog_channel_id)

    @property
    def mod_channel(self):
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(self.mod_channel_id)

    @property
    def default_channel(self):
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(self.default_channel_id)


class Event(Cog):
    """
    Event cog for message handling.
    """

    @cache()
    async def get_guild_config(self, guild_id) -> typing.Optional[EventConfig]:
        # Kinda ugly but works for now.
        query = """SELECT * FROM guild_config gc JOIN punishment_config pc ON gc.id = pc.id WHERE pc.id = $1"""

        async with self.bot.pool.acquire() as con:
            record = await con.fetchrow(query, guild_id)
            if not record:
                return

            # Also fetch vc mappings.
            vc_mapping_query = "SELECT vc_channel_id, channel_id FROM vc_channel_config WHERE guild_id = $1"
            mappings = await con.fetch(vc_mapping_query, guild_id)
            return record and await EventConfig.from_record(record, self.bot, mappings)

    @Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        config = await self.get_guild_config(guild.id)

        if is_outside_voice(before, ) and is_inside_voice(after):
            # Joined channel.
            if channel_id := config.mappings.get(after.channel.id):
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.set_permissions(member, read_messages=True)

        elif is_outside_voice(after) and is_inside_voice(before):
            # Left channel.
            if channel_id := config.mappings.get(before.channel.id):
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.set_permissions(member, read_messages=None)


setup = Event.setup
