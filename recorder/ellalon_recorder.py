#!/usr/bin/env python3
"""Ellalon recording server (Ubuntu / Windows). Standard library only.

Records the cameras that the Ellalon management server assigns to this recording server
(Storage tab -> recording server; Cameras -> Recording storage).

* Keeps its camera list in a local file, so it keeps recording - and starts recording after a
  reboot - even when the management server cannot be reached.
* Runs as a service: systemd on Ubuntu, a SYSTEM startup task on Windows (see the installers).
* Serves playback to the management server on TCP 8090 (protected by a per-recorder secret).

    ellalon_recorder.py register --server http://ELLALON-SERVER --code XXXX-XXXX
    ellalon_recorder.py run
    ellalon_recorder.py status
"""
import argparse
import json
import logging
import logging.handlers
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.2.0"
WINDOWS = os.name == "nt"
HOME = (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "EllalonRecorder" if WINDOWS
        else Path("/etc/ellalon-recorder"))
CONFIG = Path(os.environ.get("ELLALON_RECORDER_CONFIG", HOME / "recorder.json"))
DEFAULT_PATH = r"C:\EllalonRecordings" if WINDOWS else "/var/lib/ellalon-recorder/recordings"
SEGMENT = 300            # seconds per file
SYNC_EVERY = int(os.environ.get("ELLALON_RECORDER_SYNC", "30"))   # seconds between config pulls / reports
log = logging.getLogger("ellalon-recorder")


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def load(path: Path = CONFIG) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}


def save(cfg: dict, path: Path = CONFIG):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    if not WINDOWS:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)          # atomic: a power cut never leaves a half-written file


def api(server: str, method: str, path: str, token: str | None = None, body=None, timeout: float = 10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": f"ellalon-recorder/{VERSION}"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def ffmpeg_path() -> str:
    local = Path(__file__).resolve().parent / ("ffmpeg.exe" if WINDOWS else "ffmpeg")
    return str(local) if local.exists() else (shutil.which("ffmpeg") or "ffmpeg")


# ---------------------------------------------------------------- one camera
class CameraRecorder(threading.Thread):
    def __init__(self, cam: dict, root: Path):
        super().__init__(daemon=True, name=f"camera-{cam['id']}")
        self.cam, self.root = dict(cam), Path(root)
        self.stop_evt = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.proc_started = 0.0
        self.restarts, self.error = 0, ""
        self.audio = True          # set to False if this camera's audio cannot be recorded

    @property
    def folder(self) -> Path:
        return self.root / str(self.cam["id"])

    def command(self) -> list[str]:
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.cam["url"].startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp", "-timeout", "10000000"]
        return cmd + ["-i", self.cam["url"], "-map", "0:v", *(["-map", "0:a?"] if self.audio else []),
                      "-c:v", "copy", *(["-c:a", "aac", "-b:a", "64k"] if self.audio else []),
                      "-f", "segment", "-segment_time", str(SEGMENT), "-segment_atclocktime", "1",
                      "-reset_timestamps", "1", "-strftime", "1", "-segment_format", "mp4",
                      "-segment_format_options", "movflags=+frag_keyframe+empty_moov:flush_packets=1",
                      str(self.folder / "%Y%m%d-%H%M%S.mp4")]

    def run(self):
        backoff = 5
        while not self.stop_evt.is_set():
            t0 = time.time()
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                self.proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.PIPE,
                                             creationflags=0x08000000 if WINDOWS else 0)  # no console window
                self.proc_started = time.time()
                _, err = self.proc.communicate()
                self.error = ((err or b"").decode(errors="replace").strip()[-300:]
                              or f"ffmpeg stopped (exit code {self.proc.returncode})")
            except OSError as e:
                self.error = f"cannot start ffmpeg or write to {self.folder}: {e}"
            if self.stop_evt.is_set():
                break
            self.restarts += 1
            if time.time() - t0 < 30:
                drop_empty(self.folder, t0)
                if self.audio and any(h in self.error.lower() for h in AUDIO_HINTS):
                    self.audio = False
                    self.error += " - recording video only from now on"
            backoff = 5 if time.time() - t0 > 60 else min(60, backoff * 2)
            log.warning("camera %s (%s): %s - retrying in %ss", self.cam["id"], self.cam.get("name"),
                        self.error.splitlines()[-1] if self.error else "stopped", backoff)
            self.stop_evt.wait(backoff)

    def stop(self):
        self.stop_evt.set()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()

    def state(self) -> dict:
        newest = 0.0
        try:
            with os.scandir(self.folder) as it:
                newest = max((e.stat().st_mtime for e in it if e.name.endswith(".mp4")), default=0.0)
        except OSError:
            pass
        alive = self.proc is not None and self.proc.poll() is None
        if alive and time.time() - newest < 90:
            st = "recording"
        elif alive and time.time() - self.proc_started < 60:
            st = "starting"
        else:
            st = "failing"
        return {"name": self.cam.get("name"), "state": st, "restarts": self.restarts,
                "error": "" if st == "recording" else self.error, "last_write": iso(newest)}


