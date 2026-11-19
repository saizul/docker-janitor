"""Remember previous runs, so the first run on a host is always a dry run."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class State:
    def __init__(self, path: str) -> None:
        self.path = Path(os.path.expanduser(path))
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable state file %s: %s", self.path, exc)
            return
        if isinstance(raw, dict):
            self.data = raw

    @property
    def has_previous_run(self) -> bool:
        return int(self.data.get("runs") or 0) > 0

    def record(self, *, mode: str, removed: int, planned: int) -> bool:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.data.setdefault("first_run", now)
        self.data["runs"] = int(self.data.get("runs") or 0) + 1
        self.data["last_run"] = now
        self.data["last_mode"] = mode
        self.data["last_planned"] = planned
        self.data["last_removed"] = removed
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning(
                "cannot write state file %s (%s); the first-run dry-run guard will apply again next time",
                self.path,
                exc.strerror or exc,
            )
            return False
        return True
