#!/bin/bash
# Set up phone push alerts through ntfy (https://ntfy.sh) for every Uptime Kuma monitor.
#   sudo kuma-ntfy.sh            # creates a random topic (first run) and wires it up
#   sudo kuma-ntfy.sh --test     # just send a test push
#
# The topic and server are kept in /opt/monitor/.env (NTFY_TOPIC, NTFY_SERVER), not in git.
# On the public ntfy.sh server anyone who knows the topic can read the alerts, so the
# random topic name is the password: don't post it anywhere.
# Subscribe in the ntfy phone app: + -> topic name (server ntfy.sh, or your own).
set -euo pipefail
APP=${APP:-/opt/monitor}
ENV=$APP/.env
DB=$APP/uptime-kuma/kuma.db
cd "$APP"

grep -q '^NTFY_SERVER=' "$ENV" || echo "NTFY_SERVER=https://ntfy.sh" >> "$ENV"
grep -q '^NTFY_TOPIC=' "$ENV" || echo "NTFY_TOPIC=netmon-$(openssl rand -hex 16 | cut -c1-20)" >> "$ENV"
NTFY_SERVER=$(grep '^NTFY_SERVER=' "$ENV" | cut -d= -f2-)
NTFY_TOPIC=$(grep '^NTFY_TOPIC=' "$ENV" | cut -d= -f2-)
BOX=$(hostname)

send_test() {
  curl -s -o /dev/null -w "test push: HTTP %{http_code}\n" \
    -H "Title: Network monitor ($BOX) is connected" -H "Tags: white_check_mark" \
    -d "Alerts from $BOX will arrive here when something goes down or comes back up." \
    "$NTFY_SERVER/$NTFY_TOPIC"
}
[ "${1:-}" = "--test" ] && { send_test; exit 0; }

CONFIG=$(cat <<EOF
{"name":"Phone (ntfy)","type":"ntfy","isDefault":true,"applyExisting":true,
 "ntfyserverurl":"$NTFY_SERVER","ntfytopic":"$NTFY_TOPIC",
 "ntfyPriority":3,"ntfyPriorityDown":5,"ntfyAuthenticationMethod":"none"}
EOF
)
CONFIG=$(echo "$CONFIG" | tr -d '\n' | sed "s/'/''/g")

docker compose stop uptime-kuma >/dev/null
cp "$DB" "$DB.bak-$(date +%Y%m%d-%H%M%S)"
sqlite3 -bail "$DB" <<EOF
PRAGMA foreign_keys=ON;
BEGIN;
UPDATE notification SET config='$CONFIG', active=1, is_default=1 WHERE name='Phone (ntfy)';
INSERT INTO notification (name, active, user_id, is_default, config)
  SELECT 'Phone (ntfy)', 1, (SELECT min(id) FROM user), 1, '$CONFIG'
  WHERE NOT EXISTS (SELECT 1 FROM notification WHERE name='Phone (ntfy)');
INSERT INTO monitor_notification (monitor_id, notification_id)
  SELECT m.id, n.id FROM monitor m, notification n
  WHERE n.name='Phone (ntfy)' AND m.type<>'group'
    AND NOT EXISTS (SELECT 1 FROM monitor_notification x WHERE x.monitor_id=m.id AND x.notification_id=n.id);
COMMIT;
EOF
docker compose start uptime-kuma >/dev/null
echo "ntfy alerts attached to $(sqlite3 -readonly "$DB" "SELECT count(*) FROM monitor_notification mn JOIN notification n ON n.id=mn.notification_id WHERE n.name='Phone (ntfy)';") monitors"
echo "Subscribe in the ntfy app to topic: $NTFY_TOPIC  (server: $NTFY_SERVER)"
send_test
