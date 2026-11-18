"""Analytics events straight from the cameras.

Per camera (settings.events_enabled) one long-lived connection, re-established on failure:
  * Hikvision  - ISAPI alert stream       /ISAPI/Event/notification/alertStream
  * Dahua      - CGI event stream          /cgi-bin/eventManager.cgi?action=attach&codes=[All]
  * all others - ONVIF PullPoint subscription (Axis, Provision, FLIR, Uniview, Hanwha, ...)

Each detection is published on the bus:
  * camera.alarm     (severity = settings.alarm_severity) when "send to Alarm manager" is on;
                     the C&C rule "*.alarm" turns it into an alarm in the Alarm manager tab
  * camera.analytics (info) otherwise - shown in the Events tab only
"""
import asyncio
import datetime
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape

import httpx
from fastapi import HTTPException

from .bus import bus
from .db import Camera, SessionLocal

log = logging.getLogger("camera-events")
EVT_STATUS: dict[int, dict] = {}       # camera id -> {state, detail, method, last, last_event}
COOLDOWN = 15                         # seconds between two identical events of one camera
TASKS: dict[int, tuple[str, asyncio.Task]] = {}

HIK_LABELS = {
    "vmd": "Motion", "linedetection": "Line crossing", "fielddetection": "Intrusion",
    "regionentrance": "Region entrance", "regionexiting": "Region exiting", "tamperdetection": "Tampering",
    "shelteralarm": "Tampering", "facedetection": "Face detected", "scenechangedetection": "Scene change",
    "io": "Alarm input", "pir": "PIR", "loitering": "Loitering", "group": "People gathering",
    "rapidmove": "Fast moving", "parking": "Parking", "unattendedbaggage": "Object left",
    "attendedbaggage": "Object removed", "anpr": "License plate", "vehicledetection": "Vehicle detected",
    "audioexception": "Audio exception", "defocus": "Defocus", "facesnap": "Face captured",
    "humanrecognition": "Person detected", "videoloss": "Video loss",
}
DAHUA_LABELS = {
    "videomotion": "Motion", "smartmotionhuman": "Person motion", "smartmotionvehicle": "Vehicle motion",
    "crosslinedetection": "Line crossing", "crossregiondetection": "Intrusion", "leftdetection": "Object left",
    "takenawaydetection": "Object removed", "facedetection": "Face detected", "facerecognition": "Face recognised",
    "videoblind": "Tampering", "alarmlocal": "Alarm input", "scenechange": "Scene change",
    "audiomutation": "Audio change", "trafficjunction": "License plate", "videoloss": "Video loss",
    "parkingdetection": "Parking", "wanderdetection": "Loitering", "rioterdetection": "People gathering",
    "movedetection": "Fast moving", "videoabnormaldetection": "Video abnormal",
}
GENERIC = [  # ONVIF topics and anything else: keyword -> label
    ("linedetector", "Line crossing"), ("linecross", "Line crossing"), ("crossed", "Line crossing"),
    ("fielddetector", "Intrusion"), ("intrusion", "Intrusion"), ("objectsinside", "Intrusion"),
    ("tamper", "Tampering"), ("licenseplate", "License plate"), ("plate", "License plate"),
    ("face", "Face detected"), ("loiter", "Loitering"), ("abandon", "Object left"), ("leftobject", "Object left"),
    ("removed", "Object removed"), ("human", "Person detected"), ("person", "Person detected"),
    ("vehicle", "Vehicle detected"), ("digitalinput", "Alarm input"), ("motion", "Motion"), ("vmd", "Motion"),
]


def label_of(method: str, event: str) -> str:
    low = event.lower()
    table = HIK_LABELS if method == "hikvision" else DAHUA_LABELS if method == "dahua" else {}
    if low in table:
        return table[low]
    for k, v in GENERIC:
        if k in low:
            return v
    return event.rsplit("/", 1)[-1]


def method_of(brand: str) -> str:
    return brand if brand in ("hikvision", "dahua") else "onvif"


METHOD_NAMES = {"hikvision": "Hikvision event stream (ISAPI)", "dahua": "Dahua event stream",
                "onvif": "ONVIF events"}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _set(cid, state, detail="", **kw):
    st = EVT_STATUS.setdefault(cid, {})
    st.update(state=state, detail=detail, checked=_now(), **kw)


