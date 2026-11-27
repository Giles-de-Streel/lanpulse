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
import json, os, re, socket, ssl, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

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
    """Add "hint" and "clues" to devices (in place). state caches the slower per-device checks by MAC."""
    online = [d for d in devs if d.get("online") and d.get("ip")]
    if not online:
        return
    mdns, ssdp, dhcp = mdns_scan(), ssdp_scan(), load_dhcp()
    cache = state.setdefault("identify_os", {})
    now = time.time()
    need = [d for d in online if not d.get("name") and not d.get("this_box") and d["ip"] not in mdns
            and d["ip"] not in ssdp and now - (cache.get(d["mac"]) or {}).get("t", 0) > OS_TTL][:OS_PER_RUN]
    for ip, fp in os_scan([d["ip"] for d in need]).items():
        mac = next(d["mac"] for d in need if d["ip"] == ip)
        cache[mac] = {"t": now, "os": fp}
    for d in need:                              # no answer: remember that too, so it isn't re-scanned every run
        cache.setdefault(d["mac"], {"t": now, "os": None})
    # ask unnamed devices directly (certificate, web page, SNMP, NetBIOS), several at once, weekly per device
    state.pop("identify_cert", None)            # replaced by identify_probe
    probes = state.setdefault("identify_probe", {})
    todo = [d for d in online if not d.get("name") and not d.get("this_box") and d["ip"] not in ssdp
            and now - (probes.get(d["mac"]) or {}).get("t", 0) > CERT_TTL][:PROBE_PER_RUN]
    if todo:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for d, res in zip(todo, pool.map(lambda d: probe(d["ip"]), todo)):
                probes[d["mac"]] = {"t": now, **res}
    ieee = {}
    if any(not d.get("vendor") or d["vendor"].startswith("(") for d in online) or enabled_ieee():
        try:
            ieee = oui_table()
        except Exception as e:
            print(f"identify: IEEE registry: {e}", file=sys.stderr)

    for d in online:
        clues, cands = [], []                   # cands: (rank, label); lowest rank = most specific
        m, u = mdns.get(d["ip"]), ssdp.get(d["ip"])
        p = probes.get(d["mac"]) or {}
        info = dhcp.get(d["mac"].lower())
        maker = ieee_maker(ieee, d["mac"]) if ieee else None
        if maker and maker[0] and maker[0].lower() not in ("private", "ieee registration authority") and (
                not d.get("vendor") or d["vendor"].startswith("(") or maker[1] > 6):
            d["vendor"] = maker[0]               # unknown before, or a finer IEEE block names the real maker
        if info:
            c, lab, host = dhcp_clues(info)
            clues += c
            if lab:
                cands.append((7 if lab.startswith(("Android", "Windows PC", "Apple device", "Linux", "small Linux")) and
                              not (host and match_hint(HOST_HINTS, host)) else 1, lab))
            elif host:
                cands.append((9, f'device calling itself "{host}"'))
        if u:
            what = " ".join(x for x in (u.get("maker"), u.get("model")) if x)
            clues.append(f"describes itself as {u.get('name') or what}" + (f" ({what})" if u.get("name") and what else ""))
            cands.append((2, u.get("name") or what))
        if m:
            if m["names"]:
                clues.append("calls itself " + ", ".join(f'"{n}"' for n in m["names"]))
            if m["kinds"]:
                clues.append("announces itself as " + ", ".join(m["kinds"]))
            if m["models"]:
                clues.append("model " + ", ".join(m["models"]))
                am = apple_model(m["models"])
                if am:
                    cands.append((2.5, am + (f' "{m["names"][0]}"' if m["names"] else "")))
            clues += m["clues"]
            if m["kinds"]:
                cands.append((3, m["kinds"][0] + (f' "{m["names"][0]}"' if m["names"] else "")))
            amazon = "amazon" in (d.get("vendor") or "").lower()
            if amazon and "plays Spotify (speaker, TV or app)" in m["clues"]:
                cands.append((3.5, "Amazon Echo (smart speaker)"))       # Fire TVs announce themselves above
            if "Matter smart-home device" in m["clues"]:
                cands.append((3.6, "Matter smart-home device"))
        cert = p.get("cert")
        if cert:
            who = cert.get("org") or cert.get("cn")
            clues.append(f"security certificate from {who}" + (f" ({cert['cn']})" if cert.get("cn") and cert["cn"] != who else ""))
            if plain(CERT_HINTS, cert["text"]):
                cands.append((4, plain(CERT_HINTS, cert["text"])))
        if p.get("snmp"):
            clues.append(f'describes itself (SNMP) as "{p["snmp"]}"')
            cands.append((5, re.split(r"[;,]| - ", p["snmp"])[0].strip()[:60]))
        h = p.get("http") or {}
        if h.get("title"):
            clues.append(f'web page titled "{h["title"]}"' + (f' (server {h["server"]})' if h.get("server") else ""))
            if not GENERIC_TITLES.match(h["title"]):
                cands.append((6, h["title"]))
        elif h.get("server"):
            clues.append(f'web server "{h["server"]}"')
        if p.get("netbios"):
            clues.append(f'computer name "{p["netbios"]}" (Windows networking)')
            cands.append((8, f'Windows PC or NAS "{p["netbios"]}"'))
        fp = (cache.get(d["mac"]) or {}).get("os")
        if fp:
            clues.append(f"network fingerprint: {fp}")
            os_words = plain(OS_HINTS, fp)
            if os_words:
                cands.append((10 if os_words != "Linux-based device" else 12, os_words))
            elif not re.match(r"(linux|microsoft|windows|apple|mac os|freebsd|openbsd|netbsd|android|vxworks|embedded)", fp, re.I):
                cands.append((10.5, fp))         # a named product, e.g. "OSRAM Lightify ZigBee gateway"
        vendor_words = plain(VENDOR_HINTS, d.get("vendor"))
        if d.get("vendor") and d["vendor"] not in ("Private",) and not d["vendor"].startswith("("):
            clues.append(f"network chip made by {d['vendor']}")
        elif d.get("vendor") == "Private":
            clues.append("maker hidden (registered as private)")
        if vendor_words:
            cands.append((11, vendor_words))
        if d.get("random_mac"):
            clues.append("uses a private, changing address (typical of phones, tablets and laptops)")
            cands.append((13, "phone, tablet or laptop"))
        cands = [c for c in cands if c[1]]
        if cands:
            d["hint"] = min(cands, key=lambda c: c[0])[1]
        if clues:
            d["clues"] = clues


