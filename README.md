# US Checkpoint Wait Picture

A publicly reachable, mobile-friendly dashboard showing live US airport security
checkpoint wait times aggregated from officially published public sources — with
per-airport drill-down, accumulating history, transparent source attribution, and
a national coverage counter.

Built from publicly published airport data only. Not affiliated with TSA.
Demonstration only.

## How it works

- **Ingestion** (`app/poller.py`): one async polling loop per verified source,
  polling at each source's natural refresh rate (never faster than once per
  minute), with exponential backoff on failure and per-source health tracking.
- **Sources** (`app/sources/adapters.py`): polite API clients for the public
  wait-time feeds each airport's own website loads. Honest user agent, public
  endpoints only, no logins or credentials.
- **Storage**: PostgreSQL — airports, checkpoints, observations (wait value,
  lane type, source URL, source publish timestamp, fetch timestamp), raw
  payloads for provenance, poll health, and TSA daily throughput.
- **Web** (`app/main.py`): FastAPI serving the national map, airport drill-down
  pages, and a JSON API. Frontend is vanilla JS + vendored Leaflet with a
  vendored US-states GeoJSON basemap (no external tile servers).

## Travel calendar

`data/travel_calendar.json` contains the static US travel-demand calendar. It is
generated data and must never be hand-edited; regenerate it with
`python scripts/gen_travel_calendar.py`. Regenerate it when the year rolls over;
the default covers the current year and next two years, plus any prior-year
period still active at the start of the current year. At startup, the calendar is seeded into
the `travel_periods` table with a full replace. The `/api/summary` and
`/api/airport/{iata}` endpoints expose the selected period as `travel_period`.

## Live sources

See `SOURCES` in `app/sources/adapters.py` (single source of truth). Each entry
records the airport, the public page where the data is published, attribution,
and refresh rate.
Verified live sources: SEA, DEN, MCO, IAH, HOU, DFW, CLT, CVG, SLC, LAS, BOS,
PIT, JFK, LGA, EWR, PHX, DTW, MIA, DCA, ORD, PDX.

## Run locally

```bash
docker run -d --name waits-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=waits -p 5432:5432 postgres:16-alpine
pip install -r requirements.txt
uvicorn app.main:app --port 8080
```

`DATABASE_URL` overrides the default local connection string.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
ruff check app/ tests/
```

Tests never touch the network. `tests/test_fixture_replay.py` replays one
recorded payload per adapter (`tests/fixtures/<code>.json`) through the real
adapter over an `httpx.MockTransport` and asserts the parsed observations:
checkpoint count, lane classification, wait values in **seconds**, open/closed
flags, and timezone-aware timestamps. `tests/test_helpers.py` and
`tests/test_adapter_flags.py` cover the pure helpers and the close/hide flag
handling on synthetic payloads.

Adding an adapter to the harness is just recording its fixture — the replay
tests discover every file in `tests/fixtures/` automatically, and
`test_every_adapter_has_a_fixture` fails if a source in `SOURCES` has none.

## Probing a live source

```bash
python scripts/probe_source.py SEA            # fetch live, print observations, write tests/fixtures/sea.json
python scripts/probe_source.py SEA --no-save  # fetch and print only
```

`probe_source.py` runs a single live fetch for one source code, prints the
parsed observations (wait in seconds and minutes, lane type, open/closed,
publish timestamp), and records a fixture containing every HTTP exchange the
adapter made plus the observations it parsed. Re-run it after a feed changes
shape to refresh the fixture.

## Continuous deployment

The application deploys to Fly.io through the `.github/workflows/fly-deploy.yml`
workflow only after the `CI` workflow succeeds on `main`. `workflow_dispatch`
remains available for manual deploys. A CI run whose commit is no longer the
tip of `main` is skipped, so a late-finishing run cannot roll production back.
The repository must define a `FLYIO_TSA_TOKEN` secret with permission to deploy
the Fly.io application.
The canonical public hostname is `waitpicture.com` (also available at
`www.waitpicture.com`). The legacy `tsadelays.com` and `www.tsadelays.com`
hostnames return a 301 redirect to the canonical domain, handled in the app.

Pull requests and pushes to `main` run the `ci.yml` lint, type-check, and test gates.
Run them locally with `pip install -r requirements-dev.txt`, `ruff check .`, `mypy`, and `pytest`.

## Operations

See [RUNBOOK.md](RUNBOOK.md).
