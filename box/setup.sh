#!/bin/bash
# lanpulse box setup. Tested on Linux Mint 22 (Ubuntu 24.04 base); written to work on any systemd
# Linux with apt, dnf, zypper or pacman (Debian/Ubuntu/Mint, Fedora/RHEL, openSUSE, Arch).
# Run from a checkout of the repo:  sudo box/setup.sh
# Safe to re-run: it skips or refreshes whatever already exists.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

BOX=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$BOX/.." && pwd)
APP=/opt/monitor
ADMIN_USER=${SUDO_USER:-$(logname 2>/dev/null || echo root)}

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
[ -d /run/systemd/system ] || { echo "lanpulse needs a systemd-based Linux"; exit 1; }
. "$BOX/scripts/pkg.sh"

echo "== packages (${PKG_MGR:-unknown package manager} on $(. /etc/os-release; echo "${PRETTY_NAME:-$ID}"))"
pkg_refresh
# Docker: keep whatever is already installed (e.g. docker-ce from Docker's own repo), else the distro's
command -v docker >/dev/null || pkg_install required docker
pkg_install required python3 sqlite3 curl openssl iproute2 iw avahi ping
pkg_install optional nmap arp-scan mtr iperf3 traceroute dig ethtool wavemon \
  speedtest net-tools snmp smartmontools upnpc avahi-browse
[ "$ADMIN_USER" != root ] && usermod -aG docker "$ADMIN_USER"
systemctl enable --now docker >/dev/null
compose_ok || pkg_install optional compose
if ! compose_ok; then     # distro has no compose package: use Docker's own plugin binary
  echo "   fetching the docker compose plugin from github.com/docker/compose"
  mkdir -p /usr/local/lib/docker/cli-plugins
  curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
    "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)"
  chmod 755 /usr/local/lib/docker/cli-plugins/docker-compose
fi
compose_ok || { echo "!! docker compose is missing; install it and re-run"; exit 1; }
PY=$(command -v python3)

