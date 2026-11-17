"""Camera management: brand-aware stream URLs, ONVIF discovery, batch add, copy settings.

Cameras are described by structured settings (brand, ip, credentials, channel...). Core builds
the RTSP main/sub URLs from them, so every client (web, Windows, ...) shares the same logic.
"""
import asyncio
import base64
import datetime
import hashlib
import html
import os
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require
from .bus import bus
from .db import Camera, SessionLocal

router = APIRouter(prefix="/api")

# {ch} = channel number. "onvif": stream URLs are discovered from the device.
BRANDS = {
    "hikvision": {"label": "Hikvision", "main": "/Streaming/Channels/{ch}01",
                  "sub": "/Streaming/Channels/{ch}02"},
    "dahua": {"label": "Dahua", "main": "/cam/realmonitor?channel={ch}&subtype=0",
              "sub": "/cam/realmonitor?channel={ch}&subtype=1"},
    "axis": {"label": "Axis", "main": "/axis-media/media.amp?camera={ch}",
             "sub": "/axis-media/media.amp?camera={ch}&resolution=640x360"},
    "provision": {"label": "Provision-ISR", "main": "/chID={ch}&streamType=main&linkType=tcp",
                  "sub": "/chID={ch}&streamType=sub&linkType=tcp"},
    "flir": {"label": "FLIR (auto-detect via ONVIF)", "onvif": True},
    "onvif": {"label": "ONVIF (auto-detect)", "onvif": True},
    "rtsp": {"label": "Generic RTSP (custom path)", "custom": True},
}
STRUCT = ["brand", "ip", "port", "onvif_port", "username", "password", "channel",
          "main_path", "sub_path", "use_substream"]
COPY_GROUPS = {
    "brand": ["brand", "port", "onvif_port", "main_path", "sub_path"],
    "credentials": ["username", "password"],
    "substream": ["use_substream"],
    "recording": ["record"],
    "analytics": ["analytics"],
    "face_recognition": ["face_recognition"],
    "location": ["location"],
    "enabled": ["enabled"],
}


class CamSettings(BaseModel):
    brand: str = "hikvision"
    ip: str = ""
    port: int = 554
    onvif_port: int = 80
    username: str = ""
    password: str = ""
    channel: int = 1
    main_path: str = ""
    sub_path: str = ""
    use_substream: bool = True
    location: str | None = None
    record: bool = True
    analytics: bool = False
    face_recognition: bool = False
    enabled: bool = True


class CameraIn(CamSettings):
    name: str
    url: str | None = None            # optional raw URL override (API clients)
    substream_url: str | None = None


class BatchIn(CamSettings):
    name_prefix: str = "Camera"
    start_number: int = 1
    ips: list[str]
    channels: list[int] = [1]


class CopyIn(BaseModel):
    target_ids: list[int]
    fields: list[str]


# ---------------------------------------------------------------- URL building
def _cred(s: CamSettings) -> str:
    if not s.username:
        return ""
    return f"{quote(s.username, safe='')}:{quote(s.password, safe='')}@"


def _strip_creds(u: str | None) -> str | None:
    if not u:
        return u
    p = urlsplit(u)
    return urlunsplit((p.scheme, p.netloc.rsplit("@", 1)[-1], p.path, p.query, p.fragment))


def _template_url(s: CamSettings, template: str) -> str:
    path = template.replace("{ch}", str(s.channel))
    if not path.startswith("/"):
        path = "/" + path
    return f"rtsp://{_cred(s)}{s.ip}:{s.port}{path}"


def _onvif_uri(uri: str, s: CamSettings) -> str:
    """Use the stream path the camera reported, but our IP and credentials."""
    p = urlsplit(uri)
    return urlunsplit((p.scheme or "rtsp", f"{_cred(s)}{s.ip}:{p.port or 554}",
                       p.path, p.query, p.fragment))


async def build_urls(s: CamSettings) -> tuple[str, str | None]:
    b = BRANDS.get(s.brand)
    if not b:
        raise HTTPException(400, f"Unknown brand '{s.brand}'")
    s.ip = s.ip.strip()
    if not s.ip:
        raise HTTPException(400, "IP address is required")
    if b.get("onvif"):
        uris = await onvif_stream_uris(s.ip, s.onvif_port, s.username, s.password)
        main = _onvif_uri(uris[0], s)
        sub = _onvif_uri(uris[1], s) if len(uris) > 1 else None
    elif b.get("custom"):
        main = _template_url(s, s.main_path or "/")
        sub = _template_url(s, s.sub_path) if s.sub_path else None
    else:
        main, sub = _template_url(s, b["main"]), _template_url(s, b["sub"])
    return main, (sub if s.use_substream else None)


# ---------------------------------------------------------------- ONVIF
_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_PWD = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
_B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"


