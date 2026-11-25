import { PROVIDERS } from "./providers.js";

const $ = (s, el = document) => el.querySelector(s);
const params = new URLSearchParams(location.search);
const KIOSK = params.has("kiosk");
const REFRESH_MS = 30_000;
const STALE_MS = 3 * 60_000;

// ---------------------------------------------------------------- settings
// Precedence: URL parameters > saved settings > default (local when served by a box, else demo)
const onPages = location.hostname.endsWith("github.io");
function loadSettings() {
  let s = {};
  try { s = JSON.parse(localStorage.getItem("lanpulse") || "{}"); } catch {}
  if (params.get("source")) s.source = params.get("source");
  if (params.get("url")) s.url = params.get("url");
  s.source = s.source && PROVIDERS[s.source] ? s.source : (onPages ? "demo" : "local");
  return s;
}
let settings = loadSettings();
const saveSettings = () => localStorage.setItem("lanpulse", JSON.stringify(settings));

// ---------------------------------------------------------------- render
const LABEL = { up: "Working", down: "Not responding", warn: "Needs a look", pending: "Checking…",
                maintenance: "Maintenance", unknown: "No data" };
const WEAK_RSSI = -70;

// "5 GHz · Hall node · −58 dBm · 866 Mbps" / "Wired · Office switch"
function linkText(l) {
  if (!l) return "";
  const parts = [l.type === "wired" ? "Wired" : l.band ? `${l.band} GHz` : "Wi-Fi"];
  if (l.node || l.parent) parts.push(esc(l.node || l.parent));
  if (l.rssi != null) parts.push(`<span class="${l.rssi < WEAK_RSSI ? "weak" : ""}">${Math.round(l.rssi)} dBm</span>`);
  if (l.rate_mbps) parts.push(`${Math.round(l.rate_mbps)} Mbps`);
  return parts.join(" · ");
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso.replace(" ", "T") + (iso.includes("Z") || iso.includes("+") ? "" : "Z")).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (u) => (u == null ? "" : `${(u * 100).toFixed(u >= 0.9995 ? 0 : 1)}%`);

function render(snap) {
  const site = snap.site || {};
  document.title = site.name || "lanpulse";
  $("#site-name").textContent = site.name || "Network status";
  $("#site-desc").textContent = site.description || "";

  const sum = snap.summary || {};
  const bad = (sum.down || 0);
  const look = (sum.warn || 0);
  const warn = (sum.pending || 0) + (sum.unknown || 0);
  const banner = $("#banner");
  banner.className = "banner " + (bad ? "down" : look || warn ? "pending" : "up");
  $("#banner-title").textContent = bad
    ? `${bad} ${bad === 1 ? "thing needs" : "things need"} attention`
    : look ? `Working: ${look} ${look === 1 ? "thing" : "things"} to look at when convenient`
    : warn ? "Checking a few things…" : "Everything is working";
  $("#banner-sub").textContent = `${sum.up || 0} of ${sum.total || 0} working` + (look && bad ? ` · ${look} to look at` : "");

  const groups = $("#groups");
  groups.innerHTML = "";
  for (const g of snap.groups || []) {
    const sec = document.createElement("section");
    const gBad = g.items.filter((i) => i.status === "down").length;
    const gLook = g.items.filter((i) => i.status === "warn").length;
    sec.className = "group";
    sec.innerHTML = `<h2>${esc(g.name)} ${gBad ? `<span class="pill down">${gBad} down</span>` : ""}${gLook ? `<span class="pill warn">${gLook} to look at</span>` : ""}</h2><div class="tiles"></div>`;
    const tiles = $(".tiles", sec);
    // problems first, then things to look at, then original order
    const rank = (s) => (s === "down" ? 0 : s === "warn" ? 1 : 2);
    const items = [...g.items].sort((a, b) => rank(a.status) - rank(b.status));
    for (const i of items) {
      const t = document.createElement("article");
      t.className = `tile ${i.status}${i.note ? " has-note" : ""}`;
      t.title = i.message || "";
      const bars = (i.history || []).slice(-30).map((h) => `<i class="${h.s}"></i>`).join("");
      const meta = `${LABEL[i.status] || i.status}${i.status === "down" && i.since ? ` · since ${ago(i.since)}` : ""}${i.latency_ms != null && i.status === "up" ? ` · ${Math.round(i.latency_ms)} ms` : ""}`;
      t.innerHTML = `
        <div class="dot"></div>
        <div class="body">
          <div class="name">${esc(i.name)}</div>
          <div class="meta">${i.note ? esc(i.note) : meta}</div>
          ${i.link && !i.note ? `<div class="link">${linkText(i.link)}</div>` : ""}
          ${bars ? `<div class="bars">${bars}</div>` : ""}
        </div>
        <div class="uptime">${pct(i.uptime_24h)}<small>${i.uptime_24h != null ? "24 h" : ""}</small></div>`;
      tiles.append(t);
    }
    groups.append(sec);
  }
  renderDetails(snap.extras || {});

  const gen = snap.generated ? new Date(snap.generated) : null;
  const stale = !gen || Date.now() - gen.getTime() > STALE_MS;
  const failed = (snap.sources || []).filter((s) => !s.ok);
  $("#updated").textContent = gen ? `Updated ${ago(snap.generated)}` : "";
  $("#updated").dataset.t = snap.generated || "";
  $("#updated").className = stale ? "stale" : "";
  $("#warning").hidden = !(stale || failed.length);
  $("#warning").textContent = stale
    ? "This information is out of date. The monitoring box may be off or disconnected."
    : failed.length ? `Some data couldn't be read: ${failed.map((f) => f.name).join(", ")}` : "";
}

