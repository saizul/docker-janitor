"""Ping a dead man's switch (healthchecks.io, Uptime Kuma push, Cronitor, ...) around each run."""

from __future__ import annotations

import logging
import urllib.request
from collections.abc import Callable
from typing import Any

from docker_janitor.config import HealthcheckConfig

log = logging.getLogger(__name__)

Opener = Callable[..., Any]


class Pinger:
    """``healthchecks`` style: ``<url>/start``, ``<url>`` on success, ``<url>/fail`` on failure.
    ``plain`` style: only ``<url>`` on success (Uptime Kuma push monitors and similar)."""

    def __init__(self, config: HealthcheckConfig, *, opener: Opener | None = None) -> None:
        self.config = config
        self.opener = opener or urllib.request.urlopen

    @property
    def enabled(self) -> bool:
        return bool(self.config.url)

    def _ping(self, url: str, body: str | None = None) -> bool:
        data = body.encode("utf-8")[:100_000] if body and self.config.send_report else None
        request = urllib.request.Request(
            url,
            data=data,
            method="POST" if data else "GET",
            headers={"User-Agent": "docker-janitor", "Content-Type": "text/plain; charset=utf-8"},
        )
        try:
            with self.opener(request, timeout=self.config.timeout) as response:
                status = getattr(response, "status", 200)
        except Exception as exc:  # a broken monitor must never break the cleanup
            log.warning("health check ping failed: %s", type(exc).__name__)
            return False
        if status >= 400:
            log.warning("health check ping answered HTTP %s", status)
            return False
        return True

    def start(self) -> bool:
        if not self.enabled or self.config.style != "healthchecks":
            return False
        return self._ping(f"{self.config.url}/start")

    def success(self, report: str = "") -> bool:
        if not self.enabled:
            return False
        return self._ping(str(self.config.url), report)

    def fail(self, report: str = "") -> bool:
        if not self.enabled or self.config.style != "healthchecks":
            return False
        return self._ping(f"{self.config.url}/fail", report)
