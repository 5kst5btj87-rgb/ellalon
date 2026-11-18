"""Integrations module: 3rd-party security systems (RISCO intrusion panels, Apollo access control).

* Systems: one adapter per connected system (adapters.py), restarted automatically on failure.
* Devices: partitions, zones, doors... imported from each system, with live state.
* Camera links: each device can be linked to cameras. On alarm the linked cameras are attached to
  the event (so C&C incidents and the web client show them) and pushed to the video wall.
* Control: arm / stay / disarm, bypass, unlock - from the UI or as C&C commands
  (command "action", args {device_id, action}).
"""
import asyncio
import json
import uuid
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel

from adapters import ADAPTERS, apollo_test
from ellalon_sdk import EllalonModule

STORE = Path("/data/integrations.json")
m = EllalonModule(version="0.1.0")
app = m.app


class Store:
    def __init__(self):
        d = json.loads(STORE.read_text()) if STORE.exists() else {}
        self.systems: dict[str, dict] = d.get("systems", {})
        self.devices: dict[str, dict] = d.get("devices", {})

    def save(self):
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps({"systems": self.systems, "devices": self.devices}, indent=1,
                                    default=str))


store = Store()
running: dict[str, "object"] = {}


def did_of(sid, kind, number) -> str:
    return f"{sid}:{kind}:{number}"


class Hub:
    """What adapters use to talk to the platform."""

    def device(self, sid, kind, number) -> dict:
        return store.devices.get(did_of(sid, kind, number), {})

    def upsert(self, sid, kind, number, name=None, state=None) -> dict:
        did = did_of(sid, kind, number)
        d = store.devices.get(did) or {"id": did, "system_id": sid, "kind": kind,
                                       "number": number, "cameras": [], "state": {}}
        d["name"] = name or d.get("name") or f"{kind.title()} {number}"
        if state is not None:
            d["state"] = {**d.get("state", {}), **state}
        store.devices[did] = d
        return d

    def save(self):
        store.save()

    def status(self, sid, status, error=""):
        s = store.systems.get(sid)
        if s is not None:
            s["status"], s["error"] = status, (error or "")[:300]
        m.status["systems"] = {k: v.get("status") for k, v in store.systems.items()}

    def cameras_of_active_zones(self, sid) -> list[int]:
        cams = []
        for d in store.devices.values():
            st = d.get("state", {})
            if d["system_id"] == sid and d["kind"] == "zone" and (st.get("alarmed") or st.get("triggered")):
                cams += [c for c in d.get("cameras", []) if c not in cams]
        return cams

    async def event(self, sid, kind, number, type_, severity="info", text="", alarm=False,
                    extra=None, extra_cameras=None):
        system = store.systems.get(sid, {})
        dev = self.device(sid, kind, number)
        cams = list(dev.get("cameras", []))
        cams += [c for c in (extra_cameras or []) if c not in cams]
        payload = {"system": system.get("name", sid), "system_id": sid,
                   "device": dev.get("name") or system.get("name", sid), "device_id": dev.get("id"),
                   "kind": kind, "number": number, "text": text, "cameras": cams, **(extra or {})}
        await m.publish(f"{system.get('type', 'integration')}.{type_}", payload, severity,
                        cams[0] if cams else None)
        if alarm and cams:
            for i, cid in enumerate(cams[:4]):
                try:
                    await m.send_command("videowall", "push_camera",
                                         {"wall": "Main wall", "tile": i, "camera_id": cid})
                except Exception as e:
                    m.log.warning("video wall push failed: %s", e)


hub = Hub()


async def start_system(sid: str):
    await stop_system(sid)
    system = store.systems[sid]
    if not system.get("enabled", True):
        hub.status(sid, "disabled")
        return
    adapter = ADAPTERS[system["type"]](system, hub)
    running[sid] = adapter
    adapter.task = asyncio.create_task(adapter.run_forever())


async def stop_system(sid: str):
    a = running.pop(sid, None)
    if a:
        await a.stop()


@m.on_startup
async def _boot():
    for sid in list(store.systems):
        await start_system(sid)


def need_admin(request: Request):
    if request.headers.get("x-ellalon-role") not in ("admin", "module"):
        raise HTTPException(403, "Only admins can change integrations")


def public_system(s: dict) -> dict:
    fields = ADAPTERS[s["type"]].fields
    secret = {f["key"] for f in fields if f.get("type") == "password"}
    cfg = {k: ("" if k in secret else v) for k, v in s.get("config", {}).items()}
    cfg.update({f"has_{k}": bool(s.get("config", {}).get(k)) for k in secret})
    n = sum(1 for d in store.devices.values() if d["system_id"] == s["id"])
    return {**s, "config": cfg, "devices": n,
            "inbound_url": f"/modules/integrations/inbound/{s['id']}" if s["type"] == "apollo" else None}


def get_device(did: str) -> tuple[dict, object]:
    d = store.devices.get(did)
    if not d:
        raise HTTPException(404, "Unknown device")
    a = running.get(d["system_id"])
    if not a:
        raise HTTPException(409, "System is disabled")
    return d, a


