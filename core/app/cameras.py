"""Camera management: brand-aware stream URLs, ONVIF discovery, batch add, copy settings.

Cameras are described by structured settings (brand, ip, credentials, channel...). Core builds
the RTSP main/sub URLs from them, so every client (web, Windows, ...) shares the same logic.
"""
import asyncio
import base64
import datetime
import hashlib
import html
import logging
import os
import re
from typing import Literal
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require
from .bus import bus
from .camera_events import EVT_STATUS, supported_types
from .db import Camera, SessionLocal

router = APIRouter(prefix="/api")
log = logging.getLogger("cameras")
hooks = {"discover": None}     # set by main: run the discovery probe on the host network

# {ch} = channel number. "onvif": stream URLs are discovered from the device.
BRANDS = {
    "hikvision": {"label": "Hikvision", "main": "/Streaming/Channels/{ch}01",
                  "sub": "/Streaming/Channels/{ch}02"},
    "dahua": {"label": "Dahua", "main": "/cam/realmonitor?channel={ch}&subtype=0",
              "sub": "/cam/realmonitor?channel={ch}&subtype=1"},
    "axis": {"label": "Axis", "main": "/axis-media/media.amp?camera={ch}",
             "sub": "/axis-media/media.amp?camera={ch}&resolution=640x360"},
    "provision": {"label": "Provision-ISR camera", "main": "/profile1", "sub": "/profile2"},
    "provision_nvr": {"label": "Provision-ISR NVR / DVR", "main": "/chID={ch}&streamType=main&linkType=tcp",
                      "sub": "/chID={ch}&streamType=sub&linkType=tcp"},
    "flir": {"label": "FLIR (auto-detect via ONVIF)", "onvif": True},
    "onvif": {"label": "ONVIF (auto-detect)", "onvif": True},
    "rtsp": {"label": "Generic RTSP (custom path)", "custom": True},
}
STRUCT = ["brand", "ip", "port", "onvif_port", "username", "password", "channel",
          "main_path", "sub_path", "use_substream", "storage_id",
          "events_enabled", "event_types", "events_alarm", "alarm_severity"]
COPY_GROUPS = {
    "brand": ["brand", "port", "onvif_port", "main_path", "sub_path"],
    "credentials": ["username", "password"],
    "substream": ["use_substream"],
    "recording": ["record"],
    "analytics": ["analytics"],
    "face_recognition": ["face_recognition"],
    "location": ["location"],
    "enabled": ["enabled"],
    "storage": ["storage_id"],
    "events": ["events_enabled", "event_types", "events_alarm", "alarm_severity", "onvif_port"],
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
    storage_id: str | None = None       # recording storage (None = default storage)
    events_enabled: bool = False        # receive analytics events from the camera itself
    event_types: list[str] = []         # empty = all event types
    events_alarm: bool = True           # raise an alarm in the Alarm manager (else Events log only)
    alarm_severity: Literal["warning", "high", "critical"] = "high"


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


class _OnvifAuthError(Exception):
    pass


def _envelope(body: str, user: str, pw: str, mode: str = "digest", offset: float = 0) -> str:
    """mode: digest (WS-Security PasswordDigest), text (PasswordText) or none (HTTP auth only).
    offset: seconds to add to our clock so the timestamp matches the camera's clock."""
    hdr = ""
    if user and mode != "none":
        nonce = os.urandom(16)
        created = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset)
                   ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if mode == "digest":
            pwd = base64.b64encode(hashlib.sha1(nonce + created.encode() + pw.encode()).digest()).decode()
            ptype = _PWD
        else:
            pwd, ptype = escape(pw), _PWD.replace("PasswordDigest", "PasswordText")
        hdr = (f'<s:Header><Security s:mustUnderstand="1" xmlns="{_WSSE}"><UsernameToken>'
               f'<Username>{escape(user)}</Username><Password Type="{ptype}">{pwd}</Password>'
               f'<Nonce EncodingType="{_B64}">{base64.b64encode(nonce).decode()}</Nonce>'
               f'<Created xmlns="{_WSU}">{created}</Created></UsernameToken></Security></s:Header>')
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">{hdr}'
            f'<s:Body>{body}</s:Body></s:Envelope>')


