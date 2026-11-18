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
APOLLO_KINDS = {  # device kind -> words that identify its API list
    "door": ("door", "reader", "portal", "accesspoint"),
    "input": ("input", "auxin", "alarmpoint", "sensor", "monitorpoint", "iopoint", "point"),
    "output": ("output", "relay", "auxout"),
    "event": ("event", "activit", "transaction", "alarm", "history"),
}
APOLLO_CANDIDATES = {  # tried when the server publishes no API description
    "door": ["/api/doors", "/api/Doors", "/api/readers", "/api/Readers", "/api/v1/doors", "/api/AccessPoints"],
    "input": ["/api/inputs", "/api/Inputs", "/api/auxinputs", "/api/AuxInputs", "/api/AlarmPoints", "/api/v1/inputs"],
    "output": ["/api/outputs", "/api/Outputs", "/api/relays", "/api/Relays", "/api/auxoutputs", "/api/AuxOutputs", "/api/v1/outputs"],
    "event": ["/api/events", "/api/Events", "/api/activities", "/api/Activities", "/api/transactions", "/api/alarms"],
}
APOLLO_CONFIG = ["/api/configuration", "/api/Configuration", "/api/config", "/api/Config", "/api/hardware",
                 "/api/Hardware", "/api/devices", "/api/Devices", "/api/controllers", "/api/Controllers", "/api/system"]
APOLLO_INDEX = ["/api", "/", "/api/$metadata", "/$metadata", "/help", "/Help", "/api/help", "/api/Help"]
APOLLO_SPECS = ["/swagger/docs/v1", "/api/swagger/docs/v1", "/swagger/v1/swagger.json", "/api/swagger/v1/swagger.json",
                "/swagger.json", "/api/swagger.json", "/openapi.json", "/api/openapi.json"]
ID_KEYS = ("id", "Id", "ID", "DoorId", "doorId", "ReaderId", "readerId", "InputId", "inputId", "OutputId", "outputId",
           "PointId", "Number", "number", "Address", "address", "Key", "key")
NAME_KEYS = ("name", "Name", "ReaderName", "DoorName", "InputName", "OutputName", "PointName", "Description",
             "description", "DisplayName", "displayName", "Label", "label", "Title")


def api_paths_in(text: str) -> set[str]:
    """Endpoint paths mentioned in an index page: links, OData service documents, $metadata, help pages."""
    found = set()
    for m in re.finditer(r"(?:GET|POST|PUT)[\s-]+/?(api/[A-Za-z][\w/{}.-]*)", text):
        found.add("/" + m.group(1))
    for m in re.finditer(r"[Hh]elp/[Aa]pi/(?:GET|POST|PUT)-(api-[\w{}-]+)", text):      # ASP.NET Web API help page
        segs = m.group(1).split("-")
        found.add("/" + "/".join("{" + x + "}" if x.lower() in ("id", "number", "key") else x for x in segs))
    for m in re.finditer(r"[\"'(/](api/[A-Za-z][\w/{}.-]*)", text):
        found.add("/" + m.group(1).rstrip("."))
    for m in re.finditer(r'EntitySet Name="(\w+)"', text):                              # OData $metadata
        found.add(f"/api/{m.group(1)}")
    try:
        doc = json.loads(text)
        if isinstance(doc, dict) and isinstance(doc.get("value"), list):                 # OData service document
            for e in doc["value"]:
                if isinstance(e, dict) and e.get("url"):
                    found.add("/api/" + str(e["url"]).lstrip("/"))
        elif isinstance(doc, dict):
            for k, v in doc.items():
                if isinstance(v, str) and "/api/" in v:
                    found.add("/api/" + v.split("/api/", 1)[1])
                elif isinstance(v, str) and v.startswith("api/"):
                    found.add("/" + v)
        elif isinstance(doc, list) and all(isinstance(x, str) for x in doc):
            found |= {"/api/" + x.strip("/") for x in doc if re.fullmatch(r"[A-Za-z][\w/-]*", x.strip("/"))}
    except ValueError:
        pass
    for m in re.finditer(r'<collection href="([^"]+)"', text):                           # OData XML service document
        found.add("/api/" + m.group(1).lstrip("/"))
    return {re.sub(r"/{2,}", "/", p) for p in found if len(p) < 120}


