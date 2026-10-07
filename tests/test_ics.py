"""The calendar reader: what is on a day, from an iCalendar file."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from mnn import ics

NEW_YORK = ZoneInfo("America/New_York")
TUESDAY = dt.date(2026, 10, 6)


def calendar(*events: str) -> str:
    body = "".join(f"BEGIN:VEVENT\r\n{event.strip()}\r\nEND:VEVENT\r\n" for event in events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n{body}END:VCALENDAR\r\n"


def day(text: str, date: dt.date = TUESDAY, zone=NEW_YORK) -> list[str]:
    """Each thing on the day as one line, the way a test can read it."""
    found, _ = ics.on_day(text, date, zone)
    return [f"all day {o.title}" if o.all_day else
            f"{o.start:%H:%M}{f'-{o.end:%H:%M}' if o.end else ''} {o.title}" for o in found]


def test_a_timed_event_is_read_in_the_readers_own_zone():
    text = calendar("""
        SUMMARY:Dentist
        LOCATION:12 Harbour Road\\, Port Avery
        DTSTART:20261006T183000Z
        DTEND:20261006T191500Z
    """.replace("        ", ""))
    found, notes = ics.on_day(text, TUESDAY, NEW_YORK)
    assert day(text) == ["14:30-15:15 Dentist"]
    assert found[0].location == "12 Harbour Road, Port Avery" and notes == []
    # The same moment is the next morning in Tokyo, and not on the 6th at all.
    assert day(text, zone=ZoneInfo("Asia/Tokyo")) == []
    assert day(text, dt.date(2026, 10, 7), ZoneInfo("Asia/Tokyo")) == ["03:30-04:15 Dentist"]


def test_zones_floating_times_and_all_day_events():
    text = calendar(
        "SUMMARY:Call with Lisbon\nDTSTART;TZID=Europe/Lisbon:20261006T150000\nDURATION:PT45M",
        "SUMMARY:Walk\nDTSTART:20261006T073000",
        "SUMMARY:Unknown zone\nDTSTART;TZID=Eastern Standard Time:20261006T120000",
        "SUMMARY:Conference\nDTSTART;VALUE=DATE:20261005\nDTEND;VALUE=DATE:20261008",
        "SUMMARY:Birthday\nDTSTART;VALUE=DATE:20261006",
        "SUMMARY:Yesterday only\nDTSTART;VALUE=DATE:20261005",
        "SUMMARY:Called off\nDTSTART:20261006T090000\nSTATUS:CANCELLED",
    )
    assert day(text) == ["all day Birthday", "all day Conference", "07:30 Walk",
                         "10:00-10:45 Call with Lisbon", "12:00 Unknown zone"]


def test_folded_lines_escapes_and_alarms_inside_an_event():
    text = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:A very long title that the cale\r\n"
            " ndar folded\\; with a semicolon\\nand a second line\r\n"
            "DTSTART:20261006T100000\r\nBEGIN:VALARM\r\nTRIGGER:-PT10M\r\n"
            "SUMMARY:Reminder\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    assert day(text) == [
        "10:00 A very long title that the calendar folded; with a semicolon and a second line"]


@pytest.mark.parametrize("rule, dates", [
    ("FREQ=DAILY", ["10-05", "10-06", "10-07"]),
    ("FREQ=DAILY;INTERVAL=2", ["10-05", "10-07", "10-09"]),
    ("FREQ=DAILY;COUNT=2", ["10-05", "10-06"]),
    ("FREQ=DAILY;UNTIL=20261006T235959Z", ["10-05", "10-06"]),
    ("FREQ=WEEKLY", ["10-05", "10-12", "10-19"]),
    ("FREQ=WEEKLY;BYDAY=MO,WE,FR", ["10-05", "10-07", "10-09", "10-12"]),
    ("FREQ=WEEKLY;INTERVAL=2;BYDAY=TU", ["10-06", "10-20"]),
    ("FREQ=WEEKLY;BYDAY=MO,WE;COUNT=3", ["10-05", "10-07", "10-12"]),
    ("FREQ=MONTHLY", ["10-05", "11-05"]),
    ("FREQ=MONTHLY;BYDAY=1MO", ["10-05", "11-02"]),
    ("FREQ=MONTHLY;BYDAY=-1FR", ["10-30", "11-27"]),
    ("FREQ=MONTHLY;BYMONTHDAY=15,-1", ["10-15", "10-31", "11-15", "11-30"]),
    ("FREQ=YEARLY", ["10-05"]),
])
def test_repeating_events_fall_on_the_right_days(rule, dates):
    text = calendar(f"SUMMARY:Standup\nDTSTART;TZID=America/New_York:20261005T093000\nRRULE:{rule}")
    span = [dt.date(2026, 10, 5) + dt.timedelta(days=i) for i in range(60)]
    on = [f"{date:%m-%d}" for date in span if day(text, date)]
    assert on[:len(dates)] == dates
    if "COUNT" in rule or "UNTIL" in rule or rule == "FREQ=YEARLY":
        assert on == dates


def test_a_series_keeps_its_wall_clock_time_across_a_clock_change():
    text = calendar("SUMMARY:Standup\nDTSTART;TZID=America/New_York:20261005T093000\n"
                    "RRULE:FREQ=WEEKLY")
    # Clocks go back on 1 November 2026: 09:30 stays 09:30.
    assert day(text, dt.date(2026, 11, 9)) == ["09:30 Standup"]
    # Seen from London, it moves by the hour the two zones differ for a week.
    london = ZoneInfo("Europe/London")
    assert day(text, dt.date(2026, 10, 19), london) == ["14:30 Standup"]
    assert day(text, dt.date(2026, 10, 26), london) == ["13:30 Standup"]


def test_dates_left_out_of_a_series_and_a_meeting_moved_out_of_it():
    text = calendar(
        "UID:standup\nSUMMARY:Standup\nDTSTART;TZID=America/New_York:20261005T093000\n"
        "RRULE:FREQ=DAILY\nEXDATE;TZID=America/New_York:20261007T093000",
        "UID:standup\nSUMMARY:Standup (moved)\n"
        "RECURRENCE-ID;TZID=America/New_York:20261006T093000\n"
        "DTSTART;TZID=America/New_York:20261006T110000\nDTEND;TZID=America/New_York:20261006T113000",
    )
    assert day(text, dt.date(2026, 10, 5)) == ["09:30 Standup"]
    assert day(text, dt.date(2026, 10, 6)) == ["11:00-11:30 Standup (moved)"]
    assert day(text, dt.date(2026, 10, 7)) == []
    assert day(text, dt.date(2026, 10, 8)) == ["09:30 Standup"]


def test_an_all_day_series_and_an_event_before_its_first_day():
    text = calendar("SUMMARY:Bins out\nDTSTART;VALUE=DATE:20261006\nRRULE:FREQ=WEEKLY")
    assert day(text) == ["all day Bins out"]
    assert day(text, dt.date(2026, 10, 13)) == ["all day Bins out"]
    assert day(text, dt.date(2026, 9, 29)) == []
    assert day(text, dt.date(2026, 10, 7)) == []


@pytest.mark.parametrize("rule", [
    "FREQ=MONTHLY;BYSETPOS=-1;BYDAY=MO,TU,WE,TH,FR", "FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
    "FREQ=HOURLY", "FREQ=WEEKLY;BYDAY=1MO", "FREQ=DAILY;INTERVAL=0", "FREQ=DAILY;COUNT=many",
    "FREQ=MONTHLY;BYDAY=MO",
])
def test_a_rule_this_reader_does_not_follow_is_named_not_guessed(rule):
    text = calendar(f"SUMMARY:Payday\nDTSTART:20261001T090000\nRRULE:{rule}",
                    "SUMMARY:Lunch\nDTSTART:20261006T120000")
    found, notes = ics.on_day(text, TUESDAY, NEW_YORK)
    assert [o.title for o in found] == ["Lunch"]
    assert notes == ["a repeating event uses a rule this reader does not follow, "
                     "so it was left out: Payday"]


def test_what_is_not_a_calendar_is_refused_and_a_bad_date_is_noted():
    with pytest.raises(ics.CalendarError, match="not an iCalendar file"):
        ics.on_day("<html><body>Sign in</body></html>", TUESDAY, NEW_YORK)
    text = calendar("SUMMARY:Broken\nDTSTART:next tuesday", "SUMMARY:Fine\nDTSTART:20261006T080000")
    found, notes = ics.on_day(text, TUESDAY, NEW_YORK)
    assert [o.title for o in found] == ["Fine"] and "not a date or time" in notes[0]


def test_the_same_event_in_two_calendars_is_listed_once():
    event = "SUMMARY:Lunch\nDTSTART:20261006T120000"
    assert day(calendar(event, event)) == ["12:00 Lunch"]
