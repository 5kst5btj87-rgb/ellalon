"""Command & Control module: the rule engine (Rules tab) and the Alarm manager (incidents).

A rule:  When  <trigger>   event types (wildcards), minimum priority, cameras, devices, text
         Only if            days / time window, wait between firings, "N times within S seconds"
         Then  <actions>    alarm, video wall, device control, pop-up, webhook, event, raw module command
Incidents (alarms): open -> acknowledged -> closed, with notes and an audit trail.
"""
import asyncio
import fnmatch
import json
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
import httpx

from ellalon_sdk import EllalonModule

STORE = Path("/data/c2.json")
SEV = {"info": 0, "low": 1, "warning": 2, "high": 3, "critical": 4}
DEFAULT_RULES = {
    "external-alarms": {
        "id": "external-alarms", "name": "Alarms from cameras, analytics and integrations", "enabled": True,
        "trigger": {"events": ["*.alarm"], "min_severity": "warning"},
        "conditions": {}, "actions": [{"do": "alarm", "severity": "event"}]},
    "face-watchlist": {
        "id": "face-watchlist", "name": "Face on a watch list", "enabled": True,
        "trigger": {"events": ["face.match"], "min_severity": "high"},
        "conditions": {}, "actions": [{"do": "alarm", "severity": "event"}, {"do": "wall", "camera": "event", "tile": 0}]},
}


def migrate(rules: dict) -> dict:
    """Rules saved by older versions ({match, min_severity, actions:[{do: incident|command}]})."""
    out = {}
    for rid, r in rules.items():
        if "trigger" in r:
            out[rid] = r
            continue
        acts = []
        for a in r.get("actions", []):
            if a.get("do") == "incident":
                acts.append({"do": "alarm", "severity": "event"})
            elif a.get("do") == "command" and a.get("command") == "push_camera":
                acts.append({"do": "wall", "camera": "event", "tile": int((a.get("args") or {}).get("tile", 0))})
            else:
                acts.append(a)
        out[rid] = {"id": rid, "name": DEFAULT_RULES.get(rid, {}).get("name", rid), "enabled": r.get("enabled", True),
                    "trigger": {"events": [r.get("match", "*")], "min_severity": r.get("min_severity", "info")},
                    "conditions": {}, "actions": acts}
    return out


m = EllalonModule(version="0.2.0")
app = m.app
db = json.loads(STORE.read_text()) if STORE.exists() else {"rules": DEFAULT_RULES, "incidents": {}}
db["rules"] = migrate(db.get("rules", {}))
db.setdefault("seen", {})          # event types seen recently -> {count, last, example}


def save():
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(db, indent=2, default=str))


def now():
    return datetime.now(timezone.utc).isoformat()


def subst(args: dict, ev: dict) -> dict:
    return {k: ev.get(v[1:], ev.get("payload", {}).get(v[1:])) if isinstance(v, str) and v.startswith("$")
            else v for k, v in args.items()}


REPEAT_WINDOW = 300     # seconds: the same alarm again while still open -> count it, don't open a new one
MAX_INCIDENTS = 5000


def _age(ts: str) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except (TypeError, ValueError):
        return 1e9


async def open_incident(ev: dict, rule: str):
    p = ev.get("payload") or {}
    key = [ev.get("camera_id"), ev.get("type"), p.get("event") or p.get("title") or ev.get("source")]
    for inc in db["incidents"].values():
        if inc["status"] != "closed" and inc.get("key") == key and _age(inc.get("last") or inc["created"]) < REPEAT_WINDOW:
            inc["count"] = inc.get("count", 1) + 1
            inc["last"] = now()
            inc["event"] = ev
            save()
            await m.publish("incident.updated", {"id": inc["id"], "count": inc["count"], "status": inc["status"]})
            return
    iid = uuid.uuid4().hex[:8]
    inc = {"id": iid, "status": "open", "rule": rule, "severity": ev.get("severity"),
           "title": p.get("title") or f"{ev.get('type')} from {ev.get('source')}", "camera_id": ev.get("camera_id"),
           "key": key, "count": 1, "last": now(),
           "event": ev, "created": now(), "log": [{"ts": now(), "by": "system", "msg": "opened"}]}
    db["incidents"][iid] = inc
    if len(db["incidents"]) > MAX_INCIDENTS:     # drop the oldest closed alarms
        old = sorted((i for i in db["incidents"].values() if i["status"] == "closed"), key=lambda i: i["created"])
        for i in old[:len(db["incidents"]) - MAX_INCIDENTS]:
            db["incidents"].pop(i["id"], None)
    save()
    m.status["open_incidents"] = sum(i["status"] != "closed" for i in db["incidents"].values())
    await m.publish("incident.opened", {"id": iid, "title": inc["title"]}, ev.get("severity", "high"),
                    ev.get("camera_id"))


