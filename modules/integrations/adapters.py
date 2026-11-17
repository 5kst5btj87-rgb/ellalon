"""Adapters for 3rd-party security systems.

An adapter connects to one system, imports its devices into the hub, keeps device state live,
turns alarms into platform events and executes control actions. To support a new system,
subclass Adapter, describe its settings in `fields`, and add it to ADAPTERS.
"""
import asyncio
import csv
import io
import json
import logging

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
class ApolloAdapter(Adapter):
    """Apollo APACS access control.

    Live link: the APACS API Service (APACS 3.7+, licence with web sessions, HTTPS on port 8081
    by default) with an APACS operator login. Apollo does not publish the endpoint list, so the
    doors / events / unlock paths are configurable - take them from the API documentation on
    your APACS server. Without the API, doors can be imported from a CSV export and events can be
    pushed to POST /modules/integrations/inbound/<system id>.
    """
    label = "Apollo APACS access control (beta)"
    fields = [
        {"key": "base_url", "label": "APACS API address (optional)",
         "placeholder": "https://apacs-server:8081/api"},
        {"key": "username", "label": "APACS operator"},
        {"key": "password", "label": "Operator password", "type": "password"},
        {"key": "verify_tls", "label": "Verify TLS certificate", "type": "checkbox"},
        {"key": "doors_path", "label": "Doors endpoint path (from APACS API docs)"},
        {"key": "events_path", "label": "Events endpoint path (from APACS API docs)"},
        {"key": "unlock_path", "label": "Unlock endpoint path, {door} = door id"},
        {"key": "poll_seconds", "label": "Event poll interval (seconds)", "type": "number", "default": 5},
    ]
    actions = {"door": ["unlock"]}
    RULES = [  # (text contains, event type, severity, alarm?)
        (("forced",), "alarm", "critical", True),
        (("held", "ajar", "propped"), "door_held", "warning", False),
        (("duress",), "alarm", "critical", True),
        (("tamper",), "tamper", "high", False),
        (("denied", "invalid", "unknown card", "rejected"), "access_denied", "low", False),
        (("granted", "valid access", "access ok"), "access_granted", "info", False),
        (("alarm",), "alarm", "high", True),
    ]

    def client(self) -> httpx.AsyncClient:
        auth = (self.cfg.get("username", ""), self.cfg.get("password", "")) if self.cfg.get("username") else None
        return httpx.AsyncClient(base_url=self.cfg.get("base_url", "").rstrip("/"), auth=auth,
                                 verify=bool(self.cfg.get("verify_tls")), timeout=10)

    async def run(self):
        if not self.cfg.get("base_url"):
            self.hub.status(self.sid, "manual", "No API address - import doors from CSV, push events "
                                                f"to /modules/integrations/inbound/{self.sid}")
            await self._stop.wait()
            return
        async with self.client() as c:
            r = await c.get("/")
            if r.status_code in (401, 403):
                raise RuntimeError("APACS rejected the operator login")
            self.connected = True
            self.hub.status(self.sid, "connected")
            if self.cfg.get("doors_path"):
                await self._import_api(c)
            seen: dict[str, None] = {}
            first = True
            while not self._stop.is_set():
                if self.cfg.get("events_path"):
                    r = await c.get(self.cfg["events_path"])
                    r.raise_for_status()
                    for ev in _as_list(r.json()):
                        key = str(_pick(ev, "id", "Id", "ID", "EventId", "eventId")
                                  or json.dumps(ev, sort_keys=True, default=str))
                        if key in seen:
                            continue
                        seen[key] = None
                        if not first:  # do not replay history on start
                            await self.handle_inbound(ev)
                    first = False
                    if len(seen) > 5000:
                        seen = dict.fromkeys(list(seen)[-2000:])
                try:
                    await asyncio.wait_for(self._stop.wait(), float(self.cfg.get("poll_seconds") or 5))
                except asyncio.TimeoutError:
                    pass

    async def _import_api(self, c) -> int:
        r = await c.get(self.cfg["doors_path"])
        r.raise_for_status()
        n = 0
        for d in _as_list(r.json()):
            num = _pick(d, "id", "Id", "ID", "DoorId", "doorId", "ReaderId", "readerId", "number")
            if num is None:
                continue
            self.hub.upsert(self.sid, "door", num, _pick(d, "name", "Name", "Description", "description"))
            n += 1
        self.hub.save()
        return n

    async def import_devices(self, csv_text=None) -> int:
        if csv_text and csv_text.strip():
            n = 0
            for row in csv.reader(io.StringIO(csv_text.strip())):
                row = [c.strip() for c in row if c is not None]
                if not row or not row[0] or row[0].lower() in ("id", "number", "door", "door id"):
                    continue
                self.hub.upsert(self.sid, "door", row[0], row[1] if len(row) > 1 and row[1] else None)
                n += 1
            self.hub.save()
            return n
        if self.cfg.get("base_url") and self.cfg.get("doors_path"):
            async with self.client() as c:
                return await self._import_api(c)
        raise ValueError("Paste a CSV (door id, door name per line) or set the doors endpoint path")

    async def action(self, device, action):
        if action != "unlock" or device["kind"] != "door":
            raise ValueError(f"'{action}' is not possible on a {device['kind']}")
        if not (self.cfg.get("base_url") and self.cfg.get("unlock_path")):
            raise ValueError("Set the APACS API address and unlock endpoint path in the system settings")
        async with self.client() as c:
            r = await c.post(self.cfg["unlock_path"].replace("{door}", str(device["number"])))
        if r.status_code >= 400:
            raise ValueError(f"APACS returned HTTP {r.status_code}")
        await self.hub.event(self.sid, "door", device["number"], "door_unlocked", "info",
                             "Door unlocked from Ellalon")

    async def handle_inbound(self, ev: dict):
        door = _pick(ev, "door", "Door", "DoorId", "doorId", "ReaderId", "readerId", "reader", "Reader")
        text = str(_pick(ev, "type", "Type", "description", "Description", "event", "Event",
                         "message", "Message") or "event")
        low = text.lower()
        etype, sev, alarm = "event", "info", False
        for words, t, s, a in self.RULES:
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
