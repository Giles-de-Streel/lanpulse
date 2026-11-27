"""lanpulse: work out what unnamed devices probably are, from what they say about themselves.

Runs on the box (as root, from netcheck.py). Clues, best first:
  - mDNS / Bonjour announcements (avahi-browse): Fire TV "kitchen", Chromecast "Living room", printers, AirPlay
  - UPnP / SSDP descriptions: friendly name, maker and model (Roku TVs, smart TVs, media servers)
  - the security certificate on its web ports (443/8443): Blink Sync Modules, Ring and others name
    themselves there even though their network chip only says "Amazon" (cached per device for a week)
  - an nmap OS fingerprint for devices that announce nothing (cached per device for a week)
  - the maker of the network chip (from the MAC address), translated into plain words
Each device gets "hint" (a short "probably ..." label) and "clues" (the evidence, for the details view).
Nothing here changes the device; it only listens and asks politely.
"""
import json, os, re, socket, ssl, subprocess, time, urllib.request

MDNS_KINDS = {   # service type -> (what it means, TXT key holding a friendly name, TXT key holding a model)
    "_amzn-wplay._tcp": ("Fire TV", "n", None),
    "_googlecast._tcp": ("Chromecast / Google TV or speaker", "fn", "md"),
    "_androidtvremote2._tcp": ("Android / Google TV", None, None),
    "_airplay._tcp": ("AirPlay device (Apple TV, Mac or speaker)", None, "model"),
    "_raop._tcp": ("AirPlay speaker", None, "am"),
    "_companion-link._tcp": ("Apple device", None, None),
    "_hap._tcp": ("HomeKit accessory", None, "md"),
    "_hap._udp": ("HomeKit accessory", None, "md"),
    "_sonos._tcp": ("Sonos speaker", None, None),
    "_hue._tcp": ("Philips Hue bridge", None, None),
    "_esphomelib._tcp": ("ESPHome smart-home device", None, None),
    "_ipp._tcp": ("printer", None, "ty"), "_ipps._tcp": ("printer", None, "ty"),
    "_printer._tcp": ("printer", None, "ty"), "_pdl-datastream._tcp": ("printer", None, "ty"),
    "_scanner._tcp": ("scanner", None, "ty"),
    "_smb._tcp": ("computer (file sharing)", None, None), "_dosvc._tcp": ("Windows PC", None, None),
    "_workstation._tcp": ("computer", None, None), "_nvstream._tcp": ("NVIDIA Shield / gaming PC", None, None),
}
MDNS_CLUES = {   # service types that are worth mentioning but don't say what the device is
    "_spotify-connect._tcp": "plays Spotify (speaker, TV or app)",
    "_matter._tcp": "Matter smart-home device", "_matterc._udp": "Matter smart-home device",
    "_ssh._tcp": "accepts SSH logins", "_http._tcp": "has a web page", "_device-info._tcp": None,
}
VENDOR_HINTS = [   # maker of the network chip (lower-case prefix) -> plain words
    ("amazon", "Amazon device (Echo, Fire TV, Ring, Blink or Kindle)"), ("immedia", "Blink camera or Sync Module"), ("shenzhen intellirocks", "Govee smart light"),
    ("espressif", "smart-home gadget (Espressif Wi-Fi chip)"), ("tuya", "Tuya smart plug or bulb"),
    ("ring llc", "Ring doorbell or camera"), ("google", "Google / Nest device"), ("nest labs", "Nest device"),
    ("apple", "Apple device"), ("samsung", "Samsung device"), ("roku", "Roku"), ("sonos", "Sonos speaker"),
    ("whisker labs", "Ting electrical-safety sensor"), ("tesla", "Tesla car"), ("ecobee", "ecobee thermostat"),
    ("irobot", "Roomba vacuum"), ("wyze", "Wyze camera"), ("raspberry pi", "Raspberry Pi"),
    ("signify", "Philips Hue light"), ("philips lighting", "Philips Hue light"), ("lg electronics", "LG TV or appliance"),
    ("vizio", "Vizio TV"), ("tcl", "TCL TV"), ("hisense", "Hisense TV"), ("nintendo", "Nintendo console"),
    ("sony interactive", "PlayStation"), ("microsoft", "Microsoft device (PC, Surface or Xbox)"),
    ("arlo", "Arlo camera"), ("chamberlain", "garage door opener"), ("brother", "Brother printer"),
    ("canon", "Canon printer or camera"), ("seiko epson", "Epson printer"), ("hewlett", "HP printer or PC"),
    ("intel", "PC or laptop (Intel Wi-Fi)"), ("liteon", "PC, laptop or TV (Lite-On Wi-Fi module)"),
    ("azurewave", "PC, laptop or gadget (AzureWave Wi-Fi module)"), ("murata", "phone or gadget (Murata Wi-Fi module)"),
    ("texas instruments", "smart-home gadget (TI chip)"), ("universal electronics", "remote control / smart-home hub"),
]
OS_HINTS = [   # nmap OS fingerprint text (lower-case) -> plain words; first match wins
    ("espressif", "smart-home gadget (Espressif Wi-Fi chip)"), ("esp8266", "smart-home gadget (Espressif Wi-Fi chip)"),
    ("android", "Android phone, tablet or TV"), ("apple ios", "iPhone or iPad"), ("iphone", "iPhone or iPad"),
    ("mac os x", "Mac"), ("macos", "Mac"), ("windows", "Windows PC"), ("playstation", "PlayStation"),
    ("lwip", "small embedded gadget"), ("freertos", "small embedded gadget"), ("linux", "Linux-based device"),
]
OS_TTL = 7 * 86400     # re-fingerprint a device at most weekly
OS_PER_RUN = 6         # and only a few per run, to keep each netcheck run short


