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

The box can move between networks: each run first asks netswitch.py which network it is on, and
everything learned (public IP, access points, speed "normal", which devices are new) is kept per
network, so a new site starts fresh instead of raising false alarms.
Standard library only; usage:  sudo python3 netcheck.py [--print]
"""
import ipaddress, json, os, re, subprocess, sys, time, datetime, urllib.request

APP = os.environ.get("APP", "/opt/monitor")
OUT = os.path.join(APP, "data", "netcheck.json")
STATE = os.path.join(APP, "data", "netcheck-state.json")    # replaced per network in main()
NETWORK = None                                               # current network record (netswitch.py)
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


# Internet uplink probes: modems/gateways that publish link quality on the LAN without a login.
# Each returns a dict (see check_uplink) or None when the device isn't that kind.

def _get_json(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def uplink_tmobile(gw):
    """T-Mobile Home Internet gateways (Arcadyan, Sagemcom, Nokia): /TMI/v1/gateway."""
    d = _get_json(f"http://{gw}/TMI/v1/gateway?get=all")
    if not d or "signal" not in d:
        return None
    sig = d["signal"]
    cells = []
    for tech in ("5g", "4g"):
        s = sig.get(tech)
        if s:
            cells.append({"tech": tech.upper(), "bands": s.get("bands", []), "bars": s.get("bars"),
                          "rsrp": s.get("rsrp"), "rsrq": s.get("rsrq"), "sinr": s.get("sinr"), "rssi": s.get("rssi")})
    dev = d.get("device", {})
    return {"kind": "cellular", "provider": "T-Mobile Home Internet",
            "model": f"{dev.get('manufacturer', '')} {dev.get('model', '')}".strip(),
            "firmware": dev.get("softwareVersion"), "uptime_s": (d.get("time") or {}).get("upTime"),
            "registered": (sig.get("generic") or {}).get("registration") == "registered", "cells": cells}


UPLINKS = {"tmobile": uplink_tmobile}


def cell_quality(c):
    """Rate a cellular link from RSRP (signal strength) and SINR (signal vs noise)."""
    rsrp, sinr = c.get("rsrp"), c.get("sinr")
    if rsrp is None and sinr is None:
        return "unknown", "no signal figures"
    bad = (rsrp is not None and rsrp < -115) or (sinr is not None and sinr < 0)
    fair = (rsrp is not None and rsrp < -100) or (sinr is not None and sinr < 10)
    return ("weak" if bad else "fair" if fair else "good"), None


def check_uplink(gw, state):
    probes = [p.strip() for p in env("NETCHECK_UPLINK_TYPES", ",".join(UPLINKS)).split(",") if p.strip() in UPLINKS]
    host = env("NETCHECK_UPLINK_HOST", gw or "")
    info = None
    for p in probes:
        info = UPLINKS[p](host)
        if info:
            break
    if not info:
        return None, None                      # nothing recognised: the check simply doesn't appear
    if info["kind"] == "cellular":
        if not info["registered"] or not info["cells"]:
            return result("uplink", "Internet uplink", "down",
                          f"{info['provider']} gateway is not connected to the cell network"), info
        parts, worst = [], "good"
        for c in info["cells"]:
            q, _ = cell_quality(c)
            c["quality"] = q
            if q == "weak" or (q == "fair" and worst == "good"):
                worst = q
            parts.append(f"{c['tech']} {'/'.join(c['bands'])}: {q}"
                         + (f", {c['bars']:.0f} bars" if c.get("bars") is not None else "")
                         + (f", RSRP {c['rsrp']}" if c.get("rsrp") is not None else "")
                         + (f", SINR {c['sinr']}" if c.get("sinr") is not None else ""))
        # remember gateway reboots (uptime going backwards)
        up = info.get("uptime_s")
        if up is not None:
            last = state.get("uplink_uptime")
            if last is not None and up < last:
                state["uplink_last_reboot"] = int(time.time() - up)
            state["uplink_uptime"] = up
        reb = state.get("uplink_last_reboot")
        recent = reb and time.time() - reb < 86400
        st = "warn" if worst == "weak" or recent else "up"
        summ = f"{info['provider']}, " + "; ".join(parts) + \
               (". The gateway restarted in the last 24 h" if recent else "") + \
               (". Weak signal: moving the gateway near a window or higher up often helps" if worst == "weak" else "")
        return result("uplink", "Internet uplink", st, summ), info
    return None, None


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
        rsn = re.search(r"\n\s*RSN:(.*?)(?=\n\s*[A-Z][\w /-]*:|\Z)", block, re.S)
        auth = re.search(r"Authentication suites: (.*)", rsn.group(1)) if rsn else None
        a = auth.group(1) if auth else ""
        if "SAE" in a:
            security = "WPA3/WPA2" if "PSK" in a else "WPA3"
        elif "OWE" in a:
            security = "Enhanced Open"
        elif "IEEE 802.1X" in a:
            security = "WPA2-Enterprise"
        elif rsn:
            security = "WPA2"
        elif re.search(r"\n\s*WPA:", block):
            security = "WPA (old)"
        elif cap and "Privacy" in cap.group(1):
            security = "WEP (old)"
        else:
            security = "Open"
        width = re.search(r"\* channel width: \d+ \((\d+) MHz\)", block) or re.search(r"STA channel width: (\d+)", block)
        nets.append({"bssid": m.group(1), "ssid": (ssid.group(1).strip() if ssid else "") or None,
                     "band": band, "channel": chan, "signal_dbm": float(sig.group(1)) if sig else None,
                     "secure": bool(cap and "Privacy" in cap.group(1)), "security": security,
                     "width_mhz": int(width.group(1)) if width else None,
                     "utilisation_pct": round(int(util.group(1)) * 100 / 255) if util else None,
                     "stations": int(sta.group(1)) if sta else None,
                     "associated": "-- associated" in block.split("\n", 1)[0]})
    return nets, None


def radio_bands(dev):
    """Bands this box's Wi-Fi adapter can hear ('2.4', '5', '6'), from iw's frequency list."""
    _, out = run(["iw", "phy"], timeout=10)
    bands = set()
    for f, flags in re.findall(r"\*\s+(\d+)(?:\.\d+)? MHz \[\d+\](.*)", out):
        if "disabled" in flags:
            continue
        b, _ = freq_info(f)
        bands.add(b)
    return sorted(bands, key=float)


def congestion(nets, own_bssids):
    """Per band and channel: how many networks, how strong, and how busy the airtime is.
    Busy-ness comes from access points that report channel utilisation (QBSS load); otherwise it is
    estimated from the number of audible networks on (and, for 2.4 GHz, overlapping) the channel."""
    chans = {}
    for n in nets:
        key = (n["band"], n["channel"])
        c = chans.setdefault(key, {"band": n["band"], "channel": n["channel"], "networks": 0, "yours": 0,
                                   "strongest_dbm": None, "utilisation_pct": None, "overlapping": 0})
        c["networks"] += 1
        c["yours"] += n["bssid"] in own_bssids
        if n["signal_dbm"] is not None and (c["strongest_dbm"] is None or n["signal_dbm"] > c["strongest_dbm"]):
            c["strongest_dbm"] = n["signal_dbm"]
        if n["utilisation_pct"] is not None:
            c["utilisation_pct"] = max(c["utilisation_pct"] or 0, n["utilisation_pct"])
    for c in chans.values():
        if c["band"] == "2.4":
            c["overlapping"] = sum(1 for n in nets if n["band"] == "2.4" and 0 < abs(n["channel"] - c["channel"]) <= 4
                                   and (n["signal_dbm"] or -100) >= -80)
        audible = sum(1 for n in nets if n["band"] == c["band"] and n["channel"] == c["channel"]
                      and (n["signal_dbm"] or -100) >= -80)
        load = audible + 1.5 * c["overlapping"]
        u = c["utilisation_pct"]
        if u is not None:
            level = "heavy" if u > 70 else "moderate" if u > 40 else "light" if u > 15 else "clear"
        else:
            level = "heavy" if load >= 8 else "moderate" if load >= 5 else "light" if load >= 2.5 else "clear"
        c["congestion"] = level
        c["basis"] = "measured airtime" if u is not None else "estimated from neighbours"
    order = {"2.4": 0, "5": 1, "6": 2}
    return sorted(chans.values(), key=lambda c: (order.get(c["band"], 9), c["channel"]))


def ssid_summary(nets):
    """One row per visible network name, with its access points, bands, channels and security."""
    by = {}
    for n in nets:
        if not n["ssid"]:
            continue
        s = by.setdefault(n["ssid"], {"ssid": n["ssid"], "own": n["own"], "access_points": 0, "bands": set(),
                                      "channels": set(), "strongest_dbm": None, "security": set()})
        s["access_points"] += 1
        s["bands"].add(n["band"])
        s["channels"].add(f"{n['band']}/{n['channel']}")
        s["security"].add(n.get("security") or "?")
        if n["signal_dbm"] is not None and (s["strongest_dbm"] is None or n["signal_dbm"] > s["strongest_dbm"]):
            s["strongest_dbm"] = n["signal_dbm"]
    out = []
    for s in by.values():
        s["bands"] = sorted(s["bands"], key=float)
        s["channels"] = sorted(s["channels"], key=lambda x: (float(x.split("/")[0]), int(x.split("/")[1])))
        s["security"] = sorted(s["security"])
        out.append(s)
    out.sort(key=lambda s: (not s["own"], -(s["strongest_dbm"] or -100)))
    return out, sum(1 for n in nets if not n["ssid"])


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
    # Hidden networks broadcast by your own units (guest, mesh backhaul, setup) share the tail
    # of their radio address with one of your access points: treat them as yours, not neighbours.
    # Guest and extra networks (and mesh backhaul) from your own units are recognised the same way,
    # or by a radio address that differs from one of yours only in its last byte, so a guest Wi-Fi
    # isn't counted as a neighbour. They don't need adding to NETCHECK_OWN_SSIDS.
    tails = {n["bssid"][-5:] for n in mine}
    heads = {n["bssid"][:14] for n in mine}
    for n in nets:
        if n["own"]:
            continue
        if n["bssid"][-5:] in tails or n["bssid"][:14] in heads:
            n["own"], n["own_hidden"], n["own_extra"] = True, True, True
    neighbours = [n for n in nets if not n["own"]]
    audible = [n for n in neighbours if (n["signal_dbm"] or -100) >= int(env("NETCHECK_NEIGHBOUR_DBM", "-80"))]
    notes = []

    # 2.4 GHz: channels are ~4 wide, so a neighbour 1-4 channels away overlaps partially. That is
    # worse than sharing the same channel (Wi-Fi takes turns on one channel, but partial overlap is
    # just noise). Only flag it when such neighbours are actually heard.
    def pressure(ch):
        """Interference a 2.4 GHz channel would get from audible neighbours (higher = worse)."""
        total = 0.0
        for n in audible:
            if n["band"] != "2.4":
                continue
            gap = abs(n["channel"] - ch)
            if gap <= 4:
                weight = 1.0 if gap == 0 else 1.5      # partial overlap counts extra
                total += weight * (10 ** (((n["signal_dbm"] or -90) + 90) / 20))
        return total
    for n in [m for m in mine if m["band"] == "2.4" and not m.get("own_hidden")]:
        overl = [a for a in audible if a["band"] == "2.4" and 0 < abs(a["channel"] - n["channel"]) <= 4]
        best = min((1, 6, 11), key=pressure)
        if overl:
            better = f"; channel {best} looks clearest" if best != n["channel"] and pressure(best) < pressure(n["channel"]) else ""
            problems.append(f"2.4 GHz channel {n['channel']} partly overlaps {len(overl)} neighbouring network(s) "
                            f"on channel {', '.join(sorted({str(a['channel']) for a in overl}, key=int))}{better}")
        elif n["channel"] not in (1, 6, 11):
            notes.append(f"2.4 GHz is on channel {n['channel']}; fine for now (no neighbours close enough to overlap)")
    # 5 GHz/6 GHz: channels don't overlap partially; strong neighbours on the same channel share airtime.
    for n in [m for m in mine if m["band"] in ("5", "6") and not m.get("own_hidden")]:
        same = [a for a in audible if a["band"] == n["band"] and a["channel"] == n["channel"] and (a["signal_dbm"] or -100) >= -67]
        if len(same) >= 2:
            problems.append(f"{n['band']} GHz channel {n['channel']} is shared with {len(same)} strong neighbouring networks")
    # congestion on the channels your network uses
    chans = congestion(nets, {n["bssid"] for n in nets if n["own"]})   # incl. your hidden/guest networks
    for c in chans:
        own_only = c["utilisation_pct"] is not None and c["networks"] == c["yours"] and not c["overlapping"]
        if own_only:
            continue   # busy with your own traffic, not interference: the "Wi-Fi network utilization" tile reports it
        if c["yours"] and c["congestion"] in ("moderate", "heavy"):
            msg = f"{c['band']} GHz channel {c['channel']} is {'heavily' if c['congestion'] == 'heavy' else 'moderately'} congested"
            msg += (f" ({c['utilisation_pct']}% of airtime in use)" if c["utilisation_pct"] is not None
                    else f" ({c['networks']} networks on it, {c['overlapping']} overlapping)")
            if c["congestion"] == "heavy" or c["utilisation_pct"] is not None:
                problems.append(msg)
            else:
                notes.append(msg)
    # old or missing security on your own network
    weak_sec = sorted({n["security"] for n in mine if n.get("security") in ("Open", "WEP (old)", "WPA (old)")})
    if weak_sec:
        problems.append(f"your network uses {', '.join(weak_sec)} security: switch to WPA2 or WPA3")
    # 6 GHz
    hear = radio_bands(dev)
    if "6" not in hear:
        notes.append("this box's Wi-Fi adapter can't hear 6 GHz, so 6 GHz networks won't appear")
    weak_here = [n for n in mine if not n.get("own_hidden") and (n["signal_dbm"] or 0) < -75]
    if weak_here and wifi_iface() and len(weak_here) == len([m for m in mine if not m.get("own_hidden")]):
        notes.append("your Wi-Fi is weak where this box sits")
    mine = [n for n in mine if not n.get("own_hidden")]
    bands = sorted({n["band"] for n in mine})
    if not own:
        summ = f"{len(nets)} access points heard. Set NETCHECK_OWN_SSIDS to analyse your own network"
        st = "up"
    elif not mine:
        summ, st = f"None of your networks ({', '.join(own)}) are visible from this box", "warn"
    else:
        summ = (f"{len(mine)} of your access points ({'/'.join(bands)} GHz), {len(neighbours)} neighbouring"
                + (". " + "; ".join(problems) if problems else ". No interference spotted")
                + (". Note: " + "; ".join(notes) if notes else ""))
        st = "warn" if problems else "up"
    ssids, hidden = ssid_summary(nets)
    extra = {"interface": dev, "adapter_bands": hear, "own_ssids": own, "notes": notes, "problems": problems,
             "ssids": ssids, "hidden_count": hidden, "channels": chans,
             "networks": sorted(nets, key=lambda n: (not n["own"], float(n["band"]), -(n["signal_dbm"] or -100)))}
    return result("airspace", "Wi-Fi airspace", st, summ, problems=problems), extra


def check_speed():
    """Internet speed from Speedtest Tracker: latest result vs this site's normal (median of the
    last 14 days). Flags a big drop or failing tests, so 'normal' adapts to every connection."""
    db = env("NETCHECK_SPEED_DB", os.path.join(APP, "speedtest-tracker", "database.sqlite"))
    if not os.path.exists(db):
        return None, None
    import sqlite3, statistics
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        rows = con.execute("SELECT status, ping, download, upload, created_at, data FROM results "
                           "WHERE created_at >= datetime('now', '-14 days') ORDER BY created_at").fetchall()
        con.close()
    except Exception as e:
        return result("speed", "Internet speed", "unknown", f"Couldn't read Speedtest Tracker: {e}"), None
    periods = (NETWORK or {}).get("periods") or []
    def here(created):
        """Was this test run while the box was on the current network? (times are UTC, no zone)"""
        if not periods:
            return True
        try:
            t = datetime.datetime.fromisoformat(str(created)[:19]).replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            return True
        return any(a - 600 <= t <= (b or time.time()) + 600 for a, b in periods)
    rows = [r for r in rows if here(r[4])]
    hist = []
    for status, ping, down, up, created, data in rows:
        ok = str(status).lower() in ("completed", "success", "1")
        extra = {}
        try:
            d = json.loads(data or "{}")
            extra = {"jitter_ms": (d.get("ping") or {}).get("jitter"), "loss_pct": d.get("packetLoss"),
                     "server": (d.get("server") or {}).get("name"), "isp": d.get("isp")}
        except ValueError:
            pass
        # Speedtest Tracker stores bytes per second
        hist.append({"at": created, "ok": ok, "ping_ms": ping, "down_mbps": round(down * 8 / 1e6, 1) if down else None,
                     "up_mbps": round(up * 8 / 1e6, 1) if up else None, **extra})
    if not hist:
        return result("speed", "Internet speed", "pending",
                      "No speed tests on this network yet. Speedtest Tracker runs one every few hours"), {"history": []}
    good = [h for h in hist if h["ok"] and h["down_mbps"]]
    last = hist[-1]
    x = {"history": hist[-60:]}
    if not last["ok"]:
        fails = 0
        for h in reversed(hist):
            if h["ok"]:
                break
            fails += 1
        return result("speed", "Internet speed", "warn" if fails < 3 else "down",
                      f"The last {fails} speed test(s) failed"), x
    med_d = statistics.median(h["down_mbps"] for h in good) if good else None
    med_u = statistics.median(h["up_mbps"] for h in good if h["up_mbps"]) if good else None
    x.update(median_down_mbps=med_d, median_up_mbps=med_u, tests=len(good))
    txt = f"{last['down_mbps']:.0f} down / {last['up_mbps'] or 0:.0f} up Mbps, {last['ping_ms'] or 0:.0f} ms"
    pct = float(env("NETCHECK_SPEED_DROP_PCT", "50"))
    if len(good) >= 5 and med_d:
        txt += f" (normal here: {med_d:.0f} / {med_u or 0:.0f})"
        slow = last["down_mbps"] < med_d * (1 - pct / 100)
        # one slow test is noise; flag when the last two are both well below normal
        prev = good[-2] if len(good) >= 2 else None
        if slow and prev and prev["down_mbps"] < med_d * (1 - pct / 100):
            return result("speed", "Internet speed", "warn", f"Slower than normal: {txt}"), x
    else:
        txt += f" (learning what's normal: {len(good)} of 5 tests)"
    return result("speed", "Internet speed", "up", txt), x


def sync_names(db):
    """Copy names typed on the dashboard (config/settings.json "device_names") into NetAlertX, marked
    USER so its scans never overwrite them. An empty name hands the device back to NetAlertX's own naming."""
    import sqlite3
    try:
        names = json.load(open(os.path.join(APP, "config", "settings.json"))).get("device_names") or {}
    except (OSError, ValueError):
        return
    if not names:
        return
    try:
        con = sqlite3.connect(db, timeout=20)
        for mac, name in names.items():
            if name:
                con.execute("UPDATE Devices SET devName=?, devNameSource='USER' WHERE upper(devMac)=? "
                            "AND (devName IS NOT ? OR COALESCE(devNameSource,'') <> 'USER')", (name, mac.upper(), name))
            else:
                con.execute("UPDATE Devices SET devName='(unknown)', devNameSource='' WHERE upper(devMac)=? "
                            "AND devNameSource='USER'", (mac.upper(),))
        con.commit()
        con.close()
    except Exception as e:
        print(f"name sync: {e}", file=sys.stderr)


