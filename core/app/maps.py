"""Maps: floor plans / site plans with cameras, doors, inputs, outputs and links to other maps.

Each map has a background picture and a list of items placed on it. Positions are fractions of the
picture (0..1) so they stay right whatever size the map is shown at. Live status comes from the
browser (cameras, integration devices, open alarms) - the map only stores where things are.
"""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from .auth import require
from .db import Base, SessionLocal

router = APIRouter(prefix="/api")
MAX_IMAGE = 15 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml"}


class MapRow(Base):
    __tablename__ = "maps"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer, default=0)
    items: Mapped[list] = mapped_column(JSON, default=list)
    image: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    image_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    image_version: Mapped[int] = mapped_column(Integer, default=0)


class Item(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10], max_length=40)
    type: Literal["camera", "door", "input", "output", "device", "link", "label"]
    ref: str | None = Field(None, max_length=200)     # camera id, integration device id, or map id
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    angle: float = Field(0, ge=-360, le=360)          # camera direction
    fov: float = Field(70, ge=10, le=360)             # camera field of view, degrees
    range: float = Field(0.12, ge=0, le=1)            # length of the view cone (fraction of map width)
    size: float = Field(1, ge=0.4, le=3)
    label: str | None = Field(None, max_length=120)
    show_label: bool = True


class MapIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    items: list[Item] | None = None
    position: int | None = None


def out(r: MapRow) -> dict:
    return {"id": r.id, "name": r.name, "position": r.position, "items": r.items or [],
            "image": f"/api/maps/{r.id}/image?v={r.image_version}" if r.image else None}


@router.get("/maps")
def list_maps(_=Depends(require("viewer", allow_module=True))):
    with SessionLocal() as s:
        return [out(r) for r in s.query(MapRow).order_by(MapRow.position, MapRow.name)]


@router.post("/maps")
async def create_map(body: MapIn, _=Depends(require("admin"))):
    with SessionLocal() as s:
        r = MapRow(id=uuid.uuid4().hex[:12], name=body.name.strip(), items=[i.model_dump() for i in body.items or []],
                   position=body.position if body.position is not None else s.query(MapRow).count())
        s.add(r)
        s.commit()
        res = out(r)
    return res


@router.put("/maps/{mid}")
async def update_map(mid: str, body: MapIn, _=Depends(require("admin"))):
    with SessionLocal() as s:
        r = s.get(MapRow, mid)
        if not r:
            raise HTTPException(404, "No such map")
        r.name = body.name.strip()
        if body.items is not None:
            r.items = [i.model_dump() for i in body.items]
        if body.position is not None:
            r.position = body.position
        s.commit()
        res = out(r)
    return res


@router.delete("/maps/{mid}")
async def delete_map(mid: str, _=Depends(require("admin"))):
    with SessionLocal() as s:
        r = s.get(MapRow, mid)
        if not r:
            raise HTTPException(404, "No such map")
        s.delete(r)
        for o in s.query(MapRow):                         # links pointing to the deleted map go too
            if any(i.get("type") == "link" and i.get("ref") == mid for i in o.items or []):
                o.items = [i for i in o.items if not (i.get("type") == "link" and i.get("ref") == mid)]
        s.commit()
    return {"ok": True}


@router.put("/maps/{mid}/image")
async def set_image(mid: str, file: UploadFile = File(...), _=Depends(require("admin"))):
    data = await file.read(MAX_IMAGE + 1)
    if len(data) > MAX_IMAGE:
        raise HTTPException(413, "The picture is bigger than 15 MB")
    ctype = (file.content_type or "").lower()
    if ctype not in IMAGE_TYPES:
        raise HTTPException(400, "Use a PNG, JPG, WEBP, GIF or SVG picture (for a PDF plan, export a page as PNG)")
    with SessionLocal() as s:
        r = s.get(MapRow, mid)
        if not r:
            raise HTTPException(404, "No such map")
        r.image, r.image_type, r.image_version = data, ctype, (r.image_version or 0) + 1
        s.commit()
        res = out(r)
    return res


@router.get("/maps/{mid}/image")
def get_image(mid: str, _=Depends(require("viewer"))):
    with SessionLocal() as s:
        r = s.get(MapRow, mid)
        if not r or not r.image:
            raise HTTPException(404, "This map has no picture")
        headers = {"Cache-Control": "private, max-age=86400"}
        if r.image_type == "image/svg+xml":            # an uploaded SVG must not run scripts
            headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        return Response(r.image, media_type=r.image_type, headers=headers)
