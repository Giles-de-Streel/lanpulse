#!/bin/bash
# Export Uptime Kuma's current monitors to the site CSV format (stdout), so changes
# made in the web UI can be saved back to the repo.
#   sudo kuma-export.sh > monitors/<site>.csv
# Only ping/http/port/dns monitors are exported; other types are listed as comments.
# Exports the device list of the network the box is on now (the running monitors).
set -euo pipefail
DB=${APP:-/opt/monitor}/uptime-kuma/kuma.db
echo "group,name,type,target,description"
sqlite3 -readonly -separator ',' "$DB" "
SELECT CASE WHEN m.type IN ('ping','http','port','dns') THEN '' ELSE '# unsupported: ' END
       || coalesce(g.name,'Ungrouped'), m.name, m.type,
       CASE m.type WHEN 'ping' THEN m.hostname
                   WHEN 'http' THEN m.url
                   WHEN 'port' THEN m.hostname || ':' || m.port
                   WHEN 'dns'  THEN m.hostname || '@' || m.dns_resolve_server
                   ELSE coalesce(m.hostname, m.url, '') END,
       replace(coalesce(m.description,''), char(10), ' ')
FROM monitor m LEFT JOIN monitor g ON g.id = m.parent
WHERE m.type <> 'group' AND m.active = 1
ORDER BY coalesce(g.weight, 99999), m.weight, m.id;"
