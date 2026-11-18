"""What the assistant can do.

READ tools run right away (they only look). PROPOSE tools never change anything: they return a
proposal that the user sees as a card with Approve / Reject - the browser then carries it out with
the user's own permissions. SCREEN tools (open a tab, show a layout, play a recording) run in the
user's browser right away because they change nothing in the system.
"""
import fnmatch
import json
import os
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

CORE = os.environ.get("ELLALON_CORE_URL", "http://ellalon-core:8000")
TOKEN = os.environ.get("ELLALON_MODULE_TOKEN", "")
SEV = {"info": 0, "low": 1, "warning": 2, "high": 3, "critical": 4}
MAX_RESULT = 24000


async def core(path, **params):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(CORE + path, params={k: v for k, v in params.items() if v is not None},
                        headers={"Authorization": f"Bearer {TOKEN}"})
        r.raise_for_status()
        return r.json()


async def modget(module, path, **params):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"http://{module}:8000{path}", params={k: v for k, v in params.items() if v is not None},
                        headers={"x-ellalon-role": "module"})
        r.raise_for_status()
        return r.json()


async def safe(coro, default=None):
    try:
        return await coro
    except Exception:
        return default


def obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


S, I, N, B = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}
ARR = lambda t: {"type": "array", "items": t}

RULE_SCHEMA = obj({
    "name": S, "enabled": B,
    "trigger": obj({"events": ARR(S), "min_severity": {"type": "string", "enum": list(SEV)},
                    "cameras": ARR(I), "devices": ARR(S), "text": S}),
    "conditions": obj({"days": ARR(I), "time_from": S, "time_to": S, "cooldown": N, "count": I, "window": N}),
    "actions": ARR(obj({"do": {"type": "string", "enum": ["alarm", "wall", "device", "notify", "event", "webhook"]},
                        "severity": S, "title": S, "text": S, "device_id": S, "device_name": S, "action": S,
                        "wall": S, "tile": I, "camera": S, "count": I, "type": S, "url": S, "method": S, "body": S})),
}, ["name", "trigger", "actions"])

