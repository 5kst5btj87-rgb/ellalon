"""Analytics module: one worker thread per camera (cameras with analytics=true).

Each worker reads the substream (falls back to main stream) at ANALYTICS_FPS and runs every
detector in detectors.DETECTORS, publishing detections as platform events.
"""
import asyncio
import os
import threading
import time

import cv2

from detectors import DETECTORS
from ellalon_sdk import EllalonModule

FPS = float(os.environ.get("ANALYTICS_FPS", "5"))
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

m = EllalonModule(version="0.1.0")
app = m.app
workers: dict[int, "Worker"] = {}
loop: asyncio.AbstractEventLoop | None = None


class Worker(threading.Thread):
    def __init__(self, cam: dict):
        super().__init__(daemon=True)
        self.cam, self.stop_flag = cam, threading.Event()
        self.detectors = [D(cam) for D in DETECTORS]
        self.frames = 0

    def run(self):
        url = self.cam.get("substream_url") or self.cam["url"]
        while not self.stop_flag.is_set():
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                time.sleep(10)
                continue
            last = 0.0
            while not self.stop_flag.is_set():
                ok = cap.grab()
                if not ok:
                    break
                if time.time() - last < 1 / FPS:
                    continue
                last = time.time()
                ok, frame = cap.retrieve()
                if not ok:
                    break
                self.frames += 1
                for d in self.detectors:
                    for det in d.process(frame):
                        asyncio.run_coroutine_threadsafe(
                            m.publish(f"analytics.{det['type']}",
                                      {"camera": self.cam["name"], "detector": d.name, **det["data"]},
                                      det["severity"], camera_id=self.cam["id"]), loop)
            cap.release()
            time.sleep(3)


async def sync_cameras():
    cams = {c["id"]: c for c in await m.cameras() if c["enabled"] and c["analytics"]}
    for cid in list(workers):
        if cid not in cams:
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


@m.command("status")
async def status(_):
    return {cid: {"name": w.cam["name"], "frames": w.frames, "alive": w.is_alive()}
            for cid, w in workers.items()}
