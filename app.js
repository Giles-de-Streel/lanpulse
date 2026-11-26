import { PROVIDERS } from "./providers.js";

const $ = (s, el = document) => el.querySelector(s);
const params = new URLSearchParams(location.search);
const KIOSK = params.has("kiosk");
const VIEW = params.get("view");          // "devices" | "wifi": full-page detail views
const FROM_KIOSK_VIEW = VIEW && params.get("from") === "kiosk";
// tiles that open a detail page
const TILE_VIEWS = { "net-devices": ["devices", "See every device"], "net-weak-wifi": ["wifi", "See every device's signal"] };
function viewHref(view) {
  const p = new URLSearchParams(location.search);
  ["kiosk", "admin", "from", "r", "view"].forEach((k) => p.delete(k));
  p.set("view", view);
  if (KIOSK) p.set("from", "kiosk");
  return `./?${p}`;
}
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

// ------------------------------------------------------------- details pop-up for any tile or alert
const ITEMS = new Map();   // tile id -> item (with its group), refreshed on every render
const hasDetails = (i) => i.status === "down" || i.status === "warn" || (i.details && i.details.length);
function openItem(id) {
  const i = ITEMS.get(id);
  if (!i) return;
  const gen = LAST_SNAP?.generated;
  const rows = [
    ["Status", `<span class="chip ${i.status === "down" ? "bad" : i.status === "warn" ? "warn" : ""}">${esc(LABEL[i.status] || i.status)}</span>${i.status === "down" && i.since ? ` since ${esc(ago(i.since))}` : ""}`],
    ["Group", esc(i.group || "")],
    ...(i.latency_ms != null ? [["Response time", `${Math.round(i.latency_ms)} ms`]] : []),
    ...(i.uptime_24h != null ? [["Uptime, last 24 h", pct(i.uptime_24h)]] : []),
    ...(i.target ? [["Checks", esc(i.target)]] : []),
    ...(i.link ? [["Connection", linkText(i.link)]] : []),
    ...(i.details || []).map((d) => [esc(d.label), esc(d.value)]),
    ...(gen ? [["Last updated", esc(ago(gen))]] : []),
  ];
  const bars = (i.history || []).slice(-60).map((h) => `<i class="${h.s}" title="${esc(h.t || "")}"></i>`).join("");
  $("#item-body").innerHTML = `<h2>${esc(i.name)}</h2>
    ${i.message || i.note ? `<p class="item-msg">${esc(i.message || i.note)}</p>` : ""}
    ${i.hint ? `<p class="item-hint"><b>What to do:</b> ${esc(i.hint)}</p>` : ""}
    <table class="item-table"><tbody>${rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</tbody></table>
    ${bars ? `<p class="help">Recent checks, oldest first</p><div class="bars item-bars">${bars}</div>` : ""}
    ${TILE_VIEWS[i.id] ? `<p><a href="${viewHref(TILE_VIEWS[i.id][0])}"${KIOSK ? "" : ` target="_blank" rel="noopener"`}>${TILE_VIEWS[i.id][1]} ↗</a></p>` : ""}`;
  const d = $("#item-dialog");
  if (!d.open) d.showModal();
  clearTimeout(openItem.t);
  if (KIOSK) openItem.t = setTimeout(() => d.open && d.close(), 120_000);   // wall screen: don't stay open
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-item]");
  if (b) { e.preventDefault(); openItem(b.dataset.item); }
});

function recount(groups) {
  const s = { total: 0, up: 0, down: 0, warn: 0, note: 0, pending: 0, maintenance: 0, unknown: 0 };
  for (const g of groups) for (const i of g.items) {
    s.total++;
    const k = i.status === "warn" && i.priority === "low" ? "note" : i.status;
    s[k] = (s[k] || 0) + 1;
  }
  return s;
}

// ---------------------------------------------------------------- clock (in the status banner)
// In the status banner, on the right. // Shows this PC's own date and time, ticking, and whether it agrees with internet time.
// Browsers can't use NTP (UDP), so the box queries public NTP servers (extras.time) and the page
// compares the PC with the box using the server's Date header (1-second resolution, so differences
// under ~2 s count as "matches").
const CLOCK = { pcVsServer: null, measuredAt: 0, timer: null, el: null };
async function measurePcOffset() {
  if (Date.now() - CLOCK.measuredAt < 5 * 60_000 && CLOCK.pcVsServer != null) return;
  try {
    const t0 = Date.now();
    const r = await fetch(`./?clock=${t0}`, { method: "HEAD", cache: "no-store" });
    const t1 = Date.now(), d = Date.parse(r.headers.get("date"));
    if (!isNaN(d)) { CLOCK.pcVsServer = d + 500 - (t0 + t1) / 2; CLOCK.measuredAt = t1; }   // server time - PC time, ms
  } catch { /* offline: just show the PC clock */ }
}
function renderClock(x) {
  let el = CLOCK.el;   // one element and one timer
  if (!el) {
    el = CLOCK.el = document.createElement("div");
    el.id = "clock";
    el.className = "clock";
    el.innerHTML = `<div class="ctime"><b id="c-time"></b><span id="c-date"></span></div><div id="c-sync" class="csync"></div><div id="c-ntp" class="cntp"></div>`;
    $("#banner").append(el);
    const tick = () => {
      const n = new Date();
      el.querySelector("#c-time").textContent = n.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" });
      el.querySelector("#c-date").textContent = n.toLocaleDateString(undefined, { weekday: "short", year: "numeric", month: "short", day: "numeric" });
    };
    tick();
    CLOCK.timer = setInterval(tick, 1000);
  }
  if (el.parentElement !== $("#banner")) $("#banner").append(el);
  const t = x.time;
  measurePcOffset().then(() => {
    const sync = $("#c-sync");
    const box = t?.box_offset_ms;                       // + means the box is behind internet time
    if (CLOCK.pcVsServer == null) { sync.textContent = "This PC's clock"; sync.className = "csync"; }
    else {
      // PC vs internet time = (server - PC) + (internet - server box), when the page comes from the box
      const fromBox = location.port === "8080" || !onPages;
      const off = (CLOCK.pcVsServer + (fromBox && box != null ? box : 0)) / 1000;   // + means the PC is behind
      const a = Math.abs(off);
      sync.className = "csync " + (a < 2 ? "ok" : a < 30 ? "warn" : "bad");
      sync.textContent = a < 2 ? "This PC's clock · matches internet time"
        : `This PC's clock is ${a < 90 ? `${Math.round(a)} s` : fmtDur(a)} ${off > 0 ? "slow" : "fast"}`;
    }
    const ntp = $("#c-ntp");
    if (!t?.servers?.length) { ntp.innerHTML = ""; return; }
    const okN = t.servers.filter((s) => s.ok).length;
    ntp.innerHTML = `Internet time (NTP): ` + t.servers.map((s) =>
      `<span class="${s.ok ? "ok" : "bad"}" title="${esc(s.ok ? `answered in ${Math.round(s.rtt_ms)} ms; box clock ${s.offset_ms >= 0 ? "behind" : "ahead"} by ${Math.abs(s.offset_ms)} ms; stratum ${s.stratum}` : `no answer: ${s.error || ""}`)}">${esc(s.host)} ${s.ok ? "✓" : "✗"}</span>`).join(" · ") +
      (box != null ? ` <span class="help" title="checked ${esc(ago(t.at))}">· box clock ${Math.abs(box) < 500 ? "in sync" : `off by ${(Math.abs(box) / 1000).toFixed(1)} s`}${okN < t.servers.length ? ` · ${t.servers.length - okN} not answering` : ""}</span>` : "");
  });
}

function renderQuick(items, x) {
  const el = $("#quick");
  const label = (i) => {
    if (i.id === "net-devices" && x.devices) {
      const on = x.devices.filter((d) => d.online).length, nw = x.devices.filter((d) => d.new).length;
      return `${on} online${nw ? ` · ${nw} new` : ""}`;
    }
    if (i.id === "net-weak-wifi" && i.missing) return "sign in to the router to see";
    if (i.id === "net-weak-wifi" && x.clients) {
      const s = x.clients.filter((c) => c.link?.type === "wifi" && c.link.rssi != null).map((c) => c.link.rssi);
      const poor = s.filter((r) => r < -80).length, weak = s.filter((r) => r >= -80 && r < -70).length;
      return poor || weak ? [poor && `${poor} poor`, weak && `${weak} weak`].filter(Boolean).join(" · ") : `${s.length} devices, all fine`;
    }
    return "";
  };
  const names = { "net-devices": "Devices", "net-weak-wifi": "Wi-Fi signal" };
  el.innerHTML = items.map((i) => {
    const sub = label(i);
    return `<a class="qbtn${i.missing ? " needs" : ""}" href="${viewHref(TILE_VIEWS[i.id][0])}"${KIOSK ? "" : ` target="_blank" rel="noopener"`} title="${esc(i.note || i.message || "")}">
      <b>${esc(names[i.id] || i.name)}</b>${sub ? `<span>${esc(sub)}</span>` : ""}<i aria-hidden="true">↗</i></a>`;
  }).join("");
  el.querySelectorAll(".qbtn.needs").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); startRouterSignin(); }));
  if (x.history) {
    const n = historyCount(x, 1);
    el.insertAdjacentHTML("beforeend", `<a class="qbtn" href="${viewHref("history")}"${KIOSK ? "" : ` target="_blank" rel="noopener"`}>
      <b>History</b><span>${n ? `${n} outage${n === 1 ? "" : "s"} in 24 h` : "no outages in 24 h"}</span><i aria-hidden="true">↗</i></a>`);
  }

  el.hidden = !el.children.length;
}

