import discord
from discord.ext import commands

from cogs.utils import db
from cogs.utils.cache import cache
from cogs.utils.meta_cog import Cog
from cogs.utils.paginators import CannotPaginate, RolePoolPages


# TODO: Add to config setup command
class Roles(db.Table):
    id = db.PrimaryKeyColumn()
    # The guild id.
    guild_id = db.DiscordIDColumn(index=True)
    # The role id.
    role_id = db.DiscordIDColumn(index=True, unique=True)
    # The category a role belongs to, none, if un-categorised.
    category = db.Column(db.String, nullable=True)


class Community(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        self.poll_emotes = ("\N{THUMBS UP SIGN}", "\N{THUMBS DOWN SIGN}", "\N{SHRUG}")

    @cache()
    async def get_pool_roles(self, guild_id):
        query = "SELECT role_id FROM roles WHERE guild_id = $1"
        async with self.bot.pool.acquire() as con:
            records = await con.fetch(query, guild_id)
            return records and {r[0] for r in records}

    @Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        cog = self.bot.get_cog("Event")
        if not cog:
            return

        config = await cog.get_guild_config(message.guild.id)
        if not config:
            return

        if message.channel.id != config.poll_channel_id:
            return

        if not message.content.startswith("Poll: "):
            await message.channel.send("Bad poll format. Please make sure your poll starts with `Poll: `",
                                       delete_after=8)
            await message.delete(delay=7)
            return

        for emote in self.poll_emotes:
            await message.add_reaction(emote)

    @Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        query = "DELETE FROM roles WHERE guild_id = $1 AND role_id = $2"
        guild_id = role.guild.id
        await self.bot.pool.execute(query, guild_id, role.id)
        self.get_pool_roles.invalidate(guild_id)

    # TODO: Better name.
    @commands.command(name="getrole", aliases=["iam"])
    async def roles_get(self, ctx, role: discord.Role):
        """Assign a role to yourself from the rolepool."""

        roles = await self.get_pool_roles(ctx.guild.id)
        if not roles:
            return await ctx.send("This server doesn't have any assignable roles.")

        role_id = role.id
        author = ctx.author

        # Private API use, faster.
        if author._roles.has(role_id):
            return await ctx.send("You already have this role.")

        if role_id not in roles:
            return await ctx.send("This role is not assignable.")

        await author.add_roles(role)
        await ctx.message.add_reaction('\N{WHITE HEAVY CHECK MARK}')

    @commands.command(name="removerole", aliases=["iamn"])
    async def roles_remove(self, ctx, role: discord.Role):
        """Remove a role from yourself."""

        roles = await self.get_pool_roles(ctx.guild.id)
        if not roles:
            return await ctx.send("This server doesn't have any assignable roles.")

        role_id = role.id
        author = ctx.author
        if not author._roles.has(role_id):
            return await ctx.send("You currently do not have this role.")

        if role_id not in roles:
            return await ctx.send("This role is not removable.")

        await author.remove_roles(role)
        await ctx.message.add_reaction('\N{WHITE HEAVY CHECK MARK}')

    @commands.command(name="rlist", aliases=["lapr", "lsar"])
    async def roles_list(self, ctx):
        """Lists the available roles for this server."""

        roles = await self.get_pool_roles(ctx.guild.id)
        if not roles:
            return await ctx.send("This server currently doesn't have any assignable roles.")

        pages = await RolePoolPages.from_all(ctx)
        try:
            await pages.paginate()
        except CannotPaginate as e:
            return await ctx.send(e)


setup = Community.setup
