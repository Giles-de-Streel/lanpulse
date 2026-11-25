#!/usr/bin/env python3
"""
lanpulse collector: turns monitoring data into a standard snapshot and publishes it.

    sources  ->  snapshot (lanpulse.snapshot/1)  ->  publishers

Sources and publishers are small plug-ins registered below, so new ones (another
monitoring tool, Google Drive, S3, MQTT...) can be added without touching the rest.
Standard library only, so it runs in a stock python:3 image.

Configuration is all environment variables (see box/lanpulse.env.example).
"""
import json, os, sys, time, urllib.request, urllib.error, urllib.parse, datetime, traceback

SCHEMA = "lanpulse.snapshot/1"
INTERVAL = int(os.environ.get("LANPULSE_INTERVAL", "30"))
SITE_NAME = os.environ.get("LANPULSE_SITE_NAME", "")
SITE_DESC = os.environ.get("LANPULSE_SITE_DESC", "")


def log(*a):
    print(datetime.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def http(url, method="GET", data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- sources
# A source returns a partial snapshot: {"site": {...}, "groups": [...], "extras": {...}}

STATUS = {0: "down", 1: "up", 2: "pending", 3: "maintenance"}


def source_uptime_kuma():
    """Reads an Uptime Kuma status page through its public (no-login) API."""
    base = os.environ.get("KUMA_URL", "http://uptime-kuma:3001").rstrip("/")
    slug = os.environ.get("KUMA_STATUS_SLUG", "dashboard")
    _, page = http(f"{base}/api/status-page/{slug}")
    _, beats = http(f"{base}/api/status-page/heartbeat/{slug}")
    page, beats = json.loads(page), json.loads(beats)
    hb, up = beats.get("heartbeatList", {}), beats.get("uptimeList", {})
    groups = []
    for g in page.get("publicGroupList", []):
        items = []
        for m in g.get("monitorList", []):
            mid = str(m["id"])
            hist = hb.get(mid, [])[-30:]
            last = hist[-1] if hist else {}
            change = None
            for b in reversed(hist):            # when did it enter its current state?
                if b.get("status") != last.get("status"):
                    break
                change = b.get("time")
            items.append({
                "id": f"kuma-{mid}",
                "name": m.get("name"),
                "kind": m.get("type", "check"),
                "status": STATUS.get(last.get("status"), "unknown"),
                "latency_ms": last.get("ping"),
                "uptime_24h": up.get(f"{mid}_24"),
                "since": change,
                "message": (last.get("msg") or "")[:200],
                "history": [{"t": b.get("time"), "s": STATUS.get(b.get("status"), "unknown"),
                             "ms": b.get("ping")} for b in hist],
            })
        groups.append({"name": g.get("name"), "items": items})
    cfg = page.get("config", {})
    return {"site": {"name": cfg.get("title"), "description": cfg.get("description")},
            "groups": groups}


def kuma_targets():
    """Map Kuma monitor id -> host/IP it checks, read from Kuma's database (mounted read-only).
    Used to attach router link info (band, node, signal) to dashboard tiles."""
    import sqlite3, urllib.parse
    db = os.environ.get("KUMA_DB", "/kuma/kuma.db")
    if not os.path.exists(db):
        return {}
    try:
        con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
        rows = con.execute("SELECT id, hostname, url FROM monitor").fetchall()
        con.close()
    except Exception:
        return {}
    out = {}
    for mid, host, url in rows:
        h = host or (urllib.parse.urlparse(url).hostname if url else None)
        if h:
            out[f"kuma-{mid}"] = h
    return out


def source_netcheck():
    """Network health checks written by netcheck.py on the host (data/netcheck.json)."""
    path = os.path.join(os.environ.get("PUBLISH_FILE_DIR", "/data"), "netcheck.json")
    if not os.path.exists(path):
        return {}
    rep = json.load(open(path))
    age = time.time() - datetime.datetime.fromisoformat(rep["generated"]).timestamp()
    stale = age > 30 * 60
    items = [{"id": f"net-{c['id']}", "name": c["name"], "kind": "netcheck",
              "status": "unknown" if stale else c["status"],
              "note": ("(out of date) " if stale else "") + c["summary"], "message": c["summary"]}
             for c in rep.get("checks", [])]
    extras = dict(rep.get("extras", {}))
    extras["netcheck"] = {"generated": rep["generated"], "interface": rep.get("interface"),
                          "interface_kind": rep.get("interface_kind"), "gateway": rep.get("gateway"),
                          "note": rep.get("note")}
    return {"groups": [{"name": "Network health", "items": items}], "extras": extras}


# ------------------------------------------------------------------- router adapters
# Each adapter returns {"vendor", "nodes": [...], "clients": [...]} in this normal form:
#   node:   {name, mac, ip, model, role: router|node|ap|switch, online, firmware, clients,
#            backhaul: {type: wired|wifi, band, rssi, rate_mbps, parent} | None}
#   client: {mac, ip, name, online, link: {type: wired|wifi, band: "2.4"|"5"|"6", node, rssi, rate_mbps}}
# Configure with ROUTER_TYPE, ROUTER_URL (comma-separated for several units), ROUTER_USER,
# ROUTER_PASS, ROUTER_SITE (UniFi), ROUTER_VERIFY_TLS=0 for self-signed certificates.

import ssl, base64
from http.cookiejar import CookieJar


def _opener():
    ctx = ssl.create_default_context()
    if os.environ.get("ROUTER_VERIFY_TLS", "0") in ("0", "false", "no"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                       urllib.request.HTTPCookieProcessor(jar)), jar


def _req(opener, url, data=None, headers=None, method=None, timeout=20):
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode()
        headers = {"Content-Type": "application/json", **(headers or {})}
    elif isinstance(data, str):
        data = data.encode()
    r = opener.open(urllib.request.Request(url, data=data, headers=headers or {}, method=method), timeout=timeout)
    body = r.read()
    try:
        return json.loads(body)
    except ValueError:
        return body.decode(errors="replace")


def _band_from_mhz(mhz):
    mhz = float(mhz or 0)
    return None if not mhz else "2.4" if mhz < 3000 else "5" if mhz < 5925 else "6"


def router_asus(url, user, pw):
    """ASUS stock firmware (incl. AiMesh / ZenWiFi). EXPERIMENTAL: endpoints and field names
    follow the open-source asusrouter project; verify on your hardware. Uses the router's web
    login, so keep ROUTER_INTERVAL high (default 300 s) to avoid crowding out the web UI."""
    op, _ = _opener()
    ua = {"User-Agent": "asusrouter-Android-DUTUtil-1.0.0.245"}
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    tok = _req(op, f"{url}/login.cgi", data=f"login_authorization={auth}", headers=ua)
    if not isinstance(tok, dict) or "asus_token" not in tok:
        raise RuntimeError("ASUS login failed (check ROUTER_USER/ROUTER_PASS)")
    hdr = {**ua, "Cookie": f"asus_token={tok['asus_token']}"}
    def hook(h):
        r = _req(op, f"{url}/appGet.cgi?hook={h}", headers=hdr)
        return r if isinstance(r, dict) else {}
    try:
        cl = hook("get_clientlist()").get("get_clientlist", {})
        nodes_raw = hook("get_cfg_clientlist()").get("get_cfg_clientlist", [])
        allcl = hook("get_allclientlist()").get("get_allclientlist", {})
    finally:
        try:
            _req(op, f"{url}/Logout.asp", headers=hdr)
        except Exception:
            pass
    nodes, node_name = [], {}
    for i, n in enumerate(nodes_raw if isinstance(nodes_raw, list) else []):
        mac = (n.get("mac") or "").upper()
        name = n.get("alias") or n.get("ui_model_name") or n.get("model_name") or mac
        node_name[mac] = name
        bh = None
        if i > 0:                                   # first entry is the main router
            if n.get("pap5g") or n.get("rssi5g"):
                bh = {"type": "wifi", "band": "5", "rssi": _num(n.get("rssi5g")), "parent": n.get("pap5g")}
            elif n.get("pap2g") or n.get("rssi2g"):
                bh = {"type": "wifi", "band": "2.4", "rssi": _num(n.get("rssi2g")), "parent": n.get("pap2g")}
            else:
                bh = {"type": "wired"}
        nodes.append({"name": name, "mac": mac, "ip": n.get("ip"), "model": n.get("model_name"),
                      "role": "router" if i == 0 else "node", "online": str(n.get("online", "1")) == "1",
                      "firmware": n.get("fwver"), "backhaul": bh, "clients": 0})
    # per-node, per-band membership (most precise source)
    where = {}
    for nmac, bands in (allcl.items() if isinstance(allcl, dict) else []):
        for bkey, members in (bands.items() if isinstance(bands, dict) else []):
            band = {"2G": "2.4", "5G": "5", "5G1": "5", "5G2": "5", "6G": "6"}.get(bkey)
            typ = "wired" if "wired" in bkey.lower() else "wifi"
            for cmac, info in (members.items() if isinstance(members, dict) else []):
                where[cmac.upper()] = {"type": typ, "band": band if typ == "wifi" else None,
                                       "node": node_name.get(nmac.upper(), nmac),
                                       "rssi": _num((info or {}).get("rssi"))}
    clients = []
    for mac in cl.get("maclist", []) if isinstance(cl, dict) else []:
        c = cl.get(mac, {})
        w = c.get("isWL", "0")
        link = where.get(mac.upper()) or {
            "type": "wired" if w == "0" else "wifi", "band": {"1": "2.4", "2": "5", "3": "5", "4": "6"}.get(w),
            "node": node_name.get((c.get("amesh_papMac") or "").upper()), "rssi": _num(c.get("rssi"))}
        link["rate_mbps"] = _num(c.get("curTx"))
        clients.append({"mac": mac.upper(), "ip": c.get("ip"), "name": c.get("nickName") or c.get("name"),
                        "online": str(c.get("isOnline", "1")) == "1", "link": link})
    return {"vendor": "asus", "nodes": nodes, "clients": clients}


def router_openwrt(url, user, pw):
    """OpenWrt via ubus JSON-RPC (LuCI's rpcd). Works per access point: list several URLs to
    cover a multi-AP setup. Wireless clients come from iwinfo; everything else known to the
    router's DHCP/host hints is reported as wired (or connected elsewhere)."""
    op, _ = _opener()
    def call(sid, obj, meth, args=None):
        r = _req(op, f"{url}/ubus", data={"jsonrpc": "2.0", "id": 1, "method": "call",
                                          "params": [sid, obj, meth, args or {}]})
        res = r.get("result") if isinstance(r, dict) else None
        if not res or res[0] != 0:
            raise RuntimeError(f"ubus {obj}.{meth} failed: {r.get('error') if isinstance(r, dict) else r}")
        return res[1] if len(res) > 1 else {}
    sid = call("0" * 32, "session", "login", {"username": user or "root", "password": pw})["ubus_rpc_session"]
    board = call(sid, "system", "board")
    name = board.get("hostname") or url
    wifi = {}
    for dev in call(sid, "iwinfo", "devices").get("devices", []):
        info = call(sid, "iwinfo", "info", {"device": dev})
        band = _band_from_mhz(info.get("frequency"))
        for s in call(sid, "iwinfo", "assoclist", {"device": dev}).get("results", []):
            wifi[s["mac"].upper()] = {"type": "wifi", "band": band, "node": name, "rssi": s.get("signal"),
                                      "rate_mbps": round((s.get("tx") or {}).get("rate", 0) / 1000) or None,
                                      "ssid": info.get("ssid")}
    hosts = {}
    try:
        for mac, h in call(sid, "luci-rpc", "getHostHints").items():
            hosts[mac.upper()] = {"ip": (h.get("ipaddrs") or [None])[0], "name": h.get("name")}
    except Exception:
        pass
    clients = []
    for mac in sorted(set(hosts) | set(wifi)):
        h = hosts.get(mac, {})
        clients.append({"mac": mac, "ip": h.get("ip"), "name": h.get("name"), "online": True,
                        "link": wifi.get(mac) or {"type": "wired", "node": name}})
    node = {"name": name, "mac": None, "ip": urllib.parse.urlparse(url).hostname, "model": board.get("model"),
            "role": "ap", "online": True, "firmware": (board.get("release") or {}).get("description"),
            "backhaul": None, "clients": len(wifi)}
    return {"vendor": "openwrt", "nodes": [node], "clients": clients}


def router_unifi(url, user, pw):
    """UniFi Network (UniFi OS consoles: UDM/UCG/Cloud Key Gen2+, or the classic self-hosted
    controller). Use a local admin account; ROUTER_SITE defaults to 'default'."""
    op, _ = _opener()
    site = os.environ.get("ROUTER_SITE", "default")
    try:                                                    # UniFi OS
        _req(op, f"{url}/api/auth/login", data={"username": user, "password": pw})
        base = f"{url}/proxy/network/api/s/{site}"
    except urllib.error.HTTPError:                          # classic controller
        _req(op, f"{url}/api/login", data={"username": user, "password": pw})
        base = f"{url}/api/s/{site}"
    devs = _req(op, f"{base}/stat/device").get("data", [])
    stas = _req(op, f"{base}/stat/sta").get("data", [])
    by_mac = {d["mac"].upper(): d.get("name") or d.get("model") for d in devs}
    radio = {"ng": "2.4", "na": "5", "6e": "6"}
    nodes = []
    for d in devs:
        up = d.get("uplink") or {}
        bh = None
        if up:
            wl = up.get("type") == "wireless"
            bh = {"type": "wifi" if wl else "wired", "band": radio.get(up.get("radio")) if wl else None,
                  "rssi": up.get("rssi") or up.get("signal"), "rate_mbps": _num(up.get("speed")) or (
                      round(up["tx_rate"] / 1000) if up.get("tx_rate") else None),
                  "parent": by_mac.get((up.get("uplink_mac") or "").upper(), up.get("uplink_mac"))}
        nodes.append({"name": d.get("name") or d.get("model"), "mac": d["mac"].upper(), "ip": d.get("ip"),
                      "model": d.get("model"), "online": d.get("state") == 1, "firmware": d.get("version"),
                      "role": {"uap": "ap", "usw": "switch", "ugw": "router", "udm": "router", "uxg": "router"}.get(d.get("type"), "node"),
                      "backhaul": bh, "clients": d.get("num_sta")})
    clients = []
    for s in stas:
        wired = s.get("is_wired", False)
        clients.append({"mac": s["mac"].upper(), "ip": s.get("ip"), "name": s.get("name") or s.get("hostname"),
                        "online": True, "link": {
                            "type": "wired" if wired else "wifi", "band": None if wired else radio.get(s.get("radio")),
                            "node": by_mac.get((s.get("sw_mac") if wired else s.get("ap_mac") or "").upper()),
                            "rssi": None if wired else s.get("signal"),
                            "rate_mbps": round(s["tx_rate"] / 1000) if s.get("tx_rate") else None,
                            "ssid": s.get("essid")}})
    return {"vendor": "unifi", "nodes": nodes, "clients": clients}


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


ROUTERS = {"asus": router_asus, "openwrt": router_openwrt, "unifi": router_unifi}
_router_cache = {"t": 0, "data": None}


def source_router():
    """Connection type / node / signal for every client, and mesh/AP topology with backhaul."""
    kind = os.environ.get("ROUTER_TYPE", "").strip().lower()
    if not kind or kind == "none":
        return {}
    if time.time() - _router_cache["t"] < int(os.environ.get("ROUTER_INTERVAL", "300")) and _router_cache["data"]:
        data = _router_cache["data"]
    else:
        data = {"vendor": kind, "nodes": [], "clients": []}
        for url in [u.strip().rstrip("/") for u in os.environ.get("ROUTER_URL", "").split(",") if u.strip()]:
            part = ROUTERS[kind](url, os.environ.get("ROUTER_USER", ""), os.environ.get("ROUTER_PASS", ""))
            data["nodes"] += part["nodes"]
            data["clients"] += part["clients"]
        _router_cache.update(t=time.time(), data=data)
    weak = int(os.environ.get("ROUTER_WEAK_RSSI", "-70"))
    items = []
    for n in data["nodes"]:
        bh = n.get("backhaul")
        if n["role"] == "router":
            note = "Main router"
        elif not bh:
            note = n["role"].capitalize()
        elif bh["type"] == "wired":
            note = "Wired backhaul"
        else:
            note = f"Wi-Fi backhaul {bh.get('band') or ''} GHz".replace("  ", " ") + \
                   (f", {bh['rssi']:.0f} dBm" if bh.get("rssi") is not None else "") + \
                   (f", {bh['rate_mbps']:.0f} Mbps" if bh.get("rate_mbps") else "")
        weak_bh = bool(bh and bh["type"] == "wifi" and bh.get("rssi") is not None and bh["rssi"] < weak)
        if n.get("clients") is not None:
            note += f" · {n['clients']:.0f} clients"
        items.append({"id": f"node-{n.get('mac') or n.get('ip')}", "name": n["name"], "kind": n["role"],
                      "status": "down" if not n["online"] else "warn" if weak_bh else "up",
                      "note": note + (" (weak backhaul)" if weak_bh else ""),
                      "link": bh})
    bands = {}
    for c in data["clients"]:
        k = "wired" if c["link"]["type"] == "wired" else f"{c['link'].get('band') or '?'} GHz"
        bands[k] = bands.get(k, 0) + 1
    return {"groups": [{"name": "Network equipment", "items": items}] if items else [],
            "extras": {"topology": {"vendor": data["vendor"], "nodes": data["nodes"]},
                       "clients": data["clients"], "client_bands": bands}}


SOURCES = {"uptime-kuma": source_uptime_kuma, "netcheck": source_netcheck, "router": source_router}


# ------------------------------------------------------------------------ publishers
# A publisher receives the finished snapshot (dict) and its JSON bytes.

def publish_file(snap, body):
    """Always on: writes data/snapshot.json for the dashboard served by the box."""
    d = os.environ.get("PUBLISH_FILE_DIR", "/data")
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".snapshot.json.tmp")
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, os.path.join(d, "snapshot.json"))