function render(snap) {
  if (VIEW) { LAST_SNAP = snap; return renderView(snap); }
  LAST_SNAP = snap;
  ITEMS.clear();
  // Devices and Wi-Fi signal change constantly, so they aren't shown as issues: they become quick-link
  // buttons under the banner and are left out of the tiles and the counts.
  const quick = [];
  snap = { ...snap, groups: (snap.groups || []).map((g) => ({ ...g, items: g.items.filter((i) => {
    if (TILE_VIEWS[i.id]) { quick.push(i); return false; }
    return true;
  }) })).filter((g) => g.items.length) };
  snap.summary = recount(snap.groups);
  // the Wi-Fi signal button is always there; without router data its page says how to get it
  if (!quick.some((i) => i.id === "net-weak-wifi"))
    quick.push({ id: "net-weak-wifi", name: "Wi-Fi signal of devices", note: "Sign in to the router in the admin view to see each device's signal", missing: true });
  // the device list is a column on the right of the main page; its full-page view stays one click away
  const sideShown = renderSide(snap.extras || {});
  renderQuick(sideShown ? quick.filter((i) => i.id !== "net-devices") : quick, snap.extras || {});
  renderClock(snap.extras || {});
  const site = snap.site || {};
  document.title = site.name || "lanpulse";
  $("#site-name").textContent = site.name || "Network status";
  $("#site-desc").textContent = site.description || "";
  renderBoxIps((snap.extras || {}).box);

  const sum = snap.summary || {};
  const isNote = (i) => i.status === "warn" && i.priority === "low";
  const bad = (sum.down || 0);
  const look = (sum.warn || 0);
  const notes = (snap.groups || []).flatMap((g) => g.items.filter(isNote));
  const warn = (sum.pending || 0) + (sum.unknown || 0);
  // first scans after a boot or a move to another network: say so instead of "everything is working"
  const checking = snap.checking || [];
  const banner = $("#banner");
  banner.className = "banner " + (bad ? "down" : checking.length ? "checking" : look || warn ? "pending" : "up");
  $("#banner-title").textContent = bad
    ? `${bad} ${bad === 1 ? "thing needs" : "things need"} attention`
    : checking.length ? "Loading – checking the network…"
    : look ? `Working: ${look} ${look === 1 ? "thing" : "things"} to look at when convenient`
    : warn ? "Checking a few things…" : "Everything is working";
  $("#banner-sub").innerHTML = checking.length && !bad
    ? esc(`Still running: ${checking.join(", ")}. Usually done within a few minutes of starting up.`)
    : esc(`${(sum.up || 0) + notes.length} of ${sum.total || 0} working` + (look && bad ? ` · ${look} to look at` : "")) +
    (notes.length ? ` · <a href="#tile-${esc(notes[0].id)}">${notes.length} ${notes.length === 1 ? "note" : "notes"} further down</a> <span class="help">(${esc(notes.map((n) => n.name).join(", "))})</span>` : "");
  // name each real problem in the banner (not the lower-priority notes), with details and a link to its tile
  const flagged = (snap.groups || []).flatMap((g) => g.items.filter((i) => (i.status === "down" || i.status === "warn") && !isNote(i))
    .map((i) => ({ ...i, group: g.name })));
  const list = $("#banner-list");
  list.innerHTML = flagged.slice(0, 8).map((i) =>
    `<li class="${i.status}"><a href="#tile-${esc(i.id)}"><b>${esc(i.name)}</b></a>${i.note || i.message ? `: ${esc(i.note || i.message)}` : ` (${esc(i.group)})`} <button type="button" class="ghost small more-btn" data-item="${esc(i.id)}">Details…</button>${i.hint ? `<br><span class="hint">${esc(i.hint)}</span>` : ""}</li>`).join("");
  list.hidden = !flagged.length;

  const groups = $("#groups");
  groups.innerHTML = "";
  // wall screen: groups with problems move to the top (down first, then things to look at)
  const gRank = (g) => (g.items.some((i) => i.status === "down") ? 0
    : g.items.some((i) => i.status === "warn" && !isNote(i)) ? 1 : 2);
  const ordered = KIOSK ? [...(snap.groups || [])].map((g, n) => [g, n]).sort((a, b) => gRank(a[0]) - gRank(b[0]) || a[1] - b[1]).map((x) => x[0])
                        : (snap.groups || []);
  for (const g of ordered) {
    const sec = document.createElement("section");
    const gBad = g.items.filter((i) => i.status === "down").length;
    const gLook = g.items.filter((i) => i.status === "warn" && !isNote(i)).length;
    const gNote = g.items.filter(isNote).length;
    sec.className = "group";
    // "healthy" groups can collapse to one line on the wall screen; notes don't stop that
    const okCount = g.items.filter((i) => i.status === "up" || i.status === "maintenance" || isNote(i)).length;
    sec.dataset.healthy = !gBad && !gLook && okCount === g.items.length ? "1" : "";
    sec.dataset.summary = `${g.name}: ${g.items.length === 1 ? "working" : `all ${g.items.length} working`}${gNote ? ` · ${gNote} ${gNote === 1 ? "note" : "notes"}` : ""}`;
    const firstFlag = g.items.find((i) => i.status === "down") || g.items.find((i) => i.status === "warn" && !isNote(i)) || g.items.find(isNote);
    const pills = `${gBad ? `<span class="pill down">${gBad} down</span>` : ""}${gLook ? `<span class="pill warn">${gLook} to look at</span>` : ""}${gNote ? `<span class="pill note">${gNote} ${gNote === 1 ? "note" : "notes"}</span>` : ""}`;
    sec.innerHTML = `<h2>${esc(g.name)} ${firstFlag && pills ? `<a class="pill-link" href="#tile-${esc(firstFlag.id)}">${pills}</a>` : ""}</h2><div class="tiles${g.items.length === 3 ? " n3" : ""}"></div>`;
    const tiles = $(".tiles", sec);
    // problems first, then things to look at, then working, then lower-priority notes
    const rank = (i) => (i.status === "down" ? 0 : isNote(i) ? 3 : i.status === "warn" ? 1 : 2);
    const items = [...g.items].sort((a, b) => rank(a) - rank(b));
    for (const i of items) {
      const t = document.createElement("article");
      t.className = `tile ${i.status}${i.note ? " has-note" : ""}${isNote(i) ? " low" : ""}`;
      t.id = `tile-${i.id}`;
      t.title = i.message || "";
      ITEMS.set(i.id, { ...i, group: g.name });
      const bars = (i.history || []).slice(-30).map((h) => `<i class="${h.s}"></i>`).join("");
      const meta = `${LABEL[i.status] || i.status}${i.status === "down" && i.since ? ` · since ${ago(i.since)}` : ""}${i.latency_ms != null && i.status === "up" ? ` · ${Math.round(i.latency_ms)} ms` : ""}`;
      t.innerHTML = `
        <div class="dot"></div>
        <div class="body">
          <div class="name">${esc(i.name)}</div>
          <div class="meta">${i.note ? esc(i.note) : meta}</div>
          ${i.hint && i.status !== "up" ? `<div class="hint">What to do: ${esc(i.hint)}</div>` : ""}
          ${TILE_VIEWS[i.id] ? `<a class="more" href="${viewHref(TILE_VIEWS[i.id][0])}"${KIOSK ? "" : ` target="_blank" rel="noopener"`}>${TILE_VIEWS[i.id][1]} ↗</a>` : ""}
          ${i.id === "router-signin" ? `<button type="button" class="small tile-action" data-router-signin>Sign in to the router</button>` : ""}
          ${i.link && !i.note ? `<div class="link">${linkText(i.link)}</div>` : ""}
          ${bars ? `<div class="bars">${bars}</div>` : ""}
          ${hasDetails(i) ? `<button type="button" class="ghost small more-btn" data-item="${esc(i.id)}">Details…</button>` : ""}
        </div>
        <div class="uptime">${pct(i.uptime_24h)}<small>${i.uptime_24h != null ? "24 h" : ""}</small></div>`;
      tiles.append(t);
    }
    groups.append(sec);
  }
  document.querySelectorAll("[data-router-signin]").forEach((b) => (b.onclick = startRouterSignin));
  renderDetails(snap.extras || {});
  const net = (snap.extras || {}).network;
  $("#net-info").textContent = net ? `On ${net.name}${net.kind ? ` · ${net.kind === "Wi-Fi" ? `Wi-Fi${net.ssid ? ` ${net.ssid}` : ""}` : "cable"}` : ""}` : "";
  if (KIOSK) requestAnimationFrame(fitKiosk);

  const gen = snap.generated ? new Date(snap.generated) : null;
  const stale = !gen || Date.now() - gen.getTime() > STALE_MS;
  // sources still starting after a reboot (collector sets "starting" for its first 3 minutes) aren't faults
  const failed = (snap.sources || []).filter((s) => !s.ok && !s.starting);
  const starting = (snap.sources || []).some((s) => !s.ok && s.starting);
  $("#updated").dataset.s = starting ? " · starting up" : "";
  $("#updated").textContent = gen ? `Updated ${ago(snap.generated)}${$("#updated").dataset.s}` : "";
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

// ---------------------------------------------------------------- wall screen: fit to one screen
// The wall screen can't be scrolled by anyone, so everything must fit. Try progressively denser
// layouts and stop at the first that fits: full tiles → compact tiles → healthy groups collapsed
// to one line → mini tiles. If even that overflows, split the groups into pages and rotate.
const KIOSK_PAGE_MS = 15_000;
const kioskState = { page: 0, timer: null, pages: [] };

// Per-device traffic, where the router reports it. UniFi: live bits per second each way. ASUS: only how
// fast its running totals grew since the last poll, in unknown units, so it's shown as a share of the busiest.
const usageVal = (d) => (d.usage ? (d.usage.rate_bps ?? d.usage.rate ?? 0) : null);
const maxUsage = (list) => Math.max(0, ...list.map((d) => usageVal(d) || 0));
const fmtBps = (b) => (b >= 1e6 ? `${(b / 1e6).toFixed(b >= 1e7 ? 0 : 1)} Mbps` : b >= 1e3 ? `${Math.round(b / 1e3)} kbps` : `${Math.round(b)} bps`);
function usageHtml(u, top) {
  if (!u) return "";
  const v = u.rate_bps ?? u.rate ?? 0, w = top ? Math.max(2, Math.round((100 * v) / top)) : 0;
  const text = u.rate_bps != null ? (u.rate_bps < 1000 ? "idle" : `↓ ${fmtBps(u.down_bps)} · ↑ ${fmtBps(u.up_bps)}`)
    : v ? `${Math.round((100 * v) / (top || v))}% of the busiest` : "idle";
  return `<span class="ubar" title="${esc(u.basis || "")}"><i style="width:${v ? w : 0}%"></i></span> <span class="utext">${esc(text)}</span>`;
}

// Unnamed devices: the box's best guess (identify.py) in italics, plus the end of the MAC address so
// several alike devices can be told apart. Hover shows the clues.
const macTail = (d) => (d.mac ? `…${d.mac.slice(-5)}` : "");
const clueText = (d) => [d.vendor, d.mac, ...(d.clues || [])].filter(Boolean).join("\n");
function devLabel(d) {
  if (d.name) return esc(d.name);
  if (d.hint) return `<span class="guess">Probably ${esc(d.hint)}</span> <span class="help mono">${esc(macTail(d))}</span>`;
  return `${esc(d.vendor && d.vendor !== "Private" ? d.vendor : "Unknown device")} <span class="help mono">${esc(macTail(d))}</span>`;
}

// Header line: the monitoring box's own name and addresses (main connection first)
function renderBoxIps(box) {
  const el = $("#box-ips");
  const ips = (box && box.ips) || [];
  el.hidden = !ips.length;
  if (!ips.length) return;
  el.innerHTML = `Monitoring box <b>${esc(box.hostname || "")}</b> · ` + ips.map((x) =>
    `<span class="mono" title="${esc(x.dev)}${x.main ? " · main connection" : " · backup"}">${esc(x.ip)}</span> ` +
    `<span class="help">(${esc(x.kind)}${ips.length > 1 && !x.main ? ", backup" : ""})</span>`).join(" · ");
}

// Devices column (right of the tiles): only devices online now, new ones first, then by address.
// Offline devices stay on the full devices page.
function renderSide(x) {
  const side = $("#side");
  const all = (x.devices || x.clients) ? mergedDevices(x) : [];
  document.body.classList.toggle("has-side", all.length > 0);
  side.hidden = !all.length;
  if (!all.length) return false;
  const ip = (s) => (s || "").split(".").reduce((a, n) => a * 256 + (+n || 0), 0);
  const live = all.filter((d) => d.online);
  const byUsage = live.some((d) => d.usage);     // the router reports traffic (UniFi, ASUS): busiest first
  live.sort((a, b) => ((b.new ? 1 : 0) - (a.new ? 1 : 0))
    || (byUsage ? (usageVal(b) ?? -1) - (usageVal(a) ?? -1) : 0) || (ip(a.ip) - ip(b.ip)));
  const top = maxUsage(live);
  const nw = live.filter((d) => d.new).length;
  const href = viewHref("devices"), tgt = KIOSK ? "" : ` target="_blank" rel="noopener"`;
  side.innerHTML = `<h2><a href="${href}"${tgt}>Devices</a><span class="help">${live.length} online now${nw ? ` · ${nw} new` : ""}${byUsage ? " · busiest first" : ""}</span></h2>
    <ul class="sd-list">${live.length ? "" : `<li class="help">No devices online right now</li>`}${live.map((d) => `<li class="sd-row${d.online ? "" : " off"}" title="${esc(clueText(d))}">
      <span class="sdot ${d.online ? "up" : ""}"></span>
      <span class="sd-name">${devLabel(d)}${d.new ? ` <span class="chip warn">new</span>` : ""}${d.this_box ? ` <span class="chip">this box</span>` : ""}</span>
      <span class="sd-ip">${esc(d.ip || "")}</span>${d.link ? `<span class="sd-link">${linkText(d.link)}</span>` : ""}${byUsage ? `<span class="sd-use">${usageHtml(d.usage, top)}</span>` : ""}</li>`).join("")}</ul>
    <a class="sd-more" href="${href}"${tgt}>All devices, including offline ↗</a>`;
  return true;
}

// wall screen: the devices column fills the height left on screen and shows as many rows as fit
function sizeSide() {
  const side = $("#side");
  if (!KIOSK || !side || side.hidden) return;
  const list = $(".sd-list", side), more = $(".sd-more", side), rows = [...list.children];
  rows.forEach((r) => (r.hidden = false));
  side.style.height = "0px";
  const top = side.getBoundingClientRect().top + scrollY;
  const footH = document.querySelector("footer")?.offsetHeight || 0;
  side.style.height = Math.max(180, innerHeight - top - footH - 16) + "px";
  const lb = list.getBoundingClientRect().bottom;
  const cut = rows.filter((r) => r.getBoundingClientRect().bottom > lb + 1);
  cut.forEach((r) => (r.hidden = true));
  more.textContent = cut.length ? `+${cut.length} more · see all ↗` : "All devices, including offline ↗";
}

function fitsScreen() {
  sizeSide();
  return document.documentElement.scrollHeight <= window.innerHeight + 2;
}

function fitKiosk() {
  const body = document.body, groupsEl = $("#groups");
  const secs = [...document.querySelectorAll("#groups > section.group:not(#k-merged)")];
  const levels = [
    { density: "", collapse: false },
    { density: "compact", collapse: false },
    { density: "compact", collapse: true },
    { density: "mini", collapse: true },
    { density: "mini", collapse: true, merge: true },   // all healthy groups become one chip
  ];
  $("#k-merged")?.remove();
  const apply = (lv) => {
    body.classList.toggle("k-compact", lv.density === "compact");
    body.classList.toggle("k-mini", lv.density === "mini");
    $("#k-merged")?.remove();
    for (const s of secs) {
      const c = lv.collapse && s.dataset.healthy === "1";
      s.classList.toggle("collapsed", c);
      s.hidden = !!(lv.merge && c);
      const h = $("h2", s);
      if (c && !h.dataset.full) {
        h.dataset.full = h.innerHTML;
        // keep detail-page links (e.g. Devices, Wi-Fi signal) reachable when the group is a chip
        const links = [...s.querySelectorAll(".tile .more")].map((a) => `<a class="more" href="${a.getAttribute("href")}">${esc($(".name", a.closest(".tile"))?.textContent || "details")} ↗</a>`);
        h.innerHTML = esc(s.dataset.summary) + (links.length ? ` · ${links.join(" · ")}` : "");
      }
      if (!c && h.dataset.full) { h.innerHTML = h.dataset.full; delete h.dataset.full; }
    }
    if (lv.merge) {
      const healthy = secs.filter((s) => s.dataset.healthy === "1");
      if (healthy.length) {
        const total = healthy.reduce((a, s) => a + s.querySelectorAll(".tile").length, 0);
        const notes = healthy.reduce((a, s) => a + s.querySelectorAll(".tile.low").length, 0);
        const m = document.createElement("section");
        m.id = "k-merged";
        m.className = "group collapsed";
        m.innerHTML = `<h2>${secs.length > healthy.length ? "Everything else" : "Everything"}: all ${total} working in ${healthy.length} groups${notes ? ` · ${notes} ${notes === 1 ? "note" : "notes"}` : ""}</h2>`;
        groupsEl.append(m);
      }
    }
  };
  $("#k-pager")?.remove();
  clearInterval(kioskState.timer);
  for (const lv of levels) {
    apply(lv);
    if (fitsScreen()) return;
  }
  // Still too much: only groups with problems are left as full sections. Keep as many as fit,
  // most serious first (they are already ordered that way), and rotate the rest in pages.
  const rest = secs.filter((s) => !s.hidden);
  const pages = [];
  let cur = [];
  for (const s of rest) s.hidden = true;
  for (const s of rest) {
    s.hidden = false;
    if (!fitsScreen() && cur.length) {
      for (const x of cur) x.hidden = true;
      pages.push(cur);
      cur = [];
    }
    cur.push(s);
  }
  if (cur.length) pages.push(cur);
  if (kioskState.page >= pages.length) kioskState.page = 0;
  const show = () => {
    for (const s of rest) s.hidden = true;
    for (const s of pages[kioskState.page]) s.hidden = false;
    let p = $("#k-pager");
    if (!p) { p = document.createElement("div"); p.id = "k-pager"; document.body.append(p); }
    p.textContent = `Problems page ${kioskState.page + 1} of ${pages.length}`;
  };
  show();
  if (pages.length > 1) kioskState.timer = setInterval(() => { kioskState.page = (kioskState.page + 1) % pages.length; show(); }, KIOSK_PAGE_MS);
}

if (KIOSK) addEventListener("resize", () => requestAnimationFrame(fitKiosk));

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
    const apRows = (a.networks || []).map((n) => [n.own ? "★" : "", esc(n.ssid || (n.own_hidden ? "(hidden, yours)" : "(hidden)")) + (n.own_extra && n.ssid ? " (guest/extra, yours)" : ""), esc(n.bssid),
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

// Light / Dark switch: remembered in this browser (the wall screen keeps its own choice)
for (const r of document.querySelectorAll("#theme-switch input")) {
  r.checked = r.value === document.documentElement.dataset.theme;
  r.addEventListener("change", () => {
    document.documentElement.dataset.theme = r.value;
    try { localStorage.setItem("lp-theme", r.value); } catch { /* private mode */ }
    if (KIOSK) requestAnimationFrame(fitKiosk);
  });
}
refresh();
setInterval(refresh, REFRESH_MS);
setInterval(() => { const u = $("#updated"); if (u.dataset.t) u.textContent = `Updated ${ago(u.dataset.t)}${u.dataset.s || ""}`; }, 15_000);
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
      if (r.ok) {
        BOX = j; renderSetup();
        const need = BOX.router && BOX.router.type !== "none" && !BOX.router.ok;
        // ask once: ?router=1 (from the wall screen) is used up straight away, so the prompt
        // doesn't come back when the page reloads its settings after connecting
        const asked = params.has("router");
        if (asked) {
          params.delete("router");
          history.replaceState(null, "", `${location.pathname}${params.toString() ? `?${params}` : ""}${location.hash}`);
        }
        if ((asked && need) || (need && params.has("admin") && !sessionStorage.getItem("lp-router-asked"))) {
          sessionStorage.setItem("lp-router-asked", "1");
          if (!$("#router-dialog").open) openRouterDialog();
        }
      }
    } catch { /* ignore */ }
    if (params.has("admin")) renderAdminPanel();
  } else {
    $("#admin-panel").hidden = true;
    document.body.classList.remove("admin-view");
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
  const toAdmin = SESSION.signed_in && !params.has("admin") ? `<a class="button ghost" href="./?admin">Admin view</a>` : "";
  bar.innerHTML = SESSION.signed_in
    ? `<span class="who">Administrator</span> ${toAdmin} <button id="signout" class="ghost">Sign out</button> ${back}`
    : `<button id="signin" class="ghost">Administrator sign-in</button> ${back}`;
  bar.hidden = false;
  $("#signout")?.addEventListener("click", async () => {
    await api("api/logout", { method: "POST", body: "{}" });
    if (FROM_KIOSK) location.href = "./?kiosk"; else loadBoxSettings();
  });
  $("#signin")?.addEventListener("click", () => openSignIn());
}

var AFTER_SIGNIN = null;              // what to do once signed in (instead of opening the admin view)
function openSignIn(why) {
  if (typeof why !== "string") why = "";
  if (!why) AFTER_SIGNIN = null;
  const first = !SESSION.admin_set;
  $("#si-title").textContent = first ? "Create the administrator password" : "Administrator sign-in";
  $("#si-help").textContent = first
    ? "No administrator password has been set on this box yet. Choose one (at least 8 characters). It is needed to change settings such as the router connection. Only a scrambled (hashed) copy is stored on the box."
    : "Sign in to change settings such as the router connection.";
  if (why) $("#si-help").textContent = first ? `${why} ${$("#si-help").textContent}` : why;
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
    if (AFTER_SIGNIN) { const next = AFTER_SIGNIN; AFTER_SIGNIN = null; await loadBoxSettings(); next(); return; }
    if (!params.has("admin")) { location.href = `./?admin${FROM_KIOSK ? "&from=kiosk" : ""}`; return; }
    await loadBoxSettings();
  } catch (err) { say(`Couldn't reach the box: ${err.message}`); }
});

