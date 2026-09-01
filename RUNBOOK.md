# Operations Runbook — US Checkpoint Wait Picture

## Checking ingestion health

- `GET /healthz` returns per-source status:
  - `healthy: true` — last successful poll within 30 minutes.
  - `consecutive_failures`, `last_error`, `last_error_at` for diagnosis.
  - Overall `status` is `ok` only when every source is healthy.
- The UI flags any source stale after 30+ minutes (amber markers/labels and a
  `STALE` badge on drill-down pages).
- Fly health checks hit `/healthz` every 30s.

## Restarting the scrapers

The pollers run inside the web process (started on app startup).

- **Fly.io:** `fly apps restart checkpoint-wait-picture` (or
  `fly machine restart <id>`). Pollers resume automatically; history is in
  Postgres and survives restarts.
- **Local:** restart uvicorn.

Poll loops never die on errors — they back off exponentially (up to 15 min) and
keep retrying, so a transient source outage needs no action.

## Source list and refresh rates

The single source of truth is `SOURCES` in `app/sources/adapters.py`:
each entry holds the airport code, source name, public URL, attribution string,
and `refresh_seconds` (default 120s — no source is polled faster than once per
minute). The `sources` table is upserted from this list at startup.

Verified live sources: SEA, DEN, MCO, IAH, HOU, DFW, CLT, CVG, SLC, LAS, BOS, PIT, JFK, LGA, EWR, PHX, DTW, MIA, DCA, ORD, PDX.
TSA daily throughput is fetched from tsa.gov/travel/passenger-volumes every 6h.

## Weather alerts (NWS-ALERTS)

- One national `https://api.weather.gov/alerts/active` request every 600s; the
  request count does not scale with the airport list.
- Airport → NWS zone mapping lives in `airport_nws_zones`, backfilled 150
  airports per cycle via `/points/{lat},{lon}` and refreshed every 90
  days. While any airport still lacks a cached zone the poll publishes the
  alerts it can match but reports unhealthy (`NWS zones cached for N/M
  airports`) — partial coverage silently under-reports, so it must not look
  green. Backfill cycles keep the normal 600s cadence instead of backing off.
- `weather_alerts` is rewritten every cycle, so an alert that ends simply
  disappears; provenance for each cycle is in `raw_payloads` under `NWS-ALERTS`.
- Only aviation-relevant event types are stored (`RELEVANT_EVENTS` in
  `app/weather_alerts.py`); marine, hydrologic-advisory, heat, air-quality,
  fire-weather and non-weather civil messages are dropped.
- Refresh the offline fixture after a feed change with
  `python scripts/probe_weather_alerts.py`, then run `pytest`.

## Common issues

| Symptom | Likely cause | Action |
|---|---|---|
| Source unhealthy, HTTP 401/403 | Airport rotated its public API key | Re-inspect the airport site's own requests; update the key in `adapters.py` |
| Source unhealthy, parse errors | Feed shape changed | Check `raw_payloads` for that source; adjust the adapter |
| All sources unhealthy | DB or egress problem | Check `fly logs`; verify `DATABASE_URL` |
| Suspected feed change | Feed shape changed | `python scripts/probe_source.py <CODE>` — prints a live fetch and refreshes `tests/fixtures/<code>.json`; then run `pytest` |
| TSA strip missing | tsa.gov blocking datacenter IPs | Non-fatal; retried every 30 min |

## Data provenance

Every observation stores the source, the source's publish timestamp when
available, fetch time, and a reference to the raw payload (`raw_payloads`).
