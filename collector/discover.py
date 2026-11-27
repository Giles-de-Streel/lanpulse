"""lanpulse: ask smart-home gadgets, cameras and PCs what they are, using their own local discovery.

identify.py runs this during its pass (as root). For about 8 seconds it:
  - asks, the way each maker's own app does on the local network:
      TP-Link Kasa plugs/bulbs (UDP 9999: model and the name you gave it), Govee lights (UDP 4001: model),
      WiZ bulbs (UDP 38899), Yeelight bulbs (UDP 1982), LIFX bulbs (UDP 56700),
      WS-Discovery (UDP 3702: Windows PCs, network printers and scanners, ONVIF IP cameras with maker/model)
  - listens for Tuya / Smart Life devices, which broadcast "I'm here" every few seconds (UDP 6666/6667)
Replies are read with a packet socket, like dhcpsniff.py, so the box's firewall needs no new openings.
Only status questions are sent: nothing here changes a device's settings.

run() -> {ip: {"clues": [...], "cands": [(rank, label), ...]}}; ranks use identify.py's scale (lower = surer).
"""
import json, os, re, socket, struct, subprocess, threading, time, urllib.parse, uuid

PORTS = {9999, 4001, 4002, 38899, 1982, 56700, 3702, 6666, 6667}
PACKET_OUTGOING = 4


def own_ips():
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=5).stdout
        return set(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out))
    except (OSError, subprocess.SubprocessError):
        return set()


def capture(stop, out):
    """Collect (src_ip, sport, dport, payload) for UDP packets on the ports above until stop is set."""
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800))
    except (OSError, AttributeError):
        return
    s.settimeout(0.3)
    try:
        while not stop.is_set():
            try:
                f, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            if addr[2] == PACKET_OUTGOING or len(f) < 42:
                continue
            ip = f[14:]
            ihl = (ip[0] & 0x0F) * 4
            if ip[9] != 17 or len(ip) < ihl + 8:
                continue
            sport, dport = struct.unpack("!HH", ip[ihl:ihl + 4])
            if sport in PORTS or dport in PORTS:
                out.append((socket.inet_ntoa(ip[12:16]), sport, dport, bytes(ip[ihl + 8:])))
    finally:
        s.close()


# ---- the questions -------------------------------------------------------------------------------
def kasa_crypt(data, decrypt=False):
    """TP-Link's XOR "autokey" obfuscation used by Kasa's local protocol."""
    key, out = 171, bytearray()
    for c in data:
        a = key ^ c
        key = c if decrypt else a
        out.append(a)
    return bytes(out)


def lifx_get_service():
    # 36-byte LIFX header: size, protocol 1024 + addressable + tagged, source, target 0 (all), reply wanted, type 2
    return struct.pack("<HHI8s6sBB8sHH", 36, 0x3400, 0x6C70, b"\0" * 8, b"\0" * 6, 0x01, 0, b"\0" * 8, 2, 0)


def wsd_probe(types=""):
    t = f"<wsd:Types>{types}</wsd:Types>" if types else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><soap:Header>'
        "<wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>"
        "<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>"
        f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID></soap:Header><soap:Body><wsd:Probe>{t}</wsd:Probe>"
        "</soap:Body></soap:Envelope>").encode()


