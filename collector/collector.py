#!/usr/bin/env python3
"""
lanpulse collector: turns monitoring data into a standard snapshot and publishes it.

    sources  ->  snapshot (lanpulse.snapshot/1)  ->  publishers

Sources and publishers are small plug-ins registered below, so new ones (another
monitoring tool, Google Drive, S3, MQTT...) can be added without touching the rest.
Standard library only, so it runs in a stock python:3 image.

Configuration is all environment variables (see box/lanpulse.env.example).
"""
import json, os, sys, time, urllib.request, urllib.error, datetime, traceback

SCHEMA = "lanpulse.snapshot/1"
INTERVAL = int(os.environ.get("LANPULSE_INTERVAL", "30"))
SITE_NAME = os.environ.get("LANPULSE_SITE_NAME", "")
SITE_DESC = os.environ.get("LANPULSE_SITE_DESC", "")


def log(*a):
    print(datetime.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def http(url, method="GET", data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- sources
# A source returns a partial snapshot: {"site": {...}, "groups": [...], "extras": {...}}

STATUS = {0: "down", 1: "up", 2: "pending", 3: "maintenance"}


def source_uptime_kuma():
    """Reads an Uptime Kuma status page through its public (no-login) API."""
    base = os.environ.get("KUMA_URL", "http://uptime-kuma:3001").rstrip("/")
    slug = os.environ.get("KUMA_STATUS_SLUG", "dashboard")
    _, page = http(f"{base}/api/status-page/{slug}")
    _, beats = http(f"{base}/api/status-page/heartbeat/{slug}")
    page, beats = json.loads(page), json.loads(beats)
    hb, up = beats.get("heartbeatList", {}), beats.get("uptimeList", {})
    groups = []
    for g in page.get("publicGroupList", []):
        items = []
        for m in g.get("monitorList", []):
            mid = str(m["id"])
            hist = hb.get(mid, [])[-30:]
            last = hist[-1] if hist else {}
            change = None
            for b in reversed(hist):            # when did it enter its current state?
                if b.get("status") != last.get("status"):
                    break
                change = b.get("time")
            items.append({
                "id": f"kuma-{mid}",
                "name": m.get("name"),
                "kind": m.get("type", "check"),
                "status": STATUS.get(last.get("status"), "unknown"),
                "latency_ms": last.get("ping"),
                "uptime_24h": up.get(f"{mid}_24"),
                "since": change,
                "message": (last.get("msg") or "")[:200],
                "history": [{"t": b.get("time"), "s": STATUS.get(b.get("status"), "unknown"),
                             "ms": b.get("ping")} for b in hist],
            })
        groups.append({"name": g.get("name"), "items": items})
    cfg = page.get("config", {})
    return {"site": {"name": cfg.get("title"), "description": cfg.get("description")},
            "groups": groups}


SOURCES = {"uptime-kuma": source_uptime_kuma}


# ------------------------------------------------------------------------ publishers
# A publisher receives the finished snapshot (dict) and its JSON bytes.

def publish_file(snap, body):
    """Always on: writes data/snapshot.json for the dashboard served by the box."""
    d = os.environ.get("PUBLISH_FILE_DIR", "/data")
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".snapshot.json.tmp")
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, os.path.join(d, "snapshot.json"))


def publish_http_put(snap, body):
    """PUT the snapshot to any URL: WebDAV/Nextcloud, an S3/R2 presigned URL, a webhook...
    PUBLISH_HTTP_URL, optional PUBLISH_HTTP_METHOD (PUT), PUBLISH_HTTP_AUTH (header value)."""
    url = os.environ["PUBLISH_HTTP_URL"]
    hdr = {"Content-Type": "application/json"}
    if os.environ.get("PUBLISH_HTTP_AUTH"):
        hdr["Authorization"] = os.environ["PUBLISH_HTTP_AUTH"]
    http(url, method=os.environ.get("PUBLISH_HTTP_METHOD", "PUT"), data=body, headers=hdr)


def publish_github_gist(snap, body):
    """Update snapshot.json in a GitHub Gist (free, no server). GIST_ID + GIST_TOKEN (gist scope).
    Read it back from https://gist.githubusercontent.com/<user>/<GIST_ID>/raw/snapshot.json
    Note: a secret gist is unlisted, not private. Anyone with the URL can read it."""
    gid, tok = os.environ["GIST_ID"], os.environ["GIST_TOKEN"]
    payload = json.dumps({"files": {"snapshot.json": {"content": body.decode()}}}).encode()
    http(f"https://api.github.com/gists/{gid}", method="PATCH", data=payload,
         headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                  "User-Agent": "lanpulse-collector"})


PUBLISHERS = {"file": publish_file, "http": publish_http_put, "gist": publish_github_gist}


# ---------------------------------------------------------------------------- main

def build_snapshot():
    snap = {"schema": SCHEMA, "generated": now_iso(), "sources": [], "site": {},
            "groups": [], "extras": {}}
    for name in os.environ.get("LANPULSE_SOURCES", "uptime-kuma").split(","):
        name = name.strip()
        try:
            part = SOURCES[name]()
            snap["sources"].append({"name": name, "ok": True})
            for k, v in (part.get("site") or {}).items():
                snap["site"].setdefault(k, v)
            snap["groups"] += part.get("groups", [])
            snap["extras"].update(part.get("extras", {}))
        except Exception as e:
            snap["sources"].append({"name": name, "ok": False, "error": str(e)[:200]})
            log(f"source {name} failed: {e}")
    if SITE_NAME:
        snap["site"]["name"] = SITE_NAME
    if SITE_DESC:
        snap["site"]["description"] = SITE_DESC
    counts = {}
    for g in snap["groups"]:
        for i in g["items"]:
            counts[i["status"]] = counts.get(i["status"], 0) + 1
    snap["summary"] = {"total": sum(counts.values()), **{s: counts.get(s, 0) for s in
                       ("up", "down", "pending", "maintenance", "unknown")}}
    return snap


def run_once():
    snap = build_snapshot()
    body = json.dumps(snap, separators=(",", ":")).encode()
    names = ["file"] + [p.strip() for p in os.environ.get("LANPULSE_PUBLISHERS", "").split(",")
                        if p.strip() and p.strip() != "file"]
    for p in names:
        try:
            PUBLISHERS[p](snap, body)
        except Exception as e:
            log(f"publisher {p} failed: {e}")
    return snap


if __name__ == "__main__":
    once = "--once" in sys.argv
    log(f"lanpulse collector: sources={os.environ.get('LANPULSE_SOURCES', 'uptime-kuma')} "
        f"publishers=file{','+os.environ['LANPULSE_PUBLISHERS'] if os.environ.get('LANPULSE_PUBLISHERS') else ''} "
        f"every {INTERVAL}s")
    while True:
        try:
            s = run_once()
            if once:
                print(json.dumps(s["summary"]))
                break
        except Exception:
            traceback.print_exc()
            if once:
                sys.exit(1)
        time.sleep(INTERVAL)
