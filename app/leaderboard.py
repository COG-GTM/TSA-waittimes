"""Pure computation helpers for the national waits leaderboard."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

FRESH_SECONDS = 30 * 60
TOP_N = 10
MIN_ENTRIES = 3
BASELINE_AGE = timedelta(hours=3)
BASELINE_TOLERANCE = timedelta(minutes=30)
MIN_DROP_SECONDS = 60

LatestRow = Sequence[Any]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _entry(row: LatestRow) -> dict[str, Any]:
    (
        airport_iata,
        checkpoint_name,
        _lane_type,
        wait_seconds,
        _is_open,
        source_published_at,
        fetched_at,
        attribution,
        source_url,
    ) = row
    return {
        "iata": airport_iata,
        "name": "",
        "checkpoint": checkpoint_name,
        "wait_seconds": wait_seconds,
        "as_of": _iso(source_published_at or fetched_at),
        "fetched_at": _iso(fetched_at),
        "source": attribution,
        "source_url": source_url,
    }


def _rank(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: (-entry["wait_seconds"], entry["iata"]))[:TOP_N]


def build(
    latest_rows: Sequence[LatestRow],
    baseline_rows: Sequence[Sequence[Any]],
    airport_names: dict[str, str],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Build ranked national wait lists from latest and baseline observations."""
    latest_by_lane: dict[tuple[str, str], dict[str, Any]] = {}
    for row in latest_rows:
        airport_iata = row[0]
        lane_type = row[2]
        wait_seconds = row[3]
        fetched_at = row[6]
        if (
            airport_iata not in airport_names
            or lane_type not in {"standard", "precheck"}
            or wait_seconds is None
            or not row[4]
            or (now - fetched_at).total_seconds() > FRESH_SECONDS
        ):
            continue
        key = (airport_iata, lane_type)
        existing = latest_by_lane.get(key)
        if existing is None or wait_seconds > existing["wait_seconds"]:
            entry = _entry(row)
            entry["name"] = airport_names[airport_iata]
            latest_by_lane[key] = entry

    all_standard_entries = [entry for (iata, lane), entry in latest_by_lane.items() if lane == "standard"]
    standard_entries = _rank(all_standard_entries)
    precheck_entries = _rank(
        [entry for (iata, lane), entry in latest_by_lane.items() if lane == "precheck"]
    )

    baseline = {row[0]: row[1] for row in baseline_rows}
    improvements: list[dict[str, Any]] = []
    for entry in all_standard_entries:
        previous_wait = baseline.get(entry["iata"])
        if previous_wait is None:
            continue
        drop_seconds = previous_wait - entry["wait_seconds"]
        if drop_seconds < MIN_DROP_SECONDS:
            continue
        improved = dict(entry)
        improved["previous_wait_seconds"] = previous_wait
        improved["drop_seconds"] = drop_seconds
        improvements.append(improved)
    improvements.sort(key=lambda entry: (-entry["drop_seconds"], entry["iata"]))

    def section(entries: list[dict[str, Any]]) -> dict[str, Any]:
        return {"entries": entries[:TOP_N], "quiet": len(entries) < MIN_ENTRIES}

    return {
        "generated_at": _iso(now),
        "fresh_within_seconds": FRESH_SECONDS,
        "improvement_window_seconds": int(BASELINE_AGE.total_seconds()),
        "worst_standard": section(standard_entries),
        "worst_precheck": section(precheck_entries),
        "most_improved": section(improvements),
    }