class ApolloClient:
    """Talks to the APACS API Service (APACSAPIService, HTTPS port 8081 by default, operator login).

    Apollo does not publish its endpoint list, so the client finds it: it reads the API description
    (Swagger/OpenAPI) when the server offers one, otherwise it tries the usual paths. The login style
    is detected too (HTTP Basic, then a token login). Paths set by hand in the settings always win."""

    def __init__(self, cfg: dict):
        self.cfg = dict(cfg)
        host = (cfg.get("host") or "").strip()
        if not host and cfg.get("base_url"):                 # settings from older versions
            from urllib.parse import urlsplit
            u = urlsplit(cfg["base_url"])
            host = u.hostname or ""
            self.cfg.setdefault("port", u.port or 8081)
            self.cfg.setdefault("use_ssl", u.scheme == "https")
        self.host = host
        self.port = int(self.cfg.get("port") or 8081)
        self.scheme = "https" if self.cfg.get("use_ssl", True) else "http"
        self.base = f"{self.scheme}://{self.host}:{self.port}"
        self.user, self.pw = cfg.get("username", ""), cfg.get("password", "")
        self.auth_mode = cfg.get("auth_mode") or ""
        self.token = None
        self.c = None
        self.steps: list[dict] = []
        self.paths = {k: cfg.get(f"{k}s_path") or "" for k in ("door", "input", "output", "event")}
        self.paths["unlock"] = cfg.get("unlock_path") or ""
        self.probes: dict[str, str] = {}               # path -> what the server answered (for diagnosis)
        self.listed: set[str] = set()                  # endpoints the server itself mentions
        self.root_text = ""

    async def __aenter__(self):
        if not self.host:
            raise ValueError("Enter the APACS server IP address")
        self.c = httpx.AsyncClient(base_url=self.base, verify=bool(self.cfg.get("verify_tls")), timeout=10,
                                   follow_redirects=True)
        return self

    async def __aexit__(self, *a):
        await self.c.aclose()

    def step(self, ok: bool, text: str):
        self.steps.append({"ok": ok, "text": text})

    # ---------------------------------------------------------------- login
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def request(self, method, path, **kw):
        auth = (self.user, self.pw) if self.auth_mode == "basic" and self.user else None
        r = await self.c.request(method, path, auth=auth, headers=self._headers(), **kw)
        if method == "GET":
            ct = (r.headers.get("content-type") or "").split(";")[0] or "no type"
            self.probes[path] = f"{r.status_code} {ct} {len(r.content)} B"
        return r

    async def connect(self):
        try:
            r = await self.c.get("/api")
        except httpx.ConnectError as e:
            hint = " - is the APACS API Service started, and the port open in the Windows firewall?"
            if "SSL" in str(e) or "certificate" in str(e).lower():
                hint = " - HTTPS problem: untick 'Check the HTTPS certificate', or the service may run plain HTTP (untick HTTPS)"
            raise ValueError(f"Cannot reach {self.base}{hint}")
        except httpx.HTTPError as e:
            raise ValueError(f"Cannot reach {self.base} ({e.__class__.__name__})")
        self.step(True, f"Reached the APACS API Service at {self.base}")
        if self.auth_mode == "token":
            await self._token_login()
        elif self.auth_mode != "basic":
            await self._detect_login(r)

    async def _detect_login(self, first):
        r = await self.c.get("/api", auth=(self.user, self.pw)) if self.user else first
        if r.status_code not in (401, 403):
            self.auth_mode = "basic"                           # confirmed later on a data request
            return
        if await self._token_login(quiet=True):
            return
        raise ValueError("APACS rejected the operator login - check the user name / password (an APACS operator), "
                         "and that the APACS licence allows web sessions")

    async def _token_login(self, quiet=False) -> bool:
        tries = [("/token", "form"), ("/api/token", "form"), ("/api/login", "json"), ("/api/auth/login", "json"),
                 ("/api/Login", "json"), ("/api/session", "json")]
        for path, kind in tries:
            try:
                if kind == "form":
                    r = await self.c.post(path, data={"grant_type": "password", "username": self.user, "password": self.pw})
                else:
                    r = await self.c.post(path, json={"username": self.user, "password": self.pw,
                                                      "UserName": self.user, "Password": self.pw})
            except httpx.HTTPError:
                continue
            if r.status_code in (200, 201):
                try:
                    j = r.json()
                except ValueError:
                    j = {}
                tok = _pick(j, "access_token", "token", "Token", "AccessToken", "accessToken", "SessionId", "sessionId") \
                    if isinstance(j, dict) else (j if isinstance(j, str) else None)
                if tok or r.cookies:
                    self.token = str(tok) if tok else None
                    self.auth_mode = "token"
                    self.cfg["token_path"] = path
                    self.step(True, f"Logged in as {self.user} (token login {path})")
                    return True
        if not quiet:
            raise ValueError("APACS rejected the operator login")
        return False

    # ---------------------------------------------------------------- discovery
    async def discover(self):
        await self._index_pages()
        spec = None
        for p in APOLLO_SPECS:
            try:
                r = await self.request("GET", p)
                if r.status_code == 200 and "paths" in r.text[:200000]:
                    spec = r.json()
                    break
            except (httpx.HTTPError, ValueError):
                continue
        if spec:
            n = self._from_spec(spec)
            self.step(True, f"Read the API description ({n} endpoints)")
        elif self.listed:
            self._from_spec({"paths": {p: {"get": {}, "post": {}} for p in self.listed}})
            self.step(True, f"The server lists {len(self.listed)} endpoint(s): " + ", ".join(sorted(self.listed)[:12])
                      + ("…" if len(self.listed) > 12 else ""))
        else:
            self.step(True, "The server publishes no API description - trying the usual endpoints")

    async def _index_pages(self):
        """Root page, OData service document / $metadata, ASP.NET or WCF help pages: collect endpoint names."""
        for p in APOLLO_INDEX:
            try:
                r = await self.request("GET", p)
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            text = r.text[:400000]
            if p == "/api":
                self.root_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()[:200]
            self.listed |= api_paths_in(text)

    @staticmethod
    def rows_for(kind, data) -> list:
        """The rows of one kind in an answer: a plain list, a wrapped list, or a list inside a configuration object."""
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        wrapped = _as_list(data)
        if wrapped and wrapped is not None and not (len(wrapped) == 1 and wrapped[0] is data):
            return wrapped
        words = APOLLO_KINDS[kind]
        for k, v in data.items():                         # configuration: {"Readers": [...], "Inputs": [...]}
            if isinstance(v, list) and any(w in k.lower() for w in words):
                if kind == "input" and "output" in k.lower():
                    continue
                return v
        for v in data.values():                            # one level deeper
            if isinstance(v, dict):
                rows = ApolloClient.rows_for(kind, v)
                if rows:
                    return rows
        return []

    def diagnosis(self) -> str:
        shown = list(self.probes.items())[:40]
        return "; ".join(f"{p} → {a}" for p, a in shown)

    def _from_spec(self, spec) -> int:
        base = (spec.get("basePath") or "").rstrip("/")
        paths = spec.get("paths") or {}
        gets = [base + p for p, ops in paths.items() if "get" in ops and "{" not in p]
        posts = [(base + p) for p, ops in paths.items() if ("post" in ops or "put" in ops)]
        for kind, words in APOLLO_KINDS.items():
            if self.paths.get(kind):
                continue
            hits = [p for p in gets if any(w in p.rsplit("/", 1)[-1].lower() for w in words)]
            if kind == "input":
                hits = [p for p in hits if "output" not in p.lower()]
            if hits:
                self.paths[kind] = min(hits, key=len)
        if not self.paths.get("unlock"):
            for p in posts:
                low = p.lower()
                if any(w in low for w in ("door", "reader")) and any(w in low for w in ("unlock", "open", "grant", "pulse", "momentary")):
                    self.paths["unlock"] = re.sub(r"\{[^}]+\}", "{door}", p, count=1)
                    break
        return len(paths)

    async def fetch(self, kind) -> list[dict]:
        tried = [self.paths[kind]] if self.paths.get(kind) else APOLLO_CANDIDATES[kind] + APOLLO_CONFIG
        for path in tried:
            try:
                r = await self.request("GET", path)
            except httpx.HTTPError:
                continue
            if r.status_code in (401, 403):
                if self.auth_mode == "basic" and await self._token_login(quiet=True):
                    r = await self.request("GET", path)
                if r.status_code in (401, 403):
                    raise ValueError("APACS refused access with this operator - check the operator's rights")
            if r.status_code != 200:
                continue
            try:
                rows = self.rows_for(kind, r.json())
            except ValueError:
                continue
            if not rows and path in APOLLO_CONFIG:
                continue
            items = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                num = _pick(row, *ID_KEYS)
                if num is None:
                    continue
                items.append({"kind": kind, "id": str(num), "name": str(_pick(row, *NAME_KEYS) or f"{kind.title()} {num}"),
                              "where": str(_pick(row, "ControllerName", "Controller", "PanelName", "Panel", "Site",
                                                  "SiteName", "Area", "Location") or "")})
            self.paths[kind] = path
            return items
        self.paths[kind] = ""
        return []

    def discovered(self) -> dict:
        out = {f"{k}s_path": v for k, v in self.paths.items() if k in ("door", "input", "output", "event") and v}
        if self.paths.get("unlock"):
            out["unlock_path"] = self.paths["unlock"]
        out["auth_mode"] = self.auth_mode
        return out


