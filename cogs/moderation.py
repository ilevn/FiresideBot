from typing import Union

import discord
from discord.ext import commands

from cogs.utils import is_mod, human_timedelta
from cogs.utils.converters import FetchedUser
from cogs.utils.meta_cog import Cog


class Moderation(Cog):
    """Cog for moderation actions."""

    @commands.command(aliases=['newmembers', "new"])
    @is_mod()
    async def newusers(self, ctx, *, count: int = 5):
        """List the newest members of the server.
        This is useful to check if any suspicious members have joined.
        The count parameter can only be up to 25.
        """
        count = max(min(int(count), 25), 5)
        members = sorted(ctx.message.guild.members, key=lambda m: m.joined_at, reverse=True)[:count]
        embed = discord.Embed(title='New Members', colour=discord.Colour.green())

        for member in members:
            body = f'joined {human_timedelta(member.joined_at)}, created {human_timedelta(member.created_at)}'
            embed.add_field(name=f'{member} (ID: {member.id})', value=body, inline=False)

        await ctx.send(embed=embed)

    @commands.command(aliases=["avatar"])
    @is_mod()
    async def avy(self, ctx, *, user: Union[discord.Member, FetchedUser] = None):
        """Shows the avatar of a user.
        This displays your avatar by default."""
        embed = discord.Embed()
        user = user or ctx.author
        avatar = user.avatar_url_as(static_format='png')
        embed.set_author(name=str(user), url=avatar)
        embed.set_image(url=avatar)
        await ctx.send(embed=embed)


setup = Moderation.setup
