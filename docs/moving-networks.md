# Moving the box between networks

The box is meant to travel: set it up at home, then take it to an office, a church or a friend's house.
Plug it in (or join Wi-Fi from the admin view) and it adapts by itself. Nothing needs typing at a console.

## How it decides which network it is on

The box never assumes it's on a network it knows, including the one it was set up on.
Whenever the connection changes (and after a restart), `collector/netswitch.py` gathers evidence:

| Evidence | Points | Notes |
|---|---|---|
| Router's UPnP ID (UDN) | +45 | hardware; survives a router MAC change |
| Router's MAC address | +40 (−10 if different) | hardware |
| Connected to one of its Wi-Fi access points (BSSID) | +35 | hardware |
| 3+ of its access points heard nearby (signal ≥ −75 dBm) | +30 (1–2: +10) | hardware; works even on a cable |
| Several of its devices answer (arp-scan) | +35 for 5+, or 3+ that are 30% of its list (2: +12) | randomised phone MACs ignored; one travelling phone isn't evidence |
| Same Wi-Fi name | +20 (−25 if different) | |
| Same address range and router address | +10 (−15 if different) | |
| Same DHCP domain | +15 | |
| Same public IP | +15 | skipped when `NETCHECK_PUBLICIP=0` |

- **Recognised** = 60+ points **and** at least one hardware item. A Wi-Fi name or address range alone
  never counts ("NETGEAR" on 192.168.1.x is everywhere). A different Wi-Fi name with no matching
  hardware rules a network out.
- **Maybe** (30–59): the box treats it as a **new** network (starter list, no router login from the
  other network) and the admin view asks *"Is this <name>?"*. It keeps looking every 15 minutes and
  settles the question by itself if the evidence becomes strong.
- Otherwise it's a **new** network.

Every network keeps a fingerprint of all of this (with first/last seen times) that grows while the box
is there, so recognition gets surer over time. The admin view shows why the box decided what it did.

On a box set up before this existed, the old settings (monitors, router login, learned Wi-Fi access
points, NetAlertX's device list including the router's MAC) become a known network called
"Previous setup" (or the status page title). It's used only if the evidence matches, like any other.

## What changes with the network

1. **Device discovery** (NetAlertX `SCAN_SUBNETS`) points at every subnet the box is on now (big networks capped to a /22).
2. **Device list:** each network has its own list in Uptime Kuma (monitors tagged
   `lanpulse-network=<id>`). Other networks' monitors are **paused, not deleted**, so their history is kept.
   A new network gets a starter list (router, internet, name lookups).
3. **Dashboard title** = the network's name.
4. **Baselines** (public IP, Wi-Fi access points, normal speed, which devices are new) are per network.
5. **Router logins** are per network.
6. **History** notes the move.

## Correcting it

In the admin view's "This network" card:
- **Yes / No** on *"Is this <name>?"*. Yes merges the two (devices, router login, history come back);
  No remembers that they are different.
- **Not this network?** on a recognised network: splits it off as a new network and takes back
  what was learned during this visit.
- **Forget this network and start over:** deletes its device list and fingerprint. The next look (connection
  change, restart, or within 10 minutes) treats the network as never seen. Uptime Kuma's database is
  backed up before every change (`uptime-kuma/kuma.db.bak-*`, newest 10 kept).

## Finding the box

The box gets whatever address the new network's DHCP hands out. `setup.sh` names it `lanpulse` and
runs mDNS (Avahi), so from a phone or PC on the same network open:

    http://lanpulse.local:8080

To keep the existing hostname, run setup with `LANPULSE_HOSTNAME= sudo -E box/setup.sh`, or pick
another name with `LANPULSE_HOSTNAME=mybox`.

## Admin view: "This network" card

- **Rename** the network (this becomes the dashboard title there).
- **Devices to watch:** add devices NetAlertX found, copy another network's list, paste a CSV
  (`group,name,type,target,description`), or edit lines by hand. Saving restarts Uptime Kuma (about half a minute).
- **Join Wi-Fi** for when there's no cable. The password goes straight to NetworkManager on the box.
- **Other networks this box knows:** when it was last there, and how many devices are on its list.
  **Forget** removes a network and its device list.

How it works: the dashboard calls the collector's settings API (administrator sign-in required). The
collector writes a request file to `config/requests/`. A systemd path unit (`lanpulse-requests.path`)
runs `netswitch.py --requests` on the host, which makes the change and writes the answer back. The
collector container itself can't use NetworkManager or restart Uptime Kuma.

## Files

| File | What |
|---|---|
| `/opt/monitor/config/networks.json` | known networks, names, time periods (root only) |
| `/opt/monitor/config/networks/<id>/netcheck-state.json` | that network's learned baselines |
| `/opt/monitor/config/settings.json` | `routers.<id>`: per-network router logins (write-only from the dashboard) |
| `/opt/monitor/data/network.json` | the current network, without MAC addresses or passwords (served to the dashboard) |

`sudo /opt/monitor/collector/netswitch.py --print` shows which network the box thinks it is on.
`journalctl -u lanpulse-network -u lanpulse-requests` shows what it did.

## Command-line tools still work

`kuma-apply.sh` and `kuma-export.sh` work on the current network's list only: applying with `--prune`
no longer deletes other networks' devices. Monitors you add in Uptime Kuma's own web page belong to
the network the box is on when you add them.

## Limits

- A replaced router is usually still recognised from the access points, devices and Wi-Fi name. If not, answer "Yes" to the question, or copy the old list across.
- Pausing a monitor by hand in Uptime Kuma is undone when the box leaves the network and comes back.
- Speed tests from Speedtest Tracker are shared, but "normal" is worked out only from tests run while
  the box was on the current network.