// ---------------------------------------------------------------- admin view (?admin, signed in)
var LAST_SNAP = null;

function adminHealth() {
  const src = (LAST_SNAP?.sources || []).map((s) => `<li>${s.ok ? "✓" : `<span class="weak">✗</span>`} ${esc(s.name)}${s.error ? `: <span class="weak">${esc(s.error)}</span>` : ""}</li>`).join("");
  const nc = LAST_SNAP?.extras?.netcheck;
  return `<ul class="facts">${src || "<li>Loading…</li>"}</ul>` +
    (nc ? `<p class="help">Network checks last ran ${esc(ago(nc.generated))} from ${esc(nc.interface || "?")} (${esc(nc.interface_kind || "")}), gateway ${esc(nc.gateway || "?")}.</p>` : "");
}

// Phone alerts come from Uptime Kuma through ntfy. The topic is shown only here (signed in):
// on a public ntfy server, anyone who knows the topic can read the alerts.
const NTFY_APPS = [
  ["Android (Google Play)", "https://play.google.com/store/apps/details?id=io.heckel.ntfy"],
  ["Android (F-Droid)", "https://f-droid.org/packages/io.heckel.ntfy/"],
  ["iPhone / iPad (App Store)", "https://apps.apple.com/app/ntfy/id1625396347"],
];
function alertsCard() {
  const t = (BOX?.alerts || []).filter((a) => a.active);
  const apps = `<div class="applinks">${NTFY_APPS.map(([n, u]) => `<a class="button ghost" href="${u}" target="_blank" rel="noopener">${n}</a>`).join("")}</div>`;
  if (!t.length) return `<p>Alerts are sent to phones with the free <b>ntfy</b> app.</p>${apps}
    <p class="help">No phone-alert topic is set up yet. On the box run <code>sudo /opt/monitor/scripts/kuma-ntfy.sh</code>, or add an ntfy notification in Uptime Kuma.</p>`;
  return `<p>Get a notification when something stops responding: install the free <b>ntfy</b> app, then subscribe to this topic.</p>
    ${apps}
    ${t.map((a) => `<div class="topic">
      <div><span class="help">Topic</span><code class="sel">${esc(a.topic)}</code><button class="ghost small" data-copy="${esc(a.topic)}">Copy</button></div>
      <div><span class="help">Server</span><code class="sel">${esc(a.server)}</code>${a.server === "https://ntfy.sh" ? ` <span class="help">(the app's default)</span>` : ` <span class="help">(in the app: “Use another server”)</span>`}</div>
      <a href="${esc(a.server)}/${encodeURIComponent(a.topic)}" target="_blank" rel="noopener">Open this topic in a web browser ↗</a>
    </div>`).join("")}
    <p class="help">In the app: tap <b>+</b>, enter the topic, tap Subscribe. Keep the topic private: anyone who has it can read these alerts.</p>
    <button id="ap-ntfy-test" class="ghost">Send a test alert</button> <span id="ap-ntfy-result" class="result" hidden></span>`;
}
function wireAlertsCard() {
  const box = $("#ap-alerts");
  if (!box) return;
  box.onclick = async (e) => {
    const c = e.target.closest("[data-copy]");
    if (c) {
      try { await navigator.clipboard.writeText(c.dataset.copy); c.textContent = "Copied"; }
      catch { const sel = c.previousElementSibling; getSelection().selectAllChildren(sel); c.textContent = "Selected: press Ctrl+C"; }
      setTimeout(() => (c.textContent = "Copy"), 2500);
      return;
    }
    if (e.target.id === "ap-ntfy-test") {
      const out = $("#ap-ntfy-result");
      const say = (m, ok) => { out.hidden = false; out.className = `result ${ok ? "good" : "bad"}`; out.textContent = m; };
      try {
        const [r, j] = await api("api/alerts/test", { method: "POST", body: "{}" });
        say(r.ok ? "Sent: check your phone." : j.error || `Failed (HTTP ${r.status})`, r.ok);
      } catch (err) { say(`Couldn't reach the box: ${err.message}`); }
    }
  };
}

