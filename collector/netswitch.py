#!/usr/bin/env python3
"""
lanpulse netswitch: lets the box move between networks (home, office, church, a friend's house)
and adapt by itself. Runs on the host as root:

  - from NetworkManager whenever a connection comes up or changes (dispatcher script, setup.sh),
  - at the start of every netcheck run (every 10 minutes), as a safety net,
  - with --requests, from a systemd path unit, to carry out changes asked for in the dashboard.

It never assumes the box is on a network it knows. Whenever the connection changes it gathers
evidence: the router's MAC address and UPnP ID, the Wi-Fi name and access point, the access points
heard nearby, the devices answering on the network, the address range, the DHCP domain and the public
IP. Every known network is scored against that (score()). A known network is used again only when
the evidence is strong and includes hardware (router, access point or several known devices); a Wi-Fi
name or address range alone never counts. In-between evidence starts the network fresh and asks in
the dashboard ("Is this <name>?"); the box keeps looking and settles it by itself if the evidence
becomes strong. Per network it keeps:
  - a name (shown as the dashboard title),
  - its own device list in Uptime Kuma (monitors tagged lanpulse-network=<id>; the other networks'
    monitors are paused, not deleted, so their history is kept for when the box comes back),
  - its own learned baselines (netcheck state: public IP, Wi-Fi access points, gateway uptime),
  - the time periods the box spent there (speed-test "normal" and new-device detection use them),
  - its own router login (kept by the collector in config/settings.json, keyed by network id),
  - its fingerprint: every piece of evidence seen there, with when it was first and last seen.

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
import fcntl, ipaddress, json, os, re, secrets, shutil, socket, sqlite3, subprocess, sys, time
import urllib.parse, urllib.request

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


# ------------------------------------------------------------------ observing the network
# The box never assumes it is on a network it already knows. Each time the connection changes it
# collects evidence (below) and scores every known network against it (score()). Only strong,
# consistent evidence brings back a known network's device list and router login; weaker evidence
# starts the network fresh and asks in the dashboard; no evidence means a new network.

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


def wifi_link(dev):
    """(SSID, access point BSSID) of the Wi-Fi connection on dev."""
    _, out = run(["iw", "dev", dev, "link"])
    ssid = re.search(r"^\s*SSID: (.+)$", out, re.M)
    bssid = re.search(r"Connected to ([0-9a-f:]{17})", out)
    return (ssid.group(1).strip() if ssid else None), (bssid.group(1).lower() if bssid else None)


def own_macs():
    out = set()
    try:
        for dev in os.listdir("/sys/class/net"):
            try:
                out.add(open(f"/sys/class/net/{dev}/address").read().strip().lower())
            except OSError:
                pass
    except OSError:
        pass
    return out


def is_random_mac(mac):
    """Phones and laptops use made-up ('locally administered') MACs that change: useless as evidence."""
    return len(mac) >= 2 and mac[1].lower() in "26ae"


def lp_env(name, default=""):
    try:
        for line in open(os.path.join(APP, "lanpulse.env")):
            k, _, v = line.strip().partition("=")
            if k == name:
                return v.split("#")[0].strip().strip('"')
    except OSError:
        pass
    return default


def quick_observe(wait=20):
    """What the box can see straight away: router, address range, adapter, Wi-Fi name and access point."""
    deadline = time.time() + wait
    gw, dev = default_route()
    while not gw and time.time() < deadline:
        time.sleep(2)
        gw, dev = default_route()
    if not gw:
        return None
    addr = next((a for d, a in addresses() if d == dev), None)
    kind = iface_kind(dev)
    ssid, bssid = wifi_link(dev) if kind == "Wi-Fi" else (None, None)
    return {"gateway": gw, "gateway_mac": gateway_mac(gw, dev), "interface": dev, "kind": kind,
            "subnet": str(addr.network) if addr else None, "address": str(addr.ip) if addr else None,
            "ssid": ssid, "bssid": bssid}


def signature(o):
    """Changes whenever the box might be somewhere else (not when only its own address changes)."""
    return "|".join(str(o.get(k) or "") for k in ("gateway_mac", "gateway", "subnet", "interface", "ssid"))


def _clean_macs(macs, o):
    skip = own_macs() | {o.get("gateway_mac")}
    return {m for m in macs if m and m not in skip and not is_random_mac(m) and m != "00:00:00:00:00:00"}


def neighbour_macs(o):
    """Devices the box has talked to recently (kernel neighbour table): cheap, no scanning."""
    _, out = run(["ip", "neigh", "show", "dev", o["interface"]])
    return _clean_macs(set(re.findall(r"lladdr ([0-9a-f:]{17})", out)), o)


def arp_sweep(o):
    """Every device answering on the local network (arp-scan, a few seconds for a /24)."""
    net = o.get("subnet")
    if not net:
        return neighbour_macs(o)
    n = ipaddress.ip_network(net)
    if n.prefixlen < MAX_SCAN_PREFIX:
        n = ipaddress.ip_interface(f"{o['address']}/{MAX_SCAN_PREFIX}").network
    rc, out = run(["arp-scan", "-I", o["interface"], "-q", "-x", "-g", "--retry=2", str(n)], timeout=60)
    found = set(re.findall(r"^\S+\s+([0-9a-f:]{17})", out, re.M)) if rc == 0 else set()
    return _clean_macs(found, o) | neighbour_macs(o)


def nearby_aps():
    """Wi-Fi access points heard strongly from here (their BSSIDs are fixed hardware addresses), even
    when the box is on a cable: a good fingerprint of the place."""
    dev = wifi_iface()
    if not dev:
        return set()
    run(["ip", "link", "set", "dev", dev, "up"], timeout=10)
    rc, out = run(["iw", "dev", dev, "scan"], timeout=40)
    if rc != 0 or "BSS " not in out:
        rc, out = run(["iw", "dev", dev, "scan", "dump"], timeout=20)
    aps = set()
    for block in re.split(r"\n(?=BSS [0-9a-f:]{17})", "\n" + out):
        m = re.match(r"\s*BSS ([0-9a-f:]{17})", block)
        sig = re.search(r"signal: (-?[\d.]+) dBm", block)
        if m and sig and float(sig.group(1)) >= -75:
            aps.add(m.group(1).lower())
    return aps


def dhcp_domain(dev):
    _, out = run(["nmcli", "-g", "IP4.DOMAIN", "device", "show", dev], timeout=10)
    vals = [v.strip() for v in out.replace("|", "\n").splitlines() if v.strip() and "Error" not in v]
    return vals[0].lower() if vals else None


def upnp_router_id(gw):
    """The router's own unique ID (UPnP UDN), when it runs UPnP. Survives a MAC change."""
    rc, out = run(["upnpc", "-l"], timeout=20)
    m = re.search(r"desc: (http://[^\s]+)", out)
    if not m or urllib.parse.urlparse(m.group(1)).hostname != gw:
        return None
    try:
        with urllib.request.urlopen(m.group(1), timeout=5) as r:
            x = r.read(200_000).decode(errors="replace")
    except Exception:
        return None
    u = re.search(r"<UDN>\s*(uuid:[^<\s]+)\s*</UDN>", x)
    return u.group(1).lower() if u else None


