import asyncio
import datetime

import asyncpg
import discord
from discord.ext import commands

from cogs.utils import db, time, Plural
from cogs.utils.meta_cog import Cog


class Reminders(db.Table):
    id = db.PrimaryKeyColumn()

    expires = db.Column(db.Datetime, index=True)
    created = db.Column(db.Datetime, default="now() at time zone 'utc'")
    event = db.Column(db.String)
    extra = db.Column(db.JSON, default="'{}'::jsonb")


class Timer:
    __slots__ = ('args', 'kwargs', 'event', 'id', 'created_at', 'expires')

    def __init__(self, *, record):
        self.id = record['id']

        extra = record['extra']
        self.args = extra.get('args', [])
        self.kwargs = extra.get('kwargs', {})
        self.event = record['event']
        self.created_at = record['created']
        self.expires = record['expires']

    @classmethod
    def temporary(cls, *, expires, created, event, args, kwargs):
        pseudo = {
            'id': None,
            'extra': {'args': args, 'kwargs': kwargs},
            'event': event,
            'created': created,
            'expires': expires
        }
        return cls(record=pseudo)

    def __eq__(self, other):
        try:
            return self.id == other.id
        except AttributeError:
            return False

    def __hash__(self):
        return hash(self.id)

    @property
    def human_delta(self):
        return time.human_timedelta(self.created_at)

    def __repr__(self):
        return f'<Timer created={self.created_at} expires={self.expires} event={self.event}>'


