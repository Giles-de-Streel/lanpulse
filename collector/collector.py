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
              "note": ("(out of date) " if stale else "") + c["summary"], "message": c["summary"],
              "hint": c.get("hint"), "priority": c.get("priority")}
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


def router_tmobile(url, user, pw):
    """T-Mobile Home Internet gateways (Arcadyan, Sagemcom, Nokia) via the local /TMI/v1 API.
    Needs the gateway admin password (the one used in the T-Life / T-Mobile Internet app);
    ROUTER_USER defaults to admin. Reports each client as wired, 2.4, 5 or 6 GHz with signal."""
    op, _ = _opener()
    tok = _req(op, f"{url}/TMI/v1/auth/login", data={"username": user or "admin", "password": pw})
    token = (tok.get("auth") or {}).get("token") if isinstance(tok, dict) else None
    if not token:
        raise RuntimeError("T-Mobile gateway login failed (check ROUTER_PASS)")
    hdr = {"Authorization": f"Bearer {token}"}
    tel = _req(op, f"{url}/TMI/v1/network/telemetry?get=clients", headers=hdr)
    gw = _req(op, f"{url}/TMI/v1/gateway?get=all") or {}
    dev = gw.get("device", {}) if isinstance(gw, dict) else {}
    name = f"{dev.get('manufacturer', '')} {dev.get('model', 'T-Mobile gateway')}".strip()
    clients = []
    groups = (tel or {}).get("clients", {}) if isinstance(tel, dict) else {}
    shape = {}                                   # group name -> field names (no values), for diagnosing firmware variants
    for key, members in groups.items():
        k = key.lower()
        wired = "eth" in k or "lan" in k
        band = None if wired else "2.4" if k.startswith("2") else "6" if k.startswith("6") else "5" if k.startswith("5") else None
        shape[key] = sorted({f for c in (members or []) if isinstance(c, dict) for f in c})
        for c in members or []:
            rssi = _num(c.get("signal"))
            if rssi is not None and (rssi >= 0 or rssi <= -120):   # 0 / -200 = not reported
                rssi = None
            clients.append({"mac": (c.get("mac") or "").upper(), "ip": c.get("ipv4"), "name": c.get("name"),
                            "online": c.get("connected", True),
                            "link": {"type": "wired" if wired else "wifi", "band": band, "node": name,
                                     "rssi": rssi, "group": key}})
    node = {"name": name, "mac": (dev.get("macId") or "").upper() or None, "ip": urllib.parse.urlparse(url).hostname,
            "model": dev.get("model"), "role": "router", "online": True, "firmware": dev.get("softwareVersion"),
            "backhaul": None, "clients": len(clients), "client_groups": shape}
    return {"vendor": "tmobile", "nodes": [node], "clients": clients}


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


ROUTERS = {"asus": router_asus, "openwrt": router_openwrt, "unifi": router_unifi, "tmobile": router_tmobile}

# What the dashboard's "Router connection" form shows for each type: plain-language help on
# where the login comes from, and sensible defaults.
ROUTER_INFO = {
    "tmobile": {"label": "T-Mobile Home Internet gateway", "user": "admin", "url": "http://{gw}",
                "help": "The gateway's admin password: the one the T-Mobile Internet / T-Life app uses. "
                        "Unless it was changed, it's on the sticker on the gateway (not the Wi-Fi password)."},
    "asus": {"label": "ASUS router / AiMesh / ZenWiFi", "user": "admin", "url": "http://{gw}",
             "help": "The router's web login (the one for router.asus.com or the ASUS Router app). "
                     "Use the main router's address; mesh nodes are found automatically. Experimental."},
    "openwrt": {"label": "OpenWrt", "user": "root", "url": "http://{gw}",
                "help": "The LuCI login. For several access points, list each address separated by commas."},
    "unifi": {"label": "UniFi (UDM, Cloud Gateway, Cloud Key or controller)", "user": "", "url": "https://{gw}",
              "help": "A local admin account on the UniFi console (not a ui.com cloud account). "
                      "A read-only 'View only' local user is enough."},
}
_router_cache = {"t": 0, "data": None, "error": None, "key": None}

