#!/usr/bin/env python3
"""
lanpulse netswitch: lets the box move between networks (home, office, church, a friend's house)
and adapt by itself. Runs on the host as root:

  - from NetworkManager whenever a connection comes up or changes (dispatcher script, setup.sh),
  - at the start of every netcheck run (every 10 minutes), as a safety net,
  - with --requests, from a systemd path unit, to carry out changes asked for in the dashboard.

Each network is recognised by its router's MAC address, so two sites that both use 192.168.1.x
are still told apart. Per network it keeps:
  - a name (shown as the dashboard title),
  - its own device list in Uptime Kuma (monitors tagged lanpulse-network=<id>; the other networks'
    monitors are paused, not deleted, so their history is kept for when the box comes back),
  - its own learned baselines (netcheck state: public IP, Wi-Fi access points, gateway uptime),
  - the time periods the box spent there (speed-test "normal" and new-device detection use them),
  - its own router login (kept by the collector in config/settings.json, keyed by network id).

On a network it hasn't seen before, it starts a small starter device list (router, internet, name
lookups) and the dashboard's admin view offers to load or build the real one.

Device discovery (NetAlertX) is pointed at whatever subnets the box is on, on every run.

Files:
  config/networks.json             networks and periods (root only, never served)
  config/networks/<id>/            per-network netcheck state
  config/requests/*.req -> *.res   dashboard requests (written by the collector's settings API)
  data/network.json                the current network, without secrets (served to the dashboard)

Standard library only.  Usage:  sudo python3 netswitch.py [--requests] [--print]
"""
import fcntl, hashlib, ipaddress, json, os, re, shutil, socket, sqlite3, subprocess, sys, time

APP = os.environ.get("APP", "/opt/monitor")
CONF = os.path.join(APP, "config")
NETS = os.path.join(CONF, "networks.json")
NETDIR = os.path.join(CONF, "networks")
REQ = os.path.join(CONF, "requests")
DATA = os.path.join(APP, "data")
PUBLIC = os.path.join(DATA, "network.json")
KUMA_DB = os.path.join(APP, "uptime-kuma", "kuma.db")
NETALERTX_CONF = os.path.join(APP, "netalertx", "config", "app.conf")
TAG = "lanpulse-network"
SKIP_IFACES = ("lo", "docker", "br-", "veth", "virbr", "tun", "tap", "wg", "tailscale", "zt")
MAX_SCAN_PREFIX = 22       # scan at most a /22 (1,024 addresses) around the box on big networks
MONITOR_TYPES = ("ping", "http", "port", "dns")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def run(cmd, timeout=30, **kw):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out"


def write_json(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------------ detection

def default_route():
    """(gateway, interface) of the preferred default route: lowest metric, so a cable wins over Wi-Fi."""
    _, out = run(["ip", "-4", "route", "show", "default"])
    best = None
    for line in out.splitlines():
        m = re.search(r"default via (\S+) dev (\S+)", line)
        if not m:
            continue
        metric = re.search(r"metric (\d+)", line)
        cand = (int(metric.group(1)) if metric else 0, m.group(1), m.group(2))
        if best is None or cand < best:
            best = cand
    return (best[1], best[2]) if best else (None, None)


def iface_kind(dev):
    return "Wi-Fi" if dev and (dev.startswith("wl") or os.path.isdir(f"/sys/class/net/{dev}/wireless")) else "wired"


def addresses():
    """[(interface, ip_interface)] for every global IPv4 address on a real (non-container) interface."""
    _, out = run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    res = []
    for line in out.splitlines():
        m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\S+)", line)
        if not m:
            continue
        dev = m.group(1).split("@")[0]
        if dev.startswith(SKIP_IFACES):
            continue
        try:
            res.append((dev, ipaddress.ip_interface(m.group(2))))
        except ValueError:
            pass
    return res


def gateway_mac(gw, dev):
    for attempt in range(3):
        _, out = run(["ip", "neigh", "show", gw, "dev", dev])
        m = re.search(r"lladdr ([0-9a-f:]{17})", out)
        if m:
            return m.group(1).lower()
        run(["ping", "-n", "-c", "1", "-W", "1", gw], timeout=5)
    return None