class Reminder(Cog):
    """Reminders to do something."""

    def __init__(self, bot):
        super().__init__(bot)
        self._have_data = asyncio.Event(loop=bot.loop)
        self._current_timer = None
        self._task = bot.loop.create_task(self.dispatch_timers())

    def cog_unload(self):
        self._task.cancel()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False

        return True

    async def get_active_timer(self, *, connection=None, days=7):
        query = "SELECT * FROM reminders WHERE expires < (CURRENT_DATE + $1::interval) ORDER BY expires LIMIT 1;"
        con = connection or self.bot.pool

        record = await con.fetchrow(query, datetime.timedelta(days=days))
        return Timer(record=record) if record else None

    async def wait_for_active_timers(self, *, connection=None, days=7):
        async with db.MaybeAcquire(connection=connection, pool=self.bot.pool) as con:
            timer = await self.get_active_timer(connection=con, days=days)
            if timer is not None:
                self._have_data.set()
                return timer

            self._have_data.clear()
            self._current_timer = None
            await self._have_data.wait()
            return await self.get_active_timer(connection=con, days=days)

    async def call_timer(self, timer):
        # Delete the timer
        query = "DELETE FROM reminders WHERE id=$1;"
        await self.bot.pool.execute(query, timer.id)

        # Dispatch it.
        event_name = f'{timer.event}_timer_complete'
        self.bot.dispatch(event_name, timer)

    async def dispatch_timers(self):
        try:
            while not self.bot.is_closed():
                # We're gonna cap this at 40 days.
                # See: http://bugs.python.org/issue20493
                timer = self._current_timer = await self.wait_for_active_timers(days=40)
                now = datetime.datetime.utcnow()

                if timer.expires >= now:
                    to_sleep = (timer.expires - now).total_seconds()
                    await asyncio.sleep(to_sleep)

                await self.call_timer(timer)
        except asyncio.CancelledError:
            pass
        except (OSError, discord.ConnectionClosed, asyncpg.PostgresConnectionError):
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

    async def short_timer_optimisation(self, seconds, timer):
        await asyncio.sleep(seconds)
        event_name = f'{timer.event}_timer_complete'
        self.bot.dispatch(event_name, timer)

    async def create_timer(self, *args, **kwargs):
        """Creates a timer."""

        when, event, *args = args

        try:
            connection = kwargs.pop('connection')
        except KeyError:
            connection = self.bot.pool

        now = datetime.datetime.utcnow()
        timer = Timer.temporary(event=event, args=args, kwargs=kwargs, expires=when, created=now)
        delta = (when - now).total_seconds()
        if delta <= 60:
            # Shortcut for small timers.
            self.bot.loop.create_task(self.short_timer_optimisation(delta, timer))
            return timer

        query = """INSERT INTO reminders (event, extra, expires)
                   VALUES ($1, $2::jsonb, $3)
                   RETURNING id;
                """

        row = await connection.fetchrow(query, event, {'args': args, 'kwargs': kwargs}, when)
        timer.id = row[0]

        # See above (40 day cap)
        if delta <= (86400 * 40):
            self._have_data.set()

        # Check if this timer is earlier than our currently run timer
        if self._current_timer and when < self._current_timer.expires:
            # Cancel the task and re-run it
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())

        return timer

    @commands.group(aliases=['timer', 'remind'], usage='<when>', invoke_without_command=True)
    async def reminder(self, ctx, *, when: time.UserFriendlyTime(commands.clean_content, default='something')):
        """
        Reminds you about something after a certain amount of time.
        The input can be any direct date (e.g. YYYY-MM-DD) or a human
        readable offset. Examples:
        - "Next monday at 3am sleep"
        - "Talk about politics tomorrow"
        - "In two minutes do your homework"
        - "4d play with friends"
        Times are in UTC.
        """

        await self.create_timer(when.dt, 'reminder', ctx.author.id, ctx.channel.id, when.arg, connection=ctx.db,
                                message_id=ctx.message.id)

        delta = time.human_timedelta(when.dt)
        await ctx.send(f"Alright {ctx.author.mention}, in {delta}: {when.arg}")

    @reminder.command(name='list')
    async def reminder_list(self, ctx):
        """Shows the 5 latest currently running reminders."""
        query = """SELECT expires, extra #>> '{args,2}', id
                   FROM reminders
                   WHERE event = 'reminder'
                   AND extra #>> '{args,0}' = $1
                   ORDER BY expires
                   LIMIT 5;
                """

        records = await ctx.db.fetch(query, str(ctx.author.id))

        if not records:
            return await ctx.send(':x: No long-period timers are currently running')

        e = discord.Embed(colour=discord.Colour.blurple(), title='Reminders')

        if len(records) == 5:
            e.set_footer(text='Only showing the latest 5 timers.')
        else:
            e.set_footer(text=f'{Plural(len(records)):reminder}')

        for expires, message, _id in records:
            e.add_field(name=f'In {time.human_timedelta(expires)}', value=f"[{_id}] {message}", inline=False)

        await ctx.send(embed=e)

    @Cog.listener()
    async def on_reminder_timer_complete(self, timer):
        author_id, channel_id, message = timer.args

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        message_id = timer.kwargs.get('message_id')
        msg = f'<@{author_id}>, {timer.human_delta}: {message}'
        if message_id:
            msg = f'{msg}\n\n<https://discordapp.com/channels/{channel.guild.id}/{channel.id}/{message_id}>'
        await channel.send(msg)

    @reminder.command(name="cancel")
    async def reminder_cancel(self, ctx, *, id: int):
        """Cancels a reminder.
        You can get the reminder ID by invoking `reminder list`.
        """

        query = """DELETE FROM reminders
                   WHERE id = $1
                   AND event = 'reminder'
                   AND extra #>> '{args,0}' = $2
                """
        status = await ctx.db.execute(query, id, str(ctx.author.id))
        if status == 'DELETE 0':
            return await ctx.send('Could not delete any reminders with that ID.')

        # Check current reminders too.
        if self._current_timer and self._current_timer.id == id:
            # Cancel and re-run.
            self._task.cancel()
            self._task = self.bot.loop.create_task(self.dispatch_timers())
        await ctx.send('Successfully deleted reminder.')


setup = Reminder.setup
