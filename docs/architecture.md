# Architecture

```
┌──────────────────────────── monitoring box (/opt/monitor) ────────────────────────────┐
│                                                                                         │
│  Uptime Kuma :3001 ── checks every 60 s, alerts (ntfy), public status page "dashboard"  │
│        │ status-page API (no login)                                                     │
│        ▼                                                                                │
│  lanpulse-collector ── every 30 s: SOURCES → snapshot → PUBLISHERS                      │
│        │ file publisher                          └── http / gist / … (optional, cloud)  │
│        ▼                                                                                │
│  data/snapshot.json ◄── lanpulse-web (Caddy) :8080 serves web/ + data/                  │
│                                                                                         │
│  NetAlertX :20211 (device discovery)   Speedtest Tracker :8765 (ISP speed history)      │
└─────────────────────────────────────────────────────────────────────────────────────────┘
          ▲ LAN / tunnel                                    ▲ HTTPS
   wall screen (kiosk), phones, PCs              GitHub Pages copy of web/ reading a cloud snapshot
```

## Design choices

- **Proven tools under a thin layer.** Uptime Kuma does the checking and alerting. lanpulse adds the friendly view, portability and publishing. You can still use each tool directly.
- **One data contract.** Everything goes through `lanpulse.snapshot/1`. The dashboard doesn't know about Kuma, and the collector doesn't know about the dashboard.
- **Local first.** No account, cloud or internet is needed for the box to work. Cloud publishing is opt-in, per publisher.
- **No build steps.** The dashboard is plain ES modules and CSS. The collector is one stdlib-only Python file. Both are easy to read and change.
- **Config as files.** Monitors live in CSVs (`kuma-apply.sh`), settings in `/opt/monitor/lanpulse.env` and `.env`. Secrets never go in git.

## Adding a source (collector)

```python
def source_my_thing():
    return {"groups": [{"name": "My thing", "items": [
        {"id": "x1", "name": "Widget", "kind": "custom", "status": "up"}]}],
            "extras": {"widgets": 1}}
SOURCES["my-thing"] = source_my_thing
```
Then set `LANPULSE_SOURCES=uptime-kuma,my-thing` in `lanpulse.env`.

## Adding a publisher (collector)

```python
def publish_my_cloud(snap, body):   # body = JSON bytes
    http("https://…", method="PUT", data=body, headers={...})
PUBLISHERS["mycloud"] = publish_my_cloud
```
Then set `LANPULSE_PUBLISHERS=mycloud`.

## Adding a dashboard data source

```js
PROVIDERS.mything = {
  label: "My thing", help: "Where it reads from",
  fields: [{ key: "token", label: "Token", secret: true }],
  load: async (cfg) => (await fetch("https://…", { headers: { Authorization: cfg.token } })).json(),
};
```

## Box scripts

| Script | Purpose |
|---|---|
| `setup.sh` | install or upgrade the whole box (safe to re-run) |
| `kuma-apply.sh [--prune] site.csv` | CSV → Uptime Kuma monitors (matched by name, history kept) |
| `kuma-export.sh` | Uptime Kuma → CSV |
| `kuma-statuspage.sh "Title"` | public status page from monitor groups (the collector's input) |
| `kuma-ntfy.sh [--test]` | ntfy phone alerts on every monitor |
| `kiosk-setup.sh [user] [url]` | auto-login wall screen |
| `backup.sh` | nightly data tarball (cron) |

The `kuma-*` scripts edit Uptime Kuma's SQLite database directly while Kuma is stopped for a few seconds, and they back it up first. Kuma has no supported REST API for this. If a future Kuma version changes its schema, these scripts are the place to update.
