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
  // name each problem in the banner, with its details and a link to its tile
  const flagged = (snap.groups || []).flatMap((g) => g.items.filter((i) => i.status === "down" || i.status === "warn")
    .map((i) => ({ ...i, group: g.name })));
  const list = $("#banner-list");
  list.innerHTML = flagged.slice(0, 8).map((i) =>
    `<li class="${i.status}"><a href="#tile-${esc(i.id)}"><b>${esc(i.name)}</b></a>${i.note || i.message ? `: ${esc(i.note || i.message)}` : ` (${esc(i.group)})`}${i.hint ? `<br><span class="hint">${esc(i.hint)}</span>` : ""}</li>`).join("");
  list.hidden = !flagged.length;

  const groups = $("#groups");
  groups.innerHTML = "";
  for (const g of snap.groups || []) {
    const sec = document.createElement("section");
    const gBad = g.items.filter((i) => i.status === "down").length;
    const gLook = g.items.filter((i) => i.status === "warn").length;
    sec.className = "group";
    const firstFlag = g.items.find((i) => i.status === "down") || g.items.find((i) => i.status === "warn");
    const pills = `${gBad ? `<span class="pill down">${gBad} down</span>` : ""}${gLook ? `<span class="pill warn">${gLook} to look at</span>` : ""}`;
    sec.innerHTML = `<h2>${esc(g.name)} ${firstFlag && pills ? `<a class="pill-link" href="#tile-${esc(firstFlag.id)}">${pills}</a>` : ""}</h2><div class="tiles"></div>`;
    const tiles = $(".tiles", sec);
    // problems first, then things to look at, then original order
    const rank = (s) => (s === "down" ? 0 : s === "warn" ? 1 : 2);
    const items = [...g.items].sort((a, b) => rank(a.status) - rank(b.status));
    for (const i of items) {
      const t = document.createElement("article");
      t.className = `tile ${i.status}${i.note ? " has-note" : ""}`;
      t.id = `tile-${i.id}`;
      t.title = i.message || "";
      const bars = (i.history || []).slice(-30).map((h) => `<i class="${h.s}"></i>`).join("");
      const meta = `${LABEL[i.status] || i.status}${i.status === "down" && i.since ? ` · since ${ago(i.since)}` : ""}${i.latency_ms != null && i.status === "up" ? ` · ${Math.round(i.latency_ms)} ms` : ""}`;
      t.innerHTML = `
        <div class="dot"></div>
        <div class="body">
          <div class="name">${esc(i.name)}</div>
          <div class="meta">${i.note ? esc(i.note) : meta}</div>
          ${i.hint && i.status !== "up" ? `<div class="hint">What to do: ${esc(i.hint)}</div>` : ""}
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

  // Internet uplink (cellular / fixed-wireless gateways that publish link quality)
  if (x.uplink) {
    const u = x.uplink;
    const rows = (u.cells || []).map((c) => [c.tech, esc((c.bands || []).join(", ")), c.bars ?? "",
      c.rsrp ?? "", c.rsrq ?? "", c.sinr ?? "", c.quality === "weak" ? `<span class="weak">weak</span>` : esc(c.quality || "")]);
    const days = u.uptime_s != null ? `${(u.uptime_s / 86400).toFixed(1)} days` : "?";
    panels.push(["uplink", `Internet uplink (${esc(u.provider || u.kind)})`,
      `<ul class="facts"><li><b>Gateway:</b> ${esc(u.model || "?")}, firmware ${esc(u.firmware || "?")}, up ${days}</li></ul>
       ${table(["Tech", "Band", "Bars", "RSRP (dBm)", "RSRQ (dB)", "SINR (dB)", "Quality"], rows)}
       <p class="help">RSRP is signal strength (above −100 good, below −115 weak). SINR is signal over noise (above 10 good, below 0 weak).</p>`]);
  }

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
    const dbm = (v) => (v != null ? `<span class="${v < WEAK_RSSI ? "weak" : ""}">${Math.round(v)} dBm</span>` : "");
    const sec = (s) => (/Open|old/.test(s || "") ? `<span class="weak">${esc(s)}</span>` : esc(s || ""));
    const ssidRows = (a.ssids || []).map((s) => [s.own ? "★" : "", esc(s.ssid), s.access_points,
      s.bands.map((b) => `${b} GHz`).join(", "), esc(s.channels.join("  ")), dbm(s.strongest_dbm), s.security.map(sec).join(", ")]);
    const lvl = { clear: "clear", light: "light", moderate: `<span class="chip warn">moderate</span>`, heavy: `<span class="weak">heavy</span>` };
    const chanRows = (a.channels || []).map((c) => [`${c.band} GHz`, c.channel, c.yours ? "★" : "", c.networks,
      c.band === "2.4" ? c.overlapping : "–", dbm(c.strongest_dbm), c.utilisation_pct != null ? `${c.utilisation_pct}%` : "", lvl[c.congestion] || c.congestion]);
    const apRows = (a.networks || []).map((n) => [n.own ? "★" : "", esc(n.ssid || (n.own_hidden ? "(hidden, yours)" : "(hidden)")), esc(n.bssid),
      `${n.band} GHz`, n.channel, n.width_mhz ? `${n.width_mhz} MHz` : "", dbm(n.signal_dbm),
      n.utilisation_pct != null ? `${n.utilisation_pct}%` : "", n.stations ?? "", sec(n.security)]);
    const can6 = (a.adapter_bands || []).includes("6");
    const msgs = [...(a.problems || []).map((p) => `<li class="weak">${esc(p)}</li>`), ...(a.notes || []).map((p) => `<li>${esc(p)}</li>`)].join("");
    panels.push(["airspace", `Wi-Fi airspace (${(a.ssids || []).length} network names, ${(a.networks || []).length} access points heard)`,
      `<p class="help">Heard by this box's Wi-Fi adapter (${esc(a.interface)}, ${esc((a.adapter_bands || []).join(" / "))} GHz${can6 ? "" : "; it can't hear 6 GHz"}).
         ★ = your network${a.own_ssids?.length ? ` (${esc(a.own_ssids.join(", "))})` : ""}.${a.hidden_count ? ` ${a.hidden_count} hidden network(s) not listed by name.` : ""}</p>
       ${msgs ? `<ul class="facts">${msgs}</ul>` : ""}
       <h4>Network names</h4>${table(["", "Name", "Access points", "Bands", "Channels", "Strongest", "Security"], ssidRows)}
       <h4>Channel congestion</h4>
       <p class="help">2.4 GHz channels overlap their neighbours up to 4 channels away; only 1, 6 and 11 don't overlap each other. Airtime % is reported by some access points; otherwise congestion is estimated from the networks heard.</p>
       ${table(["Band", "Channel", "Yours", "Networks", "Overlapping", "Strongest", "Airtime used", "Congestion"], chanRows)}
       <h4>Every access point</h4>${table(["", "Network", "Access point", "Band", "Ch", "Width", "Signal", "Airtime", "Clients", "Security"], apRows)}`]);
  }

  // Internet speed history (Speedtest Tracker)
  if (x.speed && (x.speed.history || []).length) {
    const h = x.speed.history.filter((r) => r.ok && r.down_mbps);
    const max = Math.max(1, ...h.map((r) => r.down_mbps), x.speed.median_down_mbps || 0);
    const w = 600, ht = 120, bw = h.length ? w / h.length : w;
    const bars = h.map((r, i) => {
      const y = ht - (r.down_mbps / max) * (ht - 4);
      return `<rect x="${(i * bw + 1).toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${(ht - y).toFixed(1)}" rx="2"><title>${esc(r.at)}: ${r.down_mbps} down / ${r.up_mbps ?? "?"} up Mbps, ${Math.round(r.ping_ms ?? 0)} ms</title></rect>`;
    }).join("");
    const med = x.speed.median_down_mbps;
    const line = med ? `<line x1="0" x2="${w}" y1="${(ht - (med / max) * (ht - 4)).toFixed(1)}" y2="${(ht - (med / max) * (ht - 4)).toFixed(1)}" class="median"/>` : "";
    const rows = [...x.speed.history].reverse().slice(0, 10).map((r) => [esc(ago(r.at)), r.ok ? r.down_mbps : `<span class="weak">failed</span>`,
      r.up_mbps ?? "", r.ping_ms != null ? Math.round(r.ping_ms) : "", r.jitter_ms != null ? Math.round(r.jitter_ms) : "", esc(r.server || "")]);
    panels.push(["speed", "Internet speed (last 14 days)",
      `<svg class="speedchart" viewBox="0 0 ${w} ${ht}" preserveAspectRatio="none" role="img" aria-label="Download speed history">${bars}${line}</svg>
       <p class="help">Download speed per test${med ? `; the line is this site's normal (${Math.round(med)} Mbps)` : ""}. Hover a bar for details.</p>
       ${table(["When", "Down (Mbps)", "Up (Mbps)", "Ping (ms)", "Jitter (ms)", "Server"], rows)}`]);
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

// ---------------------------------------------------------------- router setup (box only)
// The box serves a small settings API at /api. On GitHub Pages or any other host there is none,
// so this section simply stays hidden.
let BOX = null;
let SESSION = null;                  // {admin_set, signed_in} when this page is served by a box
const FROM_KIOSK = params.get("from") === "kiosk";

async function api(path, opts = {}) {
  const r = await fetch(path, { cache: "no-store", credentials: "same-origin", ...opts,
    headers: opts.body ? { "Content-Type": "application/json" } : undefined });
  if (!(r.headers.get("content-type") || "").includes("json")) throw new Error("no settings API here");
  return [r, await r.json()];
}

async function loadBoxSettings() {
  try {
    const [, s] = await api("api/session");
    SESSION = s;
  } catch { SESSION = null; }               // GitHub Pages or another host: no admin features
  // wall screen: only a quiet sign-in link
  $("#admin-link").hidden = !(KIOSK && SESSION);
  if (KIOSK || !SESSION) { $("#setup").hidden = true; $("#admin-bar").hidden = true; return; }
  renderAdminBar();
  if (SESSION.signed_in) {
    try {
      const [r, j] = await api("api/settings");
      if (r.ok) { BOX = j; renderSetup(); }
    } catch { /* ignore */ }
  } else {
    BOX = null;
    $("#setup").hidden = true;
    if (params.has("admin") && !$("#signin-dialog").open && !loadBoxSettings.asked) {
      loadBoxSettings.asked = true;
      openSignIn();
    }
  }
}

function renderAdminBar() {
  const bar = $("#admin-bar");
  const back = FROM_KIOSK ? `<a class="button ghost" href="./?kiosk">Back to wall screen</a>` : "";
  bar.innerHTML = SESSION.signed_in
    ? `<span class="who">Administrator</span> <button id="signout" class="ghost">Sign out</button> ${back}`
    : `<button id="signin" class="ghost">Administrator sign-in</button> ${back}`;
  bar.hidden = false;
  $("#signout")?.addEventListener("click", async () => {
    await api("api/logout", { method: "POST", body: "{}" });
    if (FROM_KIOSK) location.href = "./?kiosk"; else loadBoxSettings();
  });
  $("#signin")?.addEventListener("click", openSignIn);
}

function openSignIn() {
  const first = !SESSION.admin_set;
  $("#si-title").textContent = first ? "Create the administrator password" : "Administrator sign-in";
  $("#si-help").textContent = first
    ? "No administrator password has been set on this box yet. Choose one (at least 8 characters). It is needed to change settings such as the router connection. Only a scrambled (hashed) copy is stored on the box."
    : "Sign in to change settings such as the router connection.";
  $("#si-confirm-wrap").hidden = !first;
  $("#si-go").textContent = first ? "Create and sign in" : "Sign in";
  $("#si-pass").value = $("#si-confirm").value = "";
  $("#si-pass").autocomplete = first ? "new-password" : "current-password";
  $("#si-result").hidden = true;
  $("#signin-dialog").showModal();
  $("#si-pass").focus();
}

$("#signin-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const first = !SESSION.admin_set, pw = $("#si-pass").value, out = $("#si-result");
  const say = (m) => { out.hidden = false; out.className = "result bad"; out.textContent = m; };
  if (first && pw !== $("#si-confirm").value) return say("The two passwords don't match.");
  if (first && pw.length < 8) return say("Use at least 8 characters.");
  try {
    const [r, j] = await api(first ? "api/admin/setup" : "api/login", { method: "POST", body: JSON.stringify({ password: pw }) });
    if (!r.ok) return say(j.error || `Failed (HTTP ${r.status})`);
    $("#signin-dialog").close();
    await loadBoxSettings();
  } catch (err) { say(`Couldn't reach the box: ${err.message}`); }
});

