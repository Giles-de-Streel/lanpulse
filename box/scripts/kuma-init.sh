#!/bin/bash
# lanpulse: finish Uptime Kuma's first-run setup so nobody has to open it.
#   sudo kuma-init.sh          (setup.sh runs it; does nothing once Kuma has an admin account)
# A fresh Kuma 2.x waits on two screens: "which database" and "create the admin account". Until both
# are done it has no database, so lanpulse can't add its checks. This answers them the way Kuma would:
#   1. db-config.json {"type":"sqlite"} in Kuma's data folder (Kuma skips its database screen when it exists)
#   2. an "admin" account with a random password, hashed with Kuma's own bcrypt library, then a restart
#      (Kuma decides at start-up whether it still needs setting up)
# The password is saved in /opt/monitor/.env (root only) as KUMA_ADMIN_PASSWORD, for anyone who wants to
# log in to Kuma itself. Day-to-day device changes happen in the lanpulse admin view instead.
set -euo pipefail
APP=${APP:-/opt/monitor}
D=$APP/uptime-kuma
DB=$D/kuma.db
ENVF=$APP/.env
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }

users() { sqlite3 "file:$DB?mode=ro" "select count(*) from user" 2>/dev/null || echo none; }
case "$(users)" in
  none|0) ;;
  *) echo "   Uptime Kuma already has an admin account"; exit 0 ;;
esac

# 1. database type
if [ ! -s "$D/db-config.json" ]; then
  echo '{"type":"sqlite"}' > "$D/db-config.json"
  docker restart uptime-kuma >/dev/null
fi
for i in $(seq 1 60); do [ "$(users)" = 0 ] && break; sleep 3; done
[ "$(users)" = 0 ] || { echo "!! Uptime Kuma didn't create its database: open it on port 3001 to finish its setup"; exit 1; }

# 2. admin account (the password goes in on stdin, never on a command line)
PW=$(openssl rand -base64 30 | tr -dc 'A-Za-z0-9' | cut -c1-20)
HASH=$(printf '%s' "$PW" | docker exec -i -w /app uptime-kuma node -e \
  'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>require("bcryptjs").hash(s,10).then(h=>console.log(h)))')
case "$HASH" in
  '$2'*) ;;
  *) echo "!! couldn't hash the Uptime Kuma password: open it on port 3001 to finish its setup"; exit 1 ;;
esac
sqlite3 "$DB" "INSERT INTO user (username, password, active) VALUES ('admin', '$HASH', 1);"
touch "$ENVF"; chmod 600 "$ENVF"
sed -i '/^KUMA_ADMIN_USER=/d; /^KUMA_ADMIN_PASSWORD=/d' "$ENVF"
printf 'KUMA_ADMIN_USER=admin\nKUMA_ADMIN_PASSWORD=%s\n' "$PW" >> "$ENVF"
docker restart uptime-kuma >/dev/null
for i in $(seq 1 40); do curl -s -o /dev/null http://localhost:3001/ && break; sleep 3; done
echo "   Uptime Kuma is set up: user admin, password saved as KUMA_ADMIN_PASSWORD in $ENVF"
