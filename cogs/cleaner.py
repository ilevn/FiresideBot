import re
from collections import Counter

import discord
from discord.ext import commands

from cogs.utils import is_mod
from cogs.utils.meta_cog import Cog


class Cleaner(Cog):
    """Cleaner commands to remove specific messages."""

    @commands.group(aliases=['purge', 'clean'])
    @is_mod()
    async def remove(self, ctx):
        """
        Remove messages that meet a criteria.
        Note that the bot needs Manage Messages. These commands cannot
        be used in a private message.
        You'll get messaged once the command is done doing its work.
        `search` is used to specify the message search span of the bot. Defaults to 100
        """

        if ctx.invoked_subcommand is None:
            return await ctx.send_help("remove")

    @staticmethod
    async def do_removal(ctx, limit, predicate, *, before=None, after=None):
        if limit > 2000:
            return await ctx.send(f'Too many messages to search given ({limit}/2000)')

        if before is None:
            before = ctx.message
        else:
            before = discord.Object(id=before)

        if after is not None:
            after = discord.Object(id=after)

        try:
            await ctx.message.delete()
            deleted = await ctx.channel.purge(limit=limit, before=before, after=after, check=predicate)
        except discord.Forbidden:
            return await ctx.send('I do not have permissions to delete messages.')
        except discord.HTTPException as e:
            return await ctx.send(f'Error: {e} (try a smaller search?)')

        escape = discord.utils.escape_mentions

        spammers = Counter(escape(m.author.display_name) for m in deleted)
        deleted = len(deleted)
        messages = [f'{deleted} message{" was" if deleted == 1 else "s were"} removed.']
        if deleted:
            messages.append('')
            spammers = sorted(spammers.items(), key=lambda t: t[1], reverse=True)
            messages.extend(f'**{name}**: {count}' for name, count in spammers)

        to_send = '\n'.join(messages)

        if len(to_send) > 2000:
            await ctx.send(f'Successfully removed {deleted} messages.', delete_after=10)
        else:
            await ctx.send(to_send, delete_after=10)

    @remove.command()
    async def embeds(self, ctx, search=100):
        """Remove messages that have embeds in them."""
        await self.do_removal(ctx, search, lambda e: len(e.embeds))

    @remove.command()
    async def files(self, ctx, search=100):
        """Remove messages that have attachments in them."""
        await self.do_removal(ctx, search, lambda e: len(e.attachments))

    @remove.command()
    async def images(self, ctx, search=100):
        """Remove messages that have embeds or attachments."""
        await self.do_removal(ctx, search, lambda e: len(e.embeds) or len(e.attachments))

    @remove.command(name='all')
    async def _remove_all(self, ctx, search=100):
        """Remove all messages."""
        await self.do_removal(ctx, search, lambda e: True)

    @remove.command()
    async def user(self, ctx, member: discord.Member, search=100):
        """Remove all messages by the member."""
        await self.do_removal(ctx, search, lambda e: e.author == member)

    @remove.command()
    async def contains(self, ctx, *, substr: str):
        """Remove all messages containing a substring.
        The substring must be at least 3 characters long.
        """
        if len(substr) < 3:
            await ctx.send('The substring length must be at least 3 characters.')
        else:
            await self.do_removal(ctx, 100, lambda e: substr in e.content)

    @remove.command(name='bot')
    async def _bot(self, ctx, prefix=None, search=100):
        """Remove a bot user's messages and messages with their optional prefix."""
        def predicate(m):
            return m.author.bot or (prefix and m.content.startswith(prefix))

        await self.do_removal(ctx, search, predicate)

    @remove.command()
    async def id(self, ctx, msg_id: int, search=100):
        """Remove all messages posted after <msg_id>."""
        await self.do_removal(ctx, search, lambda e: True, after=msg_id)

    @remove.command()
    async def between(self, ctx, start_id: int, end_id: int, search=100):
        """Removes messages between the start message id and the supplied end message id."""
        await self.do_removal(ctx, search, lambda e: True, before=end_id, after=start_id)

    @remove.command()
    async def regex(self, ctx, *, regex: str, search=100):
        """Remove all messages that apply to <regex>."""
        try:
            compiled = re.compile(regex)
        except re.error:
            return ctx.send(":x: Invalid regex provided...")

        def predicate(m):
            return compiled.search(m.content)

        await self.do_removal(ctx, search, predicate)

    @remove.command(name='emoji')
    async def _emoji(self, ctx, search=100):
        """Removes all messages containing custom emoji."""
        custom_emoji = re.compile(r'<a?:[a-zA-Z0-9_]+:([0-9]+)>')

        def predicate(m):
            return custom_emoji.search(m.content)

        await self.do_removal(ctx, search, predicate)

    @remove.command(name='reactions')
    async def _reactions(self, ctx, search=100):
        """Removes all reactions from messages that have them."""
        if search > 2000:
            return await ctx.send(f'Too many messages to search for ({search}/2000)')

        total_reactions = 0
        async for message in ctx.history(limit=search, before=ctx.message):
            if len(message.reactions):
                total_reactions += sum(r.count for r in message.reactions)
                await message.clear_reactions()

        await ctx.send(f'Successfully removed {total_reactions} reactions.')


setup = Cleaner.setup
