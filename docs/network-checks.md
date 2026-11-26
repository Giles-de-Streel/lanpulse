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

**Easiest:** open the dashboard on the local network, sign in as administrator (the wall screen has an "Administrator sign-in" link at the bottom; the first visit creates the password) and use **Connect your router** (choose the type, enter the password, Test, Save). It is saved in `/opt/monitor/config/settings.json` (root-only, never served or shown again) and takes priority over `lanpulse.env`. The form only works from private addresses and is hidden on the wall-screen (kiosk) view and on GitHub Pages.

Alternatively set `ROUTER_TYPE`, `ROUTER_URL`, `ROUTER_USER` and `ROUTER_PASS` in `lanpulse.env` (root-only file). The adapter adds:

- a **Network equipment** group: each router, mesh node, AP or switch, with its backhaul or uplink (wired, or Wi-Fi band and signal), flagged when the signal is weak;
- `link` on each dashboard tile whose monitor target matches a router client: `Wired`, or `5 GHz · Hall node · -61 dBm · 433 Mbps`;
- a **Wi-Fi signal of devices** check in Network health: devices below -80 dBm (poor) are flagged with names and advice, -70 to -80 dBm counts as weak but usable;
- detail panels listing every client by connection type, weakest signal first.

| `ROUTER_TYPE` | Covers | Status |
|---|---|---|
| `asus` | ASUS stock firmware, AiMesh, ZenWiFi | experimental, endpoints follow the open-source asusrouter project |
| `openwrt` | OpenWrt / LuCI (ubus JSON-RPC); list several AP URLs | |
| `unifi` | UniFi OS consoles and the classic controller | |
| `tmobile` | T-Mobile Home Internet gateways (Arcadyan, Sagemcom, Nokia), local /TMI/v1 API, admin password from the T-Mobile app | tested on Arcadyan TMO-G5AR 1.00.06: wired vs Wi-Fi and signal; this firmware does not report the band per device. Only the client list is read, never the Wi-Fi password, GPS or SIM details the API also exposes |

Adding another: write `router_<name>(url, user, pw)` in `collector.py` that returns the normal form documented above `ROUTERS`, and add it to `ROUTERS`. Good candidates: TP-Link Omada, eero (cloud API), MikroTik (REST), Netgear Orbi, pfSense/OPNsense, and SNMP for managed switches (LLDP neighbours, port speed).

## Administrator sign-in

Settings changes (such as the router connection) need an administrator sign-in. The first time anyone signs in on a box, the dashboard asks them to create the administrator password (at least 8 characters). Only a salted PBKDF2 hash is stored, in `/opt/monitor/config/settings.json`. Sessions last 12 hours; sign-ins started from the wall screen return to it after 10 minutes without use. After 5 wrong passwords, sign-in pauses for a minute.

Forgot the password? On the box: `sudo rm /opt/monitor/config/settings.json && cd /opt/monitor && sudo docker compose restart lanpulse-collector`. This also removes the saved router login, so you'd reconnect the router afterwards.

## Detail pages

The **Devices** and **Wi-Fi signal** quick-link buttons under the banner open full-page lists. They are
not shown as tiles or counted as issues on the dashboard, because both change constantly; the snapshot
still carries them as low-priority items for other consumers. The pages are
(`?view=devices`, `?view=wifi`). In a normal browser they open in a new tab; on the wall screen
they open in place with a Back button and return by themselves after 5 minutes.

- **Devices**: every device the device finder has seen, joined (by MAC, else IP) with the router's
  client list for connection type, access point and signal. Search, filters (online, new, offline,
  wired, Wi-Fi, weak signal, unnamed) and sortable columns.
- **Wi-Fi signal**: every Wi-Fi client ranked weakest first, rated Excellent (≥ −50 dBm), Good (to −67),
  Fair (to −70), Weak (to −80) or Poor, with a per-access-point summary when there is more than one.
  Band and link-speed columns appear only when the router reports them.

## History page

The **History** button under the banner (`?view=history`) shows the last 24 hours, 7 days or 30 days:

- Summary: number of outages, total downtime, longest outage, and the share of all checks that succeeded.
- Uptime by check, worst first, with a strip of hourly (24 h) or daily squares. Amber squares mean some
  checks failed but recovered on retry before counting as an outage.
- What happened: every outage and recovery (with how long it lasted) from Uptime Kuma, plus changes
  in lanpulse's own network checks. Devices and Wi-Fi signal are left out because they change constantly.
- Speed tests and devices first seen in that period.

The collector reads Uptime Kuma's summary tables (`stat_daily`, `stat_hourly`) and status-change
heartbeats every 10 minutes (`HISTORY_EVERY`, seconds) from a temporary copy of `kuma.db` and its WAL,
and keeps its own change log in `data/history-changes.json` (90 days). Everything goes into the
snapshot as `extras.history`. Kuma's daily buckets start at midnight UTC.

## Phone alerts (admin view)

The admin view has a **Phone alerts** card with links to the free ntfy app (Google Play, F-Droid,
App Store), the topic and server to subscribe to (read from Uptime Kuma's ntfy notification), a Copy
button, and **Send a test alert**. The topic is shown only to a signed-in administrator, because on a
public ntfy server anyone who knows the topic can read the alerts.

### Box restarts

The box can't check anything while it is off or restarting, so a reboot, crash or power cut would
otherwise leave only a silent gap. On start-up the collector compares the machine's boot time
(`/proc/stat`, shared with the host) with the one it saved last time; if it changed, the History
timeline gets a "Monitoring box restarted" entry saying how long monitoring was paused (time since the
last snapshot before the reboot). Gaps over 15 minutes are flagged as a possible power cut or switch-off.
Restarting only the collector (updates) is not logged.
