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

m = EllalonModule(version="0.3.0")
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
        self.audio = True          # set to False if this camera's audio cannot be recorded

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
            "-i", self.cam["url"], "-map", "0:v", *(["-map", "0:a?"] if self.audio else []),
            # video untouched; audio (often G.711, which MP4 cannot hold) converted to AAC
            "-c:v", "copy", *(["-c:a", "aac", "-b:a", "64k"] if self.audio else []),
            "-f", "segment", "-segment_time", str(SEGMENT), "-segment_atclocktime", "1",
            "-reset_timestamps", "1", "-strftime", "1", "-segment_format", "mp4",
            "-segment_format_options", "movflags=+frag_keyframe+empty_moov:flush_packets=1",
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
            self.last_error = ((err or b"").decode(errors="replace").strip()[-500:]
                               or f"ffmpeg stopped (exit code {self.proc.returncode})")
            self.restarts += 1
            quick = time.time() - started < 30
            if quick:
                drop_empty(ROOT / self.sid / str(self.id), started)
            if quick and self.audio and any(h in self.last_error.lower() for h in AUDIO_HINTS):
                self.audio = False
                self.last_error += " - recording video only from now on"
                m.log.warning("camera %s: audio cannot be recorded, video only", self.id)
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


def apply_retention(root: Path, st: dict) -> dict:
    """Delete recordings that are too old, failed (empty) files, and the oldest ones while the disk is
    below 'keep free' or the folder is over its reserved size. Returns usage numbers."""
    files = []
    for f in root.glob("*/*.mp4"):
        try:
            s = f.stat()
            files.append((s.st_mtime, s.st_size, f))
        except OSError:
            pass
    files.sort(key=lambda x: x[0])
    now = time.time()
    cutoff = now - float(st.get("retention_days") or 30) * 86400
    min_free = float(st.get("min_free_gb") or 0) * 1e9
    max_bytes = float(st.get("max_gb") or 0) * 1e9
    total, free = sum(x[1] for x in files), shutil.disk_usage(root).free
    left = []
    for mtime, size, f in files:
        drop = (mtime < cutoff
                or (size < 1024 and mtime < now - 600)                      # failed / empty attempts
                or (max_bytes and total > max_bytes and mtime < now - 600)  # over the reserved size
                or (free < min_free and mtime < now - 3600))                # disk nearly full
        if drop:
            try:
                f.unlink()
                total, free = total - size, free + size
                continue
            except OSError:
                pass
        left.append((mtime, size, f))
    du = shutil.disk_usage(root)
    return {"total_gb": round(du.total / 1e9, 1), "free_gb": round(du.free / 1e9, 1),
            "recordings_gb": round(total / 1e9, 2), "max_gb": float(st.get("max_gb") or 0),
            "files": len(left),
            "oldest": datetime.fromtimestamp(left[0][0], timezone.utc).isoformat() if left else None}


def drop_empty(folder: Path, since: float):
    """Remove the 0-byte files a failed recording attempt leaves behind."""
    try:
        for f in folder.glob("*.mp4"):
            s = f.stat()
            if s.st_size < 1024 and s.st_mtime >= since - 2:
                f.unlink()
    except OSError:
        pass


AUDIO_HINTS = ("audio", "codec", "could not write header", "not currently supported", "tag", "sample rate", "aac")


def clean_storage(sid: str, st: dict) -> dict:
    """Apply retention to one storage and return its usage numbers."""
    root = ROOT / sid
    if not root.is_dir():
        return {"mounted": False}
    return {"mounted": True, "writable": _writable(root), **apply_retention(root, st)}


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
            "retention_days": 10 ** 6, "min_free_gb": 0, "max_gb": 0})} if mounted(sid) else {"mounted": False}))
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


# ---------------------------------------------------------------- quick clips ("last 60 seconds")
class NoRecording(Exception):
    pass


def cut_clip(dirs, seconds, end_ts=None, ffmpeg="ffmpeg", segment=300, workdir=None):
    """Cut [end - seconds, end] out of the 5-minute segments (the one still being written included)
    without re-encoding. Returns the path of a temporary MP4 the caller must delete."""
    import subprocess
    import tempfile
    from datetime import datetime as _dt, timedelta as _td
    seconds = max(5, min(int(seconds), 600))
    end = _dt.fromtimestamp(float(end_ts)) if end_ts else _dt.now()     # same clock as the file names
    start = end - _td(seconds=seconds)
    segs = []
    for d in dirs:
        for f in (d.glob("*.mp4") if d.is_dir() else []):
            try:
                segs.append((_dt.strptime(f.stem, "%Y%m%d-%H%M%S"), f))
            except ValueError:
                pass
    segs.sort()
    pick = []
    for i, (t, f) in enumerate(segs):
        nxt = min(segs[i + 1][0] if i + 1 < len(segs) else _dt.max, t + _td(seconds=segment + 5))
        if t < end and nxt > start and f.stat().st_size > 0:
            pick.append((t, f))
    if not pick:
        raise NoRecording(f"No recording between {start:%H:%M:%S} and {end:%H:%M:%S}")
    offset = max(0.0, (start - pick[0][0]).total_seconds())
    tmp = Path(tempfile.mkdtemp(prefix="clip-", dir=workdir))
    lst, out = tmp / "list.txt", tmp / "clip.mp4"
    lst.write_text("".join("file '" + str(f).replace("'", "'\\''") + "'\n" for _, f in [(t, f.resolve()) for t, f in pick]))
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "concat", "-safe", "0",
           "-ss", f"{offset:.2f}", "-i", str(lst), "-t", str(seconds), "-map", "0:v", "-map", "0:a?",
           "-c", "copy", "-movflags", "+faststart", "-y", str(out)]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    lst.unlink(missing_ok=True)
    if r.returncode != 0 or not out.is_file() or out.stat().st_size < 1000:
        out.unlink(missing_ok=True)
        tmp.rmdir()
        raise NoRecording("Could not cut the clip: " + (r.stderr.decode(errors="replace").strip()[-200:] or "empty result"))
    return out


@app.get("/cameras/{cid}/clip")
async def clip(cid: int, seconds: int = 60, end: float | None = None):
    """MP4 of the last `seconds` (or the `seconds` before unix time `end`) of a camera."""
    from starlette.background import BackgroundTask
    dirs = [d / str(cid) for d in ROOT.iterdir()] if ROOT.is_dir() else []
    try:
        out = await asyncio.to_thread(cut_clip, dirs, seconds, end, "ffmpeg", SEGMENT)
    except NoRecording as e:
        raise HTTPException(404, str(e))

    def cleanup():
        out.unlink(missing_ok=True)
        out.parent.rmdir()
    return FileResponse(out, media_type="video/mp4", filename=f"camera{cid}-clip.mp4", background=BackgroundTask(cleanup))