# ---------------------------------------------------------------- rule engine
def ev_text(ev: dict) -> str:
    return (ev.get("type", "") + " " + json.dumps(ev.get("payload") or {}, ensure_ascii=False, default=str)).lower()


def ev_cameras(ev: dict) -> list[int]:
    cams = [ev["camera_id"]] if ev.get("camera_id") else []
    return cams + [c for c in (ev.get("payload") or {}).get("cameras", []) if c not in cams]


def render(text: str, ev: dict) -> str:
    """{type} {severity} {camera} {device} {text} {title} {source} and any payload field."""
    p = ev.get("payload") or {}
    vals = {"type": ev.get("type", ""), "severity": ev.get("severity", ""), "source": ev.get("source", ""),
            "camera": p.get("camera") or "",
            "device": p.get("device", ""), "text": p.get("text") or p.get("title") or p.get("label") or "",
            "title": p.get("title") or p.get("text") or ev.get("type", "")}
    return re.sub(r"\{(\w+)\}", lambda m_: str(vals.get(m_.group(1), p.get(m_.group(1), m_.group(0)))), text or "")


fired_at: dict[str, float] = {}
recent: dict[str, deque] = {}


def matches(r: dict, ev: dict) -> bool:
    t = r.get("trigger") or {}
    pats = [x for x in t.get("events") or [] if x.strip()]
    if pats and not any(fnmatch.fnmatch(ev.get("type", ""), p.strip()) for p in pats):
        return False
    if SEV.get(ev.get("severity"), 0) < SEV.get(t.get("min_severity") or "info", 0):
        return False
    if t.get("cameras") and not set(t["cameras"]) & set(ev_cameras(ev)):
        return False
    if t.get("devices") and (ev.get("payload") or {}).get("device_id") not in t["devices"]:
        return False
    if t.get("text") and t["text"].strip().lower() not in ev_text(ev):
        return False
    return True


def in_schedule(c: dict, now: datetime | None = None) -> bool:
    days, frm, to = c.get("days") or [], c.get("time_from") or "", c.get("time_to") or ""
    if not days and not (frm and to):
        return True
    try:
        tz = ZoneInfo(c.get("tz") or "UTC")
    except Exception:
        tz = timezone.utc
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    hm = now.strftime("%H:%M")
    if frm and to:
        inside = frm <= hm < to if frm < to else (hm >= frm or hm < to)   # 22:00-06:00 crosses midnight
        if not inside:
            return False
        day = now.weekday() if not (frm > to and hm < to) else (now.weekday() - 1) % 7   # night belongs to its start day
    else:
        day = now.weekday()
    return not days or day in days


def conditions_ok(r: dict, ev: dict, now: float) -> bool:
    c = r.get("conditions") or {}
    if not in_schedule(c):
        return False
    n, window = int(c.get("count") or 0), float(c.get("window") or 0)
    if n > 1 and window > 0:                     # "at least N times within S seconds"
        q = recent.setdefault(r["id"], deque())
        q.append(now)
        while q and now - q[0] > window:
            q.popleft()
        if len(q) < n:
            return False
        q.clear()
    if now - fired_at.get(r["id"], float("-inf")) < float(c.get("cooldown") or 0):
        return False
    return True