function renderAdminPanel() {
  const p = $("#admin-panel");
  document.body.classList.add("admin-view");
  if (p.dataset.done) {   // keep forms intact; refresh only the live parts
    $("#ap-health").innerHTML = adminHealth();
    const nid = LAST_SNAP?.extras?.network?.id;
    if (nid && NET && NET.network && NET.network.id !== nid) loadNetwork();   // the box moved
    const a = $("#ap-alerts"), n = String((BOX?.alerts || []).length);
    if (a && a.dataset.n !== n) { a.innerHTML = alertsCard(); a.dataset.n = n; }
    return;
  }
  p.dataset.done = "1";
  const host = location.hostname;
  const tool = (port, name, what) => `<a class="toolcard" href="http://${host}:${port}/" target="${FROM_KIOSK ? "_self" : "_blank"}" rel="noopener"><b>${name}</b><span>${what}</span></a>`;
  p.innerHTML = `
    <div class="admin-head">
      <h2>Administration</h2>
      <div class="admin-actions">
        ${FROM_KIOSK ? `<a class="button" href="./?kiosk">Back to wall screen</a>` : `<a class="button ghost" href="./">Leave admin view</a>`}
        <button id="ap-signout" class="ghost">Sign out</button>
      </div>
    </div>
    ${FROM_KIOSK ? `<p class="help">Returns to the wall screen by itself after 10 minutes without use.</p>` : ""}
    <div class="admin-grid">
      <div class="admin-card net-card" id="ap-net"><h3>This network</h3><div id="ap-net-body"><p class="help">Loading…</p></div></div>
      <div class="admin-card"><h3>Router connection</h3><div id="ap-router"></div></div>
      <div class="admin-card"><h3>Tools</h3>
        <div class="tools">
          ${tool(3001, "Uptime Kuma", "Add or change monitored devices, alerts and notifications")}
          ${tool(20211, "NetAlertX", "Every device on the network, names, new-device alerts")}
          ${tool(8765, "Speedtest Tracker", "Internet speed history and test schedule")}
        </div>
      </div>
      <div class="admin-card"><h3>Phone alerts</h3><div id="ap-alerts">${alertsCard()}</div></div>
      <div class="admin-card"><h3>Box health</h3><div id="ap-health">${adminHealth()}</div></div>
      <div class="admin-card"><h3>Administrator password</h3>
        <form id="ap-pw" autocomplete="off">
          <label>Current password <input id="ap-cur" type="password" autocomplete="current-password"></label>
          <label>New password <input id="ap-new" type="password" autocomplete="new-password"></label>
          <label>New password again <input id="ap-new2" type="password" autocomplete="new-password"></label>
          <p id="ap-pw-result" class="result" hidden></p>
          <button type="submit">Change password</button>
        </form>
      </div>
    </div>
    <h2 class="live-head">Live status</h2>`;
  p.hidden = false;
  // router card: reuse the setup card, placed inside the admin view
  const setup = $("#setup");
  $("#ap-router").append(setup);
  setup.hidden = !BOX;
  wireAlertsCard();
  loadNetwork();
  $("#ap-signout").onclick = async () => {
    try { await api("api/logout", { method: "POST", body: "{}" }); } catch {}
    location.href = FROM_KIOSK ? "./?kiosk" : "./";
  };
  $("#ap-pw").onsubmit = async (e) => {
    e.preventDefault();
    const out = $("#ap-pw-result");
    const say = (m, ok) => { out.hidden = false; out.className = `result ${ok ? "good" : "bad"}`; out.textContent = m; };
    if ($("#ap-new").value !== $("#ap-new2").value) return say("The new passwords don't match.");
    if ($("#ap-new").value.length < 8) return say("Use at least 8 characters.");
    try {
      const [r, j] = await api("api/admin/password", { method: "POST", body: JSON.stringify({ current: $("#ap-cur").value, password: $("#ap-new").value }) });
      if (!r.ok) return say(j.error || `Failed (HTTP ${r.status})`);
      $("#ap-pw").reset();
      say("Password changed.", true);
    } catch (err) { say(`Couldn't reach the box: ${err.message}`); }
  };
}

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
    html = `<div><b>See how every device is connected</b><br><span class="help">Connect ${st.network_name ? `this network's (${esc(st.network_name)})` : "your"} router to show which devices use a cable, 2.4 GHz or 5 GHz, which access point they use and how strong their signal is. Each network keeps its own router login.</span></div>
            <button id="setup-open">Connect your router</button>`;
  } else if (!st.has_password || st.error) {
    cls = "problem";
    html = `<div><b>Router connection needs attention</b><br><span class="help">${esc(label(st.type))}: ${esc(st.has_password ? st.error : "the password hasn't been entered yet")}</span></div>
            <button id="setup-open">Fix the connection</button>`;
  } else {
    cls = "ok";
    html = `<div><b>Router connected</b> <span class="help">${esc(label(st.type))} at ${esc(st.url)}${st.network_name ? `, on ${esc(st.network_name)}` : ""}</span></div>
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
  const found = types.find((t) => t.id === BOX.router_detected);
  sel.value = st.type && st.type !== "none" ? st.type : (found ? found.id : "");
  $("#r-detected").hidden = !(found && !st.has_password);
  if (found) $("#r-detected").textContent = `Found your router: ${found.label} at ${found.url}. Enter its admin password below.`;
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
  $("#router-form").onsubmit = (e) => { e.preventDefault(); routerAction("save"); };
  const link = () => { const u = ($("#r-url").value.split(",")[0] || "").trim(); $("#r-open").href = u || "#"; $("#r-open").parentElement.hidden = !u; };
  $("#r-url").oninput = link;
  sel.addEventListener("change", link);
  link();
  $("#r-remove").onclick = () => routerAction("remove");
  d.showModal();
  for (const id of ["r-type", "r-url", "r-user", "r-pass"]) {
    const f = $(`#${id}`); f.classList.remove("invalid");
    f.addEventListener("input", () => f.classList.remove("invalid"));
  }
  (sel.value ? $("#r-pass") : sel).focus();
}