def wifi_ssid(dev):
    _, out = run(["iw", "dev", dev, "link"])
    m = re.search(r"^\s*SSID: (.+)$", out, re.M)
    return m.group(1).strip() if m else None


def detect(wait=20):
    """The network the box is on now, or None when it has no default route (unplugged / no Wi-Fi)."""
    deadline = time.time() + wait
    gw, dev = default_route()
    while not gw and time.time() < deadline:
        time.sleep(2)
        gw, dev = default_route()
    if not gw:
        return None
    addr = next((a for d, a in addresses() if d == dev), None)
    subnet = str(addr.network) if addr else None
    mac = gateway_mac(gw, dev)
    kind = iface_kind(dev)
    ssid = wifi_ssid(dev) if kind == "Wi-Fi" else None
    if mac:
        nid = "n" + hashlib.sha1(mac.encode()).hexdigest()[:10]
    else:                                  # router didn't answer ARP: fall back to its address and subnet
        nid = "g" + hashlib.sha1(f"{gw}|{subnet}".encode()).hexdigest()[:10]
    return {"id": nid, "gateway": gw, "gateway_mac": mac, "interface": dev, "kind": kind,
            "subnet": subnet, "address": str(addr.ip) if addr else None, "ssid": ssid}


def default_name(cur):
    return f"Network {cur['subnet'] or cur['gateway']}" + (f" ({cur['ssid']})" if cur.get("ssid") else "")


def clean_name(s, fallback):
    """Network names end up in the dashboard title and in /opt/monitor/.env: plain text only."""
    s = re.sub(r"[\x00-\x1f\"`$\\<>]", "", str(s or "")).strip()
    return s[:80] or fallback


# ------------------------------------------------------------------ NetAlertX (device discovery)

def scan_targets():
    """NetAlertX SCAN_SUBNETS entries for every subnet the box is on (big networks capped to a /22)."""
    out, seen = [], set()
    for dev, a in addresses():
        net = a.network
        if net.prefixlen < MAX_SCAN_PREFIX:
            net = ipaddress.ip_interface(f"{a.ip}/{MAX_SCAN_PREFIX}").network
        key = (str(net), dev)
        if key not in seen:
            seen.add(key)
            out.append(f"{net} --interface={dev}")
    return out


def update_netalertx():
    """Point NetAlertX at the current subnets. Returns True when it had to change (and restart)."""
    targets = scan_targets()
    if not targets or not os.path.exists(NETALERTX_CONF):
        return False
    with open(NETALERTX_CONF) as f:
        text = f.read()
    line = "SCAN_SUBNETS=" + repr(targets)
    if re.search(r"^SCAN_SUBNETS=", text, re.M):
        new = re.sub(r"^SCAN_SUBNETS=.*$", lambda _: line, text, count=1, flags=re.M)
    else:
        new = text.rstrip("\n") + "\n" + line + "\n"
    if new == text:
        return False
    st = os.stat(NETALERTX_CONF)
    shutil.copy2(NETALERTX_CONF, NETALERTX_CONF + ".bak")
    tmp = NETALERTX_CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write(new)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, NETALERTX_CONF)
    run(["docker", "restart", "netalertx"], timeout=120)
    log(f"NetAlertX now scans: {', '.join(targets)}")
    return True


# ------------------------------------------------------------------ Uptime Kuma (device lists)

