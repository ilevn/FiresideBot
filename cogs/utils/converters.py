import re

import discord
from discord.ext import commands
from discord.ext.commands import NoPrivateMessage, BadArgument


class CaselessRole(commands.IDConverter):
    """Support case insensitive role name arguments."""

    async def convert(self, ctx, argument) -> discord.Role:
        guild = ctx.guild
        if not guild:
            raise NoPrivateMessage()

        match = self._get_id_match(argument) or re.match(r'<@&([0-9]+)>$', argument)
        if match:
            result = guild.get_role(int(match.group(1)))
        else:
            result = self._find_role(argument.lower(), guild._roles.values())

        if result is None:
            raise BadArgument(f'Role "{argument}" not found.')
        return result

    @staticmethod
    def _find_role(argument, roles):
        for role in roles:
            if role.name.lower() == argument:
                return role


class FetchedUser(commands.Converter):
    async def convert(self, ctx, argument):
        if not argument.isdigit():
            raise commands.BadArgument('Not a valid user ID.')
        try:
            return await ctx.bot.fetch_user(argument)
        except discord.NotFound:
            raise commands.BadArgument('User not found.') from None
        except discord.HTTPException:
            raise commands.BadArgument('An error occurred while fetching the user.') from None

