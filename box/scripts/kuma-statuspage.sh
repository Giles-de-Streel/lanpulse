#!/bin/bash
# Build (or rebuild) a public, read-only Uptime Kuma status page from the monitor groups.
# This is the page the volunteer screen shows. No login needed to view it.
#   sudo kuma-statuspage.sh ["Page title"] ["Description"]
# Page URL: http://<box>:3001/status/dashboard
# kuma-apply.sh calls this automatically, so the page always matches the monitors.
# The title/description given here are saved to .env and reused on later runs.
set -euo pipefail
APP=${APP:-/opt/monitor}
DB=$APP/uptime-kuma/kuma.db
ENV=$APP/.env
SLUG=dashboard
NO_RESTART=${NO_RESTART:-0}   # set to 1 when the caller has already stopped Kuma

setenv() { sed -i "/^$1=/d" "$ENV"; printf '%s="%s"\n' "$1" "$2" >> "$ENV"; }
getenv() { { grep "^$1=" "$ENV" 2>/dev/null || true; } | cut -d= -f2- | sed 's/^"//; s/"$//'; }
[ -n "${1:-}" ] && setenv STATUS_TITLE "$1"
[ -n "${2:-}" ] && setenv STATUS_DESC "$2"
TITLE=$(getenv STATUS_TITLE); TITLE=${TITLE:-Network status}
DESC=$(getenv STATUS_DESC)
DESC=${DESC:-Green means working. Red means not responding. If something stays red for more than 10 minutes, let the network volunteer know.}
q() { printf "%s" "$1" | sed "s/'/''/g"; }

CSS='.title-flex{justify-content:center} .monitor-list .item .info{font-size:1.25rem} .description{font-size:1.15rem;text-align:center}'

cd "$APP"
[ "$NO_RESTART" = 1 ] || { docker compose stop uptime-kuma >/dev/null; cp "$DB" "$DB.bak-$(date +%Y%m%d-%H%M%S)"; }
sqlite3 -bail "$DB" <<EOF
PRAGMA foreign_keys=ON;
BEGIN;
INSERT INTO status_page (slug, title, description, icon, theme, published, search_engine_index,
                         show_tags, show_powered_by, auto_refresh_interval, footer_text, custom_css)
  SELECT '$SLUG', '$(q "$TITLE")', '$(q "$DESC")', '/icon.svg', 'auto', 1, 0, 0, 0, 60,
         'Updated every minute', '$(q "$CSS")'
  WHERE NOT EXISTS (SELECT 1 FROM status_page WHERE slug='$SLUG');
UPDATE status_page SET title='$(q "$TITLE")', description='$(q "$DESC")', auto_refresh_interval=60,
       show_powered_by=0, search_engine_index=0, custom_css='$(q "$CSS")', modified_date=CURRENT_TIMESTAMP
  WHERE slug='$SLUG';
-- rebuild the page's sections from the monitor groups
DELETE FROM "group" WHERE status_page_id=(SELECT id FROM status_page WHERE slug='$SLUG');
INSERT INTO "group" (name, public, active, weight, status_page_id)
  SELECT g.name, 1, 1, g.weight, (SELECT id FROM status_page WHERE slug='$SLUG')
  FROM monitor g WHERE g.type='group' AND EXISTS (SELECT 1 FROM monitor c WHERE c.parent=g.id)
  ORDER BY g.weight;
INSERT INTO monitor_group (monitor_id, group_id, weight)
  SELECT m.id, sg.id, m.weight
  FROM monitor m JOIN monitor g ON g.id=m.parent
  JOIN "group" sg ON sg.name=g.name AND sg.status_page_id=(SELECT id FROM status_page WHERE slug='$SLUG')
  WHERE m.type<>'group' AND m.active=1;
-- monitors with no group go in an "Other" section
INSERT INTO "group" (name, public, active, weight, status_page_id)
  SELECT 'Other', 1, 1, 99999, (SELECT id FROM status_page WHERE slug='$SLUG')
  WHERE EXISTS (SELECT 1 FROM monitor WHERE type<>'group' AND parent IS NULL AND active=1);
INSERT INTO monitor_group (monitor_id, group_id, weight)
  SELECT m.id, (SELECT id FROM "group" WHERE name='Other' AND status_page_id=(SELECT id FROM status_page WHERE slug='$SLUG')), m.weight
  FROM monitor m WHERE m.type<>'group' AND m.parent IS NULL AND m.active=1;
COMMIT;
EOF
[ "$NO_RESTART" = 1 ] || docker compose start uptime-kuma >/dev/null
echo "Status page '$TITLE': http://$(hostname -I | awk '{print $1}'):3001/status/$SLUG"
sqlite3 -readonly "$DB" "SELECT '  ' || g.name || ': ' || count(mg.id) || ' monitors' FROM \"group\" g LEFT JOIN monitor_group mg ON mg.group_id=g.id WHERE g.status_page_id=(SELECT id FROM status_page WHERE slug='$SLUG') GROUP BY g.id ORDER BY g.weight;"