// Opened from the wall screen: go back there after 10 minutes without use, so it's never left signed in.
if (FROM_KIOSK) {
  let idle;
  const arm = () => { clearTimeout(idle); idle = setTimeout(async () => {
    try { await api("api/logout", { method: "POST", body: "{}" }); } catch {}
    location.href = "./?kiosk";
  }, 10 * 60_000); };
  ["pointerdown", "keydown", "scroll"].forEach((ev) => addEventListener(ev, arm, { passive: true }));
  arm();
}

function renderSetup() {
  const box = $("#setup");
  const st = BOX?.router;
  if (!BOX) { box.hidden = true; return; }
  const label = (id) => (BOX.router_types.find((t) => t.id === id) || {}).label || id;
  let cls, html;
  if (!st.type || st.type === "none") {
    cls = "invite";
    html = `<div><b>See how every device is connected</b><br><span class="help">Connect your router to show which devices use a cable, 2.4 GHz or 5 GHz, which access point they use and how strong their signal is.</span></div>
            <button id="setup-open">Connect your router</button>`;
  } else if (!st.has_password || st.error) {
    cls = "problem";
    html = `<div><b>Router connection needs attention</b><br><span class="help">${esc(label(st.type))}: ${esc(st.has_password ? st.error : "the password hasn't been entered yet")}</span></div>
            <button id="setup-open">Fix the connection</button>`;
  } else {
    cls = "ok";
    html = `<div><b>Router connected</b> <span class="help">${esc(label(st.type))} at ${esc(st.url)}</span></div>
            <button id="setup-open" class="ghost">Change</button>`;
  }
  box.className = `setup ${cls}`;
  box.innerHTML = html;
  box.hidden = false;
  $("#setup-open").onclick = openRouterDialog;
}