// One step from anywhere on the dashboard: sign in as administrator if needed, then the router prompt.
async function startRouterSignin() {
  if (KIOSK) { location.href = "./?admin&from=kiosk&router=1"; return; }
  try { const [, s] = await api("api/session"); SESSION = s; } catch { return; }
  if (!SESSION.signed_in) {
    AFTER_SIGNIN = startRouterSignin;
    openSignIn("To sign in to the router, first sign in as this box's administrator.");
    return;
  }
  if (!BOX) {
    try { const [r, j] = await api("api/settings"); if (!r.ok) return; BOX = j; } catch { return; }
  }
  openRouterDialog();
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
    if (!r.ok || j.ok === false) { show(j.error || `Failed (HTTP ${r.status})`, false, j.field); return; }
    if (action === "remove") { show("Disconnected. The saved login was removed.", true); }
    else {
      const bands = Object.entries(j.client_bands || {}).map(([k, v]) => `${v} ${k}`).join(", ");
      show(`${action === "save" ? "Saved and connected" : "It works"}: ${j.clients} devices${bands ? ` (${bands})` : ""}` +
           `${j.nodes > 1 ? `, ${j.nodes} access points` : ""}.${action === "test" ? " Press Save and connect to keep it." : " The dashboard updates in a few seconds."}`, true);
      if (action === "save") { $("#r-pass").value = ""; setTimeout(() => $("#router-dialog").close(), 2500); }
    }
    await loadBoxSettings();
    if (action !== "test") setTimeout(refresh, 3000);
  } catch (e) {
    show(`Couldn't reach the box: ${e.message}`, false);
  } finally {
    for (const b of $("#router-form").querySelectorAll("button")) b.disabled = false;
  }
  function show(msg, ok, field) {
    out.hidden = false; out.textContent = msg; out.className = `result ${ok === true ? "good" : ok === false ? "bad" : ""}`;
    for (const id of ["r-type", "r-url", "r-user", "r-pass"]) $(`#${id}`).classList.remove("invalid");
    const el = { password: "#r-pass", url: "#r-url", type: "#r-type", user: "#r-user" }[field];
    if (el) { $(el).classList.add("invalid"); if (field === "password") $("#r-user").classList.add("invalid"); $(el).focus(); $(el).select?.(); }
  }
}

// ---------------------------------------------------------------- this network (admin view)
// The box can move between networks. It recognises each one by its router, keeps a separate device
// list, router login and history for each, and starts a new network with a small starter list.
var NET = null;                       // GET api/network
const CHECKS = { ping: "Ping", http: "Web page", port: "Port open", dns: "Name lookup" };

async function netRequest(body, out) {
  const say = (m, ok) => { if (!out) return; out.hidden = false; out.className = `result ${ok === true ? "good" : ok === false ? "bad" : ""}`; out.textContent = m; };
  try {
    const [r, j] = await api("api/network", { method: "POST", body: JSON.stringify(body) });
    if (j.state) { NET = j.state; renderNetwork(); loadBoxSettings(); }   // router card follows the network
    if (!r.ok || j.ok === false) { say(j.error || `Failed (HTTP ${r.status})`, false); return null; }
    if (j.pending) { say(j.error, null); return null; }
    return j;
  } catch (e) { say(`Couldn't reach the box: ${e.message}`, false); return null; }
}

async function loadNetwork() {
  try {
    const [r, j] = await api("api/network");
    if (r.ok) { NET = j; renderNetwork(); }
  } catch { /* not on a box */ }
}

function renderNetwork() {
  const body = $("#ap-net-body");
  if (!body || !NET) return;
  const n = NET.network;
  if (!n) {
    body.innerHTML = NET.online
      ? `<p class="help">Working out which network this is. The box looks at the evidence when the connection changes, after a restart, and at least every 10 minutes.</p>`
      : `<p class="warning">The box isn't connected to a network right now (no router found). Plug in a network cable${NET.wifi ? ", or join a Wi-Fi network" : ""}.</p>
      ${NET.wifi ? `<button id="ap-wifi" class="ghost">Join Wi-Fi…</button>` : ""}`;
    $("#ap-wifi")?.addEventListener("click", openWifi);
    return;
  }
  const here = NET.networks.find((x) => x.id === n.id) || { rows: [], monitors: 0 };
  const others = NET.networks.filter((x) => x.id !== n.id);
  const fresh = n.setup === "starter";
  const when = (t) => (t ? new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "?");
  const m = n.match || {}, cand = n.candidate;
  const list = (a) => (a || []).map(esc).join("; ");
  const how = m.how === "recognised" || m.how === "confirmed"
    ? `<li>${m.how === "confirmed" ? "<b>Confirmed</b> by an administrator" : `<b>Recognised</b> from the evidence: ${list(m.reasons)}`}${m.against?.length ? ` <span class="help">(though: ${list(m.against)})</span>` : ""}
         <button class="ghost small" id="ap-split">Not this network?</button></li>`
    : `<li><b>New network:</b> it didn't match any network this box knows${m.closest && m.closest.score > 0 ? ` <span class="help">(closest: ${esc(m.closest.name)}, ${list(m.closest.reasons)}${m.closest.against?.length ? `; against: ${list(m.closest.against)}` : ""})</span>` : ""}</li>`;
  body.innerHTML = `
    ${cand ? `<div class="setup problem"><div><b>Is this ${esc(cand.name)}?</b><br>
      <span class="help">Some evidence matches (${list(cand.reasons)})${cand.against?.length ? `, some doesn't (${list(cand.against)})` : ""}, but not enough to be sure.
      Until you answer it's treated as a new network: ${esc(cand.name)}'s devices and router login aren't used. The box keeps looking and decides by itself if the evidence becomes clear.</span></div>
      <span class="net-actions"><button id="ap-yes">Yes, it's ${esc(cand.name)}</button><button id="ap-no" class="ghost">No, it's a different network</button></span></div>` : ""}
    ${fresh && !cand ? `<div class="setup invite"><div><b>${n.visits > 1 ? "This network has only the starter list" : "New network"}</b><br>
      <span class="help">The box is watching the router, the internet and name lookups here. Choose the devices that matter on this network.</span></div>
      <button id="ap-dev-open2">Choose devices</button></div>` : ""}
    <form id="ap-net-name" class="inline">
      <label>Name <span class="help">(the dashboard's title on this network)</span>
        <span class="pw"><input id="ap-net-n" value="${esc(n.name)}" maxlength="80"><button type="submit" class="ghost">Rename</button></span>
      </label>
    </form>
    <p id="ap-net-result" class="result" hidden></p>
    <ul class="facts">
      ${how}
      <li>Connected by ${n.kind === "Wi-Fi" ? `<b>Wi-Fi</b>${n.ssid ? ` (${esc(n.ssid)})` : ""}` : "<b>cable</b>"} on ${esc(n.interface || "?")}: address ${esc(n.address || "?")}, network ${esc(n.subnet || "?")}, router ${esc(n.gateway || "?")}</li>
      <li>Here since ${esc(when(n.since))}${n.visits > 1 ? ` · visit ${n.visits}` : ""} · first seen ${esc(when(n.first_seen))}</li>
      <li>Find the box from any network at <a href="http://${esc(NET.hostname || "lanpulse")}.local:8080/">${esc(NET.hostname || "lanpulse")}.local:8080</a> <span class="help">(or ${esc(n.address || "")}:8080)</span></li>
    </ul>
    <div class="net-actions">
      <button id="ap-dev-open" class="${fresh ? "ghost" : ""}">Devices to watch (${here.monitors})</button>
      ${NET.wifi ? `<button id="ap-wifi" class="ghost">Join Wi-Fi…</button>` : ""}
      <button id="ap-forget-here" class="ghost danger">Forget this network and start over</button>
    </div>
    ${others.length ? `<h4>Other networks this box knows</h4>
      <ul class="netlist">${others.map((o) => `<li><b>${esc(o.name)}</b> <span class="help">${esc(o.subnet || "")} · last there ${esc(when(o.last_seen))} · ${o.monitors} devices</span>
        <button class="ghost small danger" data-forget="${esc(o.id)}">Forget</button></li>`).join("")}</ul>
      <p class="help">Their device lists, router logins and history are kept and come back when the box is on that network again.</p>` : ""}`;
  $("#ap-net-name").onsubmit = async (e) => {
    e.preventDefault();
    const out = $("#ap-net-result");
    const j = await netRequest({ type: "rename", network: n.id, name: $("#ap-net-n").value }, out);
    if (j) { out.hidden = false; out.className = "result good"; out.textContent = `Renamed to ${j.name}.`; setTimeout(refresh, 3000); }
  };
  const out = $("#ap-net-result");
  const busy = (b, text) => { out.hidden = false; out.className = "result"; out.textContent = text; b.disabled = true; };
  $("#ap-yes")?.addEventListener("click", async (e) => {
    busy(e.target, `Merging with ${cand.name}: Uptime Kuma restarts, about half a minute…`);
    const j = await netRequest({ type: "confirm", candidate: cand.id }, out);
    if (j) { out.className = "result good"; out.textContent = `This is ${j.network.name}: its devices, router login and history are back.`; setTimeout(refresh, 20_000); }
  });
  $("#ap-no")?.addEventListener("click", async (e) => {
    busy(e.target, "Saving…");
    if (await netRequest({ type: "reject", candidate: cand.id }, out)) { out.className = "result good"; out.textContent = `Kept as a separate network. It won't be confused with ${cand.name} again.`; }
  });
  $("#ap-split")?.addEventListener("click", async (e) => {
    const b = e.target;
    if (b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = `Make this a new network, separate from ${n.name}? Click again`; return; }
    busy(b, "Separating: Uptime Kuma restarts, about half a minute…");
    const j = await netRequest({ type: "split" }, out);
    if (j) { out.className = "result good"; out.textContent = `Now a separate new network (${j.network.name}) with a starter device list. ${n.name} is kept as it was.`; setTimeout(refresh, 20_000); }
  });
  $("#ap-forget-here").onclick = async (e) => {
    const b = e.target;
    if (b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = `Delete ${n.name}'s ${here.monitors} devices, router login link and history, and start over? Click again`; return; }
    busy(b, "Forgetting…");
    const j = await netRequest({ type: "forget", network: n.id }, out);
    if (j) { out.className = "result good"; out.textContent = "Forgotten. The box looks at the network afresh on its next check (within 10 minutes), when the connection changes, or after a restart."; }
  };
  $("#ap-dev-open").onclick = openDevices;
  $("#ap-dev-open2")?.addEventListener("click", openDevices);
  $("#ap-wifi")?.addEventListener("click", openWifi);
  body.querySelectorAll("[data-forget]").forEach((b) => b.addEventListener("click", async () => {
    const o = NET.networks.find((x) => x.id === b.dataset.forget);
    if (!o) return;
    if (b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = `Forget ${o.name} and its ${o.monitors} devices? Click again`; return; }
    b.disabled = true;
    await netRequest({ type: "forget", network: o.id }, $("#ap-net-result"));
  }));
}