async def run_actions(r: dict, ev: dict) -> list[str]:
    results = []
    for a in r.get("actions") or []:
        kind = a.get("do")
        try:
            if kind == "alarm":
                sev = ev.get("severity") if a.get("severity") in (None, "", "event") else a["severity"]
                if SEV.get(sev, 0) < SEV["warning"]:
                    sev = "warning"
                p = dict(ev.get("payload") or {})
                if a.get("title"):
                    p["title"] = render(a["title"], ev)
                p.setdefault("title", f"{r.get('name')}: {ev.get('type')}")
                await open_incident({**ev, "severity": sev, "payload": p}, r["id"])
                results.append(f"alarm ({sev})")
            elif kind == "wall":
                cams = ev_cameras(ev) if a.get("camera", "event") == "event" else [int(a["camera"])]
                for i, cid in enumerate(cams[: int(a.get("count") or 1)]):
                    await m.send_command("videowall", "push_camera", {"wall": a.get("wall") or "Main wall",
                                                                      "tile": int(a.get("tile") or 0) + i, "camera_id": cid})
                results.append(f"video wall: {cams[: int(a.get('count') or 1)] or 'no camera in the event'}")
            elif kind == "device":
                await m.send_command("integrations", "action", {"device_id": a["device_id"], "action": a["action"]})
                results.append(f"{a['action']} {a.get('device_name') or a['device_id']}")
            elif kind == "notify":
                await m.publish("rule.notify", {"title": render(a.get("title") or r.get("name", ""), ev),
                                                "text": render(a.get("text") or "{title}", ev), "rule": r.get("name")},
                                a.get("severity") or "warning", ev.get("camera_id"))
                results.append("pop-up")
            elif kind == "event":
                await m.publish(a.get("type") or "rule.event", {"title": render(a.get("text") or r.get("name", ""), ev),
                                                                "rule": r.get("name"), "trigger": ev.get("type")},
                                a.get("severity") or "info", ev.get("camera_id"))
                results.append("event logged")
            elif kind == "webhook":
                body = render(a["body"], ev) if a.get("body") else json.dumps({"rule": r.get("name"), "event": ev}, default=str)
                async with httpx.AsyncClient(timeout=10, verify=False) as c:
                    resp = await c.request(a.get("method") or "POST", render(a["url"], ev), content=body,
                                           headers={"Content-Type": "application/json"})
                results.append(f"webhook HTTP {resp.status_code}")
            elif kind == "command":
                await m.send_command(a["module"], a["command"], subst(a.get("args") or {}, ev))
                results.append(f"{a['module']}.{a['command']}")
            elif kind == "incident":                      # old name of "alarm"
                await open_incident(ev, r["id"])
                results.append("alarm")
        except Exception as e:
            m.log.warning("rule %s action %s failed: %s", r.get("name"), kind, e)
            results.append(f"{kind} FAILED: {e}")
    return results


def remember(ev: dict):
    t = ev.get("type", "")
    if not t or t.startswith(("incident.", "rule.", "wall.", "command.")):
        return
    s_ = db["seen"].setdefault(t, {"count": 0})
    s_.update(count=s_["count"] + 1, last=now(), severity=ev.get("severity"),
              example=(ev.get("payload") or {}).get("title") or (ev.get("payload") or {}).get("text") or "")


@m.background
async def rule_engine():
    ps = m.redis.pubsub()
    await ps.subscribe("ellalon.events")
    last_save = time.time()
    async for msg in ps.listen():
        if msg["type"] != "message":
            continue
        ev = json.loads(msg["data"])
        if ev.get("source") == m.name:
            continue
        remember(ev)
        t = time.time()
        for r in list(db["rules"].values()):
            if not r.get("enabled") or not matches(r, ev) or not conditions_ok(r, ev, t):
                continue
            fired_at[r["id"]] = t
            res = await run_actions(r, ev)
            st = r.setdefault("stats", {"fired": 0})
            st.update(fired=st.get("fired", 0) + 1, last=now(), last_event=ev.get("type"), last_result=res)
            last_save = 0
        if time.time() - last_save > 30:
            save()
            last_save = time.time()


# ---------------------------------------------------------------- API
def need_admin(request: Request):
    if request.headers.get("x-ellalon-role") not in ("admin", "module"):
        raise HTTPException(403, "Only admins can change rules")


