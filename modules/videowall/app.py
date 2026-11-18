"""Video wall module.

* Layouts: named grids (e.g. 2x2, 3x3) with a camera per tile.
* Walls: physical/virtual displays; each shows one layout. Any client that renders a wall
  (browser on a decoder PC, Windows client, ...) subscribes to `wall.updated` events over the
  core WebSocket and re-renders when an operator pushes a camera/layout to it.
* Keeps the go2rtc streaming gateway in sync with the camera list (stream name = cam_<id>).
"""
import asyncio
import json
import os
from pathlib import Path

import httpx
from fastapi import HTTPException
from pydantic import BaseModel

from ellalon_sdk import EllalonModule

GO2RTC = os.environ.get("GO2RTC_URL", "http://ellalon-streaming:1984")
STORE = Path("/data/videowall.json")

m = EllalonModule(version="0.1.0")
app = m.app


class Layout(BaseModel):
    name: str
    rows: int = 2
    cols: int = 2
    tiles: list[int | None] = []   # camera ids, row-major


class Wall(BaseModel):
    name: str
    layout: str | None = None


def load() -> dict:
    if STORE.exists():
        return json.loads(STORE.read_text())
    return {"layouts": {"Default 2x2": {"name": "Default 2x2", "rows": 2, "cols": 2, "tiles": []}},
            "walls": {"Main wall": {"name": "Main wall", "layout": "Default 2x2"}}}


def save(d: dict):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(d, indent=2))


state = load()


# ---------------------------------------------------------------- go2rtc sync
def sources(url: str) -> list[tuple[str, str]]:
    """go2rtc sources for one camera stream, tried in order:
    1. direct RTSP without the two-way-audio request (some NVRs then return no tracks at all)
    2. through ffmpeg, which copes with unusual streams (audio converted to AAC for browsers)"""
    if not url.startswith("rtsp"):
        return [("src", url)]
    return [("src", url + "#backchannel=0"), ("src", f"ffmpeg:{url}#video=copy#audio=aac")]


async def sync_streams():
    cams = [c for c in await m.cameras() if c["enabled"]]
    async with httpx.AsyncClient(base_url=GO2RTC, timeout=5) as c:
        existing = (await c.get("/api/streams")).json() or {}
        wanted = set()
        for cam in cams:
            name = f"cam_{cam['id']}"
            wanted.add(name)
            await c.put("/api/streams", params=[("name", name), *sources(cam["url"])])
            if cam.get("substream_url"):
                wanted.add(name + "_sub")
                await c.put("/api/streams", params=[("name", name + "_sub"), *sources(cam["substream_url"])])
        for name in existing:
            if name.startswith("cam_") and name not in wanted:
                await c.delete("/api/streams", params={"src": name})
    m.status["streams"] = len(wanted)


@m.on_broadcast("camera.changed")
async def _cam_changed(_):
    await sync_streams()


@m.background
async def periodic_sync():
    while True:
        try:
            await sync_streams()
        except Exception as e:
            m.log.warning("stream sync failed (streaming module up?): %s", e)
        await asyncio.sleep(30)


# ---------------------------------------------------------------- API
@app.get("/layouts")
def layouts():
    return list(state["layouts"].values())


@app.put("/layouts/{name}")
async def put_layout(name: str, body: Layout):
    body.name = name
    state["layouts"][name] = body.model_dump()
    save(state)
    await m.publish("layout.updated", body.model_dump())
    return body


@app.delete("/layouts/{name}")
def delete_layout(name: str):
    state["layouts"].pop(name, None)
    save(state)
    return {"ok": True}


@app.get("/walls")
def walls():
    return [{**w, "layout_data": state["layouts"].get(w["layout"])} for w in state["walls"].values()]


@app.put("/walls/{name}")
async def put_wall(name: str, body: Wall):
    body.name = name
    state["walls"][name] = body.model_dump()
    save(state)
    await m.publish("wall.updated", {**body.model_dump(), "layout_data": state["layouts"].get(body.layout)})
    return body


# ---------------------------------------------------------------- C&C commands
@m.command("push_camera")
async def push_camera(args):
    """args: {wall, tile, camera_id} - put a camera on a wall tile (e.g. on alarm)."""
    wall = state["walls"].get(args["wall"])
    if not wall:
        raise HTTPException(404, "wall not found")
    lay = state["layouts"][wall["layout"]]
    tiles = lay["tiles"] + [None] * (lay["rows"] * lay["cols"] - len(lay["tiles"]))
    tiles[int(args.get("tile", 0))] = args["camera_id"]
    lay["tiles"] = tiles
    save(state)
    await m.publish("wall.updated", {**wall, "layout_data": lay})
    return {"wall": wall["name"], "tiles": tiles}


@m.command("set_layout")
async def set_layout(args):
    state["walls"][args["wall"]]["layout"] = args["layout"]
    save(state)
    w = state["walls"][args["wall"]]
    await m.publish("wall.updated", {**w, "layout_data": state["layouts"].get(w["layout"])})
    return w
