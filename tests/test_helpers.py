"""Unit tests for the pure parsing helpers in app/sources/adapters.py."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.sources.adapters import _den_wait_seconds, _lane, _ts


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3-7", 420),  # DEN ranges are minutes; upper bound wins
        ("0-2", 120),
        ("5", 300),
        (" 10 ", 600),
        ("0", 0),  # a published zero-minute wait, not a missing reading
        ("", None),
        (None, None),
        ("n/a", None),
        ("3 - 7 min", None),  # unparseable tail, not a silent 0
        ("--", None),
        ("1.5", None),
    ],
)
def test_den_wait_seconds(raw: str | None, expected: int | None) -> None:
    assert _den_wait_seconds(raw) == expected


def test_den_wait_seconds_never_returns_minutes() -> None:
    assert _den_wait_seconds("7") == 7 * 60


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("PreCheck", "precheck"),
        ("precheck", "precheck"),
        ("TSA Pre✓® Check", "precheck"),
        ("Pre", "precheck"),
        ("Standard", "standard"),
        ("General Screening", "standard"),
        ("", "standard"),
        ("Premier", "other"),
    ],
)
def test_lane(name: str, expected: str) -> None:
    assert _lane(name) == expected


def test_ts_epoch_seconds() -> None:
    assert _ts(1735689600) == datetime(2025, 1, 1, tzinfo=UTC)


def test_ts_epoch_milliseconds() -> None:
    assert _ts(1735689600000) == datetime(2025, 1, 1, tzinfo=UTC)


def test_ts_seconds_and_milliseconds_agree() -> None:
    assert _ts(1735689600) == _ts(1735689600000)


@pytest.mark.parametrize("value", [None, 0, 0.0])
def test_ts_missing(value: float | None) -> None:
    assert _ts(value) is None


def test_ts_is_timezone_aware() -> None:
    result = _ts(1735689600)
    assert result is not None
    assert result.tzinfo is UTC
