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

function renderTravelBanner(tp) {
  const el = document.getElementById("travel-banner");
  if (!el) return;
  if (!tp) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.className = "travel-banner " + (tp.active ? tp.intensity : "upcoming");
  if (tp.active) {
    const label = tp.intensity === "peak" ? "Peak travel" : "Elevated travel";
    el.innerHTML = "<b>" + esc(label) + ": " + esc(tp.name) +
      "</b> — expect longer waits. " + esc(tp.note);
  } else {
    const days = Number(tp.days_until);
    el.innerHTML = "Upcoming: <b>" + esc(tp.name) + "</b> begins " +
      (days === 0 ? "today" : "in " + days + (days === 1 ? " day" : " days")) +
      " (" + esc(tp.start) + ").";
  }
}

function renderLeaderboard(data) {
  const el = document.getElementById("leaderboard");
  if (!el) return;
  const sections = [
    ["worst_standard", "Worst standard waits"],
    ["worst_precheck", "Worst PreCheck waits"],
    ["most_improved", "Most improved (3 hr)"],
  ];
  let html = '<div class="leaderboard-grid">';
  for (const [key, title] of sections) {
    const section = data[key] || { entries: [], quiet: true };
    const quietText = key === "most_improved"
      ? "No clear improvement in the last 3 hours."
      : "Quiet right now — fewer than 3 airports reporting fresh waits.";
    html += '<article class="leaderboard-card"><h2>' + title + "</h2>";
    if (section.quiet) {
      html += '<p class="leaderboard-quiet">' + quietText + "</p>";
    }
    if (section.entries && section.entries.length) {
      html += "<ol>";
      section.entries.forEach((entry) => {
        const freshness = agoMinutes(entry.fetched_at || entry.as_of);
        const freshnessText = freshness === null ? "" : freshness + " min ago";
        const drop = key === "most_improved" && entry.drop_seconds > 0
          ? '<span class="leaderboard-drop">−' + esc(fmtMin(entry.drop_seconds)) + "</span>"
          : "";
        html += '<li class="leaderboard-row"><div class="leaderboard-main"><a class="leaderboard-iata" href="/airport/' +
          esc(entry.iata) + '">' + esc(entry.iata) + "</a> <span>" + esc(entry.name) +
          '</span><div class="leaderboard-checkpoint">' + esc(entry.checkpoint) + " · " +
          esc(entry.source) + '</div></div><div class="leaderboard-wait">' + esc(fmtMin(entry.wait_seconds)) +
          " " + drop + '<small>' + esc(freshnessText) + "</small></div></li>";
      });
      html += "</ol>";
    } else if (!section.quiet) {
      html += '<p class="leaderboard-quiet">No fresh waits reported.</p>';
    }
    html += "</article>";
  }
  html += '</div><div class="leaderboard-footer">Fresh within 30 min · published airport feeds</div>';
  el.innerHTML = html;
}

async function refreshLeaderboard() {
  try {
    const response = await fetch("/api/leaderboard");
    if (!response.ok) return;
    renderLeaderboard(await response.json());
  } catch (_) {
    // A failed refresh leaves the last successful snapshot visible.
  }
}

if (document.getElementById("leaderboard")) {
  refreshLeaderboard();
  setInterval(refreshLeaderboard, 60000);
}
