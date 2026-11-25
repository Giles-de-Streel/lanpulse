#!/bin/bash
# Back up the monitoring box's data (Kuma DB, NetAlertX, Speedtest history, .env).
# Writes /opt/monitor/backups/monitor-data-YYYYMMDD-HHMM.tgz and keeps the newest 14.
# Copy these off the box occasionally; they contain password hashes, so keep them out of git.
set -euo pipefail
APP=${APP:-/opt/monitor}
OUT=$APP/backups; mkdir -p "$OUT"; chmod 700 "$OUT"
F=$OUT/monitor-data-$(date +%Y%m%d-%H%M).tgz
cd "$APP"
# SQLite-safe copy of the Kuma DB while it runs
sqlite3 uptime-kuma/kuma.db ".backup '/tmp/kuma-backup.db'"
tar czf "$F" --exclude='uptime-kuma/kuma.db*' .env lanpulse.env docker-compose.yml config uptime-kuma netalertx speedtest-tracker \
  -C /tmp kuma-backup.db
rm -f /tmp/kuma-backup.db
chmod 600 "$F"
ls -1t "$OUT"/monitor-data-*.tgz | tail -n +15 | xargs -r rm -f
echo "$F"