// ---- device list editor
let DV_ROWS = [];
function openDevices() {
  const n = NET.network, here = NET.networks.find((x) => x.id === n.id) || { rows: [] };
  DV_ROWS = here.rows.map((r) => ({ ...r }));
  $("#dv-net").textContent = n.name;
  const listed = () => new Set(DV_ROWS.map((r) => r.target.split(/[:@/]/).find((p) => /^\d+\.\d+\.\d+\.\d+$/.test(p)) || r.target));
  const fillFound = () => {
    const have = listed();
    $("#dv-found").innerHTML = `<option value="">Choose a device…</option>` + NET.discovered.filter((d) => !d.this_box)
      .map((d) => `<option value="${esc(d.ip)}"${have.has(d.ip) ? " disabled" : ""}>${esc(d.ip)} · ${esc(d.name || (d.hint && `probably ${d.hint}`) || d.vendor || "unnamed")}${d.online ? "" : " (offline)"}${have.has(d.ip) ? " ✓" : ""}</option>`).join("");
  };
  fillFound();
  $("#dv-copy").innerHTML = `<option value="">Choose a network…</option>` + NET.networks.filter((x) => x.id !== n.id && x.rows.length)
    .map((x) => `<option value="${esc(x.id)}">${esc(x.name)} (${x.rows.length} devices)</option>`).join("");
  $("#dv-copy").disabled = $("#dv-copy").options.length < 2;
  const draw = () => {
    $("#dv-rows").innerHTML = DV_ROWS.map((r, i) => `<tr data-i="${i}">
      <td><input data-k="group" value="${esc(r.group)}" maxlength="60"></td>
      <td><input data-k="name" value="${esc(r.name)}" maxlength="80"></td>
      <td><select data-k="type">${Object.entries(CHECKS).map(([k, v]) => `<option value="${k}"${r.type === k ? " selected" : ""}>${v}</option>`).join("")}</select></td>
      <td><input data-k="target" value="${esc(r.target)}" spellcheck="false"></td>
      <td><button type="button" class="ghost small" data-del="${i}" title="Remove">✕</button></td></tr>`).join("")
      || `<tr><td colspan="5" class="help">No devices yet: add some from the lists above.</td></tr>`;
    fillFound();
  };
  $("#dv-rows").oninput = (e) => { const tr = e.target.closest("tr"); if (tr && e.target.dataset.k) DV_ROWS[+tr.dataset.i][e.target.dataset.k] = e.target.value; };
  $("#dv-rows").onclick = (e) => { const d = e.target.closest("[data-del]"); if (d) { DV_ROWS.splice(+d.dataset.del, 1); draw(); } };
  $("#dv-found").onchange = (e) => {
    const d = NET.discovered.find((x) => x.ip === e.target.value);
    if (d) DV_ROWS.push({ group: "Devices", name: uniqueName(d.name || d.vendor || `Device ${d.ip}`), type: "ping", target: d.ip, description: d.vendor || "" });
    draw();
  };
  $("#dv-copy").onchange = (e) => {
    const x = NET.networks.find((o) => o.id === e.target.value);
    if (x) for (const r of x.rows) if (!DV_ROWS.some((o) => o.name === r.name)) DV_ROWS.push({ ...r });
    e.target.value = "";
    draw();
  };
  $("#dv-csv-add").onclick = () => {
    for (const line of $("#dv-csv").value.split(/\r?\n/)) {
      const t = line.trim();
      if (!t || t.startsWith("#") || /^group\s*,/i.test(t)) continue;
      const [group, name, type, target, ...desc] = t.split(",").map((s) => s.trim());
      if (name && target) DV_ROWS.push({ group: group || "Devices", name: uniqueName(name), type: (type || "ping").toLowerCase(), target, description: desc.join(",") });
    }
    $("#dv-csv").value = "";
    draw();
  };
  $("#dv-new").onclick = () => { DV_ROWS.push({ group: "Devices", name: uniqueName("New device"), type: "ping", target: "", description: "" }); draw(); };
  $("#dv-save").onclick = async () => {
    const out = $("#dv-result"), btn = $("#dv-save");
    out.hidden = false; out.className = "result"; out.textContent = "Saving: Uptime Kuma restarts, so this takes about half a minute…";
    btn.disabled = true;
    const j = await netRequest({ type: "monitors", network: n.id, rows: DV_ROWS }, out);
    btn.disabled = false;
    if (j) { out.className = "result good"; out.textContent = `Saved: ${j.monitors} devices are watched on ${n.name}. The dashboard updates in a minute.`; setTimeout(refresh, 20_000); }
  };
  $("#dv-result").hidden = true;
  draw();
  $("#devices-dialog").showModal();
}
function uniqueName(base) {
  let n = base, i = 2;
  while (DV_ROWS.some((r) => r.name === n)) n = `${base} ${i++}`;
  return n;
}

// ---- Wi-Fi
async function openWifi() {
  const d = $("#wifi-dialog"), out = $("#wf-result");
  $("#wf-host").textContent = `http://${NET?.hostname || "lanpulse"}.local:8080`;
  $("#wf-pass").value = "";
  out.hidden = true;
  const scan = async () => {
    $("#wf-ssid").innerHTML = `<option value="">Scanning…</option>`;
    const j = await netRequest({ type: "wifi_scan" }, out);
    const nets = j?.networks || [];
    $("#wf-ssid").innerHTML = nets.length
      ? nets.map((w) => `<option value="${esc(w.ssid)}" data-open="${w.security === "open" || w.security === "" ? 1 : 0}">${esc(w.ssid)} · signal ${w.signal}%${w.security && w.security !== "open" ? "" : " · no password"}${w.in_use ? " · connected now" : ""}</option>`).join("")
      : `<option value="">No networks found</option>`;
  };
  $("#wf-scan").onclick = scan;
  $("#wf-show").onclick = () => { const p = $("#wf-pass"); p.type = p.type === "password" ? "text" : "password"; $("#wf-show").textContent = p.type === "password" ? "Show" : "Hide"; };
  $("#wf-join").onclick = async () => {
    const ssid = $("#wf-ssid").value;
    if (!ssid) return;
    out.hidden = false; out.className = "result"; out.textContent = `Joining ${ssid}…`;
    $("#wf-join").disabled = true;
    const j = await netRequest({ type: "wifi_join", ssid, password: $("#wf-pass").value }, out);
    $("#wf-join").disabled = false;
    if (j) { out.className = "result good"; out.textContent = `Connected to ${ssid}${j.network ? `: the box is now on ${j.network.name}` : ""}.`; $("#wf-pass").value = ""; }
  };
  d.showModal();
  scan();
}

loadBoxSettings();
setInterval(loadBoxSettings, 60_000);

// ---------------------------------------------------------------- detail pages (?view=devices | wifi)
const viewState = { filter: VIEW === "history" ? "7" : "all", q: "", sort: null, dir: 1 };
const RATING = [[-50, "excellent", "Excellent"], [-67, "good", "Good"], [-70, "fair", "Fair"], [-80, "weak", "Weak"], [-999, "poor", "Poor"]];
const rate = (rssi) => (rssi == null ? null : RATING.find(([min]) => rssi >= min));

function mergedDevices(x) {
  // NetAlertX devices + router clients, matched by MAC (or IP)
  const out = new Map();
  for (const d of x.devices || []) out.set((d.mac || d.ip || "").toUpperCase(), { ...d });
  for (const c of x.clients || []) {
    const k = (c.mac || c.ip || "").toUpperCase();
    const byIp = [...out.values()].find((d) => d.ip && d.ip === c.ip);
    const d = out.get(k) || byIp || {};
    if (byIp && !out.get(k)) out.delete((byIp.mac || byIp.ip || "").toUpperCase());
    out.set((d.mac || k).toUpperCase(), { ...d, mac: d.mac || c.mac, ip: d.ip || c.ip, name: [d.name, c.name].find((n) => n && n !== (d.vendor || c.vendor)) || null,
      online: d.online ?? c.online, link: c.link, usage: c.usage || d.usage, hint: d.hint, clues: d.clues });
  }
  return [...out.values()];
}

function dashboardHref() {
  const p = new URLSearchParams(location.search);
  p.delete("view");
  const s = p.toString();
  return "./" + (s ? "?" + s : "");
}

function renderView(snap) {
  const x = snap.extras || {}, site = snap.site || {};
  for (const id of ["#banner", "#groups", "#side", "#details", "#setup", "#warning", "#admin-panel"]) { const e = $(id); if (e) e.hidden = true; }
  const v = $("#view");
  v.hidden = false;
  const title = VIEW === "wifi" ? "Wi-Fi signal of devices" : VIEW === "history" ? "History" : "Devices on the network";
  document.title = `${title} · ${site.name || "lanpulse"}`;
  $("#site-name").textContent = title;
  $("#site-desc").textContent = site.name || "";
  $("#updated").textContent = snap.generated ? `Updated ${ago(snap.generated)}` : "";
  $("#updated").dataset.t = snap.generated || "";
  if (!v.dataset.built) {
    v.dataset.built = VIEW;
    const back = FROM_KIOSK_VIEW ? `<a class="button" href="./?kiosk">← Back to wall screen</a>`
                                 : `<a class="button ghost" href="${dashboardHref()}">← Dashboard</a>`;
    const filters = VIEW === "history" ? [["1", "24 hours"], ["7", "7 days"], ["30", "30 days"]]
      : VIEW === "wifi"
      ? [["all", "All"], ["poor", "Poor"], ["weak", "Weak"], ["fair", "Fair"], ["good", "Good"], ["excellent", "Excellent"]]
      : [["all", "All"], ["online", "Online"], ["new", "New"], ["offline", "Offline"], ["wired", "Wired"], ["wifi", "Wi-Fi"], ["weak", "Weak signal"], ["unnamed", "Unnamed"]];
    v.innerHTML = `
      <div class="view-bar">${back}
        <input id="v-q" type="search" placeholder="${VIEW === "history" ? "Search by device or check name…" : "Search name, IP, MAC, maker, access point…"}" autocomplete="off">
        <div class="chips" id="v-filters">${filters.map(([k, l]) => `<button class="chip${k === viewState.filter ? " on" : ""}" data-f="${k}">${l} <span></span></button>`).join("")}</div>
      </div>
      <div id="v-summary"></div>
      <div id="v-table"></div>
      ${FROM_KIOSK_VIEW ? `<p class="help">Returns to the wall screen by itself after 5 minutes.</p>` : ""}`;
    $("#v-q").addEventListener("input", (e) => { viewState.q = e.target.value.toLowerCase(); renderView(LAST_SNAP); });
    $("#v-filters").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      viewState.filter = b.dataset.f;
      v.querySelectorAll("#v-filters .chip").forEach((c) => c.classList.toggle("on", c === b));
      renderView(LAST_SNAP);
    });
    if (FROM_KIOSK_VIEW) {
      let t;
      const arm = () => { clearTimeout(t); t = setTimeout(() => { location.href = "./?kiosk"; }, 5 * 60_000); };
      ["pointerdown", "keydown", "scroll"].forEach((ev) => addEventListener(ev, arm, { passive: true }));
      arm();
    }
  }
  (VIEW === "wifi" ? viewWifi : VIEW === "history" ? viewHistory : viewDevices)(x);
}

