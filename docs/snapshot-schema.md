# Snapshot format: `lanpulse.snapshot/1`

The snapshot is the single contract between data collection and display. The collector writes it, the dashboard reads it, and other tools can produce or consume it.

```jsonc
{
  "schema": "lanpulse.snapshot/1",          // required, bump the number on breaking changes
  "generated": "2026-01-01T12:00:00+00:00", // required, ISO 8601. The dashboard flags data older than 3 min
  "sources": [ { "name": "uptime-kuma", "ok": true } ],   // what fed this snapshot, and any errors
  "site": { "name": "Example Center", "description": "Shown under the title" },
  "summary": { "total": 11, "up": 9, "down": 1, "pending": 1, "maintenance": 0, "unknown": 0 },
  "groups": [
    {
      "name": "Wi-Fi",
      "items": [
        {
          "id": "kuma-12",              // stable id, unique within the snapshot
          "name": "Main router",
          "kind": "ping",               // free text: ping, http, port, dns, device, …
          "status": "up",               // up | down | pending | maintenance | unknown
          "latency_ms": 3.2,            // optional
          "uptime_24h": 0.998,          // optional, 0-1
          "since": "2026-01-01 11:40:00", // optional, when the current status began (UTC; Z optional)
          "message": "",                // optional, last error or detail (shown on hover)
          "history": [ { "t": "…", "s": "up", "ms": 3.1 } ]  // optional, oldest first, up to ~30
        }
      ]
    }
  ],
  "extras": {}                          // free-form space for future panels (speed tests, device counts…)
}
```

## Rules

- Consumers ignore fields they don't know, so new fields can be added without a version bump.
- Status values outside the list display as `unknown`.
- Keep it small, ideally under 100 KB. It's fetched every 30 seconds.
- Times without a timezone are treated as UTC.

## Additions for network checks

- `status` may also be `warn` ("needs a look"); `summary.warn` counts them.
- Items may carry `note` (a plain-language finding shown instead of the status line) and `link`
  (`{type: wired|wifi, band: "2.4"|"5"|"6", node, rssi, rate_mbps, ssid}`).
- `extras.netcheck`: when and from which interface the checks ran.
- `extras.exposure`: `{public_ip, nat, upnp: {available, wan_ip, mappings: [{proto, external_port, internal, description}]}}`.
- `extras.airspace`: `{interface, own_ssids, networks: [{bssid, ssid, band, channel, signal_dbm, secure, utilisation_pct, stations, associated, own}]}`.
- `extras.devices`: `[{mac, name, ip, vendor, first_seen, last_seen, online, new, random_mac, ssid}]` (NetAlertX).
- `extras.topology`, `extras.clients`, `extras.client_bands`: from a router adapter (see network-checks.md).
