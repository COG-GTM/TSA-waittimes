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

Verified live sources: SEA, DEN, MCO, IAH, HOU, DFW, CLT, CVG, SLC, LAS.
TSA daily throughput is fetched from tsa.gov/travel/passenger-volumes every 6h.

## Common issues

| Symptom | Likely cause | Action |
|---|---|---|
| Source unhealthy, HTTP 401/403 | Airport rotated its public API key | Re-inspect the airport site's own requests; update the key in `adapters.py` |
| Source unhealthy, parse errors | Feed shape changed | Check `raw_payloads` for that source; adjust the adapter |
| All sources unhealthy | DB or egress problem | Check `fly logs`; verify `DATABASE_URL` |
| TSA strip missing | tsa.gov blocking datacenter IPs | Non-fatal; retried every 30 min |

## Data provenance

Every observation stores the source, the source's publish timestamp when
available, fetch time, and a reference to the raw payload (`raw_payloads`).
