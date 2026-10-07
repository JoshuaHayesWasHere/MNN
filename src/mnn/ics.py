"""Reads an iCalendar (.ics) file for one thing: what is on a given day.

It understands what the calendars people actually subscribe to contain:
timed and all-day events, time zones named the IANA way, events that repeat
daily, weekly, monthly or yearly (with an interval, a count, an end date,
chosen weekdays, and "the second Tuesday" in a month), dates left out of a
series, and single meetings moved out of one. A repeating event that uses a
rule beyond those is not guessed at: it is reported, by name, as one this
reader could not place.

Standard library only.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
BYDAY_RE = re.compile(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)")
# Rule parts this reader places. Any other part makes the rule one it cannot.
UNDERSTOOD = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "WKST"}
# A series is followed no further than this many occurrences.
MAX_OCCURRENCES = 20000
TEXT_LIMIT = 200


class CalendarError(Exception):
    """The file is not a calendar this reader can read."""


@dataclass(frozen=True)
class Occurrence:
    """One thing on the day, in the reader's own time zone."""
    title: str
    all_day: bool
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    location: str = ""

    @property
    def sort_key(self) -> tuple:
        return (not self.all_day, self.start or dt.datetime.min, self.title)


@dataclass
class _Event:
    uid: str = ""
    title: str = ""
    location: str = ""
    start: dt.datetime | dt.date | None = None
    end: dt.datetime | dt.date | None = None
    duration: dt.timedelta | None = None
    zone: ZoneInfo | dt.timezone | None = None
    rule: dict[str, str] = field(default_factory=dict)
    excluded: list[dt.datetime | dt.date] = field(default_factory=list)
    moved_from: dt.datetime | dt.date | None = None
    cancelled: bool = False


# --- Lines and values ------------------------------------------------------


def _lines(text: str) -> list[tuple[str, dict[str, str], str]]:
    """(name, parameters, value) for every content line, with folded lines
    joined."""
    unfolded: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += raw[1:]
        elif raw:
            unfolded.append(raw)
    parsed = []
    for line in unfolded:
        head, colon, value = line.partition(":")
        if not colon:
            continue
        # A quoted parameter may itself hold a colon.
        while head.count('"') % 2 and ":" in value:
            more, _, value = value.partition(":")
            head += ":" + more
        name, *parameters = head.split(";")
        parsed.append((name.upper(), {key.upper(): item.strip('"') for key, _, item in
                                      (parameter.partition("=") for parameter in parameters)},
                       value))
    return parsed


def _text(value: str) -> str:
    text = re.sub(r"\\([nN,;\\])", lambda match: " " if match.group(1) in "nN" else match.group(1),
                  value)
    return " ".join(text.split())[:TEXT_LIMIT]


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return None


def _moment(value: str, parameters: dict[str, str],
            local: dt.tzinfo) -> tuple[dt.datetime | dt.date, dt.tzinfo | None]:
    """A DTSTART-like value as (date or aware datetime, the zone it was
    written in). A time with no zone, or a zone this machine does not know,
    is taken as the reader's own."""
    value = value.strip()
    try:
        if parameters.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
            return dt.datetime.strptime(value, "%Y%m%d").date(), None
        if value.endswith(("Z", "z")):
            return dt.datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(
                tzinfo=dt.timezone.utc), dt.timezone.utc
        zone = _zone(parameters.get("TZID")) or local
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=zone), zone
    except ValueError as exc:
        raise CalendarError(f"not a date or time: {value[:40]!r}") from exc


def _duration(value: str) -> dt.timedelta | None:
    match = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
                         value.strip().upper())
    if not match:
        return None
    sign, weeks, days, hours, minutes, seconds = match.groups()
    length = dt.timedelta(weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0),
                          minutes=int(minutes or 0), seconds=int(seconds or 0))
    return -length if sign == "-" else length


