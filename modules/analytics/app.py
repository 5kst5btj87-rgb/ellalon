"""Video analytics module: one worker thread per camera with analytics switched on.

Each worker reads the camera's substream (main stream if there is none), runs the object detector
(YOLOX) when the camera has object rules, evaluates its rules (intrusion zones, line crossing,
loitering, motion areas) and publishes events with a snapshot:
  analytics.alarm               rules set to "send to Alarm manager" (opens an alarm there)
  analytics.<rule type>         other rules (Events tab only)
Cameras without rules but with analytics on get simple motion detection on the whole picture.
"""
import asyncio
import json
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import cv2
from fastapi import HTTPException, Query
from fastapi.responses import FileResponse, Response

from detectors import LABELS, ObjectDetector, RuleEngine, draw_objects, draw_rules
import tagger as tg
from ellalon_sdk import EllalonModule

FPS = float(os.environ.get("ANALYTICS_FPS", "3"))
MODEL = os.environ.get("ANALYTICS_MODEL", "/models/yolox_tiny.onnx")
SNAPS = Path(os.environ.get("ANALYTICS_SNAPSHOTS", "/data/snapshots"))
KEEP_DAYS = float(os.environ.get("ANALYTICS_SNAPSHOT_DAYS", "14"))
TAG_MODELS = os.environ.get("ANALYTICS_TAG_MODELS", "/models")
SIGHT_DIR = Path(os.environ.get("ANALYTICS_SIGHTINGS", "/data/sightings"))
SIGHT_DAYS = float(os.environ.get("ANALYTICS_SIGHTING_DAYS", "30"))
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

m = EllalonModule(version="0.3.0")
app = m.app
workers: dict[int, "Worker"] = {}
loop: asyncio.AbstractEventLoop | None = None
detector: ObjectDetector | None = None
model_error = ""
DEFAULT_RULES = [{"id": "auto-motion", "name": "Motion", "type": "motion", "enabled": True, "zone": [],
                  "sensitivity": 1, "cooldown": 10, "alarm": False, "severity": "warning"}]


tagger: "tg.Tagger | None" = None
tagger_error = "starting"
tag_queue: queue.Queue = queue.Queue(maxsize=24)
tag_stats = {"tagged": 0, "dropped": 0, "last_ms": 0}