// ---------------------------------------------------------------- detail panels (extras)
// Technical views for whoever looks after the network. Hidden in kiosk mode.
const openPanels = new Set(JSON.parse(localStorage.getItem("lanpulse-open") || "[]"));
const table = (heads, rows) => rows.length
  ? `<div class="scroll"><table><thead><tr>${heads.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${
      rows.map((r) => `<tr>${r.map((c) => `<td>${c ?? ""}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
  : `<p class="help">Nothing to show.</p>`;
const yes = (b) => (b ? "✓" : "");

function renderDetails(x) {
  const box = $("#details");
  if (KIOSK) { box.hidden = true; return; }
  const panels = [];

  // Exposure: what the internet can see
  if (x.exposure) {
    const e = x.exposure, u = e.upnp || {};
    const maps = (u.mappings || []).map((m) => [m.proto, m.external_port, esc(m.internal), esc(m.description)]);
    panels.push(["exposure", "Exposure to the internet",
      `<ul class="facts">
         <li><b>Public IP:</b> ${esc(e.public_ip || "unknown")}</li>
         <li><b>Router layout:</b> ${esc(e.nat || "unknown")}</li>
         <li><b>UPnP:</b> ${u.available === false ? "off or not answering" : u.available ? `on, router's internet side ${esc(u.wan_ip || "?")}` : esc(u.error || "unknown")}</li>
       </ul>
       ${u.available ? `<h4>Ports opened by devices (UPnP)</h4>${table(["Protocol", "Port", "Forwarded to", "Opened by"], maps)}` : ""}`]);
  }

  // Network equipment / topology from a router adapter
  if (x.topology && (x.topology.nodes || []).length) {
    const rows = x.topology.nodes.map((n) => [esc(n.name), esc(n.role), esc(n.ip), esc(n.model),
      n.online ? "online" : `<span class="weak">offline</span>`, n.backhaul ? linkText(n.backhaul) : n.role === "router" ? "(internet)" : "", n.clients ?? "", esc(n.firmware)]);
    const bands = Object.entries(x.client_bands || {}).map(([k, v]) => `<span class="chip">${esc(k)}: ${v}</span>`).join(" ");
    panels.push(["topology", `Network equipment (${esc(x.topology.vendor)})`,
      `${bands ? `<p>Clients by connection: ${bands}</p>` : ""}${table(["Name", "Role", "IP", "Model", "State", "Backhaul / uplink", "Clients", "Firmware"], rows)}`]);
  }

  // Clients with connection type (router) or devices (NetAlertX)
  if ((x.clients || []).length) {
    const rows = [...x.clients].sort((a, b) => (a.link?.rssi ?? 0) - (b.link?.rssi ?? 0)).map((c) =>
      [esc(c.name || ""), esc(c.ip || ""), esc(c.mac || ""), linkText(c.link)]);
    panels.push(["clients", `Connected devices (${x.clients.length}, weakest signal first)`, table(["Name", "IP", "MAC", "Connection"], rows)]);
  } else if ((x.devices || []).length) {
    const rows = x.devices.map((d) => [d.online ? "●" : "", esc(d.name || ""), esc(d.vendor || ""), esc(d.ip || ""), esc(d.mac),
      d.new ? `<span class="chip warn">new</span>` : "", d.random_mac ? "private MAC" : "", esc(ago(d.last_seen ? d.last_seen : ""))]);
    panels.push(["devices", `Devices seen on the network (${x.devices.filter((d) => d.online).length} online)`,
      `<p class="help">From NetAlertX. Connect a router adapter to see wired / 2.4 / 5 GHz and which access point each device uses.</p>
       ${table(["", "Name", "Maker", "IP", "MAC", "", "", "Last seen"], rows)}`]);
  }

  // Wi-Fi airspace survey
  if (x.airspace) {
    const a = x.airspace;
    const rows = (a.networks || []).map((n) => [n.own ? "★" : "", esc(n.ssid || "(hidden)"), esc(n.bssid), `${n.band} GHz`, n.channel,
      n.signal_dbm != null ? `<span class="${n.signal_dbm < WEAK_RSSI ? "weak" : ""}">${Math.round(n.signal_dbm)} dBm</span>` : "",
      n.utilisation_pct != null ? `${n.utilisation_pct}%` : "", n.stations ?? "", n.secure ? "" : `<span class="weak">open</span>`]);
    panels.push(["airspace", `Wi-Fi airspace (${(a.networks || []).length} access points heard)`,
      `<p class="help">Heard by this box's Wi-Fi adapter (${esc(a.interface)}). ★ = your network${a.own_ssids?.length ? ` (${esc(a.own_ssids.join(", "))})` : ""}.</p>
       ${table(["", "Network", "Access point", "Band", "Ch", "Signal", "Busy", "Clients", ""], rows)}`]);
  }

  const nc = x.netcheck;
  box.hidden = !panels.length;
  box.innerHTML = panels.length ? `<h2>Details</h2>${nc?.note ? `<p class="warning">${esc(nc.note)}</p>` : ""}` +
    panels.map(([id, title, html]) => `<details data-id="${id}" ${openPanels.has(id) ? "open" : ""}><summary>${title}</summary><div class="panel">${html}</div></details>`).join("") +
    (nc ? `<p class="help">Network checks run every 10 minutes from ${esc(nc.interface || "?")} (${esc(nc.interface_kind || "")}), last ${esc(ago(nc.generated))}.</p>` : "") : "";
  box.querySelectorAll("details").forEach((d) => d.addEventListener("toggle", () => {
    d.open ? openPanels.add(d.dataset.id) : openPanels.delete(d.dataset.id);
    localStorage.setItem("lanpulse-open", JSON.stringify([...openPanels]));
  }));
}

function renderError(err) {
  $("#banner").className = "banner pending";
  $("#banner-title").textContent = "Can't load status";
  $("#banner-sub").textContent = String(err.message || err);
  $("#warning").hidden = false;
  $("#warning").textContent = `Data source: ${PROVIDERS[settings.source].label}. ${KIOSK ? "" : "Open settings to change it."}`;
}

async function refresh() {
  try { render(await PROVIDERS[settings.source].load(settings)); }
  catch (e) { renderError(e); }
}

// ---------------------------------------------------------------- settings dialog
function openSettings() {
  const dlg = $("#settings");
  const sel = $("#source", dlg);
  sel.innerHTML = Object.entries(PROVIDERS).map(([k, p]) => `<option value="${k}">${esc(p.label)}</option>`).join("");
  sel.value = settings.source;
  const draw = () => {
    const p = PROVIDERS[sel.value];
    $("#source-help", dlg).textContent = p.help;
    $("#fields", dlg).innerHTML = p.fields.map((f) =>
      `<label>${esc(f.label)}<input name="${f.key}" type="${f.secret ? "password" : "text"}" placeholder="${esc(f.placeholder || "")}" value="${esc(settings[f.key] || "")}"></label>`).join("");
  };
  sel.onchange = draw; draw();
  dlg.showModal();
}
$("#settings form").addEventListener("submit", (e) => {
  if (e.submitter?.value !== "save") return;
  const fd = new FormData(e.target);
  settings = { source: fd.get("source") };
  for (const f of PROVIDERS[settings.source].fields) settings[f.key] = fd.get(f.key) || "";
  saveSettings(); refresh();
});

if (KIOSK) document.body.classList.add("kiosk");
$("#open-settings").addEventListener("click", openSettings);
refresh();
setInterval(refresh, REFRESH_MS);
setInterval(() => { const u = $("#updated"); if (u.dataset.t) u.textContent = `Updated ${ago(u.dataset.t)}`; }, 15_000);
if ("serviceWorker" in navigator && !KIOSK) navigator.serviceWorker.register("sw.js").catch(() => {});