def publish_http_put(snap, body):
    """PUT the snapshot to any URL: WebDAV/Nextcloud, an S3/R2 presigned URL, a webhook...
    PUBLISH_HTTP_URL, optional PUBLISH_HTTP_METHOD (PUT), PUBLISH_HTTP_AUTH (header value)."""
    url = os.environ["PUBLISH_HTTP_URL"]
    hdr = {"Content-Type": "application/json"}
    if os.environ.get("PUBLISH_HTTP_AUTH"):
        hdr["Authorization"] = os.environ["PUBLISH_HTTP_AUTH"]
    http(url, method=os.environ.get("PUBLISH_HTTP_METHOD", "PUT"), data=body, headers=hdr)


def publish_github_gist(snap, body):
    """Update snapshot.json in a GitHub Gist (free, no server). GIST_ID + GIST_TOKEN (gist scope).
    Read it back from https://gist.githubusercontent.com/<user>/<GIST_ID>/raw/snapshot.json
    Note: a secret gist is unlisted, not private. Anyone with the URL can read it."""
    gid, tok = os.environ["GIST_ID"], os.environ["GIST_TOKEN"]
    payload = json.dumps({"files": {"snapshot.json": {"content": body.decode()}}}).encode()
    http(f"https://api.github.com/gists/{gid}", method="PATCH", data=payload,
         headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                  "User-Agent": "lanpulse-collector"})


