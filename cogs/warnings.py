import asyncio
from collections import namedtuple
from typing import Union

import discord
from discord.ext import commands

from cogs.utils import is_mod, db
from cogs.utils.converters import FetchedUser, entry_id
from cogs.utils.meta_cog import Cog
from cogs.utils.paginators import CannotPaginate
from cogs.utils.paginators.warning_paginator import WarningPaginator


class WarningEntry(db.Table, table_name="warning_entries"):
    id = db.PrimaryKeyColumn()
    # The member who was warned.
    member_id = db.Column(db.Integer(big=True), nullable=False, index=True)
    # The moderator who warned a member.
    moderator_id = db.Column(db.Integer(big=True), nullable=False)
    # The guild, in which a member was warned.
    guild_id = db.Column(db.Integer(big=True), nullable=False)
    # Information about the warning itself.
    information = db.Column(db.String, nullable=False, index=True)
    # Whether the warning is a note or an actual warning.
    warning = db.Column(db.Boolean, default=True)
    # When the warning was created.
    created = db.Column(db.Datetime, default="now() at time zone 'utc'", index=True)


ActionInformation = namedtuple("ActionInformation", "member text is_warning")


def get_everyone_perms_for(channel):
    return channel.overwrites_for(channel.guild.default_role)


def mod_chat_only():
    """Ensures a command only gets invoked within a mod chat."""

    async def pred(ctx):
        overwrites = get_everyone_perms_for(ctx.channel)
        if overwrites.read_messages is False:
            return True
        raise PublicChannelError()

    return commands.check(pred)


class PublicChannelError(commands.CheckFailure):
    def __init__(self):
        super().__init__("This command can only be invoked from a mod channel.")