def public_ip():
    if lp_env("NETCHECK_PUBLICIP", "1").lower() in ("0", "false", "no", "off"):
        return None
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=8) as r:
            ip = r.read().decode().strip()
        ipaddress.ip_address(ip)
        return ip
    except Exception:
        return None


def full_observe(base):
    """All the evidence: used whenever the connection changes, and while a network is still uncertain."""
    o = dict(base)
    o["devices"] = sorted(arp_sweep(base))
    o["aps"] = sorted(nearby_aps())
    o["domain"] = dhcp_domain(base["interface"])
    o["udn"] = upnp_router_id(base["gateway"])
    o["public_ip"] = public_ip()
    return o


# ------------------------------------------------------------------ fingerprints and matching
# Each network keeps a fingerprint: every value seen there, with when it was first and last seen.

FP_CAP = {"devices": 400, "aps": 300, "public_ips": 10}


def learn(n, o, now):
    fp = n.setdefault("fp", {})

    def add(key, val):
        if not val:
            return
        d = fp.setdefault(key, {})
        if val in d:
            d[val][1] = now
        else:
            d[val] = [now, now]
        cap = FP_CAP.get(key, 50)
        if len(d) > cap:
            for k in sorted(d, key=lambda k: d[k][1])[:len(d) - cap]:
                del d[k]
    add("gateway_macs", o.get("gateway_mac"))
    add("udns", o.get("udn"))
    add("ssids", o.get("ssid"))
    add("bssids", o.get("bssid"))
    for a in o.get("aps") or []:
        add("aps", a)
    for m in o.get("devices") or []:
        add("devices", m)
    if o.get("subnet") and o.get("gateway"):
        add("addrs", f"{o['subnet']}|{o['gateway']}")
    add("domains", o.get("domain"))
    add("public_ips", o.get("public_ip"))


