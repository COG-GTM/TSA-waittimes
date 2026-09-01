from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.enplanements import load_enplanements, parse_enplanement_rows
from app.tsa_throughput import parse_throughput_html

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_tsa_2019_fixture() -> None:
    html = (FIXTURES / "tsa_passenger_volumes_2019.html").read_text(encoding="utf-8")
    rows = parse_throughput_html(html)
    assert len(rows) == 365
    assert (date(2019, 1, 1), 2201765) in rows
    assert (date(2019, 12, 31), 2073706) in rows


def test_parse_tsa_drops_out_of_range_rows() -> None:
    html = """
    <div class="text-align-center">12/31/2018</div><div class="text-align-center">1,000</div>
    <div class="text-align-center">1/1/2019</div><div class="text-align-center">0</div>
    <div class="text-align-center">1/2/2019</div><div class="text-align-center">10,000,001</div>
    <div class="text-align-center">1/3/2019</div><div class="text-align-center">1,234</div>
    """
    assert parse_throughput_html(html) == [(date(2019, 1, 3), 1234)]


def test_parse_enplanement_fixture() -> None:
    with (FIXTURES / "enplanement_rows.json").open(encoding="utf-8") as file:
        year, records = parse_enplanement_rows(json.load(file))
    expected = {
        "ATL": (1, 52511402),
        "DFW": (2, 42351316),
        "DEN": (3, 40012895),
        "ORD": (4, 38575693),
        "JFK": (6, 31466102),
        "DCA": (26, 12750892),
        "PDX": (33, 8639088),
    }
    assert year == 2024
    parsed = {record["locid"]: (record["rank"], record["enplanements"]) for record in records}
    assert len(records) == 513
    assert {locid: parsed[locid] for locid in expected} == expected


def test_parse_enplanements_requires_columns() -> None:
    with pytest.raises(ValueError, match="missing required column: locid"):
        parse_enplanement_rows([["Rank", "Hub", "CY 24 Enplanements"], [1, "L", 1]])


def test_committed_enplanements_data() -> None:
    data = load_enplanements()
    assert data is not None
    assert data["year"] == 2024
    airports = data["airports"]
    assert isinstance(airports, list)
    assert len(airports) == 513
    assert len({airport["locid"] for airport in airports}) == len(airports)
    assert len({airport["rank"] for airport in airports}) == len(airports)
    atl = next(airport for airport in airports if airport["locid"] == "ATL")
    assert atl["rank"] == 1
    assert atl["enplanements"] == 52511402
