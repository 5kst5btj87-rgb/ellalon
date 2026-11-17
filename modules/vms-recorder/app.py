"""VMS recorder: one supervised FFmpeg process per camera, segmented MP4 files.

Storage: every storage from the Storage tab is mounted at /storage/<storage id>. A camera records
to its own storage (camera setting) or to the default storage; if that storage is not connected
it falls back to the default one and reports it. Layout:
    /storage/<storage id>/<camera id>/<YYYYMMDD>-<HHMMSS>.mp4
Retention runs per storage: delete recordings older than its "keep days", and the oldest ones
while free space is below its "keep free GB" (the last hour is never deleted for space).
"""
import asyncio
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from ellalon_sdk import EllalonModule

ROOT = Path("/storage")
SEGMENT = int(os.environ.get("SEGMENT_SECONDS", "300"))

m = EllalonModule(version="0.2.0")
app = m.app
recorders: dict[int, "Recorder"] = {}
storages: dict[str, dict] = {}      # id -> settings from core (enabled local / network storages)
remote_ids: set[str] = set()        # recording servers: their cameras are recorded on that machine
stats: dict[str, dict] = {}         # id -> usage numbers
fallback_warned: set[int] = set()


def mounted(sid: str) -> bool:
    return (ROOT / sid).is_dir()


def default_sid() -> str:
    return next((k for k, v in storages.items() if v.get("is_default")), "local")


def target_for(cam: dict) -> tuple[str, str]:
    """(storage id to record to, reason if it is not the one the camera asked for)."""
    want = (cam.get("settings") or {}).get("storage_id") or default_sid()
    if want in storages and mounted(want):
        return want, ""
    name = storages.get(want, {}).get("name", want)
    return default_sid(), f"storage '{name}' is not connected - recording to the default storage"


class Recorder:
    def __init__(self, cam: dict, sid: str):
        self.cam, self.sid = cam, sid
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.restarts = 0
        self.last_error = ""

    @property
    def id(self):
        return self.cam["id"]

    def cmd(self) -> list[str]:
        out = ROOT / self.sid / str(self.id)
        out.mkdir(parents=True, exist_ok=True)
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.cam["url"].startswith("rtsp"):
            args += ["-rtsp_transport", "tcp", "-timeout", "10000000"]
        return args + [
            "-i", self.cam["url"], "-map", "0:v", "-map", "0:a?", "-c", "copy",
            "-f", "segment", "-segment_time", str(SEGMENT), "-segment_atclocktime", "1",
            "-reset_timestamps", "1", "-strftime", "1", "-segment_format", "mp4",
            "-segment_format_options", "movflags=+frag_keyframe+empty_moov",
            str(out / "%Y%m%d-%H%M%S.mp4"),
        ]

    async def run(self):
        while True:
            started = time.time()
            try:
                self.proc = await asyncio.create_subprocess_exec(*self.cmd(), stderr=asyncio.subprocess.PIPE)
            except OSError as e:          # storage vanished / not writable
                self.last_error = str(e)
                await asyncio.sleep(30)
                continue
            await m.publish("recording.started", {"camera": self.cam["name"], "storage": self.sid},
                            camera_id=self.id)
            _, err = await self.proc.communicate()
            self.last_error = (err or b"").decode(errors="replace")[-500:]
            self.restarts += 1
            await m.publish("recording.stopped", {"camera": self.cam["name"], "error": self.last_error},
                            "warning", camera_id=self.id)
            # back off if the camera keeps failing quickly
            await asyncio.sleep(5 if time.time() - started > 60 else min(60, 5 * self.restarts))

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()


async def refresh_storages():
    global storages, remote_ids
    enabled = [s for s in await m.api("GET", "/api/storage") if s["enabled"]]
    remote_ids = {s["id"] for s in enabled if s["type"] == "recorder"}
    storages = {s["id"]: s for s in enabled if s["type"] != "recorder"}


async def sync_cameras():
    cams = {c["id"]: c for c in await m.cameras() if c["enabled"] and c["record"]
            and (c.get("settings") or {}).get("storage_id") not in remote_ids}
    targets = {cid: target_for(cam) for cid, cam in cams.items()}
    for cid in list(recorders):
        r = recorders[cid]
        if cid not in cams or r.cam["url"] != cams[cid]["url"] or r.sid != targets[cid][0]:
            await recorders.pop(cid).stop()
    for cid, cam in cams.items():
        sid, reason = targets[cid]
        if reason and cid not in fallback_warned:
            fallback_warned.add(cid)
            await m.publish("recording.storage_fallback", {"camera": cam["name"], "reason": reason},
                            "warning", camera_id=cid)
        elif not reason:
            fallback_warned.discard(cid)
        if cid not in recorders:
            recorders[cid] = Recorder(cam, sid)
            recorders[cid].start()
    m.status["recording"] = len(recorders)


