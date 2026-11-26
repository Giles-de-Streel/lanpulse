"""History for the dashboard's History page (snapshot extras.history).

- Uptime per monitor (hourly for 48 h, daily for 30 days) from Uptime Kuma's own summary tables.
- Outages and recoveries from Kuma's "important" heartbeats (status changes), with durations.
- Status changes of lanpulse's own checks (network health, router), kept in /data/history-changes.json.

Kuma keeps recent writes in its WAL file and its folder is mounted read-only, so the database and
WAL are copied to a temporary folder and read from there. Recomputed every HISTORY_EVERY seconds.
"""
import datetime, json, os, shutil, sqlite3, tempfile, time

KUMA_DB = os.environ.get("KUMA_DB", "/kuma/kuma.db")
DATA = os.environ.get("PUBLISH_FILE_DIR", "/data")
EVERY = int(os.environ.get("HISTORY_EVERY", "600"))
DAYS = 30
MAX_EVENTS = 200
SKIP = {"net-devices", "net-weak-wifi"}       # change constantly; shown as quick links, not history
STATUS = {0: "down", 1: "up", 2: "pending", 3: "maintenance"}

_cache = {"at": 0, "data": None}


def _iso(t):
    """Kuma stores UTC as 'YYYY-MM-DD HH:MM:SS.mmm'."""
    return t.replace(" ", "T")[:19] + "Z" if t else None


def _secs(a, b):
    f = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return max(0, int((f(b) - f(a)).total_seconds()))


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kuma_query(sql, args=()):
    """Run one read query on a fresh copy of Kuma's database (+ WAL, which holds recent writes)."""
    if not os.path.exists(KUMA_DB):
        return []
    tmp = tempfile.mkdtemp(prefix="lp-kuma-")
    try:
        db = os.path.join(tmp, "kuma.db")
        shutil.copyfile(KUMA_DB, db)
        if os.path.exists(KUMA_DB + "-wal"):
            shutil.copyfile(KUMA_DB + "-wal", db + "-wal")
        con = sqlite3.connect(db, timeout=5)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ntfy_targets():
    """Phone-alert (ntfy) destinations configured in Uptime Kuma. Never returns logins or tokens."""
    out = []
    for name, active, cfg in kuma_query("SELECT name, active, config FROM notification"):
        try:
            c = json.loads(cfg or "{}")
        except ValueError:
            continue
        if c.get("type") != "ntfy" or not c.get("ntfytopic"):
            continue
        out.append({"name": name, "active": bool(active),
                    "server": (c.get("ntfyserverurl") or "https://ntfy.sh").rstrip("/"),
                    "topic": c["ntfytopic"],
                    "login": (c.get("ntfyAuthenticationMethod") or "none") != "none"})
    return out


