# Operations Runbook — US Checkpoint Wait Picture

## Checking ingestion health

- `GET /healthz` returns per-source status:
  - `healthy: true` — last successful poll within 30 minutes.
  - `consecutive_failures`, `last_error`, `last_error_at` for diagnosis.
  - Overall `status` is `ok` only when every source is healthy.
- The UI flags any source stale after 30+ minutes (amber markers/labels and a
  `STALE` badge on drill-down pages).
- Fly health checks hit `/healthz` every 30s.

## Operations dashboard

- Open `/ops` for the noindex operations dashboard, or fetch `/api/ops` for
  the same snapshot as JSON. It requires no authentication and is not linked
  from the public navigation.
- Source pills are **green** when the last success is less than twice the
  configured refresh interval old, **amber** when it is older but under 30
  minutes, and **red** when it is at least 30 minutes old, has no success, or
  has consecutive failures.
- A failed dashboard query degrades that field to a dash rather than failing
  the page. Last cleanup is not persisted in the database; the poller only
  logs successful cleanup events.

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
At startup, a separate one-time task sequentially backfills missing national
throughput years from 2019 through the last completed year. It waits briefly
for the app to come up, pauses between years, and logs failures without
affecting the web process.

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

## Annual FAA enplanements refresh

When FAA publishes the next final calendar-year commercial-service workbook:

1. Update the source constants and filename in `scripts/fetch_enplanements.py`
   to the new FAA page/file URLs.
2. Run `python3.12 scripts/fetch_enplanements.py` (or pass `--file` with the
   downloaded workbook).
3. Confirm the script's year, airport count, and top-five summary.
4. Commit the regenerated `data/enplanements.json` and run the full test gates.

The JSON intentionally stores only LOCIDs, rank, enplanements, hub, and source
metadata; airport names and cities remain sourced from `data/us_airports.json`.
During seeding, the confirmed FAA LOCID-to-IATA aliases are applied only to
the database key, and rows dropped from a regenerated file are removed for
that year.

## Data provenance

Every observation stores the source, the source's publish timestamp when
available, fetch time, and a reference to the raw payload (`raw_payloads`).

## Historical rollups and data retention

The `observations_hourly` table stores one row for every checkpoint and UTC
hour with observations. Its natural key is `(checkpoint_id, hour_bucket)`;
`airport_iata` and `lane_type` are denormalized for query speed. The rollup
loop runs every 15 minutes by default and re-rolls the current UTC hour plus
the previous two hours. A startup backfill waits briefly for the app to come
up, then rolls missing or incomplete hours oldest-first in small batches.

Retention windows are measured back from the cleanup run's current timestamp:

| Environment variable | Default | Cutoff measured from |
|---|---:|---|
| `RETENTION_RAW_PAYLOAD_DAYS` | 14 days | `raw_payloads.fetched_at` |
| `RETENTION_OBSERVATION_DAYS` | 90 days | `observations.fetched_at` |
| `RETENTION_FAA_EVENT_DAYS` | 180 days | `faa_airport_events.fetched_at` |
| `RETENTION_WEATHER_ALERT_DAYS` | 180 days | `coalesce(expires, ends, fetched_at)` |

Cleanup runs every 6 hours by default (`CLEANUP_INTERVAL_SECONDS`). It never
deletes an observation unless its checkpoint/hour has an
`observations_hourly` row. Expired observations, FAA events, and weather
alerts are deleted in batches before expired raw payloads. Because raw payloads
are referenced by foreign keys, cleanup first sets matching `raw_id` values to
NULL in all three tables, then deletes the payload rows.

Each successful cleanup emits exactly one JSON log line, for example:

```text
{"event": "retention_cleanup", "started_at": "2026-09-01T12:00:00+00:00", "duration_ms": 842, "cutoffs": {"observations": "2026-06-03T12:00:00+00:00", "raw_payloads": "2026-08-18T12:00:00+00:00", "faa_airport_events": "2026-03-05T12:00:00+00:00", "weather_alerts": "2026-03-05T12:00:00+00:00"}, "deleted": {"observations": 1200, "raw_payloads": 900, "faa_airport_events": 3, "weather_alerts": 8}, "raw_payload_refs_cleared": 1200}
```

On Fly, use `fly secrets set` to tune the retention windows, rollup cadence,
lookback, or batch size via the `RETENTION_*`, `ROLLUP_*`,
`CLEANUP_INTERVAL_SECONDS`, and `CLEANUP_BATCH_LIMIT` environment variables.
Each table is deleted in batches of `CLEANUP_BATCH_LIMIT` (default 50000) with
at most 20 passes per table per run, so a very large first cleanup drains over
successive runs rather than in one long transaction.
Grep logs with `fly logs | grep '"event": "retention_cleanup"'`.