def _unescape(s):
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), s or "").replace("\\.", ".").replace("\\\\", "\\")


def mdns_scan(seconds=10):
    """{ip: {"kinds": [...], "names": [...], "models": [...], "clues": [...]}} from mDNS announcements."""
    try:
        out = subprocess.run(["avahi-browse", "-artpk"], capture_output=True, text=True, timeout=seconds).stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    except (FileNotFoundError, OSError):
        return {}
    found = {}
    for line in out.splitlines():
        f = line.split(";")
        if len(f) < 10 or f[0] != "=" or f[2] != "IPv4":
            continue
        name, stype, ip = _unescape(f[3]), f[4], f[7]
        txt = dict(re.findall(r'"([^"=]+)=([^"]*)"', ";".join(f[9:])))
        d = found.setdefault(ip, {"kinds": [], "names": [], "models": [], "clues": []})
        if stype in MDNS_KINDS:
            kind, nkey, mkey = MDNS_KINDS[stype]
            if kind not in d["kinds"]:
                d["kinds"].append(kind)
            nm = txt.get(nkey) if nkey else (name.split("@", 1)[-1] if stype == "_raop._tcp" else None)
            if nm and nm not in d["names"]:
                d["names"].append(nm)
            if mkey and txt.get(mkey) and txt[mkey] not in d["models"]:
                d["models"].append(txt[mkey])
        elif stype == "_device-info._tcp" and txt.get("model"):
            if txt["model"] not in d["models"]:
                d["models"].append(txt["model"])
        elif MDNS_CLUES.get(stype) and MDNS_CLUES[stype] not in d["clues"]:
            d["clues"].append(MDNS_CLUES[stype])
    return found


