"""Recording storage: local folders / disks and network shares (SMB/CIFS, NFS).

Every enabled storage is mounted into modules that declare `storage_mounts: true` (the VMS
recorder) at /storage/<id>. Network shares are mounted by Docker itself (local volume driver),
so the host needs no fstab entries. Each camera records to its own storage or the default one.
"""
import asyncio
import re
import socket

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require
from .config import settings
from .db import Camera, SessionLocal, Storage
from .recorders import STATUS as RECORDER_STATUS, recorder_view, with_new_code

router = APIRouter(prefix="/api")

TYPES = {"local": "Local disk / folder", "smb": "Network share (SMB)", "nfs": "Network share (NFS)",
         "recorder": "Recording server (Ubuntu / Windows)"}
CONFIG_KEYS = ("path", "server", "share", "username", "password", "domain", "options")
RESERVED = ("/etc", "/bin", "/boot", "/dev", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin",
            "/sys", "/usr", "/var/lib/docker", "/var/run", "/opt/ellalon", "/snap")
MOUNT_ERRORS: dict[str, str] = {}   # storage id -> last mount error (written by the Docker manager)
hooks = {"apply": None, "test": None}   # set by main: re-mount storages / run a write test


class StorageIn(BaseModel):
    name: str
    type: str = "local"
    path: str = ""            # local: folder on the server
    server: str = ""          # smb / nfs: IP or host name
    share: str = ""           # smb: share[/folder]; nfs: export path
    username: str = ""
    password: str = ""
    domain: str = ""
    options: str = ""         # extra mount options (advanced)
    retention_days: int = 30
    min_free_gb: float = 20
    max_gb: float = 0         # space reserved for recordings (0 = no limit)
    enabled: bool = True


def redact(text: str) -> str:
    return re.sub(r"(password=)[^,'\"\s]*", r"\1***", text)


def validate(b: StorageIn) -> StorageIn:
    if b.type not in TYPES:
        raise HTTPException(400, "Unknown storage type")
    b.name = b.name.strip()
    if not b.name:
        raise HTTPException(400, "Enter a name")
    if b.retention_days < 1 or b.min_free_gb < 0 or b.max_gb < 0:
        raise HTTPException(400, "Days must be at least 1 and free space cannot be negative")
    if b.type == "recorder":            # folder is on the recorder machine (empty = its default)
        b.path = b.path.strip()
    elif b.type == "local":
        p = "/" + b.path.strip().strip("/")
        if not b.path.strip().startswith("/") or ".." in p.split("/") or p == "/":
            raise HTTPException(400, "Enter a full folder path, for example /mnt/disk2/recordings")
        if any(p == r or p.startswith(r + "/") for r in RESERVED):
            raise HTTPException(400, f"{p} is a system folder - choose a folder on a data disk, e.g. under /mnt")
        b.path = p
    else:
        b.server, b.share = b.server.strip(), b.share.strip().replace("\\", "/")
        if not b.server or not b.share.strip("/"):
            raise HTTPException(400, "Enter the server and the " + ("export path" if b.type == "nfs" else "share name"))
        if b.type == "smb" and "," in b.username + b.password + b.domain:
            raise HTTPException(400, "SMB username, password and domain cannot contain commas")
    return b


def mount_options(st: dict) -> dict:
    """Docker local-driver options for a network share (raises ValueError on bad input)."""
    try:
        ip = socket.gethostbyname(st["server"])
    except OSError:
        raise ValueError(f"Cannot find server '{st['server']}' - use its IP address")
    extra = st.get("options", "").strip().strip(",")
    if st["type"] == "nfs":
        o = f"addr={ip},rw,soft,timeo=150,retrans=3,nolock" + (f",{extra}" if extra else "")
        return {"type": "nfs", "o": o, "device": ":/" + st["share"].strip("/")}
    o = [f"addr={ip}", "uid=0", "gid=0", "file_mode=0660", "dir_mode=0770"]
    if "vers=" not in extra:
        o.append("vers=3.0")
    if st.get("username"):
        o += [f"username={st['username']}", f"password={st.get('password', '')}"]
    else:
        o.append("guest")
    if st.get("domain"):
        o.append(f"domain={st['domain']}")
    if extra:
        o.append(extra)
    return {"type": "cifs", "o": ",".join(o), "device": f"//{ip}/{st['share'].strip('/')}"}


def as_dict(s: Storage) -> dict:
    return {"id": s.id, "name": s.name, "type": s.type, **{k: (s.config or {}).get(k, "") for k in CONFIG_KEYS},
            "retention_days": s.retention_days, "min_free_gb": s.min_free_gb,
            "max_gb": float((s.config or {}).get("max_gb") or 0),
            "is_default": s.is_default, "enabled": s.enabled}


def location(d: dict) -> str:
    if d["type"] == "recorder":
        return d.get("path") or "default recording folder"
    if d["type"] == "local":
        return d["path"]
    if d["type"] == "nfs":
        return f"{d['server']}:/{d['share'].strip('/')}"
    return f"//{d['server']}/{d['share'].strip('/')}"


def load_storages() -> list[dict]:
    """Enabled storages with full settings (for mounting)."""
    with SessionLocal() as s:
        return [as_dict(x) for x in s.query(Storage).filter(Storage.enabled.is_(True)).order_by(Storage.name)
                if x.type != "recorder"]       # recording servers record on their own machine


def ensure_default():
    with SessionLocal() as s:
        if not s.query(Storage).count():
            s.add(Storage(id="local", name="Local disk", type="local", is_default=True, enabled=True,
                          config={"path": f"{settings.data_root}/modules/vms-recorder/recordings"},
                          retention_days=30, min_free_gb=20))
            s.commit()


