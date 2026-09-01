"""Static US travel-demand calendar helpers."""
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# Validation failures intentionally use ValueError for a single public error type.
# ruff: noqa: TRY004

INTENSITIES = frozenset({"elevated", "peak"})
TravelPeriodPayload = dict[str, str | bool | int]


@dataclass(frozen=True)
class TravelPeriod:
    name: str
    start: date
    end: date
    intensity: str
    note: str

    def to_json(self) -> dict[str, str]:
        return {
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "intensity": self.intensity,
            "note": self.note,
        }


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the nth occurrence of a weekday in a month."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + (n - 1) * 7)


def last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of a weekday in a month."""
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def thanksgiving(year: int) -> date:
    return nth_weekday(year, 11, 3, 4)


def memorial_day(year: int) -> date:
    return last_weekday(year, 5, 0)


def labor_day(year: int) -> date:
    return nth_weekday(year, 9, 0, 1)


def build_periods(years: Sequence[int]) -> list[TravelPeriod]:
    periods: list[TravelPeriod] = []
    for year in years:
        thanksgiving_day = thanksgiving(year)
        memorial = memorial_day(year)
        labor = labor_day(year)
        periods.extend(
            [
                TravelPeriod(
                    "Spring break",
                    date(year, 3, 15),
                    date(year, 4, 15),
                    "elevated",
                    "Mid-March to mid-April spring break season — elevated leisure traffic, varies by school district.",
                ),
                TravelPeriod(
                    "Memorial Day weekend",
                    memorial - timedelta(days=3),
                    memorial,
                    "elevated",
                    "Friday through Memorial Day Monday.",
                ),
                TravelPeriod(
                    "Independence Day",
                    date(year, 7, 1),
                    date(year, 7, 7),
                    "elevated",
                    "Week around the Fourth of July.",
                ),
                TravelPeriod(
                    "Labor Day weekend",
                    labor - timedelta(days=3),
                    labor,
                    "elevated",
                    "Friday through Labor Day Monday.",
                ),
                TravelPeriod(
                    "Thanksgiving",
                    thanksgiving_day - timedelta(days=2),
                    thanksgiving_day + timedelta(days=3),
                    "peak",
                    "Tuesday before Thanksgiving through the Sunday after — the busiest checkpoint week of the year.",
                ),
                TravelPeriod(
                    "Winter holidays",
                    date(year, 12, 20),
                    date(year + 1, 1, 2),
                    "peak",
                    "Christmas through New Year's Day travel period.",
                ),
            ]
        )
    return sorted(periods, key=lambda period: (period.start, period.name))


def period_payload(period: TravelPeriod, today: date) -> TravelPeriodPayload:
    active = period.start <= today <= period.end
    return {
        "name": period.name,
        "start": period.start.isoformat(),
        "end": period.end.isoformat(),
        "intensity": period.intensity,
        "note": period.note,
        "active": active,
        "days_until": 0 if active else (period.start - today).days,
    }


def select_period(
    periods: Sequence[TravelPeriod], today: date
) -> TravelPeriodPayload | None:
    for period in sorted(periods, key=lambda item: (item.start, item.name)):
        if period.end >= today:
            return period_payload(period, today)
    return None


def load_periods(path: str | Path) -> list[TravelPeriod]:
    try:
        with open(path, encoding="utf-8") as file:
            entries = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid travel calendar JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise ValueError("travel calendar must be a JSON list")

    expected_keys = {"name", "start", "end", "intensity", "note"}
    periods: list[TravelPeriod] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"entry {index} must be an object")
        if set(entry) != expected_keys:
            raise ValueError(f"entry {index} must contain exactly name, start, end, intensity, note")
        name = entry["name"]
        note = entry["note"]
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError(f"entry {index} name must be non-empty and at most 200 characters")
        if not isinstance(note, str) or not note.strip() or len(note) > 200:
            raise ValueError(f"entry {index} note must be non-empty and at most 200 characters")
        start_value = entry["start"]
        end_value = entry["end"]
        if not isinstance(start_value, str):
            raise ValueError(f"entry {index} start must be an ISO date string")
        if not isinstance(end_value, str):
            raise ValueError(f"entry {index} end must be an ISO date string")
        try:
            start = date.fromisoformat(start_value)
        except ValueError as exc:
            raise ValueError(f"entry {index} start is not a valid ISO date") from exc
        try:
            end = date.fromisoformat(end_value)
        except ValueError as exc:
            raise ValueError(f"entry {index} end is not a valid ISO date") from exc
        intensity = entry["intensity"]
        if not isinstance(intensity, str) or intensity not in INTENSITIES:
            raise ValueError(f"entry {index} intensity must be one of {sorted(INTENSITIES)}")
        if end < start:
            raise ValueError(f"entry {index} end must be on or after start")
        periods.append(TravelPeriod(name, start, end, intensity, note))
    return periods
