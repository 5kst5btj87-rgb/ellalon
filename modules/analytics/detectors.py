"""Detection building blocks: object detector (YOLOX, ONNX Runtime, CPU), tracker and rule engine.

Rules come from the camera's settings (Cameras tab -> Analytics). Coordinates are fractions 0..1 of the
picture. Rule types:
  intrusion  an object of the chosen kinds is inside the zone (once per object)
  line       an object crosses the line (both ways, or only in the arrow's direction / against it)
  loitering  an object stays inside the zone longer than `seconds`
  motion     more than `sensitivity` % of the zone changes (no object detector needed)
"""
import os
import threading
import time

import cv2
import numpy as np

COCO = ["person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
        "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
        "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
        "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
        "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
        "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
        "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
        "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
        "teddy bear", "hair drier", "toothbrush"]
WANTED = {"person", "bicycle", "car", "motorcycle", "bus", "truck", "dog", "cat", "backpack", "suitcase"}
LABELS = {"intrusion": "Intrusion", "line": "Line crossing", "loitering": "Loitering", "motion": "Motion"}


# ---------------------------------------------------------------- object detector
class ObjectDetector:
    """YOLOX (Apache-2.0) ONNX model; one shared instance, inferences serialised."""

    def __init__(self, path: str):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
        self.session = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        shape = self.session.get_inputs()[0].shape            # [1, 3, H, W]
        self.size = (int(shape[2]), int(shape[3]))
        self.lock = threading.Lock()
        self.grids, self.strides = _grids(self.size)
        self.ms = 0.0

    def detect(self, img: np.ndarray, conf: float = 0.3) -> list[dict]:
        x, r = preprocess(img, self.size)
        t = time.time()
        with self.lock:
            out = self.session.run(None, {self.input: x})[0]
        self.ms = self.ms * 0.9 + (time.time() - t) * 100      # running average, ms
        return postprocess(out[0], r, self.grids, self.strides, conf, img.shape)


def preprocess(img, size):
    """Letterbox to the model size (pad 114), BGR, 0-255, CHW."""
    h, w = size
    r = min(h / img.shape[0], w / img.shape[1])
    resized = cv2.resize(img, (int(img.shape[1] * r), int(img.shape[0] * r)), interpolation=cv2.INTER_LINEAR)
    padded = np.full((h, w, 3), 114, np.uint8)
    padded[:resized.shape[0], :resized.shape[1]] = resized
    return np.ascontiguousarray(padded.transpose(2, 0, 1)[None], dtype=np.float32), r


def _grids(size, strides=(8, 16, 32)):
    grids, exp = [], []
    for s in strides:
        hs, ws = size[0] // s, size[1] // s
        xv, yv = np.meshgrid(np.arange(ws), np.arange(hs))
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        exp.append(np.full((hs * ws, 1), s))
    return np.concatenate(grids, 0).astype(np.float32), np.concatenate(exp, 0).astype(np.float32)


def postprocess(out, r, grids, strides, conf, shape, nms=0.45) -> list[dict]:
    """YOLOX raw output [N, 85] -> boxes in picture pixels, only the object kinds we use."""
    out = out.copy()
    out[:, :2] = (out[:, :2] + grids) * strides
    out[:, 2:4] = np.exp(np.clip(out[:, 2:4], -10, 10)) * strides
    scores = out[:, 4:5] * out[:, 5:]
    cls = scores.argmax(1)
    best = scores[np.arange(len(cls)), cls]
    keep = best >= conf
    if not keep.any():
        return []
    b, cls, best = out[keep, :4], cls[keep], best[keep]
    xyxy = np.stack([b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2, b[:, 0] + b[:, 2] / 2, b[:, 1] + b[:, 3] / 2], 1) / r
    xyxy[:, [0, 2]] = xyxy[:, [0, 2]].clip(0, shape[1] - 1)
    xyxy[:, [1, 3]] = xyxy[:, [1, 3]].clip(0, shape[0] - 1)
    res = []
    for c in np.unique(cls):
        name = COCO[c] if c < len(COCO) else str(c)
        if name not in WANTED:
            continue
        idx = np.where(cls == c)[0]
        boxes = [[float(x1), float(y1), float(x2 - x1), float(y2 - y1)] for x1, y1, x2, y2 in xyxy[idx]]
        for k in np.array(cv2.dnn.NMSBoxes(boxes, best[idx].tolist(), conf, nms)).flatten():
            x1, y1, x2, y2 = xyxy[idx[k]]
            res.append({"cls": name, "conf": float(best[idx[k]]), "box": (float(x1), float(y1), float(x2), float(y2))})
    return res


