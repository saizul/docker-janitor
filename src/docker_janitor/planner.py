"""Build a cleanup plan from the Docker Engine state, and carry it out.

The planner never deletes anything. It returns a :class:`Plan` listing what would be
removed and what is kept (with a reason for each), so the same code path drives both
``--dry-run`` and real runs. :func:`execute` then removes exactly the planned items,
without ``force``: if something changed in between (a container was started, an image
got a new container), the Docker Engine itself refuses the removal.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import docker.errors

from docker_janitor.config import Config
from docker_janitor.units import format_duration, parse_timestamp

log = logging.getLogger(__name__)

KINDS = ("container", "image", "network", "volume")
BUILTIN_NETWORKS = frozenset({"bridge", "host", "none", "docker_gwbridge", "ingress"})
_ANON_VOLUME = re.compile(r"^[0-9a-f]{64}$")
_RESTART_KEEP = frozenset({"always", "unless-stopped", "on-failure"})


@dataclass
class Item:
    kind: str
    id: str
    name: str
    reason: str
    age: float | None = None
    size: int | None = None
    tags: list[str] = field(default_factory=list)
    obj: Any = field(default=None, repr=False, compare=False)

    @property
    def short_id(self) -> str:
        ident = self.id.split(":", 1)[-1]
        return ident[:12]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "reason": self.reason,
            "age_seconds": None if self.age is None else int(self.age),
            "size_bytes": self.size,
        }
        if self.tags:
            data["tags"] = self.tags
        return data


@dataclass
class BuildCacheAction:
    older_than: float
    filters: dict[str, str]
    keep_storage: int | None
    all: bool
    estimated_entries: int
    estimated_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "filters": self.filters,
            "keep_storage": self.keep_storage,
            "all": self.all,
            "estimated_entries": self.estimated_entries,
            "estimated_size_bytes": self.estimated_size,
        }


@dataclass
class Plan:
    remove: list[Item] = field(default_factory=list)
    keep: list[Item] = field(default_factory=list)
    build_cache: BuildCacheAction | None = None
    warnings: list[str] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Item]:
        return [item for item in self.remove if item.kind == kind]

    @property
    def removal_count(self) -> int:
        return len(self.remove)

    @property
    def estimated_size(self) -> int:
        total = sum(item.size or 0 for item in self.remove)
        if self.build_cache is not None:
            total += self.build_cache.estimated_size
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "remove": [item.to_dict() for item in self.remove],
            "keep": [item.to_dict() for item in self.keep],
            "build_cache": self.build_cache.to_dict() if self.build_cache else None,
            "warnings": list(self.warnings),
            "summary": {
                **{f"{kind}s": len(self.by_kind(kind)) for kind in KINDS},
                "estimated_reclaim_bytes": self.estimated_size,
            },
        }


# Label and name selectors ------------------------------------------------------


def _selector(text: str) -> tuple[str, str | None]:
    key, sep, value = text.partition("=")
    return key.strip(), (value.strip() if sep else None)


def labels_match(labels: dict[str, str] | None, selectors: list[str]) -> str | None:
    """Return the first selector (``key`` or ``key=value``) that ``labels`` satisfy."""
    if not labels:
        return None
    for selector in selectors:
        key, value = _selector(selector)
        if key in labels and (value is None or str(labels[key]).lower() == value.lower()):
            return selector
    return None


class Planner:
    def __init__(self, client: Any, config: Config, *, now: datetime | None = None) -> None:
        self.client = client
        self.config = config
        self.now = now or datetime.now(UTC)
        self._df: dict[str, Any] | None = None

    # helpers ---------------------------------------------------------------
    def _age(self, *timestamps: Any) -> float | None:
        parsed = [p for p in (parse_timestamp(t) for t in timestamps) if p is not None]
        if not parsed:
            return None
        return (self.now - max(parsed)).total_seconds()

    def _protected(self, labels: dict[str, str] | None, names: list[str]) -> str | None:
        protect = self.config.protect
        hit = labels_match(labels, protect.labels)
        if hit:
            return f"protected by label {hit}"
        if protect.only_labels and not labels_match(labels, protect.only_labels):
            return "not selected by protect.only_labels"
        for pattern in protect.names:
            for name in names:
                if fnmatch.fnmatchcase(name, pattern):
                    return f"protected by name pattern {pattern}"
        return None

    def _disk_usage(self) -> dict[str, Any]:
        if self._df is None:
            try:
                self._df = self.client.df() or {}
            except docker.errors.APIError as exc:
                self._df = {}
                self._warn(f"disk usage unavailable: {_explain(exc)}")
        return self._df

    def _warn(self, message: str) -> None:
        log.warning("%s", message)
        self._plan.warnings.append(message)

    # main ------------------------------------------------------------------
    def build(self) -> Plan:
        self._plan = Plan()
        containers = self.client.containers.list(all=True, ignore_removed=True)
        removed_ids: set[str] = set()
        if self.config.containers.enabled:
            removed_ids = self._containers(containers)
        remaining = [c for c in containers if c.id not in removed_ids]
        if self.config.images.enabled:
            self._images(remaining)
        if self.config.networks.enabled:
            self._networks(remaining)
        if self.config.volumes.enabled:
            self._volumes(remaining)
        if self.config.build_cache.enabled:
            self._build_cache()
        return self._plan

    def _containers(self, containers: list[Any]) -> set[str]:
        policy = self.config.containers
        removed: set[str] = set()
        for container in containers:
            attrs = container.attrs or {}
            state = attrs.get("State") or {}
            status = str(state.get("Status") or getattr(container, "status", "")).lower()
            if status not in policy.statuses:
                continue
            name = (container.name or attrs.get("Name") or container.id[:12]).lstrip("/")
            labels = (attrs.get("Config") or {}).get("Labels") or {}
            finished = state.get("FinishedAt") if status in ("exited", "dead") else None
            age = self._age(finished) if parse_timestamp(finished) else self._age(attrs.get("Created"))
            item = Item("container", container.id, name, "", age=age, obj=container)
            restart = ((attrs.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
            reason = self._protected(labels, [name])
            if reason is None and policy.keep_with_restart_policy and restart in _RESTART_KEEP:
                reason = f"has restart policy {restart}"
            failure = _failure(status, state)
            threshold = policy.older_than
            if failure and policy.failed_older_than is not None:
                threshold = policy.failed_older_than
            if reason is None and (age is None or age < threshold):
                what = f"failed ({failure}), stopped" if failure and threshold != policy.older_than else "stopped"
                reason = f"{what} {format_duration(age)} ago (< {format_duration(threshold)})"
            if reason:
                item.reason = reason
                self._plan.keep.append(item)
                continue
            item.reason = f"{status} for {format_duration(age)}" + (f", {failure}" if failure else "")
            self._plan.remove.append(item)
            removed.add(container.id)
        return removed

    def _images(self, remaining: list[Any]) -> None:
        policy = self.config.images
        used = {str((c.attrs or {}).get("Image") or "") for c in remaining}
        images = self.client.images.list()
        parents = {str((img.attrs or {}).get("Parent") or "") for img in images} - {""}

        # Newest N images per repository are always kept.
        newest: set[str] = set()
        by_repo: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for img in images:
            attrs = img.attrs or {}
            created = parse_timestamp(attrs.get("Created"))
            stamp = created.timestamp() if created else 0.0
            for tag in _tags(img):
                by_repo[_repo(tag)].append((stamp, img.id))
        for entries in by_repo.values():
            seen: list[str] = []
            for _, image_id in sorted(entries, reverse=True):
                if image_id not in seen:
                    seen.append(image_id)
            newest.update(seen[: policy.keep_latest])

        for img in images:
            attrs = img.attrs or {}
            tags = _tags(img)
            digests = [d.split("@", 1)[0] for d in attrs.get("RepoDigests") or []]
            name = ", ".join(tags) if tags else (digests[0] + "@<untagged>" if digests else "<none>")
            labels = (attrs.get("Config") or {}).get("Labels") or {}
            age = self._age(attrs.get("Created"), (attrs.get("Metadata") or {}).get("LastTagTime"))
            item = Item("image", img.id, name, "", age=age, size=attrs.get("Size"), tags=tags, obj=img)
            threshold = policy.unused_older_than if tags else policy.dangling_older_than
            if threshold is None:
                continue
            reason = None
            if img.id in used:
                reason = "used by a container"
            elif img.id in parents:
                reason = "parent of another image"
            if reason is None:
                reason = self._protected(labels, [*tags, *digests])
            if reason is None and tags and img.id in newest:
                reason = f"one of the {policy.keep_latest} newest of {_repo(tags[0])}"
            if reason is None and (age is None or age < threshold):
                reason = f"age {format_duration(age)} (< {format_duration(threshold)})"
            if reason:
                item.reason = reason
                self._plan.keep.append(item)
                continue
            item.reason = f"{'unused' if tags else 'dangling'} for {format_duration(age)}"
            self._plan.remove.append(item)

    def _networks(self, remaining: list[Any]) -> None:
        policy = self.config.networks
        used_ids: set[str] = set()
        used_names: set[str] = set()
        for container in remaining:
            settings = (container.attrs or {}).get("NetworkSettings") or {}
            for net_name, net in (settings.get("Networks") or {}).items():
                used_names.add(net_name)
                if isinstance(net, dict) and net.get("NetworkID"):
                    used_ids.add(net["NetworkID"])
            mode = ((container.attrs or {}).get("HostConfig") or {}).get("NetworkMode") or ""
            used_names.add(mode)
        for network in self.client.networks.list():
            attrs = network.attrs or {}
            name = network.name or attrs.get("Name") or network.id[:12]
            if name in BUILTIN_NETWORKS or attrs.get("Ingress") or attrs.get("Scope") == "swarm":
                continue
            if attrs.get("Driver") in ("host", "null"):
                continue
            labels = attrs.get("Labels") or {}
            age = self._age(attrs.get("Created"))
            item = Item("network", network.id, name, "", age=age, obj=network)
            reason = None
            if network.id in used_ids or name in used_names or attrs.get("Containers"):
                reason = "used by a container"
            if reason is None:
                reason = self._protected(labels, [name])
            if reason is None and (age is None or age < policy.older_than):
                reason = f"age {format_duration(age)} (< {format_duration(policy.older_than)})"
            if reason:
                item.reason = reason
                self._plan.keep.append(item)
                continue
            item.reason = f"unused for {format_duration(age)}"
            self._plan.remove.append(item)

    def _volumes(self, remaining: list[Any]) -> None:
        policy = self.config.volumes
        used: set[str] = set()
        for container in remaining:
            for mount in (container.attrs or {}).get("Mounts") or []:
                if mount.get("Type") == "volume" and mount.get("Name"):
                    used.add(mount["Name"])
        sizes = {
            v.get("Name"): ((v.get("UsageData") or {}).get("Size"))
            for v in (self._disk_usage().get("Volumes") or [])
            if isinstance(v, dict)
        }
        for volume in self.client.volumes.list():
            attrs = volume.attrs or {}
            name = volume.name or attrs.get("Name") or volume.id
            labels = attrs.get("Labels") or {}
            age = self._age(attrs.get("CreatedAt"))
            size = sizes.get(name)
            item = Item("volume", volume.id, name, "", age=age, size=size if size and size > 0 else None, obj=volume)
            anonymous = bool(_ANON_VOLUME.match(name)) or "com.docker.volume.anonymous" in labels
            reason = None
            if name in used:
                reason = "used by a container"
            elif (attrs.get("Driver") or "local") != "local":
                reason = f"driver {attrs.get('Driver')} is not local"
            elif policy.anonymous_only and not anonymous:
                reason = "named volume (volumes.anonymous_only is true)"
            if reason is None:
                reason = self._protected(labels, [name])
            if reason is None and (age is None or age < policy.older_than):
                reason = f"age {format_duration(age)} (< {format_duration(policy.older_than)})"
            if reason:
                item.reason = reason
                self._plan.keep.append(item)
                continue
            item.reason = f"unused {'anonymous ' if anonymous else ''}volume, {format_duration(age)} old"
            self._plan.remove.append(item)

    def _build_cache(self) -> None:
        policy = self.config.build_cache
        entries = self._disk_usage().get("BuildCache") or []
        count, size = 0, 0
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("InUse"):
                continue
            if not policy.all and entry.get("Shared"):
                continue
            age = self._age(entry.get("LastUsedAt") or entry.get("CreatedAt"))
            if age is None or age < policy.older_than:
                continue
            count += 1
            size += int(entry.get("Size") or 0)
        if policy.keep_storage is not None:
            total = sum(int(e.get("Size") or 0) for e in entries if isinstance(e, dict))
            size = max(0, min(size, total - policy.keep_storage))
        self._plan.build_cache = BuildCacheAction(
            older_than=policy.older_than,
            filters={"until": f"{int(policy.older_than)}s"},
            keep_storage=policy.keep_storage,
            all=policy.all,
            estimated_entries=count,
            estimated_size=size,
        )


def _failure(status: str, state: dict[str, Any]) -> str | None:
    """Why a stopped container counts as failed (non-zero exit, OOM kill, dead), or None."""
    if status == "dead":
        return "dead"
    if state.get("OOMKilled"):
        return "OOM killed"
    code = state.get("ExitCode")
    if status == "exited" and isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return f"exit code {code}"
    return None


def _tags(img: Any) -> list[str]:
    tags = getattr(img, "tags", None)
    if tags is None:
        tags = (img.attrs or {}).get("RepoTags") or []
    return [t for t in tags if t and t != "<none>:<none>"]


def _repo(tag: str) -> str:
    repo, sep, version = tag.rpartition(":")
    if not sep or "/" in version:
        return tag
    return repo


def _explain(exc: Exception) -> str:
    explanation = getattr(exc, "explanation", None)
    return str(explanation or exc).strip()


# Execution ----------------------------------------------------------------------


@dataclass
class Outcome:
    item: Item
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**self.item.to_dict(), "ok": self.ok, "error": self.error}


@dataclass
class Result:
    outcomes: list[Outcome] = field(default_factory=list)
    build_cache_reclaimed: int | None = None
    build_cache_error: str | None = None

    @property
    def removed(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[Outcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def reclaimed(self) -> int:
        total = sum(o.item.size or 0 for o in self.removed)
        return total + (self.build_cache_reclaimed or 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "removed": [o.to_dict() for o in self.removed],
            "failed": [o.to_dict() for o in self.failed],
            "build_cache_reclaimed_bytes": self.build_cache_reclaimed,
            "build_cache_error": self.build_cache_error,
            "reclaimed_bytes": self.reclaimed,
        }


def _remove(client: Any, item: Item) -> None:
    if item.kind == "container":
        item.obj.remove(v=False, force=False)
    elif item.kind == "image":
        if item.tags:
            # Untag one by one; Docker deletes the image with its last tag. Never force.
            for tag in item.tags:
                client.images.remove(tag, force=False, noprune=False)
        else:
            client.images.remove(item.id, force=False, noprune=False)
    elif item.kind == "volume":
        item.obj.remove(force=False)
    elif item.kind == "network":
        item.obj.remove()
    else:  # pragma: no cover - planner only produces the kinds above
        raise ValueError(item.kind)


def execute(client: Any, plan: Plan) -> Result:
    result = Result()
    order = {kind: index for index, kind in enumerate(KINDS)}
    for item in sorted(plan.remove, key=lambda i: order[i.kind]):
        try:
            _remove(client, item)
        except docker.errors.NotFound:
            log.info("%s %s was already gone", item.kind, item.name)
            result.outcomes.append(Outcome(item, ok=True, error="already removed"))
            continue
        except docker.errors.APIError as exc:
            log.warning("could not remove %s %s: %s", item.kind, item.name, _explain(exc))
            result.outcomes.append(Outcome(item, ok=False, error=_explain(exc)))
            continue
        log.info("removed %s %s (%s)", item.kind, item.name, item.reason)
        result.outcomes.append(Outcome(item, ok=True))
    if plan.build_cache is not None:
        action = plan.build_cache
        try:
            response = client.api.prune_builds(filters=action.filters, keep_storage=action.keep_storage, all=action.all)
            result.build_cache_reclaimed = int((response or {}).get("SpaceReclaimed") or 0)
            log.info("pruned build cache, reclaimed %d bytes", result.build_cache_reclaimed)
        except docker.errors.APIError as exc:
            result.build_cache_error = _explain(exc)
            log.warning("build cache prune failed: %s", result.build_cache_error)
    return result
