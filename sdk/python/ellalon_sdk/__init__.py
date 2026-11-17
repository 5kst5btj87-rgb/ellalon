"""Ellalon module SDK.

Every module built on this gets, for free:
  * heartbeat to core (liveness shown in the UI)
  * command handling from the C&C layer      -> @module.command("name")
  * reactions to platform broadcasts         -> @module.on_broadcast("camera.changed")
  * event publishing                         -> await module.publish("motion", {...})
  * authenticated calls to the core API      -> await module.cameras()
  * a FastAPI app with /health and /info     -> module.app (add your own routes)

Minimal module:
    from ellalon_sdk import EllalonModule
    m = EllalonModule(version="0.1.0")

    @m.command("ping")
    async def ping(args): return {"pong": True}

    app = m.app     # run with: uvicorn app:app --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import logging
import os
import socket
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI

Handler = Callable[[dict], Awaitable[Any]]
log = logging.getLogger("ellalon_sdk")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class EllalonModule:
    def __init__(self, name: str | None = None, version: str = "0.1.0", heartbeat_every: int = 10):
        self.name = name or os.environ.get("ELLALON_MODULE_NAME", "unnamed")
        self.version = version
        self.core_url = os.environ.get("ELLALON_CORE_URL", "http://ellalon-core:8000")
        self.token = os.environ.get("ELLALON_MODULE_TOKEN", "")
        self.base_path = os.environ.get("ELLALON_BASE_PATH", "")
        self.redis = aioredis.from_url(os.environ.get("ELLALON_REDIS_URL", "redis://redis:6379/0"),
                                       decode_responses=True)
        self.heartbeat_every = heartbeat_every
        self.started = time.time()
        self.status: dict[str, Any] = {}          # free-form, shown in UI via heartbeat
        self._commands: dict[str, Handler] = {}
        self._broadcasts: dict[str, list[Handler]] = {}
        self._startup: list[Callable[[], Awaitable[None]]] = []
        self._background: list[Callable[[], Awaitable[None]]] = []
        self._tasks: list[asyncio.Task] = []
        self.log = logging.getLogger(self.name)

        self.app = FastAPI(title=f"Ellalon module: {self.name}", version=version,
                           lifespan=self._lifespan, root_path=self.base_path)

        @self.app.get("/health")
        async def _health():
            return {"status": "ok", "module": self.name}

        @self.app.get("/info")
        async def _info():
            return {"module": self.name, "version": self.version, "status": self.status,
                    "commands": sorted(self._commands)}

    # ------------------------------------------------------------ decorators
    def command(self, name: str):
        def deco(fn: Handler):
            self._commands[name] = fn
            return fn
        return deco

    def on_broadcast(self, type_: str):
        def deco(fn: Handler):
            self._broadcasts.setdefault(type_, []).append(fn)
            return fn
        return deco

    def on_startup(self, fn):
        self._startup.append(fn)
        return fn

    def background(self, fn):
        """Long-running coroutine; restarted automatically if it crashes."""
        self._background.append(fn)
        return fn

    # ------------------------------------------------------------ API
    async def publish(self, type_: str, payload: dict | None = None, severity: str = "info",
                      camera_id: int | None = None):
        await self.redis.publish("ellalon.events", json.dumps({
            "source": self.name, "type": type_, "payload": payload or {},
            "severity": severity, "camera_id": camera_id}, default=str))

    async def api(self, method: str, path: str, **kw) -> Any:
        async with httpx.AsyncClient(base_url=self.core_url, timeout=10,
                                     headers={"Authorization": f"Bearer {self.token}"}) as c:
            r = await c.request(method, path, **kw)
            r.raise_for_status()
            return r.json()

    async def cameras(self) -> list[dict]:
        return await self.api("GET", "/api/cameras")

    async def send_command(self, module: str, command: str, args: dict | None = None):
        return await self.api("POST", f"/api/commands/{module}",
                              json={"command": command, "args": args or {}})

    # ------------------------------------------------------------ internals
    async def _heartbeat(self):
        while True:
            hb = {"module": self.name, "version": self.version, "host": socket.gethostname(),
                  "uptime": int(time.time() - self.started), "status": self.status}
            try:
                await self.redis.set(f"ellalon:hb:{self.name}", json.dumps(hb, default=str),
                                     ex=self.heartbeat_every * 3)
            except Exception as e:
                self.log.warning("heartbeat failed: %s", e)
            await asyncio.sleep(self.heartbeat_every)

    async def _listen(self):
        while True:
            try:
                ps = self.redis.pubsub()
                await ps.subscribe(f"ellalon.cmd.{self.name}", "ellalon.broadcast")
                async for msg in ps.listen():
                    if msg["type"] != "message":
                        continue
                    data = json.loads(msg["data"])
                    if msg["channel"] == "ellalon.broadcast":
                        for h in self._broadcasts.get(data.get("type"), []):
                            asyncio.create_task(self._safe(h, data.get("payload", {})))
                    else:
                        asyncio.create_task(self._run_command(data))
            except Exception as e:
                self.log.error("bus listener error: %s; reconnecting", e)
                await asyncio.sleep(2)

    async def _run_command(self, msg: dict):
        h = self._commands.get(msg.get("command"))
        result = {"id": msg.get("id"), "command": msg.get("command")}
        if not h:
            result.update(ok=False, error="unknown command", available=sorted(self._commands))
        else:
            try:
                result.update(ok=True, result=await h(msg.get("args") or {}))
            except Exception as e:
                self.log.exception("command failed")
                result.update(ok=False, error=str(e))
        await self.publish("command.result", result, "info" if result["ok"] else "warning")

    async def _safe(self, fn, *args):
        try:
            await fn(*args)
        except Exception:
            self.log.exception("handler %s failed", getattr(fn, "__name__", fn))

    async def _supervise(self, fn):
        while True:
            try:
                await fn()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("background task %s crashed; restarting in 5s", fn.__name__)
                await asyncio.sleep(5)

    @asynccontextmanager
    async def _lifespan(self, app):
        for fn in self._startup:
            await self._safe(fn)
        self._tasks = [asyncio.create_task(self._heartbeat()), asyncio.create_task(self._listen())]
        self._tasks += [asyncio.create_task(self._supervise(f)) for f in self._background]
        await self.publish("module.online", {"version": self.version})
        yield
        for t in self._tasks:
            t.cancel()
