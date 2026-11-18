"""Face recognition module.

* Watchlist: enroll people with one or more photos -> normalized embeddings stored in /data.
* Live matching: for cameras with face_recognition=true, detect faces and compare against the
  watchlist (cosine similarity); publishes face.match (high) / face.detected (info) events.
* Engine is pluggable: InsightFace (buffalo_l) by default; replace FaceEngine for another vendor.

NOTE: biometric processing is regulated in most jurisdictions (e.g. GDPR art. 9, Israel's
Privacy Protection Law). Deploy with a legal basis, signage, retention limits and access control.
"""
import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
from datetime import datetime

from fastapi import File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ellalon_sdk import EllalonModule

DATA = Path("/data")
THRESH = float(os.environ.get("MATCH_THRESHOLD", "0.45"))
FPS = float(os.environ.get("FACE_FPS", "2"))
ENGINE = os.environ.get("FACE_ENGINE", "insightface")
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

m = EllalonModule(version="0.2.1")
app = m.app


class FaceEngine:
    def __init__(self):
        self.model = None
        if ENGINE.startswith("insightface"):
            from insightface.app import FaceAnalysis
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            self.model = FaceAnalysis(name="buffalo_l", root=str(DATA / "models"), providers=providers)
            self.model.prepare(ctx_id=0, det_size=(640, 640))

    @property
    def ready(self):
        return self.model is not None

    def faces(self, img: np.ndarray) -> list[dict]:
        if not self.ready:
            raise RuntimeError("No face engine in this image (built with ENGINE=none)")
        return [{"bbox": f.bbox.astype(int).tolist(), "score": float(f.det_score),
                 "embedding": f.normed_embedding} for f in self.model.get(img)]


def enroll_variants(img: np.ndarray):
    """Close-up / passport photos: the detector misses a face that fills the frame, so also try
    the photo with a margin around it and at a smaller scale."""
    yield img
    h, w = img.shape[:2]
    for pad in (0.35, 0.8):
        py, px = int(h * pad), int(w * pad)
        yield cv2.copyMakeBorder(img, py, py, px, px, cv2.BORDER_CONSTANT, value=(127, 127, 127))
    if max(h, w) > 400:                                    # very sharp, very large close-up
        k = 300 / max(h, w)
        small = cv2.resize(img, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
        yield cv2.copyMakeBorder(small, 170, 170, 170, 170, cv2.BORDER_CONSTANT, value=(127, 127, 127))


def enroll_face(img: np.ndarray):
    """Returns (image, face) for the one person in the photo, or (None, number_of_faces)."""
    found = 0
    for v in enroll_variants(img):
        faces = [f for f in engine.faces(v) if f["score"] >= 0.4]
        if not faces:
            continue
        area = lambda f: (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1])
        faces.sort(key=area, reverse=True)
        if len(faces) == 1 or area(faces[0]) >= 3 * area(faces[1]):   # one clear main face
            return v, faces[0]
        found = len(faces)
    return None, found


class Watchlist:
    def __init__(self):
        self.file = DATA / "watchlist.json"
        self.people: dict[str, dict] = json.loads(self.file.read_text()) if self.file.exists() else {}
        self._matrix()

    def _matrix(self):
        self.ids, vecs = [], []
        for pid, p in self.people.items():
            for e in p["embeddings"]:
                self.ids.append(pid)
                vecs.append(e)
        self.M = np.array(vecs, dtype=np.float32) if vecs else np.zeros((0, 512), np.float32)

    def save(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(self.people))
        self._matrix()

    def match(self, emb: np.ndarray):
        if not len(self.M):
            return None, 0.0
        sims = self.M @ emb
        i = int(np.argmax(sims))
        return (self.ids[i], float(sims[i])) if sims[i] >= THRESH else (None, float(sims[i]))


engine: FaceEngine | None = None
engine_error = ""
watchlist = Watchlist()
workers: dict[int, threading.Thread] = {}
stops: dict[int, threading.Event] = {}
loop = None


def worker(cam: dict, stop: threading.Event):
    url = cam.get("substream_url") or cam["url"]
    seen: dict[str, float] = {}
    while not stop.is_set():
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        last = 0.0
        while cap.isOpened() and not stop.is_set():
            if not cap.grab():
                break
            if time.time() - last < 1 / FPS:
                continue
            last = time.time()
            ok, frame = cap.retrieve()
            if not ok:
                break
            for f in engine.faces(frame):
                pid, sim = watchlist.match(f["embedding"])
                if pid and time.time() - seen.get(pid, 0) > 30:     # de-duplicate per person
                    seen[pid] = time.time()
                    p = watchlist.people[pid]
                    asyncio.run_coroutine_threadsafe(m.publish("face.match", {
                        "title": f"Face match: {p['name']} - {cam['name']}", "event": f"face-{pid}",
                        "camera": cam["name"], "person_id": pid, "name": p["name"],
                        "list": p.get("list"), "similarity": round(sim, 3), "bbox": f["bbox"],
                        "snapshot": save_match(frame, f["bbox"], p["name"], cam["id"])},
                        p.get("severity") or "high", camera_id=cam["id"]), loop)
        cap.release()
        time.sleep(3)


