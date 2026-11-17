"""VMS recorder: one supervised FFmpeg process per camera, segmented MP4 files.

Layout: /recordings/<camera_id>/<YYYYMMDD>-<HHMMSS>.mp4
"""
import asyncio
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from ellalon_sdk import EllalonModule

ROOT = Path("/recordings")
SEGMENT = int(os.environ.get("SEGMENT_SECONDS", "300"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "20"))

m = EllalonModule(version="0.1.0")
app = m.app
recorders: dict[int, "Recorder"] = {}


class Recorder:
    def __init__(self, cam: dict):
        self.cam = cam
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.restarts = 0
        self.last_error = ""

    @property
    def id(self):
        return self.cam["id"]

    def cmd(self) -> list[str]:
        out = ROOT / str(self.id)
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
            self.proc = await asyncio.create_subprocess_exec(
                *self.cmd(), stderr=asyncio.subprocess.PIPE)
            await m.publish("recording.started", {"camera": self.cam["name"]}, camera_id=self.id)
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


async def sync_cameras():
    cams = {c["id"]: c for c in await m.cameras() if c["enabled"] and c["record"]}
    for cid in list(recorders):
        if cid not in cams or recorders[cid].cam["url"] != cams[cid]["url"]:
            await recorders.pop(cid).stop()
    for cid, cam in cams.items():
        if cid not in recorders:
            recorders[cid] = Recorder(cam)
            recorders[cid].start()
    m.status["recording"] = len(recorders)


@m.on_broadcast("camera.changed")
async def _changed(_):
    await sync_cameras()


@m.command("reload")
async def _reload(_):
    await sync_cameras()
    return {"recording": len(recorders)}


@m.command("status")
async def _status(_):
    return {cid: {"name": r.cam["name"], "restarts": r.restarts, "error": r.last_error[-200:]}
            for cid, r in recorders.items()}


@m.background
async def camera_poller():
    while True:  # safety net in case a broadcast was missed
        try:
            await sync_cameras()
        except Exception as e:
            m.log.warning("camera sync failed: %s", e)
        await asyncio.sleep(30)


@m.background
async def retention():
    while True:
        cutoff = time.time() - RETENTION_DAYS * 86400
        files = sorted(ROOT.glob("*/*.mp4"), key=lambda p: p.stat().st_mtime)
        for f in files:
            free_gb = shutil.disk_usage(ROOT).free / 1e9
            if f.stat().st_mtime < cutoff or free_gb < MIN_FREE_GB:
                f.unlink(missing_ok=True)
            else:
                break
        m.status["free_gb"] = round(shutil.disk_usage(ROOT).free / 1e9, 1)
        await asyncio.sleep(300)


# ---------------------------------------------------------------- playback API
@app.get("/cameras/{cid}/recordings")
def list_recordings(cid: int, day: str | None = None):
    """List segments for a camera; day=YYYYMMDD filters."""
    d = ROOT / str(cid)
    if not d.is_dir():
        return []
    pattern = f"{day}-*.mp4" if day else "*.mp4"
    out = []
    for f in sorted(d.glob(pattern)):
        start = datetime.strptime(f.stem, "%Y%m%d-%H%M%S")
        out.append({"file": f.name, "start": start.isoformat(), "seconds": SEGMENT,
                    "bytes": f.stat().st_size, "url": f"recordings/{cid}/{f.name}"})
    return out


@app.get("/recordings/{cid}/{name}")
def get_recording(cid: int, name: str):
    f = (ROOT / str(cid) / name).resolve()
    if ROOT.resolve() not in f.parents or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="video/mp4")
