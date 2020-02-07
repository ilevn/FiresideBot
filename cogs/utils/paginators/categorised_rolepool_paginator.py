import itertools

from cogs.utils.paginators import CannotPaginate, Pages


class LazyRole:
    """Meant for use with the internal paginator.
    This lazily computes and caches the request for interactive sessions.
    """
    __slots__ = ("guild", "role_id", "_cache")

    def __init__(self, guild, role_id):
        self.guild = guild
        self.role_id = role_id
        self._cache = None

    def __str__(self):
        if self._cache:
            return self._cache

        if (role := self.guild.get_role(self.role_id)) is None:
            self._cache = f"<Not found: {self.role_id}>"
        else:
            self._cache = role.name
        return self._cache


class RolePoolPages(Pages):
    def __init__(self, ctx, entries, *, per_page=12):
        super().__init__(ctx, entries=entries, per_page=per_page, use_index=False)
        self.total = len(entries)

    @classmethod
    async def from_all(cls, ctx):
        guild = ctx.guild
        query = "SELECT role_id, category FROM roles WHERE guild_id = $1 ORDER BY category"
        records = await ctx.db.fetch(query, guild.id)
        if not records:
            raise CannotPaginate("This server doesn't have any assignable roles.")

        nested_pages = []
        per_page = 8

        def key(record):
            return record["category"]

        for category, role_ids in itertools.groupby(records, key=key):
            lazy_roles = [LazyRole(guild, role_id) for role_id, _ in role_ids]

            nested_pages.extend(
                (category,
                 lazy_roles[i:i + per_page]) for i in range(0, len(lazy_roles), per_page)
            )

        self = cls(ctx, nested_pages, per_page=1)
        self.get_page = self.get_role_page
        self.total = sum(len(o) for _, o in nested_pages)
        return self

    def get_role_page(self, page):
        category, roles = self.entries[page - 1]
        self.title = category.title() if category else "No category"
        return roles

    def prepare_embed(self, entries, page, *, first=False):
        self.embed.title = self.title

        p = []
        for delimiter, entry in self._generate_delim(entries, 1 + ((page - 1) * self.per_page)):
            p.append(f'{delimiter} {entry}')

        if self.maximum_pages:
            self.embed.set_author(name=f"Page {page}/{self.maximum_pages} ({self.total} assignable roles)")

        self.embed.description = "\n".join(p)
