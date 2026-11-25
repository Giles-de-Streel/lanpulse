#!/usr/bin/env python3
"""
lanpulse netcheck: network health checks that need the box itself (root + host tools).
Runs on the host every 10 minutes (systemd timer from setup.sh) and writes
/opt/monitor/data/netcheck.json; the collector's "netcheck" source shows it on the dashboard.

Checks (each can be turned off in /opt/monitor/lanpulse.env, NETCHECK_<NAME>=0):
  dhcp     rogue DHCP servers           nmap broadcast-dhcp-discover
  nat      double NAT / carrier NAT     traceroute + router's UPnP WAN address
  upnp     ports opened to the internet upnpc -l
  loss     packet loss + jitter         ping (gateway and internet)
  dns      DNS response time            dig
  publicip public IP + changes          https://api.ipify.org (the only outside call; NETCHECK_PUBLICIP=0 to skip)
  airspace Wi-Fi survey with the box's spare radio: own APs, channels, congestion, impostors (iw)
  devices  devices seen by NetAlertX: online, new, unidentified

Status values: up (fine), warn (needs a look), down (broken), unknown (couldn't tell).
Standard library only; usage:  sudo python3 netcheck.py [--print]
"""
import ipaddress, json, os, re, subprocess, sys, time, datetime, urllib.request

APP = os.environ.get("APP", "/opt/monitor")
OUT = os.path.join(APP, "data", "netcheck.json")
STATE = os.path.join(APP, "data", "netcheck-state.json")
INTERNET_TARGET = "1.1.1.1"


def env(name, default=""):
    """Read a setting from the environment, then /opt/monitor/lanpulse.env."""
    if name in os.environ:
        return os.environ[name]
    try:
        for line in open(os.path.join(APP, "lanpulse.env")):
            k, _, v = line.strip().partition("=")
            if k == name:
                return v.strip().strip('"')
    except OSError:
        pass
    return default


def enabled(check):
    return env(f"NETCHECK_{check.upper()}", "1") not in ("0", "false", "no", "off")


def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except FileNotFoundError:
        return 127, f"{cmd[0]} not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out"