class Warnings(Cog):
    """Central warning system of the bot."""

    @commands.group(name="warn")
    @is_mod()
    async def warn(self, ctx):
        """Central warning system of the bot."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help('warn')

    @staticmethod
    async def _interactive_invocation(ctx, member: discord.Member, is_warning=True):
        # Delete invocation messages to make it less spammy
        to_delete = [ctx.message]
        try:
            page = await WarningPaginator.from_member(ctx, member, short_view=True)
            await page.paginate()
            to_delete.append(page.message)
        except CannotPaginate as e:
            to_delete.append(await ctx.send(e))

        to_delete.append(await ctx.send(f"Add a {'warning' if is_warning else 'note'} or type 'abort' to cancel."))

        try:
            info = await ctx.bot.wait_for("message", timeout=120,
                                          check=lambda m: m.channel == ctx.channel and m.author == ctx.author)
            to_delete.append(info)
        except asyncio.TimeoutError:
            await ctx.send("Took too long. Aborting...", delete_after=3)
        else:
            if info.content.lower() == 'abort':
                await ctx.send("OK. Aborting...", delete_after=3)
                return

            confirmed = await ctx.prompt(
                f"Gotcha. Are you sure you want to proceed and {'issue a warning' if is_warning else 'take a note'}?")

            if not confirmed:
                await ctx.send("OK. Aborting...", delete_after=3)
                return

            return info.clean_content
        finally:
            try:
                await ctx.channel.delete_messages(to_delete)
            except discord.HTTPException:
                pass

    async def take_action(self, ctx, information: ActionInformation):
        text = information.text
        member = information.member
        type_ = "warning" if information.is_warning else "note"

        if information.is_warning:
            confirm = await ctx.prompt(f"Please confirm that {member} was verbally and formally"
                                       f" warned before you continue.")
            if not confirm:
                await ctx.send("Aborting...", delete_after=3)
                return

        if not text:
            # Interactive warning.
            text = await self._interactive_invocation(ctx, member, information.is_warning)
            if not text:
                return
        else:
            await ctx.message.delete()

        query = """INSERT INTO warning_entries (member_id, moderator_id, guild_id, information, warning)
                   VALUES ($1, $2, $3, $4, $5) RETURNING id"""

        status = await ctx.db.fetchval(query, member.id, ctx.author.id, ctx.guild.id, text, information.is_warning)
        await ctx.send(f"OK. Added new {type_} for {member}", delete_after=3)

        cog = ctx.bot.get_cog("Event")
        if not cog:
            return

        config = await cog.get_guild_config(ctx.guild.id)
        if not (config and config.mod_channel):
            return

        embed = discord.Embed(title=f"\U00002139 {ctx.author} created a new {type_}")
        embed.add_field(name="Member", value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="Content", value=text, inline=False)
        embed.set_footer(text=f'Entry ID {status}')

        await config.mod_channel.send(embed=embed)

    @warn.command(name="issue")
    @mod_chat_only()
    async def warn_issue(self, ctx, member: discord.Member, *, text: str = None):
        """Creates a warning for a user. Intended for use in mod chats only.
        Providing text skips all database checks and straight up adds a new entry."""
        await self.take_action(ctx, ActionInformation(member, text, is_warning=True))

    @warn.command(name="note")
    @mod_chat_only()
    async def warn_note(self, ctx, member: discord.Member, *, text: str = None):
        """Adds a note for a user. Intended for use in mod chats only.
        Providing text skips all database checks and straight up adds a new entry."""
        await self.take_action(ctx, ActionInformation(member, text, is_warning=False))

    @warn_issue.error
    @warn_note.error
    async def warn_issue_error(self, ctx, error):
        if isinstance(error, PublicChannelError):
            await ctx.send(error)

    @staticmethod
    async def get_info(id_, ctx):
        # Used to double-check entries before editing/deleting them.
        query = "SELECT information, moderator_id, member_id FROM warning_entries WHERE id = $1"
        record = await ctx.db.fetchrow(query, id_)
        if not record:
            raise RuntimeError

        mod = ctx.guild.get_member(record[1])
        user = ctx.guild.get_member(record[2]) or f"Left server ({record[2]})"

        return f"Responsible mod: {mod}\nMember: {user}\nText: {record[0]}"

    @warn.command(name="edit")
    async def warn_edit(self, ctx, id: entry_id, *, text: str):
        """Edits a warning or note.
        A double-check is performed to prevent accidents.
        Please inform other moderators before editing their issued warnings or notes."""
        try:
            information = await self.get_info(id, ctx)
        except RuntimeError:
            return await ctx.send("No entry matching this ID found.")

        check = await ctx.prompt(
            f"Are you sure you want to edit entry #{id}?\n\nAvailable information:\n{information}")

        if not check:
            return await ctx.send("Aborting...")

        query = "UPDATE warning_entries SET information = $1 WHERE id = $2"
        await ctx.db.execute(query, text, id)
        await ctx.send("Successfully edited entry.")

    @warn.command(name="delete")
    async def warn_delete(self, ctx, id: entry_id):
        """Deletes a warning or note.
        A double-check is performed to prevent accidents.
        Please inform other moderators before deleting their issued warnings or notes."""
        try:
            information = await self.get_info(id, ctx)
        except RuntimeError:
            return await ctx.send("No entry matching this ID found.")

        check = await ctx.prompt(
            f"Are you sure you want to delete entry #{id}?\nAvailable information:\n\n{information}")

        if not check:
            return await ctx.send("Aborting...")

        query = "DELETE FROM warning_entries WHERE id = $1"
        await ctx.db.execute(query, id)
        await ctx.send("Successfully deleted entry.")

    @warn.command(name="list")
    async def warn_list(self, ctx, member: Union[discord.Member, FetchedUser] = None):
        """Shows all warnings and notes for a member or the server if no member is provided."""

        # Don't show notes and moderator data in public channels.
        is_public = not get_everyone_perms_for(ctx.channel).read_messages is False
        try:
            if member is not None:
                page = await WarningPaginator.from_member(ctx, member, should_redact=is_public)
            else:
                page = await WarningPaginator.from_all(ctx, should_redact=is_public)

            await page.paginate()
        except CannotPaginate as e:
            await ctx.send(e)

    @commands.command(name="mywarnings")
    async def warn_mine(self, ctx):
        """Lists your warnings on this server."""
        try:
            page = await WarningPaginator.from_member(ctx, ctx.author, should_redact=True)
            await page.paginate()
        except CannotPaginate as e:
            await ctx.send(e)


setup = Warnings.setup