CONFIG_DIR = os.environ.get("LANPULSE_CONFIG_DIR", "/config")
SETTINGS = os.path.join(CONFIG_DIR, "settings.json")


def load_settings():
    try:
        return json.load(open(SETTINGS))
    except (OSError, ValueError):
        return {}


def save_settings(s):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = SETTINGS + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, SETTINGS)


def router_config():
    """Router settings: saved from the dashboard (config/settings.json) win over lanpulse.env."""
    r = load_settings().get("router") or {}
    cfg = {"type": os.environ.get("ROUTER_TYPE", ""), "url": os.environ.get("ROUTER_URL", ""),
           "user": os.environ.get("ROUTER_USER", ""), "password": os.environ.get("ROUTER_PASS", ""),
           "source": "lanpulse.env"}
    if r:
        cfg.update({k: r.get(k, "") for k in ("type", "url", "user", "password")}, source="dashboard")
    cfg["type"] = (cfg["type"] or "").strip().lower()
    return cfg


def fetch_router(cfg):
    kind = cfg["type"]
    if kind not in ROUTERS:
        raise RuntimeError(f"unknown router type '{kind}'")
    urls = [u.strip().rstrip("/") for u in (cfg["url"] or "").split(",") if u.strip()]
    if not urls:
        raise RuntimeError("no router address set")
    if not cfg["password"]:
        raise RuntimeError("no password saved yet")
    data = {"vendor": kind, "nodes": [], "clients": []}
    for url in urls:
        part = ROUTERS[kind](url, cfg["user"], cfg["password"])
        data["nodes"] += part["nodes"]
        data["clients"] += part["clients"]
    return data


def router_status():
    cfg = router_config()
    return {"type": cfg["type"] or None, "url": cfg["url"] or None, "user": cfg["user"] or None,
            "has_password": bool(cfg["password"]), "source": cfg["source"],
            "ok": _router_cache["data"] is not None and not _router_cache["error"],
            "error": _router_cache["error"], "checked": _router_cache["t"] or None}


def source_router():
    """Connection type / node / signal for every client, and mesh/AP topology with backhaul."""
    cfg = router_config()
    kind = cfg["type"]
    if not kind or kind == "none":
        _router_cache.update(data=None, error=None)
        return {"extras": {"router": router_status()}}
    key = (kind, cfg["url"], cfg["user"], cfg["password"])
    interval = int(os.environ.get("ROUTER_INTERVAL", "300"))
    if key == _router_cache["key"] and time.time() - _router_cache["t"] < interval:
        if _router_cache["error"]:                       # back off after a failed login, don't hammer
            return {"extras": {"router": router_status()}}
        data = _router_cache["data"]
    else:
        try:
            data = fetch_router(cfg)
            _router_cache.update(t=time.time(), data=data, error=None, key=key)
        except Exception as e:
            _router_cache.update(t=time.time(), data=None, error=str(e)[:200], key=key)
            log(f"router: {e}")
            return {"extras": {"router": router_status()}}
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
        k = "wired" if c["link"]["type"] == "wired" else (f"{c['link']['band']} GHz" if c['link'].get('band') else "Wi-Fi (band not reported)")
        bands[k] = bands.get(k, 0) + 1
    groups = [{"name": "Network equipment", "items": items}] if items else []
    weak_item, weak_list = weak_wifi(data["clients"])
    if weak_item:
        groups.append({"name": "Network health", "items": [weak_item]})
    return {"groups": groups,
            "extras": {"topology": {"vendor": data["vendor"], "nodes": data["nodes"]},
                       "clients": data["clients"], "client_bands": bands, "weak_wifi": weak_list,
                       "router": router_status()}}


