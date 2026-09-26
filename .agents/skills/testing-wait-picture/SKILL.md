---
name: testing-wait-picture
description: How to run and end-to-end test the US Checkpoint Wait Picture app (TSA-waittimes) locally, including verifying new wait-time source adapters.
---

# Testing the US Checkpoint Wait Picture app locally

## Bring the app up

```bash
docker run -d --name waits-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=waits -p 5432:5432 postgres:16-alpine
# system python3 may be 3.9 (too old for `X | None` syntax used in app/main.py) — use python3.12
python3.12 -m pip install -r requirements.txt
python3.12 -m uvicorn app.main:app --port 8080
```

- No logins are required: every upstream feed is one the airport's own public website loads in an ordinary visitor's browser. The vendor API keys those pages send (DEN, MCO, IAH/HOU, DFW, CLT, CVG, LAS, BOS, PIT, PHX, MIA) must be exported as `FEED_KEY_<CODE>` environment variables before starting uvicorn (see `.env.example`); an unset key makes only that source report `FEED_KEY_<CODE> is not configured` in `/healthz`. Outbound internet access to the airport/vendor APIs is required.
- Poll loops start on app startup (`app/poller.py`), one task per source at its `refresh_seconds` (usually 120s). Allow ~30-60s after startup before checking health/UI, otherwise sources look unhealthy simply because they have not polled yet.
- Data comes from live public feeds, so exact minute values change between polls. Never assert on fixed numbers; assert on presence, lane names, open/closed state, and rough magnitude.

## Where to look

- `GET /healthz` — JSON with one entry per source: `healthy`, `consecutive_failures`, `last_error`. Top-level `status` is `ok` only when every source is healthy. Always HTTP 200, so read the body, not the code.
- `GET /api/summary` — per-airport max standard/precheck waits; homepage map + cards consume this.
- `GET /api/airport/<IATA>` and page `/airport/<IATA>` — per-checkpoint drill-down.
- Airport must exist in `data/us_airports.json` (lat/lon) or it will never appear on the map.

## Adversarial checks worth repeating for any new source adapter

1. Closed lanes must render the word **Closed** (`app/static/airport.js` uses `!cp.is_open`), never "0 min". Confirm by dumping the adapter's raw payload and checking the upstream `open`/`isOpen`/`status` field agrees.
2. Data fidelity: poll the upstream feed and `/api/airport/<IATA>` within the same poll window (watch `fetched_at` change, then immediately fetch upstream) and compare values. Feeds reporting minutes are stored as seconds (`minutes * 60`); feeds reporting seconds are stored as-is.
3. Regression when a shared helper is refactored (e.g. `_zensors` covering LAS and BOS): check every airport routed through the helper still lists all its checkpoints.

## Browser gotcha

Typing `localhost:8080/` in Chrome's omnibox autocompletes to a previously visited path (e.g. `/healthz`) and Enter navigates there instead. Press `Delete` after typing the URL to drop the inline autocomplete, then Enter.

## Devin Secrets Needed

None.
