"""lanpulse: phone alerts (ntfy) for what Uptime Kuma doesn't watch.

Uptime Kuma already pushes an alert when one of its monitors (dashboard, router, internet, name
lookups, watched devices) stops responding. This adds the box's own network health checks and new
devices, sent to the same ntfy topic(s) Kuma uses:
  - a network check turns red ("down"), or the extra-DHCP-server check turns amber: alert, then
    "back to normal" when it recovers
  - a device joins the network for the first time: one message listing the new devices
Nothing is sent while the box is starting up or still on its first checks, and on the very first
run everything already there counts as known (no flood of old news). State lives in /config,
which is never served.
"""
import json, os, time, urllib.parse, urllib.request

STATE = os.path.join(os.environ.get("LANPULSE_CONFIG_DIR", "/config"), "alert-state.json")
SKIP = {"net-airspace", "net-utilization", "net-devices"}     # informational, or handled below
WARN_TOO = {"net-dhcp"}                                         # amber here is worth a push
SILENCE = os.environ.get("LANPULSE_ALERTS", "1") in ("0", "false", "no")


def _load():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return None


def _save(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE)


def _send(targets, title, body, tags, priority="default"):
    for t in targets:
        try:
            req = urllib.request.Request(f"{t['server']}/{urllib.parse.quote(t['topic'])}", data=body.encode(),
                                         method="POST", headers={"Title": title, "Tags": tags, "Priority": priority})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            print(f"alerts: couldn't send to ntfy: {e}", flush=True)
            return False
    return True


def _label(d):
    who = d.get("name") or (f"probably {d['hint']}" if d.get("hint") else None) or d.get("vendor") or "unknown device"
    return f"{who} ({d.get('ip') or '?'}, {(d.get('mac') or '').upper()})"


def check(snap, targets, site=""):
    """Compare this snapshot with the last one and push what changed. targets: active ntfy topics."""
    if SILENCE or snap.get("checking"):
        return
    targets = [t for t in targets if t.get("active") and not t.get("login")]
    prev = _load()
    items = [i for g in snap.get("groups", []) for i in g.get("items", []) if i.get("kind") == "netcheck"]
    bad = {i["id"]: i for i in items if i["id"] not in SKIP and
           (i.get("status") == "down" or (i.get("status") == "warn" and i["id"] in WARN_TOO))}
    devs = (snap.get("extras") or {}).get("devices") or []
    new = {d["mac"].upper(): d for d in devs if d.get("new") and d.get("mac") and not d.get("this_box")}
    state = json.loads(json.dumps(prev)) if prev else {"bad": {}, "seen_new": [], "since": time.time()}
    where = f" · {site}" if site else ""
    if prev is not None and targets:
        for k, i in bad.items():
            if k not in state["bad"]:
                if _send(targets, f"Network problem: {i['name']}{where}", i.get("message") or i.get("note") or "",
                         "warning", "high"):
                    state["bad"][k] = {"t": time.time(), "name": i["name"]}
        for k in list(state["bad"]):
            cur = next((i for i in items if i["id"] == k), None)
            if k not in bad and cur and cur.get("status") in ("up", "warn"):
                mins = round((time.time() - state["bad"][k]["t"]) / 60)
                if _send(targets, f"Back to normal: {state['bad'][k]['name']}{where}",
                         f"{cur.get('message') or 'Working again'} (problem lasted about {mins} min)", "white_check_mark"):
                    del state["bad"][k]
        fresh = [d for m, d in new.items() if m not in state["seen_new"]]
        if fresh:
            body = "\n".join(_label(d) for d in fresh[:10]) + (f"\n…and {len(fresh) - 10} more" if len(fresh) > 10 else "")
            if _send(targets, f"New device{'s' if len(fresh) > 1 else ''} on the network{where}", body, "new"):
                state["seen_new"] += [d["mac"].upper() for d in fresh]
    else:                                   # first run, or nowhere to send: just learn the current state
        state["bad"] = {k: {"t": time.time(), "name": i["name"]} for k, i in bad.items()} if prev is None else state["bad"]
        state["seen_new"] = sorted(set(state["seen_new"]) | set(new))
    state["seen_new"] = state["seen_new"][-500:]
    if state != prev:
        _save(state)