def weak_wifi(clients):
    """Devices whose Wi-Fi signal (as the router hears them) is poor. Signal guide:
    -50 or better excellent, to -67 good, to -70 fair (video calls OK), to -80 weak, below -80 poor."""
    poor_at = int(os.environ.get("ROUTER_POOR_RSSI", "-80"))
    weak_at = int(os.environ.get("ROUTER_WEAK_RSSI", "-70"))
    wifi = [c for c in clients if c["link"]["type"] == "wifi" and c.get("online", True)
            and c["link"].get("rssi") is not None]
    if not wifi:
        return None, []
    def label(c):
        return c.get("name") or c.get("ip") or c.get("mac")
    poor = sorted([c for c in wifi if c["link"]["rssi"] < poor_at], key=lambda c: c["link"]["rssi"])
    weak = sorted([c for c in wifi if poor_at <= c["link"]["rssi"] < weak_at], key=lambda c: c["link"]["rssi"])
    listed = [{"name": label(c), "ip": c.get("ip"), "mac": c.get("mac"), "rssi": c["link"]["rssi"],
               "band": c["link"].get("band"), "node": c["link"].get("node"),
               "level": "poor" if c in poor else "weak"} for c in poor + weak]
    def names(cs, n=6):
        s = ", ".join(f"{label(c)} ({c['link']['rssi']:.0f} dBm)" for c in cs[:n])
        return s + (f" and {len(cs) - n} more" if len(cs) > n else "")
    if poor:
        summary = f"{len(poor)} of {len(wifi)} Wi-Fi devices have a poor signal: {names(poor)}"
        if weak:
            summary += f". {len(weak)} more are weak but usable"
        status = "warn"
    elif weak:
        summary = f"All {len(wifi)} Wi-Fi devices connect; {len(weak)} have a weak signal: {names(weak)}"
        status = "up"
    else:
        summary = f"All {len(wifi)} Wi-Fi devices have a good signal"
        status = "up"
    hint = ("Devices far from the router or behind walls drop out and slow down. Try moving the router more "
            "centrally and up high, away from metal and thick walls; add a mesh point or access point near "
            "the weak devices; and use a cable for things that never move (TVs, PCs, game consoles). "
            "Phones and cars that are leaving or just outside can show up here briefly.")
    item = {"id": "net-weak-wifi", "name": "Wi-Fi signal of devices", "kind": "netcheck", "status": status, "priority": "low",
            "note": summary, "message": summary, "hint": hint if status != "up" else None}
    return item, listed


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
            for g in part.get("groups", []):           # sources can add to the same group
                same = next((x for x in snap["groups"] if x["name"] == g["name"]), None)
                if same:
                    same["items"] += g["items"]
                else:
                    snap["groups"].append(g)
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
        # lower-priority findings (new devices, weak Wi-Fi) go to the end of their group
        g["items"].sort(key=lambda i: i.get("priority") == "low")
        for i in g["items"]:
            # a "warn" marked low priority is a note: shown amber, but not headline news
            s = "note" if i["status"] == "warn" and i.get("priority") == "low" else i["status"]
            counts[s] = counts.get(s, 0) + 1
    snap["summary"] = {"total": sum(counts.values()), **{s: counts.get(s, 0) for s in
                       ("up", "down", "warn", "note", "pending", "maintenance", "unknown")}}
    try:
        from history import history as build_history
        snap["extras"]["history"] = build_history(snap, log)
    except Exception as e:
        log(f"history failed: {e}")
    # keep Network health above the router's equipment list, and lower-priority groups last
    order = {"Network health": 1, "Network equipment": 2}
    snap["groups"].sort(key=lambda g: order.get(g["name"], 0))
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


# ---------------------------------------------------------------------- settings API
# A tiny local API the dashboard uses to set up things like the router login, so nobody needs a
# console. Served only on the box (Caddy forwards /api/* to it, from private addresses only).
# Rules: every change needs an administrator sign-in (the password is created in the dashboard on
# first use and stored only as a salted PBKDF2 hash); saved router passwords are write-only (never
# sent back); changing the router type or address clears the saved password unless a new one is
# given, so a stored login can't be redirected to another device.
# Forgotten administrator password: delete the "admin" entry from /opt/monitor/config/settings.json
# (or the whole file) and restart lanpulse-collector; the dashboard then offers to create a new one.

