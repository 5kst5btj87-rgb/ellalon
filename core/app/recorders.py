"""Recording servers: Ellalon recorder agents on other Ubuntu / Windows machines.

A recording server is a storage of type "recorder" (Storage tab). It pairs once with a one-time
code, then by itself:
  * pulls its camera list   GET  /api/recorders/<id>/config  every 30 s, cached on the recorder
  * reports its state       POST /api/recorders/<id>/status  every 30 s
Cameras whose recording storage is that recorder are recorded there (not by the local VMS
recorder). The agent keeps recording from its cached list while this server is unreachable and
starts at boot. Playback goes through this server: /api/recorders/<id>/recordings and /files.
"""
import asyncio
import datetime
import re
import secrets

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from .auth import bearer, decode, require
from .bus import bus
from .config import settings
from .db import Camera, SessionLocal, Storage

router = APIRouter(prefix="/api")
STATUS: dict[str, dict] = {}        # recorder id -> last report (+ seen, address)
OFFLINE_AFTER = 90                  # seconds without a report
CODE_TTL = datetime.timedelta(hours=24)
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def new_code() -> str:
    c = "".join(secrets.choice(CODE_CHARS) for _ in range(8))
    return f"{c[:4]}-{c[4:]}"


def with_new_code(cfg: dict) -> dict:
    return {**cfg, "code": new_code(), "code_expires": (now() + CODE_TTL).isoformat()}


def _online(st: dict | None) -> bool:
    seen = (st or {}).get("seen")
    return bool(seen and (now() - datetime.datetime.fromisoformat(seen)).total_seconds() < OFFLINE_AFTER)


def recorder_view(s: Storage, admin: bool) -> dict:
    cfg, st = s.config or {}, STATUS.get(s.id, {})
    out = {"registered": bool(cfg.get("tid")), "online": _online(st),
           "last_seen": st.get("seen") or cfg.get("last_seen"), "hostname": cfg.get("hostname", ""),
           "os": st.get("os") or cfg.get("os", ""), "version": st.get("version") or cfg.get("version", ""),
           "address": st.get("address") or cfg.get("address", ""), "port": cfg.get("port", 8090),
           "folder": st.get("path", ""), "disk": st.get("disk", {}), "cameras": st.get("cameras", {})}
    if admin and cfg.get("code") and cfg.get("code_expires", "") > now().isoformat():
        out.update(code=cfg["code"], code_expires=cfg["code_expires"])
    return out


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or request.headers.get("x-real-ip", "") or (request.client.host if request.client else "")


def _token(sid: str, tid: str) -> str:
    exp = now() + datetime.timedelta(days=3650)
    return jwt.encode({"sub": f"recorder:{sid}", "role": "recorder", "tid": tid, "exp": exp},
                      settings.jwt_secret, algorithm="HS256")


