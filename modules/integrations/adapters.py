"""Adapters for 3rd-party security systems.

An adapter connects to one system, imports its devices into the hub, keeps device state live,
turns alarms into platform events and executes control actions. To support a new system,
subclass Adapter, describe its settings in `fields`, and add it to ADAPTERS.
"""
import asyncio
import base64
import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
import re

import httpx

log = logging.getLogger("adapters")


def _pick(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _as_list(x) -> list:
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("items", "data", "results", "events", "doors", "readers", "value", "Items", "Data"):
            if isinstance(x.get(k), list):
                return x[k]
        return [x]
    return []


class Adapter:
    label = "base"
    fields: list[dict] = []
    actions: dict[str, list[str]] = {}

    def __init__(self, system: dict, hub):
        self.sys, self.hub, self.sid = system, hub, system["id"]
        self.cfg = system.get("config", {})
        self.task: asyncio.Task | None = None
        self.connected = False
        self._stop = asyncio.Event()

    async def run_forever(self):
        delay = 5
        while not self._stop.is_set():
            try:
                self.hub.status(self.sid, "connecting")
                await self.run()
                delay = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("%s: %s", self.sid, e)
                was = self.connected
                self.connected = False
                self.hub.status(self.sid, "error", f"{e.__class__.__name__}: {e}")
                if was:
                    await self.hub.event(self.sid, "system", 0, "connection_lost", "warning", str(e))
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 120)

    async def stop(self):
        self._stop.set()
        if self.task:
            self.task.cancel()
        try:
            await self.close()
        except Exception:
            pass

    async def close(self):
        pass

    async def run(self):
        raise NotImplementedError

    async def import_devices(self, csv_text: str | None = None) -> int:
        raise ValueError("Import is not supported for this system")

    async def action(self, device: dict, action: str):
        raise ValueError("Actions are not supported for this system")

    async def handle_inbound(self, event: dict):
        raise ValueError("This system does not accept pushed events")


