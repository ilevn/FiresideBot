import re
from datetime import datetime, date

import parsedatetime as pdt
from dateutil.relativedelta import relativedelta
from discord.ext import commands

from cogs.utils.formatting import Plural, human_join

__all__ = ("human_timedelta", "ShortTime", "HumanTime", "Time", "FutureTime", "UserFriendlyTime")


def human_timedelta(dt, *, source=None, accuracy=3, brief=False, suffix=True):
    now = source or datetime.utcnow()
    # Microsecond free zone
    now = now.replace(microsecond=0)
    dt = dt.replace(microsecond=0)

    if dt > now:
        delta = relativedelta(dt, now)
        suffix = ""
    else:
        delta = relativedelta(now, dt)
        suffix = " ago" if suffix else ""

    attrs = [
        ("year", "y"),
        ("month", "mo"),
        ("day", "d"),
        ("hour", "h"),
        ("minute", "m"),
        ("second", "s"),
    ]

    output = []
    for attr, brief_attr in attrs:
        elem = getattr(delta, attr + "s")
        if not elem:
            continue

        if attr == "day":
            weeks = delta.weeks
            if weeks:
                elem -= weeks * 7
                if not brief:
                    output.append(format(Plural(weeks), "week"))
                else:
                    output.append(f"{weeks}w")

        if elem <= 0:
            continue

        if brief:
            output.append(f"{elem}{brief_attr}")
        else:
            output.append(format(Plural(elem), attr))

    if accuracy is not None:
        output = output[:accuracy]

    if len(output) == 0:
        return "now"
    else:
        if not brief:
            return human_join(output, final="and") + suffix
        else:
            return " ".join(output) + suffix


class ShortTime:
    compiled = re.compile("""(?:(?P<years>[0-9])(?:years?|y))?               # e.g. 4y
                             (?:(?P<months>[0-9]{1,2})(?:months?|mo))?       # e.g. 5months
                             (?:(?P<weeks>[0-9]{1,4})(?:weeks?|w))?          # e.g. 12w
                             (?:(?P<days>[0-9]{1,5})(?:days?|d))?            # e.g. 31d
                             (?:(?P<hours>[0-9]{1,5})(?:hours?|h))?          # e.g. 12h
                             (?:(?P<minutes>[0-9]{1,5})(?:minutes?|min|m))?  # e.g. 5m
                             (?:(?P<seconds>[0-9]{1,5})(?:seconds?|s))?      # e.g. 15s
                          """, re.VERBOSE)

    date_compiled = re.compile(
        r"(?:(?P<day>0?[1-9]|[12][0-9]|3[01])([/\-.])(?P<month>0?[1-9]|1[012])\2(?P<year>\d{4}))?")

    def __init__(self, argument):
        match = self.compiled.fullmatch(argument)
        if match and match.group(0):
            data = {k: int(v) for k, v in match.groupdict(default=0).items()}
            now = datetime.utcnow()
            self.dt = now + relativedelta(**data)
        else:
            match = self.date_compiled.fullmatch(argument)
            if match is None or not match.group(0):
                raise commands.BadArgument("invalid time provided")
            # Argument is a date.
            data = {k: int(v) for k, v in match.groupdict(default=0).items()}
            self.dt = date(**data)


class HumanTime:
    calendar = pdt.Calendar(version=pdt.VERSION_CONTEXT_STYLE)

    def __init__(self, argument):
        now = datetime.utcnow()
        dt, status = self.calendar.parseDT(argument, sourceTime=now)
        if not status.hasDateOrTime:
            raise commands.BadArgument("invalid time provided, try e.g. 'tomorrow' or '3 days'")

        if not status.hasTime:
            # Use current time.
            dt = dt.replace(hour=now.hour, minute=now.minute, second=now.second, microsecond=now.microsecond)

        if status.accuracy == pdt.pdtContext.ACU_HALFDAY:
            try:
                dt = dt.replace(day=now.day + 1)
            except ValueError:
                # Oh dear.
                dt += relativedelta(days=1)

        self.dt = dt
        self._past = dt < now


