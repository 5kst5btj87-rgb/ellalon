"""Video analytics module: one worker thread per camera with analytics switched on.

Each worker reads the camera's substream (main stream if there is none), runs the object detector
(YOLOX) when the camera has object rules, evaluates its rules (intrusion zones, line crossing,
loitering, motion areas) and publishes events with a snapshot:
  analytics.alarm               rules set to "send to Alarm manager" (opens an alarm there)
  analytics.<rule type>         other rules (Events tab only)
Cameras without rules but with analytics on get simple motion detection on the whole picture.
"""
import asyncio
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

from detectors import LABELS, ObjectDetector, RuleEngine, draw_objects, draw_rules
from ellalon_sdk import EllalonModule

FPS = float(os.environ.get("ANALYTICS_FPS", "3"))
MODEL = os.environ.get("ANALYTICS_MODEL", "/models/yolox_tiny.onnx")
SNAPS = Path(os.environ.get("ANALYTICS_SNAPSHOTS", "/data/snapshots"))
KEEP_DAYS = float(os.environ.get("ANALYTICS_SNAPSHOT_DAYS", "14"))
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

m = EllalonModule(version="0.2.0")
app = m.app
workers: dict[int, "Worker"] = {}
loop: asyncio.AbstractEventLoop | None = None
detector: ObjectDetector | None = None
model_error = ""
DEFAULT_RULES = [{"id": "auto-motion", "name": "Motion", "type": "motion", "enabled": True, "zone": [],
                  "sensitivity": 1, "cooldown": 10, "alarm": False, "severity": "warning"}]


def load_model():
    global detector, model_error
    try:
        detector = ObjectDetector(MODEL)
        m.status["model"] = f"YOLOX {detector.size[1]}x{detector.size[0]}"
    except Exception as e:
        model_error = f"object detector not available ({e.__class__.__name__}: {str(e)[:120]})"
        m.status["model"] = model_error
        m.log.warning(model_error)


class Worker(threading.Thread):
    def __init__(self, cam: dict):
        super().__init__(daemon=True, name=f"analytics-{cam['id']}")
        self.cam, self.stop_flag = cam, threading.Event()
        self.key = key_of(cam)
        rules = cam.get("analytics_rules") or DEFAULT_RULES
        self.engine = RuleEngine(rules)
        self.frames, self.events, self.error = 0, 0, ""
        self.last_frame = None
        self.last_dets: list = []
        self.lock = threading.Lock()

    def run(self):
        url = self.cam.get("substream_url") or self.cam["url"]
        while not self.stop_flag.is_set():
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                self.error = "cannot open the camera stream"
                self.stop_flag.wait(10)
                continue
            self.error, last = "", 0.0
            while not self.stop_flag.is_set():
                if not cap.grab():
                    self.error = "stream interrupted"
                    break
                now = time.time()
                if now - last < 1 / FPS:
                    continue
                last = now
                ok, frame = cap.retrieve()
                if not ok:
                    break
                self.analyse(frame, now)
            cap.release()
            self.stop_flag.wait(3)

    def analyse(self, frame, now):
        self.frames += 1
        dets = []
        if self.engine.needs_objects:
            if detector is None:
                self.error = model_error
            else:
                lowest = min((float(r.get("min_confidence", 0.5)) for r in self.engine.rules if r["type"] != "motion"),
                             default=0.5)
                dets = detector.detect(frame, max(0.2, lowest * 0.8))
        with self.lock:
            self.last_frame, self.last_dets = frame, dets
        for ev in self.engine.process(frame, dets, now):
            self.events += 1
            self.publish(frame, ev, now)

    def publish(self, frame, ev, now):
        r, obj = ev["rule"], ev["object"]
        label = LABELS[r["type"]]
        what = f" ({obj.cls})" if obj else ""
        snap = save_snapshot(self.cam["id"], frame, r, obj, now)
        payload = {"title": f"{label}{what} - {self.cam['name']} · {r.get('name') or label}",
                   "event": f"rule-{r['id']}", "rule": r.get("name"), "rule_type": r["type"],
                   "object": obj.cls if obj else None, "confidence": round(obj.conf, 2) if obj else None,
                   "detail": ev.get("detail") or None, "snapshot": snap, "camera": self.cam["name"]}
        payload = {k: v for k, v in payload.items() if v is not None}
        if r.get("alarm"):
            kind, sev = "analytics.alarm", r.get("severity", "high")
        else:
            kind, sev = f"analytics.{r['type']}", "info"
        if loop:
            asyncio.run_coroutine_threadsafe(m.publish(kind, payload, sev, camera_id=self.cam["id"]), loop)

    def preview(self) -> bytes | None:
        with self.lock:
            if self.last_frame is None:
                return None
            img, dets = self.last_frame.copy(), list(self.last_dets)
        draw_rules(img, self.engine.rules)
        draw_objects(img, dets, (0, 200, 255))
        draw_objects(img, [t for t in self.engine.tracks if t.hits >= 2], (0, 255, 0))
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return jpg.tobytes() if ok else None


