import asyncio
import datetime
import io
import os
from collections import Counter
from typing import Union

import asyncpg
import discord
import psutil
from discord.ext import commands, tasks

from cogs.utils import human_timedelta, db, Plural, is_maintainer
from cogs.utils.converters import FetchedUser
from cogs.utils.meta_cog import Cog


def by_hex(arg):
    try:
        arg = int(arg, base=16)
    except ValueError:
        raise commands.ConversionError

    func = lambda x: id(x) == arg
    func.value = arg
    return func


def by_name(arg):
    func = lambda x: arg in repr(x)
    func.value = arg
    return func


def task_at(pred):
    for o in asyncio.all_tasks():
        if pred(o):
            return o
    return None


class Commands(db.Table):
    id = db.PrimaryKeyColumn()
    # The guild id.
    guild_id = db.Column(db.Integer(big=True), index=True)
    # The channel id the command was used in.
    channel_id = db.Column(db.Integer(big=True))
    # Invocation author.
    author_id = db.Column(db.Integer(big=True), index=True)
    # When the command was used.
    used = db.Column(db.Datetime, index=True)
    # Prefix the command was used with.
    prefix = db.Column(db.String)
    # The actual command name.
    command = db.Column(db.String, index=True)
    # Whether the invocation succeeded.
    failed = db.Column(db.Boolean, index=True)


