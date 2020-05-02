from discord.ext import commands

FIRESIDE_TRUSTED = 705893224023195748


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


def is_mod_or_trusted():
    async def pred(ctx):
        is_mod = await check_guild_permissions(ctx, {"manage_guild": True})
        if is_mod:
            return True

        return ctx.author._roles.has(FIRESIDE_TRUSTED)

    return commands.check(pred)


def is_admin():
    async def pred(ctx):
        return await check_guild_permissions(ctx, {"administrator": True})

    return commands.check(pred)


async def maintainer_check(ctx):
    # 0x1.
    return ctx.author.id in ctx.bot.maintainers


def is_maintainer():
    return commands.check(maintainer_check)


class PredicateCooldown:
    """Custom cooldown class to bypass a cooldown based on a predicate.."""

    def __init__(self, rate, per, bucket, predicate):
        self.predicate = predicate
        self.mapping = commands.CooldownMapping.from_cooldown(rate, per, bucket)

    def __call__(self, ctx):
        if self.predicate(ctx):
            return True

        bucket = self.mapping.get_bucket(ctx.message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            raise commands.CommandOnCooldown(bucket, retry_after)
        return True


def predicate_cooldown(rate, per, bucket, pred):
    return commands.check(PredicateCooldown(rate, per, bucket, pred))


def mod_cooldown(rate, per, bucket):
    def pred(ctx):
        return ctx.author.guild_permissions.manage_guild is True

    return predicate_cooldown(rate, per, bucket, pred)
