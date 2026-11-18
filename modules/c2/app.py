"""Command & Control module.

Rules:      {match: "face.match", min_severity: "high", actions: [...]} evaluated on every event.
Actions:    {"do": "incident"}                                  open an incident
            {"do": "command", "module": "videowall", "command": "push_camera",
             "args": {"wall": "Main wall", "tile": 0, "camera_id": "$camera_id"}}
Incidents:  open -> acknowledged -> closed, with notes and an audit trail.
"""
import fnmatch
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel

from ellalon_sdk import EllalonModule

STORE = Path("/data/c2.json")
SEV = {"info": 0, "low": 1, "warning": 2, "high": 3, "critical": 4}
DEFAULT_RULES = {
    "face-watchlist": {"match": "face.match", "min_severity": "high", "enabled": True,
                       "actions": [{"do": "incident"},
                                   {"do": "command", "module": "videowall", "command": "push_camera",
                                    "args": {"wall": "Main wall", "tile": 0, "camera_id": "$camera_id"}}]},
    "external-alarms": {"match": "*.alarm", "min_severity": "warning", "enabled": True,
                        "actions": [{"do": "incident"}]},
}

m = EllalonModule(version="0.1.0")
app = m.app
db = json.loads(STORE.read_text()) if STORE.exists() else {"rules": DEFAULT_RULES, "incidents": {}}


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


@m.background
async def rule_engine():
    ps = m.redis.pubsub()
    await ps.subscribe("ellalon.events")
    async for msg in ps.listen():
        if msg["type"] != "message":
            continue
        ev = json.loads(msg["data"])
        if ev.get("source") == m.name:
            continue
        for name, r in db["rules"].items():
            if not r.get("enabled") or not fnmatch.fnmatch(ev.get("type", ""), r["match"]):
                continue
            if SEV.get(ev.get("severity"), 0) < SEV.get(r.get("min_severity", "info"), 0):
                continue
            for a in r["actions"]:
                try:
                    if a["do"] == "incident":
                        await open_incident(ev, name)
                    elif a["do"] == "command":
                        await m.send_command(a["module"], a["command"], subst(a.get("args", {}), ev))
                except Exception as e:
                    m.log.warning("rule %s action failed: %s", name, e)


# ---------------------------------------------------------------- API
class Rule(BaseModel):
    match: str
    min_severity: str = "info"
    enabled: bool = True
    actions: list[dict]


@app.get("/rules")
def rules():
    return db["rules"]


@app.put("/rules/{name}")
def put_rule(name: str, r: Rule):
    db["rules"][name] = r.model_dump()
    save()
    return db["rules"][name]


@app.delete("/rules/{name}")
def del_rule(name: str):
    db["rules"].pop(name, None)
    save()
    return {"ok": True}


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
