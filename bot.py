import json
import logging
import sys
import traceback
from collections import deque
from itertools import cycle

import aiohttp
import discord
import logbook
from discord.ext import tasks
from discord.ext.commands import Bot
from logbook import StreamHandler
from logbook.compat import redirect_logging

import config

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
        self._prev_events = deque(maxlen=10)
        self._owner_id = None
        self._app_id = None
        # Start the game status cycle task.
        self.status = cycle(["Communism", "With Stalin", "and Chilling"])
        self.change_status.start()

        for extension in config.autoload:
            try:
                self.load_extension(extension)
            except Exception as e:
                self.logger.critical(f"Failed to load extension {extension} -> {e}.")
                traceback.print_exc()

    @property
    def config(self):
        try:
            return __import__("config")
        except ImportError:
            self.logger.critical("Config is missing. Please copy one over from "
                                 "config.example.py to config.py")
            exit(1)

    @tasks.loop(seconds=10)
    async def change_status(self):
        await self.change_presence(activity=discord.Game(next(self.status)))

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
            f".\nInvite link: {discord.utils.oauth_url(self._app_id)}")

    async def process_commands(self, message):
        # TODO: Add our own Context object.
        ctx = await self.get_context(message)

        if ctx.command is None:
            return

        await self.invoke(ctx)

    async def on_message(self, message):
        if message.author.bot:
            return

        await self.process_commands(message)

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