def _envelope(body: str, user: str, pw: str) -> str:
    hdr = ""
    if user:
        nonce = os.urandom(16)
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + pw.encode()).digest()).decode()
        hdr = (f'<s:Header><Security s:mustUnderstand="1" xmlns="{_WSSE}"><UsernameToken>'
               f'<Username>{escape(user)}</Username><Password Type="{_PWD}">{digest}</Password>'
               f'<Nonce EncodingType="{_B64}">{base64.b64encode(nonce).decode()}</Nonce>'
               f'<Created xmlns="{_WSU}">{created}</Created></UsernameToken></Security></s:Header>')
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">{hdr}'
            f'<s:Body>{body}</s:Body></s:Envelope>')


async def _soap(c: httpx.AsyncClient, url: str, body: str, user: str, pw: str) -> str:
    r = await c.post(url, content=_envelope(body, user, pw),
                     headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    if r.status_code >= 400:
        fault = re.search(r"<[^>]*Text[^>]*>([^<]+)<", r.text)
        reason = fault.group(1) if fault else f"HTTP {r.status_code}"
        if r.status_code == 401 or "uthoriz" in reason:
            reason = "wrong username or password"
        raise HTTPException(400, f"ONVIF: {reason}")
    return r.text


async def onvif_stream_uris(ip: str, port: int, user: str, pw: str) -> list[str]:
    """Return RTSP URIs of the first two media profiles (main, sub)."""
    uris = []
    try:
        async with httpx.AsyncClient(timeout=8, auth=httpx.DigestAuth(user, pw) if user else None) as c:
            caps = await _soap(c, f"http://{ip}:{port}/onvif/device_service",
                               '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl">'
                               '<Category>Media</Category></GetCapabilities>', user, pw)
            m = re.search(r"Media>.*?XAddr>([^<]+)<", caps, re.S)
            mp = urlsplit(m.group(1).strip() if m else "/onvif/media_service")
            media = urlunsplit(("http", f"{ip}:{port}", mp.path, mp.query, ""))
            profiles = await _soap(c, media, '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>',
                                   user, pw)
            for token in re.findall(r'<[^>]*Profiles[^>]*\stoken="([^"]+)"', profiles)[:2]:
                body = ('<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl"><StreamSetup>'
                        '<Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
                        '<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol>'
                        f'</Transport></StreamSetup><ProfileToken>{escape(token)}</ProfileToken>'
                        '</GetStreamUri>')
                u = re.search(r"<[^>]*Uri>([^<]+)<", await _soap(c, media, body, user, pw))
                if u:
                    uris.append(html.unescape(u.group(1).strip()))
    except httpx.HTTPError as e:
        raise HTTPException(400, f"Cannot reach ONVIF at {ip}:{port} ({e.__class__.__name__})")
    if not uris:
        raise HTTPException(400, f"No ONVIF stream profiles found on {ip}")
    return uris


# ---------------------------------------------------------------- helpers
def _pq(p) -> str:
    return p.path + (f"?{p.query}" if p.query else "")


def settings_of(c: Camera) -> CameraIn:
    """Structured settings of a stored camera (older raw-URL cameras are parsed)."""
    m = dict(c.meta or {})
    if not m.get("ip") and c.url:
        p = urlsplit(c.url)
        m.update(brand="rtsp", ip=p.hostname or "", port=p.port or 554,
                 username=unquote(p.username or ""), password=unquote(p.password or ""),
                 main_path=_pq(p), use_substream=bool(c.substream_url))
        if c.substream_url:
            m["sub_path"] = _pq(urlsplit(c.substream_url))
    return CameraIn(name=c.name, location=c.location, record=c.record, analytics=c.analytics,
                    face_recognition=c.face_recognition, enabled=c.enabled,
                    **{k: m[k] for k in STRUCT if k in m})


async def resolve(body: CameraIn) -> dict:
    if body.url:
        main, sub = body.url, body.substream_url
    else:
        main, sub = await build_urls(body)
    return dict(name=body.name, url=main, substream_url=sub, location=body.location,
                record=body.record, analytics=body.analytics,
                face_recognition=body.face_recognition, enabled=body.enabled,
                meta={k: getattr(body, k) for k in STRUCT})


def cam_dict(c: Camera, reveal: bool = True) -> dict:
    d = {k: getattr(c, k) for k in ("id", "name", "url", "substream_url", "location", "record",
                                    "analytics", "face_recognition", "enabled")}
    d["settings"] = settings_of(c).model_dump(exclude={"name", "url", "substream_url"})
    if not reveal:  # only admins and modules see credentials
        d["settings"]["password"] = ""
        d["url"], d["substream_url"] = _strip_creds(d["url"]), _strip_creds(d["substream_url"])
    return d


async def _resolve_safe(body: CameraIn):
    try:
        return await resolve(body), None
    except Exception as e:
        return None, getattr(e, "detail", str(e))


# ---------------------------------------------------------------- API
@router.get("/camera-brands")
def camera_brands(_=Depends(require("viewer"))):
    return [{"id": k, "label": v["label"], "onvif": bool(v.get("onvif")),
             "custom": bool(v.get("custom"))} for k, v in BRANDS.items()]


@router.get("/cameras")
def list_cameras(p=Depends(require("viewer", allow_module=True))):
    reveal = p.get("role") in ("admin", "module")
    with SessionLocal() as s:
        return [cam_dict(c, reveal) for c in s.query(Camera).order_by(Camera.id)]


@router.post("/cameras")
async def create_camera(body: CameraIn, _=Depends(require("admin"))):
    data = await resolve(body)
    with SessionLocal() as s:
        c = Camera(**data)
        s.add(c)
        s.commit()
        out = cam_dict(c)
    await bus.broadcast("camera.changed", {"id": out["id"], "action": "created"})
    return out


@router.put("/cameras/{cid}")
async def update_camera(cid: int, body: CameraIn, _=Depends(require("admin"))):
    data = await resolve(body)
    with SessionLocal() as s:
        c = s.get(Camera, cid)
        if not c:
            raise HTTPException(404)
        for k, v in data.items():
            setattr(c, k, v)
        s.commit()
        out = cam_dict(c)
    await bus.broadcast("camera.changed", {"id": cid, "action": "updated"})
    return out


@router.delete("/cameras/{cid}")
async def delete_camera(cid: int, _=Depends(require("admin"))):
    with SessionLocal() as s:
        c = s.get(Camera, cid)
        if not c:
            raise HTTPException(404)
        s.delete(c)
        s.commit()
    await bus.broadcast("camera.changed", {"id": cid, "action": "deleted"})
    return {"ok": True}


@router.post("/cameras/batch")
async def batch_create(body: BatchIn, _=Depends(require("admin"))):
    """Add many cameras at once: every IP x every channel (channels are for NVRs/encoders)."""
    ips = [ip.strip() for ip in body.ips if ip.strip()]
    channels = [1] if BRANDS.get(body.brand, {}).get("onvif") else (body.channels or [1])
    if not ips:
        raise HTTPException(400, "No IP addresses given")
    if len(ips) * len(channels) > 256:
        raise HTTPException(400, "Too many cameras in one batch (max 256)")
    common = body.model_dump(exclude={"name_prefix", "start_number", "ips", "channels"})
    jobs, n = [], body.start_number
    for ip in ips:
        for ch in channels:
            jobs.append(CameraIn(**{**common, "ip": ip, "channel": ch,
                                    "name": f"{body.name_prefix} {n}".strip()}))
            n += 1
    results = await asyncio.gather(*(_resolve_safe(j) for j in jobs))
    rows, failed = [], []
    for job, (data, err) in zip(jobs, results):
        if err:
            failed.append({"name": job.name, "ip": job.ip, "channel": job.channel, "error": err})
        else:
            rows.append(Camera(**data))
    with SessionLocal() as s:
        s.add_all(rows)
        s.commit()
        created = [cam_dict(r) for r in rows]
    if created:
        await bus.broadcast("camera.changed", {"action": "batch", "count": len(created)})
    return {"created": created, "failed": failed}


@router.post("/cameras/{cid}/copy")
async def copy_settings(cid: int, body: CopyIn, _=Depends(require("admin"))):
    """Copy chosen setting groups from one camera to others (URLs are rebuilt per camera)."""
    unknown = set(body.fields) - set(COPY_GROUPS)
    if unknown:
        raise HTTPException(400, f"Unknown fields: {', '.join(sorted(unknown))}")
    with SessionLocal() as s:
        src = s.get(Camera, cid)
        if not src:
            raise HTTPException(404)
        source = settings_of(src)
        targets = [(t.id, settings_of(t)) for t in
                   s.query(Camera).filter(Camera.id.in_(body.target_ids), Camera.id != cid)]
    for _, t in targets:
        for group in body.fields:
            for k in COPY_GROUPS[group]:
                setattr(t, k, getattr(source, k))
    results = await asyncio.gather(*(_resolve_safe(t) for _, t in targets))
    updated, failed = [], []
    with SessionLocal() as s:
        for (tid, _), (data, err) in zip(targets, results):
            if err:
                failed.append({"id": tid, "error": err})
                continue
            c = s.get(Camera, tid)
            for k, v in data.items():
                setattr(c, k, v)
            updated.append(tid)
        s.commit()
    if updated:
        await bus.broadcast("camera.changed", {"action": "copy", "ids": updated})
    return {"updated": updated, "failed": failed}