# ---------------------------------------------------------------- tracking
def iou(a, b):
    ix, iy = max(0, min(a[2], b[2]) - max(a[0], b[0])), max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


class Track:
    _next = 1

    def __init__(self, det, now):
        self.id, Track._next = Track._next, Track._next + 1
        self.cls, self.conf, self.box = det["cls"], det["conf"], det["box"]
        self.prev_foot = self.foot = foot(det["box"])
        self.seen, self.hits = now, 1
        self.inside: dict[str, float] = {}       # rule id -> since when inside its zone
        self.fired: set[str] = set()             # rules already reported for this object


def foot(box):
    """Where the object touches the ground: bottom centre of its box."""
    return ((box[0] + box[2]) / 2, box[3])


class Tracker:
    def __init__(self, max_age=2.0):
        self.tracks: list[Track] = []
        self.max_age = max_age

    def update(self, dets, now) -> list[Track]:
        free = list(range(len(dets)))
        for t in sorted(self.tracks, key=lambda t: -t.hits):
            best, bi = 0.2, None
            for i in free:
                if dets[i]["cls"] == t.cls:
                    v = iou(t.box, dets[i]["box"])
                    if v > best:
                        best, bi = v, i
            if bi is None:                         # also accept a short move of a small object
                for i in free:
                    if dets[i]["cls"] == t.cls:
                        f, g = foot(dets[i]["box"]), t.foot
                        size = max(t.box[2] - t.box[0], t.box[3] - t.box[1])
                        if (f[0] - g[0]) ** 2 + (f[1] - g[1]) ** 2 < (0.6 * size) ** 2:
                            bi = i
                            break
            if bi is not None:
                d = dets[bi]
                free.remove(bi)
                t.prev_foot, t.foot = t.foot, foot(d["box"])
                t.box, t.conf, t.seen, t.hits = d["box"], d["conf"], now, t.hits + 1
        self.tracks = [t for t in self.tracks if now - t.seen <= self.max_age]
        self.tracks += [Track(dets[i], now) for i in free]
        return [t for t in self.tracks if t.seen == now]


# ---------------------------------------------------------------- geometry
def side(a, b, p):
    """>0: p is on the right of the line a->b (picture coordinates, y down), <0: on the left."""
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def crossing(a, b, p0, p1) -> int:
    """+1: moved across segment a-b from left to right (the arrow's direction), -1: right to left, 0: no."""
    s0, s1 = side(a, b, p0), side(a, b, p1)
    if s0 == 0 or s1 == 0 or (s0 > 0) == (s1 > 0):
        return 0
    t0, t1 = side(p0, p1, a), side(p0, p1, b)
    if (t0 > 0) == (t1 > 0):
        return 0                                   # crossed the line's extension, not the line
    return 1 if s1 > 0 else -1


def to_px(points, w, h):
    return np.array([[p[0] * w, p[1] * h] for p in points], np.float32)


def inside(poly_px, p) -> bool:
    return poly_px is None or cv2.pointPolygonTest(poly_px, (float(p[0]), float(p[1])), False) >= 0