async def do_action(did: str, action: str, by: str):
    d, a = get_device(did)
    try:
        await a.action(d, action)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"{e.__class__.__name__}: {e}")
    await m.publish("integration.command", {"device": d["name"], "device_id": did,
                                            "action": action, "by": by})
    return {"ok": True}


# ---------------------------------------------------------------- API
class SystemIn(BaseModel):
    type: str
    name: str
    enabled: bool = True
    config: dict = {}


@app.get("/types")
def types():
    return {k: {"label": a.label, "fields": a.fields, "actions": a.actions} for k, a in ADAPTERS.items()}


@app.get("/systems")
def list_systems():
    return [public_system(s) for s in store.systems.values()]


@app.post("/systems")
async def add_system(body: SystemIn, request: Request):
    need_admin(request)
    if body.type not in ADAPTERS:
        raise HTTPException(400, "Unknown system type")
    sid = f"{body.type}-{uuid.uuid4().hex[:5]}"
    store.systems[sid] = {"id": sid, **body.model_dump(), "status": "new", "error": ""}
    store.save()
    await start_system(sid)
    return public_system(store.systems[sid])


@app.put("/systems/{sid}")
async def update_system(sid: str, body: SystemIn, request: Request):
    need_admin(request)
    s = store.systems.get(sid)
    if not s:
        raise HTTPException(404)
    secret = {f["key"] for f in ADAPTERS[s["type"]].fields if f.get("type") == "password"}
    cfg = {k: v for k, v in body.config.items() if not k.startswith("has_")}
    for k in secret:  # empty password field = keep the stored one
        if not cfg.get(k):
            cfg[k] = s["config"].get(k, "")
    s.update(name=body.name, enabled=body.enabled, config=cfg)
    store.save()
    await start_system(sid)
    return public_system(s)


@app.delete("/systems/{sid}")
async def delete_system(sid: str, request: Request):
    need_admin(request)
    await stop_system(sid)
    store.systems.pop(sid, None)
    for did in [k for k, d in store.devices.items() if d["system_id"] == sid]:
        store.devices.pop(did)
    store.save()
    return {"ok": True}


@app.post("/systems/{sid}/reconnect")
async def reconnect(sid: str):
    if sid not in store.systems:
        raise HTTPException(404)
    await start_system(sid)
    return {"ok": True}


class ImportIn(BaseModel):
    csv: str | None = None
    items: list[dict] = []          # chosen devices: {kind, id, name}


@app.post("/systems/{sid}/import")
async def import_devices(sid: str, body: ImportIn):
    a = running.get(sid)
    if not a:
        raise HTTPException(409, "System is disabled")
    try:
        n = await (a.import_devices(body.csv, body.items) if body.items else a.import_devices(body.csv))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"{e.__class__.__name__}: {e}")
    return {"imported": n}


class ApolloTestIn(BaseModel):
    config: dict = {}
    system_id: str | None = None    # editing: empty password = the saved one


@app.post("/apollo/test")
async def apollo_test_api(body: ApolloTestIn, request: Request):
    """Test the APACS connection and list its doors / inputs / outputs to choose from."""
    need_admin(request)
    cfg = {k: v for k, v in body.config.items() if v not in (None, "")}
    saved = store.systems.get(body.system_id or "")
    if saved and not cfg.get("password"):
        cfg["password"] = saved["config"].get("password", "")
    try:
        res = await asyncio.wait_for(apollo_test(cfg), 60)
    except asyncio.TimeoutError:
        return {"ok": False, "steps": [{"ok": False, "text": "No answer within 60 seconds"}], "devices": [], "found": {}}
    known = {(d["kind"], str(d["number"])): d.get("name") for d in store.devices.values()
             if body.system_id and d["system_id"] == body.system_id}
    for d in res["devices"]:
        d["imported"] = (d["kind"], d["id"]) in known
        d["current_name"] = known.get((d["kind"], d["id"]))     # name in Ellalon (may be outdated)
    return res


@app.get("/devices")
def list_devices(system_id: str | None = None):
    out = [d for d in store.devices.values() if not system_id or d["system_id"] == system_id]
    return sorted(out, key=lambda d: (d["system_id"], d["kind"], str(d["number"]).zfill(6)))


class CamerasIn(BaseModel):
    cameras: list[int]


@app.put("/devices/{did}/cameras")
def link_cameras(did: str, body: CamerasIn):
    d = store.devices.get(did)
    if not d:
        raise HTTPException(404)
    d["cameras"] = body.cameras
    store.save()
    return d


class ActionIn(BaseModel):
    action: str


@app.post("/devices/{did}/action")
async def device_action(did: str, body: ActionIn, request: Request):
    return await do_action(did, body.action, request.headers.get("x-ellalon-user", "unknown"))


@app.post("/inbound/{sid}")
async def inbound(sid: str, request: Request):
    """Push events from the 3rd-party system (or middleware) into Ellalon."""
    a = running.get(sid)
    if not a:
        raise HTTPException(404, "Unknown or disabled system")
    body = await request.json()
    try:
        for ev in body if isinstance(body, list) else [body]:
            await a.handle_inbound(ev)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@m.command("action")
async def cmd_action(args):
    """C&C / automation: {"device_id": "...", "action": "disarm"}"""
    return await do_action(args["device_id"], args["action"], "automation")
