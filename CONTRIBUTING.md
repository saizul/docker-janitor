# Contributing to docker-janitor

Thank you for helping! Bug reports, new policies, better reports and documentation are
all welcome. Because this tool deletes things, every change is reviewed with one
question first: **is it still safe by default?**

- [Ways to contribute](#ways-to-contribute)
- [Good first issues](#good-first-issues)
- [Development setup](#development-setup)
- [Project layout](#project-layout)
- [Safety rules for code changes](#safety-rules-for-code-changes)
- [Pull request process](#pull-request-process)

## Ways to contribute

| Size | Idea |
| --- | --- |
| 10 minutes | Fix a typo, add a FAQ entry, improve an error message |
| 30 minutes | Add a test for an edge case (odd timestamps, multi-repo tags, missing fields) |
| 1-2 hours | One of the good first issues below |
| Larger | Notifications, Prometheus metrics, Swarm/Podman support (open an issue first) |

Comment on an issue before you start so nobody else picks the same one.

## Good first issues

Each of these is self-contained and touches one or two files.

1. **Markdown report.** `--format markdown` for pasting a plan into an issue or a chat.
   (`report.py`, `cli.py`, tests)
2. **Keep failed containers longer.** `containers.failed_older_than: 14d` for containers that
   exited with a non-zero code, so there is time to read their logs. (`config.py`, `planner.py`, tests)
3. **Protect Compose projects.** `protect.compose_projects: [prod]` keeps every object with the
   label `com.docker.compose.project=prod`. (`config.py`, `planner.py`, tests, README)
4. **Exclude images by repository.** `images.keep_repositories: ["ghcr.io/acme/*"]` keeps all tags
   of matching repositories regardless of age. (`config.py`, `planner.py`, tests)
5. **Slack/Telegram summary.** Optional `notify.webhook_url` that posts the result summary after
   each run, without new dependencies (urllib). (`job.py`, tests)
6. **`df --top 10`.** List the largest images and volumes. (`report.py`, `cli.py`, tests)
7. **Prometheus textfile output.** Write `docker_janitor_removed_total` and
   `docker_janitor_reclaimed_bytes` to a file for node_exporter's textfile collector. (`job.py`, tests)

## Development setup

```bash
git clone https://github.com/saizul/docker-janitor
cd docker-janitor
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .[dev]
pytest
ruff check . && ruff format --check .
```

Python 3.11+ is needed. **Tests never talk to a real Docker Engine**: `tests/conftest.py`
has `FakeClient` with builders such as `add_container(...)`, `add_image(...)`,
`add_volume(...)` and `add_network(...)`, and records every removal call so tests can
assert that nothing was forced.

To look at your own machine safely, `plan` never deletes anything:

```bash
docker-janitor plan --show-kept
docker-janitor -c examples/docker-janitor.yaml plan --json | less
```

The CI workflow also runs the image against the GitHub runner's real Docker Engine, on
objects created by the workflow and selected with `only_labels: ["janitor.ci=true"]`.

## Project layout

| Path | Responsibility |
| --- | --- |
| `src/docker_janitor/config.py` | Policies, protection and defaults; YAML and environment overrides |
| `src/docker_janitor/planner.py` | `Planner` builds a `Plan` (remove + keep with reasons); `execute()` |
| `src/docker_janitor/job.py` | One run: first-run guard, `max_removals`, report, pings; the `Daemon` |
| `src/docker_janitor/cron.py` | Cron expression parser |
| `src/docker_janitor/report.py` | Tables for plans, results and disk usage |
| `src/docker_janitor/healthcheck.py` | Dead man's switch pings |
| `src/docker_janitor/state.py` | Run history used by the first-run guard |
| `src/docker_janitor/cli.py` | `plan`, `run`, `daemon`, `df`, `check-config`, `healthcheck` |

## Safety rules for code changes

- New policies are **off by default** or as conservative as the existing ones.
- The planner decides; `execute()` only removes what the plan lists. Never add a removal that
  is not shown in `plan`.
- Never pass `force=True`, and never remove running, paused or restarting containers.
- Every kept object that was a candidate gets a reason, so `--show-kept` explains itself.
- Tests must cover both sides: what is removed and what must be kept.

## Pull request process

1. Fork and create a branch (`feature/compose-projects`, `fix/timestamp-parsing`).
2. Keep pull requests focused; fill in the checklist in the template.
3. Add an entry to `CHANGELOG.md` under "Unreleased".
4. A maintainer reviews within a few days.

By contributing you agree that your work is released under the MIT License and
that you follow the [Code of Conduct](CODE_OF_CONDUCT.md).
