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
| Wi-Fi airspace | each visible network name (bands, channels, security), channel congestion per band (measured airtime when access points report it, otherwise estimated from neighbours, with 2.4 GHz partial overlap), only flags channel problems when neighbours are actually heard, old/open security, look-alike and new access points, 6 GHz when the adapter supports it | `iw scan` with the box's Wi-Fi adapter |
| Internet speed | latest Speedtest Tracker result vs this site's 14-day normal; warns on two slow tests in a row or failing tests | Speedtest Tracker database (read-only) |
| Devices | online, new (last 24 h) and unidentified devices | NetAlertX database (read-only) |
| Internet uplink | cellular signal (band, bars, RSRP, SINR) and gateway restarts, for 5G/LTE home internet gateways that publish it without a login (T-Mobile so far; add probes to `UPLINKS`) | gateway local API |

Statuses: `up`, `warn` (needs a look: the dashboard stays usable, amber), `down`, `unknown`.
Each check can be turned off with `NETCHECK_<NAME>=0` in `lanpulse.env`, where the tuning settings also live.

For trustworthy results, plug the box into the main router with a cable. A Wi-Fi-connected box can still do everything except it measures its own Wi-Fi link too, so the dashboard says so.

## router adapters (optional, need a router login)

**Easiest:** open the dashboard on the local network and use **Connect your router** (choose the type, enter the password, Test, Save). It is saved in `/opt/monitor/config/settings.json` (root-only, never served or shown again) and takes priority over `lanpulse.env`. Set `LANPULSE_ADMIN_PIN` in `lanpulse.env` to require a PIN for changes. The form only works from private addresses and is hidden on the wall-screen (kiosk) view and on GitHub Pages.

Alternatively set `ROUTER_TYPE`, `ROUTER_URL`, `ROUTER_USER` and `ROUTER_PASS` in `lanpulse.env` (root-only file). The adapter adds:

- a **Network equipment** group: each router, mesh node, AP or switch, with its backhaul or uplink (wired, or Wi-Fi band and signal), flagged when the signal is weak;
- `link` on each dashboard tile whose monitor target matches a router client: `Wired`, or `5 GHz · Hall node · -61 dBm · 433 Mbps`;
- detail panels listing every client by connection type, weakest signal first.

| `ROUTER_TYPE` | Covers | Status |
|---|---|---|
| `asus` | ASUS stock firmware, AiMesh, ZenWiFi | experimental, endpoints follow the open-source asusrouter project |
| `openwrt` | OpenWrt / LuCI (ubus JSON-RPC); list several AP URLs | |
| `unifi` | UniFi OS consoles and the classic controller | |
| `tmobile` | T-Mobile Home Internet gateways (Arcadyan, Sagemcom, Nokia), local /TMI/v1 API, admin password from the T-Mobile app | |

Adding another: write `router_<name>(url, user, pw)` in `collector.py` that returns the normal form documented above `ROUTERS`, and add it to `ROUTERS`. Good candidates: TP-Link Omada, eero (cloud API), MikroTik (REST), Netgear Orbi, pfSense/OPNsense, and SNMP for managed switches (LLDP neighbours, port speed).
