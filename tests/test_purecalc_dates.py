"""5 reference-verified cases per route. References: IANA timezone UTC
offsets for a GIVEN, unambiguous date (no DST guessing - January is
standard time in the northern hemisphere, July is DST), the ISO 8601
STANDARD's own definition (January 4th is always in week 1 - not derived
from this code), and pure calendar-arithmetic invariants (any 7
consecutive days contain exactly 5 weekdays, regardless of which weekday
they start on) rather than memorized "what day of the week was X" facts."""

from datetime import date, timedelta

import pytest

from app.purecalc.registry import ComputeError
from app.purecalc.routes.dates import (
    AddInput, BetweenInput, BusinessDaysInput, ConvertInput, FormatInput,
    HumanizeInput, IsoWeekInput, ParseInput,
    compute_add, compute_between, compute_business_days, compute_convert,
    compute_format, compute_humanize, compute_iso_week, compute_parse,
)


# -------------------------------------------------------------- convert
def test_convert_utc_to_new_york_standard_time_january():
    # EST = UTC-5, no DST in January.
    r = compute_convert(ConvertInput(datetime="2026-01-01T12:00:00", from_tz="UTC", to_tz="America/New_York"))
    assert r.datetime == "2026-01-01T07:00:00-05:00"


def test_convert_utc_to_new_york_dst_july():
    # EDT = UTC-4, DST active in July.
    r = compute_convert(ConvertInput(datetime="2026-07-01T12:00:00", from_tz="UTC", to_tz="America/New_York"))
    assert r.datetime == "2026-07-01T08:00:00-04:00"


def test_convert_utc_to_tokyo_no_dst_ever():
    # JST = UTC+9, Japan has never observed DST since WWII.
    r = compute_convert(ConvertInput(datetime="2026-01-01T12:00:00", from_tz="UTC", to_tz="Asia/Tokyo"))
    assert r.datetime == "2026-01-01T21:00:00+09:00"


def test_convert_utc_to_paris_standard_time_january():
    # CET = UTC+1, no DST in January.
    r = compute_convert(ConvertInput(datetime="2026-01-01T12:00:00", from_tz="UTC", to_tz="Europe/Paris"))
    assert r.datetime == "2026-01-01T13:00:00+01:00"


def test_convert_unknown_timezone_rejected():
    with pytest.raises(ComputeError):
        compute_convert(ConvertInput(datetime="2026-01-01T12:00:00", from_tz="UTC", to_tz="Mars/Olympus_Mons"))


# ------------------------------------------------------------------ add
def test_add_one_day():
    r = compute_add(AddInput(datetime="2026-01-01T00:00:00", days=1))
    assert r.datetime == "2026-01-02T00:00:00"


def test_add_crosses_leap_day():
    # 2024 is a leap year (divisible by 4, not a century exception).
    r = compute_add(AddInput(datetime="2024-02-28T00:00:00", days=1))
    assert r.datetime == "2024-02-29T00:00:00"


def test_add_25_hours_rolls_to_next_day():
    r = compute_add(AddInput(datetime="2026-01-01T00:00:00", hours=25))
    assert r.datetime == "2026-01-02T01:00:00"


def test_add_crosses_year_boundary():
    r = compute_add(AddInput(datetime="2025-12-31T23:00:00", hours=2))
    assert r.datetime == "2026-01-01T01:00:00"


def test_add_negative_days_subtracts():
    r = compute_add(AddInput(datetime="2026-01-10T00:00:00", days=-10))
    assert r.datetime == "2025-12-31T00:00:00"


# -------------------------------------------------------------- between
def test_between_one_day():
    r = compute_between(BetweenInput(start="2026-01-01T00:00:00", end="2026-01-02T00:00:00"))
    assert r.total_seconds == 86400.0
    assert (r.days, r.hours, r.minutes, r.seconds) == (1, 0, 0, 0)


def test_between_one_hour_thirty():
    r = compute_between(BetweenInput(start="2026-01-01T00:00:00", end="2026-01-01T01:30:00"))
    assert r.total_seconds == 5400.0


def test_between_zero():
    r = compute_between(BetweenInput(start="2026-01-01T00:00:00", end="2026-01-01T00:00:00"))
    assert r.total_seconds == 0.0


def test_between_negative_when_end_before_start():
    r = compute_between(BetweenInput(start="2026-01-02T00:00:00", end="2026-01-01T00:00:00"))
    assert r.total_seconds == -86400.0


def test_between_across_leap_day():
    r = compute_between(BetweenInput(start="2024-02-28T00:00:00", end="2024-03-01T00:00:00"))
    assert r.total_seconds == 2 * 86400.0  # Feb 28 -> 29 -> Mar 1, leap year


