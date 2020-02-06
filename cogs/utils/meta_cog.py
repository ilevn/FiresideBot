from discord.ext import commands

from bot import FiresideBot


class Cog(commands.Cog):
    def __init__(self, bot: FiresideBot):
        self._bot = bot
        self.logger = self.bot.logger

    @property
    def bot(self) -> 'FiresideBot':
        """
        :return: The bot instance associated with this cog.
        """
        return self._bot

    @classmethod
    def setup(cls, bot: FiresideBot):
        bot.add_cog(cls(bot))

    def __repr__(self):
        return f"<cogs.{self.__class__.__name__}>"