# ---------------------------------------------------------------- rules
class RuleEngine:
    """Evaluate one camera's rules on each analysed frame; returns events to publish."""

    def __init__(self, rules: list[dict]):
        self.rules = [r for r in rules if r.get("enabled", True)]
        self.tracker = Tracker()
        self.last_fire: dict[str, float] = {}
        self.bg = None
        self.motion_level: dict[str, float] = {}
        self.tracks: list[Track] = []
        self.motion_frames = 0              # the background model needs a warm-up before it is trusted

    @property
    def needs_objects(self) -> bool:
        return any(r["type"] != "motion" for r in self.rules)

    def _cool(self, r, now) -> bool:
        if now - self.last_fire.get(r["id"], float("-inf")) < float(r.get("cooldown", 10)):
            return False
        self.last_fire[r["id"]] = now
        return True

    def process(self, frame, dets, now) -> list[dict]:
        h, w = frame.shape[:2]
        events = []
        tracks = self.tracker.update(dets, now) if self.needs_objects else []
        self.tracks = tracks
        motion_mask = self._motion(frame) if any(r["type"] == "motion" for r in self.rules) else None
        for r in self.rules:
            zone = to_px(r["zone"], w, h) if r.get("zone") and len(r["zone"]) >= 3 else None
            if r["type"] == "motion":
                if self.motion_frames <= 15:
                    continue
                level = self._zone_motion(motion_mask, zone, w, h)
                self.motion_level[r["id"]] = level
                if level * 100 >= float(r.get("sensitivity", 2)) and self._cool(r, now):
                    events.append({"rule": r, "object": None, "detail": f"{level * 100:.1f}% of the area changed"})
                continue
            for t in tracks:
                if t.cls not in r.get("classes", ["person"]) or t.conf < float(r.get("min_confidence", 0.5)) or t.hits < 2:
                    continue                         # need 2 sightings: ignores one-frame false detections
                if r["type"] == "line":
                    a, b = to_px(r["line"], w, h)
                    c = crossing(a, b, t.prev_foot, t.foot)
                    want = {"both": c != 0, "a_to_b": c == 1, "b_to_a": c == -1}[r.get("direction", "both")]
                    if want and self._cool(r, now):
                        events.append({"rule": r, "object": t, "detail": "in the arrow's direction" if c == 1 else "against the arrow"})
                    continue
                if not inside(zone, t.foot):
                    t.inside.pop(r["id"], None)
                    continue
                since = t.inside.setdefault(r["id"], now)
                due = r["type"] == "intrusion" or now - since >= float(r.get("seconds", 10))
                if due and r["id"] not in t.fired:
                    t.fired.add(r["id"])
                    if self._cool(r, now):
                        events.append({"rule": r, "object": t,
                                       "detail": f"{now - since:.0f} s in the zone" if r["type"] == "loitering" else ""})
        return events

    def _motion(self, frame):
        small = cv2.resize(frame, (320, max(1, int(320 * frame.shape[0] / frame.shape[1]))))
        if self.bg is None:
            self.bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=32, detectShadows=False)
        mask = self.bg.apply(cv2.GaussianBlur(small, (5, 5), 0))
        self.motion_frames += 1
        return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    @staticmethod
    def _zone_motion(mask, zone, w, h) -> float:
        if mask is None:
            return 0.0
        if zone is None:
            return float(np.count_nonzero(mask)) / mask.size
        zm = np.zeros(mask.shape, np.uint8)
        sx, sy = mask.shape[1] / w, mask.shape[0] / h
        cv2.fillPoly(zm, [np.round(zone * [sx, sy]).astype(np.int32)], 255)
        area = np.count_nonzero(zm)
        return float(np.count_nonzero(cv2.bitwise_and(mask, zm))) / area if area else 0.0


# ---------------------------------------------------------------- drawing
COLORS = [(80, 200, 255), (255, 160, 60), (120, 220, 120), (230, 120, 230), (90, 90, 255), (255, 220, 80)]


def draw_rules(img, rules, highlight=None):
    h, w = img.shape[:2]
    for i, r in enumerate(rules):
        col = (0, 0, 255) if highlight and r["id"] == highlight else COLORS[i % len(COLORS)]
        if r["type"] == "line" and len(r.get("line", [])) == 2:
            a, b = to_px(r["line"], w, h).astype(int)
            cv2.line(img, tuple(a), tuple(b), col, 3)
            m = (a + b) // 2
            d = b - a
            n = np.array([-d[1], d[0]], float)
            n = n / (np.linalg.norm(n) or 1) * 30
            if r.get("direction", "both") != "b_to_a":
                cv2.arrowedLine(img, tuple(m), tuple((m + n).astype(int)), col, 2, tipLength=0.4)
            if r.get("direction", "both") != "a_to_b":
                cv2.arrowedLine(img, tuple(m), tuple((m - n).astype(int)), col, 2, tipLength=0.4)
        elif r.get("zone") and len(r["zone"]) >= 3:
            pts = to_px(r["zone"], w, h).astype(np.int32)
            overlay = img.copy()
            cv2.fillPoly(overlay, [pts], col)
            cv2.addWeighted(overlay, 0.18, img, 0.82, 0, img)
            cv2.polylines(img, [pts], True, col, 2)
        else:
            continue
        cv2.putText(img, r.get("name", ""), (8, 22 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 2)


def draw_objects(img, objects, color=(0, 255, 0)):
    for o in objects:
        box = o.box if isinstance(o, Track) else o["box"]
        cls = o.cls if isinstance(o, Track) else o["cls"]
        conf = o.conf if isinstance(o, Track) else o["conf"]
        x1, y1, x2, y2 = (int(v) for v in box)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        cv2.putText(img, f"{cls} {conf:.0%}", (x1, max(14, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
