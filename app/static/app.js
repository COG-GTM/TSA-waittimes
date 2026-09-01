function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtMin(sec) {
  if (sec === null || sec === undefined) return "–";
  return Math.round(sec / 60) + " min";
}

function agoMinutes(iso) {
  if (!iso) return null;
  return Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
}

function asPublished(iso) {
  const m = agoMinutes(iso);
  return m === null ? "" : "as published " + m + " min ago";
}

function renderTsaStrip(tsa) {
  const el = document.getElementById("tsa-strip");
  if (!el) return;
  if (!tsa) {
    el.innerHTML = "";
    renderTsaHistory([]);
    return;
  }
  let html = "National throughput " + esc(tsa.date) + ": <b>" +
    tsa.travelers.toLocaleString() + "</b> travelers";
  if (tsa.lastyear_travelers) {
    const delta = ((tsa.travelers - tsa.lastyear_travelers) / tsa.lastyear_travelers) * 100;
    html += " (" + (delta >= 0 ? "+" : "") + delta.toFixed(1) + "% vs same day last year)";
  }
  html += " — TSA.gov";
  el.innerHTML = html;
  renderTsaHistory(tsa.history);
}

function renderTsaHistory(history) {
  const el = document.getElementById("tsa-history");
  if (!el || !history || history.length < 2) {
    if (el) el.innerHTML = "";
    return;
  }
  const points = history.map(p => [String(p[0]), Number(p[1])])
    .filter(p => Number.isFinite(p[1]));
  if (points.length < 2) {
    el.innerHTML = "";
    return;
  }
  const w = 300, h = 30, pad = 2;
  const min = Math.min.apply(null, points.map(p => p[1]));
  const max = Math.max.apply(null, points.map(p => p[1]));
  const span = Math.max(1, max - min);
  const pts = points.map((p, i) => {
    const x = pad + (i / Math.max(1, points.length - 1)) * (w - 2 * pad);
    const y = h - pad - ((p[1] - min) / span) * (h - 2 * pad);
    return x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
  el.innerHTML = '<span>weekly avg, 2 yr</span><svg class="sparkline" viewBox="0 0 ' + w + " " + h +
    '" preserveAspectRatio="none"><polyline points="' + esc(pts) + '"/></svg>';
}
