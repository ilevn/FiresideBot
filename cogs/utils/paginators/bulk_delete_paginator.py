# Workflow
# List of messages gets submitted
# Calculate total char size
# Calculate fields and number of embeds based on that.

import typing
# 9000 / 6000 = ceil() -> 2
# 9000 / ceil()(2) -> 4500
# 4500 / 1024 -> 5 fields rounded
from collections import namedtuple
from math import ceil

import discord

EMBED_MAX = 5800
FIELD_MAX = 25

Field = namedtuple("Field", "name value inline count")
FormattedEntry = namedtuple("FormattedEntry", "content author channel")


class BulkDeletePaginator:
    def __init__(self, *, channel, entries, event_name=None, timestamp=None):
        self.channel = channel
        self.event_name = event_name
        self.timestamp = timestamp
        self.all_fields = sorted(self.chunk_entries(entries), key=lambda x: x.count)
        self.total = None
        self.embeds = self.calculate_embeds()

    @staticmethod
    def pre_process_entries(entries: typing.List[discord.Message]) -> typing.List[FormattedEntry]:
        processed_entries = []
        channel = ""

        for entry in entries:
            # Check if we're dealing with an image.
            if entry.attachments:
                additional = f'{entry.clean_content}\n\n' if entry.clean_content else ""
                content = f'{additional}Proxy URL (This URL might not be valid for long)\n' \
                          f'{entry.attachments[0].proxy_url}'
            else:
                content = entry.clean_content

            # Look for consecutive sequences.
            if entry.channel.name != channel:
                new_channel = channel = entry.channel.name
            else:
                new_channel = None

            processed_entries.append(FormattedEntry(content, entry.author, new_channel))

        return processed_entries

    def chunk_entries(self, entries, limit=1024, inline=False):
        entries = self.pre_process_entries(entries)

        for count, entry in enumerate(entries, 1):
            message = entry.content
            formatter = (f"`[{entry.channel.upper()}]`\n" if entry.channel else "") \
                        + f"{entry.author} - Message {count}"

            if len(message) > limit:
                for i, block in enumerate(message[i:i + limit] for i in range(0, len(message), limit)):
                    yield Field(f"{formatter} pt. {i + 1}", value=block, inline=inline, count=count)

            else:
                yield Field(name=f'{formatter}', value=message, inline=inline, count=count)

    def calculate_embeds(self):
        self.total = sum(len(e.name) + len(e.value) for e in self.all_fields)
        if self.total < EMBED_MAX:
            # Check whether we need to add another embed.
            if len(self.all_fields) > FIELD_MAX:
                return ceil(len(self.all_fields) / FIELD_MAX)

            return 1

        return ceil(self.total / EMBED_MAX)

    def generate_chunks(self):
        # Calculate how many fields are needed in each embed.
        k, m = divmod(len(self.all_fields), self.embeds)
        return (self.all_fields[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(self.embeds))

    def generate_embeds(self):
        for i, chunk in enumerate(self.generate_chunks(), 1):
            embed = discord.Embed(title=self.event_name, colour=0xe57373)

            for field in chunk:
                embed.add_field(name=field.name, value=field.value, inline=field.inline)

            if self.embeds > 1:
                embed.set_footer(text=f"Chunk {i}")

            if self.timestamp is not None:
                embed.timestamp = self.timestamp

            yield embed

    async def paginate(self):
        for embed in self.generate_embeds():
            await self.channel.send(embed=embed)
