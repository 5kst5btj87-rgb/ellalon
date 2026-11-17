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
from fastapi import File, Form, HTTPException, UploadFile

from ellalon_sdk import EllalonModule

DATA = Path("/data")
THRESH = float(os.environ.get("MATCH_THRESHOLD", "0.45"))
FPS = float(os.environ.get("FACE_FPS", "2"))
ENGINE = os.environ.get("FACE_ENGINE", "insightface")
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

m = EllalonModule(version="0.1.0")
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
                        "camera": cam["name"], "person_id": pid, "name": p["name"],
                        "list": p.get("list"), "similarity": round(sim, 3), "bbox": f["bbox"]},
                        "high", camera_id=cam["id"]), loop)
        cap.release()
        time.sleep(3)


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
    engine = await asyncio.to_thread(FaceEngine)
    m.status.update(engine=ENGINE, ready=engine.ready, people=len(watchlist.people))


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
@app.get("/people")
def people():
    return [{"id": k, "name": v["name"], "list": v.get("list"), "samples": len(v["embeddings"])}
            for k, v in watchlist.people.items()]


@app.post("/people")
async def enroll(name: str = Form(...), list_name: str = Form("default"),
                 photos: list[UploadFile] = File(...)):
    if not (engine and engine.ready):
        raise HTTPException(503, "Face engine not available")
    embs = []
    for ph in photos:
        img = cv2.imdecode(np.frombuffer(await ph.read(), np.uint8), cv2.IMREAD_COLOR)
        faces = await asyncio.to_thread(engine.faces, img) if img is not None else []
        if len(faces) != 1:
            raise HTTPException(400, f"{ph.filename}: expected exactly 1 face, found {len(faces)}")
        embs.append(faces[0]["embedding"].tolist())
    pid = uuid.uuid4().hex[:12]
    watchlist.people[pid] = {"name": name, "list": list_name, "embeddings": embs}
    watchlist.save()
    m.status["people"] = len(watchlist.people)
    await m.publish("watchlist.enrolled", {"person_id": pid, "name": name, "list": list_name})
    return {"id": pid}


@app.delete("/people/{pid}")
def remove(pid: str):
    watchlist.people.pop(pid, None)
    watchlist.save()
    return {"ok": True}