def score(n, o):
    """How sure are we that the box is on network n? Points for each piece of evidence that matches,
    minus points for evidence that contradicts it. 'match' needs 60+ points AND at least one piece of
    hardware evidence (router, access point or several known devices): a Wi-Fi name or an address
    range alone is never enough, since 'NETGEAR' on 192.168.1.x is everywhere."""
    fp = n.get("fp") or {}
    have = lambda k: fp.get(k) or {}
    pts, why, against, strong = 0, [], [], False
    hw = False                                      # same router or access point hardware

    def plus(p, text, is_strong=False):
        nonlocal pts, strong
        pts += p
        why.append(text)
        strong = strong or is_strong

    def minus(p, text):
        nonlocal pts
        pts -= p
        against.append(text)
    if o.get("udn") and o["udn"] in have("udns"):
        plus(45, "same router (its UPnP ID matches)", True)
        hw = True
    if o.get("gateway_mac"):
        if o["gateway_mac"] in have("gateway_macs"):
            plus(40, "same router hardware (MAC address)", True)
            hw = True
        elif have("gateway_macs"):
            minus(10, "a different router")
    if o.get("bssid") and o["bssid"] in have("bssids"):
        plus(35, "connected to one of its Wi-Fi access points", True)
        hw = True
    aps = set(o.get("aps") or []) & set(have("aps"))
    if len(aps) >= 3:
        plus(30, f"{len(aps)} Wi-Fi access points heard there are in range", True)
    elif aps:
        plus(10, f"{len(aps)} Wi-Fi access point{'s' if len(aps) > 1 else ''} heard there in range")
    ssid_conflict = False
    if o.get("ssid"):
        if o["ssid"] in have("ssids"):
            plus(20, f"same Wi-Fi name ({o['ssid']})")
        elif have("ssids"):
            minus(25, f"a different Wi-Fi name (that network uses {', '.join(sorted(have('ssids'))[:2])})")
            ssid_conflict = True
    devs = set(o.get("devices") or []) & set(have("devices"))
    known = len(have("devices"))
    if len(devs) >= 5 or (len(devs) >= 3 and known and len(devs) / known >= 0.3):
        plus(35, f"{len(devs)} of its devices are here", True)
    elif len(devs) >= 2:
        plus(12, f"{len(devs)} of its devices are here")
    if o.get("subnet") and o.get("gateway"):
        if f"{o['subnet']}|{o['gateway']}" in have("addrs"):
            plus(10, f"same address range ({o['subnet']})")
        elif have("addrs"):
            minus(15, "a different address range")
    if o.get("domain") and o["domain"] in have("domains"):
        plus(15, f"same network domain ({o['domain']})")
    if o.get("public_ip") and o["public_ip"] in have("public_ips"):
        plus(15, "same internet address")
    # a different Wi-Fi name with no matching hardware rules a network out
    conflict = ssid_conflict and not hw
    level = "match" if pts >= 60 and strong and not conflict else "maybe" if pts >= 30 and not conflict else "no"
    return {"id": None, "name": n.get("name"), "score": pts, "level": level, "reasons": why, "against": against}


def evaluate(nets, o):
    """Score every known network against the evidence, best first. When two networks both match, one
    the administrator said is *not* the other wins (see 'Not this network' in the dashboard)."""
    res = []
    for nid, n in nets["networks"].items():
        r = score(n, o)
        r["id"] = nid
        res.append(r)
    res.sort(key=lambda r: -r["score"])
    matched = [r for r in res if r["level"] == "match"]
    for r in matched:
        for other in matched:
            if other is not r and r["id"] in (nets["networks"][other["id"]].get("not") or []):
                r["level"] = "maybe"               # 'other' was explicitly told it isn't r
    res.sort(key=lambda r: (r["level"] != "match", -r["score"]))
    return res


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
    return read_json(NETS, {"version": 2, "current": None, "networks": {}})


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