# ---------------------------------------------------------------- parsers (pure, tested)
def parse_hik(block: str) -> dict | None:
    def tag(t):
        m = re.search(rf"<{t}>([^<]*)</{t}>", block)
        return m.group(1).strip() if m else ""
    ev = tag("eventType")
    if not ev or tag("eventState").lower() != "active":
        return None                                  # 'inactive' (incl. videoloss heartbeats)
    ch = tag("channelID") or tag("dynChannelID")
    return {"event": ev, "channel": int(ch) if ch.isdigit() else None, "active": True,
            "target": tag("detectionTarget") or tag("targetType"), "region": tag("regionID"),
            "rule": tag("eventDescription")}


def parse_dahua(part: str) -> dict | None:
    m = re.search(r"Code=([^;\s]+);action=([^;\s]+);index=(\d+)", part)
    if not m or m.group(1) == "Heartbeat" or m.group(2) not in ("Start", "Pulse"):
        return None
    target, rule = "", ""
    d = re.search(r";data=(\{.*\})", part, re.S)
    if d:
        try:
            data = json.loads(d.group(1))
            obj = data.get("Object") or (data.get("Objects") or [{}])[0]
            target = obj.get("ObjectType", "") if isinstance(obj, dict) else ""
            rule = data.get("Name", "")
        except (ValueError, AttributeError, IndexError):
            pass
    return {"event": m.group(1), "channel": int(m.group(3)) + 1, "active": True, "target": target,
            "region": "", "rule": rule}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_onvif_messages(xml: str) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for nm in root.iter():
        if _local(nm.tag) != "NotificationMessage":
            continue
        topic, msg = "", None
        for ch in nm.iter():
            n = _local(ch.tag)
            if n == "Topic" and not topic:
                topic = (ch.text or "").strip()
            elif n == "Message" and msg is None and any(_local(x.tag) == "Message" for x in ch):
                msg = next(x for x in ch if _local(x.tag) == "Message")
        if not topic or msg is None:
            continue
        if msg.get("PropertyOperation") == "Initialized":
            continue                                 # state dump right after subscribing
        src, data = {}, {}
        for part in msg:
            items = {i.get("Name"): i.get("Value") for i in part.iter() if _local(i.tag) == "SimpleItem"}
            (src if _local(part.tag) == "Source" else data if _local(part.tag) == "Data" else {}).update(items)
        bools = [v.lower() for v in data.values() if v and v.lower() in ("true", "false")]
        if bools and "true" not in bools:
            continue                                 # detection ended
        event = "/".join(p.split(":", 1)[-1] for p in topic.split("/"))
        ch_tok = src.get("VideoSourceConfigurationToken") or src.get("VideoSourceToken") or src.get("Source") or ""
        target = data.get("ObjectType") or data.get("ClassTypes") or ""
        out.append({"event": event, "channel": None, "active": True, "target": target,
                    "region": "", "rule": src.get("Rule", ""), "source": ch_tok})
    return out