PUBLISHERS = {"file": publish_file, "http": publish_http_put, "gist": publish_github_gist}


# ---------------------------------------------------------------------------- main

def build_snapshot():
    snap = {"schema": SCHEMA, "generated": now_iso(), "sources": [], "site": {},
            "groups": [], "extras": {}}
    for name in os.environ.get("LANPULSE_SOURCES", "uptime-kuma,netcheck,router").split(","):
        name = name.strip()
        if not name:
            continue
        try:
            part = SOURCES[name]()
            snap["sources"].append({"name": name, "ok": True})
            for k, v in (part.get("site") or {}).items():
                snap["site"].setdefault(k, v)
            snap["groups"] += part.get("groups", [])
            snap["extras"].update(part.get("extras", {}))
        except Exception as e:
            snap["sources"].append({"name": name, "ok": False, "error": str(e)[:200]})
            log(f"source {name} failed: {e}")
    if SITE_NAME:
        snap["site"]["name"] = SITE_NAME
    if SITE_DESC:
        snap["site"]["description"] = SITE_DESC
    attach_links(snap)
    counts = {}
    for g in snap["groups"]:
        for i in g["items"]:
            counts[i["status"]] = counts.get(i["status"], 0) + 1
    snap["summary"] = {"total": sum(counts.values()), **{s: counts.get(s, 0) for s in
                       ("up", "down", "warn", "pending", "maintenance", "unknown")}}
    return snap