echo "== stack -> $APP"
mkdir -p "$APP"/scripts "$APP"/data "$APP"/web "$APP"/collector "$APP"/config/requests "$APP"/config/networks
chmod 700 "$APP"/config "$APP"/config/requests "$APP"/config/networks
install -m 644 "$BOX/docker-compose.yml" "$BOX/Caddyfile" "$APP/"
install -m 755 "$BOX"/scripts/*.sh "$APP/scripts/"
rm -rf "$APP"/web/* && cp -r "$REPO"/web/. "$APP/web/"
install -m 644 "$REPO"/collector/collector.py "$REPO"/collector/netcheck.py "$REPO"/collector/history.py \
  "$REPO"/collector/netswitch.py "$REPO"/collector/identify.py "$APP/collector/"
[ -f "$APP/lanpulse.env" ] || install -m 600 "$BOX/lanpulse.env.example" "$APP/lanpulse.env"
# older installs: turn on the netcheck and router sources (router stays idle until ROUTER_TYPE is set)
sed -i 's/^LANPULSE_SOURCES=uptime-kuma$/LANPULSE_SOURCES=uptime-kuma,netcheck,router/' "$APP/lanpulse.env"

echo "== network checks (netcheck.py every 10 min)"
cat > /etc/systemd/system/lanpulse-netcheck.service <<EOF
[Unit]
Description=lanpulse network health checks
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
Environment=APP=$APP
ExecStart=$PY $APP/collector/netcheck.py
Nice=10
TimeoutStartSec=300
EOF
cat > /etc/systemd/system/lanpulse-netcheck.timer <<'EOF'
[Unit]
Description=Run lanpulse network checks every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF
echo "== moving between networks (netswitch.py: on every connection change, and dashboard requests)"
cat > /etc/systemd/system/lanpulse-network.service <<EOF
[Unit]
Description=lanpulse: notice which network the box is on and adapt to it
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
Environment=APP=$APP
ExecStart=$PY $APP/collector/netswitch.py
TimeoutStartSec=300
EOF
cat > /etc/systemd/system/lanpulse-requests.service <<EOF
[Unit]
Description=lanpulse: carry out changes asked for in the dashboard (device lists, Wi-Fi)
After=docker.service

[Service]
Type=oneshot
Environment=APP=$APP
ExecStart=$PY $APP/collector/netswitch.py --requests
TimeoutStartSec=300
EOF
cat > /etc/systemd/system/lanpulse-requests.path <<EOF
[Unit]
Description=lanpulse: watch for dashboard requests

[Path]
PathExistsGlob=$APP/config/requests/*.req
Unit=lanpulse-requests.service

[Install]
WantedBy=multi-user.target
EOF
if [ -d /etc/NetworkManager/dispatcher.d ]; then
  cat > /etc/NetworkManager/dispatcher.d/90-lanpulse <<'EOF'
#!/bin/sh
# lanpulse: a connection came up or changed, so the box may be on a different network now
case "$2" in
  up|down|dhcp4-change|connectivity-change) systemctl start --no-block lanpulse-network.service ;;
esac
EOF
  chmod 755 /etc/NetworkManager/dispatcher.d/90-lanpulse
fi
systemctl daemon-reload
systemctl enable --now lanpulse-netcheck.timer lanpulse-requests.path >/dev/null 2>&1
systemctl start --no-block lanpulse-netcheck.service
cd "$APP"
touch .env; chmod 600 .env
grep -q '^SPEEDTEST_APP_KEY=' .env || echo "SPEEDTEST_APP_KEY=base64:$(openssl rand -base64 32)" >> .env
TZNAME=$(timedatectl show -p Timezone --value 2>/dev/null || true)
[ -n "$TZNAME" ] || TZNAME=$(readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##')
sed -i '/^TZ=/d' .env; echo "TZ=${TZNAME:-UTC}" >> .env
compose pull -q
compose up -d
compose restart lanpulse-web lanpulse-collector >/dev/null   # pick up new Caddyfile / collector code

LP_HOST=${LANPULSE_HOSTNAME-lanpulse}         # LANPULSE_HOSTNAME= (empty) keeps the current name
echo "== name on the network: http://${LP_HOST:-$(hostname)}.local:8080 works whatever address the box gets"
if [ -n "$LP_HOST" ] && [ "$(hostname)" != "$LP_HOST" ]; then
  OLD=$(hostname)
  hostnamectl set-hostname "$LP_HOST" 2>/dev/null || echo "$LP_HOST" > /etc/hostname
  sed -i -E "s/^(127\.0\.1\.1[[:space:]]+)$OLD([[:space:]].*)?$/\1$LP_HOST/" /etc/hosts   # keep sudo able to resolve the new name
  grep -q "^127\.0\.1\.1" /etc/hosts || echo "127.0.1.1 $LP_HOST" >> /etc/hosts
  echo "renamed $OLD -> $LP_HOST"
fi
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
systemctl restart avahi-daemon >/dev/null 2>&1 || true

echo "== firewall (ufw or firewalld, whichever this distro uses)"
if command -v ufw >/dev/null; then      # rules only take effect if ufw is enabled
  ufw allow ssh >/dev/null
  ufw allow 5353/udp >/dev/null         # mDNS, so <name>.local can be found
  for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
    for p in 3001 8080 20211 8765; do ufw allow from $net to any port $p proto tcp >/dev/null; done
  done
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  for svc in ssh mdns; do firewall-cmd -q --permanent --add-service=$svc || true; done
  for p in 3001 8080 20211 8765; do firewall-cmd -q --permanent --add-port=$p/tcp || true; done
  firewall-cmd -q --reload || true
else
  echo "   no ufw/firewalld running: nothing to open"
fi

echo "== power: never sleep, ignore lid"
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/monitor.conf <<EOF
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
HandleSuspendKey=ignore
HandleHibernateKey=ignore
IdleAction=ignore
EOF
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1

echo "== boot menu always visible (recovery fallback)"
if [ -d /etc/default/grub.d ] && command -v update-grub >/dev/null; then   # Debian/Ubuntu/Mint style GRUB
  cat > /etc/default/grub.d/99_monitor.cfg <<EOF
GRUB_DISTRIBUTOR="$(. /etc/os-release; echo "$NAME")"
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=5
GRUB_DISABLE_OS_PROBER=true
EOF
  (cd / && update-grub >/dev/null 2>&1) || echo "update-grub failed, check manually"
else
  echo "   skipped: not a Debian-style GRUB setup (boot menu left as the distro set it)"
fi

echo "== nightly data backup (03:30, keeps 14)"
rm -f /etc/cron.d/monitor-backup       # older installs used cron; a systemd timer works on every distro
cat > /etc/systemd/system/lanpulse-backup.service <<EOF
[Unit]
Description=lanpulse nightly data backup

[Service]
Type=oneshot
Environment=APP=$APP
ExecStart=$APP/scripts/backup.sh
EOF
cat > /etc/systemd/system/lanpulse-backup.timer <<'EOF'
[Unit]
Description=lanpulse nightly data backup at 03:30

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now lanpulse-backup.timer >/dev/null 2>&1

echo "== reboot after updates that need it (04:15, only when required)"
# the OS installs updates nightly (kiosk-setup.sh) but never restarts; kernel and core-library fixes only
# take effect after a reboot. Debian/Ubuntu/Mint flag that with /run/reboot-required, Fedora/RHEL via
# needs-restarting -r, openSUSE via zypper needs-rebooting. The dashboard is back about 2 minutes later.
cat > /usr/local/sbin/lanpulse-reboot-if-needed <<'EOF'
#!/bin/sh
need=""
[ -f /run/reboot-required ] && need="$(cat /run/reboot-required.pkgs 2>/dev/null | tr '\n' ' ')"
[ -z "$need" ] && [ -f /run/reboot-required ] && need="updates"
if [ -z "$need" ] && command -v needs-restarting >/dev/null; then needs-restarting -r >/dev/null 2>&1 || need="updates"; fi
if [ -z "$need" ] && command -v zypper >/dev/null; then zypper -q needs-rebooting >/dev/null 2>&1; [ $? -eq 102 ] && need="updates"; fi
if [ -n "$need" ]; then
  logger -t lanpulse "rebooting to finish installing: $need"
  systemctl reboot
fi
EOF
chmod 755 /usr/local/sbin/lanpulse-reboot-if-needed
cat > /etc/systemd/system/lanpulse-reboot-check.service <<'EOF'
[Unit]
Description=lanpulse: reboot if installed updates need it

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lanpulse-reboot-if-needed
EOF
cat > /etc/systemd/system/lanpulse-reboot-check.timer <<'EOF'
[Unit]
Description=lanpulse: nightly check at 04:15 whether updates need a reboot

[Timer]
OnCalendar=*-*-* 04:15:00

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now lanpulse-reboot-check.timer >/dev/null 2>&1

echo "== done"
docker ps --format '{{.Names}}  {{.Status}}'
echo
systemctl start lanpulse-network.service || true   # detect the current network now
# every address the box has (cable and Wi-Fi can both be connected); the main one (default route) first
MAIN=$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="src"){print $(i+1); exit}}')
ADDRS=$(ip -4 -o addr show scope global 2>/dev/null | grep -v -E ' (docker|br-|veth)' \
  | awk -v m="$MAIN" '{split($4,a,"/"); k=(a[1]==m)?"main":""; print k" "a[1]" ("$2")"}' | sort -r)
IP=${MAIN:-$(echo "$ADDRS" | awk 'NR==1{print $2}')}
KUMA_USERS=$(python3 -c "import sqlite3;print(sqlite3.connect('file:$APP/uptime-kuma/kuma.db?mode=ro',uri=True).execute('select count(*) from user').fetchone()[0])" 2>/dev/null || echo 0)
echo "Dashboard:  http://$(hostname).local:8080   (works on any network)"
echo "$ADDRS" | while read -r k a i; do [ -n "$a" ] && echo "            http://$a:8080   $i${k:+, $k connection}"; done
echo
if [ "$KUMA_USERS" = "0" ]; then
cat <<EOF
Next:
  1. open http://$IP:3001 and create the Uptime Kuma admin account
  2. open the dashboard, sign in as administrator (you choose the password the first time) and use
     "This network" > "Devices to watch" to add devices. (Or: sudo $APP/scripts/kuma-apply.sh <site>.csv)
  optional wall screen: sudo $APP/scripts/kiosk-setup.sh
EOF
fi
cat <<EOF
Moving the box to another network: just plug it in (or join Wi-Fi from the admin view). It notices,
switches to that network's device list (a starter list the first time) and keeps each network's
history and settings apart. See docs/moving-networks.md.
EOF
