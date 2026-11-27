# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `containers.failed_older_than` sets a separate age for containers that failed: exited with a
  non-zero code, killed by the OOM killer, or `dead`. Use it to keep crashed containers (and
  their logs) longer for debugging. Unset, failed containers use `containers.older_than`.
  The plan shows the failure in the reason (`exited for 2w, exit code 137`).
- `--only KINDS` for `plan`, `run` and `daemon` limits a run to some of `containers`, `images`,
  `networks`, `volumes` and `build_cache` without editing the config. It never enables a kind
  the config disables.

### Fixed

- Test suite: the fake Docker timestamps could lie up to 0.12 s in the future, which made
  age assertions such as `stopped 1d ago` fail at random.

## [0.1.0] - 2026-11-19

### Added

- Cleanup policies, all driven by the Docker Engine API through the `docker` SDK:
  - stopped containers (`exited`, `created`, `dead`) older than a duration since they stopped;
    containers with a restart policy are kept by default;
  - dangling images and unused tagged images older than a duration (using the later of build
    time and last tag time), always keeping the newest N images per repository;
  - unused networks (built-in and swarm networks are never touched);
  - unused volumes, **off by default** and limited to anonymous volumes by default;
  - build cache prune with an age filter and `keep_storage`.
- Protection: `janitor.keep=true` is always honoured, extra label denylists, a label allowlist
  (`only_labels`) and name globs for containers, volumes, networks and image tags.
- Safety: dry run by default, the first run on a host is always a dry run, a `max_removals`
  safety stop, no forced removals, and a plan that explains why every object is removed or kept.
- `plan`, `run`, `daemon` (built-in cron scheduler), `df`, `check-config` and `healthcheck` commands,
  with text and JSON output.
- Health check pings for healthchecks.io style monitors (`/start`, success with report, `/fail`)
  and plain push URLs.
- Container image on `ghcr.io/saizul/docker-janitor` with a health check; it labels itself
  `janitor.keep=true`.

[Unreleased]: https://github.com/saizul/docker-janitor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saizul/docker-janitor/releases/tag/v0.1.0