def is_private(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private and not is_cgnat(ip)
    except ValueError:
        return False


CARRIER_NETS = [ipaddress.ip_network(n) for n in (
    "100.64.0.0/10",   # carrier-grade NAT (RFC 6598)
    "192.0.0.0/24",    # IETF protocol assignments: 464XLAT / DS-Lite (e.g. T-Mobile 5G), not a home router
)]


def is_cgnat(ip):
    """Addresses used by internet providers' NAT, never by a router on the premises."""
    try:
        a = ipaddress.ip_address(ip)
        return any(a in n for n in CARRIER_NETS)
    except ValueError:
        return False


def default_route():
    _, out = run(["ip", "-4", "route", "show", "default"])
    m = re.search(r"default via (\S+) dev (\S+)", out)
    return (m.group(1), m.group(2)) if m else (None, None)


def iface_kind(dev):
    return "Wi-Fi" if dev and (dev.startswith("wl") or os.path.isdir(f"/sys/class/net/{dev}/wireless")) else "wired"


def result(cid, name, status, summary, **detail):
    return {"id": cid, "name": name, "status": status, "summary": summary, "detail": detail}


# ------------------------------------------------------------------ checks

def check_dhcp(gw, dev):
    expected = [x.strip() for x in env("NETCHECK_DHCP_EXPECTED", gw or "").split(",") if x.strip()]
    rc, out = run(["nmap", "-n", "--script", "broadcast-dhcp-discover", "-e", dev,
                   "--script-args", "broadcast-dhcp-discover.timeout=8"], timeout=60)
    servers = sorted(set(re.findall(r"Server Identifier:\s*([\d.]+)", out)))
    if rc == 127:
        return result("dhcp", "DHCP servers", "unknown", "nmap is not installed")
    if not servers:
        return result("dhcp", "DHCP servers", "warn",
                      "No DHCP server answered. New devices may not be able to join", servers=[])
    rogue = [s for s in servers if s not in expected]
    if rogue:
        return result("dhcp", "DHCP servers", "warn",
                      f"Extra DHCP server(s) found: {', '.join(rogue)}. A second router or mesh unit "
                      f"may be handing out addresses (expected only {', '.join(expected)})",
                      servers=servers, expected=expected, rogue=rogue)
    return result("dhcp", "DHCP servers", "up", f"Only the router ({', '.join(servers)}) hands out addresses",
                  servers=servers, expected=expected)


def traceroute_hops():
    rc, out = run(["traceroute", "-n", "-I", "-q", "1", "-w", "1", "-m", "6", INTERNET_TARGET], timeout=40)
    hops = []
    for line in out.splitlines()[1:]:
        m = re.match(r"\s*(\d+)\s+(\S+)(?:\s+([\d.]+) ms)?", line)
        if m:
            hops.append({"hop": int(m.group(1)), "ip": None if m.group(2) == "*" else m.group(2),
                         "ms": float(m.group(3)) if m.group(3) else None})
    return hops


def upnp_info():
    rc, out = run(["upnpc", "-l"], timeout=30)
    if rc == 127:
        return {"available": None, "error": "upnpc not installed"}
    if "No IGD UPnP Device found" in out or "No valid UPNP Internet Gateway Device" in out:
        return {"available": False}
    wan = re.search(r"ExternalIPAddress = ([\d.]+)", out)
    maps = []
    for m in re.finditer(r"^\s*\d+\s+(TCP|UDP)\s+(\d+)->([\d.]+):(\d+)\s+'([^']*)'", out, re.M):
        maps.append({"proto": m.group(1), "external_port": int(m.group(2)), "internal": f"{m.group(3)}:{m.group(4)}",
                     "description": m.group(5)})
    return {"available": True, "wan_ip": wan.group(1) if wan else None, "mappings": maps}


def check_nat(gw, upnp):
    hops = traceroute_hops()
    seen = [h for h in hops if h["ip"]]
    if not seen:
        return result("nat", "Router layout (NAT)", "unknown", "Traceroute got no replies", hops=hops)
    # private hops answering at LAN speed (< 3 ms) are routers on the premises
    # On-site routers: the run of private hops from hop 1 that answer about as fast as the first
    # router does (within 3 ms of it), so a Wi-Fi-connected box doesn't skew the result.
    base = seen[0]["ms"] or 0
    onsite = []
    for h in seen:
        if is_private(h["ip"]) and h["ms"] is not None and h["ms"] <= base + 3:
            onsite.append(h)
        else:
            break
    carrier = [h for h in seen if is_cgnat(h["ip"]) or (is_private(h["ip"]) and h not in onsite)]
    wan = upnp.get("wan_ip") if upnp else None
    notes = []
    if wan and (is_private(wan) or is_cgnat(wan)):
        notes.append(f"router's internet-side address is {wan} (not public)")
    if len(onsite) >= 2:
        chain = " → ".join(h["ip"] for h in onsite)
        return result("nat", "Router layout (NAT)", "warn",
                      f"Double NAT: {len(onsite)} routers in a row on site ({chain}). "
                      "Port forwarding, some cameras and video calls can misbehave", hops=hops, wan_ip=wan)
    if carrier or (wan and is_cgnat(wan)):
        return result("nat", "Router layout (NAT)", "up",
                      "One router on site; the internet provider adds its own NAT (carrier-grade NAT). "
                      "Normal for 5G/fixed-wireless; inbound port forwarding won't work", hops=hops, wan_ip=wan)
    return result("nat", "Router layout (NAT)", "up", "One router between this network and the internet",
                  hops=hops, wan_ip=wan, notes=notes)


def check_upnp(upnp):
    if upnp.get("available") is None:
        return result("upnp", "Ports open to the internet (UPnP)", "unknown", upnp.get("error", "unknown"))
    if not upnp["available"]:
        return result("upnp", "Ports open to the internet (UPnP)", "up",
                      "UPnP is off or not answering, so devices can't open ports by themselves", mappings=[])
    maps = upnp["mappings"]
    if not maps:
        return result("upnp", "Ports open to the internet (UPnP)", "up",
                      "UPnP is on, but no device has opened a port", mappings=[])
    listing = "; ".join(f"{m['proto']} {m['external_port']} → {m['internal']}"
                        f"{' (' + m['description'] + ')' if m['description'] else ''}" for m in maps[:6])
    return result("upnp", "Ports open to the internet (UPnP)", "warn",
                  f"{len(maps)} port(s) opened automatically by devices: {listing}"
                  f"{' …' if len(maps) > 6 else ''}", mappings=maps)


def ping_stats(target, count=20):
    rc, out = run(["ping", "-n", "-q", "-c", str(count), "-i", "0.2", "-W", "1", target], timeout=count + 15)
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", out)
    return {"loss_pct": float(loss.group(1)) if loss else 100.0,
            "avg_ms": float(rtt.group(2)) if rtt else None,
            "jitter_ms": float(rtt.group(4)) if rtt else None}


def check_loss(gw):
    out = []
    for cid, name, target, j_warn in (("loss-gw", "Connection to router", gw, 30),
                                      ("loss-net", "Connection to internet", INTERNET_TARGET, 60)):
        if not target:
            continue
        s = ping_stats(target)
        txt = f"{s['loss_pct']:.0f}% loss" + (f", avg {s['avg_ms']:.0f} ms, jitter {s['jitter_ms']:.0f} ms"
                                               if s["avg_ms"] is not None else "")
        if s["loss_pct"] >= 50:
            st, why = "down", "Mostly unreachable"
        elif s["loss_pct"] > 2:
            st, why = "warn", "Losing packets"
        elif s["jitter_ms"] and s["jitter_ms"] > j_warn:
            st, why = "warn", "Unsteady (high jitter): calls and streams may stutter"
        else:
            st, why = "up", "Steady"
        out.append(result(cid, name, st, f"{why}: {txt}", target=target, **s))
    return out


def check_dns(gw):
    server = env("NETCHECK_DNS_SERVER", gw or "")
    rc, out = run(["dig", f"@{server}", "example.com", "+tries=1", "+time=3"], timeout=15) if server else (1, "")
    t = re.search(r"Query time: (\d+) msec", out)
    if rc != 0 or "NOERROR" not in out:
        return result("dns", "Name lookups (DNS)", "down", f"DNS server {server} didn't answer", server=server)
    ms = int(t.group(1)) if t else None
    st = "warn" if ms is not None and ms > 300 else "up"
    return result("dns", "Name lookups (DNS)", st,
                  f"{'Slow' if st == 'warn' else 'Answering'}: {ms} ms via {server}", server=server, ms=ms)


def check_publicip(state):
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as r:
            ip = r.read().decode().strip()
    except Exception as e:
        return result("publicip", "Public IP address", "unknown", f"Couldn't look it up: {e}")
    hist = state.setdefault("public_ip_history", [])
    now = int(time.time())
    if not hist or hist[-1]["ip"] != ip:
        hist.append({"ip": ip, "since": now})
        del hist[:-20]
    changed_recently = len(hist) > 1 and now - hist[-1]["since"] < 86400
    kind = "carrier NAT" if is_cgnat(ip) else "public"
    summary = f"{ip} ({kind})" + (f". Changed in the last 24 h, previously {hist[-2]['ip']}" if changed_recently else "")
    # shown, but never alarming: dynamic IPs change; the dashboard stays green
    return result("publicip", "Public IP address", "up", summary, ip=ip, history=hist[-5:])


def wifi_iface():
    for dev in sorted(os.listdir("/sys/class/net")):
        if os.path.isdir(f"/sys/class/net/{dev}/wireless"):
            return dev
    return None


def freq_info(f):
    f = float(f)
    if f < 3000:
        return "2.4", int(round((f - 2407) / 5)) if f != 2484 else 14
    if f < 5925:
        return "5", int(round((f - 5000) / 5))
    return "6", int(round((f - 5950) / 5))


def scan_wifi(dev):
    run(["ip", "link", "set", "dev", dev, "up"], timeout=10)
    rc, out = run(["iw", "dev", dev, "scan"], timeout=40)
    if rc == 127:
        return None, "iw not installed"
    if rc != 0 and "BSS" not in out:
        return None, out.strip().splitlines()[-1][:160] if out.strip() else "scan failed"
    nets = []
    for block in re.split(r"\n(?=BSS [0-9a-f:]{17})", "\n" + out):
        m = re.match(r"\s*BSS ([0-9a-f:]{17})", block)
        if not m:
            continue
        fr = re.search(r"freq: ([\d.]+)", block)
        if not fr:
            continue
        band, chan = freq_info(fr.group(1))
        ssid = re.search(r"^\s*SSID: (.*)$", block, re.M)
        sig = re.search(r"signal: (-?[\d.]+) dBm", block)
        util = re.search(r"channel utilisation: (\d+)/255", block)
        sta = re.search(r"station count: (\d+)", block)
        cap = re.search(r"capability: (.*)", block)
        nets.append({"bssid": m.group(1), "ssid": (ssid.group(1).strip() if ssid else "") or None,
                     "band": band, "channel": chan, "signal_dbm": float(sig.group(1)) if sig else None,
                     "secure": bool(cap and "Privacy" in cap.group(1)),
                     "utilisation_pct": round(int(util.group(1)) * 100 / 255) if util else None,
                     "stations": int(sta.group(1)) if sta else None,
                     "associated": "-- associated" in block.split("\n", 1)[0]})
    return nets, None


def check_airspace(state):
    dev = wifi_iface()
    if not dev:
        return result("airspace", "Wi-Fi airspace", "unknown", "No Wi-Fi adapter on this box"), None
    nets, err = scan_wifi(dev)
    if nets is None:
        return result("airspace", "Wi-Fi airspace", "unknown", f"Scan failed: {err}"), None
    own = [s.strip() for s in env("NETCHECK_OWN_SSIDS", "").split(",") if s.strip()]
    if not own:
        own = sorted({n["ssid"] for n in nets if n["associated"] and n["ssid"]}) or state.get("own_ssids", [])
    state["own_ssids"] = own
    for n in nets:
        n["own"] = n["ssid"] in own
    mine = [n for n in nets if n["own"]]
    # Learn this site's access points on the first run; afterwards an access point using one of
    # your network names that wasn't there before is flagged for 24 h (new mesh node, or an impostor).
    known = state.setdefault("known_bssids", {})          # bssid -> first seen (epoch)
    now = int(time.time())
    learned_at = state.setdefault("learned_at", now)
    for n in mine:
        known.setdefault(n["bssid"], now)
    problems = []
    recent = [n for n in mine if known[n["bssid"]] > learned_at and now - known[n["bssid"]] < 86400]
    if recent:
        problems.append("new access point(s) using your network name in the last 24 h: " +
                        ", ".join(f"{n['bssid']} ({n['band']} GHz ch {n['channel']})" for n in recent))
    open_own = [n for n in mine if not n["secure"]]
    if open_own:
        problems.append("open (no password) network using your name: " + ", ".join(n["bssid"] for n in open_own))
    bad_24 = sorted({n["channel"] for n in mine if n["band"] == "2.4" and n["channel"] not in (1, 6, 11)})
    if bad_24:
        problems.append(f"2.4 GHz on channel {', '.join(map(str, bad_24))}: only 1, 6 or 11 avoid overlapping neighbours")
    busy = [n for n in mine if (n["utilisation_pct"] or 0) > 60]
    if busy:
        problems.append("busy channel(s): " + ", ".join(f"{n['band']} GHz ch {n['channel']} {n['utilisation_pct']}%" for n in busy))
    neighbours = [n for n in nets if not n["own"]]
    bands = sorted({n["band"] for n in mine})
    if not own:
        summ = f"{len(nets)} access points heard. Set NETCHECK_OWN_SSIDS to analyse your own network"
        st = "up"
    elif not mine:
        summ, st = f"None of your networks ({', '.join(own)}) are visible from this box", "warn"
    else:
        summ = (f"{len(mine)} of your access points ({'/'.join(bands)} GHz), {len(neighbours)} neighbouring"
                + (". " + "; ".join(problems) if problems else ". No problems spotted"))
        st = "warn" if problems else "up"
    extra = {"interface": dev, "own_ssids": own, "networks": sorted(nets, key=lambda n: (not n["own"], n["band"], -(n["signal_dbm"] or -100)))}
    return result("airspace", "Wi-Fi airspace", st, summ, problems=problems), extra


def check_devices(state):
    db = os.path.join(APP, "netalertx", "db", "app.db")
    if not os.path.exists(db):
        return result("devices", "Devices on the network", "unknown", "NetAlertX database not found"), None
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        rows = con.execute("SELECT devMac, devName, devLastIP, devVendor, devFirstConnection, devLastConnection, "
                           "devPresentLastScan, devIsNew, devSSID, devParentMAC FROM Devices WHERE devIsArchived=0 "
                           "AND devMac <> 'internet'").fetchall()
        con.close()
    except Exception as e:
        return result("devices", "Devices on the network", "unknown", f"Couldn't read NetAlertX: {e}"), None
    def ts(s):
        try:
            return datetime.datetime.fromisoformat(str(s)[:19]).timestamp()
        except (TypeError, ValueError):
            return None
    # "New" = first seen in the last 24 h, but not part of the initial sweep when monitoring
    # started (NetAlertX's own "new" flag stays on until someone acknowledges it in NetAlertX).
    firsts = [t for t in (ts(r[4]) for r in rows) if t]
    sweep_end = (min(firsts) + 3600) if firsts else 0
    now = time.time()
    devs = []
    for mac, name, ip, vendor, first, last, present, _flag, ssid, parent in rows:
        unnamed = not name or name.startswith("(") or name.lower() in ("unknown", "(name not found)")
        rnd = len(mac) >= 2 and mac[1].lower() in "26ae"          # locally administered = randomized MAC
        t = ts(first)
        devs.append({"mac": mac, "name": None if unnamed else name, "ip": ip, "vendor": vendor or None,
                     "first_seen": first, "last_seen": last, "online": bool(present),
                     "new": bool(t and t > sweep_end and now - t < 86400),
                     "random_mac": rnd, "ssid": ssid or None})
    online = [d for d in devs if d["online"]]
    new = [d for d in devs if d["new"]]
    unknown = [d for d in online if not d["name"] and not d["vendor"] and not d["random_mac"]]
    parts = [f"{len(online)} online now, {len(devs)} seen in total"]
    if new:
        parts.append(f"{len(new)} new: " + ", ".join(d["name"] or d["vendor"] or d["ip"] or d["mac"] for d in new[:5]))
    if unknown:
        parts.append(f"{len(unknown)} unidentified")
    st = "warn" if new else "up"
    devs.sort(key=lambda d: [int(x) if x.isdigit() else 0 for x in (d["ip"] or "0.0.0.0").split(".")])
    return result("devices", "Devices on the network", st, "; ".join(parts),
                  online=len(online), total=len(devs), new=len(new)), {"devices": devs}


# ------------------------------------------------------------------ main

def main():
    gw, dev = default_route()
    try:
        state = json.load(open(STATE))
    except (OSError, ValueError):
        state = {}
    checks = []
    upnp = upnp_info() if (enabled("upnp") or enabled("nat")) else {}
    if enabled("dhcp") and dev:
        checks.append(check_dhcp(gw, dev))
    if enabled("nat"):
        checks.append(check_nat(gw, upnp))
    if enabled("upnp"):
        checks.append(check_upnp(upnp))
    if enabled("loss"):
        checks += check_loss(gw)
    if enabled("dns"):
        checks.append(check_dns(gw))
    if enabled("publicip"):
        checks.append(check_publicip(state))
    extras = {}
    if enabled("airspace"):
        c, x = check_airspace(state)
        checks.append(c)
        if x:
            extras["airspace"] = x
    if enabled("devices"):
        c, x = check_devices(state)
        checks.append(c)
        if x:
            extras.update(x)
    exposure = {"upnp": upnp, "public_ip": next((c["detail"].get("ip") for c in checks if c["id"] == "publicip"), None),
                "nat": next((c["summary"] for c in checks if c["id"] == "nat"), None)}
    extras["exposure"] = exposure
    report = {"generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "interface": dev, "interface_kind": iface_kind(dev), "gateway": gw, "checks": checks,
              "extras": extras}
    if iface_kind(dev) == "Wi-Fi":
        report["note"] = "The box is on Wi-Fi; plug it into the router for reliable results"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    for path, data in ((OUT, report), (STATE, state)):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    if "--print" in sys.argv:
        for c in checks:
            print(f"{c['status']:8} {c['name']}: {c['summary']}")


if __name__ == "__main__":
    main()
