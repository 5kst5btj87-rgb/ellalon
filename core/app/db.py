from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="operator")  # admin|operator|viewer
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Camera(Base):
    __tablename__ = "cameras"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)               # main stream (RTSP)
    substream_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # for analytics/live
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    record: Mapped[bool] = mapped_column(Boolean, default=True)
    analytics: Mapped[bool] = mapped_column(Boolean, default=False)
    face_recognition: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)   # module name
    type: Mapped[str] = mapped_column(String(64), index=True)     # motion, face.match, ...
    severity: Mapped[str] = mapped_column(String(16), default="info")
    camera_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ModuleState(Base):
    """Desired state of each module; the reconciler drives containers toward it."""
    __tablename__ = "module_state"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # env overrides from UI


class Storage(Base):
    """Where recordings are kept: local folder or network share (see storage.py)."""
    __tablename__ = "storage"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    type: Mapped[str] = mapped_column(String(10), default="local")     # local | smb | nfs
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    min_free_gb: Mapped[float] = mapped_column(Float, default=20)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


def init_db():
    Base.metadata.create_all(engine)
