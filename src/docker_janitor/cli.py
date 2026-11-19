"""Command line interface: ``docker-janitor plan | run | daemon | df | check-config | healthcheck``."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import os
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from docker_janitor import report
from docker_janitor._version import __version__
from docker_janitor.config import Config, ConfigError, load_config
from docker_janitor.job import EXIT_ERROR, Daemon, heartbeat_age, run_once

DEFAULT_CONFIG_PATHS = ("docker-janitor.yaml", "/config/config.yaml")
HEARTBEAT_ENV = "DOCKER_JANITOR_HEARTBEAT"

log = logging.getLogger("docker_janitor")


def _default_client() -> Any:
    import docker

    timeout = int(os.environ.get("DOCKER_JANITOR_TIMEOUT", "120"))
    return docker.from_env(timeout=timeout)


# Tests replace this with a factory returning a fake client.
client_factory: Callable[[], Any] = _default_client


def config_path(args: argparse.Namespace) -> str | None:
    explicit = args.config or os.environ.get("DOCKER_JANITOR_CONFIG")
    if explicit:
        return explicit
    for candidate in DEFAULT_CONFIG_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


def _load(args: argparse.Namespace) -> Config:
    config = load_config(config_path(args))
    if getattr(args, "max_removals", None) is not None:
        config.max_removals = args.max_removals
    return config


def _execute_flag(args: argparse.Namespace) -> bool | None:
    if getattr(args, "execute", False):
        return True
    if getattr(args, "dry_run", False):
        return False
    return None


def _client() -> Any:
    try:
        return client_factory()
    except Exception as exc:
        raise ConnectionError(
            f"cannot connect to the Docker Engine ({type(exc).__name__}). Mount /var/run/docker.sock "
            "or set DOCKER_HOST (for example tcp://docker-socket-proxy:2375)."
        ) from exc


def cmd_plan(args: argparse.Namespace) -> int:
    config = _load(args)
    outcome = run_once(_client(), config, execute_requested=False, show_kept=args.show_kept, as_json=args.json)
    return outcome.exit_code


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    outcome = run_once(
        _client(),
        config,
        execute_requested=_execute_flag(args),
        skip_first_run_guard=args.skip_first_run_guard,
        show_kept=args.show_kept,
        as_json=args.json,
    )
    return outcome.exit_code


def cmd_daemon(args: argparse.Namespace) -> int:
    config = _load(args)
    client = _client()
    execute_requested = _execute_flag(args)
    mode = "dry run" if (config.dry_run if execute_requested is None else not execute_requested) else "EXECUTE"
    log.info("docker-janitor %s daemon: schedule %r, mode %s", __version__, config.schedule, mode)

    def job() -> Any:
        return run_once(client, config, execute_requested=execute_requested, show_kept=args.show_kept)

    daemon = Daemon(job, config, heartbeat_file=os.environ.get(HEARTBEAT_ENV))

    def stop(signum: int, _frame: Any) -> None:
        log.info("received signal %d, stopping", signum)
        daemon.stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError):
            signal.signal(sig, stop)
    daemon.run(run_now=args.run_now)
    return 0


def cmd_df(args: argparse.Namespace) -> int:
    rows = report.disk_usage(_client().df() or {})
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(report.format_disk_usage(rows))
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    path = config_path(args)
    config = _load(args)
    data = dataclasses.asdict(config)
    if data["healthcheck"]["url"]:
        data["healthcheck"]["url"] = "<set>"  # ping URLs are secrets
    upcoming, moment = [], datetime.now()
    for _ in range(3):
        moment = config.cron.next_after(moment)
        upcoming.append(moment.strftime("%Y-%m-%d %H:%M"))
    data["next_runs"] = upcoming
    print(json.dumps(data, indent=2))
    print(f"OK: {path or 'no config file, using safe defaults'}", file=sys.stderr)
    return 0


def cmd_healthcheck(args: argparse.Namespace) -> int:
    path = args.file or os.environ.get(HEARTBEAT_ENV)
    if not path:
        print("no heartbeat file configured (--file or DOCKER_JANITOR_HEARTBEAT)", file=sys.stderr)
        return 1
    age = heartbeat_age(path)
    if age is None or age > args.max_age:
        print(f"unhealthy: {'no heartbeat yet' if age is None else f'last heartbeat {age:.0f}s ago'}", file=sys.stderr)
        return 1
    print(f"healthy: last heartbeat {age:.0f}s ago")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docker-janitor",
        description="Safe, policy-driven Docker cleanup. Dry run by default; never forces a removal.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", help="config file (default: $DOCKER_JANITOR_CONFIG, ./docker-janitor.yaml)")
    parser.add_argument("--log-level", default=os.environ.get("DOCKER_JANITOR_LOG_LEVEL", "info"))
    sub = parser.add_subparsers(dest="command", required=True)

    def output_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="print the plan and result as JSON")
        p.add_argument("--show-kept", action="store_true", help="also list kept objects and why")

    def mode_flags(p: argparse.ArgumentParser) -> None:
        group = p.add_mutually_exclusive_group()
        group.add_argument("--execute", action="store_true", help="really delete (overrides dry_run in the config)")
        group.add_argument("--dry-run", action="store_true", help="only show the plan (the default)")
        p.add_argument("--max-removals", type=int, help="safety stop: abort if more objects would be removed")

    p = sub.add_parser("plan", help="show what would be removed and why (never deletes)")
    output_flags(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="one cleanup run; a dry run unless --execute or dry_run: false")
    mode_flags(p)
    output_flags(p)
    p.add_argument(
        "--skip-first-run-guard", action="store_true", help="allow deleting on the very first run on this host"
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("daemon", help="run on the cron schedule (for the container image)")
    mode_flags(p)
    p.add_argument("--run-now", action="store_true", help="also run once at startup")
    p.add_argument("--show-kept", action="store_true", help="list kept objects in each report")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("df", help="disk usage report, like docker system df")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_df)

    p = sub.add_parser("check-config", help="validate the config and print the effective settings")
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser("healthcheck", help="exit 0 if the daemon heartbeat is fresh (Docker HEALTHCHECK)")
    p.add_argument("--file", help="heartbeat file (default: $DOCKER_JANITOR_HEARTBEAT)")
    p.add_argument("--max-age", type=float, default=120.0)
    p.set_defaults(func=cmd_healthcheck)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except ConnectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        return 130
