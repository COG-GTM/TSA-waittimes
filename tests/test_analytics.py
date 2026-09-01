from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app import analytics, main


def test_hour_bucket_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive datetime"):
        analytics.hour_bucket(datetime(2026, 3, 8, 1, 30))  # noqa: DTZ001


def test_hour_bucket_converts_aware_datetime_to_utc() -> None:
    value = datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo("America/New_York"))
    assert analytics.hour_bucket(value) == datetime(2026, 3, 8, 6, tzinfo=UTC)


def test_aggregate_observations_groups_and_orders_rows() -> None:
    rows = [
        analytics.ObservationRow("SEA", 2, "standard", datetime(2026, 9, 1, 1, 15, tzinfo=UTC), 7),
        analytics.ObservationRow("SEA", 1, "standard", datetime(2026, 9, 1, 1, 45, tzinfo=UTC), 8),
        analytics.ObservationRow("SEA", 1, "standard", datetime(2026, 9, 1, 1, 5, tzinfo=UTC), 7),
        analytics.ObservationRow("SEA", 1, "standard", datetime(2026, 9, 1, 2, tzinfo=UTC), None),
        analytics.ObservationRow("SEA", 1, "standard", datetime(2026, 9, 1, 2, 1, tzinfo=UTC), -1),
        analytics.ObservationRow("SEA", 2, "standard", datetime(2026, 9, 1, 1, 20, tzinfo=UTC), 10),
    ]
    assert analytics.aggregate_observations(rows) == [
        analytics.RollupRow("SEA", 1, "standard", datetime(2026, 9, 1, 1, tzinfo=UTC), 8, 8, 2),
        analytics.RollupRow("SEA", 2, "standard", datetime(2026, 9, 1, 1, tzinfo=UTC), 9, 10, 2),
        analytics.RollupRow("SEA", 1, "standard", datetime(2026, 9, 1, 2, tzinfo=UTC), None, None, 0),
    ]


def test_retention_helpers_use_strict_boundaries() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    cutoff = analytics.retention_cutoff(now, 14)
    assert cutoff == now - timedelta(days=14)
    assert analytics.is_expired(cutoff, cutoff) is False
    assert analytics.is_expired(cutoff - timedelta(microseconds=1), cutoff) is True
    with pytest.raises(ValueError, match="naive datetime"):
        analytics.retention_cutoff(datetime(2026, 9, 1), 14)  # noqa: DTZ001


@pytest.mark.parametrize(
    ("name", "default"),
    [
        ("RETENTION_RAW_PAYLOAD_DAYS", 14),
        ("RETENTION_OBSERVATION_DAYS", 90),
        ("RETENTION_FAA_EVENT_DAYS", 180),
        ("RETENTION_WEATHER_ALERT_DAYS", 180),
    ],
)
def test_retention_defaults(name: str, default: int) -> None:
    assert getattr(analytics, name) == default


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_int_env_invalid_values_use_default(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TEST_ANALYTICS_INT", value)
    assert analytics._int_env("TEST_ANALYTICS_INT", 23) == 23


def test_int_env_accepts_positive_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ANALYTICS_INT", "42")
    assert analytics._int_env("TEST_ANALYTICS_INT", 23) == 42


def test_typical_from_hours_emits_all_utc_buckets() -> None:
    hour = datetime(2026, 9, 1, 13, tzinfo=UTC)
    rows = [(hour, value, value) for value in (4, 8, 12, 16)]
    buckets = analytics.typical_from_hours(rows)
    assert len(buckets) == 168
    assert [(bucket.dow, bucket.hour) for bucket in buckets[:3]] == [(0, 0), (0, 1), (0, 2)]
    bucket = buckets[1 * 24 + 13]
    assert bucket == analytics.TypicalBucket(1, 13, 10, 13, 4, 40)
    assert buckets[0] == analytics.TypicalBucket(0, 0, None, None, 0, 0)


def test_seconds_to_minutes_rounds_to_one_decimal() -> None:
    assert analytics.seconds_to_minutes(450) == 7.5
    assert analytics.seconds_to_minutes(451) == 7.5
    assert analytics.seconds_to_minutes(None) is None


@pytest.mark.asyncio
async def test_typical_buckets_executes_parameterized_sql() -> None:
    class Cursor:
        query = ""
        params: tuple[str]

        async def execute(self, query: str, params: tuple[str]) -> None:
            self.query = query
            self.params = params

        async def fetchall(self) -> list[tuple[datetime, int, int]]:
            return [(datetime(2026, 9, 1, 13, tzinfo=UTC), 600, 12)]

    cursor = Cursor()
    buckets = await main._typical_buckets(cursor, "SEA")
    assert "WHERE airport_iata = %s" in cursor.query
    assert cursor.params == ("SEA",)
    assert buckets[1 * 24 + 13].median_seconds == 600


@pytest.mark.asyncio
async def test_typical_endpoint_rejects_invalid_iata_without_echoing_input() -> None:
    submitted = "not-an-airport"
    with pytest.raises(HTTPException) as exc_info:
        await main.api_airport_typical(submitted)
    assert exc_info.value.status_code == 404
    assert submitted not in str(exc_info.value.detail)