function openRouterDialog() {
  const d = $("#router-dialog"), st = BOX.router, types = BOX.router_types;
  const sel = $("#r-type");
  sel.innerHTML = `<option value="">Choose…</option>` + types.map((t) => `<option value="${t.id}">${esc(t.label)}</option>`).join("");
  const fill = (keepUser) => {
    const t = types.find((x) => x.id === sel.value);
    $("#r-help").textContent = t ? t.help : "Pick your router's brand or type. More can be added; see docs/network-checks.md.";
    if (t && (!keepUser || !$("#r-url").value)) $("#r-url").value = t.url;
    if (t && (!keepUser || !$("#r-user").value)) $("#r-user").value = t.user;
  };
  sel.value = st.type && st.type !== "none" ? st.type : "";
  $("#r-url").value = st.url || "";
  $("#r-user").value = st.user || "";
  fill(true);
  sel.onchange = () => fill(false);
  $("#r-pass").value = "";
  $("#r-pass").type = "password";
  $("#r-pass").placeholder = st.has_password ? "Saved. Leave empty to keep it" : "";
  $("#r-remove").hidden = !st.type || st.type === "none";
  $("#r-result").hidden = true;
  $("#r-show").onclick = () => { const p = $("#r-pass"); p.type = p.type === "password" ? "text" : "password"; $("#r-show").textContent = p.type === "password" ? "Show" : "Hide"; };
  $("#r-test").onclick = () => routerAction("test");
  $("#r-save").onclick = () => routerAction("save");
  $("#r-remove").onclick = () => routerAction("remove");
  d.showModal();
}