# ---------------------------------------------------------------- the recording server
class Agent:
    def __init__(self, cfg_path: Path = CONFIG):
        self.cfg_path = cfg_path
        self.cfg = load(cfg_path)
        if not self.cfg.get("token"):
            sys.exit("This recording server is not paired. Run:  ellalon_recorder.py register "
                     "--server http://ELLALON-SERVER --code XXXX-XXXX")
        self.recorders: dict[int, CameraRecorder] = {}
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.online, self.last_error, self.last_sync = False, "", None
        self.disk: dict = {}

    @property
    def root(self) -> Path:
        return Path(self.cfg.get("path") or DEFAULT_PATH)

    def apply(self):
        """Start / stop / restart camera recorders to match the (cached) camera list."""
        cams = {int(c["id"]): c for c in self.cfg.get("cameras", [])}
        with self.lock:
            for cid in list(self.recorders):
                r = self.recorders[cid]
                if cid not in cams or r.cam["url"] != cams[cid]["url"] or r.root != self.root:
                    self.recorders.pop(cid).stop()
            for cid, cam in cams.items():
                if cid in self.recorders:
                    self.recorders[cid].cam["name"] = cam.get("name")
                else:
                    self.recorders[cid] = CameraRecorder(cam, self.root)
                    self.recorders[cid].start()
        log.info("recording %d camera(s) to %s", len(cams), self.root)

    def sync(self):
        """Pull the camera list (and save it), then report status. Never stops recording on errors."""
        server, rid, token = self.cfg["server"], self.cfg["recorder_id"], self.cfg["token"]
        try:
            conf = api(server, "GET", f"/api/recorders/{rid}/config", token)
        except urllib.error.HTTPError as e:
            if e.code == 410:      # removed from Ellalon or paired again on another machine
                if self.cfg.get("cameras"):
                    log.error("the management server removed this recording server - recording stopped")
                    self.cfg["cameras"] = []
                    save(self.cfg, self.cfg_path)
                    self.apply()
                return
            return self._offline(f"HTTP {e.code}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            return self._offline(getattr(e, "reason", e))
        if not self.online:
            log.info("connected to the management server %s", server)
        self.online, self.last_error, self.last_sync = True, "", time.time()
        fresh = {"cameras": conf.get("cameras", []), "path": conf.get("path") or "", "name": conf.get("name"),
                 "retention_days": conf.get("retention_days", 30), "min_free_gb": conf.get("min_free_gb", 20),
                 "max_gb": conf.get("max_gb", 0)}
        if any(self.cfg.get(k) != v for k, v in fresh.items()):
            self.cfg.update(fresh)
            save(self.cfg, self.cfg_path)
            self.apply()
        try:
            api(server, "POST", f"/api/recorders/{rid}/status", token, self.status())
        except Exception as e:
            log.debug("status report failed: %s", e)

    def _offline(self, why):
        why = str(why)
        if self.online or why != self.last_error:
            log.warning("management server unreachable (%s) - recording continues from the saved camera list", why)
        self.online, self.last_error = False, why

    def retention(self):
        if self.root.is_dir():
            self.disk = apply_retention(self.root, self.cfg)

    def status(self) -> dict:
        with self.lock:
            cams = {str(cid): r.state() for cid, r in self.recorders.items()}
        return {"version": VERSION, "os": f"{platform.system()} {platform.release()}",
                "hostname": socket.gethostname(), "port": int(self.cfg.get("port", 8090)),
                "path": str(self.root), "disk": self.disk, "cameras": cams,
                "management_online": self.online, "last_sync": iso(self.last_sync)}

    def _loop(self, fn, every: float):
        while not self.stop_evt.is_set():
            try:
                fn()
            except Exception:
                log.exception("%s failed", fn.__name__)
            self.stop_evt.wait(every)

    def run(self):
        log.info("Ellalon recorder %s starting (%s) - %d camera(s) in the saved list",
                 VERSION, self.root, len(self.cfg.get("cameras", [])))
        self.apply()            # record immediately from the saved list - no server needed
        srv = None
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", int(self.cfg.get("port", 8090))), make_handler(self))
            threading.Thread(target=srv.serve_forever, daemon=True, name="playback").start()
        except OSError as e:
            log.error("playback port %s unavailable: %s (recording is not affected)", self.cfg.get("port"), e)
        threading.Thread(target=self._loop, args=(self.retention, 300), daemon=True, name="retention").start()
        threading.Thread(target=self._loop, args=(self.sync, SYNC_EVERY), daemon=True, name="sync").start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.stop_evt.set())
            except (ValueError, OSError):
                pass
        while not self.stop_evt.wait(1):
            pass
        log.info("stopping")
        with self.lock:
            for r in self.recorders.values():
                r.stop()
        if srv:
            srv.shutdown()


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
            "oldest": iso(left[0][0]) if left else None}


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


