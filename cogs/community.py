from typing import Optional

import discord

from cogs.utils import db
from cogs.utils.cache import cache
from cogs.utils.meta_cog import Cog


# TODO: Add to config setup command
class CommunityTable(db.Table, table_name="community"):
    # The guild id.
    id = db.Column(db.Integer(big=True), primary_key=True)
    # The default poll channel.
    poll_channel_id = db.DiscordIDColumn(index=True)
    # A pool of assignable roles.
    rolepool_ids = db.Column(db.Array(db.Integer(big=True)), index=True)


class CommunityConfig:
    __slots__ = ("bot", "id", "poll_channel_id", "rolepool_ids")

    @classmethod
    def from_record(cls, record, bot):
        self = cls()

        self.bot = bot
        self.id = record["id"]
        self.poll_channel_id = record["poll_channel_id"]
        self.rolepool_ids = set(record["rolepool_ids"] or [])
        return self

    @property
    def poll_channel(self):
        guild = self.bot.get_guild(self.id)
        return guild and guild.get_channel(self.poll_channel_id)


class Community(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        self.poll_emotes = ("\N{THUMBS UP SIGN}", "\N{THUMBS DOWN SIGN}", "\N{SHRUG}")

    @cache()
    async def get_community_config(self, guild_id) -> Optional[CommunityConfig]:
        query = "SELECT * FROM community WHERE id = $1"
        async with self.bot.pool.acquire() as con:
            record = await con.fetchrow(query, guild_id)
            return record and CommunityConfig.from_record(record, self.bot)

    @Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        config = await self.get_community_config(message.guild.id)
        if not config:
            return

        if message.channel.id != config.poll_channel_id:
            return

        if not message.content.startswith("Poll: "):
            await message.channel.send("Bad poll format. Please make sure your poll starts with `Poll: `",
                                       delete_after=8)
            await message.delete(delay=7)
            return

        for emote in self.poll_emotes:
            await message.add_reaction(emote)


setup = Community.setup
