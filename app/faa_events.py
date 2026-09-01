"""FAA National Airspace System airport event feed."""
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

FAA_EVENTS_URL = "https://nasstatus.faa.gov/api/airport-events"
FAA_PUBLIC_URL = "https://nasstatus.faa.gov/"
FAA_ATTRIBUTION = "FAA National Airspace System Status (nasstatus.faa.gov)"
FAA_SOURCE_CODE = "FAA-NAS"
REFRESH_SECONDS = 300

_DURATION_RE = re.compile(
    r"^\s*(?:(?P<hours>\d+(?:\.\d+)?)\s*hours?)?"
    r"(?:\s*(?:and\s*)?(?P<minutes>\d+(?:\.\d+)?)\s*minutes?)?\s*$",
    re.IGNORECASE,
)
_DAILY_CLOSURE_RE = re.compile(r"\bDLY\s+(\d{4})-(\d{4})\b", re.IGNORECASE)
_DLY_MARKER_RE = re.compile(r"\bDLY\b", re.IGNORECASE)
_CLOSURE_RE = re.compile(r"\bCLSD\b", re.IGNORECASE)


@dataclass
class FaaEvent:
    airport_iata: str
    event_type: str
    reason: str | None
    avg_delay_seconds: int | None
    start_time: datetime | None
    end_time: datetime | None
    update_time: datetime | None


def _parse_dt(s: object) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        parsed = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_duration_seconds(text: object) -> int | None:
    if not isinstance(text, str):
        return None
    match = _DURATION_RE.fullmatch(text)
    if match is None or not match.group("hours") and not match.group("minutes"):
        return None
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    return round(hours * 3600 + minutes * 60)


def _optional_text(entry: dict[str, Any], key: str) -> tuple[bool, str | None]:
    if key not in entry or entry[key] is None:
        return True, None
    value = entry[key]
    return isinstance(value, str), value if isinstance(value, str) else None


def _optional_dt(entry: dict[str, Any], key: str) -> tuple[bool, datetime | None]:
    valid, value = _optional_text(entry, key)
    if not valid:
        return False, None
    return value is None or _parse_dt(value) is not None, _parse_dt(value)


def _event_times(
    entry: dict[str, Any], update_keys: tuple[str, ...] = ("updatedAt",)
) -> tuple[bool, datetime | None, datetime | None, datetime | None]:
    start_valid, start_time = _optional_dt(entry, "startTime")
    end_valid, end_time = _optional_dt(entry, "endTime")
    update_time: datetime | None = None
    update_valid = True
    for key in update_keys:
        valid, value = _optional_dt(entry, key)
        update_valid = update_valid and valid
        if update_time is None and value is not None:
            update_time = value
    return start_valid and end_valid and update_valid, start_time, end_time, update_time


def _hhmm_minutes(value: str) -> int | None:
    hour, minute = int(value[:2]), int(value[2:])
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _daily_closure_active(reason: str, now: datetime) -> bool | None:
    if _DLY_MARKER_RE.search(reason) is None:
        return None
    match = _DAILY_CLOSURE_RE.search(reason)
    if match is None:
        return False
    start = _hhmm_minutes(match.group(1))
    end = _hhmm_minutes(match.group(2))
    if start is None or end is None:
        return False
    current = now.astimezone(UTC).hour * 60 + now.astimezone(UTC).minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