def check_devices(state):
    db = os.path.join(APP, "netalertx", "db", "app.db")
    if not os.path.exists(db):
        return result("devices", "Devices on the network", "unknown", "NetAlertX database not found"), None
    import sqlite3
    sync_names(db)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        rows = con.execute("SELECT devMac, devName, devLastIP, devVendor, devFirstConnection, devLastConnection, "
                           "devPresentLastScan, devIsNew, devSSID, devParentMAC FROM Devices WHERE devIsArchived=0 "
                           "AND devMac <> 'internet'").fetchall()
        con.close()
    except Exception as e:
        return result("devices", "Devices on the network", "unknown", f"Couldn't read NetAlertX: {e}"), None
    def ts(s):
        """NetAlertX stores times in UTC without a zone marker."""
        try:
            return datetime.datetime.fromisoformat(str(s)[:19]).replace(tzinfo=datetime.timezone.utc).timestamp()
        except (TypeError, ValueError):
            return None
    # Only devices of the network the box is on now: last seen at an address in its subnet, during
    # a time the box was on it (another site using the same 192.168.1.x range doesn't count).
    net = NETWORK or {}
    subnet = None
    try:
        subnet = ipaddress.ip_network(net["subnet"]) if net.get("subnet") else None
    except ValueError:
        pass
    periods = net.get("periods") or []
    def on_this_network(ip, last):
        try:
            if subnet and ipaddress.ip_address(ip) not in subnet:
                return False
        except ValueError:
            return False
        t = ts(last)
        return not periods or t is None or any(a - 900 <= t <= (b or time.time()) + 900 for a, b in periods)
    rows = [r for r in rows if on_this_network(r[2], r[5])]
    # "New" = first seen in the last 24 h, but not part of the initial sweep when monitoring of this
    # network started (NetAlertX's own "new" flag stays on until someone acknowledges it in NetAlertX).
    firsts = [t for t in (ts(r[4]) for r in rows) if t]
    start = net.get("first_seen")
    sweep_end = (max(start, min(firsts) if firsts else start) + 3600) if start else ((min(firsts) + 3600) if firsts else 0)
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
    # the box's own adapters are never "new" or "unidentified"
    own = set()
    for dev in os.listdir("/sys/class/net"):
        try:
            own.add(open(f"/sys/class/net/{dev}/address").read().strip().lower())
        except OSError:
            pass
    for d in devs:
        if d["mac"].lower() in own:
            d["new"], d["this_box"] = False, True
    if enabled("identify"):          # "probably a Govee light" etc. for unnamed devices (identify.py)
        try:
            import identify
            identify.identify(devs, state)
        except Exception as e:
            print(f"identify: {e}", file=sys.stderr)
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
    return {**result("devices", "Devices on the network", st, "; ".join(parts),
                     online=len(online), total=len(devs), new=len(new)), "priority": "low"}, {"devices": devs}


