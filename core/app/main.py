"""Ellalon Core - the main manager container.

Responsibilities
  * Module lifecycle: discover manifests, start/stop/restart containers, reconcile desired state
  * Health: heartbeats (SDK modules) or HTTP probes (3rd-party images)
  * Identity: users, roles, JWT for clients, service tokens for modules
  * Shared data: cameras, events
  * Event hub: persists bus events and streams them to clients over WebSocket
  * C&C: relays commands from operators to modules
Every client (web, Windows, mobile, 3rd-party) uses the same REST + WebSocket API.
"""
import asyncio
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import (ROLE_RANK, decode, ensure_admin, hash_password, issue_token, require,
                   verify_password)
from .bus import bus, event_dict
from .camera_events import events_manager
from .cameras import camera_monitor, hooks as camera_hooks, router as cameras_router
from .config import settings
from .db import Camera, Event, ModuleState, SessionLocal, User, init_db
from .docker_manager import DockerManager, cname
from .registry import registry
from .recorders import recorder_monitor, router as recorders_router
from .maps import router as maps_router
from . import storage as storage_mod
from .storage import ensure_default as ensure_default_storage, load_storages

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("core")

docker_mgr: DockerManager | None = None
http_health: dict[str, bool] = {}
last_error: dict[str, str] = {}


# ---------------------------------------------------------------- lifecycle
def sync_module_states():
    """Create a desired-state row for any newly discovered module (autostart from manifest)."""
    with SessionLocal() as s:
        for name, spec in registry.modules.items():
            if not s.get(ModuleState, name):
                s.add(ModuleState(name=name, enabled=spec.autostart, config={}))
        s.commit()


def desired_states() -> dict[str, ModuleState]:
    with SessionLocal() as s:
        return {m.name: m for m in s.query(ModuleState).all()}


async def reconcile_once():
    states = desired_states()
    enabled = [n for n, st in states.items() if st.enabled and n in registry.modules]
    for name in registry.start_order(enabled):
        spec = registry.modules[name]
        try:
            st = await asyncio.to_thread(docker_mgr.status, name)
            if st["state"] != "running":
                log.info("Reconcile: starting %s (was %s)", name, st["state"])
                await asyncio.to_thread(docker_mgr.start, spec, states[name].config, storages_for(spec))
                await bus.emit("core", "module.started", {"module": name})
            last_error.pop(name, None)
        except Exception as e:
            if last_error.get(name) != str(e):
                await bus.emit("core", "module.error", {"module": name, "error": str(e)}, "warning")
            last_error[name] = str(e)
        if spec.health.type == "http":
            port = spec.health.port or spec.http_port
            try:
                async with httpx.AsyncClient(timeout=3) as c:
                    r = await c.get(f"http://{cname(name)}:{port}{spec.health.path}")
                    http_health[name] = r.status_code < 500
            except Exception:
                http_health[name] = False
    for name, st in states.items():
        if not st.enabled:
            await asyncio.to_thread(docker_mgr.stop, name)


def storages_for(spec):
    return load_storages() if spec.storage_mounts else None


async def apply_storage():
    """Storage settings changed: recreate modules that mount storages (reconciler restarts them)."""
    for name, spec in registry.modules.items():
        if spec.storage_mounts:
            await asyncio.to_thread(docker_mgr.remove, name)
    await bus.broadcast("storage.changed", {})
    asyncio.create_task(reconcile_once())


