"""Module registry: every folder in modules/ with a module.yaml is a pluggable module."""
import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .config import settings

log = logging.getLogger("registry")


class VolumeSpec(BaseModel):
    host: str              # sub-folder under ELLALON_DATA_ROOT/<module>/
    container: str
    read_only: bool = False


class HealthSpec(BaseModel):
    type: str = "heartbeat"  # heartbeat (SDK modules) | http (3rd-party images) | none
    path: str = "/health"
    port: int | None = None


class ModuleSpec(BaseModel):
    name: str
    display_name: str = ""
    version: str = "0.1.0"
    category: str = "other"   # recording|streaming|display|analytics|integration|c2|other
    description: str = ""
    image: str
    autostart: bool = False
    http_port: int | None = None          # routed by Traefik at /modules/<name>/
    publish: list[str] = Field(default_factory=list)   # host ports, e.g. "8555:8555/udp"
    env: dict[str, str] = Field(default_factory=dict)
    volumes: list[VolumeSpec] = Field(default_factory=list)
    gpu: bool = False
    shm_size: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    command: list[str] | None = None
    health: HealthSpec = Field(default_factory=HealthSpec)
    capabilities: list[str] = Field(default_factory=list)  # advertised to clients


class Registry:
    def __init__(self):
        self.modules: dict[str, ModuleSpec] = {}

    def load(self) -> dict[str, ModuleSpec]:
        found = {}
        for f in sorted(Path(settings.modules_dir).glob("*/module.yaml")):
            try:
                spec = ModuleSpec(**yaml.safe_load(f.read_text()))
                found[spec.name] = spec
            except Exception as e:  # a broken manifest must not take down core
                log.error("Invalid manifest %s: %s", f, e)
        self.modules = found
        log.info("Loaded %d modules: %s", len(found), ", ".join(found))
        return found

    def start_order(self, names: list[str]) -> list[str]:
        """Topological sort by depends_on."""
        order, seen = [], set()

        def visit(n, stack=()):
            if n in seen or n not in self.modules:
                return
            if n in stack:
                raise ValueError(f"Dependency cycle at {n}")
            for d in self.modules[n].depends_on:
                visit(d, stack + (n,))
            seen.add(n)
            order.append(n)

        for n in names:
            visit(n)
        return order


registry = Registry()
