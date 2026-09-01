"""Unit tests for the pure parsing helpers in app/sources/adapters.py."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.sources.adapters import (
    _den_wait_seconds,
    _lane,
    _sfo_checkpoint_table,
    _sfo_published_at,
    _sfo_wait_seconds,
    _SfoPageParser,
    _ts,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4 mins", 240),
        ("1 mins", 60),
        ("0 mins", 0),  # a published zero-minute wait, not a missing reading
        ("12 min", 720),
        ("Not Available", None),
        ("", None),
        (None, None),
        ("4", None),  # bare number without a unit is not a reading
    ],
)
def test_sfo_wait_seconds(raw: str | None, expected: int | None) -> None:
    assert _sfo_wait_seconds(raw) == expected


def test_sfo_published_at_is_pacific_and_utc_aware() -> None:
    now = datetime(2026, 9, 1, 21, 30, tzinfo=UTC)
    result = _sfo_published_at("Sep 01 at 02:11 pm", now)
    assert result == datetime(2026, 9, 1, 14, 11, tzinfo=PACIFIC)
    assert result is not None and result.tzinfo is UTC


def test_sfo_published_at_rolls_back_a_year_across_new_year() -> None:
    now = datetime(2027, 1, 1, 8, 5, tzinfo=UTC)  # still Dec 31 in Pacific
    result = _sfo_published_at("Dec 31 at 11:58 pm", now)
    assert result == datetime(2026, 12, 31, 23, 58, tzinfo=PACIFIC)


@pytest.mark.parametrize("raw", [None, "", "yesterday", "Sep 01 at 25:00 pm"])
def test_sfo_published_at_unparseable(raw: str | None) -> None:
    assert _sfo_published_at(raw, datetime(2026, 9, 1, tzinfo=UTC)) is None


SFO_TABLE_HTML = """
<table><tr><th>Airline</th><th>Terminal</th></tr><tr><td>United</td><td>3</td></tr></table>
<table class="flysfo-checkpoints-table">
  <thead><tr><th>Checkpoint</th><th>General</th><th>TSA PreCheck</th></tr></thead>
  <tbody>
    <tr><td>Checkpoint <strong>A</strong></td><td>4 mins</td><td>Not Available</td></tr>
    <tr><td>Checkpoint G</td><td>0 mins</td><td>1 mins</td></tr>
  </tbody>
</table>
<p>Checkpoint data last updated: Sep 01 at 02:11 pm</p>
"""


def test_sfo_checkpoint_table_skips_unrelated_tables() -> None:
    parser = _SfoPageParser()
    parser.feed(SFO_TABLE_HTML)
    parser.close()
    found = _sfo_checkpoint_table(parser.tables)
    assert found is not None
    header, rows = found
    assert header == ["Checkpoint", "General", "TSA PreCheck"]
    assert rows == [["Checkpoint A", "4 mins", "Not Available"], ["Checkpoint G", "0 mins", "1 mins"]]
    assert [_lane(h) for h in header[1:]] == ["standard", "precheck"]


def test_sfo_checkpoint_table_missing() -> None:
    parser = _SfoPageParser()
    parser.feed("<table><tr><th>Airline</th><th>Terminal</th></tr><tr><td>United</td><td>3</td></tr></table>")
    parser.close()
    assert _sfo_checkpoint_table(parser.tables) is None