PROBE_PER_RUN = 12


def enabled_ieee():
    """Look up every device in the IEEE registry (finer blocks name the real maker); IDENTIFY_IEEE=0 turns it off."""
    return os.environ.get("IDENTIFY_IEEE", "1") not in ("0", "false", "no", "off")


# ---------------------------------------------------------------- more precise clues
# DHCP: what a device said about itself when it joined (dhcpsniff.py writes dhcp-seen.json)
DHCP_FILE = os.path.join(os.environ.get("APP", "/opt/monitor"), "config", "dhcp-seen.json")
HOST_HINTS = [   # its own hostname (regex, case-insensitive) -> plain words; {0} = the matched model part
    (r"^iphone", "iPhone"), (r"^ipad", "iPad"), (r"macbook-?air", "MacBook Air"), (r"macbook-?pro", "MacBook Pro"),
    (r"macbook", "MacBook"), (r"^imac", "iMac"), (r"mac-?mini", "Mac mini"), (r"apple-?tv", "Apple TV"), (r"homepod", "HomePod"),
    (r"^galaxy-?(\w[\w-]*)", "Samsung Galaxy {0}"), (r"^(sm-[a-z]\d+\w*)", "Samsung phone or tablet ({0})"),
    (r"^pixel-?(\w+)", "Google Pixel {0}"), (r"^oneplus", "OnePlus phone"), (r"^moto", "Motorola phone"),
    (r"^android-", "Android phone or tablet"), (r"lgwebostv", "LG webOS TV"), (r"^samsung", "Samsung TV or appliance"),
    (r"^roku", "Roku"), (r"^chromecast", "Chromecast"), (r"^google-?home", "Google Home / Nest speaker"),
    (r"^nest", "Nest device"), (r"^echo-?show|^echoshow", "Amazon Echo Show"), (r"^echo", "Amazon Echo"),
    (r"^fire-?tv|^amazon-[0-9a-f]+", "Amazon Fire TV"), (r"^kindle", "Amazon Kindle"), (r"^ring-", "Ring doorbell or camera"),
    (r"^blink", "Blink camera or Sync Module"), (r"^wyze", "Wyze camera"),
    (r"^(hs1\d\d|kp\d{3}|ep\d\d|kl\d{3})", "TP-Link Kasa plug or bulb ({0})"),
    (r"^tapo", "TP-Link Tapo device"), (r"^esp[_-]", "smart-home gadget (Espressif)"), (r"^shelly", "Shelly smart relay"),
    (r"^tasmota", "Tasmota smart plug"), (r"^wled", "WLED light controller"), (r"^myq", "myQ garage door opener"),
    (r"^ecobee", "ecobee thermostat"), (r"^sonos", "Sonos speaker"), (r"^ting-", "Ting electrical-safety sensor"),
    (r"^(brw|brn)[0-9a-f]{6,}", "Brother printer"), (r"^(hp|npi)[0-9a-f]{6}", "HP printer"), (r"^epson", "Epson printer"),
    (r"^canon", "Canon printer"), (r"^nintendo|^switch", "Nintendo Switch"), (r"^ps[345]", "PlayStation"), (r"^xbox", "Xbox"),
    (r"^(desktop|laptop)-[0-9a-z]{7}$", "Windows PC"), (r"^raspberrypi", "Raspberry Pi"), (r"^tesla", "Tesla car"),
    (r"^(diskstation|ds\d{3})", "Synology storage (NAS)"), (r"^unifi|^u6-|^uap", "UniFi access point"),
]
VENDOR_CLASS_HINTS = [   # DHCP option 60 (regex) -> plain words
    (r"^android-dhcp-(\d+)", "Android {0} phone or tablet"), (r"^msft", "Windows PC"), (r"^ubnt", "Ubiquiti device"),
    (r"^dhcpcd", "Linux device"), (r"^udhcp", "small Linux gadget (camera, plug or router)"), (r"^cisco", "Cisco device"),
    (r"^polycom", "Polycom phone"), (r"^yealink", "Yealink phone"), (r"^aastra", "Aastra phone"), (r"^hp ", "HP printer"),
]
APPLE_MODELS = [   # mDNS / device-info model codes (prefix) -> plain words
    ("MacBookPro", "MacBook Pro"), ("MacBookAir", "MacBook Air"), ("MacBook", "MacBook"), ("Macmini", "Mac mini"),
    ("iMac", "iMac"), ("MacPro", "Mac Pro"), ("Mac1", "Mac (Apple silicon)"), ("AppleTV", "Apple TV"),
    ("AudioAccessory", "HomePod"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("iPod", "iPod"), ("Watch", "Apple Watch"),
]
GENERIC_TITLES = re.compile(r"^(index|home|login|log ?in|sign ?in|welcome|document|untitled|loading|redirect|error|"
                            r"\d{3}\b|not found|forbidden|unauthorized|it works|nginx|apache|web ?server|webui|web ui)", re.I)


def load_dhcp():
    try:
        return json.load(open(DHCP_FILE))
    except (OSError, ValueError):
        return {}


def match_hint(table, text):
    for pat, words in table:
        m = re.search(pat, text or "", re.I)
        if m:
            return words.format(*(g.upper() if re.match(r"^[a-z]", g) else g for g in m.groups())) if m.groups() else words
    return None


def dhcp_clues(info):
    """(clues, label, hostname) from what the device said in its DHCP request."""
    clues, label = [], None
    host, vc, params = info.get("host"), info.get("vendor"), (info.get("params") or "").split(",")
    if host:
        clues.append(f'calls itself "{host}" when it joins')
        label = match_hint(HOST_HINTS, host)
    if vc:
        clues.append(f'DHCP vendor tag "{vc}"')
        label = label or match_hint(VENDOR_CLASS_HINTS, vc)
    if not vc and "108" in params and "114" in params:
        clues.append("asks for network settings the way Apple devices do")
        label = label or "Apple device (iPhone, iPad or Mac)"
    return clues, label, host


def apple_model(models):
    for m in models or []:
        for prefix, words in APPLE_MODELS:
            if m.startswith(prefix):
                return f"{words} ({m})"
    return None


def http_info(ip):
    """{"title", "server"} from the device's own web page on port 80 or 8080, or None."""
    for port in (80, 8080):
        try:
            with socket.create_connection((ip, port), timeout=2) as s:
                s.settimeout(2)
                s.sendall(f"GET / HTTP/1.0\r\nHost: {ip}\r\nUser-Agent: lanpulse\r\n\r\n".encode())
                data = b""
                while len(data) < 20000:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
        except OSError:
            continue
        head, _, body = data.partition(b"\r\n\r\n")
        h = head.decode("latin-1", "replace")
        server = (re.search(r"(?im)^server:\s*(.+)$", h) or [None, None])[1]
        t = re.search(rb"(?is)<title[^>]*>(.*?)</title>", body)
        title = re.sub(r"\s+", " ", t.group(1).decode("utf-8", "replace")).strip()[:70] if t else None
        if title or server:
            return {"title": title or None, "server": server and server.strip()[:60], "port": port}
    return None


def snmp_descr(ip):
    """The device's own description (SNMP sysDescr, community "public"), or None. Printers and switches name their model."""
    req = bytes.fromhex("302902010004067075626c6963a01c0204") + os.urandom(4) + \
        bytes.fromhex("020100020100300e300c06082b060102010101000500")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.5)
            s.sendto(req, (ip, 161))
            data = s.recv(4096)
    except OSError:
        return None
    i = data.find(bytes.fromhex("2b06010201010100"))
    if i < 0 or i + 9 >= len(data) or data[i + 8] != 0x04:
        return None
    j, n = i + 9, data[i + 9]
    if n & 0x80:                                       # long form length
        k = n & 0x7F
        n = int.from_bytes(data[j + 1:j + 1 + k], "big")
        j += k
    txt = re.sub(r"\s+", " ", data[j + 1:j + 1 + n].decode("utf-8", "replace")).strip()
    return txt[:100] or None