# What to do, in plain words, when a check isn't green. Shown on the tile and in the banner.
HINTS = {
    "devices": "If you recognise them, there's nothing to do: they stop counting as new after 24 hours. "
               "If not, look them up in the device finder (NetAlertX, port 20211) or the router's device list.",
    "dhcp": "Find the device at that address and turn off its DHCP server, or switch it to access point (bridge) mode.",
    "nat": "Put the second router into access point / bridge mode (or the provider's modem into bridge mode), "
           "so there is only one router.",
    "upnp": "Check you recognise the devices listed. If nothing needs to be reachable from the internet, "
            "turn off UPnP in the router's settings.",
    "loss-gw": "Restart the router. If the box is on Wi-Fi, a cable gives a fairer reading.",
    "loss-net": "Usually the internet provider. Restart the modem/router; if it continues, contact the provider.",
    "dns": "Restart the router. If it keeps happening, set the router's DNS to 1.1.1.1 or 9.9.9.9.",
    "airspace": "Change the Wi-Fi channel in the router's settings (or set it to automatic). "
                "The Wi-Fi airspace details below show which channels are clearest.",
    "uplink": "Move the gateway near a window or higher up. Frequent restarts can mean power or overheating problems.",
    "speed": "Wait for the next test. If it stays slow, restart the gateway, then contact the provider.",
    "publicip": "",
}