def new_id(nets):
    while True:
        nid = "n" + secrets.token_hex(5)
        if nid not in nets["networks"]:
            return nid


def default_name(o):
    return f"Network {o.get('subnet') or o.get('gateway')}" + (f" ({o['ssid']})" if o.get("ssid") else "")


def legacy_setup(nets):
    """Boxes set up before networks were tracked: turn what they already have (Uptime Kuma monitors,
    router login, learned Wi-Fi access points, NetAlertX's device list) into a known network with a
    fingerprint. It is NOT assumed to be the network the box is on now: it has to win on evidence
    like any other."""
    if nets.get("legacy_checked"):
        return
    nets["legacy_checked"] = True
    st = read_json(os.path.join(DATA, "netcheck-state.json"), {})
    settings = read_json(os.path.join(CONF, "settings.json"), {})
    router = settings.get("router") or {}
    monitors = []
    if os.path.exists(KUMA_DB):
        try:
            con = sqlite3.connect(f"file:{KUMA_DB}?mode=ro", uri=True, timeout=10)
            monitors = [h for (h,) in con.execute(
                "SELECT hostname FROM monitor WHERE type<>'group' AND id NOT IN (SELECT monitor_id FROM monitor_tag "
                "WHERE tag_id IN (SELECT id FROM tag WHERE name=?))", (TAG,))]
            con.close()
        except sqlite3.Error:
            monitors = []
    if not (st or router.get("type") or monitors):
        return
    t0 = int(st.get("learned_at") or time.time())
    n = {"name": clean_name(env_value("STATUS_TITLE"), "Previous setup"), "first_seen": t0, "periods": [],
         "setup": "migrated", "legacy": True, "fp": {}}
    for s in st.get("own_ssids") or []:
        learn(n, {"ssid": s}, t0)
    for b in (st.get("known_bssids") or {}):
        learn(n, {"bssid": b.lower(), "aps": [b.lower()]}, t0)
    for h in st.get("public_ip_history") or []:
        learn(n, {"public_ip": h.get("ip")}, t0)
    # address range and router address: from the router login, else from the monitors' private IPs
    gw = urllib.parse.urlparse(router.get("url") or "").hostname if router.get("url") else None
    ips = []
    for h in monitors + ([gw] if gw else []):
        try:
            a = ipaddress.ip_address(h or "")
            if a.is_private:
                ips.append(a)
        except ValueError:
            pass
    nets_seen = {}
    for a in ips:
        k = str(ipaddress.ip_interface(f"{a}/24").network)
        nets_seen[k] = nets_seen.get(k, 0) + 1
    subnet = max(nets_seen, key=nets_seen.get) if nets_seen else None
    # NetAlertX knows the devices (and the router's MAC) of the network it scanned
    na = os.path.join(APP, "netalertx", "db", "app.db")
    if os.path.exists(na) and subnet:
        try:
            con = sqlite3.connect(f"file:{na}?mode=ro", uri=True, timeout=10)
            for mac, ip in con.execute("SELECT lower(devMac), devLastIP FROM Devices WHERE devMac <> 'internet'"):
                try:
                    inside = ipaddress.ip_address(ip or "") in ipaddress.ip_network(subnet)
                except ValueError:
                    inside = False
                if not inside:
                    continue
                if gw and ip == gw:
                    learn(n, {"gateway_mac": mac}, t0)
                elif not is_random_mac(mac):
                    learn(n, {"devices": [mac]}, t0)
            con.close()
        except sqlite3.Error:
            pass
    if subnet and gw:
        learn(n, {"subnet": subnet, "gateway": gw}, t0)
    nid = new_id(nets)
    nets["networks"][nid] = n
    nets["legacy_id"] = nid
    old = os.path.join(DATA, "netcheck-state.json")
    if os.path.exists(old):
        os.makedirs(os.path.dirname(state_path(nid)), mode=0o700, exist_ok=True)
        shutil.move(old, state_path(nid))
    log(f"previous setup kept as network {nid} ({n['name']}); it will be used only if the evidence matches")