# ---------------------------------------------------------------- playback server
def make_handler(agent: Agent):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"EllalonRecorder/{VERSION}"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.headers.get("Authorization") != f"Bearer {agent.cfg.get('secret')}":
                return self.send_error(401)
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/status":
                return self._json(agent.status())
            if u.path == "/recordings":
                cid, day = q.get("camera", [""])[0], q.get("day", [""])[0]
                if not cid.isdigit() or (day and not re.fullmatch(r"\d{8}", day)):
                    return self.send_error(400)
                d, out = agent.root / cid, []
                for f in sorted(d.glob(f"{day}-*.mp4" if day else "*.mp4")) if d.is_dir() else []:
                    try:
                        start = datetime.strptime(f.stem, "%Y%m%d-%H%M%S")
                    except ValueError:
                        continue
                    out.append({"file": f.name, "start": start.isoformat(), "seconds": SEGMENT,
                                "bytes": f.stat().st_size})
                return self._json(out)
            m = re.fullmatch(r"/files/(\d+)/(\d{8}-\d{6}\.mp4)", u.path)
            if m:
                return self._file(agent.root / m.group(1) / m.group(2))
            m = re.fullmatch(r"/clip/(\d+)", u.path)
            if m:
                try:
                    secs = int(q.get("seconds", ["60"])[0])
                    end = q.get("end", [None])[0]
                    out = cut_clip([agent.root / m.group(1)], secs, float(end) if end else None, ffmpeg_path(), SEGMENT)
                except (ValueError, NoRecording) as e:
                    return self._json({"detail": str(e)}, 404)
                try:
                    return self._file(out)
                finally:
                    out.unlink(missing_ok=True)
                    out.parent.rmdir()
            self.send_error(404)

        def _file(self, f: Path):
            if not f.is_file():
                return self.send_error(404)
            size, start, end, code = f.stat().st_size, 0, None, 200
            rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "") or "")
            end = size - 1
            if rng and (rng.group(1) or rng.group(2)):
                if rng.group(1):
                    start = int(rng.group(1))
                    end = int(rng.group(2)) if rng.group(2) else size - 1
                else:
                    start = max(0, size - int(rng.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(f, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(65536, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)

    return Handler


# ---------------------------------------------------------------- command line
def setup_logging(to_file: bool):
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if to_file:
        HOME.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(HOME / "recorder.log", maxBytes=5_000_000,
                                                             backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")


def cmd_register(a):
    body = {"code": a.code, "hostname": socket.gethostname(), "os": f"{platform.system()} {platform.release()}",
            "port": a.port, "version": VERSION}
    try:
        r = api(a.server, "POST", "/api/recorders/register", body=body, timeout=20)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            detail = f"HTTP {e.code}"
        sys.exit(f"Pairing failed: {detail}")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"Cannot reach the Ellalon server at {a.server}: {getattr(e, 'reason', e)}")
    old = load(CONFIG)
    cfg = {"server": a.server.rstrip("/"), "recorder_id": r["id"], "name": r["name"], "token": r["token"],
           "secret": r["secret"], "port": a.port, "path": r.get("path") or "",
           "cameras": old.get("cameras", []) if old.get("recorder_id") == r["id"] else [],
           "retention_days": 30, "min_free_gb": 20}
    save(cfg)
    print(f"Paired as recording server '{r['name']}' with {a.server}")


def cmd_status(_):
    cfg = load(CONFIG)
    if not cfg:
        sys.exit("Not paired")
    print(f"Recording server '{cfg.get('name')}' - management server {cfg.get('server')}")
    print(f"Folder: {cfg.get('path') or DEFAULT_PATH}   Cameras in saved list: {len(cfg.get('cameras', []))}")
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{cfg.get('port', 8090)}/status",
                                     headers={"Authorization": f"Bearer {cfg.get('secret')}"})
        st = json.loads(urllib.request.urlopen(req, timeout=5).read())
        print(f"Management server reachable: {st['management_online']}")
        for cid, c in st["cameras"].items():
            print(f"  camera {cid:>4} {c['name'] or '':<24} {c['state']:<10} {c['error'][:80]}")
    except Exception as e:
        print(f"Service not answering on port {cfg.get('port', 8090)}: {e}")


def main():
    ap = argparse.ArgumentParser(description="Ellalon recording server")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register", help="pair with the Ellalon management server")
    r.add_argument("--server", required=True)
    r.add_argument("--code", required=True)
    r.add_argument("--port", type=int, default=8090)
    sub.add_parser("run", help="record (used by the service)")
    sub.add_parser("status", help="show what is being recorded")
    a = ap.parse_args()
    if a.cmd == "register":
        cmd_register(a)
    elif a.cmd == "status":
        cmd_status(a)
    else:
        setup_logging(to_file=WINDOWS)
        Agent().run()


if __name__ == "__main__":
    main()