class Time(HumanTime):
    def __init__(self, argument):
        try:
            o = ShortTime(argument)
        except:
            super().__init__(argument)
        else:
            self.dt = o.dt
            self._past = False


class FutureTime(Time):
    def __init__(self, argument):
        super().__init__(argument)

        if self._past:
            raise commands.BadArgument("This time is in the past")


class UserFriendlyTime(commands.Converter):
    """Don"t require quotes for time parsing."""

    def __init__(self, converter=None, *, default=None):
        if isinstance(converter, type) and issubclass(converter, commands.Converter):
            converter = converter()

        if converter is not None and not isinstance(converter, commands.Converter):
            raise TypeError("commands.Converter subclass necessary.")

        self.converter = converter
        self.default = default

    async def check_constraints(self, ctx, now, remaining):
        if self.dt < now:
            raise commands.BadArgument("This time is in the past.")

        if not remaining:
            if self.default is None:
                raise commands.BadArgument("Missing argument after the time.")
            remaining = self.default

        if self.converter is not None:
            self.arg = await self.converter.convert(ctx, remaining)
        else:
            self.arg = remaining
        return self

    @staticmethod
    def extract_match(argument, match):
        data = {k: int(v) for k, v in match.groupdict(default=0).items()}
        remaining = argument[match.end():].strip()
        return data, remaining

    async def convert(self, ctx, argument):
        try:
            calendar = HumanTime.calendar
            regex, date_regex = ShortTime.compiled, ShortTime.date_compiled
            now = datetime.utcnow()

            match = regex.match(argument)
            if match is not None and match.group(0):
                data, remaining = self.extract_match(argument, match)
                self.dt = now + relativedelta(**data)
                return await self.check_constraints(ctx, now, remaining)

            match = date_regex.match(argument)
            if match is not None and match.group(0):
                data, remaining = self.extract_match(argument, match)
                self.dt = datetime.combine(date(**data), now.time())
                return await self.check_constraints(ctx, now, remaining)

            # NLP has a thing against "from now", however, it does like "from X".
            # We"re going to handle this here.
            if argument.endswith("from now"):
                argument = argument[:-8].strip()

            if argument[0:2] == "me":
                # Starts with "me to" or "me in" or "me at".
                if argument[0:6] in ("me to ", "me in ", "me at "):
                    argument = argument[6:]

            elements = calendar.nlp(argument, sourceTime=now)
            if elements is None or len(elements) == 0:
                raise commands.BadArgument("Invalid time provided, try e.g. 'tomorrow' or '3 days'.")

            # Handle the following:
            # "date time" foo
            # date time foo
            # foo date time

            # Start with the first two cases:
            dt, status, begin, end, dt_string = elements[0]

            if not status.hasDateOrTime:
                raise commands.BadArgument("Invalid time provided, try e.g. 'tomorrow' or '3 days'.")

            if begin not in (0, 1) and end != len(argument):
                raise commands.BadArgument("Time is either in an inappropriate location, which "
                                           "must be either at the end or beginning of your input, "
                                           "or I just flat out did not understand what you meant. Sorry.")

            if not status.hasTime:
                # Use the current time
                dt = dt.replace(hour=now.hour, minute=now.minute, second=now.second, microsecond=now.microsecond)

            if status.accuracy == pdt.pdtContext.ACU_HALFDAY:
                try:
                    dt = dt.replace(day=now.day + 1)
                except ValueError:
                    # Oh dear.
                    dt += relativedelta(days=1)

            self.dt = dt

            if begin in (0, 1):
                if begin == 1:
                    # Quoted?:
                    if argument[0] != '"':
                        raise commands.BadArgument("Expected quote before time input...")

                    if not (end < len(argument) and argument[end] == '"'):
                        raise commands.BadArgument("If the time is quoted, you must unquote it.")

                    remaining = argument[end + 1:].lstrip(" ,.!")
                else:
                    remaining = argument[end:].lstrip(" ,.!")
            elif len(argument) == end:
                remaining = argument[:begin].strip()

            return await self.check_constraints(ctx, now, remaining)
        except:
            import traceback
            traceback.print_exc()
            raise