@m.on_broadcast("camera.changed")
async def _changed(_):
    await sync_cameras()


@m.on_broadcast("storage.changed")
async def _storage_changed(_):
    await refresh_storages()
    await sync_cameras()


@m.command("reload")
async def _reload(_):
    await refresh_storages()
    await sync_cameras()
    return {"recording": len(recorders)}


@m.command("status")
async def _status(_):
    return {cid: {"name": r.cam["name"], "storage": r.sid, "restarts": r.restarts,
                  "error": r.last_error[-200:]} for cid, r in recorders.items()}


@m.background
async def camera_poller():
    while True:  # safety net in case a broadcast was missed
        try:
            await refresh_storages()
            await sync_cameras()
        except Exception as e:
            m.log.warning("camera sync failed: %s", e)
        await asyncio.sleep(30)


def _writable(root: Path) -> bool:
    probe = root / ".ellalon-rw"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def clean_storage(sid: str, st: dict) -> dict:
    """Apply retention to one storage and return its usage numbers."""
    root = ROOT / sid
    if not root.is_dir():
        return {"mounted": False}
    files = sorted(root.glob("*/*.mp4"), key=lambda p: p.stat().st_mtime)
    cutoff = time.time() - float(st.get("retention_days", 30)) * 86400
    min_free = float(st.get("min_free_gb", 20))
    protect = time.time() - 3600                       # never free space by deleting the last hour
    for f in files:
        try:
            mtime = f.stat().st_mtime
            if mtime < cutoff or (shutil.disk_usage(root).free / 1e9 < min_free and mtime < protect):
                f.unlink(missing_ok=True)
            else:
                break
        except OSError:
            pass
    left = [f for f in files if f.exists()]
    du = shutil.disk_usage(root)
    oldest = datetime.fromtimestamp(left[0].stat().st_mtime, timezone.utc).isoformat() if left else None
    return {"mounted": True, "writable": _writable(root), "total_gb": round(du.total / 1e9, 1),
            "free_gb": round(du.free / 1e9, 1), "recordings_gb": round(sum(f.stat().st_size for f in left) / 1e9, 2),
            "files": len(left), "oldest": oldest}


@m.background
async def retention():
    await asyncio.sleep(10)
    while True:
        for sid, st in list(storages.items()):
            try:
                stats[sid] = await asyncio.to_thread(clean_storage, sid, st)
            except Exception as e:
                stats[sid] = {"mounted": mounted(sid), "error": str(e)}
        m.status["storage"] = {k: v.get("free_gb") for k, v in stats.items()}
        await asyncio.sleep(300)


# ---------------------------------------------------------------- storage & playback API
@app.get("/storage")
async def storage_stats():
    """Usage per storage (Storage tab)."""
    out = {}
    for sid in storages:
        s = dict(stats.get(sid) or ({"mounted": True, **await asyncio.to_thread(clean_storage, sid, {
            "retention_days": 10 ** 6, "min_free_gb": 0})} if mounted(sid) else {"mounted": False}))
        s["cameras"] = sum(1 for r in recorders.values() if r.sid == sid)
        out[sid] = s
    return out


@app.get("/cameras/{cid}/recordings")
def list_recordings(cid: int, day: str | None = None):
    """List segments for a camera across all storages; day=YYYYMMDD filters."""
    pattern = f"{day}-*.mp4" if day else "*.mp4"
    out = []
    for sdir in ROOT.iterdir() if ROOT.is_dir() else []:
        d = sdir / str(cid)
        if not d.is_dir():
            continue
        for f in d.glob(pattern):
            try:
                start = datetime.strptime(f.stem, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            out.append({"file": f.name, "start": start.isoformat(), "seconds": SEGMENT, "storage": sdir.name,
                        "bytes": f.stat().st_size, "url": f"recordings/{sdir.name}/{cid}/{f.name}"})
    return sorted(out, key=lambda r: r["start"])


@app.get("/recordings/{sid}/{cid}/{name}")
def get_recording(sid: str, cid: int, name: str):
    f = (ROOT / sid / str(cid) / name).resolve()
    if ROOT.resolve() not in f.parents or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="video/mp4")
