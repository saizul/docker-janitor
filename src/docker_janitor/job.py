"""One cleanup run (plan, safety checks, execute, report, ping) and the scheduling loop."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from docker_janitor import report
from docker_janitor.config import Config
from docker_janitor.healthcheck import Pinger
from docker_janitor.planner import Plan, Planner, Result, execute
from docker_janitor.state import State

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TOO_MANY = 3


@dataclass
class RunOutcome:
    mode: str  # dry-run | execute | aborted | error
    exit_code: int
    plan: Plan | None = None
    result: Result | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "exit_code": self.exit_code,
            "message": self.message,
            "plan": self.plan.to_dict() if self.plan else None,
            "result": self.result.to_dict() if self.result else None,
        }


def run_once(
    client: Any,
    config: Config,
    *,
    execute_requested: bool | None = None,
    skip_first_run_guard: bool = False,
    show_kept: bool = False,
    as_json: bool = False,
    printer: Callable[[str], None] = print,
    pinger: Pinger | None = None,
    state: State | None = None,
) -> RunOutcome:
    """``execute_requested``: ``True`` (--execute), ``False`` (--dry-run) or ``None`` (use the config)."""
    pinger = pinger or Pinger(config.healthcheck)
    state = state or State(config.state_file)
    dry_run = config.dry_run if execute_requested is None else not execute_requested
    notes: list[str] = []
    if not dry_run and config.first_run_dry_run and not state.has_previous_run and not skip_first_run_guard:
        dry_run = True
        notes.append(
            "First run on this host: running as a DRY RUN even though deletion was requested. "
            "Review the plan above; the next run will delete. (Skip with --skip-first-run-guard.)"
        )
    pinger.start()
    try:
        plan = Planner(client, config).build()
    except Exception as exc:
        message = f"could not read the Docker state: {type(exc).__name__}: {exc}"
        log.error("%s", message)
        pinger.fail(message)
        return _emit(RunOutcome("error", EXIT_ERROR, message=message), as_json, printer)

    limit = config.max_removals
    if not dry_run and limit and plan.removal_count > limit:
        message = (
            f"Safety stop: the plan would remove {plan.removal_count} objects, more than "
            f"max_removals={limit}. Nothing was removed. Review the plan and raise max_removals "
            "(or pass --max-removals) if this is expected."
        )
        if not as_json:
            printer(report.format_plan(plan, dry_run=True, show_kept=show_kept))
            printer("\n" + message)
        pinger.fail(message)
        return _emit(RunOutcome("aborted", EXIT_TOO_MANY, plan=plan, message=message), as_json, printer, quiet=True)

    if dry_run:
        text = report.format_plan(plan, dry_run=True, show_kept=show_kept)
        footer = "\n".join([*notes, "DRY RUN: nothing was removed. Use --execute (or dry_run: false) to delete."])
        state.record(mode="dry-run", removed=0, planned=plan.removal_count)
        pinger.success(f"{text}\n\n{footer}")
        outcome = RunOutcome("dry-run", EXIT_OK, plan=plan, message=footer)
        if not as_json:
            printer(text)
            printer("\n" + footer)
        return _emit(outcome, as_json, printer, quiet=True)

    text = report.format_plan(plan, dry_run=False, show_kept=show_kept)
    if not as_json:
        printer(text)
    result = execute(client, plan)
    summary = report.format_result(result)
    state.record(mode="execute", removed=len(result.removed), planned=plan.removal_count)
    pinger.success(f"{text}\n\n{summary}")
    if not as_json:
        printer("\n" + summary)
    return _emit(
        RunOutcome("execute", EXIT_OK, plan=plan, result=result, message=summary), as_json, printer, quiet=True
    )


def _emit(outcome: RunOutcome, as_json: bool, printer: Callable[[str], None], *, quiet: bool = False) -> RunOutcome:
    if as_json:
        printer(json.dumps(outcome.to_dict(), indent=2, default=str))
    elif not quiet and outcome.message:
        printer(outcome.message)
    return outcome


def touch(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).touch()
    except OSError as exc:  # pragma: no cover - read-only filesystem
        log.debug("cannot touch heartbeat %s: %s", path, exc)


def heartbeat_age(path: str) -> float | None:
    try:
        return datetime.now().timestamp() - os.stat(path).st_mtime
    except OSError:
        return None


class Daemon:
    """Run :func:`run_once` on the cron schedule until stopped."""

    def __init__(
        self,
        job: Callable[[], RunOutcome],
        config: Config,
        *,
        heartbeat_file: str | None = None,
        now: Callable[[], datetime] = datetime.now,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.job = job
        self.config = config
        self.heartbeat_file = heartbeat_file
        self.now = now
        self.stop_event = stop_event or threading.Event()
        self.runs = 0

    def _run_job(self) -> None:
        try:
            outcome = self.job()
            log.info("run finished: %s (exit code %d)", outcome.mode, outcome.exit_code)
        except Exception:  # keep the daemon alive whatever happens in one run
            log.exception("cleanup run crashed")
        self.runs += 1

    def wait_until(self, moment: datetime) -> bool:
        """Sleep until ``moment``, touching the heartbeat; ``False`` if stopped meanwhile."""
        while not self.stop_event.is_set():
            touch(self.heartbeat_file)
            remaining = (moment - self.now()).total_seconds()
            if remaining <= 0:
                return True
            self.stop_event.wait(min(remaining, 30.0))
        return False

    def run(self, *, run_now: bool = False, max_runs: int | None = None) -> None:
        cron = self.config.cron
        touch(self.heartbeat_file)
        if run_now:
            self._run_job()
        while not self.stop_event.is_set() and (max_runs is None or self.runs < max_runs):
            moment = cron.next_after(self.now())
            log.info("next cleanup at %s (schedule %r)", moment.strftime("%Y-%m-%d %H:%M"), cron.expression)
            if not self.wait_until(moment):
                break
            self._run_job()
