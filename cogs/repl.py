import asyncio
import io
import textwrap
import time
import traceback
from contextlib import redirect_stdout

import discord
from discord.ext import commands

from cogs.utils import Plural, Context, TabularData, maintainer_check
from cogs.utils.meta_cog import Cog


class Repl(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        self._last_result = None

    @staticmethod
    def cleanup_code(content):
        # Remove ```py\n```.
        if content.startswith('```') and content.endswith('```'):
            return '\n'.join(content.split('\n')[1:-1])

        # Remove `foo`.
        return content.strip('` \n')

    @staticmethod
    def get_syntax_error(e):
        if e.text is None:
            return f'```py\n{e.__class__.__name__}: {e}\n```'
        return f'```py\n{e.text}{"^":>{e.offset}}\n{type(e).__name__}: {e}```'

    async def cog_check(self, ctx):
        return await maintainer_check(ctx)

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            await ctx.send(":x: You're not a bot maintainer.")

    @commands.command(hidden=True, name='eval')
    async def _eval(self, ctx: Context, *, body: str):
        """Eval some code."""
        env = {
            'bot': self.bot,
            'ctx': ctx,
            'message': ctx.message,
            'guild': ctx.guild,
            'channel': ctx.channel,
            'author': ctx.author,
            '_': self._last_result,
            'db': self.bot.pool,
            'discord': discord,
            'cog': self.bot.get_cog,
            'asyncio': asyncio,
            '_gm': ctx.guild.get_member,
            '_gc': ctx.guild.get_channel
        }

        # Update the global env with our custom mappings.
        env.update(globals())

        body = self.cleanup_code(body)
        stdout = io.StringIO()

        to_compile = f'async def func():\n{textwrap.indent(body, "  ")}'

        try:
            exec(to_compile, env)
        except Exception as e:
            return await ctx.send(f'```py\n{e.__class__.__name__}: {e}\n```')

        func = env['func']
        try:
            with redirect_stdout(stdout):
                ret = await func()
        except Exception:
            value = stdout.getvalue()
            await ctx.send(f'```py\n{value}{traceback.format_exc()}\n```')
        else:
            value = stdout.getvalue()
            try:
                await ctx.message.add_reaction('\u2705')
            except:
                pass

            to_send = None

            if ret is None:
                if value:
                    to_send = f'```py\n{value}\n```'
            else:
                self._last_result = ret
                to_send = f'```py\n{value}{ret}\n```'

            if len(str(to_send)) > 1994:
                async with ctx.session.post("http://0x0.st", data={"file": io.StringIO(str(to_send))}) as r:
                    return await ctx.send(f":x: Content too big to be printed: {await r.text()}")

            if to_send:
                await ctx.send(to_send)

    @staticmethod
    def format_table(results):
        table = TabularData()
        table.set_columns(list(results[0].keys()))
        table.add_rows(list(r.values()) for r in results)
        return table.render()

    @commands.command(hidden=True)
    async def sql(self, ctx, *, query: str):
        """Run some SQL."""

        query = self.cleanup_code(query)

        is_multistatement = query.count(';') > 1
        if is_multistatement:
            # Fetch does not support multiple statements.
            strategy = ctx.db.execute
        else:
            strategy = ctx.db.fetch

        try:
            start = time.perf_counter()
            results = await strategy(query)
            dt = (time.perf_counter() - start) * 1000.0
        except Exception:
            return await ctx.send(f'```py\n{traceback.format_exc()}\n```')

        rows = len(results)
        if is_multistatement or rows == 0:
            return await ctx.send(f'`{dt:.2f}ms: {results}`')

        render = self.format_table(results)

        fmt = f'```\n{render}\n```\n*Returned {Plural(rows):row} in {dt:.2f}ms*'
        if len(fmt) > 2000:
            async with ctx.session.post("http://0x0.st", data={"file": io.StringIO(fmt)}) as r:
                await ctx.send(f'Too many results: {await r.text()}')
                return

        await ctx.send(fmt)

    @commands.command(hidden=True)
    async def sql_table(self, ctx, *, table_name: str):
        """Runs a query describing the table schema."""

        query = """SELECT column_name, data_type, column_default, is_nullable
                       FROM INFORMATION_SCHEMA.COLUMNS
                       WHERE table_name = $1
                """

        results = await ctx.db.fetch(query, table_name)
        render = self.format_table(results)

        async with ctx.session.post("http://0x0.st", data={"file": io.StringIO(render)}) as r:
            await ctx.send(f'Result: {await r.text()}')


setup = Repl.setup