class Trigger(BaseModel):
    events: list[str] = []
    min_severity: str = "info"
    cameras: list[int] = []
    devices: list[str] = []
    text: str = ""


class Conditions(BaseModel):
    days: list[int] = []            # 0 = Monday ... 6 = Sunday; empty = every day
    time_from: str = ""             # "22:00"
    time_to: str = ""               # "06:00"
    tz: str = "UTC"                 # the browser's time zone, sent by the Rules tab
    cooldown: float = 0
    count: int = 0                  # at least <count> matching events ...
    window: float = 0               # ... within <window> seconds


class RuleIn(BaseModel):
    name: str
    enabled: bool = True
    trigger: Trigger = Field(default_factory=Trigger)
    conditions: Conditions = Field(default_factory=Conditions)
    actions: list[dict] = []


ACTIONS = {"alarm", "wall", "device", "notify", "event", "webhook", "command", "incident"}


def check(r: RuleIn) -> dict:
    r.name = r.name.strip()
    if not r.name:
        raise HTTPException(400, "Give the rule a name")
    if not r.actions:
        raise HTTPException(400, "Add at least one action")
    for a in r.actions:
        if a.get("do") not in ACTIONS:
            raise HTTPException(400, f"Unknown action {a.get('do')}")
        if a["do"] == "device" and not (a.get("device_id") and a.get("action")):
            raise HTTPException(400, "Choose the device and what to do with it")
        if a["do"] == "webhook" and not str(a.get("url", "")).startswith(("http://", "https://")):
            raise HTTPException(400, "The webhook needs an http:// or https:// address")
        if a["do"] == "command" and not (a.get("module") and a.get("command")):
            raise HTTPException(400, "A module command needs the module and the command")
    for hm in (r.conditions.time_from, r.conditions.time_to):
        if hm and not re.fullmatch(r"[0-2]\d:[0-5]\d", hm):
            raise HTTPException(400, f"Time {hm} must look like 22:00")
    if bool(r.conditions.time_from) != bool(r.conditions.time_to):
        raise HTTPException(400, "Give both a start and an end time (or neither)")
    return r.model_dump()


@app.get("/rules")
def rules():
    return sorted(db["rules"].values(), key=lambda r: r.get("name", "").lower())


@app.post("/rules")
def add_rule(r: RuleIn, request: Request):
    need_admin(request)
    rid = uuid.uuid4().hex[:8]
    db["rules"][rid] = {"id": rid, **check(r), "stats": {"fired": 0}}
    save()
    return db["rules"][rid]


@app.put("/rules/{rid}")
def put_rule(rid: str, r: RuleIn, request: Request):
    need_admin(request)
    old = db["rules"].get(rid, {})
    db["rules"][rid] = {"id": rid, **check(r), "stats": old.get("stats", {"fired": 0})}
    save()
    return db["rules"][rid]


@app.delete("/rules/{rid}")
def del_rule(rid: str, request: Request):
    need_admin(request)
    db["rules"].pop(rid, None)
    save()
    return {"ok": True}


@app.post("/rules/{rid}/test")
async def test_rule(rid: str, request: Request):
    """Run the rule's actions now with a sample event (conditions are not checked)."""
    need_admin(request)
    r = db["rules"].get(rid)
    if not r:
        raise HTTPException(404)
    pats = [p for p in (r.get("trigger") or {}).get("events", []) if p and "*" not in p and "?" not in p]
    cams = (r.get("trigger") or {}).get("cameras") or []
    ev = {"source": "rules-test", "type": pats[0] if pats else "rule.test", "severity": "high",
          "camera_id": cams[0] if cams else None,
          "payload": {"title": f"TEST of rule '{r['name']}'", "text": "Test run from the Rules tab", "test": True}}
    return {"results": await run_actions(r, ev)}