def save_match(frame, bbox, name, cid) -> str | None:
    try:
        img = frame.copy()
        x1, y1, x2, y2 = (int(v) for v in bbox)
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 3)
        cv2.putText(img, name, (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        folder = DATA / "matches"
        folder.mkdir(parents=True, exist_ok=True)
        fn = f"{datetime.now():%Y%m%d-%H%M%S}-{cid}-{uuid.uuid4().hex[:4]}.jpg"
        cv2.imwrite(str(folder / fn), img, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return f"/modules/face-recognition/matches/{fn}"
    except Exception:
        return None


def save_face(img, bbox, pid):
    """Keep a square crop of the enrolled face for the watch list."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in bbox)
    pad = int(0.35 * max(x2 - x1, y2 - y1))
    crop = img[max(0, y1 - pad):min(h, y2 + pad), max(0, x1 - pad):min(w, x2 + pad)]
    (DATA / "faces").mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(DATA / "faces" / f"{pid}.jpg"), cv2.resize(crop, (200, 200)), [cv2.IMWRITE_JPEG_QUALITY, 85])


async def sync_cameras():
    if not (engine and engine.ready):
        return
    cams = {c["id"]: c for c in await m.cameras() if c["enabled"] and c["face_recognition"]}
    for cid in list(workers):
        if cid not in cams:
            stops.pop(cid).set()
            workers.pop(cid)
    for cid, cam in cams.items():
        if cid not in workers:
            stops[cid] = threading.Event()
            workers[cid] = threading.Thread(target=worker, args=(cam, stops[cid]), daemon=True)
            workers[cid].start()
    m.status["cameras"] = len(workers)


@m.on_startup
async def _start():
    global engine, loop
    loop = asyncio.get_running_loop()
    try:
        engine = await asyncio.to_thread(FaceEngine)        # first start downloads the face model
    except Exception as e:
        global engine_error
        engine_error = f"{e.__class__.__name__}: {str(e)[:200]}"
        m.log.error("face engine failed: %s", engine_error)
        engine = FaceEngine.__new__(FaceEngine)
        engine.model = None
    m.status.update(engine=ENGINE, ready=engine.ready, people=len(watchlist.people), error=engine_error)


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


# ---------------------------------------------------------------- watchlist API
@app.get("/status")
def status():
    return {"engine": ENGINE, "ready": bool(engine and engine.ready), "starting": engine is None, "error": engine_error,
            "cameras": len(workers), "people": len(watchlist.people), "threshold": THRESH}


@app.get("/people")
def people():
    return [{"id": k, "name": v["name"], "list": v.get("list"), "severity": v.get("severity", "high"),
             "samples": len(v["embeddings"]), "photo": (DATA / "faces" / f"{k}.jpg").is_file()}
            for k, v in sorted(watchlist.people.items(), key=lambda kv: kv[1]["name"].lower())]


@app.post("/people")
async def enroll(name: str = Form(...), list_name: str = Form("Watch list"), severity: str = Form("high"),
                 photos: list[UploadFile] = File(...)):
    if not (engine and engine.ready):
        raise HTTPException(503, f"Face engine not available: {engine_error}" if engine_error else
                            "The face engine is still starting (first start downloads the model) - try again in a minute")
    if severity not in ("info", "warning", "high", "critical"):
        severity = "high"
    embs, first = [], None
    for ph in photos:
        img = cv2.imdecode(np.frombuffer(await ph.read(), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(400, f"{ph.filename}: not a picture (use JPG or PNG)")
        used, face = await asyncio.to_thread(enroll_face, img)
        if used is None:
            raise HTTPException(400, f"{ph.filename}: no face found - use a clear, front-facing photo with the whole head visible"
                                if not face else f"{ph.filename}: {face} faces of similar size - use a photo of this person alone")
        embs.append(face["embedding"].tolist())
        first = first or (used, face["bbox"])
    pid = uuid.uuid4().hex[:12]
    watchlist.people[pid] = {"name": name.strip() or "Unnamed", "list": list_name.strip() or "Watch list",
                             "severity": severity, "embeddings": embs}
    save_face(*first, pid)
    watchlist.save()
    m.status["people"] = len(watchlist.people)
    await m.publish("watchlist.enrolled", {"person_id": pid, "name": name, "list": list_name})
    return {"id": pid}


@app.get("/people/{pid}/photo")
def person_photo(pid: str):
    f = DATA / "faces" / f"{pid}.jpg"
    if pid not in watchlist.people or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg")


@app.delete("/people/{pid}")
def remove(pid: str):
    watchlist.people.pop(pid, None)
    (DATA / "faces" / f"{pid}.jpg").unlink(missing_ok=True)
    watchlist.save()
    m.status["people"] = len(watchlist.people)
    return {"ok": True}


@app.get("/matches/{name}")
def match_photo(name: str):
    f = DATA / "matches" / name
    if "/" in name or ".." in name or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg")
