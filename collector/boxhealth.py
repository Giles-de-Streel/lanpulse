"""lanpulse: the monitoring box's own health, shown in the "Monitoring box" group.

  - Power: on a laptop, mains power dropping means the site's power is out (the battery keeps the box
    running). Shown red with how long and how much battery is left; afterwards the tile says when the
    last outage was. Desktops without a battery get no power tile.
  - Disk: amber above 85% full, red above 95%.
Runs inside the collector container: /sys is visible read-only there, and /data is the box's disk.
"""
import json, os, time

STATE = os.path.join(os.environ.get("LANPULSE_CONFIG_DIR", "/config"), "power-state.json")
PS = "/sys/class/power_supply"


def _read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return None


def _hm(t):
    return time.strftime("%-I:%M %p", time.localtime(t)).lower() if os.name != "nt" else time.strftime("%H:%M", time.localtime(t))


def _mins(sec):
    m = round(sec / 60)
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60} min"


def power():
    if not os.path.isdir(PS):
        return None
    mains, bats = [], []
    for d in os.listdir(PS):
        kind = _read(f"{PS}/{d}/type")
        if kind == "Mains":
            mains.append(_read(f"{PS}/{d}/online") == "1")
        elif kind == "Battery" and _read(f"{PS}/{d}/present") != "0":
            cap = _read(f"{PS}/{d}/capacity")
            now = _read(f"{PS}/{d}/energy_now") or _read(f"{PS}/{d}/charge_now")
            rate = _read(f"{PS}/{d}/power_now") or _read(f"{PS}/{d}/current_now")
            bats.append({"pct": int(cap) if cap and cap.isdigit() else None, "status": _read(f"{PS}/{d}/status"),
                         "left_h": (int(now) / int(rate)) if now and rate and now.isdigit() and rate.isdigit() and int(rate) > 0 else None})
    if not mains or not bats:
        return None                          # desktop, or no battery: nothing to say
    return {"mains": any(mains), "battery": bats[0]}


def items():
    out = []
    try:
        st = json.load(open(STATE))
    except (OSError, ValueError):
        st = {}
    p = power()
    now = time.time()
    if p:
        b = p["battery"]
        pct = f"{b['pct']}%" if b["pct"] is not None else "?"
        if not p["mains"]:
            st.setdefault("since", now)
            left = f", about {_mins(b['left_h'] * 3600)} left" if b.get("left_h") else ""
            low = " The box will shut down when the battery runs out." if (b["pct"] or 100) <= 20 else ""
            msg = f"Power cut? Running on battery since {_hm(st['since'])} ({_mins(now - st['since'])}): {pct}{left}.{low}"
            out.append({"id": "box-power", "name": "Power", "kind": "box", "status": "down", "note": msg, "message": msg,
                        "hint": "Check the site's power and the circuit the box is plugged into. If only this socket "
                                "is off, plug the box into another one."})
        else:
            if st.get("since"):
                st["last"] = {"start": st.pop("since"), "end": now}
            msg = f"Mains power · battery {pct}" + (" (charging)" if b["status"] == "Charging" else "")
            last = st.get("last")
            if last and now - last["end"] < 86400:
                msg += f" · power was out {_hm(last['start'])}–{_hm(last['end'])} ({_mins(last['end'] - last['start'])})"
            out.append({"id": "box-power", "name": "Power", "kind": "box", "status": "up", "note": msg, "message": msg})
        try:
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            json.dump(st, open(STATE, "w"))
        except OSError:
            pass
    try:
        v = os.statvfs(os.environ.get("PUBLISH_FILE_DIR", "/data"))
        total, free = v.f_blocks * v.f_frsize, v.f_bavail * v.f_frsize
        used = round(100 * (1 - free / total)) if total else 0
        status = "down" if used >= 95 else "warn" if used >= 85 else "up"
        msg = f"{used}% used · {free / 1e9:.0f} GB free"
        out.append({"id": "box-disk", "name": "Disk space", "kind": "box", "status": status, "note": msg, "message": msg,
                    **({"hint": "Old backups are in /opt/monitor/backups and container logs are capped; "
                                "check for large files with: sudo du -xh /opt/monitor | sort -h | tail"} if status != "up" else {})})
    except (OSError, ZeroDivisionError, AttributeError):
        pass
    return out
