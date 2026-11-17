"""Thin layer over the Docker API: core uses this to run every module container."""
import logging
import re

import docker
from docker.errors import APIError, ContainerError, ImageNotFound, NotFound
from docker.types import DeviceRequest

from .auth import module_token
from .config import settings
from .registry import ModuleSpec
from .storage import MOUNT_ERRORS, mount_options, redact

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

    def start(self, spec: ModuleSpec, overrides: dict | None = None, storages: list[dict] | None = None):
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
        remote = set()
        if spec.storage_mounts:
            mounts, remote = self._storage_mounts(storages or [])
            volumes.update(mounts)
        log.info("Creating container for %s", spec.name)
        while True:
            try:
                self.client.containers.run(spec.image, **kwargs)
                break
            except APIError as e:
                # a network share that cannot be mounted must not stop recording to the others
                bad = set(re.findall(r"ellalon-storage-([a-z0-9-]+)", str(e))) & remote
                if not bad:
                    raise
                for sid in bad:
                    MOUNT_ERRORS[sid] = "Cannot connect: " + redact(str(e).split("failed to mount")[-1])[-300:]
                    volumes.pop(f"ellalon-storage-{sid}", None)
                    remote.discard(sid)
                    log.warning("storage %s not mounted: %s", sid, MOUNT_ERRORS[sid])
                self.remove(spec.name)
        for sid in remote:
            MOUNT_ERRORS.pop(sid, None)

    def _storage_mounts(self, storages: list[dict]) -> tuple[dict, set]:
        mounts, remote = {}, set()
        for st in storages:
            target = {"bind": f"/storage/{st['id']}", "mode": "rw"}
            if st["type"] == "local":
                mounts[st["path"]] = target
                continue
            name = f"ellalon-storage-{st['id']}"
            try:
                opts = mount_options(st)
            except ValueError as e:
                MOUNT_ERRORS[st["id"]] = str(e)
                continue
            try:                      # recreate so changed settings take effect
                self.client.volumes.get(name).remove(force=True)
            except NotFound:
                pass
            except APIError:
                pass
            self.client.volumes.create(name=name, driver="local", driver_opts=opts,
                                       labels={"ellalon.storage": st["id"]})
            mounts[name] = target
            remote.add(st["id"])
        return mounts, remote

    def test_storage(self, st: dict) -> dict:
        """Mount a storage in a throw-away container, write a test file, report free space."""
        vol = None
        try:
            if st["type"] == "local":
                mounts = {st["path"]: {"bind": "/t", "mode": "rw"}}
            else:
                vol = "ellalon-storage-probe"
                try:
                    self.client.volumes.get(vol).remove(force=True)
                except (NotFound, APIError):
                    pass
                self.client.volumes.create(name=vol, driver="local", driver_opts=mount_options(st))
                mounts = {vol: {"bind": "/t", "mode": "rw"}}
            out = self.client.containers.run(
                f"ellalon/core:{settings.version}",
                ["sh", "-c", "touch /t/.ellalon-write-test && rm -f /t/.ellalon-write-test && df -Pk /t | tail -1"],
                volumes=mounts, remove=True, network_mode="none")
            f = out.decode().split()
            return {"ok": True, "total_gb": round(int(f[1]) / 1048576, 1),
                    "free_gb": round(int(f[3]) / 1048576, 1), "detail": "writable"}
        except ValueError as e:
            return {"ok": False, "detail": str(e)}
        except ContainerError as e:
            return {"ok": False, "detail": "Connected, but cannot write: "
                    + redact((e.stderr or b"").decode(errors="replace").strip()[-200:])}
        except APIError as e:
            msg = redact(str(e))
            hint = (" - check username/password and share permissions" if "permission denied" in msg.lower()
                    else " - check the server address and share/export name" if "no such" in msg.lower()
                    or "not found" in msg.lower() else "")
            return {"ok": False, "detail": "Cannot connect: " + msg.split("failed to mount")[-1][-250:] + hint}
        finally:
            if vol:
                try:
                    self.client.volumes.get(vol).remove(force=True)
                except Exception:
                    pass

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