TOOLS = [
    # ---------------- read
    {"name": "get_system_status", "description": "Health of the whole system: modules (running/stopped), cameras online/offline/recording, storage, integrations (Apollo, RISCO) connection, open alarms. Use for 'is everything OK', status questions and dashboards.",
     "input_schema": obj({})},
    {"name": "list_cameras", "description": "Cameras with id, name, location, online status, recording, analytics, people tagging, face recognition. Optional text filter.",
     "input_schema": obj({"query": S})},
    {"name": "list_devices", "description": "Integration devices (doors/readers, inputs/zones, outputs/relays, partitions) with id, name, kind, system, live state, linked cameras, and the actions each supports.",
     "input_schema": obj({"query": S, "kind": S})},
    {"name": "list_alarms", "description": "Alarms in the Alarm manager (incidents). status: open (default = not closed), acknowledged, closed or all.",
     "input_schema": obj({"status": S, "limit": I})},
    {"name": "search_events", "description": "Events (everything that happened): newest first. Filter by hours back, type pattern (fnmatch, e.g. 'apollo.*' or '*.alarm'), camera id, minimum severity, text.",
     "input_schema": obj({"hours": N, "type": S, "camera_id": I, "min_severity": S, "text": S, "limit": I})},
    {"name": "event_statistics", "description": "Counts of events over a period, grouped by type, camera, severity, day, hour or source. For reports and 'how many / when' questions.",
     "input_schema": obj({"hours": N, "group_by": {"type": "string", "enum": ["type", "camera", "severity", "day", "hour", "source", "device"]},
                          "type": S, "min_severity": S}, ["group_by"])},
    {"name": "list_rules", "description": "The automation rules (When - Only if - Then) that exist now.", "input_schema": obj({})},
    {"name": "get_rule_options", "description": "Event types a rule can react to (catalog) and the rule format - call before proposing rules.",
     "input_schema": obj({})},
    {"name": "list_maps", "description": "Maps (floor plans) and what is placed on them.", "input_schema": obj({})},
    {"name": "list_layouts", "description": "Saved video-wall layouts (name, grid, cameras).", "input_schema": obj({})},
    {"name": "search_people", "description": "People seen live by cameras with people tagging, filtered by appearance tags (call get_people_tags for the tag list), cameras and hours back.",
     "input_schema": obj({"tags": ARR(S), "camera_ids": ARR(I), "hours": N, "limit": I})},
    {"name": "get_people_tags", "description": "Appearance tags available for people search (man/woman, clothes and colours, bags, hats, glasses...).",
     "input_schema": obj({})},
    # ---------------- propose (need the user's approval)
    {"name": "propose_rules", "description": "Show the user one or more NEW rules as a demo card (with how often each would have fired recently). Nothing is created until the user approves. Use camera ids and device ids from list_cameras / list_devices.",
     "input_schema": obj({"rules": ARR(RULE_SCHEMA), "explanation": S}, ["rules"])},
    {"name": "propose_map", "description": "Show the user a NEW map built from an attached floor plan (file_id + page) with cameras/devices placed at x,y (fractions 0..1 of the page picture, from the top-left). Nothing is created until the user approves. type: camera (ref = camera id) | door | input | output (ref = device id) | label (text only, for things not in Ellalon). angle = camera direction in degrees (0 = up, 90 = right).",
     "input_schema": obj({"name": S, "file_id": S, "page": I, "explanation": S,
                          "items": ARR(obj({"type": S, "ref": S, "x": N, "y": N, "angle": N, "label": S}, ["type", "x", "y"]))},
                         ["name", "file_id", "items"])},
    {"name": "propose_commands", "description": "Ask the user to approve control commands: open/lock doors, door modes, relays, arm/disarm. Each: device_id + action (one the device supports, see list_devices). Nothing happens until the user approves.",
     "input_schema": obj({"commands": ARR(obj({"device_id": S, "action": S}, ["device_id", "action"])), "explanation": S}, ["commands"])},
    # ---------------- screen (immediate, change nothing)
    {"name": "show_on_screen", "description": "Show something on the user's screen now: open a tab (wall, incidents, events, fastpb, playback, cameras, rules, maps, analytics, faces, integrations, storage, modules, dashboard), show a saved video-wall layout, show a camera live, or play a recording at a time (unix seconds).",
     "input_schema": obj({"what": {"type": "string", "enum": ["open_tab", "show_layout", "show_camera", "play_recording", "open_map"]},
                          "tab": S, "layout": S, "camera_id": I, "time": N, "map_id": S}, ["what"])},
    {"name": "create_report", "description": "Write a report the user can read, print or save as PDF. Gather the numbers first with the read tools. body = markdown text; tables and simple bar/line charts optional.",
     "input_schema": obj({"title": S, "period": S, "body": S,
                          "tables": ARR(obj({"title": S, "columns": ARR(S), "rows": ARR(ARR(S))})),
                          "charts": ARR(obj({"title": S, "type": {"type": "string", "enum": ["bar", "line"]}, "labels": ARR(S),
                                             "series": ARR(obj({"name": S, "values": ARR(N)}))}))}, ["title", "body"])},
]
PROPOSE = {"propose_rules", "propose_map", "propose_commands"}


def clip(x) -> str:
    s = json.dumps(x, ensure_ascii=False, default=str)
    return s if len(s) <= MAX_RESULT else s[:MAX_RESULT] + ' ... (cut - ask for less, e.g. with a filter)'


def since_iso(hours) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=float(hours or 24))).isoformat()


async def events(hours=24, limit=5000):
    return await core("/api/events", since=since_iso(hours), limit=limit)


def ev_text(ev):
    return (ev.get("type", "") + " " + json.dumps(ev.get("payload") or {}, ensure_ascii=False, default=str)).lower()


def ev_matches(t: dict, ev: dict) -> bool:
    """Same test as the rules engine (C&C) uses for a rule's 'When'."""
    pats = [x for x in t.get("events") or [] if x.strip()]
    if pats and not any(fnmatch.fnmatch(ev.get("type", ""), p.strip()) for p in pats):
        return False
    if SEV.get(ev.get("severity"), 0) < SEV.get(t.get("min_severity") or "info", 0):
        return False
    cams = [ev["camera_id"]] if ev.get("camera_id") else []
    cams += (ev.get("payload") or {}).get("cameras", [])
    if t.get("cameras") and not set(t["cameras"]) & set(cams):
        return False
    if t.get("devices") and (ev.get("payload") or {}).get("device_id") not in t["devices"]:
        return False
    if t.get("text") and t["text"].strip().lower() not in ev_text(ev):
        return False
    return True