def publish(nets, online):
    """data/network.json: what the dashboard shows. No MAC addresses, no passwords."""
    nid = nets.get("current")
    n = nets["networks"].get(nid) if nid else None
    doc = {"generated": int(time.time()), "online": bool(online), "hostname": socket.gethostname()}
    if n:
        cand = n.get("candidate")
        doc.update({"id": nid, "name": n["name"], "subnet": n.get("subnet"), "gateway": n.get("gateway"),
                    "interface": n.get("interface"), "kind": n.get("kind"), "ssid": n.get("ssid"),
                    "address": n.get("address"), "first_seen": n.get("first_seen"),
                    "since": (n.get("periods") or [[None]])[-1][0], "setup": n.get("setup"),
                    "visits": len(n.get("periods") or []), "match": n.get("match"),
                    "candidate": cand and {k: cand.get(k) for k in ("id", "name", "score", "reasons", "against")},
                    "known": [{"id": k, "name": v["name"], "subnet": v.get("subnet"), "last_seen": v.get("last_seen")}
                              for k, v in sorted(nets["networks"].items(), key=lambda kv: -(kv[1].get("last_seen") or 0))]})
    write_json(PUBLIC, doc, 0o644)


def ensure(verbose=False):
    """Work out which network the box is on and switch everything over if it changed.
    Returns the current network record (or None when offline)."""
    os.makedirs(CONF, exist_ok=True)
    with open(os.path.join(CONF, ".netswitch.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _ensure(verbose)


CONN_KEYS = ("gateway", "gateway_mac", "interface", "kind", "subnet", "address", "ssid")


def _enter(nets, nid, o, now):
    """Make nid the current network: close the previous visit, start a new one."""
    prev = nets.get("current")
    n = nets["networks"][nid]
    n.update({k: o.get(k) for k in CONN_KEYS})
    n["last_seen"] = now
    if prev != nid or not n.get("periods"):
        if prev and prev in nets["networks"] and nets["networks"][prev].get("periods"):
            p = nets["networks"][prev]
            p["periods"][-1][1] = p.get("last_seen") or now
        n.setdefault("periods", []).append([now, now])
        del n["periods"][:-200]
    nets["current"] = nid
    return prev


def _ensure(verbose):
    base = quick_observe()
    nets = load()
    now = int(time.time())
    if not base:
        publish(nets, False)
        if verbose:
            print("offline: no default route")
        return None
    legacy_setup(nets)
    sig = signature(base)
    cur = nets.get("current")
    if cur in nets["networks"] and nets.get("signature") == sig:
        # same connection as last time: stay, and keep learning what this network looks like
        n = nets["networks"][cur]
        learn(n, {**base, "devices": sorted(neighbour_macs(base))}, now)
        n.update({k: base.get(k) for k in CONN_KEYS})
        n["last_seen"] = now
        n["periods"][-1][1] = now
        cand = n.get("candidate")
        if cand and now - n.get("evaluated", 0) > 900 and cand.get("id") in nets["networks"]:
            # still unsure: look again, and settle it once the evidence is strong either way
            o = full_observe(base)
            learn(n, o, now)
            n["evaluated"] = now
            r = score(nets["networks"][cand["id"]], o)
            r["id"] = cand["id"]
            if r["level"] == "match" and cur not in (nets["networks"][cand["id"]].get("not") or []) \
                    and cand["id"] not in (n.get("not") or []):
                save(nets)
                merge(nets, cur, cand["id"], why=r["reasons"], auto=True)
                cur = nets["current"]
            elif r["level"] == "maybe":
                n["candidate"] = r
            else:
                n.pop("candidate", None)
        save(nets)
        update_netalertx()
        sync_kuma(nets, base)
        publish(nets, True)
        if verbose:
            print(json.dumps({"id": nets["current"], "changed": False, "name": nets["networks"][nets["current"]]["name"]}))
        return {"id": nets["current"], **nets["networks"][nets["current"]]}

    # the connection changed (or first run): decide from the evidence alone
    o = full_observe(base)
    results = evaluate(nets, o)
    best = results[0] if results else None
    if best and best["level"] == "match":
        nid = best["id"]
        n = nets["networks"][nid]
        n["match"] = {"how": "recognised", "score": best["score"], "reasons": best["reasons"], "against": best["against"], "at": now}
        n.pop("candidate", None)
        log(f"recognised network {nid} ({n['name']}): {', '.join(best['reasons'])} (score {best['score']})")
    else:
        nid = new_id(nets)
        n = {"name": default_name(o), "first_seen": now, "periods": [], "setup": "starter", "fp": {},
             "match": {"how": "new", "at": now,
                       "closest": best and {k: best[k] for k in ("id", "name", "score", "level", "reasons", "against")}}}
        if best and best["level"] == "maybe":
            n["candidate"] = best
        nets["networks"][nid] = n
        log(f"new network {nid}: {n['name']}" + (f" (might be {best['name']}, score {best['score']}: "
                                                   f"{', '.join(best['reasons'])})" if best and best['level'] == 'maybe' else ""))
    n["evaluated"] = now
    learn(n, o, now)
    _enter(nets, nid, o, now)
    nets["signature"] = sig
    save(nets)
    update_netalertx()
    sync_kuma(nets, base)
    publish(nets, True)
    if verbose:
        print(json.dumps({"id": nid, "name": n["name"], "match": n.get("match"), "candidate": n.get("candidate")}, indent=1))
    return {"id": nid, **n}


def sync_kuma(nets, base):
    """Run only the current network's devices in Uptime Kuma. Retried on later runs until it works
    (for example when the Kuma admin account hasn't been created yet on a new box)."""
    nid = nets["current"]
    if nets.get("kuma_network") == nid:
        return False
    prev = nets.get("kuma_network")
    n = nets["networks"][nid]
    try:
        with Kuma() as k:
            # monitors made outside lanpulse: the previous setup's, or the network they were made on
            if not nets.get("legacy_claimed") and nets.get("legacy_id") in nets["networks"]:
                k.claim_untagged(nets["legacy_id"])
            k.claim_untagged(prev if prev in nets["networks"] else nid)
            nets["legacy_claimed"] = True
            if k.count(nid) == 0:
                k.set_rows(nid, starter_rows(base), active=True)
                if n.get("setup") == "migrated":
                    n["setup"] = "starter"
            k.activate(nid)
            for x in nets["networks"]:
                nets["networks"][x]["monitors"] = k.count(x)
            k.status_page(n["name"])
        nets["kuma_network"] = nid
        save(nets)
        log(f"Uptime Kuma now runs network {nid}'s devices ({n['name']}); was {prev or 'none'}")
        if prev:
            run(["systemctl", "start", "--no-block", "lanpulse-netcheck.service"], timeout=10)
        return True
    except Exception as e:
        log(f"Uptime Kuma switch failed (will retry): {e}")
        return False


def merge(nets, src, dst, why=None, auto=False):
    """The administrator (or strong evidence) says network src is really dst: fold src into dst."""
    a, b = nets["networks"][src], nets["networks"][dst]
    with Kuma() as k:
        t = k.tag_id()
        if k.count(dst):
            k.con.execute("DELETE FROM monitor WHERE id IN (SELECT monitor_id FROM monitor_tag WHERE tag_id=? AND value=?)", (t, src))
        else:
            k.con.execute("UPDATE monitor_tag SET value=? WHERE tag_id=? AND value=?", (dst, t, src))
        k.tidy_groups()
        for key, d in (a.get("fp") or {}).items():
            bd = b.setdefault("fp", {}).setdefault(key, {})
            for v, (f, l) in d.items():
                bd[v] = [min(f, bd[v][0]), max(l, bd[v][1])] if v in bd else [f, l]
        b["periods"] = sorted((b.get("periods") or []) + (a.get("periods") or []))[-200:]
        b["first_seen"] = min(b.get("first_seen") or a["first_seen"], a.get("first_seen") or b["first_seen"])
        b.update({kk: a.get(kk) for kk in CONN_KEYS + ("last_seen",)})
        b["match"] = {"how": "recognised" if auto else "confirmed", "reasons": why or ["confirmed in the dashboard"],
                      "at": int(time.time())}
        if b.get("setup") == "migrated" and a.get("setup") == "saved":
            b["setup"] = "saved"
        b["not"] = sorted(set((b.get("not") or [])) | set(a.get("not") or []) - {dst})
        del nets["networks"][src]
        nets.setdefault("merged", {})[src] = dst
        shutil.rmtree(os.path.join(NETDIR, src), ignore_errors=True)
        if nets.get("current") == src:
            nets["current"] = dst
        if nets.get("kuma_network") == src:
            nets["kuma_network"] = dst
        k.activate(nets["current"])
        for x in nets["networks"]:
            nets["networks"][x]["monitors"] = k.count(x)
        k.status_page(nets["networks"][nets["current"]]["name"])
    save(nets)
    log(f"network {src} is {dst} ({b['name']}){' (evidence)' if auto else ' (confirmed)'}")


def split(nets):
    """The administrator says the current network is NOT the known network it was matched to:
    make it a new network, and take back what was learned during this visit."""
    old = nets["current"]
    a = nets["networks"][old]
    start = a["periods"][-1][0] if a.get("periods") else int(time.time())
    nid = new_id(nets)
    n = {"name": default_name(a), "first_seen": start, "periods": [a["periods"].pop()] if a.get("periods") else [],
         "setup": "starter", "fp": {}, "not": [old], "match": {"how": "new", "at": int(time.time()), "split_from": old}}
    n.update({k: a.get(k) for k in CONN_KEYS + ("last_seen",)})
    for key, d in (a.get("fp") or {}).items():
        for v in [v for v, (f, l) in d.items() if f >= start]:
            n["fp"].setdefault(key, {})[v] = d.pop(v)
    a["not"] = sorted(set(a.get("not") or []) | {nid})
    nets["networks"][nid] = n
    nets["current"] = nid
    nets["kuma_network"] = None
    save(nets)
    sync_kuma(nets, {"gateway": n.get("gateway")})
    return nid


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
        # Forgetting the network the box is on now makes it start over: the next look at the network
        # (connection change, restart, or the 10-minute check) treats it as never seen before.
        nid = req.get("network")
        if nid not in nets["networks"]:
            return {"ok": False, "error": "Unknown network"}
        with Kuma() as k:
            t = k.tag_id()
            k.con.execute("DELETE FROM monitor WHERE id IN (SELECT monitor_id FROM monitor_tag WHERE tag_id=? AND value=?)", (t, nid))
            k.tidy_groups()
        del nets["networks"][nid]
        for other in nets["networks"].values():
            if nid in (other.get("not") or []):
                other["not"].remove(nid)
        shutil.rmtree(os.path.join(NETDIR, nid), ignore_errors=True)
        if nets.get("current") == nid:
            nets["current"] = nets["signature"] = nets["kuma_network"] = None
        if nets.get("legacy_id") == nid:
            nets.pop("legacy_id")
        save(nets)
        publish(nets, True)
        return {"ok": True, "current_forgotten": nets.get("current") is None}
    if kind == "confirm":                      # "Yes, this is <candidate>"
        cur, cand = nets.get("current"), req.get("candidate")
        if cur not in nets["networks"] or cand not in nets["networks"] or cand == cur:
            return {"ok": False, "error": "Unknown network"}
        merge(nets, cur, cand)
        publish(nets, True)
        return {"ok": True, "network": {"id": cand, "name": nets["networks"][cand]["name"]}}
    if kind == "reject":                       # "No, it's a different network"
        cur, cand = nets.get("current"), req.get("candidate")
        if cur not in nets["networks"]:
            return {"ok": False, "error": "Unknown network"}
        n = nets["networks"][cur]
        n.pop("candidate", None)
        if cand in nets["networks"]:
            n["not"] = sorted(set(n.get("not") or []) | {cand})
        save(nets)
        publish(nets, True)
        return {"ok": True}
    if kind == "split":                        # "This isn't <current>": recognised wrongly
        if nets.get("current") not in nets["networks"]:
            return {"ok": False, "error": "Unknown network"}
        nid = split(nets)
        publish(nets, True)
        return {"ok": True, "network": {"id": nid, "name": nets["networks"][nid]["name"]}}
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