def send_all(sock):
    msgs = [
        (kasa_crypt(b'{"system":{"get_sysinfo":{}}}'), ("255.255.255.255", 9999)),
        (b'{"msg":{"cmd":"scan","data":{"account_topic":"reserve"}}}', ("239.255.255.250", 4001)),
        (b'{"method":"getSystemConfig","params":{}}', ("255.255.255.255", 38899)),
        (b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1982\r\nMAN: "ssdp:discover"\r\nST: wifi_bulb\r\n', ("239.255.255.250", 1982)),
        (lifx_get_service(), ("255.255.255.255", 56700)),
        (wsd_probe(), ("239.255.255.250", 3702)),
        (wsd_probe("dn:NetworkVideoTransmitter"), ("239.255.255.250", 3702)),
    ]
    for data, dest in msgs:
        try:
            sock.sendto(data, dest)
        except OSError:
            pass


# ---- reading the answers -------------------------------------------------------------------------
def _json(b):
    m = re.search(rb"\{.*\}", b, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


def _add(found, ip, clue=None, cand=None):
    d = found.setdefault(ip, {"clues": [], "cands": []})
    if clue and clue not in d["clues"]:
        d["clues"].append(clue)
    if cand and cand[1] and cand not in d["cands"]:
        d["cands"].append(cand)


WSD_TYPES = [   # WS-Discovery device types -> (plain words, rank)
    ("networkvideotransmitter", "IP camera", 2.2), ("printdevicetype", "network printer", 3),
    ("printer", "network printer", 3), ("scandevicetype", "network scanner", 3), ("computer", "Windows PC", 8.5),
]


def interpret(packets):
    found = {}
    for ip, sport, dport, data in packets:
        if sport == 9999:                                            # Kasa
            j = _json(kasa_crypt(data, decrypt=True)) or {}
            si = (j.get("system") or {}).get("get_sysinfo") or {}
            model = (si.get("model") or "").split("(")[0] or None
            kind = si.get("dev_name") or ("bulb" if "light" in str(si.get("mic_type", "")).lower() else "plug or bulb")
            label = f"TP-Link Kasa {kind}" + (f" ({model})" if model else "")
            _add(found, ip, f'answers as a TP-Link Kasa device: {model or "model unknown"}'
                 + (f', named "{si["alias"]}"' if si.get("alias") else ""),
                 (1.5, label + (f' "{si["alias"]}"' if si.get("alias") else "")))
        elif dport == 4002 or sport == 4001:                         # Govee reply
            d = ((_json(data) or {}).get("msg") or {}).get("data") or {}
            sku = d.get("sku")
            if sku or d.get("device"):
                _add(found, d.get("ip") or ip, f"answers as a Govee light{f' (model {sku})' if sku else ''}",
                     (1.5, f"Govee light{f' {sku}' if sku else ''}"))
        elif sport == 38899:                                         # WiZ
            r = (_json(data) or {}).get("result") or {}
            mod = r.get("moduleName")
            if r:
                _add(found, ip, f"answers as a WiZ smart light{f' (module {mod})' if mod else ''}",
                     (1.5, "WiZ smart light" + (" or plug" if mod and "SOCKET" in mod.upper() else "")))
        elif sport == 1982:                                          # Yeelight
            t = data.decode("utf-8", "replace")
            model = (re.search(r"(?im)^model:\s*(\S+)", t) or [None, None])[1]
            name = (re.search(r"(?im)^name:\s*(.+)$", t) or [None, ""])[1].strip()
            _add(found, ip, f"answers as a Yeelight bulb{f' ({model})' if model else ''}" + (f', named "{name}"' if name else ""),
                 (1.5, f"Yeelight bulb{f' ({model})' if model else ''}"))
        elif sport == 56700 and len(data) >= 36 and struct.unpack("<H", data[32:34])[0] == 3:   # LIFX StateService
            _add(found, ip, "answers as a LIFX bulb", (1.5, "LIFX smart bulb"))
        elif sport == 3702 or dport == 3702:                         # WS-Discovery match or "hello"
            t = data.decode("utf-8", "replace")
            if not re.search(r"ProbeMatch|:Hello\b|<Hello", t):
                continue
            types = " ".join(re.findall(r"<(?:[\w-]+:)?Types>([^<]*)<", t)).lower()
            scopes = " ".join(re.findall(r"<(?:[\w-]+:)?Scopes[^>]*>([^<]*)<", t))
            sc = lambda k: urllib.parse.unquote((re.search(rf"onvif://www\.onvif\.org/{k}/(\S+)", scopes) or [None, ""])[1]).strip() or None
            hw, nm = sc("hardware"), sc("name")
            for key, words, rank in WSD_TYPES:
                if key in types:
                    if words == "IP camera":
                        extra = " ".join(x for x in (nm, hw) if x and not (nm and hw and hw.lower() in nm.lower() and x == hw))
                        _add(found, ip, "answers camera discovery (ONVIF)" + (f": {extra}" if extra else ""),
                             (rank, f"IP camera ({extra})" if extra else "IP camera"))
                    else:
                        _add(found, ip, f"answers Windows network discovery as a {words.replace('Windows ', '')}", (rank, words))
                    break
        elif dport in (6666, 6667):                                  # Tuya broadcast
            j = _json(data) or {}
            ver = j.get("version")
            _add(found, j.get("ip") or ip, "broadcasts like a Tuya / Smart Life device" + (f" (protocol {ver})" if ver else ""),
                 (3.4, "Tuya / Smart Life device (smart plug, bulb or similar)"))
    return found


def run(seconds=8):
    pkts, stop = [], threading.Event()
    t = threading.Thread(target=capture, args=(stop, pkts), daemon=True)
    t.start()
    time.sleep(0.3)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        send_all(s)
        time.sleep(2.5)
        send_all(s)                      # UDP can be lost: ask twice
        time.sleep(max(0, seconds - 2.8))
        s.close()
    except OSError:
        time.sleep(seconds)
    stop.set()
    t.join(2)
    mine = own_ips()
    return {ip: v for ip, v in interpret([p for p in pkts if p[0] not in mine]).items() if ip not in mine}


if __name__ == "__main__":
    print(json.dumps(run(), indent=1))
