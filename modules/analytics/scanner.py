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
        self.phase, self.skimmed, self.scenes = "", 0, 0

    def add(self, view: dict, described: list[dict]):
        """Every person is kept (so the filter can change after the search); `views` = tags per view."""
        self.people += 1
        tags = described[0]["tags"]
        for r in self.results[-8:]:                       # the same person split in two tracks
            if abs(r["ts"] - view["ts"]) < 6 and set(r["tags"]) == set(tags):
                return
        name = f"{len(self.results):04d}.jpg"
        crop = view["crop"]
        if crop.shape[0] > 320:
            crop = cv2.resize(crop, (max(1, int(crop.shape[1] * 320 / crop.shape[0])), 320), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(self.folder / name), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        self.results.append({"ts": round(view["ts"], 1), "tags": tags, "views": [d["tags"] for d in described],
                             "scores": described[0]["scores"], "image": f"/modules/analytics/scans/{self.id}/{name}"})

    def matches(self, r) -> bool:
        return not self.tags or any(tags_match(self.tags, set(v)) for v in r.get("views") or [r["tags"]])

    def view(self) -> dict:
        return {"id": self.id, "camera_id": self.cam["id"], "camera": self.cam["name"], "start": self.start, "end": self.end,
                "tags": self.tags, "mode": self.mode, "state": self.state, "error": self.error, "created": self.created,
                "progress": round(min(1.0, self.done_s / self.total_s), 3), "people": len(self.results),
                "matches": sum(1 for r in self.results if self.matches(r)), "phase": self.phase,
                "skimmed": self.skimmed, "scenes": self.scenes, "segments": self.segments, "results": self.results}


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
                r = c.get(f"http://ellalon-vms-recorder:8000/cameras/{cam['id']}/recordings", params={"day": day})
                if r.status_code == 200:
                    segs += [{"t": datetime.fromisoformat(x["start"]).timestamp(), "url": f"http://ellalon-vms-recorder:8000/{x['url']}",
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
def frames(path: Path, offset: float, duration: float, rate, stop):
    """Yields (seconds from `offset`, BGR frame) using ffmpeg. rate="key": key frames only (very fast,
    nothing else is decoded); a number: that many frames per second."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info"]
    if rate == "key":
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-ss", f"{max(0.0, offset):.2f}", "-i", str(path), "-t", f"{max(0.5, duration):.2f}", "-an", "-sn"]
    vf = (f"fps={rate}," if rate != "key" else "") + "showinfo"
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
class People:
    """Tracks people through a run of frames and keeps the best views of each one for tagging."""

    def __init__(self, scan: Scan, tagger, regions, max_age: float, views: int):
        self.scan, self.tagger, self.regions, self.views = scan, tagger, regions, views
        self.tracker = Tracker(max_age=max_age)
        self.cand: dict[int, list[dict]] = {}
        self.last: dict[int, float] = {}

    def frame(self, frame, dets, now):
        h, w = frame.shape[:2]
        for t in self.tracker.update(dets, now):
            self.last[t.id] = now
            x1, y1, x2, y2 = t.box
            if y2 - y1 < self.scan.min_height or y1 <= 2 or y2 >= h - 2:
                continue
            area = (x2 - x1) * (y2 - y1)
            c = self.cand.setdefault(t.id, [])
            if len(c) >= 4 and area <= min(v["area"] for v in c):
                continue
            fx1, fy1, fx2, fy2 = self.regions(t.box, w, h)["full"]
            crop = frame[fy1:fy2, fx1:fx2].copy()
            c.append({"area": area, "ts": now, "crop": crop, "box": (x1 - fx1, y1 - fy1, x2 - fx1, y2 - fy1)})
            c.sort(key=lambda v: -v["area"])
            del c[4:]
        for tid in [k for k, v in self.last.items() if now - v > self.tracker.max_age]:
            self.finish(tid)

    def finish(self, tid):
        views = self.cand.pop(tid, [])
        self.last.pop(tid, None)
        if not views:
            return
        pick = [views[0]]                                  # biggest view, plus others a moment apart
        for v in views[1:]:
            if len(pick) >= self.views:
                break
            if all(abs(v["ts"] - p["ts"]) >= 1.0 for p in pick):
                pick.append(v)
        described = [self.tagger.describe(v["crop"], v["box"]) for v in pick]
        self.scan.add(pick[0], described)

    def close(self):
        for tid in list(self.cand):
            self.finish(tid)
        self.last.clear()


def windows(hits: list[float], keys: list[float], start: float, end: float) -> list[tuple[float, float]]:
    """Scenes to look at closely: from the key frame before each hit to the key frame after it."""
    out = []
    for h in hits:
        prev = max([k for k in keys if k < h - 0.01], default=h - 3)
        nxt = min([k for k in keys if k > h + 0.01], default=h + 3)
        prev, nxt = max(prev, h - 6), min(nxt, h + 6)        # very long key-frame gaps: stay near the hit
        a, b = max(start, min(prev, h - 1)), min(end, max(nxt, h + 1))
        if out and a <= out[-1][1] + 0.5:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def run_scan(scan: Scan, detector, tagger, regions, core_url: str, token: str, log):
    """fast:     1) skim the key frames for people (no tagging), 2) go back to each scene with people
                 and look at it closely (2 frames/s): track, tag the best view of each person.
       accurate: 3 frames/s through the whole time, each person tagged from two different views."""
    scan.state = "running"
    tmp = scan.folder / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    stop = lambda: scan.cancelled
    people_dets = lambda frame, conf: [d for d in detector.detect(frame, conf) if d["cls"] == "person"]
    try:
        if detector is None:
            raise RuntimeError("the people detector is not available in the analytics app")
        if tagger is None:
            raise RuntimeError("people tagging is not available (model missing - rebuild the analytics app)")
        segs = list_segments(scan.cam, scan.start, scan.end, core_url, token)
        scan.segments = len(segs)
        if not segs:
            raise RuntimeError("no recordings of this camera in that time")
        for seg in segs:
            if scan.cancelled:
                break
            a, b = max(scan.start, seg["t"]), min(scan.end, seg["until"])
            if b <= a:
                continue
            f = tmp / "seg.mp4"
            try:
                download(seg, f)
            except Exception as e:
                log.warning("scan %s: cannot fetch %s: %s", scan.id, seg["url"], e)
                continue
            off = seg["t"]
            if scan.mode == "fast":
                keys, hits = [], []
                scan.phase = "skimming"
                for pts, frame in frames(f, a - off, b - a, "key", stop):
                    now = a + pts
                    keys.append(now)
                    scan.done_s = max(scan.done_s, now - scan.start)
                    small = max(30, scan.min_height * 0.6)
                    if any(d["box"][3] - d["box"][1] >= small for d in people_dets(frame, 0.35)):
                        hits.append(now)
                scan.skimmed += len(keys)
                scan.phase = "looking closer"
                for wa, wb in windows(hits, keys, a, b):
                    if scan.cancelled:
                        break
                    scan.scenes += 1
                    ppl = People(scan, tagger, regions, max_age=2.0, views=1)
                    for pts, frame in frames(f, wa - off, wb - wa, 2, stop):
                        ppl.frame(frame, people_dets(frame, 0.4), wa + pts)
                    ppl.close()
            else:
                scan.phase = "analysing"
                ppl = People(scan, tagger, regions, max_age=2.0, views=2)
                for pts, frame in frames(f, a - off, b - a, 3, stop):
                    now = a + pts
                    scan.done_s = max(scan.done_s, now - scan.start)
                    ppl.frame(frame, people_dets(frame, 0.35), now)
                ppl.close()
            f.unlink(missing_ok=True)
            scan.done_s = max(scan.done_s, b - scan.start)
        scan.results.sort(key=lambda r: r["ts"])
        if not scan.cancelled:
            scan.done_s = scan.total_s
        scan.state = "cancelled" if scan.cancelled else "done"
    except Exception as e:
        scan.state, scan.error = "error", str(e)[:300]
        log.warning("scan %s failed: %s", scan.id, e)
    finally:
        scan.phase = ""
        shutil.rmtree(tmp, ignore_errors=True)