function sortable(cols, rows, render) {
  // cols: [key, label, valueFn]; clicking a header sorts by it
  if (viewState.sort) {
    const c = cols.find((k) => k[0] === viewState.sort);
    if (c) rows.sort((a, b) => {
      const A = c[2](a), B = c[2](b);
      if (A == null && B == null) return 0;
      if (A == null) return 1;
      if (B == null) return -1;
      return (typeof A === "number" && typeof B === "number" ? A - B : String(A).localeCompare(String(B), undefined, { numeric: true })) * viewState.dir;
    });
  }
  const head = cols.map(([k, l]) => `<th data-k="${k}" class="sort${viewState.sort === k ? (viewState.dir > 0 ? " asc" : " desc") : ""}">${l}</th>`).join("");
  $("#v-table").innerHTML = rows.length
    ? `<div class="scroll"><table class="view-table"><thead><tr>${head}</tr></thead><tbody>${rows.map(render).join("")}</tbody></table></div>`
    : `<p class="help">Nothing matches.</p>`;
  $("#v-table").querySelectorAll("th.sort").forEach((th) => th.addEventListener("click", () => {
    viewState.dir = viewState.sort === th.dataset.k ? -viewState.dir : 1;
    viewState.sort = th.dataset.k;
    renderView(LAST_SNAP);
  }));
}

const ipNum = (ip) => (ip || "").split(".").reduce((a, n) => a * 256 + (+n || 0), 0) || null;
const tsOf = (s) => (s ? Date.parse(String(s).replace(" ", "T") + (/[Z+]/.test(String(s).slice(10)) ? "" : "Z")) : null);
const matches = (d) => !viewState.q || [d.name, d.hint, d.ip, d.mac, d.vendor, d.link?.node, d.link?.band && `${d.link.band} ghz`]
  .some((f) => f && String(f).toLowerCase().includes(viewState.q));

function viewDevices(x) {
  const all = mergedDevices(x);
  const weakAt = -70;
  const test = {
    all: () => true, online: (d) => d.online, offline: (d) => !d.online, new: (d) => d.new,
    wired: (d) => d.link?.type === "wired", wifi: (d) => d.link?.type === "wifi",
    weak: (d) => d.link?.rssi != null && d.link.rssi < weakAt, unnamed: (d) => !d.name,
  };
  $("#v-filters").querySelectorAll(".chip").forEach((c) => { $("span", c).textContent = all.filter(test[c.dataset.f]).length; });
  const rows = all.filter(test[viewState.filter] || test.all).filter(matches);
  if (!viewState.sort) rows.sort((a, b) => ((b.new ? 1 : 0) - (a.new ? 1 : 0)) || ((b.online ? 1 : 0) - (a.online ? 1 : 0)) || (ipNum(a.ip) - ipNum(b.ip)));
  const conn = (d) => (d.link ? linkText({ ...d.link, rssi: null }) : `<span class="help">${x.clients ? "not in router's list" : "connect a router to see"}</span>`);
  $("#v-summary").innerHTML = `<p class="help">${all.filter((d) => d.online).length} online now, ${all.length} seen in total.
    ${x.clients ? "Connection details come from the router." : "Connect your router (admin view) to see wired / 2.4 / 5 GHz and signal for each device."}
    Names and makers come from the device finder (NetAlertX); rename devices there.</p>`;
  sortable([
    ["state", "", (d) => (d.online ? 0 : 1)], ["name", "Name", (d) => d.name || ""], ["hint", "Looks like", (d) => d.hint || ""], ["vendor", "Maker", (d) => d.vendor || ""],
    ["ip", "IP address", (d) => ipNum(d.ip)], ["mac", "MAC", (d) => d.mac || ""], ["conn", "Connection", (d) => d.link?.type || ""],
    ["rssi", "Signal", (d) => d.link?.rssi ?? null],
    ...(rows.some((d) => d.usage) ? [["usage", "Usage", (d) => usageVal(d)]] : []),
    ["first", "First seen", (d) => tsOf(d.first_seen)], ["last", "Last seen", (d) => tsOf(d.last_seen)],
  ], rows, (d) => `<tr class="${d.online ? "" : "off"}">
      <td><span class="sdot ${d.online ? "up" : ""}" title="${d.online ? "online" : "offline"}"></span></td>
      <td title="${esc(clueText(d))}">${devLabel(d)} ${d.new ? `<span class="chip warn">new</span>` : ""}${d.this_box ? ` <span class="chip">this box</span>` : ""}</td>
      <td title="${esc((d.clues || []).join("\n"))}">${esc(d.hint || "")}</td>
      <td>${esc(d.vendor || "")}${d.random_mac ? ` <span class="help" title="Phones and laptops often use a changing private address">private MAC</span>` : ""}</td>
      <td>${esc(d.ip || "")}</td><td class="mono">${esc(d.mac || "")}</td><td>${conn(d)}</td>
      <td>${d.link?.rssi != null ? `<span class="${d.link.rssi < weakAt ? "weak" : ""}">${Math.round(d.link.rssi)} dBm</span>` : ""}</td>
      ${rows.some((r) => r.usage) ? `<td>${usageHtml(d.usage, maxUsage(rows))}</td>` : ""}
      <td>${esc(d.first_seen ? ago(d.first_seen) : "")}</td><td>${esc(d.last_seen ? ago(d.last_seen) : "")}</td></tr>`);
}

function viewWifi(x) {
  const wifi = (x.clients || []).filter((c) => c.link?.type === "wifi");
  if (!wifi.length) {
    $("#v-summary").innerHTML = `<p class="warning">No signal data yet. Sign in to the admin view and connect your router: it reports each Wi-Fi device's signal.</p>`;
    $("#v-table").innerHTML = "";
    return;
  }
  const withSig = wifi.filter((c) => c.link.rssi != null);
  const level = (c) => rate(c.link.rssi)?.[1] || "unknown";
  $("#v-filters").querySelectorAll(".chip").forEach((c) => {
    $("span", c).textContent = c.dataset.f === "all" ? wifi.length : withSig.filter((w) => level(w) === c.dataset.f).length;
  });
  const aps = {};
  for (const c of withSig) {
    const k = c.link.node || "(unknown)";
    (aps[k] ||= []).push(c.link.rssi);
  }
  const apRows = Object.entries(aps).sort((a, b) => b[1].length - a[1].length).map(([n, s]) =>
    `<tr><td>${esc(n)}</td><td>${s.length}</td><td>${Math.round(s.reduce((a, b) => a + b, 0) / s.length)} dBm</td><td>${Math.round(Math.min(...s))} dBm</td></tr>`).join("");
  const counts = RATING.map(([, k, l]) => `<span class="rate ${k}">${l}: ${withSig.filter((c) => level(c) === k).length}</span>`).join(" ");
  $("#v-summary").innerHTML = `
    <div class="rates">${counts}${wifi.length > withSig.length ? ` <span class="help">${wifi.length - withSig.length} not reported</span>` : ""}</div>
    <p class="help">Signal as the router hears each device. Excellent −50 dBm or better · Good to −67 · Fair to −70 (video calls OK) · Weak to −80 · Poor below −80.
      For weak devices: move the router more centrally and higher, add a mesh point or access point near them, or use a cable for things that never move.</p>
    ${Object.keys(aps).length > 1 ? `<details open><summary>By access point</summary><div class="panel"><table><thead><tr><th>Access point</th><th>Devices</th><th>Average</th><th>Weakest</th></tr></thead><tbody>${apRows}</tbody></table></div></details>` : ""}`;
  const rows = wifi.filter((c) => viewState.filter === "all" || level(c) === viewState.filter).filter(matches);
  if (!viewState.sort) rows.sort((a, b) => (a.link.rssi ?? 0) - (b.link.rssi ?? 0));
  const bar = (r) => {
    if (r == null) return "";
    const p = Math.max(4, Math.min(100, (r + 95) * 100 / 55));
    return `<span class="sigbar"><i class="${rate(r)[1]}" style="width:${p.toFixed(0)}%"></i></span>`;
  };
  const hasBand = wifi.some((c) => c.link.band), hasRate = wifi.some((c) => c.link.rate_mbps);
  sortable([
    ["name", "Device", (c) => c.name || ""], ["ip", "IP address", (c) => ipNum(c.ip)], ["node", "Access point", (c) => c.link.node || ""],
    ...(hasBand ? [["band", "Band", (c) => (c.link.band ? +c.link.band : null)]] : []), ["rssi", "Signal", (c) => c.link.rssi ?? null],
    ["rating", "Rating", (c) => c.link.rssi ?? null], ...(hasRate ? [["rate", "Link speed", (c) => c.link.rate_mbps ?? null]] : []),
  ], rows, (c) => {
    const r = rate(c.link.rssi);
    return `<tr>
      <td>${esc(c.name || "") || `<span class="help">(no name)</span>`}</td><td>${esc(c.ip || "")}</td><td>${esc(c.link.node || "")}</td>
      ${hasBand ? `<td>${c.link.band ? `${c.link.band} GHz` : `<span class="help">not reported</span>`}</td>` : ""}
      <td class="sigcell">${bar(c.link.rssi)} ${c.link.rssi != null ? `${Math.round(c.link.rssi)} dBm` : `<span class="help">not reported</span>`}</td>
      <td>${r ? `<span class="rate ${r[1]}">${r[2]}</span>` : ""}</td>${hasRate ? `<td>${c.link.rate_mbps ? `${Math.round(c.link.rate_mbps)} Mbps` : ""}</td>` : ""}</tr>`;
  });
}