def recorder_auth(sid: str, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    if not creds:
        raise HTTPException(401, "Missing token")
    p = decode(creds.credentials)
    if p.get("role") != "recorder" or p.get("sub") != f"recorder:{sid}":
        raise HTTPException(403, "Not this recorder")
    with SessionLocal() as s:
        x = s.get(Storage, sid)
        if not x or x.type != "recorder":
            raise HTTPException(410, "This recording server was removed from Ellalon")
        if (x.config or {}).get("tid") != p.get("tid"):
            raise HTTPException(410, "This recording server was paired again on another machine")
        return {"id": x.id, "name": x.name, "enabled": x.enabled, "config": dict(x.config or {}),
                "retention_days": x.retention_days, "min_free_gb": x.min_free_gb}


# ---------------------------------------------------------------- agent endpoints
class RegisterIn(BaseModel):
    code: str
    hostname: str = ""
    os: str = ""
    port: int = 8090
    version: str = ""


@router.post("/recorders/register")
def register(body: RegisterIn, request: Request):
    code = body.code.strip().upper()
    with SessionLocal() as s:
        for x in s.query(Storage).filter_by(type="recorder"):
            cfg = dict(x.config or {})
            if code and cfg.get("code") == code and cfg.get("code_expires", "") > now().isoformat():
                tid, secret = secrets.token_hex(8), secrets.token_urlsafe(24)
                cfg.update(code="", code_expires="", tid=tid, secret=secret, hostname=body.hostname[:80],
                           os=body.os[:80], port=body.port, version=body.version[:20],
                           address=client_ip(request), last_seen=now().isoformat())
                x.config = cfg
                s.commit()
                STATUS.pop(x.id, None)
                return {"id": x.id, "name": x.name, "token": _token(x.id, tid), "secret": secret,
                        "path": cfg.get("path", "")}
    raise HTTPException(403, "Invalid or expired pairing code - create a new one in the Storage tab")


@router.get("/recorders/{sid}/config")
def recorder_config(sid: str, x=Depends(recorder_auth)):
    with SessionLocal() as s:
        cams = [{"id": c.id, "name": c.name, "url": c.url} for c in s.query(Camera).order_by(Camera.id)
                if c.enabled and c.record and (c.meta or {}).get("storage_id") == sid]
    return {"name": x["name"], "path": x["config"].get("path", ""), "retention_days": x["retention_days"],
            "min_free_gb": x["min_free_gb"], "cameras": cams if x["enabled"] else []}


@router.post("/recorders/{sid}/status")
async def recorder_status(sid: str, request: Request, x=Depends(recorder_auth)):
    body = await request.json()
    prev, addr = STATUS.get(sid), client_ip(request)
    STATUS[sid] = {**body, "seen": now().isoformat(), "address": addr}
    if not _online(prev):
        await bus.emit("core", "recorder.online", {"recorder": x["name"], "address": addr}, "info")
    old_cams = (prev or {}).get("cameras") or {}
    for cid, c in (body.get("cameras") or {}).items():
        old, new = (old_cams.get(cid) or {}).get("state"), c.get("state")
        if old == "recording" and new == "failing":
            await bus.emit("core", "recording.stopped", {"camera": c.get("name"), "recorder": x["name"],
                                                         "error": c.get("error", "")}, "warning", int(cid))
        elif old == "failing" and new == "recording":
            await bus.emit("core", "recording.started", {"camera": c.get("name"), "recorder": x["name"]},
                           "info", int(cid))
    cfg = x["config"]
    if cfg.get("address") != addr or cfg.get("version") != body.get("version"):
        with SessionLocal() as s:
            row = s.get(Storage, sid)
            row.config = {**(row.config or {}), "address": addr, "version": body.get("version", ""),
                          "os": body.get("os", ""), "last_seen": now().isoformat()}
            s.commit()
    return {"ok": True}


async def recorder_monitor():
    """Raise an event when a recording server stops reporting."""
    flagged: set[str] = set()
    while True:
        await asyncio.sleep(30)
        try:
            with SessionLocal() as s:
                recs = [(x.id, x.name) for x in s.query(Storage).filter_by(type="recorder")
                        if (x.config or {}).get("tid")]
            for sid, name in recs:
                st = STATUS.get(sid)
                if st and not _online(st) and sid not in flagged:
                    flagged.add(sid)
                    await bus.emit("core", "recorder.offline",
                                   {"recorder": name, "last_seen": st.get("seen"),
                                    "note": "it keeps recording on its own"}, "warning")
                elif _online(st):
                    flagged.discard(sid)
        except Exception:
            pass


# ---------------------------------------------------------------- playback through this server
def _agent(sid: str) -> tuple[str, str]:
    with SessionLocal() as s:
        x = s.get(Storage, sid)
        if not x or x.type != "recorder":
            raise HTTPException(404, "Unknown recording server")
        cfg = x.config or {}
    addr = STATUS.get(sid, {}).get("address") or cfg.get("address")
    if not addr or not cfg.get("secret"):
        raise HTTPException(409, "The recording server has not connected yet")
    return f"http://{addr}:{cfg.get('port', 8090)}", cfg["secret"]


@router.get("/recorders/{sid}/recordings")
async def recorder_recordings(sid: str, camera: int, day: str | None = None, _=Depends(require("viewer"))):
    base, secret = _agent(sid)
    params = {"camera": camera, **({"day": day} if day else {})}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/recordings", params=params, headers={"Authorization": f"Bearer {secret}"})
    except httpx.HTTPError:
        raise HTTPException(502, "Cannot reach the recording server for playback (TCP port "
                                 f"{base.rsplit(':', 1)[-1]} must be open on it)")
    if r.status_code != 200:
        raise HTTPException(502, f"Recording server answered HTTP {r.status_code}")
    return [{**x, "storage": sid, "url": f"/api/recorders/{sid}/files/{camera}/{x['file']}"} for x in r.json()]


@router.get("/recorders/{sid}/files/{cid}/{name}")
async def recorder_file(sid: str, cid: int, name: str, request: Request, _=Depends(require("viewer"))):
    if not re.fullmatch(r"\d{8}-\d{6}\.mp4", name):
        raise HTTPException(404)
    base, secret = _agent(sid)
    headers = {"Authorization": f"Bearer {secret}"}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=None))
    try:
        r = await client.send(client.build_request("GET", f"{base}/files/{cid}/{name}", headers=headers),
                              stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(502, "Cannot reach the recording server")
    if r.status_code not in (200, 206):
        await r.aclose()
        await client.aclose()
        raise HTTPException(404 if r.status_code == 404 else 502)
    keep = {k: r.headers[k] for k in ("content-type", "content-length", "content-range", "accept-ranges")
            if k in r.headers}

    async def body():
        try:
            async for chunk in r.aiter_bytes():
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=r.status_code, headers=keep)