async def apollo_test(cfg: dict) -> dict:
    """Connect, log in, find the endpoints and list every door / input / output."""
    found = {"door": [], "input": [], "output": []}
    if not (cfg.get("host") or cfg.get("base_url") or "").strip():
        return {"ok": False, "steps": [{"ok": False, "text": "Enter the APACS server IP address"}], "devices": [], "found": {}}
    async with ApolloClient(cfg) as a:
        try:
            await a.connect()
            await a.discover()
            for kind, label in (("door", "doors"), ("input", "inputs"), ("output", "outputs")):
                found[kind] = await a.fetch(kind)
                a.step(bool(found[kind]), f"{len(found[kind])} {label}" + (f" ({a.paths[kind]})" if a.paths[kind] else " - no list found"))
            if a.auth_mode == "basic" and any(found.values()):
                a.step(True, f"Logged in as {a.user} (basic login)")
            elif a.auth_mode == "basic":
                a.step(False, "The API did not ask for a login on its start page - its own login method was not recognised")
            await a.fetch("event") if not a.paths.get("event") else None
            a.step(bool(a.paths.get("event")), "Live events: " + (a.paths["event"] or "no event list found - events can be pushed to Ellalon instead"))
            a.step(bool(a.paths.get("unlock")), "Door unlock: " + (a.paths["unlock"] or "not found (set it under Advanced)"))
        except ValueError as e:
            a.step(False, str(e))
            return {"ok": False, "steps": a.steps, "devices": [], "found": {}}
        devices = found["door"] + found["input"] + found["output"]
        ok = bool(devices)
        if not ok:
            if a.root_text:
                a.step(False, f"The API's start page says: {a.root_text}")
            a.step(False, "What the server answered: " + a.diagnosis())
            a.step(False, "Connected, but no doors, inputs or outputs were found. Send this list to support, or set the "
                          "endpoint paths under Advanced (from the API documentation on the APACS server).")
        return {"ok": ok, "steps": a.steps, "devices": devices, "found": a.discovered()}


