#!/bin/bash
# Restore a lanpulse backup onto this box (after box/setup.sh has installed everything):
#   sudo /opt/monitor/scripts/restore.sh monitor-data-YYYYMMDD-HHMM.tgz
#   sudo /opt/monitor/scripts/restore.sh monitor-data-YYYYMMDD-HHMM.tgz.age key.txt    (off-site copy)
# Brings back device history and names, Uptime Kuma (monitors, alerts), NetAlertX, Speedtest history,
# dashboard settings (admin password, router login) and the .env files. The program files themselves
# come from the repo (setup.sh), not the backup. The current data is kept in a .before-restore folder.
set -euo pipefail
APP=${APP:-/opt/monitor}
SRC=${1:?usage: restore.sh <backup.tgz | backup.tgz.age key.txt>}
KEYF=${2:-}
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
if [[ "$SRC" == *.age ]]; then
  [ -n "$KEYF" ] || { echo "an encrypted backup needs the age private key file as the second argument"; exit 1; }
  age -d -i "$KEYF" -o "$T/b.tgz" "$SRC"
else
  cp "$SRC" "$T/b.tgz"
fi
mkdir "$T/x" && tar xzf "$T/b.tgz" -C "$T/x"
[ -f "$T/x/kuma-backup.db" ] || { echo "that doesn't look like a lanpulse backup"; exit 1; }
cd "$APP"
echo "== stopping the containers"
docker compose down
KEEP=$APP/.before-restore-$(date +%Y%m%d-%H%M); mkdir -p "$KEEP"
for d in config uptime-kuma netalertx speedtest-tracker .env lanpulse.env; do
  [ -e "$d" ] && mv "$d" "$KEEP/"
done
echo "== restoring (previous data kept in $KEEP)"
for d in config uptime-kuma netalertx speedtest-tracker .env lanpulse.env; do
  [ -e "$T/x/$d" ] && cp -a "$T/x/$d" "$APP/"
done
cp "$T/x/kuma-backup.db" uptime-kuma/kuma.db
rm -f uptime-kuma/kuma.db-wal uptime-kuma/kuma.db-shm
chmod 700 config; chmod 600 .env lanpulse.env 2>/dev/null || true
echo "== starting the containers"
docker compose up -d
echo "Done. The dashboard is back within a minute or two: http://$(hostname).local:8080"
echo "If this box has a different network card, its own entry in NetAlertX will show as a new device once."