def parse_topicset(xml: str) -> list[str]:
    """ONVIF GetEventProperties -> list of topic paths."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    ts = next((e for e in root.iter() if _local(e.tag) == "TopicSet"), None)
    found = []

    def walk(e, path):
        for ch in e:
            n = _local(ch.tag)
            if n in ("MessageDescription", "Documentation"):
                continue
            p = path + [n]
            if any(k.endswith("}topic") and v == "true" for k, v in ch.attrib.items()):
                found.append("/".join(p))
            walk(ch, p)
    if ts is not None:
        walk(ts, [])
    return found


# ---------------------------------------------------------------- dispatch
_last: dict[tuple, float] = {}


async def dispatch(cid: int, name: str, s: dict, ev: dict):
    method = method_of(s.get("brand", ""))
    if method in ("hikvision", "dahua") and ev.get("channel") and s.get("channel") and ev["channel"] != s["channel"]:
        return                                       # NVR: another channel's event
    if s.get("event_types") and ev["event"] not in s["event_types"]:
        return
    key = (cid, ev["event"], ev.get("region") or ev.get("rule"))
    if time.time() - _last.get(key, 0) < COOLDOWN:
        return
    _last[key] = time.time()
    label = label_of(method, ev["event"])
    payload = {"title": f"{label} - {name}", "event": ev["event"], "label": label,
               "target": ev.get("target") or None, "rule": ev.get("rule") or None,
               "region": ev.get("region") or None, "method": METHOD_NAMES[method]}
    payload = {k: v for k, v in payload.items() if v is not None}
    st = EVT_STATUS.setdefault(cid, {})
    st.update(last=_now(), last_event=label)
    if s.get("events_alarm", True):
        await bus.emit("camera", "camera.alarm", payload, s.get("alarm_severity") or "high", cid)
    else:
        await bus.emit("camera", "camera.analytics", payload, "info", cid)


# ---------------------------------------------------------------- listeners
def _http(s: dict, timeout) -> httpx.AsyncClient:
    auth = httpx.DigestAuth(s.get("username", ""), s.get("password", "")) if s.get("username") else None
    return httpx.AsyncClient(timeout=timeout, auth=auth)


def _base(s):
    return f"http://{s['ip']}:{s.get('onvif_port') or 80}"


def _http_error(r: httpx.Response, what: str):
    if r.status_code == 401:
        raise HTTPException(400, f"{what}: wrong username or password")
    if r.status_code in (403, 404, 501):
        raise HTTPException(400, f"{what}: not supported or not allowed (HTTP {r.status_code}) - "
                                 "check the user has event/notification rights")
    raise HTTPException(400, f"{what}: HTTP {r.status_code}")


async def listen_hikvision(cid, name, s):
    async with _http(s, httpx.Timeout(10, read=90)) as c:
        async with c.stream("GET", _base(s) + "/ISAPI/Event/notification/alertStream") as r:
            if r.status_code != 200:
                _http_error(r, "Hikvision event stream")
            _set(cid, "connected", "receiving events", method=METHOD_NAMES["hikvision"])
            buf = ""
            async for chunk in r.aiter_text():
                buf += chunk
                while True:
                    m = re.search(r"<EventNotificationAlert.*?</EventNotificationAlert>", buf, re.S)
                    if not m:
                        buf = buf[-200_000:]
                        break
                    buf = buf[m.end():]
                    ev = parse_hik(m.group(0))
                    if ev:
                        await dispatch(cid, name, s, ev)


async def listen_dahua(cid, name, s):
    url = _base(s) + "/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5"
    async with _http(s, httpx.Timeout(10, read=60)) as c:
        async with c.stream("GET", url) as r:
            if r.status_code != 200:
                _http_error(r, "Dahua event stream")
            _set(cid, "connected", "receiving events", method=METHOD_NAMES["dahua"])
            buf = ""
            async for chunk in r.aiter_text():
                buf += chunk
                parts = re.split(r"--myboundary\s*", buf)
                buf = parts.pop()                    # last part may be incomplete
                for p in parts:
                    ev = parse_dahua(p)
                    if ev:
                        await dispatch(cid, name, s, ev)
                if len(buf) > 200_000:
                    buf = ""


_WSA = "http://www.w3.org/2005/08/addressing"
_EV = "http://www.onvif.org/ver10/events/wsdl"


def _with_addressing(env: str, action: str, to: str) -> str:
    wsa = (f'<a:Action xmlns:a="{_WSA}">{action}</a:Action>'
           f'<a:To xmlns:a="{_WSA}">{escape(to)}</a:To>')
    if "<s:Header>" in env:
        return env.replace("<s:Header>", "<s:Header>" + wsa, 1)
    return env.replace("<s:Body>", f"<s:Header>{wsa}</s:Header><s:Body>", 1)


class Onvif:
    """Minimal ONVIF events client (PullPoint) using the camera's accepted login style."""

    def __init__(self, s: dict, c: httpx.AsyncClient):
        self.s, self.c, self.mode, self.offset = s, c, "digest", 0.0

    def _fix(self, url: str) -> str:
        p = urlsplit(url.strip())                    # cameras often report an internal address
        return urlunsplit(("http", f"{self.s['ip']}:{self.s.get('onvif_port') or 80}", p.path, p.query, ""))

    async def call(self, url: str, body: str, action: str) -> str:
        from .cameras import _envelope
        env = _with_addressing(_envelope(body, self.s.get("username", ""), self.s.get("password", ""),
                                         self.mode, self.offset), action, url)
        r = await self.c.post(url, content=env, headers={
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'})
        if r.status_code >= 400:
            fault = re.search(r"<[^>]*Text[^>]*>([^<]+)<", r.text)
            raise _SoapError(r.status_code, fault.group(1) if fault else f"HTTP {r.status_code}", r.text)
        return r.text

    async def events_url(self) -> str:
        from .cameras import _camera_clock_offset
        device = _base(self.s) + "/onvif/device_service"
        self.offset = await _camera_clock_offset(self.c, device)
        last = None
        for mode in ("digest", "text", "none"):
            self.mode = mode
            try:
                caps = await self.call(device, '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl">'
                                       '<Category>Events</Category></GetCapabilities>',
                                       "http://www.onvif.org/ver10/device/wsdl/GetCapabilities")
                m = re.search(r"Events[^>]*>.*?XAddr[^>]*>([^<]+)<", caps, re.S)
                return self._fix(m.group(1)) if m else _base(self.s) + "/onvif/event_service"
            except _SoapError as e:
                last = e
                if not e.auth:
                    break
        if last and last.auth:
            raise HTTPException(400, "ONVIF events: the camera rejected the username or password"
                                     " (some cameras need a separate ONVIF user)")
        return _base(self.s) + "/onvif/event_service"

    async def subscribe(self, ev_url: str) -> str:
        x = await self.call(ev_url, f'<CreatePullPointSubscription xmlns="{_EV}">'
                            '<InitialTerminationTime>PT120S</InitialTerminationTime></CreatePullPointSubscription>',
                            f"{_EV}/EventPortType/CreatePullPointSubscriptionRequest")
        m = re.search(r"SubscriptionReference[^>]*>.*?Address[^>]*>([^<]+)<", x, re.S)
        if not m:
            raise HTTPException(400, "ONVIF events: the camera did not create a subscription")
        return self._fix(m.group(1))


class _SoapError(Exception):
    def __init__(self, code, reason, text):
        super().__init__(reason)
        self.auth = code in (401, 403) or bool(re.search(r"uthori|ender|ccess|assword|ecurity", text))


async def listen_onvif(cid, name, s):
    async with _http(s, httpx.Timeout(20)) as c:
        o = Onvif(s, c)
        try:
            pp = await o.subscribe(await o.events_url())
        except _SoapError as e:
            raise HTTPException(400, f"ONVIF events not available: {e}")
        except httpx.HTTPError as e:
            raise HTTPException(400, f"Cannot reach {s['ip']}:{s.get('onvif_port') or 80} ({e.__class__.__name__})")
        _set(cid, "connected", "subscribed to ONVIF events", method=METHOD_NAMES["onvif"])
        renewed = time.time()
        try:
            while True:
                x = await o.call(pp, f'<PullMessages xmlns="{_EV}"><Timeout>PT10S</Timeout>'
                                 '<MessageLimit>50</MessageLimit></PullMessages>',
                                 f"{_EV}/PullPointSubscription/PullMessagesRequest")
                for ev in parse_onvif_messages(x):
                    await dispatch(cid, name, s, ev)
                if time.time() - renewed > 60:
                    await o.call(pp, '<Renew xmlns="http://docs.oasis-open.org/wsn/b-2">'
                                 '<TerminationTime>PT120S</TerminationTime></Renew>',
                                 "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest")
                    renewed = time.time()
        finally:
            try:
                await o.call(pp, '<Unsubscribe xmlns="http://docs.oasis-open.org/wsn/b-2"/>',
                             "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest")
            except Exception:
                pass


LISTENERS = {"hikvision": listen_hikvision, "dahua": listen_dahua, "onvif": listen_onvif}


async def run_camera(cid: int, name: str, s: dict):
    method = method_of(s.get("brand", ""))
    delay = 5
    while True:
        _set(cid, "connecting", "connecting to the camera", method=METHOD_NAMES[method])
        started = time.time()
        try:
            await LISTENERS[method](cid, name, s)
            detail = "the camera closed the event connection"
        except asyncio.CancelledError:
            raise
        except HTTPException as e:
            detail = e.detail
        except _SoapError as e:
            detail = f"ONVIF: {e}"
        except httpx.HTTPError as e:
            detail = f"connection lost ({e.__class__.__name__})"
        except Exception as e:                       # never let one camera kill the manager
            detail = f"{e.__class__.__name__}: {e}"
        if time.time() - started > 120:
            delay = 5
        _set(cid, "error", detail, method=METHOD_NAMES[method])
        log.info("camera %s events: %s - retry in %ss", cid, detail, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


def wanted() -> dict[int, tuple[str, str, dict]]:
    out = {}
    with SessionLocal() as db:
        for c in db.query(Camera):
            m = dict(c.meta or {})
            if c.enabled and m.get("events_enabled") and m.get("ip"):
                s = {k: m.get(k) for k in ("brand", "ip", "onvif_port", "username", "password", "channel",
                                            "event_types", "events_alarm", "alarm_severity")}
                out[c.id] = (json.dumps([c.name, s], sort_keys=True), c.name, s)
    return out


async def events_manager():
    """Keep exactly one listener per camera that has events enabled; restart on setting changes."""
    while True:
        try:
            want = wanted()
            for cid in list(TASKS):
                if cid not in want or TASKS[cid][0] != want[cid][0]:
                    TASKS.pop(cid)[1].cancel()
                    if cid not in want:
                        EVT_STATUS.pop(cid, None)
            for cid, (key, name, s) in want.items():
                if cid not in TASKS:
                    TASKS[cid] = (key, asyncio.create_task(run_camera(cid, name, s)))
        except Exception as e:
            log.warning("events manager: %s", e)
        await asyncio.sleep(10)


# ---------------------------------------------------------------- "Find event types"
async def supported_types(s: dict) -> dict:
    method = method_of(s.get("brand", ""))
    types: list[str] = []
    try:
        async with _http(s, httpx.Timeout(10)) as c:
            if method == "hikvision":
                r = await c.get(_base(s) + "/ISAPI/Event/triggers")
                if r.status_code != 200:
                    _http_error(r, "Hikvision events")
                for blk in re.findall(r"<EventTrigger>.*?</EventTrigger>", r.text, re.S):
                    t = re.search(r"<eventType>([^<]+)<", blk)
                    ch = re.search(r"<(?:videoInputChannelID|dynVideoInputChannelID|inputIOPortID)>(\d+)<", blk)
                    if t and (not ch or "IOPort" in blk or int(ch.group(1)) == int(s.get("channel") or 1)):
                        types.append(t.group(1))
            elif method == "dahua":
                r = await c.get(_base(s) + "/cgi-bin/eventManager.cgi?action=getExposureEvents")
                if r.status_code == 200:
                    types = re.findall(r"events\[\d+\]=(\w+)", r.text)
                elif r.status_code == 401:
                    _http_error(r, "Dahua events")
                if not types:                        # older firmware: check the stream works, offer common codes
                    async with c.stream("GET", _base(s) + "/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5") as r2:
                        if r2.status_code != 200:
                            _http_error(r2, "Dahua events")
                    types = ["VideoMotion", "CrossLineDetection", "CrossRegionDetection", "SmartMotionHuman",
                             "SmartMotionVehicle", "VideoBlind", "LeftDetection", "TakenAwayDetection",
                             "FaceDetection", "AlarmLocal"]
            else:
                o = Onvif(s, c)
                url = await o.events_url()
                x = await o.call(url, f'<GetEventProperties xmlns="{_EV}"/>',
                                 f"{_EV}/EventPortType/GetEventPropertiesRequest")
                types = [t for t in parse_topicset(x)]
    except _SoapError as e:
        raise HTTPException(400, f"ONVIF events not available: {e}")
    except httpx.HTTPError as e:
        raise HTTPException(400, f"Cannot reach {s['ip']}:{s.get('onvif_port') or 80} for events "
                                 f"({e.__class__.__name__}) - check the HTTP/ONVIF port")
    seen, out = set(), []
    for t in types:
        if t not in seen:
            seen.add(t)
            out.append({"id": t, "label": label_of(method, t)})
    if not out:
        raise HTTPException(400, "The camera reported no analytics events - enable detections "
                                 "(motion, line crossing, intrusion...) in the camera's own settings first")
    return {"method": METHOD_NAMES[method], "types": out}
