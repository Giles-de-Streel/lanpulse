"""lanpulse: listen for DHCP requests and remember what each device says about itself.

When a phone, TV or gadget joins the network it broadcasts a DHCP request that carries its own
hostname (option 12, e.g. "Galaxy-S23", "HS105"), a vendor class (option 60, e.g. "android-dhcp-14",
"MSFT 5.0") and the list of settings it asks for (option 55, which differs between Apple, Android,
Windows and embedded devices). identify.py turns these into "probably ..." labels.

Runs on the box as root (systemd lanpulse-dhcpsniff.service). It only listens: nothing is sent.
Writes $APP/config/dhcp-seen.json: {mac: {"t", "host", "vendor", "params", "ip"}}.
"""
import json, os, socket, struct, sys, time

APP = os.environ.get("APP", "/opt/monitor")
OUT = os.path.join(APP, "config", "dhcp-seen.json")
KEEP = 90 * 86400            # forget devices not heard from in 90 days
MAX = 2000
ETH_P_IP = 0x0800


def load():
    try:
        return json.load(open(OUT))
    except (OSError, ValueError):
        return {}


def save(seen):
    now = time.time()
    seen = {k: v for k, v in seen.items() if now - v.get("t", 0) < KEEP}
    if len(seen) > MAX:
        seen = dict(sorted(seen.items(), key=lambda kv: -kv[1].get("t", 0))[:MAX])
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seen, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, OUT)
    return seen


def text(b):
    return b.decode("utf-8", "replace").strip("\x00 ").strip()[:64] or None


def parse(frame):
    """(mac, info) from an Ethernet frame carrying a DHCP request from a client, or None."""
    if len(frame) < 14 + 20 + 8 + 240 or struct.unpack("!H", frame[12:14])[0] != ETH_P_IP:
        return None
    ip = frame[14:]
    ihl = (ip[0] & 0x0F) * 4
    if ip[9] != 17:                                   # UDP
        return None
    udp = ip[ihl:]
    sport, dport = struct.unpack("!HH", udp[:4])
    if dport != 67 or sport != 68:                    # client -> server only
        return None
    bootp = udp[8:]
    if len(bootp) < 240 or bootp[0] != 1 or bootp[236:240] != b"\x63\x82\x53\x63":
        return None
    mac = ":".join(f"{b:02x}" for b in bootp[28:34])
    opts, i = {}, 240
    while i < len(bootp):
        code = bootp[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(bootp):
            break
        n = bootp[i + 1]
        opts[code] = bootp[i + 2:i + 2 + n]
        i += 2 + n
    if 53 in opts and opts[53][:1] not in (b"\x01", b"\x03"):   # DISCOVER or REQUEST
        return None
    info = {}
    if 12 in opts:
        info["host"] = text(opts[12])
    if 60 in opts:
        info["vendor"] = text(opts[60])
    if 55 in opts:
        info["params"] = ",".join(str(b) for b in opts[55])
    if 50 in opts and len(opts[50]) == 4:             # the address it asks for
        info["ip"] = socket.inet_ntoa(opts[50])
    elif bootp[12:16] != b"\x00\x00\x00\x00":
        info["ip"] = socket.inet_ntoa(bootp[12:16])
    return mac, {k: v for k, v in info.items() if v}


def main():
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_IP))
    seen, dirty, last = load(), False, 0.0
    print(f"dhcpsniff: listening, {len(seen)} devices remembered", flush=True)
    s.settimeout(30)
    while True:
        try:
            frame = s.recv(2048)
        except socket.timeout:
            frame = None
        got = parse(frame) if frame else None
        if got:
            mac, info = got
            old = seen.get(mac, {})
            new = {**old, **info, "t": time.time()}
            if {k: v for k, v in new.items() if k != "t"} != {k: v for k, v in old.items() if k != "t"}:
                print(f"dhcpsniff: {mac} {info}", flush=True)
            seen[mac], dirty = new, True
        if dirty and time.time() - last > 20:        # a burst of requests is written once
            seen, dirty, last = save(seen), False, time.time()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