def _events(text: str, local: dt.tzinfo) -> tuple[list[_Event], list[str]]:
    events, notes, current, depth = [], [], None, 0
    seen_calendar = False
    for name, parameters, value in _lines(text):
        if name == "BEGIN":
            kind = value.strip().upper()
            seen_calendar = seen_calendar or kind == "VCALENDAR"
            if kind == "VEVENT" and current is None:
                current, depth = _Event(), 0
            elif current is not None:
                depth += 1  # an alarm inside the event
            continue
        if name == "END":
            if current is not None and depth:
                depth -= 1
            elif current is not None and value.strip().upper() == "VEVENT":
                if current.start is not None:
                    events.append(current)
                current = None
            continue
        if current is None or depth:
            continue
        try:
            if name == "DTSTART":
                current.start, current.zone = _moment(value, parameters, local)
            elif name == "DTEND":
                current.end = _moment(value, parameters, local)[0]
            elif name == "RECURRENCE-ID":
                current.moved_from = _moment(value, parameters, local)[0]
            elif name == "EXDATE":
                current.excluded += [_moment(item, parameters, local)[0]
                                     for item in value.split(",") if item.strip()]
        except CalendarError as exc:
            notes.append(str(exc))
            continue
        if name == "SUMMARY":
            current.title = _text(value)
        elif name == "LOCATION":
            current.location = _text(value)
        elif name == "UID":
            current.uid = value.strip()
        elif name == "DURATION":
            current.duration = _duration(value)
        elif name == "STATUS":
            current.cancelled = value.strip().upper() == "CANCELLED"
        elif name == "RRULE":
            current.rule = {key.upper(): item for key, _, item in
                            (part.partition("=") for part in value.strip().split(";")) if key}
    if not seen_calendar:
        raise CalendarError("not an iCalendar file")
    return events, notes


# --- Repeating events ------------------------------------------------------


class _Unplaceable(Exception):
    """The rule uses something this reader does not follow."""


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> dt.date | None:
    days = [day for day in calendar.Calendar().itermonthdates(year, month)
            if day.month == month and day.weekday() == weekday]
    try:
        return days[ordinal - 1] if ordinal > 0 else days[ordinal]
    except IndexError:
        return None


def _series(first: dt.date, rule: dict[str, str]):
    """The dates a rule falls on, in order, from `first` on. Endless unless
    the caller stops; COUNT and UNTIL are the caller's to apply."""
    if set(rule) - UNDERSTOOD:
        raise _Unplaceable
    frequency = rule.get("FREQ", "").upper()
    try:
        interval = int(rule.get("INTERVAL", "1"))
    except ValueError as exc:
        raise _Unplaceable from exc
    if interval < 1:
        raise _Unplaceable
    by_day = [BYDAY_RE.fullmatch(item.strip().upper()) for item in rule["BYDAY"].split(",")] \
        if rule.get("BYDAY") else []
    if not all(by_day):
        raise _Unplaceable
    days = [(int(match.group(1) or 0), WEEKDAYS.index(match.group(2))) for match in by_day]
    try:
        month_days = [int(item) for item in rule["BYMONTHDAY"].split(",")] \
            if rule.get("BYMONTHDAY") else []
    except ValueError as exc:
        raise _Unplaceable from exc

    if frequency == "DAILY":
        if days or month_days:
            raise _Unplaceable
        day = first
        while True:
            yield day
            day += dt.timedelta(days=interval)
    elif frequency == "WEEKLY":
        if month_days or any(ordinal for ordinal, _ in days):
            raise _Unplaceable
        weekdays = sorted({weekday for _, weekday in days} or {first.weekday()})
        try:
            week_start = WEEKDAYS.index(rule.get("WKST", "MO").upper())
        except ValueError as exc:
            raise _Unplaceable from exc
        week = first - dt.timedelta(days=(first.weekday() - week_start) % 7)
        while True:
            for day in sorted(week + dt.timedelta(days=(weekday - week_start) % 7)
                              for weekday in weekdays):
                if day >= first:
                    yield day
            week += dt.timedelta(weeks=interval)
    elif frequency == "MONTHLY":
        if days and month_days:
            raise _Unplaceable
        year, month = first.year, first.month
        while True:
            if days:
                if not all(ordinal for ordinal, _ in days):
                    raise _Unplaceable
                found = [_nth_weekday(year, month, weekday, ordinal) for ordinal, weekday in days]
            else:
                last = calendar.monthrange(year, month)[1]
                found = [dt.date(year, month, day if day > 0 else last + 1 + day)
                         for day in (month_days or [first.day])
                         if 1 <= (day if day > 0 else last + 1 + day) <= last]
            for day in sorted(day for day in found if day and day >= first):
                yield day
            month += interval
            year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
            if year > dt.MAXYEAR - 1:
                return
    elif frequency == "YEARLY":
        if days or month_days:
            raise _Unplaceable
        year = first.year
        while year < dt.MAXYEAR:
            try:
                yield first.replace(year=year)
            except ValueError:
                pass  # 29 February in a year without one
            year += interval
    else:
        raise _Unplaceable


