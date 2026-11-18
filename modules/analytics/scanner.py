"""Search recordings for people: decode a time range of a camera's recordings, detect and track people,
tag the best view of each person and keep those that match the wanted tags.

Speed: "fast" decodes only key frames (one every 1-4 s on most cameras, very cheap), "thorough"
decodes everything and analyses one frame per second.
"""
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import httpx
import numpy as np

from detectors import Tracker, tags_match

SEGMENT_GUESS = 300
INFO = re.compile(r"pts_time:\s*([-\d.]+).*?\bs:(\d+)x(\d+)")


class Scan:
    def __init__(self, folder: Path, cam: dict, start: float, end: float, tags: list[str], mode: str, min_height: int):
        self.id = uuid.uuid4().hex[:12]
        self.folder = folder / self.id
        self.cam, self.start, self.end, self.tags, self.mode, self.min_height = cam, start, end, tags, mode, min_height
        self.created = time.time()
        self.state, self.error = "queued", ""
        self.done_s, self.total_s = 0.0, max(1.0, end - start)
        self.people = 0                     # people looked at
        self.results: list[dict] = []
        self.cancelled = False
        self.segments = 0

    def view(self) -> dict:
        return {"id": self.id, "camera_id": self.cam["id"], "camera": self.cam["name"], "start": self.start, "end": self.end,
                "tags": self.tags, "mode": self.mode, "state": self.state, "error": self.error, "created": self.created,
                "progress": round(min(1.0, self.done_s / self.total_s), 3), "people": self.people,
                "segments": self.segments, "results": self.results}


# ---------------------------------------------------------------- where the recordings are
def list_segments(cam: dict, start: float, end: float, core_url: str, token: str) -> list[dict]:
    """Recorded segments of the camera that overlap [start, end], from the local recorder and/or the
    camera's recording server. Each: {t (unix start), url, headers}."""
    days, d = set(), datetime.fromtimestamp(start - SEGMENT_GUESS)
    while d.timestamp() <= end:
        days.add(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    days.add(datetime.fromtimestamp(end).strftime("%Y%m%d"))
    segs, auth = [], {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=15) as c:
        for day in sorted(days):
            try:
                r = c.get(f"http://vms-recorder:8000/cameras/{cam['id']}/recordings", params={"day": day})
                if r.status_code == 200:
                    segs += [{"t": datetime.fromisoformat(x["start"]).timestamp(), "url": f"http://vms-recorder:8000/{x['url']}",
                              "headers": {}} for x in r.json()]
            except httpx.HTTPError:
                pass
            sid = (cam.get("settings") or {}).get("storage_id")
            if sid:
                try:
                    r = c.get(f"{core_url}/api/recorders/{sid}/recordings", params={"camera": cam["id"], "day": day}, headers=auth)
                    if r.status_code == 200:
                        segs += [{"t": datetime.fromisoformat(x["start"]).timestamp(), "url": core_url + x["url"],
                                  "headers": auth} for x in r.json()]
                except httpx.HTTPError:
                    pass
    segs.sort(key=lambda s: s["t"])
    out = []
    for i, s in enumerate(segs):
        nxt = segs[i + 1]["t"] if i + 1 < len(segs) else s["t"] + SEGMENT_GUESS + 5
        s["until"] = min(nxt, s["t"] + SEGMENT_GUESS * 4)
        if s["t"] < end and s["until"] > start and (not out or out[-1]["t"] != s["t"]):
            out.append(s)
    return out


def download(seg: dict, dest: Path):
    with httpx.Client(timeout=httpx.Timeout(30, read=120)) as c, c.stream("GET", seg["url"], headers=seg["headers"]) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)


