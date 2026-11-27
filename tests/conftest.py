"""A small in-memory fake of the parts of the ``docker`` SDK that docker-janitor uses."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import docker.errors
import pytest


def ago(days: float = 0, hours: float = 0) -> str:
    moment = datetime.now(UTC) - timedelta(days=days, hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "123456789Z"


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class Removable:
    def __init__(self, client: FakeClient, kind: str, ident: str, name: str, attrs: dict[str, Any]) -> None:
        self.client, self.kind, self.id, self.name, self.attrs = client, kind, ident, name, attrs

    def remove(self, **kwargs: Any) -> None:
        self.client.calls.append((self.kind, self.name, kwargs))
        error = self.client.errors.get(self.name)
        if error is not None:
            raise error
        self.client.removed.append((self.kind, self.name))


class FakeImage:
    def __init__(self, ident: str, tags: list[str], attrs: dict[str, Any]) -> None:
        self.id, self.tags, self.attrs = ident, tags, attrs


class Collection:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def list(self, **kwargs: Any) -> list[Any]:
        return list(self.items)


class Images(Collection):
    def __init__(self, client: FakeClient) -> None:
        super().__init__()
        self.client = client

    def remove(self, image: str, force: bool = False, noprune: bool = False) -> None:
        self.client.calls.append(("image", image, {"force": force, "noprune": noprune}))
        error = self.client.errors.get(image)
        if error is not None:
            raise error
        self.client.removed.append(("image", image))


class Api:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.prune_calls: list[dict[str, Any]] = []
        self.prune_error: Exception | None = None

    def prune_builds(self, filters: Any = None, keep_storage: Any = None, all: Any = None) -> dict[str, Any]:
        self.prune_calls.append({"filters": filters, "keep_storage": keep_storage, "all": all})
        if self.prune_error:
            raise self.prune_error
        return {"CachesDeleted": ["a"], "SpaceReclaimed": 1234}


class FakeClient:
    def __init__(self) -> None:
        self.containers = Collection()
        self.images = Images(self)
        self.volumes = Collection()
        self.networks = Collection()
        self.api = Api(self)
        self.removed: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.errors: dict[str, Exception] = {}
        self.disk_usage: dict[str, Any] = {"Images": [], "Containers": [], "Volumes": [], "BuildCache": []}

    def df(self) -> dict[str, Any]:
        return self.disk_usage

    # builders -----------------------------------------------------------------
    def add_container(
        self,
        name: str,
        *,
        status: str = "exited",
        finished: str | None = None,
        created: str | None = None,
        image: str = "",
        labels: dict[str, str] | None = None,
        restart: str = "no",
        volumes: list[str] | None = None,
        networks: list[str] | None = None,
        exit_code: int = 0,
        oom_killed: bool = False,
    ) -> Removable:
        attrs = {
            "Name": "/" + name,
            "Created": created or ago(days=60),
            "Image": image,
            "State": {
                "Status": status,
                "FinishedAt": finished or "0001-01-01T00:00:00Z",
                "ExitCode": exit_code,
                "OOMKilled": oom_killed,
            },
            "Config": {"Labels": labels or {}},
            "HostConfig": {"RestartPolicy": {"Name": restart}, "NetworkMode": "bridge"},
            "Mounts": [{"Type": "volume", "Name": v} for v in volumes or []],
            "NetworkSettings": {"Networks": {n: {"NetworkID": sha("net-" + n)} for n in networks or []}},
        }
        container = Removable(self, "container", sha("c-" + name), name, attrs)
        self.containers.items.append(container)
        return container

    def add_image(
        self,
        key: str,
        tags: list[str] | None = None,
        *,
        created: str | None = None,
        last_tag: str | None = None,
        size: int = 100_000_000,
        labels: dict[str, str] | None = None,
        parent: str = "",
        digests: list[str] | None = None,
    ) -> FakeImage:
        attrs = {
            "Id": sha("i-" + key),
            "RepoTags": tags or [],
            "RepoDigests": digests or [],
            "Created": created or ago(days=90),
            "Size": size,
            "Parent": parent,
            "Config": {"Labels": labels},
            "Metadata": {"LastTagTime": last_tag or "0001-01-01T00:00:00Z"},
        }
        image = FakeImage(sha("i-" + key), list(tags or []), attrs)
        self.images.items.append(image)
        return image

    def add_volume(
        self, name: str, *, created: str | None = None, labels: dict[str, str] | None = None, driver: str = "local"
    ) -> Removable:
        attrs = {"Name": name, "CreatedAt": created or ago(days=90), "Labels": labels, "Driver": driver}
        volume = Removable(self, "volume", name, name, attrs)
        self.volumes.items.append(volume)
        return volume

    def add_network(
        self,
        name: str,
        *,
        created: str | None = None,
        labels: dict[str, str] | None = None,
        driver: str = "bridge",
        scope: str = "local",
    ) -> Removable:
        attrs = {
            "Name": name,
            "Created": created or ago(days=90),
            "Labels": labels or {},
            "Driver": driver,
            "Scope": scope,
            "Containers": {},
        }
        network = Removable(self, "network", sha("net-" + name), name, attrs)
        self.networks.items.append(network)
        return network


def api_error(message: str, status: int = 409) -> docker.errors.APIError:
    class Response:
        status_code = status
        reason = "Conflict"

    return docker.errors.APIError(message, response=Response(), explanation=message)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()