async def _soap(c: httpx.AsyncClient, url: str, body: str, user: str, pw: str,
                mode: str = "digest", offset: float = 0) -> str:
    r = await c.post(url, content=_envelope(body, user, pw, mode, offset),
                     headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    if r.status_code >= 400:
        fault = re.search(r"<[^>]*Text[^>]*>([^<]+)<", r.text)
        reason = fault.group(1) if fault else f"HTTP {r.status_code}"
        if r.status_code in (401, 403) or (
                r.status_code in (400, 500) and re.search(r"uthori|ender|ccess|assword|ecurity", r.text)):
            raise _OnvifAuthError(reason)
        raise HTTPException(400, f"ONVIF: {reason}")
    return r.text


async def _camera_clock_offset(c: httpx.AsyncClient, url: str) -> float:
    """Seconds between the camera's clock and ours (WS-Security timestamps must match it)."""
    try:
        r = await c.post(url, content=_envelope(
            '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>', "", ""),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        block = re.search(r"UTCDateTime>(.*?)</[^>]*UTCDateTime>", r.text, re.S)
        if not block:
            return 0
        v = {k: int(re.search(rf"{k}>(\d+)<", block.group(1)).group(1))
             for k in ("Year", "Month", "Day", "Hour", "Minute", "Second")}
        cam = datetime.datetime(v["Year"], v["Month"], v["Day"], v["Hour"], v["Minute"], v["Second"],
                                tzinfo=datetime.timezone.utc)
        return (cam - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    except Exception:
        return 0


async def onvif_stream_uris(ip: str, port: int, user: str, pw: str) -> list[str]:
    """Return RTSP URIs of the first two media profiles (main, sub).

    Logs in the way the camera accepts: WS-Security digest using the camera's own clock,
    then plain-text password, then HTTP digest only."""
    uris = []
    device = f"http://{ip}:{port}/onvif/device_service"
    try:
        async with httpx.AsyncClient(timeout=8, auth=httpx.DigestAuth(user, pw) if user else None) as c:
            offset = await _camera_clock_offset(c, device)
            media = None
            try:
                caps = await _soap(c, device, '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl">'
                                   '<Category>Media</Category></GetCapabilities>', user, pw, "digest", offset)
                m = re.search(r"Media>.*?XAddr>([^<]+)<", caps, re.S)
                if m:
                    mp = urlsplit(m.group(1).strip())
                    media = urlunsplit(("http", f"{ip}:{port}", mp.path, mp.query, ""))
            except _OnvifAuthError:
                pass
            media = media or f"http://{ip}:{port}/onvif/media_service"
            profiles, mode, last = None, None, ""
            for mode in ("digest", "text", "none"):
                try:
                    profiles = await _soap(c, media, '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>',
                                           user, pw, mode, offset)
                    break
                except _OnvifAuthError as e:
                    last = str(e)
            if profiles is None:
                raise HTTPException(400, "ONVIF: the camera rejected the username or password"
                                         f" ({last}). Some cameras need a separate ONVIF user"
                                         " or ONVIF enabled in their settings.")
            for token in re.findall(r'<[^>]*Profiles[^>]*\stoken="([^"]+)"', profiles)[:2]:
                body = ('<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl"><StreamSetup>'
                        '<Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
                        '<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol>'
                        f'</Transport></StreamSetup><ProfileToken>{escape(token)}</ProfileToken>'
                        '</GetStreamUri>')
                try:
                    u = re.search(r"<[^>]*Uri>([^<]+)<", await _soap(c, media, body, user, pw, mode, offset))
                except _OnvifAuthError as e:
                    raise HTTPException(400, f"ONVIF: stream address refused ({e})")
                if u:
                    uris.append(html.unescape(u.group(1).strip()))
    except httpx.HTTPError as e:
        raise HTTPException(400, f"Cannot reach ONVIF at {ip}:{port} ({e.__class__.__name__}) - "
                                 "check the ONVIF port (often 80, 8000 or 8080)")
    if not uris:
        raise HTTPException(400, f"No ONVIF stream profiles found on {ip}")
    return uris


# ---------------------------------------------------------------- connection monitor
CHECK_INTERVAL = int(os.environ.get("ELLALON_CAMERA_CHECK_INTERVAL", "30"))
_key = lambda u: hashlib.sha256((u or "").encode()).hexdigest()[:16]   # no credentials in status
AUTH_RETRY = 600   # seconds between automatic re-tries after a refused login
CAM_STATUS: dict[int, dict] = {}   # camera id -> {state, detail, checked, since}
STATE_TEXT = {"online": "online", "offline": "offline", "auth": "wrong username or password",
              "path": "wrong stream path", "error": "stream error"}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _digest(challenge: str, user: str, pw: str, method: str, uri: str) -> str:
    f = dict(re.findall(r'(\w+)="?([^",]+)"?', challenge))
    realm, nonce, qop = f.get("realm", ""), f.get("nonce", ""), f.get("qop", "")
    h = lambda x: hashlib.md5(x.encode()).hexdigest()
    ha1, ha2 = h(f"{user}:{realm}:{pw}"), h(f"{method}:{uri}")
    extra = ""
    if "auth" in [q.strip() for q in qop.split(",")]:
        nc, cnonce = "00000001", os.urandom(8).hex()
        resp = h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        extra = f', qop=auth, nc={nc}, cnonce="{cnonce}"'
    else:
        resp = h(f"{ha1}:{nonce}:{ha2}")
    out = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp}"{extra}'
    return out + (f', opaque="{f["opaque"]}"' if f.get("opaque") else "")


class _Rtsp:
    """One RTSP connection. Many cameras (e.g. Provision/TVT) only accept the login on the same
    connection that received the challenge, so the authenticated DESCRIBE reuses it."""

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout, self.cseq = host, port, timeout, 1
        self.r = self.w = None

    async def open(self):
        self.r, self.w = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)

    def close(self):
        if self.w:
            self.w.close()

    async def describe(self, uri, auth=None):
        lines = [f"DESCRIBE {uri} RTSP/1.0", f"CSeq: {self.cseq}", "Accept: application/sdp",
                 "User-Agent: Ellalon"]
        self.cseq += 1
        if auth:
            lines.append(f"Authorization: {auth}")
        self.w.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self.w.drain()
        head = (await asyncio.wait_for(self.r.readuntil(b"\r\n\r\n"), self.timeout)).decode(errors="replace")
        status_line, *rest = head.split("\r\n")
        if not status_line.startswith("RTSP/"):
            raise ValueError("not RTSP")
        hdrs: dict[str, list[str]] = {}
        for line in rest:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs.setdefault(k.strip().lower(), []).append(v.strip())
        n = int((hdrs.get("content-length") or ["0"])[0])
        if n:
            await asyncio.wait_for(self.r.readexactly(n), self.timeout)
        return int(status_line.split()[1]), hdrs


def _auth_for(hdrs, user, pw, uri, scheme=None):
    challenges = hdrs.get("www-authenticate", [])
    dig = next((c for c in challenges if c.lower().startswith("digest")), None)
    if dig and scheme != "basic":
        return _digest(dig, user, pw, "DESCRIBE", uri)
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def _nonce(hdrs) -> str:
    m = re.search(r'nonce="?([^",]+)', " ".join(hdrs.get("www-authenticate", [])))
    return m.group(1) if m else ""


def _who(hdrs) -> str:
    """Short description of the camera's login request, for error messages."""
    ch = hdrs.get("www-authenticate", [])
    schemes = sorted({c.split()[0].capitalize() for c in ch if c.strip()})
    realm = re.search(r'realm="([^"]*)"', " ".join(ch))
    server = (hdrs.get("server") or [""])[0]
    bits = [("/".join(schemes) + " login") if schemes else "", f'realm "{realm.group(1)}"' if realm else "",
            f"server {server}" if server else ""]
    return ", ".join(b for b in bits if b)


async def probe_url(url: str | None, timeout: float = 5) -> dict:
    """Check a stream like a player would: connect, RTSP DESCRIBE, authenticate."""
    p = urlsplit(url or "")
    host = p.hostname
    if not host:
        return {"state": "error", "detail": "no stream address"}
    rtsp = p.scheme.startswith("rtsp")
    port = p.port or (554 if rtsp else 443 if p.scheme == "https" else 80)
    conn = _Rtsp(host, port, timeout)
    try:
        await conn.open()
    except ConnectionRefusedError:
        return {"state": "offline", "detail": f"{host} refuses port {port} (wrong RTSP port?)"}
    except (OSError, asyncio.TimeoutError):
        return {"state": "offline", "detail": f"no answer from {host}:{port}"}
    if not rtsp:
        conn.close()
        return {"state": "online", "detail": f"{host}:{port} answers"}
    user, pw = unquote(p.username or ""), unquote(p.password or "")
    uri = urlunsplit((p.scheme, f"{host}:{port}", p.path or "/", p.query, ""))
    challenge: dict = {}

    async def login(conn, hdrs, scheme=None):
        """Answer the challenge; if the camera replies with a new nonce (one-time nonces), answer that once."""
        code, h2 = await conn.describe(uri, _auth_for(hdrs, user, pw, uri, scheme))
        if code == 401 and scheme != "basic" and _nonce(h2) and _nonce(h2) != _nonce(hdrs):
            code, h2 = await conn.describe(uri, _auth_for(h2, user, pw, uri, scheme))
        return code, h2

    try:
        try:
            code, hdrs = await conn.describe(uri)
        except Exception as e:
            return {"state": "error", "detail": f"not an RTSP stream ({e.__class__.__name__})"}
        challenge = hdrs
        if code == 401 and user:
            try:      # same connection first (session-bound nonce)
                code, h2 = await login(conn, hdrs)
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError):
                conn.close()      # camera hung up after the challenge: log in on a new connection
                conn = _Rtsp(host, port, timeout)
                await conn.open()
                code, h2 = await conn.describe(uri, _auth_for(hdrs, user, pw, uri))
                if code == 401:   # it wants a fresh challenge answered on this connection
                    try:
                        code, h2 = await login(conn, h2)
                    except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError):
                        code = 401
            offered = " ".join(hdrs.get("www-authenticate", [])).lower()
            if code == 401 and "digest" in offered and "basic" in offered:
                conn.close()      # offers both and refused Digest: try Basic
                conn = _Rtsp(host, port, timeout)
                await conn.open()
                c1, h1 = await conn.describe(uri)
                if c1 == 401:
                    code, h2 = await conn.describe(uri, _auth_for(h1, user, pw, uri, "basic"))
    except Exception as e:
        return {"state": "error", "detail": f"login failed ({e.__class__.__name__})"}
    finally:
        conn.close()
    if code == 200:
        return {"state": "online", "detail": "stream OK"}
    if code in (401, 403):
        who = _who(challenge)
        if not user:
            return {"state": "auth", "detail": "camera needs a username and password" + (f" ({who})" if who else "")}
        return {"state": "auth", "detail": "the device refused this username/password" + (f" ({who})" if who else "")
                + ". If the password is right, the device may have locked the account after failed attempts - "
                  "wait 30 minutes or unlock it on the device, and check the user may view live video (RTSP)."}
    if code in (400, 404, 454):
        return {"state": "path", "detail": f"stream not found (RTSP {code}) - check brand and channel"}
    return {"state": "error", "detail": f"camera answered RTSP {code}"}