def save_snapshot(cid, frame, rule, obj, now) -> str | None:
    try:
        img = frame.copy()
        draw_rules(img, [rule], highlight=rule["id"])
        if obj:
            draw_objects(img, [obj], (0, 0, 255))
        folder = SNAPS / str(cid)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.fromtimestamp(now):%Y%m%d-%H%M%S}-{re.sub(r'[^a-zA-Z0-9]', '', rule['id'])[:20]}.jpg"
        cv2.imwrite(str(folder / name), img, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return f"/modules/analytics/snapshots/{cid}/{name}"
    except Exception as e:
        m.log.warning("snapshot failed: %s", e)
        return None


def key_of(cam) -> tuple:
    return (cam.get("url"), cam.get("substream_url"), repr(cam.get("analytics_rules")))


async def sync_cameras():
    cams = {c["id"]: c for c in await m.cameras() if c["enabled"] and c["analytics"]}
    for cid in list(workers):
        if cid not in cams or workers[cid].key != key_of(cams[cid]):   # removed, or rules changed
            workers.pop(cid).stop_flag.set()
    for cid, cam in cams.items():
        if cid not in workers:
            workers[cid] = Worker(cam)
            workers[cid].start()
    m.status["cameras"] = len(workers)


@m.on_startup
async def _start():
    global loop
    loop = asyncio.get_running_loop()
    await asyncio.to_thread(load_model)


@m.on_broadcast("camera.changed")
async def _changed(_):
    await sync_cameras()


@m.background
async def poller():
    while True:
        try:
            await sync_cameras()
        except Exception as e:
            m.log.warning("camera sync failed: %s", e)
        await asyncio.sleep(30)


@m.background
async def snapshot_cleanup():
    while True:
        await asyncio.sleep(3600)
        cutoff = time.time() - KEEP_DAYS * 86400
        for f in SNAPS.glob("*/*.jpg"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- API (behind Ellalon login)
@app.get("/status")
async def status():
    return {"model": m.status.get("model"), "model_ms": round(detector.ms, 1) if detector else None, "fps": FPS,
            "cameras": {cid: {"name": w.cam["name"], "frames": w.frames, "events": w.events, "alive": w.is_alive(),
                              "error": w.error, "rules": len(w.engine.rules),
                              "objects": len([t for t in w.engine.tracks if t.hits >= 2])}
                        for cid, w in workers.items()}}


@app.get("/cameras/{cid}/preview.jpg")
async def preview(cid: int):
    w = workers.get(cid)
    if not w:
        raise HTTPException(404, "Analytics is not running for this camera")
    jpg = await asyncio.to_thread(w.preview)
    if not jpg:
        raise HTTPException(404, w.error or "No picture yet")
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/snapshots/{cid}/{name}")
async def snapshot(cid: int, name: str):
    if not re.fullmatch(r"[\w-]+\.jpg", name):
        raise HTTPException(404)
    f = SNAPS / str(cid) / name
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg")


@m.command("status")
async def status_cmd(_):
    return await status()