# ---------------------------------------------------------------- sightings (tagged people)
class Sightings:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.folder, self.lock = folder, threading.Lock()
        self.db = sqlite3.connect(str(folder / "sightings.db"), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS sightings (id TEXT PRIMARY KEY, ts REAL, last REAL, camera_id INTEGER,
                           camera TEXT, tags TEXT, scores TEXT, image TEXT)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS s_ts ON sightings(ts)")
        self.db.commit()

    def save(self, sid, cam, ts, tags, scores, crop) -> str:
        new = sid is None
        sid = sid or uuid.uuid4().hex[:16]
        day = time.strftime("%Y%m%d", time.localtime(ts))
        (self.folder / day).mkdir(exist_ok=True)
        image = f"{day}/{sid}.jpg"
        cv2.imwrite(str(self.folder / image), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        text = " " + " ".join(sorted(tags)) + " "
        with self.lock:
            if new:
                self.db.execute("INSERT INTO sightings VALUES (?,?,?,?,?,?,?,?)",
                                (sid, ts, ts, cam["id"], cam["name"], text, json.dumps(scores), image))
            else:
                self.db.execute("UPDATE sightings SET tags=?, scores=?, image=?, last=? WHERE id=?",
                                (text, json.dumps(scores), image, ts, sid))
            self.db.commit()
        return sid

    def search(self, cameras, tags, since, until, limit, before):
        q, args = "SELECT id, ts, camera_id, camera, tags, scores, image FROM sightings WHERE 1=1", []
        if cameras:
            q += f" AND camera_id IN ({','.join('?' * len(cameras))})"
            args += cameras
        groups: dict[str, list[str]] = {}
        for t in tags:
            groups.setdefault(t.split(":", 1)[0], []).append(t)
        for g in groups.values():                  # all groups must match, any tag inside a group
            q += " AND (" + " OR ".join("tags LIKE ?" for _ in g) + ")"
            args += [f"% {t} %" for t in g]
        if since:
            q += " AND ts >= ?"
            args.append(since)
        if until:
            q += " AND ts <= ?"
            args.append(until)
        if before:
            q += " AND ts < ?"
            args.append(before)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        return [{"id": r[0], "ts": r[1], "camera_id": r[2], "camera": r[3], "tags": r[4].split(),
                 "scores": json.loads(r[5] or "{}"), "image": f"/modules/analytics/sightings/{r[0]}.jpg"} for r in rows]

    def image(self, sid) -> Path | None:
        with self.lock:
            r = self.db.execute("SELECT image FROM sightings WHERE id=?", (sid,)).fetchone()
        return self.folder / r[0] if r else None

    def cleanup(self, days):
        cut = time.time() - days * 86400
        with self.lock:
            self.db.execute("DELETE FROM sightings WHERE ts < ?", (cut,))
            self.db.commit()
        for d in self.folder.iterdir():
            if d.is_dir() and d.name.isdigit() and d.name < time.strftime("%Y%m%d", time.localtime(cut)):
                for f in d.iterdir():
                    f.unlink(missing_ok=True)
                d.rmdir()


sightings: Sightings | None = None


def load_tagger():
    global tagger, tagger_error
    try:
        tagger = tg.Tagger(TAG_MODELS, "/data/cache")
        tagger_error = ""
        m.status["tagging"] = "ready"
    except Exception as e:
        tagger_error = f"{e.__class__.__name__}: {str(e)[:160]}"
        m.status["tagging"] = "not available - " + tagger_error
        m.log.warning("people tagging not available: %s", tagger_error)


def tag_loop():
    """One thread tags people for all cameras (CPU-heavy; the queue drops work when it is full)."""
    while True:
        worker, track, frame, box = tag_queue.get()
        try:
            if tagger is None:
                continue
            t0 = time.time()
            res = tagger.describe(frame, box)
            tag_stats["last_ms"] = int((time.time() - t0) * 1000)
            track.tags = set(res["tags"])
            x1, y1, x2, y2 = tg.regions(box, frame.shape[1], frame.shape[0])["full"]
            crop = frame[y1:y2, x1:x2]
            if crop.shape[0] > 320:
                k = 320 / crop.shape[0]
                crop = cv2.resize(crop, (max(1, int(crop.shape[1] * k)), 320), interpolation=cv2.INTER_AREA)
            if sightings:
                track.sighting = sightings.save(track.sighting, worker.cam, time.time(), res["tags"], res["scores"], crop)
            tag_stats["tagged"] += 1
        except Exception as e:
            m.log.warning("tagging failed: %s", e)
        finally:
            track.tag_busy = False


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
        self.tagging = cam.get("analytics_tagging") or {}
        self.tag_on = bool(self.tagging.get("enabled"))
        self.engine = RuleEngine(rules, track_always=self.tag_on)
        self.frames, self.events, self.error = 0, 0, ""
        self.last_frame = None
        self.last_dets: list = []
        self.lock = threading.Lock()

    def run(self):
        url = self.cam.get("substream_url") or self.cam["url"]
        if self.tag_on and self.tagging.get("main_stream", True):
            url = self.cam["url"]                 # sharper picture for clothes, glasses, colours
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
                             default=0.45)
                dets = detector.detect(frame, max(0.2, lowest * 0.8))
        with self.lock:
            self.last_frame, self.last_dets = frame, dets
        for ev in self.engine.process(frame, dets, now):
            self.events += 1
            self.publish(frame, ev, now)
        if self.tag_on and tagger is not None:
            self.queue_tagging(frame)

    def queue_tagging(self, frame):
        h, w = frame.shape[:2]
        min_h = float(self.tagging.get("min_height", 90))
        for t in self.engine.tracks:
            if t.cls != "person" or t.hits < 3 or t.tag_busy or t.conf < 0.45:
                continue
            x1, y1, x2, y2 = t.box
            area = (x2 - x1) * (y2 - y1)
            if y2 - y1 < min_h or y1 <= 2 or y2 >= h - 2:         # too small, or cut by the picture edge
                continue
            if t.tag_area and area < 1.7 * t.tag_area:            # tag again only when much closer / bigger
                continue
            bx = tg.regions(t.box, w, h)["full"]
            pad = 4
            cx1, cy1, cx2, cy2 = max(0, bx[0] - pad), max(0, bx[1] - pad), min(w, bx[2] + pad), min(h, bx[3] + pad)
            crop = frame[cy1:cy2, cx1:cx2].copy()
            try:
                t.tag_busy, t.tag_area = True, area
                tag_queue.put_nowait((self, t, crop, (x1 - cx1, y1 - cy1, x2 - cx1, y2 - cy1)))
            except queue.Full:
                t.tag_busy, t.tag_area = False, 0
                tag_stats["dropped"] += 1

    def publish(self, frame, ev, now):
        r, obj = ev["rule"], ev["object"]
        label = LABELS[r["type"]]
        what = f" ({obj.cls})" if obj else ""
        snap = save_snapshot(self.cam["id"], frame, r, obj, now)
        if r["type"] == "appearance" and obj is not None:
            ev = {**ev, "detail": ", ".join(tg.tag_label(t) for t in sorted(obj.tags))}
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
    return (cam.get("url"), cam.get("substream_url"), repr(cam.get("analytics_rules")), repr(cam.get("analytics_tagging")))


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
    global sightings
    try:
        sightings = Sightings(SIGHT_DIR)
    except Exception as e:
        m.log.warning("sightings store not available: %s", e)
    threading.Thread(target=tag_loop, daemon=True, name="tagger").start()
    threading.Thread(target=load_tagger, daemon=True, name="tagger-load").start()   # takes a few seconds


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
        if sightings:
            try:
                await asyncio.to_thread(sightings.cleanup, SIGHT_DAYS)
            except Exception as e:
                m.log.warning("sightings cleanup failed: %s", e)


# ---------------------------------------------------------------- API (behind Ellalon login)
@app.get("/status")
async def status():
    return {"model": m.status.get("model"), "model_ms": round(detector.ms, 1) if detector else None, "fps": FPS,
            "cameras": {cid: {"name": w.cam["name"], "frames": w.frames, "events": w.events, "alive": w.is_alive(),
                              "error": w.error, "rules": len(w.engine.rules),
                              "objects": len([t for t in w.engine.tracks if t.hits >= 2]), "tagging": w.tag_on}
                        for cid, w in workers.items()},
            "tagging": {"ready": tagger is not None, "error": tagger_error, "queue": tag_queue.qsize(), **tag_stats}}


@app.get("/tags")
async def tags():
    """The appearance tags people can get, by group (for search filters and rules)."""
    return {"groups": tg.taxonomy(), "ready": tagger is not None, "error": tagger_error}


@app.get("/sightings")
async def search_sightings(camera: list[int] = Query([]), tag: list[str] = Query([]), since: float | None = None,
                           until: float | None = None, before: float | None = None, limit: int = 120):
    """People seen by the cameras, newest first, filtered by camera, time and tags."""
    if not sightings:
        raise HTTPException(503, "The sightings store is not available")
    rows = await asyncio.to_thread(sightings.search, camera, [t for t in tag if re.fullmatch(r"[a-z_]+:[a-z_]+", t)],
                                   since, until, max(1, min(limit, 500)), before)
    for r in rows:
        r["labels"] = [tg.tag_label(t) for t in r["tags"]]
    return rows


@app.get("/sightings/{sid}.jpg")
async def sighting_image(sid: str):
    f = sightings.image(sid) if sightings and re.fullmatch(r"[a-f0-9]{8,32}", sid) else None
    if not f or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


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
