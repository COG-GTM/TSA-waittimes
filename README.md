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

## Live sources

See `SOURCES` in `app/sources/adapters.py` (single source of truth). Each entry
records the airport, the public page where the data is published, attribution,
and refresh rate.

## Run locally

```bash
docker run -d --name waits-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=waits -p 5432:5432 postgres:16-alpine
pip install -r requirements.txt
uvicorn app.main:app --port 8080
```

`DATABASE_URL` overrides the default local connection string.

## Continuous deployment

Every push to `main` deploys the application to Fly.io through the
`.github/workflows/fly-deploy.yml` workflow. The repository must define a
`FLYIO_TSA_TOKEN` secret with permission to deploy the Fly.io application.
The canonical public hostname is `waitpicture.com` (also available at
`www.waitpicture.com`). The legacy `tsadelays.com` and `www.tsadelays.com`
hostnames return a 301 redirect to the canonical domain, handled in the app.

## Operations

See [RUNBOOK.md](RUNBOOK.md).