async def reconciler():
    while True:
        try:
            await reconcile_once()
        except Exception as e:
            log.exception("Reconcile loop error: %s", e)
        await asyncio.sleep(settings.reconcile_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global docker_mgr
    init_db()
    ensure_default_storage()
    ensure_admin()
    registry.load()
    sync_module_states()
    docker_mgr = DockerManager()
    docker_mgr.ensure_network()
    storage_mod.hooks.update(apply=apply_storage, test=docker_mgr.test_storage)
    camera_hooks["discover"] = docker_mgr.discover
    tasks = [asyncio.create_task(reconciler()), asyncio.create_task(bus.consume()),
             asyncio.create_task(camera_monitor()), asyncio.create_task(recorder_monitor()),
             asyncio.create_task(events_manager())]
    log.info("Ellalon core %s started", settings.version)
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Ellalon Core API", version=settings.version, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------- auth & users
class LoginIn(BaseModel):
    username: str
    password: str


class UserIn(BaseModel):
    username: str
    password: str
    role: str = "operator"


@app.post("/api/auth/login")
def login(body: LoginIn, response: Response):
    with SessionLocal() as s:
        u = s.query(User).filter_by(username=body.username).first()
        if not u or not verify_password(body.password, u.password_hash):
            raise HTTPException(401, "Bad credentials")
        token = issue_token(u.username, u.role)
        # Cookie lets browser tiles (video <video>, iframes, WebSockets) pass module forward-auth
        response.set_cookie("ellalon_token", token, httponly=True, samesite="strict",
                            max_age=settings.jwt_ttl_minutes * 60)
        return {"token": token, "role": u.role, "username": u.username}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("ellalon_token")
    return {"ok": True}


@app.get("/api/auth/verify")
def verify(request: Request):
    """Traefik forwardAuth target for every /modules/* route.
    Accepts Bearer header or ellalon_token cookie. Reads need viewer, writes need operator."""
    h = request.headers
    token = h.get("authorization", "").removeprefix("Bearer ").strip() or \
        request.cookies.get("ellalon_token", "")
    if not token:
        raise HTTPException(401, "Missing token")
    p = decode(token)
    method = h.get("x-forwarded-method", "GET").upper()
    need = "viewer" if method in ("GET", "HEAD", "OPTIONS") else "operator"
    if p.get("role") != "module" and ROLE_RANK.get(p.get("role"), 0) < ROLE_RANK[need]:
        raise HTTPException(403, "Insufficient role")
    return Response(status_code=200, headers={"X-Ellalon-User": p["sub"],
                                              "X-Ellalon-Role": p["role"]})


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/auth/password")
def change_password(body: PasswordIn, p=Depends(require("viewer"))):
    """Signed-in user changes their own password."""
    if len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    with SessionLocal() as s:
        u = s.query(User).filter_by(username=p["sub"]).first()
        if not u or not verify_password(body.current_password, u.password_hash):
            raise HTTPException(400, "Current password is incorrect")
        u.password_hash = hash_password(body.new_password)
        s.commit()
    return {"ok": True}


@app.get("/api/auth/me")
def me(p=Depends(require("viewer"))):
    return p


@app.get("/api/users")
def list_users(_=Depends(require("admin"))):
    with SessionLocal() as s:
        return [{"id": u.id, "username": u.username, "role": u.role} for u in s.query(User)]


@app.post("/api/users")
def create_user(body: UserIn, _=Depends(require("admin"))):
    if body.role not in ("admin", "operator", "viewer"):
        raise HTTPException(400, "Invalid role")
    with SessionLocal() as s:
        if s.query(User).filter_by(username=body.username).first():
            raise HTTPException(409, "User exists")
        s.add(User(username=body.username, password_hash=hash_password(body.password), role=body.role))
        s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- system / modules
BUILD = (Path(__file__).with_name("BUILD").read_text().strip() if Path(__file__).with_name("BUILD").is_file() else "")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": settings.version, "build": BUILD}


async def module_view(name: str) -> dict:
    spec = registry.modules[name]
    st = desired_states().get(name)
    container = await asyncio.to_thread(docker_mgr.status, name)
    if spec.health.type == "heartbeat":
        hb = await bus.heartbeat(name)
        healthy = hb is not None
    elif spec.health.type == "http":
        hb, healthy = None, http_health.get(name, False)
    else:
        hb, healthy = None, container["state"] == "running"
    return {**spec.model_dump(exclude={"env"}), "enabled": bool(st and st.enabled),
            "container": container, "healthy": healthy and container["state"] == "running",
            "heartbeat": hb, "error": last_error.get(name),
            "url": f"/modules/{name}/" if spec.http_port else None}


def get_spec(name: str):
    if name not in registry.modules:
        raise HTTPException(404, "Unknown module")
    return registry.modules[name]


@app.get("/api/modules")
async def list_modules(_=Depends(require("viewer", allow_module=True))):
    return [await module_view(n) for n in registry.modules]


@app.get("/api/modules/{name}")
async def get_module(name: str, _=Depends(require("viewer"))):
    get_spec(name)
    return await module_view(name)


def set_enabled(name: str, enabled: bool):
    with SessionLocal() as s:
        st = s.get(ModuleState, name) or ModuleState(name=name, config={})
        st.enabled = enabled
        s.merge(st)
        s.commit()


@app.post("/api/modules/{name}/enable")
async def enable_module(name: str, _=Depends(require("admin"))):
    spec = get_spec(name)
    for dep in registry.start_order([name]):   # enabling a module enables its dependencies
        set_enabled(dep, True)
    try:
        for dep in registry.start_order([name]):
            await asyncio.to_thread(docker_mgr.start, registry.modules[dep], desired_states()[dep].config,
                                    storages_for(registry.modules[dep]))
    except Exception as e:
        raise HTTPException(500, str(e))
    await bus.emit("core", "module.enabled", {"module": spec.name})
    return await module_view(name)


@app.post("/api/modules/{name}/disable")
async def disable_module(name: str, _=Depends(require("admin"))):
    get_spec(name)
    set_enabled(name, False)
    await asyncio.to_thread(docker_mgr.stop, name)
    await bus.emit("core", "module.disabled", {"module": name})
    return await module_view(name)


@app.post("/api/modules/{name}/restart")
async def restart_module(name: str, _=Depends(require("operator"))):
    get_spec(name)
    await asyncio.to_thread(docker_mgr.restart, name)
    return {"ok": True}


@app.post("/api/modules/{name}/recreate")
async def recreate_module(name: str, _=Depends(require("admin"))):
    """Remove + create again (after an image upgrade or config change)."""
    spec = get_spec(name)
    await asyncio.to_thread(docker_mgr.remove, name)
    await asyncio.to_thread(docker_mgr.start, spec, desired_states()[name].config, storages_for(spec))
    return {"ok": True}


class ConfigIn(BaseModel):
    env: dict[str, str]


@app.put("/api/modules/{name}/config")
def put_module_config(name: str, body: ConfigIn, _=Depends(require("admin"))):
    get_spec(name)
    with SessionLocal() as s:
        st = s.get(ModuleState, name)
        st.config = body.env
        s.commit()
    return {"ok": True, "note": "POST /recreate to apply"}


@app.get("/api/modules/{name}/logs")
async def module_logs(name: str, tail: int = 200, _=Depends(require("operator"))):
    get_spec(name)
    return {"logs": await asyncio.to_thread(docker_mgr.logs, name, tail)}


@app.post("/api/modules/reload")
def reload_manifests(_=Depends(require("admin"))):
    registry.load()
    sync_module_states()
    return {"modules": list(registry.modules)}


# ---------------------------------------------------------------- cameras
# brand templates, ONVIF discovery, batch add and copy-settings live in cameras.py
app.include_router(cameras_router)
app.include_router(storage_mod.router)
app.include_router(recorders_router)
app.include_router(maps_router)


# ---------------------------------------------------------------- events & C&C
@app.get("/api/events")
def list_events(limit: int = 100, source: str | None = None, type: str | None = None,
                camera_id: int | None = None, since: datetime | None = None, until: datetime | None = None,
                p=Depends(require("viewer", allow_module=True))):
    cap = 20000 if p.get("role") == "module" else 1000      # modules build statistics and reports
    with SessionLocal() as s:
        q = s.query(Event)
        if since:
            q = q.filter(Event.ts >= since)
        if until:
            q = q.filter(Event.ts <= until)
        if source:
            q = q.filter(Event.source == source)
        if type:
            q = q.filter(Event.type == type)
        if camera_id:
            q = q.filter(Event.camera_id == camera_id)
        return [event_dict(e) for e in q.order_by(Event.id.desc()).limit(min(limit, cap))]


class CommandIn(BaseModel):
    command: str
    args: dict = {}


@app.post("/api/commands/{module}")
async def send_command(module: str, body: CommandIn, p=Depends(require("operator", allow_module=True))):
    get_spec(module)
    cid = await bus.command(module, body.command, body.args, p["sub"])
    await bus.emit("core", "command.sent", {"id": cid, "module": module, "command": body.command,
                                            "by": p["sub"]})
    return {"id": cid}


@app.websocket("/api/ws")
async def ws(websocket: WebSocket):
    try:
        decode(websocket.query_params.get("token", "") or websocket.cookies.get("ellalon_token", ""))
    except HTTPException:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    bus.clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / future client->server messages
    except WebSocketDisconnect:
        pass
    finally:
        bus.clients.discard(websocket)


# ---------------------------------------------------------------- web client
if Path(settings.web_dir).is_dir():
    class WebFiles(StaticFiles):
        """Browsers must re-check the web client each time (cheap: unchanged files answer 304),
        otherwise after an update a cached old app.js can run against the new page."""
        async def get_response(self, path, scope):
            r = await super().get_response(path, scope)
            r.headers["Cache-Control"] = "no-cache"
            return r
    app.mount("/", WebFiles(directory=settings.web_dir, html=True), name="web")