class Kuma:
    """Edits Uptime Kuma's database while it is stopped (backed up first), then starts it again.
    Kuma only reads its monitor list at start-up, so changes need the stop/start anyway."""

    def __init__(self):
        self.con = None

    def __enter__(self):
        if not os.path.exists(KUMA_DB):
            raise RuntimeError("Uptime Kuma isn't set up yet (open it once and create the admin account)")
        try:
            con = sqlite3.connect(f"file:{KUMA_DB}?mode=ro", uri=True, timeout=10)
            users = con.execute("SELECT count(*) FROM user").fetchone()[0]
            con.close()
        except sqlite3.Error as e:
            raise RuntimeError(f"Uptime Kuma's database isn't ready: {e}")
        if not users:
            raise RuntimeError("Uptime Kuma has no admin account yet (open port 3001 and create it)")
        run(["docker", "compose", "stop", "uptime-kuma"], timeout=120, cwd=APP)
        shutil.copy2(KUMA_DB, f"{KUMA_DB}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        self._prune_backups()
        self.con = sqlite3.connect(KUMA_DB, timeout=30)
        self.con.execute("PRAGMA foreign_keys=ON")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.con:
                if exc_type:
                    self.con.rollback()
                else:
                    self.con.commit()
                self.con.close()
        finally:
            run(["docker", "compose", "start", "uptime-kuma"], timeout=120, cwd=APP)
        return False

    def _prune_backups(self):
        d = os.path.dirname(KUMA_DB)
        baks = sorted(f for f in os.listdir(d) if f.startswith("kuma.db.bak-"))
        for f in baks[:-10]:                       # keep the newest 10
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass

    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def tag_id(self):
        r = self.q("SELECT id FROM tag WHERE name=?", (TAG,))
        if r:
            return r[0][0]
        cur = self.con.execute("INSERT INTO tag (name, color) VALUES (?, ?)", (TAG, "#0f766e"))
        return cur.lastrowid

    def owners(self):
        """monitor id -> network id, for monitors tagged with a network."""
        t = self.tag_id()
        return {m: v for m, v in self.q("SELECT monitor_id, value FROM monitor_tag WHERE tag_id=?", (t,))}

    def claim_untagged(self, nid):
        """Monitors made outside lanpulse (Kuma's own UI, kuma-apply.sh) belong to the network
        that was current when they were made."""
        t = self.tag_id()
        self.con.execute("INSERT INTO monitor_tag (monitor_id, tag_id, value) SELECT id, ?, ? FROM monitor "
                         "WHERE type<>'group' AND id NOT IN (SELECT monitor_id FROM monitor_tag WHERE tag_id=?)",
                         (t, nid, t))

    def count(self, nid):
        t = self.tag_id()
        return self.q("SELECT count(*) FROM monitor_tag WHERE tag_id=? AND value=?", (t, nid))[0][0]

    def group_id(self, name, weight):
        r = self.q("SELECT id FROM monitor WHERE type='group' AND name=?", (name,))
        if r:
            return r[0][0]
        cur = self.con.execute(
            "INSERT INTO monitor (name, type, user_id, interval, retry_interval, maxretries, weight) "
            "VALUES (?, 'group', (SELECT min(id) FROM user), 60, 60, 0, ?)", (name, weight))
        return cur.lastrowid

    def set_rows(self, nid, rows, active, prune=True):
        """Make network nid's device list match rows (group,name,type,target,description).
        Monitors are matched by name within that network, so their history is kept."""
        t = self.tag_id()
        mine = {name: mid for mid, name in self.q(
            "SELECT m.id, m.name FROM monitor m JOIN monitor_tag mt ON mt.monitor_id=m.id "
            "WHERE mt.tag_id=? AND mt.value=? AND m.type<>'group'", (t, nid))}
        keep, w = set(), 1000
        for r in rows:
            w += 10
            host = url = port = dsrv = dtype = None
            if r["type"] == "ping":
                host = r["target"]
            elif r["type"] == "http":
                url = r["target"]
            elif r["type"] == "port":
                host, port = r["target"].rsplit(":", 1)
                port = int(port)
            elif r["type"] == "dns":
                host, dsrv = r["target"].rsplit("@", 1)
                port, dtype = 53, "A"
            parent = self.group_id(r["group"], w)
            vals = (r["type"], host, url, port, dsrv, dtype, r.get("description") or None, parent, w, int(active))
            if r["name"] in mine:
                mid = mine[r["name"]]
                self.con.execute("UPDATE monitor SET type=?, hostname=?, url=?, port=?, dns_resolve_server=?, "
                                 "dns_resolve_type=?, description=?, parent=?, weight=?, active=? WHERE id=?", vals + (mid,))
            else:
                cur = self.con.execute(
                    "INSERT INTO monitor (name, type, user_id, hostname, url, port, dns_resolve_server, dns_resolve_type, "
                    "description, parent, weight, active, interval, retry_interval, maxretries, timeout) "
                    "VALUES (?, ?, (SELECT min(id) FROM user), ?, ?, ?, ?, ?, ?, ?, ?, ?, 60, 60, 2, 48)",
                    (r["name"],) + vals)
                mid = cur.lastrowid
                self.con.execute("INSERT INTO monitor_tag (monitor_id, tag_id, value) VALUES (?, ?, ?)", (mid, t, nid))
                mine[r["name"]] = mid
            keep.add(mid)
        if prune:
            for name, mid in mine.items():
                if mid not in keep:
                    self.con.execute("DELETE FROM monitor WHERE id=?", (mid,))
        self.attach_notifications()
        self.tidy_groups()

    def activate(self, nid):
        """Run only network nid's monitors; pause every other network's (history kept)."""
        own = self.owners()
        for mid, typ in self.q("SELECT id, type FROM monitor WHERE type<>'group'"):
            self.con.execute("UPDATE monitor SET active=? WHERE id=?", (int(own.get(mid) == nid), mid))
        self.tidy_groups()

    def tidy_groups(self):
        # a group runs while any of its members runs; empty groups are removed
        self.con.execute("DELETE FROM monitor WHERE type='group' AND id NOT IN "
                         "(SELECT parent FROM monitor WHERE parent IS NOT NULL)")
        self.con.execute("UPDATE monitor SET active = EXISTS (SELECT 1 FROM monitor c WHERE c.parent=monitor.id "
                         "AND c.active=1) WHERE type='group'")

    def attach_notifications(self):
        # phone alerts etc.: every default notification goes on every monitor (as kuma-apply.sh does)
        self.con.execute("INSERT INTO monitor_notification (monitor_id, notification_id) "
                         "SELECT m.id, n.id FROM monitor m, notification n WHERE n.is_default=1 AND n.active=1 "
                         "AND m.type<>'group' AND NOT EXISTS (SELECT 1 FROM monitor_notification x "
                         "WHERE x.monitor_id=m.id AND x.notification_id=n.id)")

    def status_page(self, title):
        """Rebuild the volunteer status page with this network's name as its title. Commits first:
        kuma-statuspage.sh writes to the same database."""
        self.con.commit()
        if self.q("SELECT count(*) FROM status_page WHERE slug='dashboard'")[0][0]:
            rc, out = run([os.path.join(APP, "scripts", "kuma-statuspage.sh"), title], timeout=60,
                          env={**os.environ, "NO_RESTART": "1", "APP": APP})
            if rc != 0:
                log(f"status page rebuild failed: {out.strip()[-200:]}")


def starter_rows(cur):
    """A first device list for a network the box hasn't seen before."""
    gw = cur["gateway"]
    return [{"group": "Network", "name": "Router", "type": "ping", "target": gw,
             "description": "The router this box connects through"},
            {"group": "Network", "name": "Internet", "type": "ping", "target": "1.1.1.1",
             "description": "Reaching the internet (Cloudflare)"},
            {"group": "Network", "name": "Name lookups", "type": "dns", "target": f"example.com@{gw}",
             "description": "The router answers DNS"}]


def validate_rows(rows):
    """Check a device list from the dashboard. Returns (clean rows, error)."""
    if not isinstance(rows, list) or not rows:
        return None, "The device list is empty"
    if len(rows) > 300:
        return None, "That's more than 300 devices"
    host_re = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
    out, names = [], set()
    for n, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            return None, f"Line {n} isn't valid"
        g = clean_name(r.get("group"), "Devices")[:60]
        name = clean_name(r.get("name"), "")[:80]
        typ = str(r.get("type") or "ping").strip().lower()
        tgt = str(r.get("target") or "").strip()
        desc = re.sub(r"[\x00-\x1f]", " ", str(r.get("description") or "")).strip()[:200]
        if not name:
            return None, f"Line {n}: give the device a name"
        if name in names:
            return None, f"Line {n}: the name '{name}' is used twice"
        if typ not in MONITOR_TYPES:
            return None, f"{name}: unknown check type '{typ}'"
        ok = False
        if typ == "ping":
            ok = bool(host_re.match(tgt))
        elif typ == "http":
            ok = bool(re.match(r"^https?://[^\s\"'<>]{1,490}$", tgt))
        elif typ == "port":
            h, _, p = tgt.rpartition(":")
            ok = bool(host_re.match(h)) and p.isdigit() and 0 < int(p) < 65536
        elif typ == "dns":
            h, _, s = tgt.rpartition("@")
            ok = bool(host_re.match(h)) and bool(host_re.match(s))
        if not ok:
            return None, f"{name}: '{tgt}' isn't a valid target for a {typ} check"
        names.add(name)
        out.append({"group": g, "name": name, "type": typ, "target": tgt, "description": desc})
    return out, None


# ------------------------------------------------------------------ networks

def load():
    return read_json(NETS, {"version": 1, "current": None, "networks": {}})


def save(nets):
    write_json(NETS, nets)


def state_path(nid):
    return os.path.join(NETDIR, nid, "netcheck-state.json")


def env_value(name):
    try:
        for line in open(os.path.join(APP, ".env")):
            k, _, v = line.strip().partition("=")
            if k == name:
                return v.strip().strip('"')
    except OSError:
        pass
    return ""


def publish(nets, cur):
    """data/network.json: what the dashboard shows. No MAC addresses, no passwords."""
    nid = nets.get("current")
    n = nets["networks"].get(nid) if nid else None
    doc = {"generated": int(time.time()), "online": bool(cur), "hostname": socket.gethostname()}
    if n:
        doc.update({"id": nid, "name": n["name"], "subnet": n.get("subnet"), "gateway": n.get("gateway"),
                    "interface": n.get("interface"), "kind": n.get("kind"), "ssid": n.get("ssid"),
                    "address": n.get("address"), "first_seen": n.get("first_seen"),
                    "since": (n.get("periods") or [[None]])[-1][0], "setup": n.get("setup"),
                    "visits": len(n.get("periods") or []),
                    "known": [{"id": k, "name": v["name"], "subnet": v.get("subnet"), "last_seen": v.get("last_seen"),
                               "monitors": v.get("monitors")}
                              for k, v in sorted(nets["networks"].items(), key=lambda kv: -(kv[1].get("last_seen") or 0))]})
    write_json(PUBLIC, doc, 0o644)


def ensure(verbose=False):
    """Work out which network the box is on and switch everything over if it changed.
    Returns the current network record (or None when offline)."""
    os.makedirs(CONF, exist_ok=True)
    with open(os.path.join(CONF, ".netswitch.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _ensure(verbose)


def _ensure(verbose):
    cur = detect()
    nets = load()
    now = int(time.time())
    if not cur:
        publish(nets, None)
        if verbose:
            print("offline: no default route")
        return None
    nid, prev = cur["id"], nets.get("current")
    first_ever = not nets["networks"]
    n = nets["networks"].get(nid)
    if n is None:
        n = {"name": default_name(cur), "first_seen": now, "periods": [], "setup": "starter"}
        if first_ever:
            # upgrading an existing box: the network it's on keeps everything it has learned
            n["setup"] = "migrated"
            title = env_value("STATUS_TITLE")
            if title:
                n["name"] = clean_name(title, n["name"])
            old = os.path.join(DATA, "netcheck-state.json")
            st = read_json(old, {})
            if st.get("learned_at"):
                n["first_seen"] = min(now, int(st["learned_at"]))
            if os.path.exists(old):
                os.makedirs(os.path.dirname(state_path(nid)), exist_ok=True)
                shutil.move(old, state_path(nid))
        nets["networks"][nid] = n
        log(f"new network {nid}: {n['name']}")
    n.update({k: cur[k] for k in ("gateway", "gateway_mac", "interface", "kind", "subnet", "address", "ssid")})
    n["last_seen"] = now
    changed = prev != nid
    if changed or not n["periods"]:
        if prev and prev in nets["networks"] and nets["networks"][prev].get("periods"):
            p = nets["networks"][prev]
            p["periods"][-1][1] = p.get("last_seen") or now
        n["periods"].append([now, now])
        del n["periods"][:-200]
    else:
        n["periods"][-1][1] = now
    nets["current"] = nid
    save(nets)

    update_netalertx()
    # Uptime Kuma follows the network. Retried on later runs until it works (for example when the
    # Kuma admin account hasn't been created yet on a new box).
    if changed or nets.get("kuma_network") != nid:
        try:
            with Kuma() as k:
                # monitors made outside lanpulse belong to the network they were made on
                k.claim_untagged(prev if prev in nets["networks"] and prev != nid else nid)
                if k.count(nid) == 0:
                    k.set_rows(nid, starter_rows(cur), active=True)
                    if n["setup"] == "migrated":
                        n["setup"] = "starter"
                k.activate(nid)
                for x in nets["networks"]:
                    nets["networks"][x]["monitors"] = k.count(x)
                k.status_page(n["name"])
            nets["kuma_network"] = nid
            save(nets)
            log(f"Uptime Kuma now runs network {nid}'s devices ({n['name']}); was {prev or 'none'}")
        except Exception as e:
            log(f"Uptime Kuma switch failed (will retry): {e}")
        if changed and prev:
            # fresh network checks for the new network right away
            run(["systemctl", "start", "--no-block", "lanpulse-netcheck.service"], timeout=10)
    publish(nets, cur)
    if verbose:
        print(json.dumps({"id": nid, "changed": changed, **{k: n.get(k) for k in ("name", "subnet", "interface", "ssid")}}, indent=1))
    return {"id": nid, **n}


def current():
    """The current network record without re-detecting (for netcheck)."""
    nets = load()
    nid = nets.get("current")
    return ({"id": nid, **nets["networks"][nid]} if nid in nets.get("networks", {}) else None)


# ------------------------------------------------------------------ dashboard requests

def wifi_iface():
    for dev in sorted(os.listdir("/sys/class/net")):
        if os.path.isdir(f"/sys/class/net/{dev}/wireless"):
            return dev
    return None


def nm_split(line):
    """Split one line of `nmcli -t` output (':' separated, '\\:' escaped)."""
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in re.split(r"(?<!\\):", line)]


def handle(req):
    kind = req.get("type")
    nets = load()
    if kind == "rename":
        nid = req.get("network") or nets.get("current")
        if nid not in nets["networks"]:
            return {"ok": False, "error": "Unknown network"}
        n = nets["networks"][nid]
        n["name"] = clean_name(req.get("name"), n["name"])
        save(nets)
        if nid == nets.get("current") and os.path.exists(KUMA_DB):
            run([os.path.join(APP, "scripts", "kuma-statuspage.sh"), n["name"]], timeout=120, env={**os.environ, "APP": APP})
        publish(nets, True)
        return {"ok": True, "name": n["name"]}
    if kind == "monitors":
        nid = req.get("network") or nets.get("current")
        if nid not in nets["networks"]:
            return {"ok": False, "error": "Unknown network"}
        rows, err = validate_rows(req.get("rows"))
        if err:
            return {"ok": False, "error": err}
        with Kuma() as k:
            k.claim_untagged(nets.get("current") or nid)
            k.set_rows(nid, rows, active=(nid == nets.get("current")), prune=True)
            nets["networks"][nid]["monitors"] = k.count(nid)
            if nid == nets.get("current"):
                k.status_page(nets["networks"][nid]["name"])
        nets["networks"][nid]["setup"] = "saved"
        save(nets)
        publish(nets, True)
        return {"ok": True, "monitors": len(rows)}
    if kind == "forget":
        nid = req.get("network")
        if nid not in nets["networks"]:
            return {"ok": False, "error": "Unknown network"}
        if nid == nets.get("current"):
            return {"ok": False, "error": "The box is on that network now; it can't be forgotten"}
        with Kuma() as k:
            t = k.tag_id()
            k.con.execute("DELETE FROM monitor WHERE id IN (SELECT monitor_id FROM monitor_tag WHERE tag_id=? AND value=?)", (t, nid))
            k.tidy_groups()
        del nets["networks"][nid]
        shutil.rmtree(os.path.join(NETDIR, nid), ignore_errors=True)
        save(nets)
        publish(nets, True)
        return {"ok": True}
    if kind == "wifi_scan":
        dev = wifi_iface()
        if not dev:
            return {"ok": False, "error": "This box has no Wi-Fi adapter"}
        rc, out = run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "device", "wifi", "list",
                       "ifname", dev, "--rescan", "yes"], timeout=40)
        if rc != 0:
            return {"ok": False, "error": out.strip()[:200] or "Wi-Fi scan failed"}
        best = {}
        for line in out.splitlines():
            p = nm_split(line)
            if len(p) < 5 or not p[1]:
                continue
            sig = int(p[2]) if p[2].isdigit() else 0
            if p[1] not in best or sig > best[p[1]]["signal"]:
                best[p[1]] = {"ssid": p[1], "signal": sig, "security": p[3] or "open", "channel": p[4],
                              "in_use": p[0].strip() == "*"}
        return {"ok": True, "networks": sorted(best.values(), key=lambda x: (not x["in_use"], -x["signal"]))[:40]}
    if kind == "wifi_join":
        dev = wifi_iface()
        ssid, pw = str(req.get("ssid") or ""), str(req.get("password") or "")
        if not dev:
            return {"ok": False, "error": "This box has no Wi-Fi adapter"}
        if not ssid or len(ssid) > 32 or len(pw) > 63 or "\n" in ssid + pw:
            return {"ok": False, "error": "Check the network name and password"}
        cmd = ["nmcli", "--wait", "40", "device", "wifi", "connect", ssid, "ifname", dev]
        if pw:
            cmd += ["password", pw]
        rc, out = run(cmd, timeout=60)
        if rc != 0:
            msg = out.strip().splitlines()[-1] if out.strip() else "couldn't connect"
            msg = msg.replace(pw, "•••") if pw else msg
            if "Secrets were required" in msg or "802-11-wireless-security" in msg:
                msg = "the password was not accepted"
            return {"ok": False, "error": f"Couldn't join {ssid}: {msg[:160]}"}
        cur = _ensure(False)                     # the caller already holds the lock
        return {"ok": True, "network": cur and {"id": cur["id"], "name": cur["name"]}}
    return {"ok": False, "error": f"unknown request '{kind}'"}


def process_requests():
    os.makedirs(REQ, mode=0o700, exist_ok=True)
    now = time.time()
    for f in os.listdir(REQ):                      # old results nobody collected
        p = os.path.join(REQ, f)
        if f.endswith(".res") and now - os.path.getmtime(p) > 3600:
            os.remove(p)
    for f in sorted(os.listdir(REQ)):
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}\.req", f):
            continue
        p = os.path.join(REQ, f)
        req = read_json(p, None)
        os.remove(p)                               # don't leave Wi-Fi passwords lying around
        try:
            with open(os.path.join(CONF, ".netswitch.lock"), "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)   # never at the same time as a network switch
                res = handle(req) if isinstance(req, dict) else {"ok": False, "error": "bad request"}
        except Exception as e:
            log(f"request {f} failed: {e}")
            res = {"ok": False, "error": str(e)[:200]}
        write_json(os.path.join(REQ, f[:-4] + ".res"), res)


def main():
    if "--requests" in sys.argv:
        process_requests()
        return
    ensure(verbose="--print" in sys.argv)


if __name__ == "__main__":
    main()
