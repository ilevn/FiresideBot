import asyncio
import json
import logging
import sys
import traceback
from collections import deque
from itertools import cycle

import aiohttp
import discord
import logbook
from discord.ext import commands
from discord.ext.commands import Bot
from logbook import StreamHandler
from logbook.compat import redirect_logging
from sentry_sdk import init as sen_init, configure_scope as sen_configure_scope, capture_exception

import config
from cogs.utils.context import Context

redirect_logging()

StreamHandler(sys.stderr).push_application()


class FiresideBot(Bot):
    def __init__(self, command_prefix, **options):
        super().__init__(command_prefix, **options)

        # Logging stuff
        self.logger = logbook.Logger("FiresideBot")
        self.logger.level = logbook.INFO
        logging.root.setLevel(logging.INFO)

        self.session = aiohttp.ClientSession(loop=self.loop)
        self.pool = None

        self._prev_events = deque(maxlen=10)
        self._owner_id = None
        # Hard-code Penloy and 0x1.
        self.maintainers = (320285462864461835, 189462608334553089, 292406013422993410)
        self.dev_mode = getattr(config, "dev_mode", False)
        self._app_id = None
        # Start the game status cycle task.
        self.status = cycle(["Communism", "With Stalin", "and Chilling"])
        self.loop.create_task(self.change_status())
        # Support for sentry.
        self.sentry = None
        if sentry_dsn := config.sentry_dsn:
            self.logger.info("Logging errors to sentry.")
            self.sentry = sen_init(dsn=sentry_dsn, max_breadcrumbs=0)

        if self.dev_mode:
            self.command_prefix = config.dev_prefix
            fmt = "!!RUNNING IN DEV MODE. TURN OFF IN PRODUCTION!! " \
                  f"Prefix set to `{self.command_prefix}`."
            self.logger.critical(fmt)

        for extension in config.autoload:
            try:
                self.load_extension(extension)
            except Exception as e:
                self.logger.critical(f"Failed to load extension {extension} -> {e}.")
                traceback.print_exc()
            else:
                self.logger.info(f"Loaded cog {extension}.")

    @property
    def config(self):
        try:
            return __import__("config")
        except ImportError:
            self.logger.critical("Config is missing. Please copy one over from "
                                 "config.example.py to config.py")
            exit(1)

    async def change_status(self):
        await self.wait_until_ready()
        while True:
            await self.change_presence(activity=discord.Game(next(self.status)))
            await asyncio.sleep(10)

    async def on_socket_response(self, data):
        self._prev_events.append(data)

    async def on_ready(self):
        try:
            app_info = await self.application_info()
            # We log these as back-up for `self.owner_id` + app_info
            self._owner_id = app_info.owner.id
            self._app_id = app_info.id
        except discord.HTTPException:
            self.logger.warn("Could not fetch regular owner info. Defaulting to MFA provided owner.")

        self.logger.info(
            f"Loaded Fireside Bot, logged in as {self.user.name}#{self.user.discriminator}"
            f".\nInvite link: {discord.utils.oauth_url(self._app_id, discord.Permissions(8))}")

    async def process_commands(self, message):
        ctx = await self.get_context(message, cls=Context)

        if ctx.command is None:
            return

        async with ctx.acquire():
            await self.invoke(ctx)

    async def on_message(self, message):
        if message.author.bot:
            return

        await self.process_commands(message)

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.author.send('This command cannot be used in private messages.')
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.channel.send(f"\N{CROSS MARK} Bad argument: {' '.join(error.args)}")
        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            if not isinstance(original, discord.HTTPException) or self.dev_mode:
                self.logger.critical(f'In {ctx.command.qualified_name}:')
                traceback.print_tb(error.__traceback__)
                self.logger.critical(f'{error.__class__.__name__}: {error}')
                await ctx.channel.send("Hmmm, this shouldn't normally happen."
                                       " This incident has been logged and reported!")

            if self.sentry is not None:
                data = {"Guild": ctx.guild,
                        "Channel": ctx.channel,
                        "Command": ctx.message.content,
                        "Invoked by": "<id='{0.id}' name='{0.name}' discriminator='{0.discriminator}'"
                                      " nick='{0.display_name}'>".format(ctx.author)
                        }

                with sen_configure_scope() as scope:
                    scope.set_context("Invoker information", data)
                    capture_exception(original)

        elif isinstance(error, commands.ArgumentParsingError) or self.dev_mode:
            await ctx.send(error)

    async def on_error(self, event, *args, **kwargs):
        if self.sentry is not None:
            # Get additional information in regard to our event.
            data = {
                "Event": event,
                **{f"Argument {i}": arg for i, arg in enumerate(args, 1)}
            }

            with sen_configure_scope() as scope:
                scope.set_context("Event information", data)
                # sys.exc_info() is used under the hood.
                capture_exception()
        else:
            traceback.print_exc()

    def run(self):
        try:
            super().run(config.token, reconnect=True)
        finally:
            with open("prev_events.log", "w", encoding="utf-8") as fp:
                for data in self._prev_events:
                    try:
                        x = json.dumps(data, ensure_ascii=True, indent=4)
                    except:
                        fp.write(f"{data}\n")
                    else:
                        fp.write(f"{x}\n")