def netbios_name(ip):
    """The computer name a Windows PC or NAS gives over NetBIOS, or None."""
    q = os.urandom(2) + bytes.fromhex("00000001000000000000") + b"\x20" + b"CK" + b"A" * 30 + bytes.fromhex("0000210001")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.5)
            s.sendto(q, (ip, 137))
            data = s.recv(2048)
    except OSError:
        return None
    if len(data) < 57:
        return None
    count, off = data[56], 57
    for _ in range(count):
        e = data[off:off + 18]
        if len(e) < 18:
            break
        name, suffix, flags = e[:15].decode("ascii", "replace").strip(), e[15], int.from_bytes(e[16:18], "big")
        if suffix == 0 and not flags & 0x8000 and name:
            return name
        off += 18
    return None


def probe(ip):
    """Everything the box can ask a device directly. A few seconds each; cached for a week per device."""
    return {"cert": cert_scan(ip), "http": http_info(ip), "snmp": snmp_descr(ip), "netbios": netbios_name(ip)}


# full IEEE registry: also the finer MA-M / MA-S blocks, so fewer devices show an unknown maker
OUI_DIR = os.path.join(os.environ.get("APP", "/opt/monitor"), "config", "oui")
OUI_FILES = {"oui.csv": ("oui", 6), "mam.csv": ("oui28", 7), "oui36.csv": ("oui36", 9)}   # file -> (IEEE folder, hex digits)
OUI_TTL = 30 * 86400