def _until(rule: dict[str, str], zone: dt.tzinfo | None) -> dt.date | dt.datetime | None:
    value = rule.get("UNTIL", "").strip()
    if not value:
        return None
    try:
        moment = _moment(value, {}, zone or dt.timezone.utc)[0]
    except CalendarError as exc:
        raise _Unplaceable from exc
    if isinstance(moment, dt.datetime) and zone is not None:
        return moment.astimezone(zone)
    return moment


def _wall(moment: dt.datetime | dt.date, zone: dt.tzinfo | None) -> dt.datetime | dt.date:
    """A moment as the clock on the event's own wall reads it."""
    if isinstance(moment, dt.datetime) and zone is not None:
        return moment.astimezone(zone).replace(tzinfo=None)
    return moment


def _starts(event: _Event, moved: set, earliest: dt.date, latest: dt.date) -> list:
    """Every start of the event (a date for an all-day one, else an aware
    datetime) whose own date lies between the two given."""
    start = event.start
    timed = isinstance(start, dt.datetime)
    first = start.astimezone(event.zone).date() if timed else start

    def at(day: dt.date):
        if not timed:
            return day
        clock = start.astimezone(event.zone)
        return dt.datetime.combine(day, clock.time(), tzinfo=event.zone)

    if not event.rule:
        return [start] if earliest <= first <= latest else []
    until = _until(event.rule, event.zone if timed else None)
    try:
        count = int(event.rule["COUNT"]) if "COUNT" in event.rule else None
    except ValueError as exc:
        raise _Unplaceable from exc
    excluded = {_wall(moment, event.zone) for moment in event.excluded} | moved
    found = []
    for number, day in enumerate(_series(first, event.rule), start=1):
        if day > latest or number > MAX_OCCURRENCES or (count is not None and number > count):
            break
        occurrence = at(day)
        if isinstance(until, dt.datetime) and timed:
            if occurrence > until:
                break
        elif until is not None and day > (until.date() if isinstance(until, dt.datetime)
                                          else until):
            break
        if day < earliest:
            continue
        wall = _wall(occurrence, event.zone)
        if wall in excluded or (timed and wall.date() in excluded):
            continue
        found.append(occurrence)
    return found


# --- The day ---------------------------------------------------------------


def on_day(text: str, day: dt.date, local: dt.tzinfo) -> tuple[list[Occurrence], list[str]]:
    """What the calendar holds for `day`, as the reader's clock sees it:
    all-day events first, then by time. The notes name anything left out
    because it could not be read."""
    events, notes = _events(text, local)
    # A meeting moved out of a series replaces that one occurrence of it.
    moved: dict[str, set] = {}
    for event in events:
        if event.moved_from is not None and event.uid:
            moved.setdefault(event.uid, set()).add(_wall(event.moved_from, event.zone))

    found = []
    for event in events:
        if event.cancelled:
            continue
        if event.moved_from is not None:
            event.rule = {}
        timed = isinstance(event.start, dt.datetime)
        if event.end is not None and type(event.end) is type(event.start):
            length = event.end - event.start
        elif event.duration is not None:
            length = event.duration
        else:
            length = dt.timedelta(0) if timed else dt.timedelta(days=1)
        if length < dt.timedelta(0):
            length = dt.timedelta(0)
        # A start in another zone can fall a day either side; an all-day
        # event that began earlier may still be running.
        reach = dt.timedelta(days=1) if timed else max(length, dt.timedelta(days=1))
        try:
            starts = _starts(event, moved.get(event.uid, set()) if event.rule else set(),
                             day - reach, day + dt.timedelta(days=1))
        except _Unplaceable:
            notes.append("a repeating event uses a rule this reader does not follow, "
                         f"so it was left out: {event.title or 'untitled'}")
            continue
        title = event.title or "Untitled event"
        for start in starts:
            if timed:
                begins = start.astimezone(local)
                if begins.date() != day:
                    continue
                ends = (start + length).astimezone(local) if length else None
                found.append(Occurrence(title, False, begins, ends, event.location))
            elif start <= day < start + max(length, dt.timedelta(days=1)):
                found.append(Occurrence(title, True, location=event.location))
    unique = {(o.title, o.all_day, o.start, o.end, o.location): o for o in found}
    return sorted(unique.values(), key=lambda occurrence: occurrence.sort_key), notes
