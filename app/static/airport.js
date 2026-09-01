const container = document.getElementById("checkpoints");
const iata = container.dataset.iata;

function sparkline(history) {
  if (!history || history.length < 2) {
    return '<div class="cp-src">History accrues from first ingest — check back shortly.</div>';
  }
  const w = 300, h = 44, pad = 2;
  const t0 = history[0][0], t1 = history[history.length - 1][0];
  const maxW = Math.max.apply(null, history.map(p => p[1])) || 1;
  const pts = history.map(p => {
    const x = pad + ((p[0] - t0) / Math.max(1, t1 - t0)) * (w - 2 * pad);
    const y = h - pad - (p[1] / maxW) * (h - 2 * pad - 8);
    return x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
  return '<svg class="sparkline" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' +
    '<polyline points="' + pts + '"/>' +
    '<text class="axis" x="2" y="8">peak ' + fmtMin(maxW) + "</text></svg>";
}

function renderWeather(alerts) {
  const el = document.getElementById("wx-alerts");
  if (!el) return;
  if (!alerts || !alerts.length) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = alerts.map(al =>
    '<div class="wx-alert ' + alertClass(al) + '">' +
      '<div class="wx-event">&#9888; ' + esc(al.event) +
        '<span class="wx-sev">' + esc(al.severity) + "</span></div>" +
      '<div class="wx-headline">' + esc(al.headline || "") + "</div>" +
      (al.area ? '<div class="wx-area">' + esc(al.area) + "</div>" : "") +
      '<div class="wx-src">' + esc(al.sender || "") + " · Source: " +
        '<a href="' + esc(al.source_url) + '" rel="noopener">' + esc(al.source) + "</a>" +
        (al.expires ? " · until " + esc(new Date(al.expires).toLocaleString()) : "") +
      "</div></div>"
  ).join("");
}

const DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const outlookState = { typical: null, forecast: null, buckets: null };

function heatColor(minutes, maxMinutes) {
  if (minutes === null || minutes === undefined) return "var(--border)";
  const t = Math.min(1, minutes / Math.max(5, maxMinutes));
  if (t < 0.5) return "rgba(63,185,80," + (0.25 + t) + ")";
  if (t < 0.8) return "var(--amber)";
  return "var(--red)";
}

function renderTypicalNow(t) {
  if (!t || t.median_minutes === null || t.median_minutes === undefined) return "";
  let cmp = "";
  if (t.delta_minutes !== null && t.delta_minutes !== undefined) {
    const d = Math.round(t.delta_minutes);
    cmp = d > 1 ? '<span class="outlook-worse">' + d + " min above typical</span>"
      : d < -1 ? '<span class="outlook-better">' + (-d) + " min below typical</span>"
      : '<span class="outlook-normal">about typical</span>';
  }
  return '<div class="outlook-row">' +
    (t.current_minutes !== null && t.current_minutes !== undefined
      ? "Now: <b>" + Math.round(t.current_minutes) + " min</b> · " : "") +
    "Typical this hour: <b>" + Math.round(t.median_minutes) + " min</b>" +
    (t.p75_minutes !== null && t.p75_minutes !== undefined
      ? " (p75 " + Math.round(t.p75_minutes) + " min)" : "") +
    (cmp ? " — " + cmp : "") + "</div>";
}

function renderForecastChips(fc) {
  if (!fc) return "";
  if (!fc.available) {
    return '<div class="outlook-row outlook-muted">Forecast: not enough history yet.</div>';
  }
  return '<div class="outlook-row outlook-chips">Forecast: ' + fc.horizons.map(h =>
    '<span class="forecast-chip conf-' + esc(h.confidence) + '">+' + esc(h.horizon_minutes) +
    "m <b>" + esc(h.wait_minutes) + " min</b><small>" + esc(h.confidence) + "</small></span>"
  ).join("") + "</div>";
}

function renderHeatmap(buckets) {
  if (!buckets) return "";
  const withData = buckets.filter(b => b.median_minutes !== null && b.sample_count > 0);
  if (withData.length < 24) return "";
  const maxMinutes = Math.max.apply(null, withData.map(b => b.median_minutes));
  const byKey = {};
  buckets.forEach(b => { byKey[b.dow + "-" + b.hour] = b; });
  const now = new Date();
  const nowDow = (now.getUTCDay() + 6) % 7, nowHour = now.getUTCHours();
  let html = '<div class="heatmap"><div class="heatmap-title">Typical standard wait by hour of week (UTC)</div>';
  html += '<div class="heatmap-grid">';
  for (let d = 0; d < 7; d++) {
    html += '<span class="heatmap-dow">' + DOW_LABELS[d] + "</span>";
    for (let h = 0; h < 24; h++) {
      const b = byKey[d + "-" + h];
      const m = b && b.sample_count > 0 ? b.median_minutes : null;
      const cur = d === nowDow && h === nowHour ? " now" : "";
      const label = DOW_LABELS[d] + " " + String(h).padStart(2, "0") + ":00 UTC — " +
        (m === null ? "no data" : Math.round(m) + " min typical");
      html += '<span class="heatmap-cell' + cur + '" title="' + esc(label) +
        '" style="background:' + heatColor(m, maxMinutes) + '"></span>';
    }
  }
  html += '</div><div class="heatmap-legend"><span class="heatmap-cell" style="background:' +
    heatColor(1, 60) + '"></span> shorter <span class="heatmap-cell" style="background:var(--amber)"></span>' +
    ' <span class="heatmap-cell" style="background:var(--red)"></span> longer · outline = current hour</div></div>';
  return html;
}

function renderOutlook() {
  const el = document.getElementById("outlook");
  if (!el) return;
  const html = renderTypicalNow(outlookState.typical) +
    renderForecastChips(outlookState.forecast) +
    renderHeatmap(outlookState.buckets);
  el.hidden = !html;
  el.innerHTML = html ? '<h2 class="outlook-head">Outlook</h2>' + html : "";
}

function render(data) {
  const a = data.airport;
  document.getElementById("airport-title").textContent = a.iata + " — " + a.name;
  document.getElementById("airport-sub").textContent = a.city + ", " + a.state;
  const rankEl = document.getElementById("airport-rank");
  const e = a.enplanements;
  if (e) {
    const millions = (Number(e.enplanements) / 1000000).toFixed(1);
    rankEl.innerHTML = "#" + esc(e.rank) + " busiest US airport — " + millions +
      "M enplanements (CY" + esc(e.year) + ", FAA) — " +
      '<a href="' + esc(e.source_url) + '" rel="noopener">' + esc(e.source) + "</a>";
  } else {
    rankEl.innerHTML = "";
  }
  document.getElementById("faa-banner").innerHTML = (data.faa_events || []).map(event =>
    '<div class="faa-banner">' +
      '<div>' + esc(faaEventText(event)) + "</div>" +
      '<div class="faa-attribution"><a href="https://nasstatus.faa.gov/" rel="noopener">' +
        esc(data.faa_attribution || "FAA National Airspace System Status (nasstatus.faa.gov)") +
      "</a></div>" +
      "</div>"
  ).join("");
  renderWeather(data.weather_alerts);
  outlookState.typical = data.typical || null;
  renderOutlook();
  if (!data.checkpoints.length) {
    container.innerHTML = '<div class="cp-card"><div class="cp-name">No public wait-time data published for this airport.</div>' +
      '<div class="cp-src">This airport does not currently publish live checkpoint wait times on its public website.</div></div>';
    return;
  }
  container.innerHTML = data.checkpoints.map(cp => {
    const waitCls = !cp.is_open ? "closed" : (cp.stale ? "stale" : "");
    const waitTxt = !cp.is_open ? "Closed" : fmtMin(cp.wait_seconds);
    return '<div class="cp-card">' +
      '<div class="cp-name">' + esc(cp.name) +
        '<span class="cp-lane">' + esc(cp.lane_type) + "</span>" +
        (cp.stale ? '<span class="stale-flag">STALE — no update in 30+ min</span>' : "") +
      "</div>" +
      '<div class="cp-wait ' + waitCls + '">' + waitTxt + "</div>" +
      '<div class="cp-src">Source: <a href="' + esc(cp.source_url) + '" rel="noopener">' + esc(cp.source) + "</a><br>" +
        asPublished(cp.published_at || cp.fetched_at) +
        (cp.published_at ? "" : " (fetch time; source does not publish a timestamp)") +
      "</div>" + sparkline(cp.history) + "</div>";
  }).join("");
}

function refreshAirport() {
  fetch("/api/airport/" + iata).then(r => r.json()).then(render).catch(() => {});
  fetch("/api/airport/" + iata + "/forecast").then(r => r.ok ? r.json() : null).then(fc => {
    if (!fc) return;
    outlookState.forecast = fc;
    renderOutlook();
  }).catch(() => {});
  fetch("/api/airport/" + iata + "/typical").then(r => r.ok ? r.json() : null).then(t => {
    if (!t) return;
    outlookState.buckets = t.buckets || null;
    renderOutlook();
  }).catch(() => {});
  fetch("/api/summary").then(r => r.json()).then(d => {
    renderTsaStrip(d.tsa_throughput);
    renderTravelBanner(d.travel_period);
  }).catch(() => {});
}
setInterval(refreshAirport, 60000);
refreshAirport();
