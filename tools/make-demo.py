#!/usr/bin/env python3
"""Generate lanpulse's generic demo snapshot:  python3 tools/make-demo.py > web/demo/snapshot.json Everything is invented:
generic names, 192.168.1.x addresses, locally administered 02:... MAC addresses."""
import json, random, datetime

random.seed(7)
T0 = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
iso = lambda dt: dt.isoformat(timespec="seconds")
ago = lambda **kw: iso(T0 - datetime.timedelta(**kw))
mac = lambda n: "02:00:5E:%02X:%02X:%02X" % (n // 65536 % 256, n // 256 % 256, n % 256)


def hist(pattern="up", n=30, blips=()):
    h = [{"s": "up"} for _ in range(n)]
    if pattern == "down":
        for i in range(n - 4, n):
            h[i] = {"s": "down"}
    for b in blips:
        h[b] = {"s": "down"}
    return h


def ping(i, name, ms, up=1.0, status="up", **kw):
    it = {"id": f"d{i}", "name": name, "kind": "ping", "status": status, "latency_ms": ms if status == "up" else None,
          "uptime_24h": up, "history": hist("down" if status == "down" else "up",
                                             blips=kw.pop("blips", ()))}
    it.update(kw)
    return it


W = lambda band, node, rssi, rate: {"type": "wifi", "band": band, "node": node, "rssi": rssi, "rate_mbps": rate}
WIRED = lambda node: {"type": "wired", "node": node}

groups = [
    {"name": "Internet", "items": [
        ping(1, "Internet connection", 18, link=None),
        {"id": "d2", "name": "Websites load", "kind": "http", "status": "up", "latency_ms": 142, "uptime_24h": 1, "history": hist()},
        ping(3, "Name lookups (DNS)", 4),
    ]},
    {"name": "Network equipment", "items": [
        ping(4, "Main router", 1, link=WIRED("Office switch")),
        ping(5, "Office switch", 1, link=WIRED("Main router")),
        ping(6, "Mesh point: Hall", 6, link=W("5", "Main router", -58, 1200)),
        ping(7, "Mesh point: Classroom wing", 11, up=0.986, link=W("5", "Mesh point: Hall", -74, 390), blips=(9, 21)),
        ping(8, "Access point: Kitchen", 2, link=WIRED("Office switch")),
    ]},
    {"name": "Office", "items": [
        ping(9, "Front office printer", 3, link=WIRED("Office switch")),
        ping(10, "Copier / scanner", 0, up=0.71, status="down", since=ago(minutes=12),
             message="No reply for 12 minutes", link=W("2.4", "Main router", -79, 39)),
        ping(11, "Office PC", 2, link=WIRED("Office switch")),
        ping(12, "File server (NAS)", 1, link=WIRED("Office switch")),
        ping(13, "Phone system", 2, link=WIRED("Office switch")),
    ]},
    {"name": "Security & building", "items": [
        ping(14, "Camera: Front door", 9, link=W("2.4", "Mesh point: Hall", -63, 72)),
        ping(15, "Camera: Parking lot", 14, up=0.973, link=W("2.4", "Mesh point: Classroom wing", -81, 26), blips=(4, 17, 26)),
        ping(16, "Door access controller", 2, link=WIRED("Office switch")),
        ping(17, "Thermostat: Hall", 21, link=W("2.4", "Mesh point: Hall", -66, 65)),
        ping(18, "Thermostat: Classroom wing", 25, link=W("2.4", "Mesh point: Classroom wing", -71, 52)),
        {"id": "d19", "name": "Fire panel dialer", "kind": "port", "status": "maintenance", "latency_ms": None,
         "uptime_24h": None, "history": hist(), "message": "Paused for scheduled service"},
    ]},
    {"name": "Media & meeting rooms", "items": [
        ping(20, "Hall projector", 4, link=WIRED("Office switch")),
        ping(21, "Livestream encoder", 3, link=WIRED("Office switch")),
        ping(22, "Smart TV: Lounge", 8, link=W("5", "Access point: Kitchen", -55, 433)),
        ping(23, "Sound board", 2, link=WIRED("Office switch")),
    ]},
    {"name": "Network health", "items": [
        {"id": "net-uplink", "name": "Internet uplink", "kind": "netcheck", "status": "up",
         "note": "Example 5G home internet, 5G n41: good, 4 bars, RSRP -88, SINR 21; LTE B66: fair, RSRP -104, SINR 9"},
        {"id": "net-speed", "name": "Internet speed", "kind": "netcheck", "status": "up",
         "note": "286 down / 22 up Mbps, 17 ms (normal here: 291 / 23)"},
        {"id": "net-dhcp", "name": "DHCP servers", "kind": "netcheck", "status": "warn",
         "note": "Extra DHCP server found: 192.168.1.60. A second router or mesh unit may be handing out addresses (expected only 192.168.1.1)",
         "hint": "Find the device at that address and turn off its DHCP server, or switch it to access point (bridge) mode."},
        {"id": "net-nat", "name": "Router layout (NAT)", "kind": "netcheck", "status": "up",
         "note": "One router between this network and the internet"},
        {"id": "net-upnp", "name": "Ports open to the internet (UPnP)", "kind": "netcheck", "status": "warn",
         "note": "2 port(s) opened automatically by devices: TCP 8443 → 192.168.1.41:443 (Camera NVR); UDP 3478 → 192.168.1.77:3478 (Game console)",
         "hint": "Check you recognise the devices listed. If nothing needs to be reachable from the internet, turn off UPnP in the router's settings."},
        {"id": "net-loss-gw", "name": "Connection to router", "kind": "netcheck", "status": "up", "note": "Steady: 0% loss, avg 1 ms, jitter 0 ms"},
        {"id": "net-loss-net", "name": "Connection to internet", "kind": "netcheck", "status": "up", "note": "Steady: 0% loss, avg 17 ms, jitter 3 ms"},
        {"id": "net-dns", "name": "Name lookups (DNS)", "kind": "netcheck", "status": "up", "note": "Answering: 6 ms via 192.168.1.1"},
        {"id": "net-publicip", "name": "Public IP address", "kind": "netcheck", "status": "up", "note": "203.0.113.45 (public)"},
        {"id": "net-airspace", "name": "Wi-Fi airspace", "kind": "netcheck", "status": "warn",
         "note": "5 of your access points (2.4/5/6 GHz), 9 neighbouring. 2.4 GHz channel 6 is heavily congested (78% of airtime in use); 2.4 GHz channel 3 partly overlaps 3 neighbouring network(s) on channel 1, 6; channel 11 looks clearest",
         "hint": "Change the Wi-Fi channel in the router's settings (or set it to automatic). The Wi-Fi airspace details below show which channels are clearest."},
        {"id": "net-devices", "name": "Devices on the network", "kind": "netcheck", "status": "warn", "priority": "low",
         "note": "41 online now, 63 seen in total; 2 new: Galaxy-Tab, (unknown device)",
         "hint": "If you recognise them, there's nothing to do: they stop counting as new after 24 hours. If not, look them up in the device finder (NetAlertX, port 20211) or the router's device list."},
        {"id": "net-weak-wifi", "name": "Wi-Fi signal of devices", "kind": "netcheck", "status": "warn", "priority": "low",
         "note": "3 of 34 Wi-Fi devices have a poor signal: Camera: Parking lot (-81 dBm), Laptop-Guest-7 (-84 dBm), Sensor-Garden (-86 dBm). 5 more are weak but usable",
         "hint": "Devices far from the router or behind walls drop out and slow down. Try moving the router more centrally and up high, away from metal and thick walls; add a mesh point or access point near the weak devices; and use a cable for things that never move (TVs, PCs, game consoles)."},
    ]},
]

# ---------------------------------------------------------------- extras
nodes = [
    {"name": "Main router", "mac": mac(1), "ip": "192.168.1.1", "model": "Example AX6000", "role": "router", "online": True,
     "firmware": "3.0.0.4", "backhaul": None, "clients": 14},
    {"name": "Office switch", "mac": mac(2), "ip": "192.168.1.2", "model": "16-port managed switch", "role": "switch",
     "online": True, "firmware": "2.6.1", "backhaul": {"type": "wired", "rate_mbps": 1000, "parent": "Main router"}, "clients": 11},
    {"name": "Mesh point: Hall", "mac": mac(3), "ip": "192.168.1.3", "model": "Example AX6000 node", "role": "node", "online": True,
     "firmware": "3.0.0.4", "backhaul": {"type": "wifi", "band": "5", "rssi": -58, "rate_mbps": 1200, "parent": "Main router"}, "clients": 12},
    {"name": "Mesh point: Classroom wing", "mac": mac(4), "ip": "192.168.1.4", "model": "Example AX6000 node", "role": "node",
     "online": True, "firmware": "3.0.0.4", "backhaul": {"type": "wifi", "band": "5", "rssi": -74, "rate_mbps": 390, "parent": "Mesh point: Hall"}, "clients": 6},
    {"name": "Access point: Kitchen", "mac": mac(5), "ip": "192.168.1.5", "model": "Ceiling AP (Wi-Fi 6E)", "role": "ap", "online": True,
     "firmware": "6.6.55", "backhaul": {"type": "wired", "rate_mbps": 1000, "parent": "Office switch"}, "clients": 7},
]
client_specs = [
    ("Office PC", "wired", None, "Office switch", None, 1000), ("File server (NAS)", "wired", None, "Office switch", None, 1000),
    ("Hall projector", "wired", None, "Office switch", None, 1000), ("Livestream encoder", "wired", None, "Office switch", None, 1000),
    ("Front office printer", "wired", None, "Office switch", None, 100), ("Phone system", "wired", None, "Office switch", None, 100),
    ("Laptop-Pastor", "wifi", "5", "Main router", -48, 1201), ("Laptop-Office-2", "wifi", "6", "Access point: Kitchen", -52, 2402),
    ("Tablet-Welcome-Desk", "wifi", "5", "Main router", -57, 866), ("Smart TV: Lounge", "wifi", "5", "Access point: Kitchen", -55, 433),
    ("Camera: Front door", "wifi", "2.4", "Mesh point: Hall", -63, 72), ("Camera: Parking lot", "wifi", "2.4", "Mesh point: Classroom wing", -81, 26),
    ("Thermostat: Hall", "wifi", "2.4", "Mesh point: Hall", -66, 65), ("Thermostat: Classroom wing", "wifi", "2.4", "Mesh point: Classroom wing", -71, 52),
    ("Copier / scanner", "wifi", "2.4", "Main router", -79, 39), ("Phone-Volunteer-1", "wifi", "5", "Mesh point: Hall", -61, 573),
    ("Phone-Volunteer-2", "wifi", "5", "Mesh point: Hall", -67, 433), ("Phone-Guest-3", "wifi", "5", "Access point: Kitchen", -69, 390),
    ("Laptop-Guest-7", "wifi", "2.4", "Mesh point: Classroom wing", -84, 13), ("Sensor-Garden", "wifi", "2.4", "Mesh point: Classroom wing", -86, 6),
    ("Galaxy-Tab", "wifi", "5", "Mesh point: Hall", -59, 600), ("Smart speaker", "wifi", "2.4", "Main router", -62, 72),
    ("Game console", "wifi", "6", "Access point: Kitchen", -50, 1921), ("Chromebook-Classroom-1", "wifi", "5", "Mesh point: Classroom wing", -72, 270),
    ("Chromebook-Classroom-2", "wifi", "5", "Mesh point: Classroom wing", -76, 173), ("Door access controller", "wired", None, "Office switch", None, 100),
]
# invented traffic (Mbps down, up) so the demo shows devices ranked by usage
DEMO_USE = {"Smart TV: Lounge": (18.4, 0.6), "Livestream encoder": (0.4, 6.2), "Game console": (9.1, 1.3),
            "Laptop-Pastor": (3.8, 0.9), "File server (NAS)": (1.2, 2.7), "Camera: Front door": (0.05, 1.9),
            "Camera: Parking lot": (0.04, 1.6), "Phone-Volunteer-1": (1.1, 0.2), "Galaxy-Tab": (0.7, 0.1),
            "Office PC": (0.6, 0.15), "Chromebook-Classroom-1": (0.3, 0.05), "Smart speaker": (0.12, 0.02),
            "Laptop-Office-2": (0.2, 0.05), "Tablet-Welcome-Desk": (0.08, 0.01), "Phone system": (0.09, 0.09),
            "Thermostat: Hall": (0.0004, 0.0003), "Sensor-Garden": (0.0002, 0.0001)}
clients = []
for n, (name, typ, band, node, rssi, rate) in enumerate(client_specs, start=20):
    ip = f"192.168.1.{n + 20}"
    link = {"type": typ, "band": band, "node": node, "rssi": rssi, "rate_mbps": rate} if typ == "wifi" else \
           {"type": "wired", "node": node, "rate_mbps": rate}
    c = {"mac": mac(100 + n), "ip": ip, "name": name, "online": True, "link": link}
    if " " in name or ":" in name:              # typed by a person ("Office PC"); hostnames like "Galaxy-Tab" stay unconfirmed
        c["named"] = True
    if name in DEMO_USE:                        # live traffic, as a UniFi controller reports it (bits/s)
        d, u = DEMO_USE[name]
        c["usage"] = {"down_bps": round(d * 1e6), "up_bps": round(u * 1e6), "rate_bps": round((d + u) * 1e6),
                      "basis": "live, from UniFi"}
    clients.append(c)
bands = {}
for c in clients:
    k = "wired" if c["link"]["type"] == "wired" else f"{c['link']['band']} GHz"
    bands[k] = bands.get(k, 0) + 1

aps = []
def ap(ssid, bssid_n, band, ch, sig, util=None, sta=None, own=False, sec="WPA2", width=None, extra=False):
    aps.append({"bssid": mac(bssid_n).lower(), "ssid": ssid, "band": band, "channel": ch, "signal_dbm": sig, "secure": sec != "Open",
                "security": sec, "width_mhz": width, "utilisation_pct": util, "stations": sta, "associated": False,
                "own": own, **({"own_hidden": True, "own_extra": True} if extra else {})})
ap("Example-Site", 301, "2.4", 6, -44, 78, 14, True, "WPA3/WPA2", 20)
ap("Example-Site", 302, "5", 36, -47, 22, 9, True, "WPA3/WPA2", 80)
ap("Example-Site", 303, "6", 37, -55, 6, 3, True, "WPA3", 160)
ap("Example-Site", 304, "2.4", 3, -61, 41, 6, True, "WPA3/WPA2", 20)
ap("Example-Site", 305, "5", 149, -66, 18, 5, True, "WPA3/WPA2", 80)
ap("Example-Site-Guest", 306, "2.4", 6, -45, 78, 4, True, "WPA2", 20, extra=True)
ap("Example-Site-Guest", 307, "5", 36, -47, 22, 2, True, "WPA2", 80, extra=True)
ap(None, 308, "5", 36, -48, 22, 0, True, "WPA3", 80, extra=True)
for i, (ssid, band, ch, sig, sec) in enumerate([
        ("Cafe-Next-Door", "2.4", 1, -71, "WPA2"), ("Cafe-Next-Door", "5", 44, -78, "WPA2"),
        ("NETGEAR-Home", "2.4", 6, -69, "WPA2"), ("Printer-Direct-55", "2.4", 6, -73, "WPA2"),
        ("Office-Upstairs", "2.4", 4, -67, "WPA3/WPA2"), ("Office-Upstairs", "5", 52, -74, "WPA3/WPA2"),
        ("FreeWiFi", "2.4", 11, -84, "Open"), ("Old-Router", "2.4", 1, -80, "WPA (old)"), (None, "5", 100, -82, "WPA2")]):
    ap(ssid, 400 + i, band, ch, sig, None, None, False, sec, 20 if band == "2.4" else 80)

def ssid_rows():
    by = {}
    for a in aps:
        if not a["ssid"]:
            continue
        s = by.setdefault(a["ssid"], {"ssid": a["ssid"], "own": a["own"], "access_points": 0, "bands": set(), "channels": set(),
                                      "strongest_dbm": None, "security": set()})
        s["access_points"] += 1; s["bands"].add(a["band"]); s["channels"].add(f"{a['band']}/{a['channel']}"); s["security"].add(a["security"])
        s["strongest_dbm"] = max(a["signal_dbm"], s["strongest_dbm"] or -999)
    out = []
    for s in by.values():
        s["bands"] = sorted(s["bands"], key=float); s["security"] = sorted(s["security"])
        s["channels"] = sorted(s["channels"], key=lambda x: (float(x.split("/")[0]), int(x.split("/")[1])))
        out.append(s)
    return sorted(out, key=lambda s: (not s["own"], -s["strongest_dbm"]))

channels = []
for band, ch in sorted({(a["band"], a["channel"]) for a in aps}, key=lambda x: (float(x[0]), x[1])):
    on = [a for a in aps if a["band"] == band and a["channel"] == ch]
    util = max([a["utilisation_pct"] for a in on if a["utilisation_pct"] is not None], default=None)
    over = sum(1 for a in aps if band == "2.4" and a["band"] == "2.4" and 0 < abs(a["channel"] - ch) <= 4 and a["signal_dbm"] >= -80)
    if util is not None:
        level = "heavy" if util > 70 else "moderate" if util > 40 else "light" if util > 15 else "clear"
    else:
        load = len(on) + 1.5 * over
        level = "heavy" if load >= 8 else "moderate" if load >= 5 else "light" if load >= 2.5 else "clear"
    channels.append({"band": band, "channel": ch, "networks": len(on), "yours": sum(1 for a in on if a["own"]),
                     "strongest_dbm": max(a["signal_dbm"] for a in on), "utilisation_pct": util, "overlapping": over,
                     "congestion": level, "basis": "measured airtime" if util is not None else "estimated from neighbours"})

speed = []
for k in range(42):
    t = T0 - datetime.timedelta(hours=8 * (41 - k))
    ok = k != 30
    down = round(random.gauss(290, 14) if k not in (17, 18) else random.gauss(120, 10), 1)
    speed.append({"at": iso(t).replace("T", " ")[:19], "ok": ok, "ping_ms": round(random.gauss(17, 2), 1) if ok else None,
                  "down_mbps": down if ok else None, "up_mbps": round(random.gauss(23, 1.5), 1) if ok else None,
                  "jitter_ms": round(abs(random.gauss(3, 1)), 1) if ok else None, "loss_pct": 0, "server": "Example ISP - Springfield"})

devices = []
for c in clients:
    devices.append({"mac": c["mac"], "name": c["name"], "ip": c["ip"], "vendor": None, "first_seen": ago(hours=5) if c["name"] == "Galaxy-Tab" else ago(days=40),
                    "last_seen": ago(minutes=1), "online": True, "new": c["name"] == "Galaxy-Tab", "random_mac": c["name"].startswith("Phone"), "ssid": None,
                    **({"named": True} if c.get("named") else {}),
                    **({"hint": "Samsung Galaxy tablet", "clues": ['calls itself "Galaxy-Tab" when it joins', 'DHCP vendor tag "android-dhcp-14"']}
                       if c["name"] == "Galaxy-Tab" else {})})
devices.append({"mac": "02:11:22:33:44:55", "name": None, "ip": "192.168.1.88", "vendor": None, "first_seen": ago(hours=3),
                "last_seen": ago(minutes=2), "online": True, "new": True, "random_mac": True, "ssid": None})
devices.append({"mac": mac(999), "name": "Old laptop", "named": True, "ip": "192.168.1.99", "vendor": None, "first_seen": ago(days=90),
                "last_seen": ago(days=6), "online": False, "new": False, "random_mac": False, "ssid": None})

extras = {
    "netcheck": {"generated": ago(minutes=4), "interface": "eth0", "interface_kind": "wired", "gateway": "192.168.1.1", "note": None},
    "uplink": {"kind": "cellular", "provider": "Example 5G home internet", "model": "Example 5G gateway", "firmware": "1.2.3",
               "uptime_s": 23 * 86400, "registered": True, "cells": [
                   {"tech": "5G", "bands": ["n41"], "bars": 4, "rsrp": -88, "rsrq": -9, "sinr": 21, "rssi": -76, "quality": "good"},
                   {"tech": "4G", "bands": ["B66"], "bars": 3, "rsrp": -104, "rsrq": -12, "sinr": 9, "rssi": -90, "quality": "fair"}]},
    "speed": {"history": speed, "median_down_mbps": 291.0, "median_up_mbps": 23.0, "tests": 41},
    "exposure": {"public_ip": "203.0.113.45", "nat": "One router between this network and the internet",
                 "upnp": {"available": True, "wan_ip": "203.0.113.45", "mappings": [
                     {"proto": "TCP", "external_port": 8443, "internal": "192.168.1.41:443", "description": "Camera NVR"},
                     {"proto": "UDP", "external_port": 3478, "internal": "192.168.1.77:3478", "description": "Game console"}]}},
    "airspace": {"interface": "wlan0", "adapter_bands": ["2.4", "5", "6"], "own_ssids": ["Example-Site"],
                 "notes": ["guest network Example-Site-Guest shares your access points"],
                 "problems": ["2.4 GHz channel 6 is heavily congested (78% of airtime in use)",
                              "2.4 GHz channel 3 partly overlaps 3 neighbouring network(s) on channel 1, 6; channel 11 looks clearest"],
                 "ssids": ssid_rows(), "hidden_count": sum(1 for a in aps if not a["ssid"]), "channels": channels,
                 "networks": sorted(aps, key=lambda a: (not a["own"], float(a["band"]), -a["signal_dbm"]))},
    "devices": devices,
    "topology": {"vendor": "example", "nodes": nodes},
    "clients": clients,
    "client_bands": bands,
}


# ---------------------------------------------------------------- history (History page)
# invented outages: (item name, start minutes ago, duration minutes or None = still down, message)
OUTAGES = [
    ("Internet connection", 3 * 1440 + 130, 14, "Request timed out"),
    ("Internet connection", 12 * 1440 + 600, 47, "Request timed out"),
    ("Websites load", 3 * 1440 + 128, 11, "getaddrinfo EAI_AGAIN"),
    ("Camera: Parking lot", 6 * 1440 + 300, 125, "Host unreachable"),
    ("Front office printer", 1440 + 200, 6, "Request timed out"),
    ("Front office printer", 4 * 1440 + 50, 9, "Request timed out"),
    ("Front office printer", 9 * 1440 + 700, 4, "Request timed out"),
    ("Mesh point: Hall", 20 * 1440 + 90, 32, "Host unreachable"),
]
T0s = int(T0.timestamp())
mins = lambda m: T0 - datetime.timedelta(minutes=m)


def buckets(outs, step, n):
    """up/down check counts per bucket (one check a minute), Kuma-style [start_ts, up, down]."""
    end = T0s // step * step
    rows = []
    for k in range(n - 1, -1, -1):
        a = end - k * step
        b = min(a + step, T0s)
        total = max(0, (b - a) // 60)
        down = 0
        for s, e in outs:
            down += max(0, (min(b, e) - max(a, s)) // 60)
        rows.append([a, total - down, down])
    return rows


history_monitors, history_events = [], []
for g in groups:
    for it in g["items"]:
        if it.get("kind") == "netcheck":
            continue
        outs, olist = [], []
        for name, start, dur, msg in OUTAGES:
            if name != it["name"]:
                continue
            s = T0s - start * 60
            e = s + dur * 60
            outs.append((s, e))
            olist.append([iso(mins(start)), iso(mins(start - dur)), dur * 60])
            history_events.append({"t": iso(mins(start)), "id": it["id"], "name": it["name"], "s": "down", "msg": msg})
            history_events.append({"t": iso(mins(start - dur)), "id": it["id"], "name": it["name"], "s": "up", "msg": "", "dur_s": dur * 60})
        if it["status"] == "down":                       # still down now
            since = 12
            outs.append((T0s - since * 60, T0s))
            olist.append([iso(mins(since)), None, since * 60])
            history_events.append({"t": iso(mins(since)), "id": it["id"], "name": it["name"], "s": "down", "msg": it.get("message") or "Request timed out"})
        history_monitors.append({"id": it["id"], "name": it["name"], "daily": buckets(outs, 86400, 30),
                                 "hourly": buckets(outs, 3600, 48), "outages": olist})
history_events.sort(key=lambda e: e["t"])
extras["time"] = {"at": ago(minutes=4), "box_offset_ms": -2.1, "servers": [
    {"host": "time.cloudflare.com", "ok": True, "offset_ms": -1.8, "rtt_ms": 14.2, "stratum": 3},
    {"host": "time.google.com", "ok": True, "offset_ms": -2.1, "rtt_ms": 18.9, "stratum": 1},
    {"host": "pool.ntp.org", "ok": True, "offset_ms": 0.9, "rtt_ms": 41.5, "stratum": 2},
    {"host": "time.nist.gov", "ok": True, "offset_ms": -2.6, "rtt_ms": 33.0, "stratum": 1},
    {"host": "time.windows.com", "ok": True, "offset_ms": -6.3, "rtt_ms": 47.7, "stratum": 4}]}
extras["box"] = {"hostname": "lanpulse", "ips": [{"ip": "192.168.1.5", "dev": "eth0", "kind": "wired", "main": True},
                                         {"ip": "192.168.1.6", "dev": "wlan0", "kind": "Wi-Fi", "main": False}]}
extras["history"] = {
    "from": ago(days=45), "monitors": history_monitors, "events": history_events,
    "changes": [
        {"t": ago(days=2, hours=5), "id": "net-speed", "name": "Internet speed", "s": "warn", "from": "up",
         "msg": "Two tests in a row well below normal: 61 Mbps vs usual 312"},
        {"t": ago(days=2, hours=1), "id": "net-speed", "name": "Internet speed", "s": "up", "from": "warn", "msg": "Back to normal: 298 Mbps"},
        {"t": ago(days=8), "id": "net-publicip", "name": "Public IP address", "s": "warn", "from": "up",
         "msg": "Public IP changed (the provider assigned a new one)"},
        {"t": ago(days=7, hours=23), "id": "net-publicip", "name": "Public IP address", "s": "up", "from": "warn", "msg": ""},
    ],
}

snap = {"schema": "lanpulse.snapshot/1", "generated": iso(T0), "sources": [{"name": "demo", "ok": True}],
        "site": {"name": "Example Site",
                 "description": "Sample data. Green means working, amber means worth a look, red means not responding."},
        "groups": groups, "extras": extras}
counts = {}
for g in groups:
    for i in g["items"]:
        s = "note" if i["status"] == "warn" and i.get("priority") == "low" else i["status"]
        counts[s] = counts.get(s, 0) + 1
snap["summary"] = {"total": sum(counts.values()), **{s: counts.get(s, 0) for s in
                   ("up", "down", "warn", "note", "pending", "maintenance", "unknown")}}
print(json.dumps(snap, indent=1))