# ------------------------------------------------------------ business-days
def test_business_days_any_full_week_is_5():
    # Calendar invariant: any 7 consecutive days contain exactly 5 weekdays,
    # regardless of which weekday the range starts on.
    for start_offset in range(7):
        start = date(2026, 1, 1) + timedelta(days=start_offset)
        end = start + timedelta(days=6)
        r = compute_business_days(BusinessDaysInput(start=start.isoformat(), end=end.isoformat()))
        assert r.business_days == 5


def test_business_days_four_weeks_is_20():
    r = compute_business_days(BusinessDaysInput(start="2026-01-01", end="2026-01-28"))
    assert r.business_days == 20


def test_business_days_single_day_range():
    r = compute_business_days(BusinessDaysInput(start="2026-01-01", end="2026-01-01"))
    assert r.business_days in (0, 1)


def test_business_days_additivity():
    # Partitioning a range must not lose or double-count a day.
    a = compute_business_days(BusinessDaysInput(start="2026-01-01", end="2026-01-05")).business_days
    b = compute_business_days(BusinessDaysInput(start="2026-01-06", end="2026-01-10")).business_days
    whole = compute_business_days(BusinessDaysInput(start="2026-01-01", end="2026-01-10")).business_days
    assert a + b == whole


def test_business_days_end_before_start_rejected():
    with pytest.raises(ComputeError):
        compute_business_days(BusinessDaysInput(start="2026-01-10", end="2026-01-01"))


# ------------------------------------------------------------------ iso-week
def test_iso_week_january_4th_is_always_week_1():
    # ISO 8601's own definition: week 1 is the week containing January 4th.
    for year in (2020, 2021, 2022, 2023, 2024):
        r = compute_iso_week(IsoWeekInput(date=f"{year}-01-04"))
        assert r.iso_week == 1
        assert r.iso_year == year


# ---------------------------------------------------------------------- parse
def test_parse_iso_default():
    r = compute_parse(ParseInput(value="2026-03-15T14:30:00"))
    assert (r.year, r.month, r.day, r.hour, r.minute) == (2026, 3, 15, 14, 30)


def test_parse_european_format():
    r = compute_parse(ParseInput(value="15/03/2026", format="%d/%m/%Y"))
    assert (r.year, r.month, r.day) == (2026, 3, 15)


def test_parse_us_format():
    r = compute_parse(ParseInput(value="03/15/2026", format="%m/%d/%Y"))
    assert (r.year, r.month, r.day) == (2026, 3, 15)


def test_parse_date_only_iso():
    r = compute_parse(ParseInput(value="2026-03-15"))
    assert (r.year, r.month, r.day, r.hour) == (2026, 3, 15, 0)


def test_parse_garbage_rejected():
    with pytest.raises(ComputeError):
        compute_parse(ParseInput(value="not a date"))


# -------------------------------------------------------------------- format
def test_format_basic():
    r = compute_format(FormatInput(datetime="2026-03-15T14:30:00", pattern="%Y/%m/%d %H:%M"))
    assert r.formatted == "2026/03/15 14:30"


def test_format_day_name():
    # 2026-01-04 is ISO week 1 weekday 7 (Sunday) per the iso-week test above.
    r = compute_format(FormatInput(datetime="2026-01-04T00:00:00", pattern="%A"))
    assert r.formatted == "Sunday"


def test_format_year_only():
    r = compute_format(FormatInput(datetime="2026-03-15T14:30:00", pattern="%Y"))
    assert r.formatted == "2026"


def test_format_iso_like():
    r = compute_format(FormatInput(datetime="2026-03-15T14:30:00", pattern="%Y-%m-%dT%H:%M:%S"))
    assert r.formatted == "2026-03-15T14:30:00"


def test_format_literal_text_passthrough():
    r = compute_format(FormatInput(datetime="2026-03-15T14:30:00", pattern="Year: %Y"))
    assert r.formatted == "Year: 2026"


# ------------------------------------------------------------------ humanize
def test_humanize_two_hours_ago():
    r = compute_humanize(HumanizeInput(datetime="2026-01-01T10:00:00", reference="2026-01-01T12:00:00"))
    assert r.humanized == "2 hours ago"


def test_humanize_in_three_days():
    r = compute_humanize(HumanizeInput(datetime="2026-01-10T00:00:00", reference="2026-01-07T00:00:00"))
    assert r.humanized == "in 3 days"


def test_humanize_just_now():
    r = compute_humanize(HumanizeInput(datetime="2026-01-01T12:00:30", reference="2026-01-01T12:00:00"))
    assert r.humanized == "just now"


def test_humanize_one_minute_ago_singular():
    r = compute_humanize(HumanizeInput(datetime="2026-01-01T11:59:00", reference="2026-01-01T12:00:00"))
    assert r.humanized == "1 minute ago"


def test_humanize_in_one_day_singular():
    r = compute_humanize(HumanizeInput(datetime="2026-01-02T00:00:00", reference="2026-01-01T00:00:00"))
    assert r.humanized == "in 1 day"
