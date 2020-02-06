from discord.ext import commands


# TODO: Dedicated bot admin roles?
# DB integration with cached roles.

async def check_guild_permissions(ctx, perms, *, check=all):
    is_owner = await ctx.bot.is_owner(ctx.author)
    if is_owner:
        return True

    if ctx.guild is None:
        return False

    resolved = ctx.author.guild_permissions
    return check(getattr(resolved, name, None) == value for name, value in perms.items())


def is_mod():
    async def pred(ctx):
        return await check_guild_permissions(ctx, {"manage_guild": True})

    return commands.check(pred)


def is_admin():
    async def pred(ctx):
        return await check_guild_permissions(ctx, {"administrator": True})

    return commands.check(pred)


async def maintainer_check(ctx):
    # Penloy & 0x1.
    return ctx.author.id in ctx.bot.maintainers


def is_maintainer():
    return commands.check(maintainer_check)
