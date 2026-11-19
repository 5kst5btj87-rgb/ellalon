"""Maps: floor plans / site plans with cameras, doors, inputs, outputs and links to other maps.

Each map has a background picture and a list of items placed on it. Positions are fractions of the
picture (0..1) so they stay right whatever size the map is shown at. Live status comes from the
browser (cameras, integration devices, open alarms) - the map only stores where things are.
"""
import asyncio
import base64
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
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


class IconRow(Base):
    __tablename__ = "map_icons"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(60))
    mime: Mapped[str] = mapped_column(String(40))
    data: Mapped[bytes] = mapped_column(LargeBinary)


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
    icon: str | None = Field(None, max_length=60)      # "b:<built-in name>" or "c:<imported icon id>"; None = default


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


@router.post("/maps/pdf-pages")
async def pdf_pages(file: UploadFile = File(...), _=Depends(require("admin"))):
    """Plan import: the pages of a PDF as PNG pictures (data URLs), at most 12 pages."""
    data = await file.read(60 * 1024 * 1024 + 1)
    if len(data) > 60 * 1024 * 1024:
        raise HTTPException(413, "The PDF is bigger than 60 MB")
    if data[:5] != b"%PDF-":
        raise HTTPException(400, "This is not a PDF file")
    return await asyncio.to_thread(_render_pdf, data)


def _render_pdf(data: bytes) -> dict:
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "plan.pdf"
        src.write_bytes(data)
        r = subprocess.run(["pdftoppm", "-png", "-r", "200", "-scale-to", "3000", "-f", "1", "-l", "12", str(src), str(Path(d) / "p")],
                           capture_output=True, timeout=180)
        pages = sorted(Path(d).glob("p-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[-1]))
        if not pages:
            raise HTTPException(400, "Could not read this PDF: " + r.stderr.decode(errors="replace")[-200:])
        return {"pages": ["data:image/png;base64," + base64.b64encode(p.read_bytes()).decode() for p in pages]}


# ---------------------------------------------------------------- icons imported for maps
ICON_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
              ".gif": "image/gif"}
MAX_ICON = 400 * 1024


@router.get("/maps/icons")
def list_icons(_=Depends(require("viewer"))):
    with SessionLocal() as s:
        return [{"id": r.id, "name": r.name, "category": r.category, "url": f"/api/maps/icons/{r.id}"}
                for r in s.query(IconRow.id, IconRow.name, IconRow.category).order_by(IconRow.category, IconRow.name)]


@router.post("/maps/icons")
async def import_icons(files: list[UploadFile] = File(...), category: str = Form("My icons"), _=Depends(require("admin"))):
    """SVG / PNG / JPG / WEBP files, or ZIP files full of them (folders in the ZIP become categories)."""
    import io
    import zipfile
    category = (category or "My icons").strip()[:60] or "My icons"
    found, skipped = [], []

    def add(name: str, data: bytes, cat: str):
        ext = Path(name).suffix.lower()
        if ext not in ICON_TYPES:
            return
        if len(data) > MAX_ICON:
            skipped.append(f"{Path(name).name} (bigger than 400 KB)")
            return
        if ext == ".svg" and b"<svg" not in data[:4096].lower():
            skipped.append(f"{Path(name).name} (not an SVG)")
            return
        found.append((Path(name).stem.replace("_", " ").replace("-", " ")[:80], cat[:60], ICON_TYPES[ext], data))

    for f in files[:50]:
        data = await f.read(80 * 1024 * 1024 + 1)
        fname = Path(f.filename or "icon").name
        if fname.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for info in z.infolist()[:3000]:
                        if info.is_dir() or "__MACOSX" in info.filename or Path(info.filename).name.startswith("."):
                            continue
                        if info.file_size > MAX_ICON:
                            skipped.append(f"{Path(info.filename).name} (bigger than 400 KB)")
                            continue
                        parts = Path(info.filename).parts
                        cat = parts[-2] if len(parts) > 1 else category
                        add(info.filename, z.read(info), cat)
            except zipfile.BadZipFile:
                skipped.append(f"{fname} (damaged ZIP)")
        else:
            add(fname, data, category)
    if len(found) > 2000:
        raise HTTPException(400, "At most 2000 icons at a time")
    with SessionLocal() as s:
        for name, cat, mime, data in found:
            s.add(IconRow(id=uuid.uuid4().hex[:12], name=name or "icon", category=cat, mime=mime, data=data))
        s.commit()
    return {"imported": len(found), "skipped": skipped[:30]}


@router.get("/maps/icons/{iid}")
def get_icon(iid: str, _=Depends(require("viewer"))):
    with SessionLocal() as s:
        r = s.get(IconRow, iid)
        if not r:
            raise HTTPException(404)
        headers = {"Cache-Control": "private, max-age=604800"}
        if r.mime == "image/svg+xml":
            headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        return Response(r.data, media_type=r.mime, headers=headers)


@router.delete("/maps/icons/{iid}")
def delete_icon(iid: str, _=Depends(require("admin"))):
    with SessionLocal() as s:
        r = s.get(IconRow, iid)
        if r:
            s.delete(r)
            s.commit()
    return {"ok": True}


@router.delete("/maps/icon-categories/{category}")
def delete_icon_category(category: str, _=Depends(require("admin"))):
    with SessionLocal() as s:
        n = s.query(IconRow).filter(IconRow.category == category).delete()
        s.commit()
    return {"deleted": n}
