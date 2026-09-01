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

const ALERT_SEVERITY_CLASS = {
  Extreme: "sev-extreme", Severe: "sev-extreme", Moderate: "sev-moderate",
  Minor: "sev-minor", Unknown: "sev-minor",
};

function alertClass(alert) {
  return ALERT_SEVERITY_CLASS[alert && alert.severity] || "sev-minor";
}

function alertBadge(alert) {
  if (!alert) return "";
  return '<span class="wx-badge ' + alertClass(alert) + '" title="' +
    esc(alert.headline || alert.event) + '">&#9888; ' + esc(alert.event) + "</span>";
}

function renderTsaStrip(tsa) {
  const el = document.getElementById("tsa-strip");
  if (!el || !tsa) return;
  let html = "National throughput " + esc(tsa.date) + ": <b>" +
    tsa.travelers.toLocaleString() + "</b> travelers";
  if (tsa.lastyear_travelers) {
    const delta = ((tsa.travelers - tsa.lastyear_travelers) / tsa.lastyear_travelers) * 100;
    html += " (" + (delta >= 0 ? "+" : "") + delta.toFixed(1) + "% vs same day last year)";
  }
  html += " — TSA.gov";
  el.innerHTML = html;
}
