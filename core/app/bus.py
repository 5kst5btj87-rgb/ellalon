"""Redis-based message bus.

Channels:
  ellalon.events        modules -> core   (every event; core stores + fans out to clients)
  ellalon.broadcast     core -> all modules (camera.changed, config.changed, ...)
  ellalon.cmd.<module>  core -> one module (C&C commands)
Keys:
  ellalon:hb:<module>   heartbeat JSON written by each module, TTL-based liveness
"""
import asyncio
import json
import logging
from datetime import datetime

import redis.asyncio as aioredis
from fastapi import WebSocket

from .config import settings
from .db import Event, SessionLocal

log = logging.getLogger("bus")
EVENTS = "ellalon.events"
BROADCAST = "ellalon.broadcast"


class Bus:
    def __init__(self):
        self.r = aioredis.from_url(settings.redis_url, decode_responses=True)
        self.clients: set[WebSocket] = set()

    async def broadcast(self, type_: str, payload: dict | None = None):
        await self.r.publish(BROADCAST, json.dumps({"type": type_, "payload": payload or {}}))

    async def command(self, module: str, command: str, args: dict, issued_by: str) -> str:
        cid = f"{module}-{datetime.utcnow().timestamp():.6f}"
        msg = {"id": cid, "command": command, "args": args, "issued_by": issued_by}
        await self.r.publish(f"ellalon.cmd.{module}", json.dumps(msg))
        return cid

    async def heartbeat(self, module: str) -> dict | None:
        raw = await self.r.get(f"ellalon:hb:{module}")
        return json.loads(raw) if raw else None

    async def emit(self, source: str, type_: str, payload=None, severity="info", camera_id=None):
        """Core-originated events go through the same pipeline as module events."""
        await self.r.publish(EVENTS, json.dumps({
            "source": source, "type": type_, "payload": payload or {},
            "severity": severity, "camera_id": camera_id}))

    async def consume(self):
        """Persist every event and push it to connected clients (web, Windows, ...)."""
        while True:
            try:
                ps = self.r.pubsub()
                await ps.subscribe(EVENTS)
                async for msg in ps.listen():
                    if msg["type"] != "message":
                        continue
                    try:
                        ev = json.loads(msg["data"])
                    except ValueError:
                        continue
                    stored = await asyncio.to_thread(self._store, ev)
                    await self._fanout(stored)
            except Exception as e:
                log.error("Event consumer error: %s; retrying", e)
                await asyncio.sleep(2)

    @staticmethod
    def _store(ev: dict) -> dict:
        with SessionLocal() as s:
            row = Event(source=str(ev.get("source", "?"))[:64], type=str(ev.get("type", "?"))[:64],
                        severity=ev.get("severity", "info"), camera_id=ev.get("camera_id"),
                        payload=ev.get("payload") or {})
            s.add(row)
            s.commit()
            return event_dict(row)

    async def _fanout(self, ev: dict):
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json({"kind": "event", "data": ev})
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


def event_dict(e: Event) -> dict:
    return {"id": e.id, "ts": e.ts.isoformat() if e.ts else None, "source": e.source,
            "type": e.type, "severity": e.severity, "camera_id": e.camera_id,
            "payload": e.payload}


bus = Bus()