def in_schedule(c: dict, when: datetime, tz) -> bool:
    days, frm, to = c.get("days") or [], c.get("time_from") or "", c.get("time_to") or ""
    now = when.astimezone(tz)
    hm = now.strftime("%H:%M")
    day = now.weekday()
    if frm and to:
        if not (frm <= hm < to if frm < to else (hm >= frm or hm < to)):
            return False
        if frm > to and hm < to:
            day = (day - 1) % 7
    return not days or day in days


def simulate(rule: dict, evs: list[dict], tz) -> dict:
    """How often the rule would have fired on these events (newest first in `evs`)."""
    c, fired, last, recent = rule.get("conditions") or {}, [], float("-inf"), []
    for ev in reversed(evs):
        if not ev_matches(rule.get("trigger") or {}, ev):
            continue
        when = datetime.fromisoformat(ev["ts"])
        if not in_schedule(c, when, tz):
            continue
        t = when.timestamp()
        n, win = int(c.get("count") or 0), float(c.get("window") or 0)
        if n > 1 and win > 0:
            recent = [x for x in recent + [t] if t - x <= win]
            if len(recent) < n:
                continue
            recent = []
        if t - last < float(c.get("cooldown") or 0):
            continue
        last = t
        fired.append(ev)
    ex = [{"time": e["ts"], "type": e["type"], "title": (e.get("payload") or {}).get("title")} for e in fired[-3:]]
    return {"would_fire": len(fired), "examples": ex}


class Context:
    """Per request: who asks, their time zone, and things collected for the page."""

    def __init__(self, user: str, role: str, tz: str):
        self.user, self.role, self.tz = user, role, tz
        try:
            self.zone = ZoneInfo(tz)
        except Exception:
            self.zone = timezone.utc
        self.proposals: list[dict] = []
        self.screen: list[dict] = []
        self.reports: list[dict] = []
        self.files: dict = {}


