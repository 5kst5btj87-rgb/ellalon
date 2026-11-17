"""Thin layer over the Docker API: core uses this to run every module container."""
import logging

import docker
from docker.errors import ImageNotFound, NotFound
from docker.types import DeviceRequest

from .auth import module_token
from .config import settings
from .registry import ModuleSpec

log = logging.getLogger("docker")
LABEL = "ellalon.module"


def cname(name: str) -> str:
    return f"ellalon-{name}"


def _ports(publish: list[str]) -> dict:
    out = {}
    for p in publish:  # "8555:8555/udp" -> {"8555/udp": 8555}
        host, _, cont = p.partition(":")
        proto = "tcp"
        if "/" in cont:
            cont, proto = cont.split("/")
        out[f"{cont}/{proto}"] = int(host)
    return out


class DockerManager:
    def __init__(self):
        self.client = docker.DockerClient(base_url=settings.docker_host, version="auto")

    def ensure_network(self):
        try:
            self.client.networks.get(settings.docker_network)
        except NotFound:
            self.client.networks.create(settings.docker_network, driver="bridge")

    def get(self, name: str):
        try:
            return self.client.containers.get(cname(name))
        except NotFound:
            return None

    def status(self, name: str) -> dict:
        c = self.get(name)
        if not c:
            return {"state": "absent"}
        c.reload()
        st = c.attrs.get("State", {})
        return {"state": c.status, "started_at": st.get("StartedAt"),
                "restarts": c.attrs.get("RestartCount", 0), "image": c.image.tags[:1]}

    def _ensure_image(self, spec: ModuleSpec):
        try:
            self.client.images.get(spec.image)
        except ImageNotFound:
            log.info("Pulling %s", spec.image)
            try:
                self.client.images.pull(spec.image)
            except Exception as e:
                raise RuntimeError(
                    f"Image {spec.image} not found locally and pull failed. "
                    f"Build it with: ./ellalon.sh build {spec.name}") from e

    def start(self, spec: ModuleSpec, overrides: dict | None = None):
        c = self.get(spec.name)
        if c:
            if c.status != "running":
                c.start()
            return
        self._ensure_image(spec)
        env = {
            **spec.env, **(overrides or {}),
            "ELLALON_MODULE_NAME": spec.name,
            "ELLALON_CORE_URL": settings.core_url,
            "ELLALON_REDIS_URL": settings.redis_url,
            "ELLALON_MODULE_TOKEN": module_token(spec.name),
            "ELLALON_BASE_PATH": f"/modules/{spec.name}",
        }
        labels = {LABEL: spec.name, "ellalon.version": spec.version}
        if spec.http_port:
            r = f"mod-{spec.name}"
            labels.update({
                "traefik.enable": "true",
                f"traefik.http.routers.{r}.rule": f"PathPrefix(`/modules/{spec.name}`)",
                f"traefik.http.routers.{r}.entrypoints": "web",
                f"traefik.http.routers.{r}.priority": "10",
                f"traefik.http.routers.{r}.middlewares": f"ellalon-auth@docker,{r}-strip",
                f"traefik.http.middlewares.{r}-strip.stripprefix.prefixes": f"/modules/{spec.name}",
                f"traefik.http.services.{r}.loadbalancer.server.port": str(spec.http_port),
            })
        volumes = {
            f"{settings.data_root}/modules/{spec.name}/{v.host}": {
                "bind": v.container, "mode": "ro" if v.read_only else "rw"}
            for v in spec.volumes
        }
        kwargs = dict(
            name=cname(spec.name), detach=True, network=settings.docker_network,
            environment=env, labels=labels, volumes=volumes, hostname=spec.name,
            restart_policy={"Name": "unless-stopped"}, ports=_ports(spec.publish) or None,
        )
        if spec.command:
            kwargs["command"] = spec.command
        if spec.shm_size:
            kwargs["shm_size"] = spec.shm_size
        if spec.gpu:
            kwargs["device_requests"] = [DeviceRequest(count=-1, capabilities=[["gpu"]])]
        log.info("Creating container for %s", spec.name)
        self.client.containers.run(spec.image, **kwargs)

    def stop(self, name: str):
        c = self.get(name)
        if c and c.status == "running":
            c.stop(timeout=15)

    def restart(self, name: str):
        c = self.get(name)
        if c:
            c.restart(timeout=15)

    def remove(self, name: str):
        c = self.get(name)
        if c:
            c.remove(force=True)

    def logs(self, name: str, tail: int = 200) -> str:
        c = self.get(name)
        return c.logs(tail=tail, timestamps=True).decode(errors="replace") if c else ""

    def orphans(self, known: set[str]) -> list[str]:
        """Module containers whose manifest no longer exists."""
        cs = self.client.containers.list(all=True, filters={"label": LABEL})
        return [c.labels[LABEL] for c in cs if c.labels.get(LABEL) not in known]