def ssdp_scan(seconds=4):
    """{ip: {"name", "maker", "model"}} from UPnP devices answering an SSDP search."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           "MX: 2\r\nST: ssdp:all\r\n\r\n").encode()
    locs = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.5)
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + seconds
        while time.time() < end:
            try:
                data, (ip, _) = s.recvfrom(4096)
            except socket.timeout:
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                locs.setdefault(ip, set()).add(m.group(1).decode(errors="replace"))
        s.close()
    except OSError:
        return {}
    found = {}
    for ip, urls in locs.items():
        for url in sorted(urls)[:3]:
            if not url.startswith(f"http://{ip}"):
                continue                       # only ask the device about itself
            try:
                x = urllib.request.urlopen(url, timeout=2).read(30000).decode(errors="replace")
            except Exception:
                continue
            get = lambda k: ((re.search(rf"<{k}>([^<]{{1,80}})</{k}>", x) or [None, None])[1] or "").strip() or None
            info = {"name": get("friendlyName"), "maker": get("manufacturer"), "model": get("modelName")}
            if any(info.values()):
                found[ip] = info
                break
    return found


def os_scan(ips):
    """{ip: fingerprint text} from nmap -O (root only; skipped when nmap isn't installed)."""
    if not ips or os.geteuid() != 0:
        return {}
    try:
        out = subprocess.run(["nmap", "-O", "--osscan-guess", "--max-os-tries", "1", "-F", "-T4", "-n", *ips],
                             capture_output=True, text=True, timeout=150).stdout
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return {}
    res, ip = {}, None
    for line in out.splitlines():
        m = re.match(r"Nmap scan report for (\S+)", line)
        if m:
            ip = m.group(1)
            continue
        m = re.match(r"(OS details|Running|Aggressive OS guesses): (.+)", line)
        if ip and m and ip not in res:
            res[ip] = re.sub(r"\s*\(\d+%\)", "", m.group(2)).split(",")[0].strip()
    return res


CERT_HINTS = [   # text in the certificate's subject or issuer (lower-case) -> plain words; first match wins
    (".sm.blink.com", "Blink Sync Module (hub for Blink cameras)"), ("blink", "Blink camera or Sync Module"),
    ("immedia-semi", "Blink camera or Sync Module"), ("ring.com", "Ring doorbell or camera"),
    ("amazon", None), ("unifi", "UniFi network equipment"), ("ubiquiti", "Ubiquiti network equipment"),
    ("synology", "Synology storage (NAS)"), ("qnap", "QNAP storage (NAS)"), ("hikvision", "Hikvision camera or recorder"),
    ("dahua", "Dahua camera or recorder"), ("reolink", "Reolink camera"), ("axis", "Axis camera"),
    ("hewlett", "HP printer"), ("brother", "Brother printer"), ("epson", "Epson printer"), ("canon", "Canon printer"),
    ("sonos", "Sonos speaker"), ("philips hue", "Philips Hue bridge"), ("ecobee", "ecobee thermostat"),
]
CERT_TTL, CERT_PER_RUN = 7 * 86400, 8


def _cert_names(ip, port):
    """(subject, issuer) of the certificate on ip:port, or None. Reads it without trusting or sending anything."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with socket.create_connection((ip, port), timeout=2) as raw, ctx.wrap_socket(raw) as s:
            der = s.getpeercert(binary_form=True)
        out = subprocess.run(["openssl", "x509", "-noout", "-subject", "-issuer", "-nameopt", "oneline"],
                             input=ssl.DER_cert_to_PEM_cert(der).encode(), capture_output=True, timeout=5).stdout.decode()
        f = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return f.get("subject", "").strip(), f.get("issuer", "").strip()
    except Exception:
        return None


def cert_scan(ip):
    """{"cn": ..., "org": ..., "text": ...} from the first certificate found on 443 or 8443, preferring one that names a maker."""
    best = None
    for port in (8443, 443):
        names = _cert_names(ip, port)
        if not names:
            continue
        subj, iss = names
        cn = (re.search(r"CN\s*=\s*([^,/]+)", subj) or [None, None])[1]
        org = (re.search(r"O\s*=\s*([^,/]+)", subj) or re.search(r"O\s*=\s*([^,/]+)", iss) or [None, None])[1]
        found = {"cn": cn and cn.strip(), "org": org and org.strip(), "text": f"{subj} {iss}".lower(), "port": port}
        if plain(CERT_HINTS, found["text"]):
            return found
        best = best or found
    return best


def plain(table, text):
    t = (text or "").lower()
    return next((words for key, words in table if key in t), None)


def identify(devs, state):
    """Add "hint" and "clues" to devices (in place). state["identify_os"] caches fingerprints by MAC."""
    online = [d for d in devs if d.get("online") and d.get("ip")]
    if not online:
        return
    mdns, ssdp = mdns_scan(), ssdp_scan()
    cache = state.setdefault("identify_os", {})
    now = time.time()
    need = [d for d in online if not d.get("name") and not d.get("this_box") and d["ip"] not in mdns
            and d["ip"] not in ssdp and now - (cache.get(d["mac"]) or {}).get("t", 0) > OS_TTL][:OS_PER_RUN]
    for ip, fp in os_scan([d["ip"] for d in need]).items():
        mac = next(d["mac"] for d in need if d["ip"] == ip)
        cache[mac] = {"t": now, "os": fp}
    for d in need:                              # no answer: remember that too, so it isn't re-scanned every run
        cache.setdefault(d["mac"], {"t": now, "os": None})
    certs = state.setdefault("identify_cert", {})
    for d in [d for d in online if not d.get("name") and not d.get("this_box") and d["ip"] not in ssdp
              and now - (certs.get(d["mac"]) or {}).get("t", 0) > CERT_TTL][:CERT_PER_RUN]:
        certs[d["mac"]] = {"t": now, "cert": cert_scan(d["ip"])}
    for d in online:
        clues, label = [], None
        m, u = mdns.get(d["ip"]), ssdp.get(d["ip"])
        if u:
            what = " ".join(x for x in (u.get("maker"), u.get("model")) if x)
            clues.append(f"describes itself as {u.get('name') or what}" + (f" ({what})" if u.get("name") and what else ""))
            label = u.get("name") or what
        if m:
            if m["names"]:
                clues.append("calls itself " + ", ".join(f'"{n}"' for n in m["names"]))
            if m["kinds"]:
                clues.append("announces itself as " + ", ".join(m["kinds"]))
            if m["models"]:
                clues.append("model " + ", ".join(m["models"]))
            clues += m["clues"]
            if not label and m["kinds"]:
                label = m["kinds"][0] + (f' "{m["names"][0]}"' if m["names"] else "")
            amazon = "amazon" in (d.get("vendor") or "").lower()
            if not label and amazon and "plays Spotify (speaker, TV or app)" in m["clues"]:
                label = "Amazon Echo (smart speaker)"       # Fire TVs announce themselves above
            if not label and "Matter smart-home device" in m["clues"]:
                label = "Matter smart-home device"
        cert = (certs.get(d["mac"]) or {}).get("cert")
        if cert:
            who = cert.get("org") or cert.get("cn")
            clues.append(f"security certificate from {who}" + (f" ({cert['cn']})" if cert.get("cn") and cert["cn"] != who else ""))
            label = label or plain(CERT_HINTS, cert["text"])
        fp = (cache.get(d["mac"]) or {}).get("os")
        if fp:
            clues.append(f"network fingerprint: {fp}")
        vendor_words = plain(VENDOR_HINTS, d.get("vendor"))
        if d.get("vendor") and d["vendor"] not in ("Private",) and not d["vendor"].startswith("("):
            clues.append(f"network chip made by {d['vendor']}")
        elif d.get("vendor") == "Private":
            clues.append("maker hidden (registered as private)")
        if d.get("random_mac"):
            clues.append("uses a private, changing address (typical of phones, tablets and laptops)")
        if not label:
            os_words = plain(OS_HINTS, fp)
            if os_words and os_words != "Linux-based device":
                label = os_words
            else:
                label = vendor_words or os_words or ("phone, tablet or laptop" if d.get("random_mac") else None)
        if label:
            d["hint"] = label
        if clues:
            d["clues"] = clues
