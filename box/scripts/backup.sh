#!/bin/bash
# Back up the monitoring box's data (Kuma DB, NetAlertX, Speedtest history, settings, .env).
# Writes /opt/monitor/backups/monitor-data-YYYYMMDD-HHMM.tgz and keeps the newest 14.
#
# Off-site copy (optional, set in lanpulse.env):
#   BACKUP_AGE_RECIPIENT  an age public key (age1...). Each backup is encrypted to it before it
#                         leaves the box; only the matching private key (kept OFF the box) can open it.
#   BACKUP_GIT_REPO       a private git repo (git@github.com:you/lanpulse-backups.git) that receives the
#                         newest 7 encrypted backups, as a single commit (no growing history).
#   The box pushes with the deploy key in config/offsite_key (box/scripts/offsite-setup.sh makes it).
# Restore with box/scripts/restore.sh.
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

# ---- off-site copy
env_get() { sed -n "s/^$1=//p" "$APP/lanpulse.env" 2>/dev/null | tail -1 | tr -d '"'"'"; }
RCPT=${BACKUP_AGE_RECIPIENT:-$(env_get BACKUP_AGE_RECIPIENT)}
REPO=${BACKUP_GIT_REPO:-$(env_get BACKUP_GIT_REPO)}
[ -n "$RCPT" ] && [ -n "$REPO" ] || exit 0
command -v age >/dev/null || { echo "off-site: age isn't installed (run setup.sh)"; exit 1; }
KEY=$APP/config/offsite_key
[ -f "$KEY" ] || { echo "off-site: no deploy key at $KEY (run scripts/offsite-setup.sh)"; exit 1; }
W=$APP/backups/offsite; rm -rf "$W"; mkdir -p "$W"; chmod 700 "$W"
for f in $(ls -1t "$OUT"/monitor-data-*.tgz | head -7); do
  age -r "$RCPT" -o "$W/$(basename "$f").age" "$f"
done
cat > "$W/README.md" <<EOF
Encrypted lanpulse backups from $(hostname), newest 7, updated $(date '+%Y-%m-%d %H:%M %Z').
Open with the age private key kept off the box:  age -d -i key.txt -o x.tgz monitor-data-....tgz.age
Restore on a fresh box: run box/setup.sh, then  sudo box/scripts/restore.sh <file>.tgz.age <key.txt>
EOF
cd "$W"
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$APP/config/offsite_known_hosts"
git init -q -b main
git -c user.name="lanpulse $(hostname)" -c user.email="lanpulse@$(hostname).local" add -A
git -c user.name="lanpulse $(hostname)" -c user.email="lanpulse@$(hostname).local" commit -q -m "Backups from $(hostname), $(date '+%Y-%m-%d %H:%M')"
git push -q --force "$REPO" main
cd / && rm -rf "$W"
echo "off-site: pushed $(ls -1t "$OUT"/monitor-data-*.tgz | head -7 | wc -l) encrypted backups"
