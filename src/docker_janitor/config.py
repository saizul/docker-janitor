"""Configuration: cleanup policies, protection rules, schedule and health check.

Every default is chosen so that a run with no config at all is harmless: it is a dry
run, volumes are never touched, and anything labelled ``janitor.keep=true`` is kept.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from docker_janitor.cron import Cron, CronError
from docker_janitor.units import parse_duration, parse_size

KEEP_LABEL = "janitor.keep=true"
REMOVABLE_STATUSES = ("exited", "created", "dead")
DEFAULT_STATE_FILE = "~/.local/state/docker-janitor/state.json"


class ConfigError(ValueError):
    """The configuration is invalid."""


@dataclass
class Protect:
    labels: list[str] = field(default_factory=lambda: [KEEP_LABEL])
    only_labels: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)


@dataclass
class ContainerPolicy:
    enabled: bool = True
    older_than: float = 7 * 86400
    statuses: list[str] = field(default_factory=lambda: list(REMOVABLE_STATUSES))
    keep_with_restart_policy: bool = True


@dataclass
class ImagePolicy:
    enabled: bool = True
    dangling_older_than: float | None = 86400
    unused_older_than: float | None = 30 * 86400
    keep_latest: int = 3


@dataclass
class VolumePolicy:
    enabled: bool = False
    older_than: float = 30 * 86400
    anonymous_only: bool = True


@dataclass
class NetworkPolicy:
    enabled: bool = True
    older_than: float = 7 * 86400


@dataclass
class BuildCachePolicy:
    enabled: bool = True
    older_than: float = 7 * 86400
    keep_storage: int | None = None
    all: bool = False


@dataclass
class HealthcheckConfig:
    url: str | None = None
    style: str = "healthchecks"  # healthchecks: /start and /fail suffixes; plain: only the url
    timeout: float = 10.0
    send_report: bool = True


@dataclass
class Config:
    dry_run: bool = True
    first_run_dry_run: bool = True
    max_removals: int = 100
    state_file: str = DEFAULT_STATE_FILE
    schedule: str = "0 4 * * *"
    protect: Protect = field(default_factory=Protect)
    containers: ContainerPolicy = field(default_factory=ContainerPolicy)
    images: ImagePolicy = field(default_factory=ImagePolicy)
    volumes: VolumePolicy = field(default_factory=VolumePolicy)
    networks: NetworkPolicy = field(default_factory=NetworkPolicy)
    build_cache: BuildCachePolicy = field(default_factory=BuildCachePolicy)
    healthcheck: HealthcheckConfig = field(default_factory=HealthcheckConfig)

    @property
    def cron(self) -> Cron:
        return Cron.parse(self.schedule)


def _check_keys(section: str, raw: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{section}: unknown key(s) {', '.join(sorted(unknown))}")


def _bool(section: str, key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false", "yes", "no", "1", "0"):
        return value.lower() in ("true", "yes", "1")
    raise ConfigError(f"{section}.{key} must be true or false")


def _duration(section: str, key: str, value: Any, *, optional: bool = False) -> float | None:
    if optional and (value is None or value is False):
        return None
    try:
        return parse_duration(value, field=f"{section}.{key}")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _str_list(section: str, key: str, value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{section}.{key} must be a list of strings")
    return [v.strip() for v in value]


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    raw = data.get(name)
    if raw is None:
        return {}
    if raw is False:
        return {"enabled": False}
    if raw is True:
        return {"enabled": True}
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a mapping")
    return raw


def parse_config(data: Any) -> Config:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("the config file must contain a mapping")
    top = {
        "dry_run",
        "first_run_dry_run",
        "max_removals",
        "state_file",
        "schedule",
        "protect",
        "containers",
        "images",
        "volumes",
        "networks",
        "build_cache",
        "healthcheck",
    }
    _check_keys("config", data, top)
    cfg = Config()
    if "dry_run" in data:
        cfg.dry_run = _bool("config", "dry_run", data["dry_run"])
    if "first_run_dry_run" in data:
        cfg.first_run_dry_run = _bool("config", "first_run_dry_run", data["first_run_dry_run"])
    if "max_removals" in data:
        value = data["max_removals"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError("max_removals must be a non-negative integer (0 = no limit)")
        cfg.max_removals = value
    if data.get("state_file"):
        cfg.state_file = str(data["state_file"])
    if "schedule" in data:
        cfg.schedule = str(data["schedule"])
    try:
        Cron.parse(cfg.schedule)
    except CronError as exc:
        raise ConfigError(f"schedule: {exc}") from exc

    raw = _section(data, "protect")
    _check_keys("protect", raw, {"labels", "only_labels", "names"})
    labels = _str_list("protect", "labels", raw.get("labels"))
    cfg.protect = Protect(
        labels=[KEEP_LABEL, *[label for label in labels if label != KEEP_LABEL]],
        only_labels=_str_list("protect", "only_labels", raw.get("only_labels")),
        names=_str_list("protect", "names", raw.get("names")),
    )

    raw = _section(data, "containers")
    _check_keys("containers", raw, {"enabled", "older_than", "statuses", "keep_with_restart_policy"})
    c = cfg.containers
    c.enabled = _bool("containers", "enabled", raw.get("enabled", c.enabled))
    if "older_than" in raw:
        c.older_than = _duration("containers", "older_than", raw["older_than"])  # type: ignore[assignment]
    if "statuses" in raw:
        statuses = _str_list("containers", "statuses", raw["statuses"])
        bad = [s for s in statuses if s not in REMOVABLE_STATUSES]
        if bad or not statuses:
            raise ConfigError(
                f"containers.statuses may only contain {', '.join(REMOVABLE_STATUSES)}; "
                "running, paused and restarting containers are never removed"
            )
        c.statuses = statuses
    c.keep_with_restart_policy = _bool(
        "containers", "keep_with_restart_policy", raw.get("keep_with_restart_policy", c.keep_with_restart_policy)
    )

    raw = _section(data, "images")
    _check_keys("images", raw, {"enabled", "dangling_older_than", "unused_older_than", "keep_latest"})
    i = cfg.images
    i.enabled = _bool("images", "enabled", raw.get("enabled", i.enabled))
    if "dangling_older_than" in raw:
        i.dangling_older_than = _duration("images", "dangling_older_than", raw["dangling_older_than"], optional=True)
    if "unused_older_than" in raw:
        i.unused_older_than = _duration("images", "unused_older_than", raw["unused_older_than"], optional=True)
    if "keep_latest" in raw:
        keep = raw["keep_latest"]
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 0:
            raise ConfigError("images.keep_latest must be a non-negative integer")
        i.keep_latest = keep

    raw = _section(data, "volumes")
    _check_keys("volumes", raw, {"enabled", "older_than", "anonymous_only"})
    v = cfg.volumes
    v.enabled = _bool("volumes", "enabled", raw.get("enabled", v.enabled))
    if "older_than" in raw:
        v.older_than = _duration("volumes", "older_than", raw["older_than"])  # type: ignore[assignment]
    v.anonymous_only = _bool("volumes", "anonymous_only", raw.get("anonymous_only", v.anonymous_only))

    raw = _section(data, "networks")
    _check_keys("networks", raw, {"enabled", "older_than"})
    n = cfg.networks
    n.enabled = _bool("networks", "enabled", raw.get("enabled", n.enabled))
    if "older_than" in raw:
        n.older_than = _duration("networks", "older_than", raw["older_than"])  # type: ignore[assignment]

    raw = _section(data, "build_cache")
    _check_keys("build_cache", raw, {"enabled", "older_than", "keep_storage", "all"})
    b = cfg.build_cache
    b.enabled = _bool("build_cache", "enabled", raw.get("enabled", b.enabled))
    if "older_than" in raw:
        b.older_than = _duration("build_cache", "older_than", raw["older_than"])  # type: ignore[assignment]
    if raw.get("keep_storage") is not None:
        try:
            b.keep_storage = parse_size(raw["keep_storage"], field="build_cache.keep_storage")
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    b.all = _bool("build_cache", "all", raw.get("all", b.all))

    raw = _section(data, "healthcheck")
    _check_keys("healthcheck", raw, {"url", "style", "timeout", "send_report"})
    h = cfg.healthcheck
    if raw.get("url"):
        url = str(raw["url"])
        if not url.startswith(("http://", "https://")):
            raise ConfigError("healthcheck.url must start with http:// or https://")
        h.url = url.rstrip("/")
    style = str(raw.get("style", h.style))
    if style not in ("healthchecks", "plain"):
        raise ConfigError("healthcheck.style must be 'healthchecks' or 'plain'")
    h.style = style
    if "timeout" in raw:
        h.timeout = _duration("healthcheck", "timeout", raw["timeout"])  # type: ignore[assignment]
    h.send_report = _bool("healthcheck", "send_report", raw.get("send_report", h.send_report))
    return cfg


ENV_OVERRIDES = {
    "DOCKER_JANITOR_DRY_RUN": "dry_run",
    "DOCKER_JANITOR_SCHEDULE": "schedule",
    "DOCKER_JANITOR_HEALTHCHECK_URL": "healthcheck.url",
    "DOCKER_JANITOR_STATE_FILE": "state_file",
    "DOCKER_JANITOR_MAX_REMOVALS": "max_removals",
}


def apply_env(data: dict[str, Any], env: dict[str, str] | os._Environ[str] | None = None) -> dict[str, Any]:
    """Let ``docker run -e DOCKER_JANITOR_SCHEDULE=...`` work without a config file."""
    env = os.environ if env is None else env
    data = dict(data)
    for var, key in ENV_OVERRIDES.items():
        value = env.get(var)
        if value is None or value == "":
            continue
        if key == "max_removals":
            if not value.isdigit():
                raise ConfigError(f"{var} must be a non-negative integer")
            data[key] = int(value)
        elif "." in key:
            section, sub = key.split(".")
            data[section] = {**(data.get(section) or {}), sub: value}
        else:
            data[key] = value
    return data


def load_config(path: str | os.PathLike[str] | None, *, env: dict[str, str] | None = None) -> Config:
    """Load ``path``; ``None`` means "no config file", i.e. all safe defaults."""
    data: Any = {}
    if path is not None:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config {path}: {exc.strerror or exc}") from exc
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("the config file must contain a mapping")
    return parse_config(apply_env(data, env))