# ------------------------------------------------------------------ main

NTP_EPOCH = 2208988800      # seconds between 1900 (NTP) and 1970 (Unix)


def _ntp_ts(b):
    sec, frac = int.from_bytes(b[:4], "big"), int.from_bytes(b[4:8], "big")
    return sec - NTP_EPOCH + frac / 2**32


def ntp_query(host, timeout=2.0):
    """One SNTP request. Returns (offset_s, round_trip_s, stratum): how far this box's clock is
    from the server (positive = the box is behind)."""
    import socket
    addr = socket.getaddrinfo(host, 123, 0, socket.SOCK_DGRAM)[0]
    with socket.socket(addr[0], socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t1 = time.time()
        s.sendto(b"\x23" + 47 * b"\0", addr[4])            # LI 0, version 4, mode 3 (client)
        data, _ = s.recvfrom(512)
        t4 = time.time()
    if len(data) < 48:
        raise ValueError("short reply")
    stratum = data[1]
    if stratum == 0:
        raise ValueError("server refused (kiss-o'-death)")
    t2, t3 = _ntp_ts(data[32:40]), _ntp_ts(data[40:48])
    return ((t2 - t1) + (t3 - t4)) / 2, (t4 - t1) - (t3 - t2), stratum


def box_addresses():
    """The monitoring box's own name and IPv4 addresses, main connection (default route) first."""
    import socket
    try:
        addrs = json.loads(subprocess.run(["ip", "-j", "-4", "addr", "show", "up"], capture_output=True,
                                          text=True, timeout=10).stdout or "[]")
        routes = json.loads(subprocess.run(["ip", "-j", "route", "show", "default"], capture_output=True,
                                           text=True, timeout=10).stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    metric = {r.get("dev"): r.get("metric", 0) for r in routes}
    ips = []
    for a in addrs:
        dev = a.get("ifname", "")
        if dev == "lo" or dev.startswith(("docker", "br-", "veth", "virbr", "tun", "wg", "tailscale")):
            continue
        kind = "Wi-Fi" if os.path.isdir(f"/sys/class/net/{dev}/wireless") else "wired"
        for i in a.get("addr_info", []):
            if i.get("family") == "inet" and i.get("scope") == "global":
                ips.append({"ip": i["local"], "dev": dev, "kind": kind, "main": False,
                            "_m": metric.get(dev, 1 << 30)})
    ips.sort(key=lambda x: x["_m"])
    for x in ips:
        if x["_m"] < 1 << 30 and not any(y["main"] for y in ips):
            x["main"] = True
        x.pop("_m")
    return {"hostname": socket.gethostname(), "ips": ips}


def time_sources():
    """Compare this box's clock with a few free public NTP servers (NETCHECK_NTP_SERVERS)."""
    hosts = [h.strip() for h in env("NETCHECK_NTP_SERVERS",
             "time.cloudflare.com,time.google.com,pool.ntp.org,time.nist.gov,time.windows.com").split(",") if h.strip()]
    out = []
    for h in hosts:
        try:
            off, rtt, stratum = ntp_query(h)
            out.append({"host": h, "ok": True, "offset_ms": round(off * 1000, 1), "rtt_ms": round(rtt * 1000, 1), "stratum": stratum})
        except Exception as e:
            out.append({"host": h, "ok": False, "error": str(e)[:80]})
    good = sorted(s["offset_ms"] for s in out if s["ok"])
    median = good[len(good) // 2] if good else None
    return {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "servers": out, "box_offset_ms": median}


def main():
    global NETWORK, STATE
    try:
        import netswitch                                    # same folder
        NETWORK = netswitch.ensure()
        if NETWORK:
            STATE = netswitch.state_path(NETWORK["id"])
            os.makedirs(os.path.dirname(STATE), mode=0o700, exist_ok=True)
    except Exception as e:
        print(f"netswitch: {e}", file=sys.stderr)
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
    if enabled("uplink"):
        c, x = check_uplink(gw, state)
        if c:
            checks.insert(0, c)
            extras["uplink"] = x
    if enabled("speed"):
        c, x = check_speed()
        if c:
            checks.insert(1 if extras.get("uplink") else 0, c)
            extras["speed"] = x
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
    box = box_addresses()
    if box:
        extras["box"] = box
    if enabled("ntp"):
        try:
            extras["time"] = time_sources()
        except Exception as e:
            print(f"ntp: {e}", file=sys.stderr)
    for c in checks:
        if c["status"] in ("warn", "down") and c["id"] in HINTS:
            c["hint"] = HINTS[c["id"]]
    report = {"generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "interface": dev, "interface_kind": iface_kind(dev), "gateway": gw, "checks": checks,
              "extras": extras,
              "network": {"id": NETWORK["id"], "name": NETWORK["name"]} if NETWORK else None}
    if iface_kind(dev) == "Wi-Fi":
        report["note"] = "The box is on Wi-Fi; plug it into the router for reliable results"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    for path, data, mode in ((OUT, report, 0o644), (STATE, state, 0o600)):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    if "--print" in sys.argv:
        for c in checks:
            print(f"{c['status']:8} {c['name']}: {c['summary']}")


if __name__ == "__main__":
    main()
