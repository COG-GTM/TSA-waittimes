---
name: testing-waittimes
description: How to run and end-to-end test the TSA-waittimes FastAPI dashboard locally (Postgres, poller, UI paths, live-source verification).
---

# Testing the TSA wait-times dashboard locally

## Bring the app up
```bash
docker run -d --name waits-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=waits -p 5432:5432 postgres:16-alpine
pip install -r requirements.txt
uvicorn app.main:app --port 8080
```
Reuse an existing `waits-pg` container / uvicorn process if already running (`docker ps`, `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/`).

Sources in `app/sources/adapters.py` are polled every 120s by `app/poller.py`, so after startup wait one cycle before expecting live data. Live sources require outbound internet to the airports' own sites; if a source's site is blocked or changes its feed, that airport simply won't appear in the live list.

## Verify data before opening the browser
- `GET /api/summary` → `live_count` and per-airport `live` flags.
- `GET /api/airport/{IATA}` → `checkpoints[]` with `name`, `lane_type` (`standard` / `precheck` / `other`), `wait_seconds`, `is_open`, `source`, `source_url`.
Using the API first tells you which UI states (e.g. a closed lane) are currently reproducible, so you don't plan a test the live feed can't show.

## UI paths
- `/` — national map (`app/static/map.js`) plus live-airport list (`app/static/app.js`, `#live-list`); live airports are green markers with `IATA N min` labels.
- `/airport/{IATA}` — checkpoint cards (`app/static/airport.js`). A card shows the checkpoint name, lane badge, wait, and `Source: <link>`.
- Closed lanes: `airport.js` renders the literal text `Closed` when `is_open` is false; a 0-minute wait is a distinct, valid state. Verifying "Closed" vs "0 min" is the sharpest check for adapters that map open/closed flags.

## Gotchas
- Newly ingested checkpoints show "History accrues from first ingest — check back shortly." instead of a sparkline; that is expected, not a bug.
- Checkpoint names can contain an em dash (e.g. `Terminal B — Gates 40-49`) from the upstream feed; confirm it renders as text, not escaped markup.

## Devin Secrets Needed
None for local testing.
