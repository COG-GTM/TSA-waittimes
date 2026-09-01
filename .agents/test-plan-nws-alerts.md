# Test plan — PR #17 NWS weather alerts (commit 1c2796c)

Env: local uvicorn on :8080, docker pg `waits-pg`, live NWS feed.
Setup already verified: `/healthz` HTTP 200, `status: ok`, `NWS-ALERTS healthy: true`,
`consecutive_failures: 0`, `last_error: null` (zone coverage 502/502 → no ZoneBackfillIncomplete).
Current affected set from `/api/summary`: BPT, CLL, ESD, FAI, FRD, HOU(live), IAH(live), LCH.

Code paths traced: app/main.py:66-98,140-150,188-192 (API), app/static/app.js:22-36 (alertBadge/esc),
app/static/map.js:17-21,78 (popup + live-card badge), app/static/airport.js:21-40 (renderWeather),
app/templates/airport.html:9 (#wx-alerts section).

## T1 — National list shows severity badge only on affected airports
1. Open http://localhost:8080/ , scroll to the live-airport card grid.
2. PASS: IAH and HOU cards each show an amber "⚠ Tropical Cyclone Local Statement" badge
   (class wx-badge sev-moderate). Cards for live airports with no alert (SEA, BOS, JFK, DEN…)
   show NO badge. FAIL if badges appear on every card or on none.

## T2 — Map popup badge for affected airport
1. Zoom the map to Houston, click the IAH marker.
2. PASS: popup shows "IAH — George Bush Intercontinental…", the ⚠ event badge, and the line
   "National Weather Service (weather.gov)".
3. Click the SEA marker (regression/negative): popup shows wait time but NO ⚠ badge and no NWS line.

## T3 — Airport drill-down banner (affected)
1. Navigate to http://localhost:8080/airport/IAH .
2. PASS: banner section above checkpoints shows: event "Tropical Cyclone Local Statement",
   severity chip "MODERATE", headline text, area text containing "Houston",
   footer with sender + "Source: National Weather Service (weather.gov)" as a link to
   https://www.weather.gov/documentation/services-web-api and "· until <localized 09/01/2026 …>"
   matching API expires 2026-09-01T23:45:00Z.
3. PASS: existing checkpoint cards for IAH still render with lane names and wait values/Closed.
4. Click the attribution link → weather.gov documentation page loads (or verify href via zoom).

## T4 — Unaffected airport: no banner, no regression
1. Navigate to /airport/SEA.
2. PASS: no `.wx-alert` banner visible between the header and checkpoints; SEA checkpoint cards
   render with lane names, wait minutes / "Closed", and sparklines as before.

## T5 — Feed-derived strings are HTML-escaped (adversarial)
1. Insert a synthetic alert row for a live airport PIT directly in Postgres with hostile values:
   event `<img src=x onerror=alert(1)>Storm`, headline `<script>window.__pwn=1</script>bad`,
   area `<b>AREA</b>`, severity `Severe`, expires now()+2h.
2. Reload /airport/PIT.
3. PASS: banner displays the tags as literal text (e.g. shows `<img src=x onerror=...>Storm`),
   no broken image / no injected element. Verify in console `window.__pwn === undefined` and
   `document.querySelectorAll('#wx-alerts img, #wx-alerts script b').length === 0`.
   FAIL if any tag renders as markup (bold "AREA", missing literal `<b>`).
4. Reload / (national) and confirm the PIT card badge also shows the literal text.
5. Delete the synthetic row afterwards.
