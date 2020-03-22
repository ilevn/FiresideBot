from enum import Enum, auto

import discord


class ActionType(Enum):
    BAN = auto()
    UNBAN = auto()
    KICK = auto()
    SHITPOST = auto()
    JAIL = auto()

    @property
    def title(self):
        return self.name.title()


class Punishment:
    """A punishment model used by `on_punishment_add` and `on_punishment_remove`."""
    __slots__ = ("guild", "target", "moderator", "duration", "reason", "type")

    def __init__(self, guild: discord.Guild, target: discord.Member, moderator: discord.Member,
                 type_: ActionType, reason=None, duration=None):
        self.guild = guild
        self.target = target
        self.moderator = moderator
        self.duration = duration or "Indefinite"
        self.reason = reason
        self.type = type_
