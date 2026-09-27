const state = { packets: [], map: null, marker: null, trail: null, fitted: false, frequencyEditing: false };

function text(id, value) {
  document.getElementById(id).textContent = value;
}

function localTime(timestamp) {
  return new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function number(value, digits = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString([], { maximumFractionDigits: digits }) : "—";
}

function initialiseMap() {
  if (typeof L === "undefined") {
    document.getElementById("map").classList.add("map-unavailable");
    text("map-message", "The map library could not load. Packet fields and history will still update.");
    return;
  }
  state.map = L.map("map", { zoomControl: true }).setView([46.8, 8.2], 7);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(state.map);
  state.trail = L.polyline([], { color: "#e96c38", weight: 4, opacity: 0.85 }).addTo(state.map);
}

function updateMap(packets) {
  if (!state.trail) return;
  const positions = packets.filter(packet => packet.lat !== null && packet.lon !== null);
  const points = positions.map(packet => [packet.lat, packet.lon]);
  state.trail.setLatLngs(points);
  document.getElementById("map-message").hidden = positions.length > 0;
  if (!positions.length) return;

  const latest = positions[positions.length - 1];
  const point = [latest.lat, latest.lon];
  if (!state.marker) {
    state.marker = L.circleMarker(point, {
      radius: 8, color: "#fff", weight: 3, fillColor: "#135f78", fillOpacity: 1,
    }).addTo(state.map);
  } else {
    state.marker.setLatLng(point);
  }
  const payload = latest.fields.callsign || "Balloon";
  state.marker.bindPopup(`<strong>${escapeHtml(payload)}</strong><br>${latest.lat.toFixed(5)}, ${latest.lon.toFixed(5)}<br>${number(latest.alt)} m`);
  if (!state.fitted) fitTrail();
}

function fitTrail() {
  if (!state.trail) return;
  const bounds = state.trail.getBounds();
  if (bounds.isValid()) {
    state.map.fitBounds(bounds, { padding: [35, 35], maxZoom: 14 });
    state.fitted = true;
  }
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

function updateLatest(packet) {
  const fields = document.getElementById("decoded-fields");
  fields.replaceChildren();
  Object.entries(packet.fields).forEach(([name, value]) => {
    const wrapper = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = name.replaceAll("_", " ");
    description.textContent = value || "—";
    wrapper.append(term, description);
    fields.appendChild(wrapper);
  });
  text("raw-packet", packet.raw);
  text("latest-snr", packet.snr === null ? "SNR —" : `SNR ${number(packet.snr, 1)} dB`);
  text("payload-name", packet.fields.callsign || "—");
  text("altitude", packet.alt === null ? "—" : `${number(packet.alt)} m`);
  text("last-update", localTime(packet.received_at));
}

function updateHistory(packets) {
  const body = document.getElementById("packet-history");
  body.replaceChildren();
  [...packets].reverse().slice(0, 50).forEach(packet => {
    const row = document.createElement("tr");
    const values = [
      localTime(packet.received_at),
      packet.fields.callsign || "—",
      packet.fields.frame || "—",
      packet.lat === null ? "—" : `${packet.lat.toFixed(5)}, ${packet.lon.toFixed(5)}`,
      packet.alt === null ? "—" : `${number(packet.alt)} m`,
      packet.snr === null ? "—" : `${number(packet.snr, 1)} dB`,
    ];
    values.forEach(value => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
}

function connectionStatus(packets) {
  const element = document.getElementById("connection");
  if (!packets.length) {
    element.className = "connection waiting";
    element.lastChild.textContent = "Waiting for packets";
    return;
  }
  const ageSeconds = (Date.now() - new Date(packets[packets.length - 1].received_at).getTime()) / 1000;
  const live = ageSeconds < 30;
  element.className = `connection ${live ? "live" : "stale"}`;
  element.lastChild.textContent = live ? "Receiving packets" : "No recent packet";
}

function updateFrequency(frequencyMhz) {
  if (frequencyMhz === null || frequencyMhz === undefined) return;
  const input = document.getElementById("frequency-input");
  if (!state.frequencyEditing) input.value = Number(frequencyMhz).toFixed(3);
  text("frequency-status", `Listening on ${Number(frequencyMhz).toFixed(3)} MHz`);
}

async function tuneReceiver(event) {
  event.preventDefault();
  const input = document.getElementById("frequency-input");
  const button = event.currentTarget.querySelector("button");
  const frequencyMhz = Number(input.value);
  button.disabled = true;
  text("frequency-status", "Retuning receiver…");
  try {
    const response = await fetch("/api/frequency", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ frequency_mhz: frequencyMhz }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    state.frequencyEditing = false;
    updateFrequency(result.frequency_mhz);
  } catch (error) {
    text("frequency-status", `Tune failed: ${error.message}`);
  } finally {
    button.disabled = false;
  }
}

async function refresh() {
  try {
    const response = await fetch("/api/packets", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const feed = await response.json();
    const packets = feed.packets;
    state.packets = packets;
    updateFrequency(feed.frequency_mhz);
    text("packet-count", feed.total.toLocaleString());
    connectionStatus(packets);
    updateMap(packets);
    updateHistory(packets);
    if (packets.length) updateLatest(packets[packets.length - 1]);
  } catch (error) {
    const element = document.getElementById("connection");
    element.className = "connection stale";
    element.lastChild.textContent = "Dashboard disconnected";
  }
}

initialiseMap();
document.getElementById("fit-trail").addEventListener("click", fitTrail);
document.getElementById("frequency-form").addEventListener("submit", tuneReceiver);
document.getElementById("frequency-input").addEventListener("input", () => { state.frequencyEditing = true; });
refresh();
setInterval(refresh, 2000);
