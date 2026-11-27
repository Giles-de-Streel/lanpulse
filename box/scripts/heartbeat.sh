#!/bin/bash
# "Still alive" ping for an outside dead-man's-switch service (healthchecks.io, Uptime Kuma push
# monitor elsewhere, ...), so you hear about it when the whole box goes quiet: dead, frozen, unplugged
# or offline. Set LANPULSE_HEARTBEAT_URL in lanpulse.env; a systemd timer runs this every 5 minutes.
# It pings only while the dashboard data is fresh, so a stopped collector also counts as "quiet".
# healthchecks.io URLs get /fail instead, which alerts straight away rather than after the grace time.
APP=${APP:-/opt/monitor}
URL=$(sed -n 's/^LANPULSE_HEARTBEAT_URL=//p' "$APP/lanpulse.env" 2>/dev/null | tail -1 | tr -d '"'"'")
[ -n "$URL" ] || exit 0
SNAP=$APP/data/snapshot.json
age=999999
[ -f "$SNAP" ] && age=$(( $(date +%s) - $(stat -c %Y "$SNAP") ))
if [ "$age" -le 300 ]; then
  curl -fsS -m 15 --retry 3 -o /dev/null "$URL"
elif [[ "$URL" == *hc-ping.com* ]]; then
  curl -fsS -m 15 --retry 3 -o /dev/null --data-raw "dashboard data is ${age}s old: collector stopped?" "$URL/fail"
fi