async def run(name: str, args: dict, ctx: Context, store) -> str:
    """Execute one tool; returns the text the model sees."""
    a = args or {}
    if name == "get_system_status":
        mods = await safe(core("/api/modules"), [])
        cams = await safe(core("/api/cameras"), [])
        stor = await safe(core("/api/storage"), [])
        systems = await safe(modget("integrations", "/systems"), [])
        inc = await safe(modget("c2", "/incidents"), [])
        st = Counter((c.get("status") or {}).get("state", "unknown") for c in cams)
        return clip({
            "modules": [{"name": x.get("display_name") or x["name"], "enabled": x.get("enabled"), "healthy": x.get("healthy"),
                         "state": (x.get("container") or {}).get("state")} for x in mods],
            "cameras": {"total": len(cams), "by_state": dict(st), "recording": sum(1 for c in cams if c.get("record")),
                        "offline": [c["name"] for c in cams if (c.get("status") or {}).get("state") != "online"][:30]},
            "storage": [{"name": s.get("name"), "type": s.get("type"), "status": s.get("status"), "free_gb": s.get("free_gb"),
                         "used_gb": s.get("used_gb"), "cameras": s.get("cameras")} for s in stor],
            "integrations": [{"name": s.get("name"), "type": s.get("type"), "status": s.get("status"), "devices": s.get("devices"),
                              "error": s.get("error")} for s in systems],
            "alarms": {"open": sum(1 for i in inc if i.get("status") == "open"),
                       "acknowledged": sum(1 for i in inc if i.get("status") == "acknowledged")}})
    if name == "list_cameras":
        cams = await core("/api/cameras")
        q = (a.get("query") or "").lower()
        return clip([{"id": c["id"], "name": c["name"], "location": c.get("location"), "status": (c.get("status") or {}).get("state"),
                      "recording": c.get("record"), "analytics": c.get("analytics"),
                      "people_tagging": (c.get("analytics_tagging") or {}).get("enabled"), "face_recognition": c.get("face_recognition"),
                      "enabled": c.get("enabled")}
                     for c in cams if not q or q in (c["name"] + " " + (c.get("location") or "")).lower()])
    if name == "list_devices":
        devs = await safe(modget("integrations", "/devices"), None)
        if devs is None:
            return "The integrations module is not running."
        types = await safe(modget("integrations", "/types"), {})
        systems = {s["id"]: s for s in await safe(modget("integrations", "/systems"), [])}
        q, kind = (a.get("query") or "").lower(), a.get("kind")
        out = []
        for d in devs:
            if kind and d["kind"] != kind or q and q not in d["name"].lower():
                continue
            sy = systems.get(d["system_id"], {})
            out.append({"id": d["id"], "name": d["name"], "kind": d["kind"], "system": sy.get("name"),
                        "state": (d.get("state") or {}).get("state") or (d.get("state") or {}).get("last_event"),
                        "cameras": d.get("cameras"), "actions": (types.get(sy.get("type"), {}).get("actions") or {}).get(d["kind"], [])})
        return clip(out)
    if name == "list_alarms":
        inc = await safe(modget("c2", "/incidents"), None)
        if inc is None:
            return "The C&C module (Alarm manager) is not running."
        st = a.get("status") or "open"
        inc = [i for i in inc if st == "all" or (i["status"] != "closed" if st == "open" else i["status"] == st)]
        return clip([{"id": i["id"], "title": i.get("title"), "status": i["status"], "severity": i.get("severity"),
                      "created": i.get("created"), "count": i.get("count"), "camera_id": i.get("camera_id")} for i in inc[: int(a.get("limit") or 50)]])
    if name == "search_events":
        evs = await events(a.get("hours") or 24, 5000)
        out = []
        for e in evs:
            if a.get("type") and not fnmatch.fnmatch(e["type"], a["type"]):
                continue
            if a.get("camera_id") and e.get("camera_id") != a["camera_id"]:
                continue
            if a.get("min_severity") and SEV.get(e["severity"], 0) < SEV.get(a["min_severity"], 0):
                continue
            if a.get("text") and a["text"].lower() not in ev_text(e):
                continue
            p = e.get("payload") or {}
            out.append({"time": e["ts"], "type": e["type"], "severity": e["severity"], "camera_id": e.get("camera_id"),
                        "title": p.get("title") or p.get("text"), "device": p.get("device") or p.get("device_name")})
            if len(out) >= int(a.get("limit") or 60):
                break
        return clip({"found": len(out), "events": out})
    if name == "event_statistics":
        hours = float(a.get("hours") or 24)
        evs = await events(hours, 20000)
        cams = {c["id"]: c["name"] for c in await safe(core("/api/cameras"), [])}
        g = a["group_by"]
        cnt = Counter()
        for e in evs:
            if a.get("type") and not fnmatch.fnmatch(e["type"], a["type"]):
                continue
            if a.get("min_severity") and SEV.get(e["severity"], 0) < SEV.get(a["min_severity"], 0):
                continue
            when = datetime.fromisoformat(e["ts"]).astimezone(ctx.zone)
            key = {"type": e["type"], "severity": e["severity"], "source": e.get("source"),
                   "camera": cams.get(e.get("camera_id"), "no camera" if not e.get("camera_id") else f"#{e['camera_id']}"),
                   "day": when.strftime("%Y-%m-%d"), "hour": when.strftime("%H:00"),
                   "device": (e.get("payload") or {}).get("device") or (e.get("payload") or {}).get("device_name") or "-"}[g]
            cnt[key] += 1
        items = sorted(cnt.items()) if g in ("day", "hour") else cnt.most_common(60)
        return clip({"period_hours": hours, "total": sum(cnt.values()), "group_by": g, "counts": dict(items),
                     "note": "events older than the period, or beyond 20000, are not counted" if len(evs) >= 20000 else ""})
    if name == "list_rules":
        rules = await safe(modget("c2", "/rules"), None)
        return "The C&C module is not running." if rules is None else clip(rules)
    if name == "get_rule_options":
        cat = await safe(modget("c2", "/catalog"), [])
        return clip({"event_types": cat, "format": RULE_HELP})
    if name == "list_maps":
        maps = await safe(core("/api/maps"), [])
        return clip([{"id": m["id"], "name": m["name"], "has_picture": bool(m.get("image")),
                      "items": [{"type": i["type"], "ref": i.get("ref"), "label": i.get("label")} for i in m.get("items", [])]} for m in maps])
    if name == "list_layouts":
        lays = await safe(modget("videowall", "/layouts"), [])
        return clip([{"name": l.get("name"), "cols": l.get("cols"), "cameras": l.get("tiles")} for l in lays])
    if name == "search_people":
        params = [("hours", None)]
        q = {"since": (datetime.now(timezone.utc) - timedelta(hours=float(a.get("hours") or 24))).timestamp(),
             "limit": int(a.get("limit") or 40)}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get("http://analytics:8000/sightings", params=[*q.items(), *[("tag", t) for t in a.get("tags") or []],
                                                                            *[("camera", x) for x in a.get("camera_ids") or []]])
                r.raise_for_status()
                rows = r.json()
        except Exception:
            return "The analytics module is not running (or people tagging is off)."
        del params
        return clip([{"time": datetime.fromtimestamp(x["ts"], ctx.zone).isoformat(timespec="seconds"), "ts": x["ts"],
                      "camera_id": x["camera_id"], "camera": x["camera"], "tags": x.get("labels") or x["tags"]} for x in rows])
    if name == "get_people_tags":
        t = await safe(modget("analytics", "/tags"), None)
        return "The analytics module is not running." if t is None else clip(t.get("groups"))
    if name in PROPOSE:
        return await propose(name, a, ctx, store)
    if name == "show_on_screen":
        ctx.screen.append({k: v for k, v in a.items() if v is not None})
        return "Done - it is on the user's screen now."
    if name == "create_report":
        rid = uuid.uuid4().hex[:10]
        rep = {"id": rid, "title": a.get("title") or "Report", "period": a.get("period") or "", "body": a.get("body") or "",
               "tables": a.get("tables") or [], "charts": a.get("charts") or [], "created": datetime.now(timezone.utc).isoformat(),
               "user": ctx.user}
        store.save_report(rep)
        ctx.reports.append({"id": rid, "title": rep["title"]})
        return f"Report {rid} created and shown to the user (they can print it or save it as PDF)."
    return f"Unknown tool {name}"


