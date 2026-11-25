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
  speedtest-cli net-tools curl snmp smartmontools >/dev/null
[ "$ADMIN_USER" != root ] && usermod -aG docker "$ADMIN_USER"
systemctl enable --now docker >/dev/null

echo "== stack -> $APP"
mkdir -p "$APP"/scripts "$APP"/data "$APP"/web "$APP"/collector
install -m 644 "$BOX/docker-compose.yml" "$BOX/Caddyfile" "$APP/"
install -m 755 "$BOX"/scripts/*.sh "$APP/scripts/"
rm -rf "$APP"/web/* && cp -r "$REPO"/web/. "$APP/web/"
install -m 644 "$REPO"/collector/collector.py "$APP/collector/"
[ -f "$APP/lanpulse.env" ] || install -m 600 "$BOX/lanpulse.env.example" "$APP/lanpulse.env"
cd "$APP"
touch .env; chmod 600 .env
grep -q '^SPEEDTEST_APP_KEY=' .env || echo "SPEEDTEST_APP_KEY=base64:$(openssl rand -base64 32)" >> .env
sed -i '/^TZ=/d' .env; echo "TZ=$(timedatectl show -p Timezone --value)" >> .env
docker compose pull -q
docker compose up -d
docker compose restart lanpulse-web lanpulse-collector >/dev/null   # pick up new Caddyfile / collector code

echo "== firewall (only matters if ufw is enabled)"
ufw allow ssh >/dev/null
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
IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
cat <<EOF
Next:
  1. open http://$IP:3001 and create the Uptime Kuma admin account
  2. sudo $APP/scripts/kuma-apply.sh <site>.csv            # load monitors
  3. sudo $APP/scripts/kuma-statuspage.sh "Site name"      # feeds the lanpulse dashboard
  4. dashboard: http://$IP:8080      optional wall screen: sudo $APP/scripts/kiosk-setup.sh
EOF