def parse_airport_events(
    payload: Any, *, now: datetime | None = None
) -> list[FaaEvent]:
    if not isinstance(payload, list):
        raise ValueError("FAA airport events payload must be a list")  # noqa: TRY004

    now = now or datetime.now(UTC)
    events: list[FaaEvent] = []
    for entry in payload:
        if not isinstance(entry, dict) or not isinstance(entry.get("airportId"), str):
            continue
        airport_iata = entry["airportId"].upper()
        if not airport_iata:
            continue

        ground_stop = entry.get("groundStop")
        if isinstance(ground_stop, dict):
            reason_valid, reason = _optional_text(ground_stop, "impactingCondition")
            times_valid, start_time, end_time, update_time = _event_times(ground_stop)
            if reason_valid and times_valid:
                events.append(
                    FaaEvent(
                        airport_iata,
                        "ground_stop",
                        reason,
                        None,
                        start_time,
                        end_time,
                        update_time,
                    )
                )

        ground_delay = entry.get("groundDelay")
        if isinstance(ground_delay, dict):
            reason_valid, reason = _optional_text(ground_delay, "impactingCondition")
            avg_delay = ground_delay.get("avgDelay")
            avg_valid = avg_delay is None or (
                isinstance(avg_delay, (int, float)) and not isinstance(avg_delay, bool)
            )
            times_valid, start_time, end_time, update_time = _event_times(
                ground_delay, ("updatedAt", "sourceTimeStamp")
            )
            if reason_valid and avg_valid and times_valid:
                events.append(
                    FaaEvent(
                        airport_iata,
                        "ground_delay",
                        reason,
                        round(avg_delay * 60) if avg_delay is not None else None,
                        start_time,
                        end_time,
                        update_time,
                    )
                )

        for key, event_type in (
            ("departureDelay", "departure_delay"),
            ("arrivalDelay", "arrival_delay"),
        ):
            delay = entry.get(key)
            if not isinstance(delay, dict):
                continue
            reason_valid, reason = _optional_text(delay, "reason")
            nested = delay.get("arrivalDeparture")
            nested_valid = nested is None or isinstance(nested, dict)
            max_text: object = None
            if isinstance(nested, dict):
                max_text = nested.get("max")
                max_valid = max_text is None or isinstance(max_text, str)
            else:
                max_valid = True
            average = delay.get("averageDelay")
            average_valid = average is None or isinstance(average, str)
            avg_delay_seconds: int | None = None
            if isinstance(average, str):
                try:
                    avg_delay_seconds = int(average) * 60
                except ValueError:
                    avg_delay_seconds = _parse_duration_seconds(max_text)
            elif average is None:
                avg_delay_seconds = _parse_duration_seconds(max_text)
            times_valid, start_time, end_time, update_time = _event_times(delay, ("updateTime",))
            if reason_valid and nested_valid and max_valid and average_valid and times_valid:
                events.append(
                    FaaEvent(
                        airport_iata,
                        event_type,
                        reason,
                        avg_delay_seconds,
                        start_time,
                        end_time,
                        update_time,
                    )
                )

        closure = entry.get("airportClosure")
        if isinstance(closure, dict):
            simple_valid, simple_text = _optional_text(closure, "simpleText")
            text_valid, text = _optional_text(closure, "text")
            times_valid, start_time, end_time, update_time = _event_times(closure)
            reason = (simple_text or text or "").replace("\r", "").replace("\n", "")
            daily_active = _daily_closure_active(reason, now)
            if simple_valid and text_valid and times_valid and daily_active is not False:
                events.append(
                    FaaEvent(
                        airport_iata,
                        "closure",
                        reason or None,
                        None,
                        start_time,
                        end_time,
                        update_time,
                    )
                )

        free_form = entry.get("freeForm")
        if isinstance(free_form, dict):
            simple_valid, simple_text = _optional_text(free_form, "simpleText")
            text_valid, text = _optional_text(free_form, "text")
            reason = (simple_text or text or "").replace("\r", "").replace("\n", "")
            times_valid, start_time, end_time, update_time = _event_times(free_form)
            daily_active = _daily_closure_active(reason, now)
            if (
                simple_valid and text_valid and times_valid
                and _CLOSURE_RE.search(reason) is not None
                and daily_active is not False
            ):
                events.append(
                    FaaEvent(
                        airport_iata,
                        "closure",
                        reason or None,
                        None,
                        start_time,
                        end_time,
                        update_time,
                    )
                )
    return events


async def fetch_faa_events(
    client: httpx.AsyncClient, *, now: datetime | None = None
) -> tuple[Any, list[FaaEvent]]:
    response = await client.get(FAA_EVENTS_URL)
    response.raise_for_status()
    payload = response.json()
    return payload, parse_airport_events(payload, now=now)