def attach_links(snap):
    """Give each monitored device its connection details (wired / band / node / signal) when a
    router adapter knows them, matching the monitor's target IP or hostname to the router's
    client list. Also uses NetAlertX names when the router has none."""
    clients = snap["extras"].get("clients") or []
    if not clients:
        return
    by_ip = {c["ip"]: c for c in clients if c.get("ip")}
    by_name = {(c.get("name") or "").lower(): c for c in clients if c.get("name")}
    targets = kuma_targets()
    for g in snap["groups"]:
        for i in g["items"]:
            host = targets.get(i["id"])
            c = by_ip.get(host) or by_name.get((host or "").lower())
            if c and not i.get("link"):
                i["link"] = c["link"]


def run_once():
    snap = build_snapshot()
    body = json.dumps(snap, separators=(",", ":")).encode()
    names = ["file"] + [p.strip() for p in os.environ.get("LANPULSE_PUBLISHERS", "").split(",")
                        if p.strip() and p.strip() != "file"]
    for p in names:
        try:
            PUBLISHERS[p](snap, body)
        except Exception as e:
            log(f"publisher {p} failed: {e}")
    return snap


if __name__ == "__main__":
    once = "--once" in sys.argv
    log(f"lanpulse collector: sources={os.environ.get('LANPULSE_SOURCES', 'uptime-kuma')} "
        f"publishers=file{','+os.environ['LANPULSE_PUBLISHERS'] if os.environ.get('LANPULSE_PUBLISHERS') else ''} "
        f"every {INTERVAL}s")
    while True:
        try:
            s = run_once()
            if once:
                print(json.dumps(s["summary"]))
                break
        except Exception:
            traceback.print_exc()
            if once:
                sys.exit(1)
        time.sleep(INTERVAL)