def kuma_history(items):
    """items: the snapshot's kuma-* items (their ids and names match the tiles)."""
    if not os.path.exists(KUMA_DB):
        return None
    want = {int(i["id"][5:]): i["name"] for i in items if i["id"].startswith("kuma-") and i["id"][5:].isdigit()}
    if not want:
        return None
    tmp = tempfile.mkdtemp(prefix="lp-kuma-")
    try:
        db = os.path.join(tmp, "kuma.db")
        shutil.copyfile(KUMA_DB, db)
        if os.path.exists(KUMA_DB + "-wal"):
            shutil.copyfile(KUMA_DB + "-wal", db + "-wal")
        con = sqlite3.connect(db, timeout=5)
        ids = ",".join(str(i) for i in want)
        since_day = int(time.time()) // 86400 * 86400 - (DAYS - 1) * 86400
        daily, hourly = {}, {}
        for mid, ts, up, down in con.execute(
                f"SELECT monitor_id, timestamp, up, down FROM stat_daily WHERE monitor_id IN ({ids}) AND timestamp >= ?", (since_day,)):
            daily.setdefault(mid, []).append([ts, up, down])
        for mid, ts, up, down in con.execute(
                f"SELECT monitor_id, timestamp, up, down FROM stat_hourly WHERE monitor_id IN ({ids}) AND timestamp >= ?",
                (int(time.time()) // 3600 * 3600 - 47 * 3600,)):
            hourly.setdefault(mid, []).append([ts, up, down])
        beats = con.execute(
            f"SELECT monitor_id, status, time, msg FROM heartbeat WHERE important = 1 AND monitor_id IN ({ids}) "
            f"AND time >= datetime('now', '-{DAYS} days') ORDER BY time").fetchall()
        first = con.execute(f"SELECT min(time) FROM heartbeat WHERE monitor_id IN ({ids})").fetchone()[0]
        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    now = _now()
    events, last, outages = [], {}, {m: [] for m in want}
    for mid, st, t, msg in beats:
        s, t = STATUS.get(st, "unknown"), _iso(t)
        prev = last.get(mid)
        ev = {"t": t, "id": f"kuma-{mid}", "name": want[mid], "s": s, "msg": (msg or "")[:120]}
        if prev is None:
            ev["first"] = True                         # monitoring started: not a change
        elif s == "up" and prev["s"] == "down":
            ev["dur_s"] = _secs(prev["t"], t)
            outages[mid].append([prev["t"], t, ev["dur_s"]])
        last[mid] = {"s": s, "t": t}
        events.append(ev)
    for mid, l in last.items():                        # still down now
        if l["s"] == "down":
            outages[mid].append([l["t"], None, _secs(l["t"], now)])

    monitors = []
    for mid, name in want.items():
        monitors.append({"id": f"kuma-{mid}", "name": name,
                         "daily": sorted(daily.get(mid, [])), "hourly": sorted(hourly.get(mid, [])),
                         "outages": outages[mid][-50:]})
    return {"from": _iso(first), "monitors": monitors, "events": events[-MAX_EVENTS:]}


def _boot_time():
    """Machine boot time (epoch). Containers share the host kernel, so this is the box's boot."""
    try:
        for line in open("/proc/stat"):
            if line.startswith("btime "):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def _last_snapshot_time():
    """When the previous snapshot was written (read before this run publishes a new one)."""
    try:
        g = json.load(open(os.path.join(DATA, "snapshot.json"))).get("generated")
        return datetime.datetime.fromisoformat(g.replace("Z", "+00:00")).timestamp() if g else None
    except (OSError, ValueError, AttributeError):
        return None


_boot_checked = {"done": False}


def note_boot(state):
    """Once per collector start: if the box rebooted since last time, add a timeline entry saying how
    long monitoring was paused (the box can't watch anything while it is off or restarting, so a
    reboot, crash or power cut otherwise leaves only a silent gap in the history)."""
    if _boot_checked["done"]:
        return False
    _boot_checked["done"] = True
    bt = _boot_time()
    if bt is None:
        return False
    prev = state.get("boot")
    state["boot"] = bt
    if prev is None or abs(bt - prev) < 5:      # first run, or only the collector restarted
        return True
    last = _last_snapshot_time()
    gap = int(time.time() - last) if last and last < time.time() else None
    long_off = gap is not None and gap > 15 * 60
    msg = (f"monitoring paused for about {max(1, round(gap / 60))} min" if gap else "monitoring paused") + \
          (" (it was off for a while: power cut or switched off?)" if long_off else "")
    state["events"].append({"t": datetime.datetime.fromtimestamp(bt, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "id": "box-boot", "name": "Monitoring box", "s": "restart", "from": "up",
                            "kind": "boot", "gap_s": gap, "msg": msg})
    return True


def track_changes(snap):
    """Record status changes of lanpulse's own (non-Kuma) items between snapshots."""
    path = os.path.join(DATA, "history-changes.json")
    try:
        state = json.load(open(path))
    except (OSError, ValueError):
        state = {"last": {}, "events": []}
    now = _now()
    changed = note_boot(state)
    for g in snap.get("groups", []):
        for i in g["items"]:
            if i["id"].startswith("kuma-") or i["id"] in SKIP:
                continue
            was = state["last"].get(i["id"])
            if was != i["status"]:
                if was is not None:
                    state["events"].append({"t": now, "id": i["id"], "name": i["name"], "s": i["status"],
                                            "from": was, "msg": (i.get("note") or i.get("message") or "")[:120]})
                state["last"][i["id"]] = i["status"]
                changed = True
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["events"] = [e for e in state["events"] if e["t"] >= cutoff][-1000:]
    if changed:
        tmp = path + ".tmp"
        json.dump(state, open(tmp, "w"), separators=(",", ":"))
        os.replace(tmp, path)
    return state["events"]


def history(snap, log=print):
    try:
        changes = track_changes(snap)
    except Exception as e:
        log(f"history: change log failed: {e}")
        changes = []
    # Refresh every EVERY seconds, or on every cycle while there's nothing yet (e.g. just after boot,
    # when Uptime Kuma is still starting and the snapshot has no Kuma items).
    if time.time() - _cache["at"] > EVERY or not _cache["data"]:
        try:
            items = [i for g in snap.get("groups", []) for i in g["items"]]
            data = kuma_history(items)
            if data:
                _cache.update(at=time.time(), data=data)
        except Exception as e:
            log(f"history: kuma read failed: {e}")
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {**(_cache["data"] or {}), "changes": [c for c in changes if c["t"] >= cutoff][-MAX_EVENTS:]}
