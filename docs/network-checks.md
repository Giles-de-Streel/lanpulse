# Network checks and router adapters

lanpulse looks at the network itself as well as individual devices. Nothing here assumes a particular brand of router or mesh.

## netcheck (built in, needs no passwords)

`collector/netcheck.py` runs on the box every 10 minutes (systemd `lanpulse-netcheck.timer`) and writes `data/netcheck.json`. The collector's `netcheck` source shows the results in a **Network health** group.

| Check | Looks for | Tool |
|---|---|---|
| DHCP servers | a second router or mesh unit in router mode handing out addresses | `nmap broadcast-dhcp-discover` |
| Router layout (NAT) | double NAT (two routers in a row on site) and carrier-grade NAT (100.64/10, 192.0.0/24 464XLAT) | `traceroute`, plus the router's UPnP WAN address |
| Ports open to the internet | ports opened automatically by cameras, consoles, NAS boxes | `upnpc -l` |
| Connection to router / internet | packet loss and jitter | `ping` |
| Name lookups | DNS failures and slow answers | `dig` |
| Public IP | the current address and when it changes | api.ipify.org (the only outside call) |
| Wi-Fi airspace | your access points, bands, channels, congestion, open or impostor networks using your name, new APs | `iw scan` with the box's Wi-Fi adapter |
| Devices | online, new (last 24 h) and unidentified devices | NetAlertX database (read-only) |

Statuses: `up`, `warn` (needs a look: the dashboard stays usable, amber), `down`, `unknown`.
Each check can be turned off with `NETCHECK_<NAME>=0` in `lanpulse.env`, where the tuning settings also live.

For trustworthy results, plug the box into the main router with a cable. A Wi-Fi-connected box can still do everything except it measures its own Wi-Fi link too, so the dashboard says so.

## router adapters (optional, need a router login)

Set `ROUTER_TYPE`, `ROUTER_URL`, `ROUTER_USER` and `ROUTER_PASS` in `lanpulse.env` (root-only file). The adapter adds:

- a **Network equipment** group: each router, mesh node, AP or switch, with its backhaul or uplink (wired, or Wi-Fi band and signal), flagged when the signal is weak;
- `link` on each dashboard tile whose monitor target matches a router client: `Wired`, or `5 GHz · Hall node · -61 dBm · 433 Mbps`;
- detail panels listing every client by connection type, weakest signal first.

| `ROUTER_TYPE` | Covers | Status |
|---|---|---|
| `asus` | ASUS stock firmware, AiMesh, ZenWiFi | experimental, endpoints follow the open-source asusrouter project |
| `openwrt` | OpenWrt / LuCI (ubus JSON-RPC); list several AP URLs | |
| `unifi` | UniFi OS consoles and the classic controller | |

Adding another: write `router_<name>(url, user, pw)` in `collector.py` that returns the normal form documented above `ROUTERS`, and add it to `ROUTERS`. Good candidates: TP-Link Omada, eero (cloud API), MikroTik (REST), Netgear Orbi, pfSense/OPNsense, and SNMP for managed switches (LLDP neighbours, port speed).