async function routerAction(action) {
  const out = $("#r-result");
  const body = { action, type: $("#r-type").value, url: $("#r-url").value.trim(), user: $("#r-user").value.trim(),
                 password: $("#r-pass").value };
  if (action !== "remove" && (!body.type || !body.url)) { show("Choose the router type and enter its address.", false); return; }
  for (const b of $("#router-form").querySelectorAll("button")) b.disabled = true;
  show(action === "remove" ? "Disconnecting…" : "Connecting to the router…", null);
  try {
    const r = await fetch("api/router", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok || j.ok === false) { show(j.error || `Failed (HTTP ${r.status})`, false); return; }
    if (action === "remove") { show("Disconnected. The saved login was removed.", true); }
    else {
      const bands = Object.entries(j.client_bands || {}).map(([k, v]) => `${v} ${k}`).join(", ");
      show(`${action === "save" ? "Saved and connected" : "It works"}: ${j.clients} devices${bands ? ` (${bands})` : ""}` +
           `${j.nodes > 1 ? `, ${j.nodes} access points` : ""}.${action === "test" ? " Press Save and connect to keep it." : " The dashboard updates in a few seconds."}`, true);
      if (action === "save") $("#r-pass").value = "";
    }
    await loadBoxSettings();
    if (action !== "test") setTimeout(refresh, 3000);
  } catch (e) {
    show(`Couldn't reach the box: ${e.message}`, false);
  } finally {
    for (const b of $("#router-form").querySelectorAll("button")) b.disabled = false;
  }
  function show(msg, ok) { out.hidden = false; out.textContent = msg; out.className = `result ${ok === true ? "good" : ok === false ? "bad" : ""}`; }
}

loadBoxSettings();
setInterval(loadBoxSettings, 60_000);
