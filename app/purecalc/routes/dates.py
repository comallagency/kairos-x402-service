"""Date/time pure-compute routes - stdlib datetime/zoneinfo only."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from app.purecalc.registry import ComputeError, ComputeSpec, register


def _parse_iso(value: str, field: str = "datetime") -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ComputeError("invalid_datetime", f"{field}: {value!r} is not a valid ISO 8601 datetime") from exc
    return dt


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ComputeError("unknown_timezone", f"{name!r} is not a known IANA timezone") from exc


# --------------------------------------------------------------- convert
class ConvertInput(BaseModel):
    datetime: str
    from_tz: str
    to_tz: str


class ConvertOutput(BaseModel):
    datetime: str


def compute_convert(inp: ConvertInput) -> ConvertOutput:
    dt = _parse_iso(inp.datetime)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(inp.from_tz))
    else:
        dt = dt.astimezone(_zone(inp.from_tz))
    converted = dt.astimezone(_zone(inp.to_tz))
    return ConvertOutput(datetime=converted.isoformat())


register(ComputeSpec(
    slug="time/convert", price="$0.001", service_name="time-convert",
    description="Convert an ISO 8601 datetime from one IANA timezone to another (handles DST automatically).",
    tags=["datetime", "timezone", "convert", "iana", "dst"],
    input_model=ConvertInput, output_model=ConvertOutput, compute=compute_convert,
    sample_input={"datetime": "2026-01-01T12:00:00", "from_tz": "UTC", "to_tz": "America/New_York"},
    sample_output={"datetime": "2026-01-01T07:00:00-05:00"},
))


# -------------------------------------------------------------------- add
class AddInput(BaseModel):
    datetime: str
    days: int = 0
    hours: int = 0
    minutes: int = 0
    seconds: int = 0


class AddOutput(BaseModel):
    datetime: str


def compute_add(inp: AddInput) -> AddOutput:
    dt = _parse_iso(inp.datetime)
    delta = timedelta(days=inp.days, hours=inp.hours, minutes=inp.minutes, seconds=inp.seconds)
    return AddOutput(datetime=(dt + delta).isoformat())


register(ComputeSpec(
    slug="time/add", price="$0.001", service_name="time-add",
    description="Add a fixed duration (days/hours/minutes/seconds) to an ISO 8601 datetime.",
    tags=["datetime", "add", "duration", "date math"],
    input_model=AddInput, output_model=AddOutput, compute=compute_add,
    sample_input={"datetime": "2026-01-01T00:00:00", "days": 1, "hours": 2},
    sample_output={"datetime": "2026-01-02T02:00:00"},
))


# --------------------------------------------------------------- between
class BetweenInput(BaseModel):
    start: str
    end: str


class BetweenOutput(BaseModel):
    total_seconds: float
    days: int
    hours: int
    minutes: int
    seconds: int


def compute_between(inp: BetweenInput) -> BetweenOutput:
    start, end = _parse_iso(inp.start, "start"), _parse_iso(inp.end, "end")
    delta = end - start
    total = delta.total_seconds()
    remainder = abs(delta)
    days = remainder.days
    hours, rem = divmod(remainder.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return BetweenOutput(total_seconds=total, days=days, hours=hours, minutes=minutes, seconds=seconds)


register(ComputeSpec(
    slug="time/between", price="$0.001", service_name="time-between",
    description="Duration between two ISO 8601 datetimes, as total seconds and a days/hours/minutes/seconds breakdown.",
    tags=["datetime", "duration", "difference", "elapsed time"],
    input_model=BetweenInput, output_model=BetweenOutput, compute=compute_between,
    sample_input={"start": "2026-01-01T00:00:00", "end": "2026-01-02T01:30:00"},
    sample_output={"total_seconds": 91800.0, "days": 1, "hours": 1, "minutes": 30, "seconds": 0},
))


# ---------------------------------------------------------- business-days
class BusinessDaysInput(BaseModel):
    start: str
    end: str


class BusinessDaysOutput(BaseModel):
    business_days: int


def compute_business_days(inp: BusinessDaysInput) -> BusinessDaysOutput:
    start = _parse_iso(inp.start, "start").date()
    end = _parse_iso(inp.end, "end").date()
    if end < start:
        raise ComputeError("invalid_range", "end must not be before start")
    count = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return BusinessDaysOutput(business_days=count)


register(ComputeSpec(
    slug="time/business-days", price="$0.001", service_name="time-business-days",
    description="Count weekdays (Mon-Fri) between two dates, inclusive of both endpoints.",
    tags=["datetime", "business days", "weekdays", "working days"],
    input_model=BusinessDaysInput, output_model=BusinessDaysOutput, compute=compute_business_days,
    sample_input={"start": "2026-01-01", "end": "2026-01-07"},
    sample_output={"business_days": 5},
))


# -------------------------------------------------------------- iso-week
class IsoWeekInput(BaseModel):
    date: str


class IsoWeekOutput(BaseModel):
    iso_year: int
    iso_week: int
    iso_weekday: int


def compute_iso_week(inp: IsoWeekInput) -> IsoWeekOutput:
    d = _parse_iso(inp.date, "date").date()
    iso_year, iso_week, iso_weekday = d.isocalendar()
    return IsoWeekOutput(iso_year=iso_year, iso_week=iso_week, iso_weekday=iso_weekday)


register(ComputeSpec(
    slug="time/iso-week", price="$0.001", service_name="time-iso-week",
    description="ISO 8601 week number, ISO year and ISO weekday (1=Monday) for a date.",
    tags=["datetime", "iso week", "week number", "iso 8601"],
    input_model=IsoWeekInput, output_model=IsoWeekOutput, compute=compute_iso_week,
    sample_input={"date": "2026-01-04"},
    sample_output={"iso_year": 2026, "iso_week": 1, "iso_weekday": 7},
))


# ----------------------------------------------------------------- parse
class ParseInput(BaseModel):
    value: str
    format: str | None = None  # strptime directive, e.g. "%d/%m/%Y" - omit for ISO 8601


class ParseOutput(BaseModel):
    iso: str
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int


def compute_parse(inp: ParseInput) -> ParseOutput:
    if inp.format:
        try:
            dt = datetime.strptime(inp.value, inp.format)
        except ValueError as exc:
            raise ComputeError("unparseable", f"{inp.value!r} does not match format {inp.format!r}") from exc
    else:
        dt = _parse_iso(inp.value, "value")
    return ParseOutput(
        iso=dt.isoformat(), year=dt.year, month=dt.month, day=dt.day,
        hour=dt.hour, minute=dt.minute, second=dt.second,
    )


register(ComputeSpec(
    slug="time/parse", price="$0.001", service_name="time-parse",
    description="Parse a date/time string (ISO 8601 by default, or a given strptime format) into its components.",
    tags=["datetime", "parse", "strptime", "date string"],
    input_model=ParseInput, output_model=ParseOutput, compute=compute_parse,
    sample_input={"value": "15/03/2026", "format": "%d/%m/%Y"},
    sample_output={"iso": "2026-03-15T00:00:00", "year": 2026, "month": 3, "day": 15, "hour": 0, "minute": 0, "second": 0},
))


# ---------------------------------------------------------------- format
class FormatInput(BaseModel):
    datetime: str
    pattern: str


class FormatOutput(BaseModel):
    formatted: str


def compute_format(inp: FormatInput) -> FormatOutput:
    dt = _parse_iso(inp.datetime)
    try:
        formatted = dt.strftime(inp.pattern)
    except ValueError as exc:
        raise ComputeError("invalid_pattern", str(exc)[:200]) from exc
    return FormatOutput(formatted=formatted)


register(ComputeSpec(
    slug="time/format", price="$0.001", service_name="time-format",
    description="Format an ISO 8601 datetime using a strftime pattern.",
    tags=["datetime", "format", "strftime", "date formatting"],
    input_model=FormatInput, output_model=FormatOutput, compute=compute_format,
    sample_input={"datetime": "2026-03-15T14:30:00", "pattern": "%Y/%m/%d %H:%M"},
    sample_output={"formatted": "2026/03/15 14:30"},
))


# -------------------------------------------------------------- humanize
class HumanizeInput(BaseModel):
    datetime: str
    reference: str | None = None  # defaults to now (UTC) if omitted


class HumanizeOutput(BaseModel):
    humanized: str


_UNITS = (("day", 86400), ("hour", 3600), ("minute", 60))


def compute_humanize(inp: HumanizeInput) -> HumanizeOutput:
    target = _parse_iso(inp.datetime, "datetime")
    reference = _parse_iso(inp.reference, "reference") if inp.reference else datetime.now(target.tzinfo)
    delta_seconds = (target - reference).total_seconds()
    future = delta_seconds >= 0
    magnitude = abs(delta_seconds)

    if magnitude < 60:
        return HumanizeOutput(humanized="just now")

    for name, size in _UNITS:
        if magnitude >= size:
            n = int(magnitude // size)
            plural = "s" if n != 1 else ""
            phrase = f"{n} {name}{plural}"
            return HumanizeOutput(humanized=f"in {phrase}" if future else f"{phrase} ago")

    return HumanizeOutput(humanized="just now")


register(ComputeSpec(
    slug="time/humanize", price="$0.002", service_name="time-humanize",
    description="Human-readable relative time ('2 hours ago', 'in 3 days') between a datetime and a reference (default now).",
    tags=["datetime", "humanize", "relative time", "fuzzy time"],
    input_model=HumanizeInput, output_model=HumanizeOutput, compute=compute_humanize,
    sample_input={"datetime": "2026-01-01T10:00:00", "reference": "2026-01-01T12:00:00"},
    sample_output={"humanized": "2 hours ago"},
))