async def _record(cid: int, name: str, res: dict):
    prev = CAM_STATUS.get(cid)
    same = prev and prev["state"] == res["state"]
    CAM_STATUS[cid] = {**res, "checked": _now(), "since": prev["since"] if same else _now()}
    if same or res["state"] == "disabled" or (prev and prev["state"] == "disabled" and res["state"] == "online"):
        return
    if res["state"] == "online":
        if prev and prev["state"] not in ("checking",):
            await bus.emit("core", "camera.online", {"camera": name}, "info", cid)
    else:
        await bus.emit("core", "camera.offline", {"camera": name, "reason": STATE_TEXT[res["state"]],
                                                  "detail": res["detail"]}, "warning", cid)


async def check_camera(cid: int):
    with SessionLocal() as s:
        c = s.get(Camera, cid)
        if not c:
            CAM_STATUS.pop(cid, None)
            return
        name, url, enabled = c.name, c.url, c.enabled
    if not enabled:
        CAM_STATUS[cid] = {"state": "disabled", "detail": "camera disabled", "checked": _now(), "since": _now()}
        return
    prev = CAM_STATUS.get(cid)
    if (prev and prev["state"] == "auth" and prev.get("key") == _key(url) and
            (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(prev["checked"])).total_seconds() < AUTH_RETRY):
        return            # refused login: retry rarely, or devices lock the account
    res = await probe_url(url)
    await _record(cid, name, res)
    CAM_STATUS[cid]["key"] = _key(url)


