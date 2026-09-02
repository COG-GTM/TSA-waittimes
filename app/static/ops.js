(function () {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const escOps = (value) => String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

  function fmtInt(value) {
    return value == null ? "—" : Number(value).toLocaleString();
  }

  function fmtAge(seconds) {
    if (seconds == null) return "—";
    const value = Math.max(0, Math.floor(Number(seconds)));
    if (value < 60) return `${value}s`;
    const minutes = Math.floor(value / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ${minutes % 60}m`;
    return `${Math.floor(hours / 24)}d`;
  }

  function fmtBytes(value) {
    if (value == null) return "—";
    let amount = Number(value);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) {
      amount /= 1024;
      unit += 1;
    }
    return `${amount.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${units[unit]}`;
  }

  function fmtTimestamp(value) {
    if (value == null) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "—" : `${date.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC")}`;
  }

  function setDl(id, entries) {
    byId(id).innerHTML = entries.map(([label, value]) =>
      `<dt>${escOps(label)}</dt><dd>${escOps(value == null ? "—" : value)}</dd>`
    ).join("");
  }

  function render(payload) {
    const counts = payload.status_counts || {};
    byId("ops-green").textContent = fmtInt(counts.green);
    byId("ops-amber").textContent = fmtInt(counts.amber);
    byId("ops-red").textContent = fmtInt(counts.red);
    const system = payload.system || {};
    setDl("ops-system", [
      ["Open checkpoints", fmtInt(system.open_checkpoints)],
      ["Fresh airports", fmtInt(system.fresh_airports)],
      ["Observations", fmtInt(system.observations_rows)],
      ["Hourly rollups", fmtInt(system.observations_hourly_rows)],
      ["Raw payloads", fmtInt(system.raw_payloads_rows)],
      ["Last rollup", fmtTimestamp(system.last_rollup_at)],
      ["Last cleanup", fmtTimestamp(system.last_cleanup_at)],
      ["Database size", fmtBytes(system.db_size_bytes)],
      ["Uptime", fmtAge(system.uptime_seconds)],
      ["Started", fmtTimestamp(system.started_at)],
    ]);
    const dataSources = payload.data_sources || {};
    const faa = dataSources.faa_events || {};
    const weather = dataSources.weather_alerts || {};
    const tsa = dataSources.tsa_throughput || {};
    setDl("ops-faa", [["Rows", fmtInt(faa.rows)], ["Latest", fmtTimestamp(faa.latest_at)]]);
    setDl("ops-weather", [["Rows", fmtInt(weather.rows)], ["Latest", fmtTimestamp(weather.latest_at)]]);
    setDl("ops-tsa", [
      ["Rows", fmtInt(tsa.rows)],
      ["Latest date", tsa.latest_date == null ? "—" : tsa.latest_date],
      ["Latest fetch", fmtTimestamp(tsa.latest_at)],
    ]);

    const body = byId("ops-sources").querySelector("tbody");
    const sources = payload.sources || [];
    if (payload.sources_available === false) {
      body.innerHTML = '<tr><td colspan="9" class="ops-empty">Source inventory unavailable</td></tr>';
    } else if (sources.length === 0) {
      body.innerHTML = '<tr><td colspan="9" class="ops-empty">No sources registered</td></tr>';
    } else {
      body.innerHTML = sources.map((source) => {
        const status = ["green", "amber", "red"].includes(source.status) ? source.status : "red";
        const code = source.iata && /^[A-Z]{3}$/.test(source.iata) ? source.iata : null;
        const airport = code
          ? `<a href="/airport/${escOps(code)}">${escOps(code)}</a>`
          : escOps(source.code);
        const error = source.last_error == null ? "—" : escOps(source.last_error);
        return `<tr>
          <td>${airport}</td>
          <td>${escOps(source.name)}</td>
          <td><span class="pill pill-${status}">${status}</span></td>
          <td>${escOps(fmtTimestamp(source.last_success_at))}</td>
          <td>${escOps(fmtAge(source.last_success_age_seconds))}</td>
          <td>${escOps(fmtInt(source.consecutive_failures))}</td>
          <td>${escOps(fmtAge(source.estimated_backoff_seconds))}</td>
          <td>${escOps(fmtInt(source.observations_last_hour))}</td>
          <td class="err" title="${error}">${error}</td>
        </tr>`;
      }).join("");
    }
    const generated = payload.generated_at ? new Date(payload.generated_at) : null;
    const generatedText = generated && !Number.isNaN(generated.getTime())
      ? generated.toISOString().slice(11, 19)
      : "—";
    byId("ops-updated").textContent = `Updated ${generatedText} UTC · auto-refresh 60s`;
  }

  async function refresh() {
    try {
      const response = await fetch("/api/ops");
      if (!response.ok) throw new Error("ops request failed");
      render(await response.json());
    } catch (_error) {
      byId("ops-updated").textContent = "Refresh failed — retrying";
    }
  }

  byId("ops-sources").querySelector("tbody").innerHTML =
    '<tr><td colspan="9" class="ops-empty">Loading…</td></tr>';
  byId("ops-updated").textContent = "Loading…";
  refresh();
  window.setInterval(refresh, 60000);
})();