class Stats(Cog):

    def __init__(self, bot):
        super().__init__(bot)
        self._batch_lock = asyncio.Lock(loop=bot.loop)
        self._data_batch = []
        self.bulk_insert_loop.add_exception_type(asyncpg.PostgresConnectionError)
        self.bulk_insert_loop.start()
        self.command_stats = Counter()
        self.socket_stats = Counter()
        self.process = psutil.Process()

    async def cog_check(self, ctx):
        return bool(ctx.guild)

    async def bulk_insert(self):
        query = """INSERT INTO commands (guild_id, channel_id, author_id, used, prefix, command, failed)
                   SELECT x.guild, x.channel, x.author, x.used, x.prefix, x.command, x.failed
                   FROM jsonb_to_recordset($1::jsonb) AS
                   x(guild BIGINT, channel BIGINT, author BIGINT, used TIMESTAMP, prefix TEXT, command TEXT,
                    failed BOOLEAN)
                """

        if self._data_batch:
            await self.bot.pool.execute(query, self._data_batch)
            total = len(self._data_batch)
            if total > 1:
                self.logger.info(f'Registered {len(self._data_batch)} commands to the database.')
            self._data_batch.clear()

    def cog_unload(self):
        self.bulk_insert_loop.stop()

    @tasks.loop(seconds=10.0)
    async def bulk_insert_loop(self):
        async with self._batch_lock:
            await self.bulk_insert()

    async def register_command(self, ctx):
        command = ctx.command.qualified_name
        self.command_stats[command] += 1
        message = ctx.message

        if ctx.guild is None:
            destination = 'Private Message'
            guild_id = None
        else:
            destination = f'#{message.channel} ({message.guild})'
            guild_id = ctx.guild.id

        self.logger.info(f'{message.created_at}: {message.author} in {destination}: {message.content}')

        async with self._batch_lock:
            self._data_batch.append({
                'guild': guild_id,
                'channel': ctx.channel.id,
                'author': ctx.author.id,
                'used': message.created_at.isoformat(),
                'prefix': ctx.prefix,
                'command': command,
                'failed': ctx.command_failed
            })

    @Cog.listener()
    async def on_command_completion(self, ctx):
        await self.register_command(ctx)

    @Cog.listener()
    async def on_socket_response(self, msg):
        self.socket_stats[msg.get('t')] += 1

    @commands.command(hidden=True)
    @is_maintainer()
    async def commandstats(self, ctx, limit=20):
        """Shows command stats.
        Use a negative number for bottom instead of top.
        This is only for the current session.
        """
        counter = self.command_stats
        width = len(max(counter, key=len))

        if limit > 0:
            common = counter.most_common(limit)
        else:
            common = counter.most_common()[limit:]

        output = '\n'.join(f'{k:<{width}}: {c}' for k, c in common)

        await ctx.send(f'```\n{output}\n```')

    @commands.command()
    async def info(self, ctx, *, user: Union[discord.Member, FetchedUser] = None):
        """Shows info about a user."""
        user = user or ctx.author
        is_user = isinstance(user, discord.Member)

        if ctx.guild and is_user:
            user = ctx.guild.get_member(user.id) or user

        e = discord.Embed(colour=0x738bd7)
        roles = tuple(role.name.replace('@', '@\u200b') for role in getattr(user, 'roles', []))
        e.set_author(name=f"Information about {user}")

        def format_date(dt):
            if dt is None:
                return 'N/A'
            return f'{dt:%d/%m/%Y %H:%M} ({human_timedelta(dt, accuracy=3)})'

        if is_user and ctx.author.guild_permissions.manage_guild:
            e.add_field(name="Display Name", value=user.mention)

        e.add_field(name='ID', value=user.id, inline=False)
        e.add_field(name='Joined', value=format_date(getattr(user, 'joined_at', None)), inline=False)
        e.add_field(name='Created', value=format_date(user.created_at), inline=False)

        voice = getattr(user, 'voice', None)
        if voice is not None:
            vc = voice.channel
            other_people = len(vc.members) - 1
            voice = f'{vc.name} with {Plural(other_people):other person|others}' \
                if other_people else f'{vc.name} by themselves'
            e.add_field(name='Voice', value=voice, inline=False)

        if roles:
            e.add_field(name='Roles', value=', '.join(roles) if len(roles) < 10 else f'{len(roles)} roles',
                        inline=False)

        if is_user and ctx.author.guild_permissions.manage_guild:
            query = "SELECT COUNT(*) FROM warning_entries WHERE guild_id=$1 AND member_id=$2"
            warnings = await ctx.db.fetchval(query, ctx.guild.id, user.id)
            if warnings:
                info = f"{warnings}\nCheck them with `{ctx.prefix}warn list {user.id}`"
                e.add_field(name='Number of warnings', value=info, inline=False)

        if user.avatar:
            e.set_thumbnail(url=user.avatar_url)

        if user not in ctx.guild.members:
            e.set_footer(text='This user is not in this server.')

        await ctx.send(embed=e)

    @staticmethod
    async def show_guild_stats(ctx):
        lookup = (
            '\N{FIRST PLACE MEDAL}',
            '\N{SECOND PLACE MEDAL}',
            '\N{THIRD PLACE MEDAL}',
            '\N{SPORTS MEDAL}',
            '\N{SPORTS MEDAL}'
        )

        embed = discord.Embed(title='Server Command Stats', colour=discord.Colour.blurple())

        # Total command uses.
        query = "SELECT COUNT(*), MIN(used) FROM commands WHERE guild_id=$1;"
        count = await ctx.db.fetchrow(query, ctx.guild.id)

        embed.description = f'{count[0]} commands used.'
        embed.set_footer(text='Tracking command usage since').timestamp = count[1] or datetime.datetime.utcnow()

        query = """SELECT command,
                          COUNT(*) AS "uses"
                   FROM commands
                   WHERE guild_id=$1
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: {command} ({Plural(uses):use})'
                          for (index, (command, uses)) in enumerate(records)) or 'No Commands'

        embed.add_field(name='Top Commands', value=value, inline=True)

        query = """SELECT command,
                          COUNT(*) AS "uses"
                   FROM commands
                   WHERE guild_id=$1
                   AND used > (CURRENT_TIMESTAMP - INTERVAL '1 day')
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: {command} ({Plural(uses):use})'
                          for (index, (command, uses)) in enumerate(records)) or 'No Commands.'
        embed.add_field(name='Top Commands Today', value=value, inline=True)
        embed.add_field(name='\u200b', value='\u200b', inline=True)

        query = """SELECT author_id,
                          COUNT(*) AS "uses"
                   FROM commands
                   WHERE guild_id=$1
                   GROUP BY author_id
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: <@!{author_id}> ({uses} bot {"uses" if uses > 1 else "use"})'
                          for (index, (author_id, uses)) in enumerate(records)) or 'No bot users.'

        embed.add_field(name='Top Command Users', value=value, inline=False)

        query = """SELECT author_id,
                          COUNT(*) AS "uses"
                   FROM commands
                   WHERE guild_id=$1
                   AND used > (CURRENT_TIMESTAMP - INTERVAL '1 day')
                   GROUP BY author_id
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query, ctx.guild.id)

        value = '\n'.join(f'{lookup[index]}: <@!{author_id}> ({uses} bot {"uses" if uses > 1 else "use"})'
                          for (index, (author_id, uses)) in enumerate(records)) or 'No command users.'

        embed.add_field(name='Top Command Users Today', value=value, inline=True)
        await ctx.send(embed=embed)

    @staticmethod
    async def show_member_stats(ctx, member):
        lookup = (
            '\N{FIRST PLACE MEDAL}',
            '\N{SECOND PLACE MEDAL}',
            '\N{THIRD PLACE MEDAL}',
            '\N{SPORTS MEDAL}',
            '\N{SPORTS MEDAL}'
        )

        embed = discord.Embed(title='Command Stats', colour=discord.Colour.blurple())
        embed.set_author(name=str(member), icon_url=member.avatar_url)

        # total command uses
        query = "SELECT COUNT(*), MIN(used) FROM commands WHERE guild_id=$1 AND author_id=$2;"
        count = await ctx.db.fetchrow(query, ctx.guild.id, member.id)

        embed.description = f'{count[0]} commands used.'
        embed.set_footer(text='First command used').timestamp = count[1] or datetime.datetime.utcnow()

        query = """SELECT command,
                          COUNT(*) AS "uses"
                   FROM commands
                   WHERE guild_id=$1 AND author_id=$2
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query, ctx.guild.id, member.id)

        value = '\n'.join(f'{lookup[index]}: {command} ({Plural(uses):use})'
                          for (index, (command, uses)) in enumerate(records)) or 'No Commands'

        embed.add_field(name='Most Used Commands', value=value, inline=False)

        query = """SELECT command,
                          COUNT(*) AS "uses"
                   FROM commands
                   WHERE guild_id=$1
                   AND author_id=$2
                   AND used > (CURRENT_TIMESTAMP - INTERVAL '1 day')
                   GROUP BY command
                   ORDER BY "uses" DESC
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query, ctx.guild.id, member.id)

        value = '\n'.join(f'{lookup[index]}: {command} ({Plural(uses):use})'
                          for (index, (command, uses)) in enumerate(records)) or 'No Commands'

        embed.add_field(name='Most Used Commands Today', value=value, inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    async def stats(self, ctx, *, member: discord.Member = None):
        """Tells you command usage stats for the server or a member."""
        if member is None:
            await self.show_guild_stats(ctx)
        else:
            await self.show_member_stats(ctx, member)

    @commands.command(hidden=True)
    @is_maintainer()
    async def bothealth(self, ctx):
        """Various bot health monitoring tools."""

        # This uses a lot of private methods because there is no
        # clean way of doing this otherwise.
        HEALTHY = discord.Colour(value=0x43B581)
        UNHEALTHY = discord.Colour(value=0xF04947)
        WARNING = discord.Colour(value=0xF09E47)
        total_warnings = 0

        embed = discord.Embed(title='Bot Health Report', colour=HEALTHY)

        # Check the connection pool health.
        pool = self.bot.pool
        total_waiting = len(pool._queue._getters)
        current_generation = pool._generation

        description = [
            f'Total `Pool.acquire` Waiters: {total_waiting}',
            f'Current Pool Generation: {current_generation}',
            f'Connections In Use: {len(pool._holders) - pool._queue.qsize()}'
        ]

        questionable_connections = 0
        connection_value = []
        for index, holder in enumerate(pool._holders, start=1):
            generation = holder._generation
            in_use = holder._in_use is not None
            is_closed = holder._con is None or holder._con.is_closed()
            display = f'gen={holder._generation} in_use={in_use} closed={is_closed}'
            questionable_connections += any((in_use, generation != current_generation))
            connection_value.append(f'<Holder i={index} {display}>')

        joined_value = '\n'.join(connection_value)
        embed.add_field(name='Connections', value=f'```py\n{joined_value}\n```', inline=False)

        description.append(f'Questionable Connections: {questionable_connections}')

        total_warnings += questionable_connections

        all_tasks = asyncio.all_tasks(loop=self.bot.loop)

        event_tasks = [
            t for t in all_tasks
            if 'Client._run_event' in repr(t) and not t.done()
        ]

        cogs_directory = os.path.dirname(__file__)

        inner_tasks = [
            t for t in all_tasks
            if cogs_directory in repr(t)
        ]

        bad_inner_tasks = ", ".join(hex(id(t)) for t in inner_tasks if t.done() and t._exception is not None)
        total_warnings += bool(bad_inner_tasks)
        embed.add_field(name='Inner Tasks', value=f'Total: {len(inner_tasks)}\nFailed: {bad_inner_tasks or "None"}')
        embed.add_field(name='Events Waiting', value=f'Total: {len(event_tasks)}', inline=False)

        command_waiters = len(self._data_batch)
        is_locked = self._batch_lock.locked()
        description.append(f'Commands Waiting: {command_waiters}, Batch Locked: {is_locked}')

        memory_usage = self.process.memory_full_info().uss / 1024 ** 2
        cpu_usage = self.process.cpu_percent() / psutil.cpu_count()
        embed.add_field(name='Process', value=f'{memory_usage:.2f} MiB\n{cpu_usage:.2f}% CPU', inline=False)

        global_rate_limit = not self.bot.http._global_over.is_set()
        description.append(f'Global Rate Limit: {global_rate_limit}')

        if command_waiters >= 8:
            total_warnings += 1
            embed.colour = WARNING

        if global_rate_limit or total_warnings >= 9:
            embed.colour = UNHEALTHY

        embed.set_footer(text=f'{Plural(total_warnings):warning}')
        embed.description = '\n'.join(description)
        await ctx.send(embed=embed)

    @commands.command(hidden=True, aliases=['cancel_task'])
    @is_maintainer()
    async def debug_task(self, ctx, predicate: Union[by_hex, by_name]):
        """Debug a task by a memory location or its name"""
        task = task_at(predicate)
        if task is None or not isinstance(task, asyncio.Task):
            return await ctx.send(f'Could not find Task object for predicate `{predicate.value}`.')

        if ctx.invoked_with == 'cancel_task':
            task.cancel()
            return await ctx.send(f'Cancelled task object {task!r}.')

        paginator = commands.Paginator(prefix='```py')
        fp = io.StringIO()
        frames = len(task.get_stack())
        paginator.add_line(f'# Total Frames: {frames}')
        task.print_stack(file=fp)

        for line in fp.getvalue().splitlines():
            paginator.add_line(line)

        for page in paginator.pages:
            await ctx.send(page)

    @commands.command()
    async def uptime(self, ctx):
        """Tells you how long the bot has been up for."""
        up_for = human_timedelta(self.bot.uptime, accuracy=None, suffix=False)
        await ctx.send(f'Uptime: **{up_for}**')


setup = Stats.setup
