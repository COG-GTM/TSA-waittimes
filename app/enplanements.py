"""FAA commercial-service airport enplanements data loading and parsing."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Sequence
from typing import cast

ENPLANEMENTS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "enplanements.json")
LOCID_RE = re.compile(r"^[A-Z0-9]{3}$")
_ENPLANEMENTS_RE = re.compile(r"^cy (\d{2,4}) enplanements$")
LOCID_TO_IATA = {
    "AWI": "AIN", "IWA": "AZA", "BBG": "BKG", "BVU": "BLD", "CRQ": "CLD",
    "ENM": "EMK", "ORS": "ESD", "GPI": "FCA", "HXD": "HHH", "HLA": "HSL",
    "AKW": "KLW", "SAW": "MQT", "UNV": "SCE", "JQF": "USA", "NYL": "YUM",
}


def iata_for_locid(locid: str) -> str:
    return LOCID_TO_IATA.get(locid, locid)


def _normalize_header(value: object) -> str:
    return " ".join(str(value).strip().split()).casefold()


def parse_enplanement_rows(
    rows: Iterable[Sequence[object]], *, min_records: int = 300
) -> tuple[int, list[dict[str, object]]]:
    """Parse FAA rows into validated records, allowing small test fixtures."""
    iterator = iter(rows)
    try:
        header = next(iterator)
    except StopIteration as exc:
        raise ValueError("enplanements worksheet is empty") from exc
    headers = [_normalize_header(value) for value in header]
    indexes: dict[str, int] = {}
    for required in ("rank", "locid", "hub"):
        try:
            indexes[required] = headers.index(required)
        except ValueError as exc:
            raise ValueError(f"enplanements worksheet missing required column: {required}") from exc
    matches = [
        (int(match.group(1)) if len(match.group(1)) == 4 else 2000 + int(match.group(1)), index)
        for index, header_value in enumerate(headers)
        if (match := _ENPLANEMENTS_RE.fullmatch(header_value))
    ]
    if not matches:
        raise ValueError("enplanements worksheet missing CY enplanements column")
    year, enplanements_index = max(matches)

    records: list[dict[str, object]] = []
    for row in iterator:
        if len(row) <= max((*indexes.values(), enplanements_index)):
            continue
        rank = row[indexes["rank"]]
        locid = row[indexes["locid"]]
        enplanements = row[enplanements_index]
        if isinstance(rank, bool) or not isinstance(rank, int):
            continue
        if not isinstance(locid, str):
            continue
        airport_iata = locid.strip().upper()
        if not LOCID_RE.fullmatch(airport_iata):
            continue
        if isinstance(enplanements, bool) or not isinstance(enplanements, (int, float)) or enplanements < 0:
            continue
        hub = row[indexes["hub"]]
        hub_value = hub.strip() if isinstance(hub, str) else None
        if hub_value in ("", "None"):
            hub_value = None
        records.append({
            "locid": airport_iata,
            "rank": rank,
            "enplanements": int(enplanements),
            "hub": hub_value,
        })
    if len(records) < min_records:
        raise ValueError(
            f"enplanements worksheet yielded only {len(records)} records; expected at least {min_records}"
        )
    if len({record["locid"] for record in records}) != len(records):
        raise ValueError("enplanements worksheet contains duplicate locids")
    if len({record["rank"] for record in records}) != len(records):
        raise ValueError("enplanements worksheet contains duplicate ranks")
    rank_one = next(record for record in records if record["rank"] == 1)
    if cast(int, rank_one["enplanements"]) != max(cast(int, record["enplanements"]) for record in records):
        raise ValueError("enplanements worksheet rank 1 is not the maximum")
    return year, records


def load_enplanements() -> dict[str, object] | None:
    """Load the committed FAA enplanements JSON, or None if it is absent."""
    try:
        with open(ENPLANEMENTS_PATH, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return None
