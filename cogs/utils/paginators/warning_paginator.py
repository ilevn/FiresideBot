import itertools

from cogs.utils import Plural
from cogs.utils.paginators import Pages, CannotPaginate


class WarningPaginator(Pages):
    def __init__(self, ctx, entries, *, per_page=4, should_redact=False):
        super().__init__(ctx, entries=entries, per_page=per_page)
        self.total = len(entries)
        self.should_redact = should_redact

    @staticmethod
    def _format_desc(warnings, notes):
        return f"{Plural(warnings):warning}, {Plural(notes):note}"

    @classmethod
    async def from_all(cls, ctx, should_redact=False):
        # Get all entries.
        query = """SELECT id, information, created, warning, member_id, moderator_id
                   FROM warning_entries
                   WHERE guild_id = $1
                   ORDER BY created DESC"""

        records = await ctx.db.fetch(query, ctx.guild.id)
        if not records:
            raise CannotPaginate("No warnings or notes found.")

        nested_pages = []
        per_page = 8

        def key(r):
            return r["member_id"]

        records = sorted(records, key=key)

        # 0: <member>, <len(warnings) len(notes)>, <ID, text, created, warning type>
        # 1: <member>, <len(warnings) len(notes)>, <ID, text, created, warning type>
        # ...
        for member_id, info in itertools.groupby(records, key=key):
            member = ctx.guild.get_member(member_id)
            if not member:
                # Warned member left guild. Don't bother paginating.
                continue

            def key(r):
                return not r["warning"]

            info = sorted(info, key=key)
            notes = sum(1 for _ in filter(key, info))
            if should_redact:
                desc = format(Plural(len(info) - notes), 'warning')
            else:
                desc = cls._format_desc(len(info) - notes, notes)

            needed_info = [r[0:4] + (getattr(ctx.guild.get_member(r[5]), 'name', 'Mod left'),) for r in info]
            nested_pages.extend(
                (str(member), desc, needed_info[i:i + per_page]) for i in range(0, len(needed_info), per_page))

        self = cls(ctx, nested_pages, per_page=1, should_redact=should_redact)
        self.get_page = self.get_member_page
        # This number is off in public channels, but w/e.
        self.total = sum(len(o) for _, _, o in nested_pages)
        return self

    @classmethod
    async def from_member(cls, ctx, member, short_view=False, should_redact=False):
        query = f"""SELECT id, information, created, warning, moderator_id
                    FROM warning_entries
                    WHERE guild_id = $1
                      AND member_id = $2
                    ORDER BY created DESC
                   {'LIMIT 4' if short_view else ''}"""

        guild = ctx.guild
        records = await ctx.db.fetch(query, guild.id, member.id)
        if not records:
            raise CannotPaginate(f"No warnings or notes found for {member}.")

        # Sort by type actual warning > note.
        info = sorted(records, key=lambda r: not r["warning"])

        # Get number of warnings and notes on this member.
        query = """SELECT count(*) FILTER (WHERE NOT warning) AS note,
                          count(*) FILTER (WHERE warning)     AS warned
                   FROM warning_entries
                   WHERE guild_id = $1
                   AND member_id = $2"""

        notes, warnings = await ctx.db.fetchrow(query, guild.id, member.id)
        self = cls(ctx, [r[0:4] + (getattr(guild.get_member(r[4]), 'name', 'Mod left'),) for r in info])
        self.title = f'Overview for {member}'
        self.should_redact = should_redact

        if should_redact:
            self.total = warnings
            self.description = format(Plural(len(info) - notes), 'warning')
        else:
            self.total = warnings + notes
            self.description = cls._format_desc(len(info) - notes, notes)

        return self

    def get_member_page(self, page):
        member, description, info = self.entries[page - 1]
        self.title = f'Overview for {member}'
        self.description = description
        return info

    def format_entry(self, entry):
        if self.should_redact:
            if entry[3] is True:
                return "Warning", f"{entry[2]:%d/%m/%Y} - {entry[1]}"
        else:
            # Get type.
            type_ = "Warning" if entry[3] else "Note"
            # Format the actual entry.
            fmt = f"[{entry[0]}] {entry[2]:%d/%m/%Y} - {entry[1]} [*{entry[4]}*]"
            return type_, fmt

    def prepare_embed(self, entries, page, *, first=False):
        self.embed.clear_fields()
        self.embed.description = self.description
        self.embed.title = self.title
        self.embed.set_footer(text=f'Warning log')

        for entry in entries:
            actual_entry = self.format_entry(entry)
            if not actual_entry:
                continue

            type_, fmt = actual_entry
            self.embed.add_field(name=type_, value=fmt, inline=False)

        if self.maximum_pages:
            self.embed.set_author(name=f'Page {page}/{self.maximum_pages} ({self.total} DB entries)')