async def camera_monitor():
    sem = asyncio.Semaphore(10)

    async def one(cid):
        async with sem:
            await check_camera(cid)

    while True:
        try:
            with SessionLocal() as s:
                ids = [c.id for c in s.query(Camera)]
            for cid in [k for k in CAM_STATUS if k not in ids]:
                CAM_STATUS.pop(cid)
            await asyncio.gather(*(one(cid) for cid in ids))
        except Exception as e:
            log.warning("camera monitor: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)


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
    d["status"] = CAM_STATUS.get(c.id, {"state": "checking", "detail": "first check in progress"})
    d["events"] = EVT_STATUS.get(c.id) if (c.meta or {}).get("events_enabled") else None
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
    asyncio.create_task(check_camera(out["id"]))
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
    CAM_STATUS.pop(cid, None)
    asyncio.create_task(check_camera(cid))
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


class DiscoverIn(BaseModel):
    ranges: str = ""          # optional: "192.168.1.0/24, 10.0.0.10-10.0.0.200"


@router.post("/cameras/discover")
async def discover_cameras(body: DiscoverIn, _=Depends(require("admin"))):
    """Find cameras on the network (ONVIF, Hikvision SADP, RTSP port scan) and mark known ones."""
    ranges = [r.strip() for r in re.split(r"[,;\s]+", body.ranges) if r.strip()]
    for r in ranges:
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+(/\d{1,2}|-\d+(\.\d+\.\d+\.\d+)?)?", r):
            raise HTTPException(400, f"Not a network or range: {r} (examples: 192.168.1.0/24, 192.168.1.10-200)")
        if "/" in r and int(r.split("/")[1]) < 20:
            raise HTTPException(400, f"{r} is too large - scan /20 or smaller")
    if not hooks["discover"]:
        raise HTTPException(503, "Discovery is not available")
    try:
        res = await asyncio.wait_for(asyncio.to_thread(hooks["discover"], {"ranges": ranges, "timeout": 4}), 120)
    except Exception as e:
        raise HTTPException(502, f"Discovery failed: {getattr(e, 'detail', e)}")
    with SessionLocal() as s:
        known = {settings_of(c).ip: c.name for c in s.query(Camera)}
    for d in res.get("devices", []):
        d["existing"] = known.get(d["ip"])
    return res


@router.post("/cameras/events/probe")
async def probe_events(body: CamSettings, _=Depends(require("admin"))):
    """Which analytics events does this camera offer (and can we log in to its event service)?"""
    if not body.ip.strip():
        raise HTTPException(400, "Enter the camera IP address first")
    s = body.model_dump()
    s["ip"] = s["ip"].strip()
    return await asyncio.wait_for(supported_types(s), 30)


class BulkDeleteIn(BaseModel):
    ids: list[int] = []
    all: bool = False


@router.post("/cameras/delete")
async def delete_cameras(body: BulkDeleteIn, _=Depends(require("admin"))):
    """Delete the chosen cameras, or all of them. Recordings already saved are kept."""
    with SessionLocal() as s:
        q = s.query(Camera) if body.all else s.query(Camera).filter(Camera.id.in_(body.ids or [-1]))
        ids = [c.id for c in q]
        for c in s.query(Camera).filter(Camera.id.in_(ids or [-1])):
            s.delete(c)
        s.commit()
    for cid in ids:
        CAM_STATUS.pop(cid, None)
    if ids:
        await bus.broadcast("camera.changed", {"action": "deleted", "ids": ids})
    return {"deleted": len(ids)}


@router.post("/cameras/probe")
async def probe_camera(body: CameraIn, _=Depends(require("admin"))):
    """Test a camera's settings before saving: builds the URLs and checks main + substream."""
    main, sub = (body.url, body.substream_url) if body.url else await build_urls(body)
    res = {"main": await probe_url(main)}
    if sub:
        res["sub"] = await probe_url(sub)
    return res


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
        for c in created:
            asyncio.create_task(check_camera(c["id"]))
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
