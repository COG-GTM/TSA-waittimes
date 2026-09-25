# Changelog

All notable changes to this repository are documented in this file. Entries are
grouped by week and reference the pull request that merged each change.

## Week of 2026-09-18 to 2026-09-25

### Features

- None

### Bug Fixes

- Fix retention cleanup that never kept up (the cause of the mid-September Postgres disk-full outage) by indexing the `raw_id` foreign keys on `observations`, `faa_airport_events`, and `weather_alerts` so batch deletes of `raw_payloads` no longer seq-scan ([#29](https://github.com/COG-GTM/TSA-waittimes/pull/29))

### Improvements

- Replace exact `count(*)` on large tables with `estimated_row_count_sql` in `/healthz` and the ops page so the 30 s health check stops timing out during recovery; rename leaderboard headings to "Longest"; document DB sizing and index requirements in `RUNBOOK.md` ([#29](https://github.com/COG-GTM/TSA-waittimes/pull/29))

### Breaking Changes

- Retention defaults tightened: `RETENTION_RAW_PAYLOAD_DAYS` 14 → 3 and `RETENTION_OBSERVATION_DAYS` 90 → 30; raw observations older than 30 days are now pruned (hourly rollups keep the history), so set the environment overrides if a longer raw window is needed ([#29](https://github.com/COG-GTM/TSA-waittimes/pull/29))