# ======================================================================== RISCO
class RiscoLocalAdapter(Adapter):
    """RISCO LightSYS / LightSYS+ / ProSYS Plus / Agility / WiComm over the LAN.

    Talks to the panel's IP module (default TCP 1000) with the panel access code (default 5678)
    using the open-source pyrisco library. Note: on mono-socket IP modules the panel accepts only
    one connection, so RISCO Cloud may need to be disabled on the panel.
    """
    label = "RISCO intrusion panel (LAN)"
    fields = [
        {"key": "host", "label": "Panel IP address", "required": True, "placeholder": "192.168.1.50"},
        {"key": "port", "label": "Port", "type": "number", "default": 1000},
        {"key": "code", "label": "Panel access code", "type": "password", "default": "5678"},
        {"key": "log_zone_activity", "label": "Log every zone open/close (busy sites: leave off)",
         "type": "checkbox"},
    ]
    actions = {"partition": ["arm", "stay", "disarm"], "zone": ["bypass", "unbypass"]}
    P_KEYS = ("armed", "partially_armed", "disarmed", "triggered", "ready", "arming")
    Z_KEYS = ("triggered", "bypassed", "alarmed", "armed")

    def __init__(self, system, hub):
        super().__init__(system, hub)
        self.r = None

    @classmethod
    def _state(cls, obj, keys) -> dict:
        out = {}
        for k in keys:
            try:
                v = getattr(obj, k)
            except Exception:
                continue
            if not callable(v):
                out[k] = bool(v)
        return out

    def _sync_all(self) -> int:
        n = 0
        for pid, p in self.r.partitions.items():
            self.hub.upsert(self.sid, "partition", pid, getattr(p, "name", None) or f"Partition {pid}",
                            self._state(p, self.P_KEYS))
            n += 1
        for zid, z in self.r.zones.items():
            self.hub.upsert(self.sid, "zone", zid, getattr(z, "name", None) or f"Zone {zid}",
                            self._state(z, self.Z_KEYS))
            n += 1
        self.hub.save()
        return n

    async def run(self):
        from pyrisco import RiscoLocal
        self.r = RiscoLocal(self.cfg["host"], int(self.cfg.get("port") or 1000),
                            str(self.cfg.get("code") or "5678"))
        await self.r.connect()
        lost = asyncio.get_running_loop().create_future()

        async def on_error(err):
            if not lost.done():
                lost.set_exception(RuntimeError(str(err) or err.__class__.__name__))

        self.r.add_error_handler(on_error)
        self.r.add_zone_handler(self._on_zone)
        self.r.add_partition_handler(self._on_partition)
        self.connected = True
        n = self._sync_all()
        self.hub.status(self.sid, "connected")
        await self.hub.event(self.sid, "system", 0, "connected", "info", f"Panel connected, {n} devices")
        try:
            await lost
        finally:
            await self.close()

    async def close(self):
        if self.r:
            try:
                await self.r.disconnect()
            except Exception:
                pass

    async def _on_partition(self, pid, p):
        prev = dict(self.hub.device(self.sid, "partition", pid).get("state", {}))
        new = self._state(p, self.P_KEYS)
        self.hub.upsert(self.sid, "partition", pid, getattr(p, "name", None), new)
        self.hub.save()
        rose = lambda k: new.get(k) and not prev.get(k)
        if rose("triggered"):
            cams = self.hub.cameras_of_active_zones(self.sid)
            await self.hub.event(self.sid, "partition", pid, "alarm", "critical", "Intrusion alarm",
                                 alarm=True, extra_cameras=cams)
        if rose("armed"):
            await self.hub.event(self.sid, "partition", pid, "armed", "info", "Armed (away)")
        elif rose("partially_armed"):
            await self.hub.event(self.sid, "partition", pid, "stay_armed", "info", "Armed (stay)")
        if rose("disarmed") and prev:
            await self.hub.event(self.sid, "partition", pid, "disarmed", "info", "Disarmed")

    async def _on_zone(self, zid, z):
        prev = dict(self.hub.device(self.sid, "zone", zid).get("state", {}))
        new = self._state(z, self.Z_KEYS)
        self.hub.upsert(self.sid, "zone", zid, getattr(z, "name", None), new)
        self.hub.save()
        rose = lambda k: new.get(k) and not prev.get(k)
        fell = lambda k: prev.get(k) and not new.get(k)
        if rose("alarmed"):
            await self.hub.event(self.sid, "zone", zid, "alarm", "critical", "Zone in alarm", alarm=True)
        elif rose("triggered") and self.cfg.get("log_zone_activity"):
            await self.hub.event(self.sid, "zone", zid, "zone_open", "info", "Zone open")
        if rose("bypassed"):
            await self.hub.event(self.sid, "zone", zid, "zone_bypassed", "info", "Zone bypassed")
        elif fell("bypassed"):
            await self.hub.event(self.sid, "zone", zid, "zone_unbypassed", "info", "Bypass removed")

    async def import_devices(self, csv_text=None) -> int:
        if not (self.connected and self.r):
            raise ValueError("Panel is not connected yet - check IP, port and access code")
        return self._sync_all()

    async def action(self, device, action):
        if not (self.connected and self.r):
            raise ValueError("Panel is not connected")
        n, fn = int(device["number"]), None
        if device["kind"] == "partition":
            p = self.r.partitions[n]
            fn = {"arm": p.arm, "stay": p.partial_arm, "disarm": p.disarm}.get(action)
        elif device["kind"] == "zone":
            z = self.r.zones[n]
            if not hasattr(z, "bypass"):
                raise ValueError("This pyrisco version cannot bypass zones")
            fn = {"bypass": lambda: z.bypass(True), "unbypass": lambda: z.bypass(False)}.get(action)
        if not fn:
            raise ValueError(f"'{action}' is not possible on a {device['kind']}")
        await fn()


