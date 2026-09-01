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

function renderTravelBanner(tp) {
  const el = document.getElementById("travel-banner");
  if (!el || !tp) return;
  el.hidden = false;
  el.className = "travel-banner " + (tp.active ? tp.intensity : "upcoming");
  if (tp.active) {
    const label = tp.intensity === "peak" ? "Peak travel" : "Elevated travel";
    el.innerHTML = "<b>" + esc(label) + ": " + esc(tp.name) +
      "</b> — expect longer waits. " + esc(tp.note);
  } else {
    el.innerHTML = "Upcoming: <b>" + esc(tp.name) + "</b> begins in " +
      Number(tp.days_until) + " day(s) (" + esc(tp.start) + ").";
  }
}
