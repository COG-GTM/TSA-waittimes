import json
from datetime import date
from itertools import pairwise
from pathlib import Path

import pytest

from app import main
from app.travel_calendar import (
    INTENSITIES,
    build_periods,
    labor_day,
    last_weekday,
    load_periods,
    memorial_day,
    nth_weekday,
    select_period,
    thanksgiving,
)


def test_holiday_dates() -> None:
    assert [thanksgiving(year) for year in range(2024, 2029)] == [
        date(2024, 11, 28),
        date(2025, 11, 27),
        date(2026, 11, 26),
        date(2027, 11, 25),
        date(2028, 11, 23),
    ]
    assert [memorial_day(year) for year in range(2025, 2028)] == [
        date(2025, 5, 26),
        date(2026, 5, 25),
        date(2027, 5, 31),
    ]
    assert [labor_day(year) for year in range(2025, 2028)] == [
        date(2025, 9, 1),
        date(2026, 9, 7),
        date(2027, 9, 6),
    ]


def test_weekday_helpers() -> None:
    assert nth_weekday(2026, 11, 3, 4) == date(2026, 11, 26)
    assert last_weekday(2025, 5, 0) == date(2025, 5, 26)


def test_thanksgiving_window_is_tuesday_to_sunday() -> None:
    periods = build_periods(range(2024, 2031))
    thanksgiving_periods = [period for period in periods if period.name == "Thanksgiving"]
    period_2026 = next(period for period in thanksgiving_periods if period.start.year == 2026)
    assert (period_2026.start, period_2026.end) == (date(2026, 11, 24), date(2026, 11, 29))
    assert all(period.start.weekday() == 1 and period.end.weekday() == 6 for period in thanksgiving_periods)


def test_winter_holidays_span_year_boundary() -> None:
    periods = build_periods([2024, 2025])
    winters = [period for period in periods if period.name == "Winter holidays"]
    assert [(period.start, period.end) for period in winters] == [
        (date(2024, 12, 20), date(2025, 1, 2)),
        (date(2025, 12, 20), date(2026, 1, 2)),
    ]


def test_build_periods_are_sorted_valid_and_non_overlapping() -> None:
    periods = build_periods(range(2024, 2031))
    assert len(periods) == 42
    assert all(period.intensity in INTENSITIES and period.start <= period.end for period in periods)
    assert periods == sorted(periods, key=lambda period: (period.start, period.name))
    for year in range(2024, 2031):
        year_periods = [period for period in periods if period.start.year == year]
        assert all(left.end < right.start for left, right in pairwise(year_periods))


def test_select_period_active_upcoming_and_past() -> None:
    periods = build_periods([2026])
    active = select_period(periods, date(2026, 11, 26))
    assert active is not None
    assert active["name"] == "Thanksgiving"
    assert active["active"] is True
    assert active["days_until"] == 0
    upcoming = select_period(periods, date(2026, 1, 1))
    assert upcoming is not None
    assert upcoming["name"] == "Spring break"
    assert upcoming["active"] is False
    assert upcoming["days_until"] == 73
    assert select_period(periods, date(2027, 1, 3)) is None


@pytest.mark.asyncio
async def test_sql_travel_period_uses_shared_payload_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    today = date(2026, 9, 1)

    class Cursor:
        row = ("Labor Day weekend", date(2026, 9, 4), date(2026, 9, 7), "elevated", "Friday through Labor Day Monday.")
        query = ""
        params: tuple[date]

        async def execute(self, query: str, params: tuple[date]) -> None:
            self.query = query
            self.params = params

        async def fetchone(self) -> tuple[str, date, date, str, str]:
            return self.row

    cursor = Cursor()
    monkeypatch.setattr(main, "_today", lambda: today)
    result = await main._travel_period(cursor)
    expected = select_period(build_periods([2026]), today)
    assert result == expected
    assert "WHERE end_date >= %s" in cursor.query
    assert cursor.params == (today,)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intensity", "normal"),
        ("end", "2026-01-01"),
        ("start", "not-a-date"),
    ],
)
def test_load_periods_rejects_invalid_values(tmp_path: Path, field: str, value: str) -> None:
    entry = {
        "name": "Test",
        "start": "2026-01-02",
        "end": "2026-01-03",
        "intensity": "elevated",
        "note": "Test note",
    }
    entry[field] = value
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_periods(path)


def test_load_periods_rejects_missing_key(tmp_path: Path) -> None:
    path = tmp_path / "calendar.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "Test",
                    "start": "2026-01-02",
                    "end": "2026-01-03",
                    "intensity": "elevated",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_periods(path)


def test_committed_calendar_matches_generated_periods() -> None:
    path = Path(__file__).parents[1] / "data" / "travel_calendar.json"
    periods = load_periods(path)
    years = sorted({period.start.year for period in periods})
    generated = build_periods(years)
    loaded_tuples = {
        (period.name, period.start, period.end, period.intensity, period.note) for period in periods
    }
    generated_tuples = {
        (period.name, period.start, period.end, period.intensity, period.note) for period in generated
    }
    assert loaded_tuples == generated_tuples
    assert generated_tuples == loaded_tuples
