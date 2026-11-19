# US Checkpoint Wait Picture

A publicly reachable, mobile-friendly dashboard showing live US airport security
checkpoint wait times aggregated from officially published public sources — with
per-airport drill-down, accumulating history, transparent source attribution, and
a national coverage counter.

Built from publicly published airport data only. Not affiliated with TSA.
Demonstration only.

## How it works

See [docs/architecture.md](docs/architecture.md) for the full architecture and
the [diagram](docs/diagrams/architecture.png) (source:
[`docs/diagrams/architecture.dot`](docs/diagrams/architecture.dot)).

![Architecture diagram](docs/diagrams/architecture.png)

One FastAPI process runs both the background pollers and the website, backed
by PostgreSQL:

- **Ingestion** (`app/poller.py`): started from the app lifespan. One async
  polling loop per verified source, polling at each source's natural refresh
  rate (never faster than once per minute), with exponential backoff on failure
  and per-source health tracking, plus loops for FAA events, NWS alerts, TSA
  throughput, hourly rollups and retention cleanup.
- **Sources** (`app/sources/adapters.py`): polite API clients for the public
  wait-time feeds each airport's own website loads. Honest user agent, public
  endpoints only, no logins or credentials.
- **FAA airport events** (`app/faa_events.py`): the FAA NAS Status
  airport-events feed every 5 minutes (ground stops, delay programs, closures),
  shown on airport pages and `/ops`.
- **Weather** (`app/weather_alerts.py`): one national `api.weather.gov`
  `/alerts/active` request every 10 minutes, matched to each airport by its NWS
  forecast/county/fire zone (resolved once per airport via `/points` and cached
  in `airport_nws_zones`) with a point-in-polygon fallback for alerts that carry
  geometry. Only aviation-relevant event types are surfaced; see
  `RELEVANT_EVENTS`. Attribution: National Weather Service (weather.gov).
- **Analytics** (`app/analytics.py`, `app/forecast.py`): hourly rollups into
  `observations_hourly`, typical-wait percentiles, short-horizon forecasts, and
  retention cleanup.
- **Storage** (`app/db.py`): PostgreSQL — airports, checkpoints, observations
  (wait value, lane type, source URL, source publish timestamp, fetch
  timestamp), hourly rollups, raw payloads for provenance, poll health, FAA
  events, weather alerts, TSA daily throughput, FAA airport enplanements, and
  travel periods.
- **Web** (`app/main.py`): FastAPI serving the national map with a national
  leaderboard (`app/leaderboard.py`), airport drill-down pages, the `/ops`
  dashboard (`app/ops.py`), and a JSON API. `/api/airport/{iata}/typical`
  provides historical standard-lane wait percentiles by UTC hour of week.
  Frontend is vanilla JS + vendored Leaflet with a vendored US-states GeoJSON
  basemap (no external tile servers).
- **Public API** (`app/public_api.py`): versioned, CORS-enabled, rate-limited
  JSON at `/api/v1` (documented at `/api`) and an embeddable `/embed/{iata}`
  widget.
- **Security** (`app/security.py`): security headers, input validation,
  client-IP attribution and structured audit logs for every route.

## Project layout

```text
app/
  main.py            FastAPI app, lifespan, pages, internal JSON API
  public_api.py      /api/v1 sub-app and /embed widget
  poller.py          background polling, rollup and cleanup tasks
  sources/           airport wait-time adapters (SOURCES) and shared types
  faa_events.py      FAA NAS airport events
  weather_alerts.py  NWS alerts and zone matching
  tsa_throughput.py  TSA national passenger volumes
  enplanements.py    FAA enplanements loader
  travel_calendar.py travel-demand calendar
  analytics.py       rollups, typical waits, retention
  forecast.py        short-horizon wait forecasts
  leaderboard.py     national leaderboard
  ops.py             /ops dashboard data
  queries.py         shared SQL and response shaping
  security.py        headers, validation, audit logging
  db.py              schema, migrations, seeding
  templates/         Jinja2 pages
  static/            JS, CSS, vendored Leaflet and US-states GeoJSON
data/                committed airports, enplanements and travel calendar
scripts/             probes and data generators
tests/               offline pytest suite with recorded fixtures
docs/                architecture doc and diagram
```

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

National TSA throughput history is backfilled from 2019 through the last
completed year at startup, one year at a time. A missing or blocked historical
page is logged and does not prevent the app from starting. FAA CY2024
commercial-service enplanements are committed in `data/enplanements.json` and
seeded into the `airport_enplanements` table. Regenerate the static file with:

```bash
python3.12 scripts/fetch_enplanements.py
```

The generator also accepts `--file <path>` for a downloaded FAA workbook.
FAA LOCIDs are normalized to the app's IATA codes for the small set of
confirmed aliases during seeding; the source JSON remains faithful to FAA
LOCIDs. Re-seeding also removes stale rows for that year that are no longer
present in the regenerated file.

## Run locally

```bash
docker run -d --name waits-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=waits -p 5432:5432 postgres:16-alpine
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8080
```

Requires Python 3.12 (CI and the Docker image use 3.12). `DATABASE_URL`
overrides the default `postgresql://postgres:postgres@localhost:5432/waits`.
Startup creates the schema and seeds the `data/` files; the app does not start
without a database. Open http://localhost:8080/ for the map,
`/airport/SEA` for a drill-down, `/ops` for source health and `/api` for the
public API docs.

## Tests

```bash
pip install -r requirements-dev.txt
ruff check .
mypy
pytest
```

Tests never touch the network or a database. `tests/test_fixture_replay.py` replays one
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
python scripts/probe_weather_alerts.py        # live NWS cycle, refresh tests/weather_fixtures/nws_alerts.json
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
