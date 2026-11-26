# lanpulse

**At-a-glance network status for small sites: home, office, church, club or classroom.** Green means working, red means not responding. Anyone can read it, and it alerts your phone when something drops.

lanpulse turns a spare laptop or mini-PC into a monitoring box. It wraps proven open-source tools ([Uptime Kuma](https://github.com/louislam/uptime-kuma), [NetAlertX](https://github.com/jokob-sk/NetAlertX), [Speedtest Tracker](https://github.com/alexjustesen/speedtest-tracker)) with a friendly dashboard. Data stays on your network by default, and you can publish it to the cloud when you need to.

**[Live demo](https://giles-de-streel.github.io/lanpulse/)** (uses sample data)

## How it works

```
 checks            collector                 publishers              dashboard (this repo's web/)
 Uptime Kuma ──►  builds a snapshot  ──►  file (local, default)  ──►  served by the box :8080
 (more later)     lanpulse.snapshot/1      http PUT / Gist / …   ──►  GitHub Pages or any host,
                                                                       reading a snapshot URL
```

- **Snapshot:** one small JSON document describing every check. See [docs/snapshot-schema.md](docs/snapshot-schema.md). Anything that can read or write this format can join in.
- **Data sources** (dashboard side, [`web/providers.js`](web/providers.js)): *local box*, *snapshot URL* (any cloud), *demo*.
- **Publishers** (box side, [`collector/collector.py`](collector/collector.py)): *file* (always on), *http* (WebDAV, Nextcloud, S3/R2 presigned URL, webhook), *gist* (GitHub Gist).
- **Sources** (box side): *uptime-kuma*. The collector is designed for more: NetAlertX devices, speed tests, SNMP, Home Assistant.

Local-only is the default. Nothing leaves the box unless you turn on a publisher. See [docs/cloud-options.md](docs/cloud-options.md).

**Take it anywhere.** Plug the box into another network and it notices: it recognises each network by its router, points device discovery at it, and switches to that network's own device list, router login, baselines and history (a starter list the first time). Find it at `http://lanpulse.local:8080` whatever address it gets. See [docs/moving-networks.md](docs/moving-networks.md).

**Network health, not just device pings.** The box also checks for rogue DHCP servers, double NAT, ports opened to the internet by UPnP, packet loss, DNS, public IP changes, the Wi-Fi airspace and new devices, with no router password needed. Optional router adapters (ASUS/AiMesh, OpenWrt, UniFi) add wired / 2.4 / 5 / 6 GHz, access point and backhaul details. See [docs/network-checks.md](docs/network-checks.md).

## Quick start (on the box)

Tested on Linux Mint 22.3 Xfce on a Dell Latitude 7380 (4 GB RAM). The setup scripts are written for any systemd-based Linux with apt, dnf, zypper or pacman: Debian, Ubuntu and Mint, Fedora and RHEL-like distros, openSUSE, and Arch. They pick the right package names and use whatever firewall (ufw or firewalld) and login manager (LightDM, GDM or SDDM) the distro has. Only Mint has been tested on real hardware so far; the package names are checked on all five families.

```bash
git clone https://github.com/Giles-de-Streel/lanpulse.git
sudo lanpulse/box/setup.sh
```

1. Open `http://<box>:3001` and create the Uptime Kuma admin account. Until you do, anyone on the LAN can claim it.
2. List your devices in a CSV file (see [examples/monitors/example.csv](examples/monitors/example.csv)), then load it:
   `sudo /opt/monitor/scripts/kuma-apply.sh my-site.csv`
3. Name the site: `sudo /opt/monitor/scripts/kuma-statuspage.sh "My Site network status"`
4. Open the dashboard at `http://<box>:8080`.

Optional steps:

| Command | What it does |
|---|---|
| `sudo /opt/monitor/scripts/kuma-ntfy.sh` | Phone push alerts through [ntfy](https://ntfy.sh). Subscribe to the topic it prints |
| `sudo /opt/monitor/scripts/kiosk-setup.sh` | Wall screen: a non-admin user logs in automatically and shows the dashboard full screen |
| `sudo /opt/monitor/scripts/kuma-export.sh > my-site.csv` | Save monitors edited in the web UI back to CSV |
| `sudo /opt/monitor/scripts/kuma-apply.sh --prune other-site.csv` | Replace the current network's device list from a CSV (the admin view can do this too) |

Keep your site CSVs somewhere private (your own repo or the box). They describe your network.

## What `setup.sh` does

- installs Docker from the distro's own packages, keeping an existing Docker (for example docker-ce) if there is one, plus CLI tools: nmap, arp-scan, mtr, iperf3, wavemon, speedtest-cli, snmp, smartctl. A tool the distro doesn't package is skipped
- runs the stack in `/opt/monitor`

  | Service | Port |
  |---|---|
  | Uptime Kuma | 3001 |
  | lanpulse dashboard | 8080 |
  | NetAlertX | 20211 |
  | Speedtest Tracker | 8765 |
  | collector | none |

- disables sleep, suspend and the lid switch. On Debian-style GRUB setups it also makes the boot menu always visible, so an older kernel is one keypress away
- opens the dashboard ports in ufw or firewalld, if either is running
- adds a nightly data backup to `/opt/monitor/backups` (keeps 14)
- installs OS updates nightly (kiosk setup) and reboots at 04:15 only when an update needs it

It is safe to re-run, for example after a `git pull`.

| First visit | Default |
|---|---|
| Speedtest Tracker | `admin@example.com` / `password`. Change it |
| NetAlertX | No login |

## Dashboard anywhere

The dashboard is static files: `web/` runs from the box, from GitHub Pages, or from any web host.

- **On the box:** `http://<box>:8080` reads the live local snapshot.
- **GitHub Pages / elsewhere:** open settings (⚙), choose *Snapshot URL*, and paste an HTTPS URL that a publisher writes to.
- **URL parameters:** `?source=url&url=https://…/snapshot.json`. Add `&kiosk` for a wall screen (bigger text, no settings button).
- **Install as an app:** it's a PWA, so use "Install" or "Add to Home Screen".

## Repo layout

```
web/                 dashboard (static, no build step), also deployed to GitHub Pages
collector/           snapshot builder: sources -> snapshot -> publishers (Python stdlib)
box/                 docker-compose, Caddyfile, setup.sh, lanpulse.env.example
box/scripts/         kuma-apply / export / statuspage / ntfy, kiosk-setup, backup
examples/monitors/   example site CSV
docs/                architecture, snapshot schema, cloud options, network checks
tools/publish-pages.sh  push web/ to the gh-pages branch (the live demo)
```

## Extending

- **New dashboard data source:** add an entry to `PROVIDERS` in `web/providers.js`.
- **New publisher or source:** add a function to `PUBLISHERS` or `SOURCES` in `collector/collector.py`.
- **New check types:** anything Uptime Kuma supports (HTTP keyword, DNS, TCP port, SNMP, MQTT, Docker…). `kuma-apply.sh` handles ping, http, port and dns today.

See [docs/architecture.md](docs/architecture.md). Issues and pull requests are welcome.

## License

MIT. The bundled tools keep their own licenses.