class ApolloAdapter(Adapter):
    """Apollo APACS access control through the APACS API Service."""
    label = "Apollo APACS access control"
    fields = [
        {"key": "host", "label": "APACS server IP", "placeholder": "192.168.1.20", "required": True},
        {"key": "port", "label": "Port", "type": "number", "default": 8081},
        {"key": "username", "label": "APACS operator"},
        {"key": "password", "label": "Password", "type": "password"},
        {"key": "use_ssl", "label": "HTTPS (APACS default)", "type": "checkbox", "default": True},
        {"key": "verify_tls", "label": "Check the HTTPS certificate", "type": "checkbox", "advanced": True},
        {"key": "doors_path", "label": "Doors list path", "advanced": True, "placeholder": "found automatically"},
        {"key": "inputs_path", "label": "Inputs list path", "advanced": True, "placeholder": "found automatically"},
        {"key": "outputs_path", "label": "Outputs list path", "advanced": True, "placeholder": "found automatically"},
        {"key": "events_path", "label": "Events list path", "advanced": True, "placeholder": "found automatically"},
        {"key": "unlock_path", "label": "Door unlock path, {door} = door id", "advanced": True, "placeholder": "found automatically"},
        {"key": "auth_mode", "label": "Login method", "advanced": True, "placeholder": "found automatically"},
        {"key": "poll_seconds", "label": "Event poll interval (seconds)", "type": "number", "default": 5, "advanced": True},
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

    async def run(self):
        async with ApolloClient(self.cfg) as a:
            await a.connect()
            self.connected = True
            self.hub.status(self.sid, "connected")
            if not a.paths.get("event"):
                self.hub.status(self.sid, "connected", "No event list on the API - events can be pushed to "
                                                       f"/modules/integrations/inbound/{self.sid}")
                await self._stop.wait()
                return
            seen: dict[str, None] = {}
            first = True
            while not self._stop.is_set():
                r = await a.request("GET", a.paths["event"])
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

    async def import_devices(self, csv_text=None, items=None) -> int:
        n = 0
        if items:
            for it in items:
                kind = it.get("kind") if it.get("kind") in ("door", "input", "output") else "door"
                self.hub.upsert(self.sid, kind, str(it["id"]), it.get("name"))
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
        if action != "unlock" or device["kind"] != "door":
            raise ValueError(f"'{action}' is not possible on a {device['kind']}")
        if not self.cfg.get("unlock_path"):
            raise ValueError("No door unlock path - run Test connection or set it under Advanced in the system settings")
        async with ApolloClient(self.cfg) as a:
            await a.connect()
            r = await a.request("POST", self.cfg["unlock_path"].replace("{door}", str(device["number"])))
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
