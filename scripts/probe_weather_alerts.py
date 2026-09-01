#!/usr/bin/env python3
"""Run one live NWS alert cycle against every airport and record a replay fixture.

    python scripts/probe_weather_alerts.py            # fetch, print, write the fixture
    python scripts/probe_weather_alerts.py --no-save  # print only

Zones come from live ``/points`` lookups (one per airport, rate limited); the
fixture keeps those payloads plus every alert feature that matched an airport
and a sample of the ones the relevance filter dropped, so ``pytest`` can replay
the whole match offline.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import weather_alerts as wx
from app.sources.base import USER_AGENT

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "tests" / "weather_fixtures" / "nws_alerts.json"
AIRPORTS_PATH = Path(__file__).resolve().parent.parent / "data" / "us_airports.json"
DROPPED_SAMPLE = 5
# The fixture keeps every affected airport plus a sample of unaffected ones, so it
# stays reviewable instead of embedding ~500 /points payloads.
UNAFFECTED_SAMPLE = 15


async def collect(limit: int | None) -> dict[str, Any]:
    with AIRPORTS_PATH.open(encoding="utf-8") as fh:
        airports = json.load(fh)
    if limit:
        airports = airports[:limit]
    coords = {a["iata"]: (a["lat"], a["lon"]) for a in airports}
    points: dict[str, Any] = {}
    zones: dict[str, wx.AirportZones] = {}
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as client:
        for a in airports:
            url = wx.POINTS_URL.format(lat=a["lat"], lon=a["lon"])
            try:
                resp = await client.get(url, headers={"Accept": "application/geo+json"})
                resp.raise_for_status()
                payload = resp.json()
            except Exception as err:  # noqa: BLE001 - report and keep walking
                print(f"  ! {a['iata']} points lookup failed: {err}")
                continue
            parsed = wx.parse_zones(a["iata"], payload)
            if parsed is None:
                print(f"  ! {a['iata']} returned no usable zone")
                continue
            points[a["iata"]] = payload
            zones[a["iata"]] = parsed
            await asyncio.sleep(wx.POINTS_DELAY_SECONDS)
        alerts_payload = await wx.fetch_alerts(client)
    return {"coords": coords, "points": points, "zones": zones, "alerts_payload": alerts_payload}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-save", action="store_true", help="print only; do not write the fixture")
    parser.add_argument("--limit", type=int, default=None, help="only probe the first N airports")
    args = parser.parse_args()

    print(f"{wx.SOURCE_CODE}: {wx.SOURCE_NAME}\n  public page: {wx.PUBLIC_PAGE}\n  attribution: {wx.ATTRIBUTION}")
    captured_at = datetime.now(tz=UTC)
    data = asyncio.run(collect(args.limit))
    payload = data["alerts_payload"]
    features = payload.get("features", [])
    alerts = wx.parse_alerts(payload)
    matched = wx.match_alerts(alerts, data["zones"], data["coords"], now=captured_at)

    print(f"  {len(data['zones'])} airports with NWS zones; {len(features)} active alerts nationally, "
          f"{len(alerts)} aviation-relevant, {len(matched)} airports affected")
    for iata, entries in sorted(matched.items()):
        for alert, basis in entries:
            print(f"  - {iata} [{basis}] {alert.event} ({alert.severity}) until {alert.expires}: {alert.headline}")

    if args.no_save:
        return 0

    keep_airports = set(matched) | set(sorted(data["points"])[:UNAFFECTED_SAMPLE])
    points = {iata: p for iata, p in data["points"].items() if iata in keep_airports}

    kept_ids = {a.alert_id for entries in matched.values() for a, _ in entries}
    kept: list[Any] = []
    dropped: list[Any] = []
    for feature in features:
        props = feature.get("properties", {})
        if props.get("id") in kept_ids:
            kept.append(feature)
        elif len(dropped) < DROPPED_SAMPLE and props.get("event") not in wx.RELEVANT_EVENTS:
            dropped.append(feature)
    fixture = {
        "version": 1,
        "source": wx.SOURCE_CODE,
        "captured_at": captured_at.isoformat(),
        "public_page": wx.PUBLIC_PAGE,
        "attribution": wx.ATTRIBUTION,
        "alerts_url": wx.ALERTS_URL,
        "national_feature_count": len(features),
        "points": points,
        "alerts": {"type": "FeatureCollection", "features": kept + dropped},
        "expected": {
            "relevant_events": sorted({a.event for a in alerts if a.alert_id in kept_ids}),
            "dropped_events": sorted({f["properties"].get("event") for f in dropped}),
            "matched": {
                iata: [
                    {"event": a.event, "severity": a.severity, "basis": basis, "alert_id": a.alert_id}
                    for a, basis in entries
                ]
                for iata, entries in sorted(matched.items())
            },
        },
    }
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FIXTURE_PATH.open("w", encoding="utf-8") as fh:
        json.dump(fixture, fh, indent=2, sort_keys=False)
        fh.write("\n")
    print(f"  wrote {FIXTURE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