CATALOG = [  # (group, type, label)
    ("Cameras", "camera.alarm", "Camera detection sent to the Alarm manager"),
    ("Cameras", "camera.analytics", "Camera detection (events only)"),
    ("Cameras", "camera.offline", "Camera lost connection"),
    ("Cameras", "camera.online", "Camera connected again"),
    ("Video analytics", "analytics.alarm", "Analytics rule raised an alarm"),
    ("Video analytics", "analytics.intrusion", "Intrusion (events only)"),
    ("Video analytics", "analytics.line", "Line crossing (events only)"),
    ("Video analytics", "analytics.loitering", "Loitering (events only)"),
    ("Video analytics", "analytics.motion", "Motion (events only)"),
    ("Face recognition", "face.match", "Face matched a watch list"),
    ("Access control (Apollo)", "apollo.door_forced", "Door forced open"),
    ("Access control (Apollo)", "apollo.door_held", "Door held open"),
    ("Access control (Apollo)", "apollo.access_denied", "Access denied"),
    ("Access control (Apollo)", "apollo.access_granted", "Access granted"),
    ("Access control (Apollo)", "apollo.alarm", "Alarm (input / other)"),
    ("Access control (Apollo)", "apollo.tamper", "Tamper"),
    ("Access control (Apollo)", "apollo.offline", "Controller / device offline"),
    ("Intrusion (RISCO)", "risco.alarm", "Alarm"),
    ("Intrusion (RISCO)", "risco.armed", "Armed"),
    ("Intrusion (RISCO)", "risco.disarmed", "Disarmed"),
    ("Intrusion (RISCO)", "risco.connection_lost", "Panel connection lost"),
    ("Recording", "recorder.offline", "Recording server offline"),
    ("Recording", "recording.stopped", "Camera stopped recording"),
    ("System", "module.error", "Module error"),
    ("Any", "*.alarm", "Any alarm"),
]


@app.get("/catalog")
def catalog():
    """Event types to choose from: the known ones plus every type seen on this system."""
    known = {t for _, t, _ in CATALOG}
    seen = [{"group": "Seen on this system", "type": t, "label": (v.get("example") or "")[:60], "count": v.get("count", 0),
             "last": v.get("last")} for t, v in sorted(db["seen"].items()) if t not in known]
    return [{"group": g, "type": t, "label": l, "count": db["seen"].get(t, {}).get("count", 0),
             "last": db["seen"].get(t, {}).get("last")} for g, t, l in CATALOG] + seen


@app.get("/incidents")
def incidents(status: str | None = None):
    out = [i for i in db["incidents"].values() if not status or i["status"] == status]
    return sorted(out, key=lambda i: i["created"], reverse=True)


class IncidentUpdate(BaseModel):
    status: str | None = None     # acknowledged | closed
    note: str | None = None


class BulkUpdate(BaseModel):
    ids: list[str] = []
    status: str                   # acknowledged | closed
    note: str | None = None


@app.post("/incidents/bulk")
async def bulk_update(body: BulkUpdate, request: Request):
    """Acknowledge or close many alarms at once (empty ids = every alarm that is not closed)."""
    user = request.headers.get("x-ellalon-user", "unknown")
    done = []
    for inc in db["incidents"].values():
        if inc["status"] == "closed" or (body.ids and inc["id"] not in body.ids):
            continue
        if body.status == "acknowledged" and inc["status"] != "open":
            continue
        inc["status"] = body.status
        inc["log"].append({"ts": now(), "by": user, "msg": f"status -> {body.status}" + (f": {body.note}" if body.note else "")})
        done.append(inc["id"])
    save()
    m.status["open_incidents"] = sum(i["status"] != "closed" for i in db["incidents"].values())
    if done:
        await m.publish("incident.updated", {"ids": done, "status": body.status, "by": user})
    return {"updated": len(done)}


@app.patch("/incidents/{iid}")
async def update_incident(iid: str, body: IncidentUpdate, request: Request):
    inc = db["incidents"].get(iid)
    if not inc:
        raise HTTPException(404)
    user = request.headers.get("x-ellalon-user", "unknown")   # injected by forward-auth
    if body.status:
        inc["status"] = body.status
        inc["log"].append({"ts": now(), "by": user, "msg": f"status -> {body.status}"})
    if body.note:
        inc["log"].append({"ts": now(), "by": user, "msg": body.note})
    save()
    await m.publish("incident.updated", {"id": iid, "status": inc["status"], "by": user})
    m.status["open_incidents"] = sum(i["status"] != "closed" for i in db["incidents"].values())
    return inc
