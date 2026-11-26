#!/bin/bash
# Load monitors from a site CSV into Uptime Kuma.
#   sudo kuma-apply.sh [--prune] monitors/<site>.csv
#
# CSV columns: group,name,type,target,description
#   type    target example
#   ping    192.168.1.1
#   http    https://example.com          (up = HTTP 200-299)
#   port    192.168.1.10:445             (TCP port open)
#   dns     google.com@192.168.1.1       (name lookup via that server)
# Lines starting with # are ignored. Only the description may contain commas.
#
# Matching is by monitor name: existing monitors are updated in place (history kept),
# new ones are added. Monitors not in the CSV are left alone, unless --prune is given,
# which deletes them (and any groups left empty).
# Each network the box has been on keeps its own device list (see collector/netswitch.py): this
# script changes only the list of the network the box is on now, and never touches the others.
# The dashboard's admin view does the same without a console (the "This network" card).
# Uptime Kuma is stopped for a few seconds; the database is backed up first.
set -euo pipefail

PRUNE=0; [ "${1:-}" = "--prune" ] && { PRUNE=1; shift; }
CSV=${1:?usage: kuma-apply.sh [--prune] <site.csv>}
APP=${APP:-/opt/monitor}
DB=$APP/uptime-kuma/kuma.db
[ -f "$CSV" ] || { echo "No such file: $CSV"; exit 1; }
[ -f "$DB" ] || { echo "No Kuma database at $DB (open Kuma once and create the admin account first)"; exit 1; }

q() { printf "%s" "$1" | sed "s/'/''/g"; }
# the network the box is on now (netswitch.py); empty on boxes that haven't detected one yet
CUR=$(python3 -c "import json;print(json.load(open('$APP/config/networks.json')).get('current') or '')" 2>/dev/null || true)
[[ "$CUR" =~ ^[a-z0-9]{1,20}$ ]] || CUR=""
TAGQ="(SELECT id FROM tag WHERE name='lanpulse-network')"
OTHER="(SELECT monitor_id FROM monitor_tag WHERE tag_id=$TAGQ AND value<>'$CUR')"   # other networks' monitors
BOXM="(SELECT monitor_id FROM monitor_tag WHERE tag_id=$TAGQ AND value='box')"   # the box's own checks (netswitch.py)
lit() { [ -z "$1" ] && echo NULL || echo "'$(q "$1")'"; }

SQL=$(mktemp); trap 'rm -f "$SQL"' EXIT
UID_SQL="(SELECT min(id) FROM user)"
echo "PRAGMA foreign_keys=ON; BEGIN;" > "$SQL"
w=1000; n=0; NAMES=""
while IFS=, read -r group name type target desc || [ -n "${group:-}" ]; do
  trim() { printf "%s" "$1" | sed 's/\r$//; s/^[[:space:]]*//; s/[[:space:]]*$//'; }
  group=$(trim "$group"); name=$(trim "${name:-}"); type=$(trim "${type:-}")
  target=$(trim "${target:-}"); desc=$(trim "${desc:-}")
  [[ -z "$group" || "$group" == \#* || "$group" == group ]] && continue
  w=$((w+10))
  host=""; url=""; port=""; dsrv=""; dtype=""
  case "$type" in
    ping) host=$target ;;
    http) url=$target ;;
    port) host=${target%:*}; port=${target##*:} ;;
    dns)  host=${target%@*}; dsrv=${target##*@}; port=53; dtype=A ;;
    *) echo "skipping '$name': unknown type '$type'" >&2; continue ;;
  esac
  G=$(q "$group"); N=$(q "$name")
  PARENT="(SELECT id FROM monitor WHERE name='$G' AND type='group')"
  cat >> "$SQL" <<EOF
INSERT INTO monitor (name,type,user_id,interval,retry_interval,maxretries,weight)
  SELECT '$G','group',$UID_SQL,60,60,0,$w WHERE NOT EXISTS (SELECT 1 FROM monitor WHERE name='$G' AND type='group');
UPDATE monitor SET type='$type', hostname=$(lit "$host"), url=$(lit "$url"), port=${port:-NULL},
  dns_resolve_server=$(lit "$dsrv"), dns_resolve_type=$(lit "$dtype"), description=$(lit "$desc"),
  parent=$PARENT, weight=$w
  WHERE name='$N' AND type<>'group' AND id NOT IN $OTHER;
INSERT INTO monitor (name,type,user_id,hostname,url,port,dns_resolve_server,dns_resolve_type,
                     interval,retry_interval,maxretries,timeout,weight,parent,description)
  SELECT '$N','$type',$UID_SQL,$(lit "$host"),$(lit "$url"),${port:-NULL},$(lit "$dsrv"),$(lit "$dtype"),
         60,60,2,48,$w,$PARENT,$(lit "$desc")
  WHERE NOT EXISTS (SELECT 1 FROM monitor WHERE name='$N' AND type<>'group' AND (id NOT IN $OTHER OR id IN $BOXM));
EOF
  n=$((n+1)); NAMES="$NAMES,'$N'"
done < "$CSV"
if [ $PRUNE = 1 ] && [ -n "$NAMES" ]; then
  echo "DELETE FROM monitor WHERE type<>'group' AND name NOT IN (${NAMES#,}) AND id NOT IN $OTHER;" >> "$SQL"
  echo "DELETE FROM monitor WHERE type='group' AND id NOT IN (SELECT parent FROM monitor WHERE parent IS NOT NULL);" >> "$SQL"
fi
# Attach every default notification (e.g. ntfy from kuma-ntfy.sh) to monitors that lack it
cat >> "$SQL" <<'EOF'
INSERT INTO monitor_notification (monitor_id, notification_id)
  SELECT m.id, n.id FROM monitor m, notification n
  WHERE n.is_default=1 AND n.active=1 AND m.type<>'group'
    AND NOT EXISTS (SELECT 1 FROM monitor_notification x WHERE x.monitor_id=m.id AND x.notification_id=n.id);
EOF
# new monitors belong to the current network
if [ -n "$CUR" ]; then cat >> "$SQL" <<EOF
INSERT INTO tag (name, color) SELECT 'lanpulse-network', '#0f766e' WHERE NOT EXISTS (SELECT 1 FROM tag WHERE name='lanpulse-network');
INSERT INTO monitor_tag (monitor_id, tag_id, value) SELECT id, $TAGQ, '$CUR' FROM monitor
  WHERE type<>'group' AND id NOT IN (SELECT monitor_id FROM monitor_tag WHERE tag_id=$TAGQ);
EOF
fi
echo "COMMIT;" >> "$SQL"

cd "$APP"
docker compose stop uptime-kuma >/dev/null
cp "$DB" "$DB.bak-$(date +%Y%m%d-%H%M%S)"
if sqlite3 -bail "$DB" ".read $SQL"; then echo "applied $n monitors from $CSV"; else echo "FAILED, database unchanged"; fi
# keep the volunteer status page in step with the monitors (if it has been created)
if [ "$(sqlite3 -readonly "$DB" "SELECT count(*) FROM status_page WHERE slug='dashboard';")" = 1 ]; then
  NO_RESTART=1 "$(dirname "$0")/kuma-statuspage.sh" >/dev/null && echo "status page updated"
fi
docker compose start uptime-kuma >/dev/null
echo "Uptime Kuma restarted. Monitors now:"
sqlite3 -readonly "$DB" "SELECT coalesce(g.name,'-') || ' / ' || m.name FROM monitor m LEFT JOIN monitor g ON g.id=m.parent WHERE m.type<>'group' AND m.active=1 ORDER BY m.weight;"