# ---------------------------------------------------------------- decoding
def frames(path: Path, offset: float, duration: float, mode: str, stop):
    """Yields (seconds from `offset`, BGR frame) using ffmpeg; key frames only in fast mode."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info"]
    if mode == "fast":
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-ss", f"{max(0.0, offset):.2f}", "-i", str(path), "-t", f"{max(1.0, duration):.2f}", "-an", "-sn"]
    vf = ("fps=1," if mode != "fast" else "") + "showinfo"
    cmd += ["-vf", vf, "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    infos: queue.Queue = queue.Queue()

    def read_err():
        for line in iter(p.stderr.readline, b""):
            m = INFO.search(line.decode(errors="replace"))
            if m and "showinfo" in line.decode(errors="replace"):
                infos.put((float(m.group(1)), int(m.group(2)), int(m.group(3))))
        infos.put(None)
    threading.Thread(target=read_err, daemon=True).start()
    try:
        while not stop():
            info = infos.get(timeout=120)
            if info is None:
                break
            pts, w, h = info
            need, buf = w * h * 3, bytearray()
            while len(buf) < need:
                chunk = p.stdout.read(need - len(buf))
                if not chunk:
                    return
                buf += chunk
            yield pts, np.frombuffer(bytes(buf), np.uint8).reshape(h, w, 3)
    except queue.Empty:
        return
    finally:
        p.kill()
        p.wait()


# ---------------------------------------------------------------- the scan
def run_scan(scan: Scan, detector, tagger, regions, core_url: str, token: str, log):
    scan.state = "running"
    tmp = scan.folder / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        if detector is None:
            raise RuntimeError("the people detector is not available in the analytics app")
        if tagger is None:
            raise RuntimeError("people tagging is not available (model missing - rebuild the analytics app)")
        segs = list_segments(scan.cam, scan.start, scan.end, core_url, token)
        scan.segments = len(segs)
        if not segs:
            raise RuntimeError("no recordings of this camera in that time")
        tracker, step = Tracker(max_age=6.0 if scan.mode == "fast" else 3.0), 0
        best: dict[int, dict] = {}                       # track id -> best view so far
        last_seen: dict[int, float] = {}

        def finish(tid):
            b = best.pop(tid, None)
            last_seen.pop(tid, None)
            if not b:
                return
            scan.people += 1
            res = tagger.describe(b["crop"], b["box"])
            if scan.tags and not tags_match(scan.tags, set(res["tags"])):
                return
            for r in scan.results[-6:]:                  # the same person split in two tracks
                if abs(r["ts"] - b["ts"]) < 8 and set(r["tags"]) == set(res["tags"]):
                    return
            name = f"{len(scan.results):04d}.jpg"
            cv2.imwrite(str(scan.folder / name), b["thumb"], [cv2.IMWRITE_JPEG_QUALITY, 85])
            scan.results.append({"ts": round(b["ts"], 1), "tags": res["tags"], "scores": res["scores"],
                                 "image": f"/modules/analytics/scans/{scan.id}/{name}"})

        covered = scan.start
        for seg in segs:
            if scan.cancelled:
                break
            a, b_ = max(scan.start, seg["t"]), min(scan.end, seg["until"])
            if b_ <= a:
                continue
            f = tmp / "seg.mp4"
            try:
                download(seg, f)
            except Exception as e:
                log.warning("scan %s: cannot fetch %s: %s", scan.id, seg["url"], e)
                continue
            base = a
            for pts, frame in frames(f, a - seg["t"], b_ - a, scan.mode, lambda: scan.cancelled):
                now = base + pts
                scan.done_s = max(scan.done_s, now - scan.start)
                dets = [d for d in detector.detect(frame, 0.4) if d["cls"] == "person"]
                tracks = tracker.update(dets, now)
                h, w = frame.shape[:2]
                for t in tracks:
                    x1, y1, x2, y2 = t.box
                    last_seen[t.id] = now
                    if y2 - y1 < scan.min_height or y1 <= 2 or y2 >= h - 2:
                        continue
                    area = (x2 - x1) * (y2 - y1)
                    if t.id in best and area <= best[t.id]["area"]:
                        continue
                    fx1, fy1, fx2, fy2 = regions(t.box, w, h)["full"]
                    crop = frame[fy1:fy2, fx1:fx2].copy()
                    thumb = crop if crop.shape[0] <= 320 else cv2.resize(crop, (max(1, int(crop.shape[1] * 320 / crop.shape[0])), 320))
                    best[t.id] = {"area": area, "ts": now, "crop": crop, "thumb": thumb,
                                  "box": (x1 - fx1, y1 - fy1, x2 - fx1, y2 - fy1)}
                for tid in [k for k, v in last_seen.items() if now - v > tracker.max_age]:
                    finish(tid)
                step += 1
            f.unlink(missing_ok=True)
            covered = b_
            scan.done_s = max(scan.done_s, covered - scan.start)
        for tid in list(best):
            finish(tid)
        scan.results.sort(key=lambda r: r["ts"])
        scan.done_s = scan.total_s if not scan.cancelled else scan.done_s
        scan.state = "cancelled" if scan.cancelled else "done"
    except Exception as e:
        scan.state, scan.error = "error", str(e)[:300]
        log.warning("scan %s failed: %s", scan.id, e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