// ---------------------------------------------------------------- History page (?view=history)
function fmtDur(s) {
  if (s == null) return "";
  if (s < 60) return `${Math.max(1, Math.round(s))} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) { const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60); return m ? `${h} h ${m} min` : `${h} h`; }
  const d = Math.floor(s / 86400), h = Math.round((s % 86400) / 3600);
  return h ? `${d} d ${h} h` : `${d} d`;
}
const whenText = (iso) => {
  const d = new Date(iso);
  return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};
// outages that overlap the last `days` days, with the downtime inside that window
function outagesIn(m, days) {
  const from = Date.now() - days * 86400_000;
  return (m.outages || []).map(([a, b, s]) => {
    const A = Date.parse(a), B = b ? Date.parse(b) : Date.now();
    return { start: a, end: b, dur: s, inside: Math.max(0, (B - Math.max(A, from)) / 1000) };
  }).filter((o) => (o.end ? Date.parse(o.end) : Date.now()) >= from);
}
function historyCount(x, days) {
  return (x.history?.monitors || []).reduce((n, m) => n + outagesIn(m, days).length, 0);
}

function viewHistory(x) {
  const h = x.history || {};
  const days = +viewState.filter || 7;
  const from = Date.now() - days * 86400_000;
  $("#v-filters").querySelectorAll(".chip span").forEach((s) => (s.textContent = ""));
  if (!h.monitors && !(h.changes || []).length) {
    $("#v-summary").innerHTML = `<p class="warning">No history yet. It is collected from Uptime Kuma and the network checks, and fills in over the first hours.</p>`;
    $("#v-table").innerHTML = "";
    return;
  }
  const q = viewState.q;
  const mons = (h.monitors || []).filter((m) => !q || m.name.toLowerCase().includes(q));

  // per check: uptime, outages, downtime, and a strip of hours (24 h) or days
  const rows = mons.map((m) => {
    const src = days === 1 ? (m.hourly || []).filter(([t]) => t * 1000 >= from - 3600_000) : (m.daily || []).filter(([t]) => t * 1000 >= from - 86400_000);
    const up = src.reduce((a, r) => a + r[1], 0), down = src.reduce((a, r) => a + r[2], 0);
    const outs = outagesIn(m, days);
    const cells = [];
    const step = days === 1 ? 3600 : 86400, n = days === 1 ? 24 : days;
    const end = Math.floor(Date.now() / 1000 / step) * step;
    const by = new Map(src.map((r) => [r[0], r]));
    for (let k = n - 1; k >= 0; k--) {
      const t = end - k * step, r = by.get(t);
      const pct = r && r[1] + r[2] ? r[1] / (r[1] + r[2]) : null;
      const cls = pct == null ? "none" : pct >= 0.9999 ? "up" : pct >= 0.98 ? "dip" : "down";
      // daily buckets start at UTC midnight, so name the day in UTC (local time would show the day before in the Americas)
      const label = new Date(t * 1000).toLocaleString(undefined, days === 1 ? { hour: "numeric" } : { month: "short", day: "numeric", timeZone: "UTC" });
      cells.push(`<i class="${cls}" title="${esc(label)}: ${pct == null ? "no data" : `${(pct * 100).toFixed(pct >= 0.9995 ? 0 : 1)}% up`}"></i>`);
    }
    return { m, pct: up + down ? up / (up + down) : null, up, down, outs,
             downtime: outs.reduce((a, o) => a + o.inside, 0), strip: cells.join("") };
  }).sort((a, b) => (a.pct ?? 2) - (b.pct ?? 2) || b.outs.length - a.outs.length || a.m.name.localeCompare(b.m.name));

  const allOut = rows.flatMap((r) => r.outs.map((o) => ({ ...o, name: r.m.name })));
  const longest = allOut.reduce((a, o) => (o.dur > (a?.dur || 0) ? o : a), null);
  const totUp = rows.reduce((a, r) => a + r.up, 0), totDown = rows.reduce((a, r) => a + r.down, 0);
  const range = days === 1 ? "24 hours" : `${days} days`;
  const started = h.from && Date.parse(h.from) > from ? `<p class="help">History started ${esc(whenText(h.from))}, so this covers less than ${range}.</p>` : "";
  const card = (big, small, cls = "") => `<div class="hcard ${cls}"><b>${big}</b><span>${small}</span></div>`;
  $("#v-summary").innerHTML = `
    <div class="hcards">
      ${card(allOut.length, `outage${allOut.length === 1 ? "" : "s"} in ${range}`, allOut.length ? "bad" : "good")}
      ${card(allOut.length ? fmtDur(allOut.reduce((a, o) => a + o.inside, 0)) : "none", "total downtime")}
      ${card(longest ? fmtDur(longest.dur) : "none", longest ? `longest: ${esc(longest.name)}` : "longest outage")}
      ${card(totUp + totDown ? `${((totUp / (totUp + totDown)) * 100).toFixed(totUp === totUp + totDown ? 0 : 2)}%` : "–", "of all checks succeeded")}
    </div>${started}`;

  // timeline: Kuma outages/recoveries + changes in the network checks, newest first
  const label = { up: "OK", warn: "needs a look", down: "problem", unknown: "unknown", pending: "checking", maintenance: "maintenance" };
  const ev = [
    ...(h.events || []).filter((e) => !e.first && (e.s === "down" || (e.s === "up" && e.dur_s != null) || e.s === "maintenance"))
      .map((e) => ({ t: e.t, s: e.s, name: e.name,
        text: e.s === "down" ? `stopped responding${e.msg ? `: ${e.msg}` : ""}` : e.s === "up" ? `back up after ${fmtDur(e.dur_s)}` : "maintenance started" })),
    ...(h.changes || []).map((c) => c.kind === "boot"
      ? { t: c.t, s: "restart", name: c.name, text: `restarted: ${c.msg}. Nothing was checked during that time.` }
      : c.kind === "network"
      ? { t: c.t, s: "restart", name: c.name, text: `moved to another network: ${c.msg}. From here on it watches that network's devices.` }
      : { t: c.t, s: c.s === "up" ? "up" : c.s === "down" ? "down" : "warn", name: c.name,
        text: `${label[c.from] || c.from} → ${label[c.s] || c.s}${c.msg ? `: ${c.msg}` : ""}` }),
  ].filter((e) => Date.parse(e.t) >= from && (!q || e.name.toLowerCase().includes(q))).sort((a, b) => b.t.localeCompare(a.t));

  // speed tests and newly seen devices in the range
  // Speedtest Tracker times are UTC without a zone marker; tsOf() reads them as UTC
  const speed = ((x.speed || {}).history || []).map((r) => ({ ...r, at: new Date(tsOf(r.at)).toISOString() })).filter((r) => Date.parse(r.at) >= from);
  const devs = x.devices || [];
  const firstSweep = Math.min(...devs.map((d) => tsOf(d.first_seen)).filter(Boolean));
  const newDevs = devs.filter((d) => !d.this_box && tsOf(d.first_seen) >= from && tsOf(d.first_seen) > firstSweep + 3600_000)
    .sort((a, b) => tsOf(b.first_seen) - tsOf(a.first_seen));

  const speedSvg = () => {
    const ok = speed.filter((r) => r.ok && r.down_mbps);
    if (ok.length < 3) return "";
    const max = Math.max(...ok.map((r) => r.down_mbps)), w = 600, ht = 90, bw = w / ok.length;
    return `<svg class="speedchart" viewBox="0 0 ${w} ${ht}" preserveAspectRatio="none" role="img" aria-label="Download speed per test">${ok.map((r, i) => {
      const y = ht - (r.down_mbps / max) * (ht - 4);
      return `<rect x="${(i * bw + 1).toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${(ht - y).toFixed(1)}" rx="2"><title>${esc(whenText(r.at))}: ${r.down_mbps} down / ${r.up_mbps ?? "?"} up Mbps</title></rect>`;
    }).join("")}</svg>`;
  };

  $("#v-table").innerHTML = `
    <h3 class="hhead">Uptime by check <span class="help">${days === 1 ? "each square is an hour" : "each square is a day"} · worst first</span></h3>
    ${rows.length ? `<div class="scroll"><table class="view-table htable"><thead><tr><th>Check</th><th>${days === 1 ? "Last 24 hours" : `Last ${days} days`}</th><th>Uptime</th><th>Outages</th><th>Downtime</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${esc(r.m.name)}</td><td><span class="hstrip">${r.strip}</span></td>
        <td class="${r.pct != null && r.pct < 0.99 ? "weak" : ""}">${r.pct == null ? "–" : `${(r.pct * 100).toFixed(r.pct >= 0.9995 ? 0 : 2)}%`}</td>
        <td>${r.outs.length || ""}</td><td>${r.downtime ? fmtDur(r.downtime) : ""}</td></tr>`).join("")}
    </tbody></table></div>` : `<p class="help">Nothing matches.</p>`}

    <h3 class="hhead">What happened <span class="help">outages, recoveries and changes in the network checks, newest first</span></h3>
    ${ev.length ? `<ul class="timeline">${ev.slice(0, 150).map((e) => `<li class="${e.s}"><time>${esc(whenText(e.t))}</time><b>${esc(e.name)}</b> ${esc(e.text)}</li>`).join("")}</ul>`
      : `<p class="help">Nothing went down or changed in the last ${range}.</p>`}

    ${speed.length ? `<h3 class="hhead">Internet speed tests <span class="help">${speed.length} in ${range}</span></h3>${speedSvg()}
      <div class="scroll"><table class="view-table"><thead><tr><th>When</th><th>Down (Mbps)</th><th>Up (Mbps)</th><th>Ping (ms)</th></tr></thead><tbody>
      ${[...speed].reverse().slice(0, 30).map((r) => `<tr><td>${esc(whenText(r.at))}</td><td>${r.ok ? r.down_mbps ?? "" : `<span class="weak">failed</span>`}</td><td>${r.up_mbps ?? ""}</td><td>${r.ping_ms != null ? Math.round(r.ping_ms) : ""}</td></tr>`).join("")}
      </tbody></table></div>` : ""}

    <h3 class="hhead">Devices first seen <span class="help">in the last ${range}</span></h3>
    ${newDevs.length ? `<div class="scroll"><table class="view-table"><thead><tr><th>First seen</th><th>Name</th><th>Maker</th><th>IP address</th></tr></thead><tbody>
      ${newDevs.slice(0, 50).map((d) => `<tr><td>${esc(whenText(new Date(tsOf(d.first_seen)).toISOString()))}</td><td>${esc(d.name || "") || `<span class="help">(no name)</span>`}</td><td>${esc(d.vendor || "")}</td><td>${esc(d.ip || "")}</td></tr>`).join("")}
      </tbody></table></div>` : `<p class="help">No new devices in the last ${range}.</p>`}`;
}

// Cancel/Close buttons are plain buttons (so Enter in a field means Sign in / Save, not Cancel)
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-close]");
  if (b) b.closest("dialog")?.close();
});

// Wall screen: reload by itself when a new version of the page is deployed. Every minute it asks the
// server for the page files' version tags (ETag / Last-Modified, a cheap HEAD request); if any changed
// since the page loaded, it reloads, twice in a row to be sure the deploy has finished.
if (KIOSK && !VIEW) {
  const FILES = ["index.html", "app.js", "style.css", "providers.js"];
  const tag = async (f) => {
    const r = await fetch(`${f}?v=${Date.now()}`, { method: "HEAD", cache: "no-store" });
    return r.ok ? (r.headers.get("etag") || r.headers.get("last-modified") || "") : null;
  };
  let first = null, changedOnce = false;
  const check = async () => {
    try {
      const now = await Promise.all(FILES.map(tag));
      if (now.some((t) => t === null)) return;            // server busy or offline: try again later
      const key = now.join("|");
      if (first === null) { first = key; return; }
      if (key === first) { changedOnce = false; return; }
      if (changedOnce) location.reload();                  // changed on two checks in a row
      changedOnce = true;
    } catch { /* offline */ }
  };
  check();
  setInterval(check, 60_000);
}
