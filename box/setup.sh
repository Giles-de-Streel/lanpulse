#!/bin/bash
# lanpulse box setup (Linux Mint 22 / Ubuntu 24.04).
# Run from a checkout of the repo:  sudo box/setup.sh
# Safe to re-run: it skips or refreshes whatever already exists.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

BOX=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$BOX/.." && pwd)
APP=/opt/monitor
ADMIN_USER=${SUDO_USER:-$(logname 2>/dev/null || echo root)}

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }

echo "== packages"
apt-get update -q >/dev/null
apt-get install -y -q docker.io docker-compose-v2 sqlite3 \
  nmap arp-scan mtr-tiny iperf3 traceroute dnsutils ethtool wavemon \
  speedtest-cli net-tools curl snmp smartmontools iw miniupnpc python3 avahi-daemon >/dev/null
[ "$ADMIN_USER" != root ] && usermod -aG docker "$ADMIN_USER"
systemctl enable --now docker >/dev/null

echo "== stack -> $APP"
mkdir -p "$APP"/scripts "$APP"/data "$APP"/web "$APP"/collector "$APP"/config/requests "$APP"/config/networks
chmod 700 "$APP"/config "$APP"/config/requests "$APP"/config/networks
install -m 644 "$BOX/docker-compose.yml" "$BOX/Caddyfile" "$APP/"
install -m 755 "$BOX"/scripts/*.sh "$APP/scripts/"
rm -rf "$APP"/web/* && cp -r "$REPO"/web/. "$APP/web/"
install -m 644 "$REPO"/collector/collector.py "$REPO"/collector/netcheck.py "$REPO"/collector/history.py \
  "$REPO"/collector/netswitch.py "$APP/collector/"
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
ExecStart=/usr/bin/python3 $APP/collector/netcheck.py
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
ExecStart=/usr/bin/python3 $APP/collector/netswitch.py
TimeoutStartSec=300
EOF
cat > /etc/systemd/system/lanpulse-requests.service <<EOF
[Unit]
Description=lanpulse: carry out changes asked for in the dashboard (device lists, Wi-Fi)
After=docker.service

[Service]
Type=oneshot
Environment=APP=$APP
ExecStart=/usr/bin/python3 $APP/collector/netswitch.py --requests
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
sed -i '/^TZ=/d' .env; echo "TZ=$(timedatectl show -p Timezone --value)" >> .env
docker compose pull -q
docker compose up -d
docker compose restart lanpulse-web lanpulse-collector >/dev/null   # pick up new Caddyfile / collector code

LP_HOST=${LANPULSE_HOSTNAME-lanpulse}         # LANPULSE_HOSTNAME= (empty) keeps the current name
echo "== name on the network: http://${LP_HOST:-$(hostname)}.local:8080 works whatever address the box gets"
if [ -n "$LP_HOST" ] && [ "$(hostname)" != "$LP_HOST" ]; then
  OLD=$(hostname)
  hostnamectl set-hostname "$LP_HOST"
  sed -i -E "s/^(127\.0\.1\.1[[:space:]]+)$OLD([[:space:]].*)?$/\1$LP_HOST/" /etc/hosts   # keep sudo able to resolve the new name
  grep -q "^127\.0\.1\.1" /etc/hosts || echo "127.0.1.1 $LP_HOST" >> /etc/hosts
  echo "renamed $OLD -> $LP_HOST"
fi
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
systemctl restart avahi-daemon >/dev/null 2>&1 || true

echo "== firewall (only matters if ufw is enabled)"
ufw allow ssh >/dev/null
ufw allow 5353/udp >/dev/null           # mDNS, so <name>.local can be found
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
  for p in 3001 8080 20211 8765; do ufw allow from $net to any port $p proto tcp >/dev/null; done
done

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
cat > /etc/default/grub.d/99_monitor.cfg <<EOF
GRUB_DISTRIBUTOR="$(. /etc/os-release; echo "$NAME")"
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=5
GRUB_DISABLE_OS_PROBER=true
EOF
(cd / && update-grub >/dev/null 2>&1) || echo "update-grub failed, check manually"

echo "== nightly data backup (03:30, keeps 14)"
echo "30 3 * * * root $APP/scripts/backup.sh >/dev/null 2>&1" > /etc/cron.d/monitor-backup

echo "== done"
docker ps --format '{{.Names}}  {{.Status}}'
echo
IP=$(ip -4 -o addr show scope global 2>/dev/null | grep -v -E ' (docker|br-|veth)' | awk '{print $4}' | cut -d/ -f1 | head -1)
systemctl start lanpulse-network.service || true   # detect the current network now
cat <<EOF
Next:
  1. open http://$IP:3001 and create the Uptime Kuma admin account
  2. sudo $APP/scripts/kuma-apply.sh <site>.csv            # load monitors
  3. sudo $APP/scripts/kuma-statuspage.sh "Site name"      # feeds the lanpulse dashboard
  4. dashboard: http://$IP:8080  (or http://$(hostname).local:8080 from any network)
     optional wall screen: sudo $APP/scripts/kiosk-setup.sh
Moving the box to another network: just plug it in (or join Wi-Fi from the admin view). It notices,
switches to that network's device list (a starter list the first time) and keeps each network's
history and settings apart. See docs/moving-networks.md.
EOF