# ======================================================================== Apollo
# ---------------------------------------------------------------- Apollo APACS
# Protocol from Apollo's own API samples (C:\APACS\Examples: ApacsApiClientLib/Client.cs, Python/BasicSample.py):
#   every request carries   Apacs.Auth: base64("user:password")   (+ HTTP Basic, as the Python sample does)
#   GET  api/global/permission/login      -> response header Apacs.Session, sent on every later request
#   GET  api/objectstate/all              -> {TotalCount, Records:[{Source:{Id, Label, ParentId, TypeId}, State}]}
#   GET  api/event?filter={...}           -> events {Date, Source, Type, Code, Cause, Person}
#   GET  api/<typeid>/<id>/<command>      -> reader unlock|lock, relay On|Off|Pulse, input Arm|Disarm
#   GET  api/global/permission/logout
def apacs_utc(value) -> datetime | None:
    """APACS returns local times with an offset (2026-05-18T11:16:01.946+03:00); filters must be plain UTC."""
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def apacs_time(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S")          # the API rejects formats with a time zone


def apacs_kind(type_id: str) -> str | None:
    t = (type_id or "").lower()
    if t.endswith("reader") and "group" not in t:
        return "door"                                   # ASPReader, AANReader, AAN4Reader, Reader
    if "input" in t and "group" not in t:
        return "input"                                  # ASPInput, ASPReaderInput, Input
    if t in ("relay", "aspstrike", "strike") or t.endswith("relay"):
        return "output"
    return None


class ApacsClient:
    def __init__(self, cfg: dict):
        self.cfg = dict(cfg)
        host = (cfg.get("host") or "").strip()
        if not host and cfg.get("base_url"):                 # settings from an older version
            from urllib.parse import urlsplit
            u = urlsplit(cfg["base_url"])
            host = u.hostname or ""
            self.cfg.setdefault("port", u.port or 8081)
            self.cfg.setdefault("use_ssl", u.scheme == "https")
        self.host = host
        self.port = int(self.cfg.get("port") or 8081)
        self.base = f"{'https' if self.cfg.get('use_ssl', False) else 'http'}://{self.host}:{self.port}/api/"
        self.user, self.pw = cfg.get("username", ""), cfg.get("password", "")
        auth = base64.b64encode(f"{self.user}:{self.pw}".encode()).decode()
        self.headers = {"Apacs.Auth": auth, "Apacs.Session": "", "User-Agent": "Ellalon/1.0"}
        self.c: httpx.AsyncClient | None = None

    async def __aenter__(self):
        if not self.host:
            raise ValueError("Enter the APACS server IP address")
        self.c = httpx.AsyncClient(base_url=self.base, timeout=15, verify=bool(self.cfg.get("verify_tls")),
                                   auth=(self.user, self.pw) if self.user else None)
        return self

    async def __aexit__(self, *a):
        try:
            if self.headers["Apacs.Session"]:
                await self.c.get("global/permission/logout", headers=self.headers)
        except Exception:
            pass
        await self.c.aclose()

    async def login(self):
        try:
            r = await self.c.get("global/permission/login", headers=self.headers)
        except httpx.ConnectError as e:
            if "SSL" in str(e) or "certificate" in str(e).lower() or "wrong version" in str(e).lower():
                raise ValueError(f"Cannot connect to {self.base}: HTTPS problem - untick or tick 'HTTPS' to match the APACS API Service")
            raise ValueError(f"Cannot reach {self.base} - is the APACS API Service started, and port {self.port} open in the Windows firewall?")
        except httpx.HTTPError as e:
            if self.cfg.get("use_ssl") is False and "RemoteProtocolError" in e.__class__.__name__:
                raise ValueError(f"{self.base} did not answer plain HTTP - tick 'HTTPS'")
            raise ValueError(f"Cannot reach {self.base} ({e.__class__.__name__})")
        sess = r.headers.get("Apacs.Session")
        if r.status_code >= 400 or not sess:
            detail = re.sub(r"\s+", " ", r.text)[:160]
            raise ValueError(f"APACS refused the login (HTTP {r.status_code}){': ' + detail if detail else ''} - "
                             "use an APACS operator name and password")
        self.headers["Apacs.Session"] = sess

    async def get(self, path, params=None):
        r = await self.c.get(path, params=params, headers=self.headers)
        if r.status_code == 401:                          # session expired: log in again once
            await self.login()
            r = await self.c.get(path, params=params, headers=self.headers)
        if r.status_code == 204:
            return None
        if r.status_code >= 400:
            raise ValueError(f"APACS answered HTTP {r.status_code} for {path}: {re.sub(r'\s+', ' ', r.text)[:160]}")
        try:
            return r.json()
        except ValueError:
            return r.text

    async def objects(self) -> list[dict]:
        """Every object with its live state: [{id, label, parent, type, state}]."""
        data = None
        for path, params in (("objectstate/all", None), ("objectstate", {"filter": json.dumps({"Start": 0, "Count": 20000})})):
            try:
                data = await self.get(path, params)
                break
            except ValueError:
                continue
        if data is None:                                  # fall back to the configuration tree (no states)
            data = await self.get("treeobject", {"filter": json.dumps({"Start": 0, "Count": 20000})})
        recs = data.get("Records", data) if isinstance(data, dict) else (data or [])
        out = []
        for r in recs if isinstance(recs, list) else []:
            src = r.get("Source") if isinstance(r, dict) and isinstance(r.get("Source"), dict) else r
            if not isinstance(src, dict) or src.get("Id") is None:
                continue
            out.append({"id": str(src["Id"]), "label": src.get("Label") or str(src["Id"]), "parent": str(src.get("ParentId") or ""),
                        "type": src.get("TypeId") or "", "state": r.get("State") if src is not r else None,
                        "active": src.get("IsActive", True)})
        return out

    async def events_since(self, since: datetime) -> list[dict]:
        flt = {"Start": 0, "Count": 200, "Expressions": [{"Property": "Date", "Operation": "Gt", "Value": apacs_time(since)}],
               "Orders": [{"Property": "Date", "Asc": True}]}
        data = await self.get("event", {"filter": json.dumps(flt)})
        recs = data.get("Records", []) if isinstance(data, dict) else (data or [])
        return [e for e in recs if isinstance(e, dict)]

    async def command(self, type_id: str, oid, cmd: str):
        await self.get(f"{type_id.lower()}/{oid}/{cmd}")


def apacs_devices(objs: list[dict]) -> list[dict]:
    by_id = {o["id"]: o for o in objs}
    devs = []
    for o in objs:
        kind = apacs_kind(o["type"])
        if not kind:
            continue
        parent = by_id.get(o["parent"])
        if o["type"].lower() == "aspreaderinput" and parent:   # a door's own input, e.g. "Door forced open"
            where = parent["label"]
        else:
            where = parent["label"] if parent else ""
        devs.append({"kind": kind, "id": o["id"], "name": o["label"], "type": o["type"], "state": o["state"] or "",
                     "where": where, "active": o["active"]})
    order = {"door": 0, "input": 1, "output": 2}
    return sorted(devs, key=lambda d: (order[d["kind"]], d["name"]))


async def apollo_test(cfg: dict) -> dict:
    """Log in, read every object and list the doors / inputs / outputs to choose from."""
    steps = []
    step = lambda ok, text: steps.append({"ok": ok, "text": text})
    if not (cfg.get("host") or cfg.get("base_url") or "").strip():
        return {"ok": False, "steps": [{"ok": False, "text": "Enter the APACS server IP address"}], "devices": [], "found": {}}
    try:
        async with ApacsClient(cfg) as a:
            await a.login()
            step(True, f"Logged in to the APACS API at {a.base} as {a.user}")
            objs = await a.objects()
            devs = apacs_devices(objs)
            n = {k: sum(d["kind"] == k for d in devs) for k in ("door", "input", "output")}
            step(bool(devs), f"{len(objs)} objects in APACS: {n['door']} doors, {n['input']} inputs, {n['output']} outputs")
            try:
                await a.events_since(datetime.now(timezone.utc).replace(tzinfo=None))
                step(True, "Live events: available")
            except ValueError as e:
                step(False, f"Live events not available: {e}")
    except ValueError as e:
        step(False, str(e))
        return {"ok": False, "steps": steps, "devices": [], "found": {}}
    if not devs:
        step(False, "Logged in, but APACS returned no doors, inputs or outputs for this operator")
    return {"ok": bool(devs), "steps": steps, "devices": devs, "found": {}}


class ApolloAdapter(Adapter):
    """Apollo APACS access control through the APACS API Service."""
    label = "Apollo APACS access control"
    fields = [
        {"key": "host", "label": "APACS server IP", "placeholder": "192.168.1.20", "required": True},
        {"key": "port", "label": "Port", "type": "number", "default": 8081},
        {"key": "username", "label": "APACS operator"},
        {"key": "password", "label": "Password", "type": "password"},
        {"key": "use_ssl", "label": "HTTPS", "type": "checkbox", "default": False},
        {"key": "verify_tls", "label": "Check the HTTPS certificate", "type": "checkbox", "advanced": True},
        {"key": "poll_seconds", "label": "Check for events every (seconds)", "type": "number", "default": 3, "advanced": True},
    ]
    actions = {"door": ["onepass", "unlock", "lock"], "output": ["pulse", "on", "off"], "input": ["arm", "disarm"]}
    COMMANDS = {"onepass": "OnePass", "unlock": "Unlock", "lock": "Lock", "pulse": "Pulse", "on": "On", "off": "Off",
                "arm": "Arm", "disarm": "Disarm"}             # APACS API manual, chapter 10
    # (event type, code words) -> (Ellalon event, severity, alarm?)
    RULES = [
        ("alarm", ("forced",), "door_forced", "critical", True),
        ("alarm", ("held",), "door_held", "warning", False),
        ("alarm", ("duress",), "duress", "critical", True),
        ("alarm", ("tamper",), "tamper", "high", True),
        ("alarm", ("",), "alarm", "high", True),
        ("access", ("grant",), "access_granted", "info", False),
        ("access", ("",), "access_denied", "low", False),
        ("connect", ("offline", "off"), "offline", "warning", False),
        ("connect", ("",), "online", "info", False),
        ("systemerror", ("",), "system_error", "high", False),
    ]
    RULES_TEXT = [  # pushed events (inbound URL), free text
        (("forced",), "alarm", "critical", True), (("held", "ajar", "propped"), "door_held", "warning", False),
        (("duress",), "alarm", "critical", True), (("tamper",), "tamper", "high", False),
        (("denied", "invalid", "unknown card", "rejected"), "access_denied", "low", False),
        (("granted", "valid access", "access ok"), "access_granted", "info", False), (("alarm",), "alarm", "high", True),
    ]

    async def run(self):
        async with ApacsClient(self.cfg) as a:
            await a.login()
            objs = {o["id"]: o for o in await a.objects()}
            self.sync_names(objs.values())
            self.connected = True
            self.hub.status(self.sid, "connected")
            since = datetime.now(timezone.utc).replace(tzinfo=None)
            seen: set = set()
            ticks = 0
            while not self._stop.is_set():
                for ev in await a.events_since(since):
                    key = (ev.get("Date"), (ev.get("Source") or {}).get("Id"), ev.get("Type"), ev.get("Code"))
                    if key in seen:
                        continue
                    seen.add(key)
                    t = apacs_utc(ev.get("Date"))
                    if t:                                 # 1 s overlap: same-second events are de-duplicated
                        since = max(since, t - timedelta(seconds=1))
                    await self.apacs_event(ev, objs)
                if len(seen) > 5000:
                    seen = set(list(seen)[-1000:])
                ticks += 1
                if ticks % 10 == 0:                       # refresh names and states of imported devices
                    fresh = await a.objects()
                    objs.update({o["id"]: o for o in fresh})
                    self.sync_names(fresh)
                try:
                    await asyncio.wait_for(self._stop.wait(), float(self.cfg.get("poll_seconds") or 3))
                except asyncio.TimeoutError:
                    pass

    def sync_names(self, objs):
        """Names and states of imported devices follow APACS (a door renamed there is renamed here)."""
        changed = False
        for o in objs:
            kind = apacs_kind(o["type"])
            d = self.hub.device(self.sid, kind, o["id"]) if kind else None
            if not d:
                continue
            state = {"state": o["state"]} if o["state"] is not None else {}
            if d.get("name") != o["label"] or any((d.get("state") or {}).get(k) != v for k, v in state.items()):
                self.hub.upsert(self.sid, kind, o["id"], o["label"], state)
                changed = True
        if changed:
            self.hub.save()

    async def apacs_event(self, ev: dict, objs: dict):
        src = ev.get("Source") or {}
        typ, code = str(ev.get("Type") or ""), str(ev.get("Code") or "")
        etype, sev, alarm = "event", "info", False
        for t, words, et, sv, al in self.RULES:
            if typ.lower() == t and any(w in code.lower() for w in words):
                etype, sev, alarm = et, sv, al
                break
        sid_, kind = str(src.get("Id", "")), apacs_kind(src.get("TypeId", ""))
        o = objs.get(sid_) or {}
        if src.get("TypeId", "").lower() == "aspreaderinput" and o.get("parent") in objs:   # door contact -> its door
            p = objs[o["parent"]]
            if not self.hub.device(self.sid, "input", sid_):
                sid_, kind = p["id"], "door"
        person = ev.get("Person") or {}
        who = " ".join(str(person.get(k) or "") for k in ("FirstName", "LastName")).strip() if isinstance(person, dict) else ""
        text = " ".join(x for x in (re.sub(r"(?<!^)(?=[A-Z])", " ", code) or typ, f"- {src.get('Label')}" if src.get("Label") else "") if x)
        if kind and self.hub.device(self.sid, kind, sid_):
            self.hub.upsert(self.sid, kind, sid_, None, {"last_event": text})
            self.hub.save()
            await self.hub.event(self.sid, kind, sid_, etype, sev, text, alarm=alarm, extra={"user": who} if who else None)
        elif sev != "info":                               # not imported, but important: report it on the system
            await self.hub.event(self.sid, "system", 0, etype, sev, text, alarm=alarm, extra={"user": who} if who else None)

    async def import_devices(self, csv_text=None, items=None) -> int:
        n = 0
        if items:
            for it in items:
                kind = it.get("kind") if it.get("kind") in ("door", "input", "output") else "door"
                self.hub.upsert(self.sid, kind, str(it["id"]), it.get("name"),
                                {"apacs_type": it.get("type", ""), "state": it.get("state", "")})
                n += 1
            self.hub.save()
            return n
        if csv_text and csv_text.strip():
            for row in csv.reader(io.StringIO(csv_text.strip())):
                row = [c.strip() for c in row if c is not None]
                if not row or not row[0] or row[0].lower() in ("id", "number", "door", "door id"):
                    continue
                self.hub.upsert(self.sid, "door", row[0], row[1] if len(row) > 1 and row[1] else None)
                n += 1
            self.hub.save()
            return n
        raise ValueError("Choose the doors and I/O to import")

    async def action(self, device, action):
        if action not in self.actions.get(device["kind"], []):
            raise ValueError(f"'{action}' is not possible on a {device['kind']}")
        type_id = (device.get("state") or {}).get("apacs_type") or {"door": "aspreader", "output": "relay", "input": "aspinput"}[device["kind"]]
        async with ApacsClient(self.cfg) as a:
            await a.login()
            await a.command(type_id, device["number"], self.COMMANDS[action])
        await self.hub.event(self.sid, device["kind"], device["number"], f"{device['kind']}_{action}", "info",
                             f"{action.capitalize()} from Ellalon")

    async def handle_inbound(self, ev: dict):
        door = _pick(ev, "door", "Door", "DoorId", "doorId", "ReaderId", "readerId", "reader", "Reader")
        text = str(_pick(ev, "type", "Type", "description", "Description", "event", "Event",
                         "message", "Message") or "event")
        low = text.lower()
        etype, sev, alarm = "event", "info", False
        for words, t, s, a in self.RULES_TEXT:
            if any(w in low for w in words):
                etype, sev, alarm = t, s, a
                break
        if door is not None:
            self.hub.upsert(self.sid, "door", door, _pick(ev, "door_name", "DoorName", "ReaderName"),
                            {"last_event": text})
            self.hub.save()
        user = _pick(ev, "cardholder", "CardHolder", "user", "User", "name", "Name")
        await self.hub.event(self.sid, "door" if door is not None else "system", door if door is not None else 0,
                             etype, sev, text, alarm=alarm, extra={"user": user} if user else None)


ADAPTERS = {"risco": RiscoLocalAdapter, "apollo": ApolloAdapter}
