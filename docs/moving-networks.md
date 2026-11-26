# Moving the box between networks

The box is meant to travel: set it up at home, then take it to an office, a church or a friend's house.
Plug it in (or join Wi-Fi from the admin view) and it adapts by itself. Nothing needs typing at a console.

## What happens when it lands on a network

`collector/netswitch.py` runs whenever NetworkManager reports a connection change, and at the start of
every network check (every 10 minutes) as a safety net. It:

1. **Recognises the network** by its router's MAC address, so two sites that both use 192.168.1.x are
   still told apart. A cable wins over Wi-Fi when both are connected.
2. **Points device discovery at it.** NetAlertX's `SCAN_SUBNETS` is rewritten for every subnet the box
   is on, on the right adapter (big networks are capped to the /22 around the box).
3. **Switches the device list.** Each network has its own list in Uptime Kuma (monitors tagged
   `lanpulse-network=<id>`). The other networks' monitors are **paused, not deleted**, so their history
   is there when the box comes back. A network seen for the first time gets a starter list (router,
   internet, name lookups), and the admin view asks you to choose the real devices.
4. **Uses that network's name as the dashboard title.**
5. **Keeps learned baselines per network:** public IP, Wi-Fi access points, gateway restarts, what
   "normal" internet speed is, and which devices count as new. A new site starts in "learning" mode
   instead of raising false alarms.
6. **Keeps router logins per network.** A login saved at one site is never tried against another
   site's router.
7. **Notes the move in History** ("moved to another network").

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

- A router replaced at a site looks like a new network. Copy the old list across with "Copy the list
  from another network", then forget the old entry.
- Pausing a monitor by hand in Uptime Kuma is undone when the box leaves the network and comes back.
- Speed tests from Speedtest Tracker are shared, but "normal" is worked out only from tests run while
  the box was on the current network.