RULE_HELP = """A rule: {name, enabled, trigger, conditions, actions}.
trigger: events = list of event-type patterns (fnmatch: 'apollo.door_forced', 'analytics.*', '*.alarm'); min_severity (info|low|warning|high|critical);
  cameras = camera ids (empty = any); devices = integration device ids (empty = any); text = words that must appear in the event.
conditions (all optional): days = 0 Monday ... 6 Sunday (empty = every day); time_from/time_to 'HH:MM' (22:00-06:00 crosses midnight, both or neither);
  cooldown = seconds between firings; count + window = at least <count> matching events within <window> seconds.
actions (do = ...):
  alarm   {severity: event|warning|high|critical, title?}  -> opens an alarm in the Alarm manager
  wall    {wall: 'Main wall', tile: 0, camera: 'event' or a camera id, count: 1} -> shows the camera on the video wall
  device  {device_id, device_name, action} -> e.g. lock a door, pulse a relay, door mode (action from list_devices)
  notify  {title, text, severity} -> pop-up for operators ({title} {camera} {device} {type} are filled in)
  event   {type, text, severity} -> writes an event
  webhook {url, method, body}
Event types come from get_rule_options (event_types) and from search_events."""


async def propose(name: str, a: dict, ctx: Context, store) -> str:
    pid = "P" + uuid.uuid4().hex[:6]
    if name == "propose_rules":
        rules = a.get("rules") or []
        if not rules:
            return "No rules given."
        evs = await safe(events(24 * 7, 20000), [])
        devs = {d["id"]: d for d in await safe(modget("integrations", "/devices"), [])}
        cams = {c["id"] for c in await safe(core("/api/cameras"), [])}
        problems, demo = [], []
        for r in rules:
            r.setdefault("enabled", True)
            r.setdefault("conditions", {})
            r["conditions"]["tz"] = ctx.tz
            for cid in (r.get("trigger") or {}).get("cameras") or []:
                if cid not in cams:
                    problems.append(f"{r.get('name')}: camera id {cid} does not exist")
            for act in r.get("actions") or []:
                if act.get("do") == "device":
                    d = devs.get(act.get("device_id"))
                    if not d:
                        problems.append(f"{r.get('name')}: device {act.get('device_id')} does not exist")
                    else:
                        act.setdefault("device_name", d["name"])
            if not r.get("actions"):
                problems.append(f"{r.get('name')}: no actions")
            demo.append(simulate(r, evs, ctx.zone))
        if problems:
            return "Not shown - fix these first: " + "; ".join(problems)
        p = {"id": pid, "kind": "rules", "rules": rules, "demo": demo, "explanation": a.get("explanation") or "", "status": "pending"}
        ctx.proposals.append(p)
        return (f"Proposal {pid} shown to the user as a demo card with Approve/Reject. Over the last 7 days the rules would have "
                f"fired: {[d['would_fire'] for d in demo]} times. Do NOT say they are created - wait for the user.")
    if name == "propose_map":
        f = store.file(a.get("file_id") or "")
        if not f:
            return "Unknown file_id - the user must attach the floor plan (PDF or picture) first."
        page = int(a.get("page") or 1)
        if page < 1 or page > len(f["pages"]):
            return f"That file has pages 1..{len(f['pages'])}."
        cams = {str(c["id"]): c["name"] for c in await safe(core("/api/cameras"), [])}
        devs = {d["id"]: d for d in await safe(modget("integrations", "/devices"), [])}
        items, unknown = [], []
        for it in a.get("items") or []:
            t, ref = it.get("type"), str(it.get("ref") or "")
            x, y = min(1, max(0, float(it.get("x", 0)))), min(1, max(0, float(it.get("y", 0))))
            if t == "camera" and ref not in cams or t in ("door", "input", "output") and ref not in devs:
                unknown.append(it.get("label") or ref)
                t, ref = "label", None
            items.append({"id": uuid.uuid4().hex[:10], "type": t if t in ("camera", "door", "input", "output", "label") else "label",
                          "ref": ref or None, "x": x, "y": y, "angle": float(it.get("angle") or 0), "fov": 70,
                          "range": 0.12 if t == "camera" else 0, "size": 1, "label": it.get("label") or None, "show_label": True})
        p = {"id": pid, "kind": "map", "name": a.get("name") or f["name"], "file_id": f["id"], "page": page, "items": items,
             "image": f"/modules/assistant/files/{f['id']}/page/{page}.png", "explanation": a.get("explanation") or "",
             "unknown": unknown, "status": "pending"}
        ctx.proposals.append(p)
        extra = f" {len(unknown)} item(s) did not match anything in Ellalon and were placed as text labels: {unknown}." if unknown else ""
        return f"Proposal {pid} (map with {len(items)} items) shown to the user for approval.{extra} Wait for the user."
    if name == "propose_commands":
        devs = {d["id"]: d for d in await safe(modget("integrations", "/devices"), [])}
        types = await safe(modget("integrations", "/types"), {})
        systems = {s["id"]: s for s in await safe(modget("integrations", "/systems"), [])}
        cmds, problems = [], []
        for c in a.get("commands") or []:
            d = devs.get(c.get("device_id"))
            if not d:
                problems.append(f"device {c.get('device_id')} does not exist")
                continue
            acts = (types.get(systems.get(d["system_id"], {}).get("type"), {}).get("actions") or {}).get(d["kind"], [])
            if c.get("action") not in acts:
                problems.append(f"{d['name']} cannot '{c.get('action')}' (possible: {acts})")
                continue
            cmds.append({"device_id": d["id"], "device_name": d["name"], "kind": d["kind"], "action": c["action"]})
        if problems:
            return "Not shown - " + "; ".join(problems)
        p = {"id": pid, "kind": "commands", "commands": cmds, "explanation": a.get("explanation") or "", "status": "pending"}
        ctx.proposals.append(p)
        return f"Proposal {pid} shown to the user with Approve/Reject. Nothing was done yet - wait for the user."
    return "?"