def _camera_counts() -> dict:
    with SessionLocal() as s:
        default = next((x.id for x in s.query(Storage).filter_by(is_default=True)), "local")
        counts: dict[str, int] = {}
        for c in s.query(Camera):
            if c.record:
                sid = (c.meta or {}).get("storage_id") or default
                counts[sid] = counts.get(sid, 0) + 1
        return counts


def _slug(name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "storage"
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}-{n}", n + 1
    return sid


async def _apply():
    if hooks["apply"]:
        await hooks["apply"]()


# ---------------------------------------------------------------- API
@router.get("/storage")
def list_storage(p=Depends(require("viewer", allow_module=True))):
    counts = _camera_counts()
    with SessionLocal() as s:
        out = []
        for x in s.query(Storage).order_by(Storage.is_default.desc(), Storage.name):
            d = as_dict(x)
            d.update(location=location(d), has_password=bool(d["password"]), password="",
                     cameras=counts.get(x.id, 0), mount_error=MOUNT_ERRORS.get(x.id, ""))
            if x.type == "recorder":
                d["recorder"] = recorder_view(x, p.get("role") == "admin")
            out.append(d)
        return out


@router.post("/storage")
async def add_storage(body: StorageIn, _=Depends(require("admin"))):
    b = validate(body)
    with SessionLocal() as s:
        sid = _slug(b.name, {x.id for x in s.query(Storage)})
        cfg = {k: getattr(b, k) for k in CONFIG_KEYS}
        if b.type == "recorder":
            cfg = with_new_code({"path": b.path})
        cfg["max_gb"] = b.max_gb
        s.add(Storage(id=sid, name=b.name, type=b.type, enabled=b.enabled, is_default=False,
                      retention_days=b.retention_days, min_free_gb=b.min_free_gb, config=cfg))
        s.commit()
    if b.type != "recorder":
        await _apply()
    return {"id": sid, "code": cfg.get("code")}


@router.put("/storage/{sid}")
async def update_storage(sid: str, body: StorageIn, _=Depends(require("admin"))):
    b = validate(body)
    with SessionLocal() as s:
        x = s.get(Storage, sid)
        if not x:
            raise HTTPException(404)
        if x.is_default and not b.enabled:
            raise HTTPException(400, "The default storage cannot be disabled - make another one default first")
        if (x.type == "recorder") != (b.type == "recorder"):
            raise HTTPException(400, "A recording server cannot be changed into a disk or share - add a new storage")
        cfg = {**(x.config or {}), **{k: getattr(b, k) for k in CONFIG_KEYS}}
        if not b.password:
            cfg["password"] = (x.config or {}).get("password", "")
        cfg["max_gb"] = b.max_gb
        x.name, x.type, x.enabled, x.config = b.name, b.type, b.enabled, cfg
        x.retention_days, x.min_free_gb = b.retention_days, b.min_free_gb
        s.commit()
    MOUNT_ERRORS.pop(sid, None)
    await _apply()
    return {"ok": True}


@router.post("/storage/{sid}/default")
async def make_default(sid: str, _=Depends(require("admin"))):
    with SessionLocal() as s:
        x = s.get(Storage, sid)
        if not x:
            raise HTTPException(404)
        if not x.enabled:
            raise HTTPException(400, "Enable the storage first")
        if x.type == "recorder":
            raise HTTPException(400, "A recording server cannot be the default - choose it per camera instead")
        for y in s.query(Storage):
            y.is_default = y.id == sid
        s.commit()
    await _apply()
    return {"ok": True}


@router.delete("/storage/{sid}")
async def delete_storage(sid: str, _=Depends(require("admin"))):
    with SessionLocal() as s:
        x = s.get(Storage, sid)
        if not x:
            raise HTTPException(404)
        if x.is_default:
            raise HTTPException(400, "Make another storage the default before deleting this one")
        n = sum(1 for c in s.query(Camera) if (c.meta or {}).get("storage_id") == sid)
        if n:
            raise HTTPException(409, f"{n} camera(s) record to this storage - move them to another storage first")
        s.delete(x)
        s.commit()
    MOUNT_ERRORS.pop(sid, None)
    RECORDER_STATUS.pop(sid, None)
    await _apply()
    return {"ok": True}


@router.post("/storage/{sid}/pairing")
def new_pairing_code(sid: str, _=Depends(require("admin"))):
    """New one-time code to install (or reinstall) a recording server."""
    with SessionLocal() as s:
        x = s.get(Storage, sid)
        if not x or x.type != "recorder":
            raise HTTPException(404)
        x.config = with_new_code(x.config or {})
        s.commit()
        return {"code": x.config["code"], "code_expires": x.config["code_expires"]}


@router.post("/storage/test")
async def test_storage(body: StorageIn, id: str | None = None, _=Depends(require("admin"))):
    """Mount the storage in a throw-away container and write a test file."""
    b = validate(body)
    if b.type == "recorder":
        raise HTTPException(400, "Recording servers are checked automatically once installed")
    st = {"id": "test", "type": b.type, **{k: getattr(b, k) for k in CONFIG_KEYS}}
    if id and not st["password"]:
        with SessionLocal() as s:
            x = s.get(Storage, id)
            st["password"] = (x.config or {}).get("password", "") if x else ""
    if not hooks["test"]:
        raise HTTPException(503, "Storage test is not available")
    return await asyncio.to_thread(hooks["test"], st)
