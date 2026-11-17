"""API connectivity module.

Outbound: webhooks subscribe to event types (glob patterns, e.g. "face.*", "analytics.motion")
          and minimum severity; each delivery is HMAC-SHA256 signed (X-Ellalon-Signature).
Inbound:  POST /inbound/{source} lets external systems (access control, alarm panels, PSIM,
          LPR cameras...) inject events into the platform bus.
"""
import asyncio
import fnmatch
import hashlib
import hmac
import json
import uuid
from pathlib import Path

import httpx
from pydantic import BaseModel, HttpUrl

from ellalon_sdk import EllalonModule

STORE = Path("/data/webhooks.json")
SEV = {"info": 0, "low": 1, "warning": 2, "high": 3, "critical": 4}

m = EllalonModule(version="0.1.0")
app = m.app


class Webhook(BaseModel):
    url: HttpUrl
    events: list[str] = ["*"]
    min_severity: str = "info"
    secret: str = ""
    enabled: bool = True


hooks: dict[str, dict] = json.loads(STORE.read_text()) if STORE.exists() else {}


def save():
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(hooks, indent=2))


async def deliver(hook: dict, ev: dict):
    body = json.dumps(ev).encode()
    headers = {"Content-Type": "application/json"}
    if hook.get("secret"):
        headers["X-Ellalon-Signature"] = hmac.new(hook["secret"].encode(), body, hashlib.sha256).hexdigest()
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.post(hook["url"], content=body, headers=headers)
                if r.status_code < 500:
                    return
        except Exception:
            pass
        await asyncio.sleep(2 ** attempt)
    m.log.warning("webhook delivery failed: %s", hook["url"])


@m.background
async def forward_events():
    ps = m.redis.pubsub()
    await ps.subscribe("ellalon.events")
    async for msg in ps.listen():
        if msg["type"] != "message":
            continue
        ev = json.loads(msg["data"])
        if ev.get("source") == m.name and ev.get("type") != "inbound":
            continue
        for h in hooks.values():
            if (h["enabled"] and SEV.get(ev.get("severity"), 0) >= SEV.get(h["min_severity"], 0)
                    and any(fnmatch.fnmatch(ev.get("type", ""), p) for p in h["events"])):
                asyncio.create_task(deliver(h, ev))


@app.get("/webhooks")
def list_hooks():
    return [{"id": k, **{**v, "secret": "***" if v.get("secret") else ""}} for k, v in hooks.items()]


@app.post("/webhooks")
def add_hook(body: Webhook):
    hid = uuid.uuid4().hex[:10]
    hooks[hid] = {**body.model_dump(), "url": str(body.url)}
    save()
    return {"id": hid}


@app.delete("/webhooks/{hid}")
def del_hook(hid: str):
    hooks.pop(hid, None)
    save()
    return {"ok": True}


class InboundEvent(BaseModel):
    type: str
    severity: str = "info"
    camera_id: int | None = None
    payload: dict = {}


@app.post("/inbound/{source}")
async def inbound(source: str, ev: InboundEvent):
    """External system -> platform. Event type becomes '<source>.<type>'."""
    await m.publish(f"{source}.{ev.type}", {"external_source": source, **ev.payload},
                    ev.severity, ev.camera_id)
    return {"ok": True}