import threading, hashlib, hmac, secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie

WAKE = threading.Event()
_last_test = {"t": 0.0}
SESSIONS = {}                                   # token -> expiry (epoch); in memory, cleared on restart
SESSION_HOURS = 12
_login_fail = {"n": 0, "t": 0.0}


def hash_password(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return {"salt": salt, "hash": h, "algo": "pbkdf2-sha256-200k"}


def check_password(pw, rec):
    if not rec:
        return False
    return hmac.compare_digest(hash_password(pw, rec["salt"])["hash"], rec["hash"])


def gateway_ip():
    try:
        return json.load(open(os.path.join(os.environ.get("PUBLISH_FILE_DIR", "/data"), "netcheck.json"))).get("gateway")
    except (OSError, ValueError):
        return None


class SettingsAPI(BaseHTTPRequestHandler):
    server_version = "lanpulse"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, obj, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- sessions
    def _token(self):
        try:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c["lp_session"].value if "lp_session" in c else None
        except Exception:
            return None

    def _signed_in(self):
        t = self._token()
        now = time.time()
        for k in [k for k, exp in SESSIONS.items() if exp < now]:
            SESSIONS.pop(k, None)
        return bool(t and t in SESSIONS)

    def _new_session(self):
        t = secrets.token_urlsafe(32)
        SESSIONS[t] = time.time() + SESSION_HOURS * 3600
        return f"lp_session={t}; Path=/api; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS * 3600}"

    def _session_info(self):
        return {"admin_set": bool(load_settings().get("admin")), "signed_in": self._signed_in()}

    def _info(self):
        gw = gateway_ip() or "192.168.1.1"
        types = [{"id": k, **{**v, "url": v["url"].format(gw=gw)}} for k, v in ROUTER_INFO.items()]
        try:
            from history import ntfy_targets
            alerts = ntfy_targets()
        except Exception as e:
            log(f"ntfy lookup failed: {e}")
            alerts = []
        return {"router": router_status(), "router_types": types, "gateway": gw, "alerts": alerts}

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except ValueError:
            return None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/session":
            return self._send(200, self._session_info())
        if path == "/api/settings":
            if not self._signed_in():
                return self._send(401, {"error": "Sign in as administrator first", **self._session_info()})
            return self._send(200, {**self._info(), **self._session_info()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        req = self._body()
        if req is None:
            return self._send(400, {"error": "bad request"})
        if path == "/api/admin/setup":                       # first-time: create the admin password
            s = load_settings()
            if s.get("admin"):
                return self._send(409, {"error": "An administrator password already exists. Sign in instead."})
            pw = str(req.get("password") or "")
            if len(pw) < 8:
                return self._send(400, {"error": "Use at least 8 characters"})
            s["admin"] = hash_password(pw)
            save_settings(s)
            log("administrator password created from the dashboard")
            return self._send(200, {"ok": True, "admin_set": True, "signed_in": True}, cookie=self._new_session())
        if path == "/api/login":
            if _login_fail["n"] >= 5 and time.time() - _login_fail["t"] < 60:
                return self._send(429, {"error": "Too many wrong tries. Wait a minute and try again."})
            if check_password(str(req.get("password") or ""), load_settings().get("admin")):
                _login_fail["n"] = 0
                return self._send(200, {"ok": True, "admin_set": True, "signed_in": True}, cookie=self._new_session())
            _login_fail.update(n=_login_fail["n"] + 1, t=time.time())
            time.sleep(1)
            return self._send(403, {"error": "That password isn't right"})
        if path == "/api/logout":
            SESSIONS.pop(self._token(), None)
            return self._send(200, {"ok": True, "signed_in": False},
                              cookie="lp_session=; Path=/api; HttpOnly; SameSite=Strict; Max-Age=0")
        if path == "/api/admin/password":                    # change the admin password (signed in)
            if not self._signed_in():
                return self._send(401, {"error": "Sign in as administrator first"})
            if not check_password(str(req.get("current") or ""), load_settings().get("admin")):
                time.sleep(1)
                return self._send(403, {"error": "The current password isn't right"})
            pw = str(req.get("password") or "")
            if len(pw) < 8:
                return self._send(400, {"error": "Use at least 8 characters"})
            s = load_settings()
            s["admin"] = hash_password(pw)
            save_settings(s)
            SESSIONS.clear()
            return self._send(200, {"ok": True}, cookie=self._new_session())
        if path == "/api/alerts/test":                       # send a test push to each ntfy topic (signed in)
            if not self._signed_in():
                return self._send(401, {"error": "Sign in as administrator first"})
            if time.time() - _last_test["t"] < 10:
                return self._send(429, {"error": "One moment: wait a few seconds between tests"})
            _last_test["t"] = time.time()
            from history import ntfy_targets
            sent, skipped = [], []
            for t in ntfy_targets():
                if not t["active"] or t["login"]:
                    skipped.append(t["topic"])
                    continue
                try:
                    http(f"{t['server']}/{urllib.parse.quote(t['topic'])}", method="POST",
                         data="Test alert from the network monitor. If you see this, phone alerts work.".encode(),
                         headers={"Title": "lanpulse test", "Tags": "white_check_mark"})
                    sent.append(t["topic"])
                except Exception as e:
                    return self._send(502, {"error": f"Couldn't send: {str(e)[:120]}"})
            if not sent:
                return self._send(400, {"error": "No phone-alert topic to test" + (" (it needs a login: send a test from Uptime Kuma)" if skipped else "")})
            return self._send(200, {"ok": True, "sent": len(sent)})
        if path != "/api/router":
            return self._send(404, {"error": "not found"})
        if not self._signed_in():
            return self._send(401, {"error": "Sign in as administrator first"})
        action = req.get("action", "save")
        if action == "remove":
            s = load_settings()
            s["router"] = {"type": "none"}
            save_settings(s)
            _router_cache.update(t=0, data=None, error=None, key=None)
            WAKE.set()
            return self._send(200, {"ok": True, **self._info()})
        kind = str(req.get("type", "")).strip().lower()
        if kind not in ROUTERS:
            return self._send(400, {"error": "Choose a router type"})
        cur = router_config()
        url = str(req.get("url", "")).strip()
        user = str(req.get("user", "")).strip()
        password = req.get("password") or ""
        if not password and kind == cur["type"] and url == cur["url"]:
            password = cur["password"]           # keep the saved one when only re-testing / renaming
        cfg = {"type": kind, "url": url, "user": user, "password": password}
        if time.time() - _last_test["t"] < 3:
            return self._send(429, {"error": "One moment, still busy with the last try"})
        _last_test["t"] = time.time()
        try:
            data = fetch_router(cfg)
        except urllib.error.HTTPError as e:
            msg = "the router refused the login (check the password)" if e.code in (401, 403) else f"router answered HTTP {e.code}"
            return self._send(200, {"ok": False, "error": f"Couldn't connect: {msg}"})
        except Exception as e:
            return self._send(200, {"ok": False, "error": f"Couldn't connect: {str(e)[:200]}"})
        bands = {}
        for c in data["clients"]:
            k = "wired" if c["link"]["type"] == "wired" else (f"{c['link']['band']} GHz" if c['link'].get('band') else "Wi-Fi (band not reported)")
            bands[k] = bands.get(k, 0) + 1
        out = {"ok": True, "nodes": len(data["nodes"]), "clients": len(data["clients"]), "client_bands": bands}
        if action == "save":
            s = load_settings()
            s["router"] = cfg
            save_settings(s)
            _router_cache.update(t=time.time(), data=data, error=None, key=(kind, url, user, password))
            WAKE.set()
            out["saved"] = True
        self._send(200, out)


def start_api():
    port = int(os.environ.get("LANPULSE_API_PORT", "8081"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), SettingsAPI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"settings API on :{port}")


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
    if not once and os.environ.get("LANPULSE_API", "1") != "0":
        start_api()
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
        WAKE.wait(INTERVAL)
        WAKE.clear()