def oui_table():
    """{prefix hex: organisation} from the IEEE registries, downloaded monthly; {} when never downloaded."""
    import csv
    table = {}
    try:
        os.makedirs(OUI_DIR, exist_ok=True)
    except OSError:
        return table
    for name, (folder, digits) in OUI_FILES.items():
        path = os.path.join(OUI_DIR, name)
        if not os.path.exists(path) or time.time() - os.path.getmtime(path) > OUI_TTL:
            try:
                req = urllib.request.Request(f"https://standards-oui.ieee.org/{folder}/{name}",
                                             headers={"User-Agent": "Mozilla/5.0 (lanpulse device identification)"})
                data = urllib.request.urlopen(req, timeout=30).read()
                if b"Registry" in data[:200]:
                    with open(path + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(path + ".tmp", path)
            except Exception as e:
                print(f"identify: IEEE {name}: {e}", file=sys.stderr)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for row in csv.reader(f):
                    if len(row) >= 3 and len(row[1]) >= digits:
                        table[row[1][:digits].upper()] = row[2].strip()
        except OSError:
            pass
    return table


def ieee_maker(table, mac):
    h = (mac or "").replace(":", "").replace("-", "").upper()
    for digits in (9, 7, 6):                         # most specific block first
        if h[:digits] in table:
            return table[h[:digits]], digits
    return None
