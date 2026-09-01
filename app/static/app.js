function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

const FAA_EVENT_LABELS = {
  ground_stop: "FAA ground stop",
  ground_delay: "FAA ground delay program",
  departure_delay: "FAA departure delay",
  arrival_delay: "FAA arrival delay",
  closure: "FAA airport closure",
};

function faaEventText(ev) {
  const label = FAA_EVENT_LABELS[ev.event_type] || "FAA event";
  const reason = ev.reason ? ": " + ev.reason : "";
  const average = ev.avg_delay_seconds ? ", avg " + Math.round(ev.avg_delay_seconds / 60) + " min" : "";
  return label + reason + average;
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
