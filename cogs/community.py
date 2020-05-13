import discord
from discord.ext import commands

from cogs.utils import db
from cogs.utils.cache import cache
from cogs.utils.converters import CaselessRole
from cogs.utils.meta_cog import Cog
from cogs.utils.paginators import CannotPaginate, RolePoolPages

FIRESIDE_SHITPOST_ACCESS = 710172301399687249


class Roles(db.Table):
    id = db.PrimaryKeyColumn()
    # The guild id.
    guild_id = db.DiscordIDColumn(index=True)
    # The role id.
    role_id = db.DiscordIDColumn(index=True, unique=True)
    # The category a role belongs to, none, if un-categorised.
    category = db.Column(db.String, nullable=True)


class Community(Cog):
    async def cog_check(self, ctx):
        if ctx.guild is None:
            return False

        return True

    @cache()
    async def get_pool_roles(self, guild_id):
        query = "SELECT role_id FROM roles WHERE guild_id = $1"
        async with self.bot.pool.acquire() as con:
            records = await con.fetch(query, guild_id)
            return records and {r[0] for r in records}

    @Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        query = "DELETE FROM roles WHERE guild_id = $1 AND role_id = $2"
        guild_id = role.guild.id
        await self.bot.pool.execute(query, guild_id, role.id)
        self.get_pool_roles.invalidate(self, guild_id)

    @commands.command(name="getrole", aliases=["iam"])
    async def roles_get(self, ctx, *, role: CaselessRole):
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
    async def roles_remove(self, ctx, *, role: CaselessRole):
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

    @staticmethod
    async def toggle_role(ctx, role_id):
        if any(r.id == role_id for r in ctx.author.roles):
            try:
                await ctx.author.remove_roles(discord.Object(id=role_id))
            except:
                await ctx.message.add_reaction('\N{NO ENTRY SIGN}')
            else:
                await ctx.message.add_reaction('\N{HEAVY MINUS SIGN}')
            finally:
                return

        try:
            await ctx.author.add_roles(discord.Object(id=role_id))
        except:
            await ctx.message.add_reaction('\N{NO ENTRY SIGN}')
        else:
            await ctx.message.add_reaction('\N{WHITE HEAVY CHECK MARK}')

    @commands.command()
    async def shitposter(self, ctx):
        """Allows you to opt-in or out of the shitpost channel."""
        prompt = await ctx.prompt(
            "By accepting this prompt, you show you understand that you may be subjected"
            " to offensive behaviour."
        )
        if not prompt:
            await ctx.send("Aborting...", delete_after=3)

        await self.toggle_role(ctx, FIRESIDE_SHITPOST_ACCESS)


setup = Community.setup
