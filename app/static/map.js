const map = L.map("map", { zoomSnap: 0.25, attributionControl: false });
map.fitBounds([[24.5, -125], [49.5, -66.5]], { padding: [10, 10] });
L.control.attribution({ prefix: false })
  .addAttribution("Basemap: US Census state boundaries · Airports: FAA/OurAirports public data")
  .addTo(map);

fetch("/static/us-states.json").then(r => r.json()).then(gj => {
  L.geoJSON(gj, {
    style: { color: "#2d333b", weight: 1, fillColor: "#161b22", fillOpacity: 1 },
  }).addTo(map);
  refresh();
});

let liveLayer = L.layerGroup().addTo(map);
let grayLayer = L.layerGroup().addTo(map);

function popupHtml(a) {
  let html = "<b>" + esc(a.iata) + "</b> — " + esc(a.name) + "<br>";
  if (a.weather_alert) {
    html += '<div class="popup-wx">' + alertBadge(a.weather_alert) +
      '<div class="wx-src">National Weather Service (weather.gov)</div></div>';
  }
  if (a.live) {
    html += '<div class="popup-wait">' + fmtMin(a.max_wait_seconds) + (a.stale ? " (stale)" : "") + "</div>";
    if (a.max_precheck_seconds !== undefined)
      html += "PreCheck: " + fmtMin(a.max_precheck_seconds) + "<br>";
    html += '<div class="popup-src">' + esc(a.source) + "<br>" + asPublished(a.as_of || a.last_fetch) + "</div>";
    html += '<a href="/airport/' + esc(a.iata) + '">Checkpoint detail →</a>';
  } else {
    html += '<span class="popup-src">No public wait-time data published</span>';
  }
  return html;
}

function render(data) {
  document.getElementById("live-count").textContent = data.live_count;
  document.getElementById("gray-count").textContent = data.no_data_count;
  document.getElementById("updated").textContent =
    "Auto-refreshes every 60s · Updated " + new Date(data.generated_at).toLocaleTimeString();
  renderTsaStrip(data.tsa_throughput);

  liveLayer.clearLayers();
  grayLayer.clearLayers();
  const liveCards = [];
  data.airports.forEach(a => {
    if (a.live) {
      const wait = a.max_wait_seconds;
      const marker = L.circleMarker([a.lat, a.lon], {
        radius: 8, color: "#0d1117", weight: 1.5,
        fillColor: a.stale ? "#d29922" : "#2ea043", fillOpacity: 1,
      }).bindPopup(popupHtml(a));
      marker.on("click", () => marker.openPopup());
      liveLayer.addLayer(marker);
      const label = L.marker([a.lat, a.lon], {
        icon: L.divIcon({
          className: "wait-label",
          html: '<span class="' + (a.stale ? "stale" : "") + '">' + esc(a.iata) + " " + fmtMin(wait) + "</span>",
        }),
        interactive: false,
      });
      liveLayer.addLayer(label);
      liveCards.push(a);
    } else {
      grayLayer.addLayer(
        L.circleMarker([a.lat, a.lon], {
          radius: a.hub === "large" ? 4 : 2.5,
          color: "#0d1117", weight: 0.5, fillColor: "#484f58", fillOpacity: 0.9,
        }).bindPopup(popupHtml(a))
      );
    }
  });

  liveCards.sort((x, y) => (y.max_wait_seconds || 0) - (x.max_wait_seconds || 0));
  document.getElementById("live-list").innerHTML = liveCards.map(a =>
    '<a class="live-card" href="/airport/' + esc(a.iata) + '">' +
      '<span class="wait ' + (a.stale ? "stale" : "") + '">' + fmtMin(a.max_wait_seconds) + "</span>" +
      '<span class="code">' + esc(a.iata) + "</span>" +
      alertBadge(a.weather_alert) +
      '<div class="meta">' + esc(a.name) + (a.stale ? " · STALE" : "") + "<br>" +
      esc(a.source) + " · " + asPublished(a.as_of || a.last_fetch) + "</div></a>"
  ).join("");
}

function refresh() {
  fetch("/api/summary").then(r => r.json()).then(render).catch(() => {});
}
setInterval(refresh, 60000);
refresh();
