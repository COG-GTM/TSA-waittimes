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
  fetch("/api/summary").then(r => r.json()).then(d => {
    renderTsaStrip(d.tsa_throughput);
    renderTravelBanner(d.travel_period);
  }).catch(() => {});
}
setInterval(refreshAirport, 60000);
refreshAirport();
