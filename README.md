# docker-janitor

[![CI](https://github.com/saizul/docker-janitor/actions/workflows/ci.yml/badge.svg)](https://github.com/saizul/docker-janitor/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/saizul/docker-janitor?sort=semver)](https://github.com/saizul/docker-janitor/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Container image](https://img.shields.io/badge/ghcr.io-saizul%2Fdocker--janitor-2496ED?logo=docker&logoColor=white)](https://github.com/saizul/docker-janitor/pkgs/container/docker-janitor)
![Python](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**Docker cleanup you can leave running.** docker-janitor removes old stopped containers,
dangling and unused images (always keeping the newest tags of every repository), unused
networks, optionally unused volumes, and old build cache, on a cron schedule, with a
plan that explains every decision. It is a **dry run until you say otherwise**.

`docker system prune -a` on a cron job is how disks get freed and databases get lost.
docker-janitor is the careful version: age thresholds, label protection, "keep the
newest N", a first-run guard, a safety stop and no forced removals.

```text
$ docker-janitor plan
Would remove 4 object(s):
KIND       ID            NAME                         AGE  SIZE     REASON
---------  ------------  ---------------------------  ---  -------  -------------------
container  3f2a9c1b7e40  migrate-2026-10-02           6w   -        exited for 6w
image      9b1e0d55c3aa  ghcr.io/acme/api:1.4.0       9w   412.3MB  unused for 9w
image      51c0aa9e2f17  <none>                       12d  188.0MB  dangling for 12d
network    c09d7e3f3c18  old-stack_default            5w   -        unused for 5w

Build cache: would prune entries unused for 1w; estimate 31 entr(ies), up to 2.1GB

27 object(s) kept (use --show-kept to list them with the reason).

Summary: 1 container(s), 2 image(s), 1 network(s), 0 volume(s); up to 2.7GB reclaimable.
DRY RUN: nothing was removed. Use --execute (or dry_run: false) to delete.
```

## Contents

- [Safety first](#safety-first)
- [Quick start](#quick-start)
- [Policies](#policies)
- [Protecting things](#protecting-things)
- [Configuration reference](#configuration-reference)
- [CLI](#cli)
- [Scheduling and health checks](#scheduling-and-health-checks)
- [Least privilege](#least-privilege)
- [FAQ](#faq)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Development](#development)
- [License](#license)

## Safety first

These are the defaults. You can loosen some of them, but you have to do it on purpose.

| Safeguard | Default |
| --- | --- |
| **Dry run** | Every run only prints the plan unless you pass `--execute` or set `dry_run: false` |
| **First run is always a dry run** | Even with `--execute`, nothing is deleted on a host until one dry run has been recorded in the state file (`first_run_dry_run: true`) |
| **Safety stop** | A run that would remove more than `max_removals` (100) objects removes nothing and exits with code 3 |
| **Volumes are off** | `volumes.enabled: false`. When enabled, only anonymous volumes (random 64-character names) unless you also set `anonymous_only: false` |
| **`janitor.keep=true`** | Always protected, on containers, images, volumes and networks; it cannot be removed from the protect list |
| **Never forced** | No call uses `force=True`. If something changed after planning (a container started, an image got a new container), the Docker Engine refuses and the refusal is reported |
| **Running things are untouchable** | Only `exited`, `created` and `dead` containers are candidates; `running`, `paused` and `restarting` cannot even be configured |
| **Restart policies** | A stopped container with `restart: always`, `unless-stopped` or `on-failure` is kept (`keep_with_restart_policy: true`) |
| **Newest images stay** | The 3 newest images of every repository are kept, whatever their age (`keep_latest: 3`) |
| **In use means kept** | Images, networks and volumes used by any remaining container are kept |
| **Age thresholds** | Containers 7 days after they stopped, dangling images after 1 day, unused tagged images after 30 days, networks after 7 days, build cache after 7 days |
| **Built-ins are skipped** | `bridge`, `host`, `none`, `docker_gwbridge`, `ingress` and swarm-scoped networks; volumes with non-local drivers |
| **Its own image** | The image labels itself `janitor.keep=true` |
| **Explained decisions** | `plan --show-kept` lists every candidate that was kept and why |

## Quick start

### 1. Look before you leap

```bash
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/saizul/docker-janitor plan --show-kept
```

`plan` never deletes anything. Read the list, then label anything you want to keep for
good (labels are set when an object is created):

```bash
docker run --label janitor.keep=true ...            # containers (Compose: labels: {janitor.keep: "true"})
docker volume create --label janitor.keep=true pgdata
docker network create --label janitor.keep=true backend
# images: LABEL janitor.keep="true" in the Dockerfile, or protect.names: ["myapp:*"]
```

### 2. Run it on a schedule

```yaml
# docker-compose.yml
services:
  docker-janitor:
    image: ghcr.io/saizul/docker-janitor:latest
    restart: unless-stopped
    environment:
      DOCKER_JANITOR_SCHEDULE: "30 3 * * *"   # every night at 03:30 (TZ below)
      DOCKER_JANITOR_DRY_RUN: "true"          # flip to "false" once the plans look right
      TZ: Europe/Berlin
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - janitor-state:/data                   # keeps the first-run guard and history
    labels:
      janitor.keep: "true"
volumes:
  janitor-state:
```

`docker compose up -d`, then read `docker compose logs docker-janitor` after the first
night. When the plan is what you expect, set `DOCKER_JANITOR_DRY_RUN: "false"`.

### 3. Or as a one-off / host cron job

```bash
pip install docker-janitor                 # or: pipx install docker-janitor
docker-janitor plan                        # what would go, and why
docker-janitor run --execute               # first time: still a dry run (first-run guard)
docker-janitor run --execute               # now it deletes
docker-janitor df                          # disk usage, like docker system df
```

## Policies

| Policy | What is removed | Default |
| --- | --- | --- |
| `containers` | Containers in `exited`, `created` or `dead` state whose stop time (or creation time, for `created`) is older than `older_than`. Anonymous volumes are **not** removed with them (`v=False`) | on, 7d |
| `images` | **Dangling** images older than `dangling_older_than`; **tagged** images no container uses that are older than `unused_older_than` and are not among the `keep_latest` newest of their repository. Age is the later of build time and last tag (pull) time | on, 1d / 30d, keep 3 |
| `networks` | User-defined networks no container is attached to, older than `older_than` | on, 7d |
| `volumes` | Volumes no container mounts, older than `older_than`; only anonymous ones unless `anonymous_only: false` | **off**, 30d |
| `build_cache` | `docker builder prune` with `until=older_than` and optional `keep_storage` / `all` | on, 7d |

Order of a run: containers first, then images, networks and volumes, so objects freed by
removed containers are cleaned in the same run. The plan already accounts for that.

Tagged images: every tag of a removable image is untagged one by one; Docker deletes the
image with its last tag. An image is kept as a whole if any of its tags is protected or
among the newest of its repository.

## Protecting things

```yaml
protect:
  labels:            # denylist: objects with any of these labels are never removed
    - janitor.keep=true          # always present, even if you leave it out
    - com.example.tier=database  # key=value
    - com.example.important      # key only: any value
  only_labels:       # allowlist: if set, ONLY objects with one of these labels are candidates
    - com.example.ephemeral=true
  names:             # globs on container, volume and network names and image tags/repositories
    - "postgres:*"
    - "*-backup"
```

`only_labels` turns docker-janitor into an opt-in tool: perfect for CI runners, where you
label throwaway objects and want nothing else touched.

## Configuration reference

The config file is YAML; every key is optional. The path comes from `-c`,
`$DOCKER_JANITOR_CONFIG`, `./docker-janitor.yaml` or `/config/config.yaml` (the image). With
no file at all, the safe defaults below apply. A full example is in
[`examples/docker-janitor.yaml`](examples/docker-janitor.yaml).

| Key | Default | Description |
| --- | --- | --- |
| `dry_run` | `true` | Only print the plan. `--execute` / `--dry-run` override it |
| `first_run_dry_run` | `true` | Downgrade the first run on a host to a dry run |
| `max_removals` | `100` | Safety stop for real runs (`0` = no limit; `--max-removals` overrides) |
| `state_file` | `~/.local/state/docker-janitor/state.json` (`/data/state.json` in the image) | Run history |
| `schedule` | `0 4 * * *` | Cron expression for `daemon` (5 fields, names, ranges, steps, `@daily`, `@hourly`, ...) |
| `protect.labels` / `only_labels` / `names` | `[janitor.keep=true]` / `[]` / `[]` | See [Protecting things](#protecting-things) |
| `containers.enabled` / `older_than` | `true` / `7d` | |
| `containers.statuses` | `[exited, created, dead]` | Subset of those three only |
| `containers.keep_with_restart_policy` | `true` | Keep stopped containers that have a restart policy |
| `images.enabled` | `true` | |
| `images.dangling_older_than` | `1d` | `false` disables dangling cleanup |
| `images.unused_older_than` | `30d` | `false` disables tagged-image cleanup |
| `images.keep_latest` | `3` | Newest images kept per repository (`0` = none) |
| `networks.enabled` / `older_than` | `true` / `7d` | |
| `volumes.enabled` / `older_than` / `anonymous_only` | `false` / `30d` / `true` | |
| `build_cache.enabled` / `older_than` | `true` / `7d` | |
| `build_cache.keep_storage` / `all` | none / `false` | e.g. `10GB`; `all` also prunes shared cache records |
| `healthcheck.url` | none | Ping URL, see below |
| `healthcheck.style` | `healthchecks` | `healthchecks` (`/start`, success, `/fail`) or `plain` (success only) |
| `healthcheck.timeout` / `send_report` | `10s` / `true` | `send_report` POSTs the plan or result text with the ping |

Durations: `30s`, `12h`, `7d`, `2w`, `1d12h` or seconds. Sizes: `500MB`, `10GB`, `2GiB` or bytes.

Environment overrides (handy with `docker run -e`): `DOCKER_JANITOR_DRY_RUN`,
`DOCKER_JANITOR_SCHEDULE`, `DOCKER_JANITOR_HEALTHCHECK_URL`, `DOCKER_JANITOR_MAX_REMOVALS`,
`DOCKER_JANITOR_STATE_FILE`. The Docker connection uses the usual `DOCKER_HOST`,
`DOCKER_TLS_VERIFY` and `DOCKER_CERT_PATH`; `DOCKER_JANITOR_TIMEOUT` sets the API timeout
(default 120 seconds).

## CLI

```text
docker-janitor [-c CONFIG] [--log-level LEVEL] COMMAND

plan           show what would be removed and why; never deletes
               --show-kept, --json
run            one cleanup run (a dry run unless --execute or dry_run: false)
               --execute | --dry-run, --max-removals N, --skip-first-run-guard, --show-kept, --json
daemon         run on the schedule (default command of the image)
               --execute | --dry-run, --run-now, --show-kept
df             disk usage by type with reclaimable space (--json)
check-config   validate and print the effective config and the next 3 run times
healthcheck    exit 0 if the daemon heartbeat is fresh (used by the image HEALTHCHECK)
```

Exit codes: `0` success (also when Docker refused a removal; refusals are listed),
`1` cannot reach Docker, `2` configuration error, `3` safety stop (`max_removals`).

## Scheduling and health checks

`docker-janitor daemon` has a built-in cron scheduler (local time; set `TZ` in the
container), so no host crontab is needed. Each run is logged with the full plan and result.

Add a dead man's switch so you notice when cleanup stops running:

```yaml
healthcheck:
  url: https://hc-ping.com/your-uuid     # healthchecks.io: /start, then success or /fail
  # url: https://kuma.example.com/api/push/abc?status=up
  # style: plain                         # Uptime Kuma and other push URLs: success ping only
```

The success ping carries the plan or result text as the request body. A failing monitor
never fails the cleanup. The image also has a Docker `HEALTHCHECK` based on a heartbeat file.

## Least privilege

Access to `/var/run/docker.sock` is equivalent to root on the host, for any tool. To limit
what docker-janitor can do, run it behind a filtering socket proxy and point `DOCKER_HOST`
at it; see [`examples/socket-proxy.yml`](examples/socket-proxy.yml). The image runs as root
only because the socket is root-owned on most hosts; with a TCP socket proxy you can run it
as a non-root `user:` as long as `/data` stays writable for that user.

## FAQ

**Why not just `docker system prune -af --volumes` in cron?** It removes every unused image
(including the one you roll back to), every stopped container (including the one you
stopped for maintenance) and every unused volume (including your database's, if its
container happens to be down). It also has no plan, no history and no "keep newest N".

**I set `--execute` but nothing was deleted.** That is the first-run guard: the first run on
a host is a dry run. Run again, or pass `--skip-first-run-guard` if you have already reviewed
the plan. Make sure `/data` (or `state_file`) is persistent, otherwise every run is a "first" run.

**Can it delete my database volume?** Not with the defaults: volumes are off. If you enable
them, named volumes stay protected by `anonymous_only: true`, volumes used by any container
are kept, and `janitor.keep=true` always wins. Label important volumes anyway.

**A run stopped with "Safety stop".** More objects than `max_removals` were planned. Check the
plan; if it is right (for example the first cleanup of a busy CI host), run once with
`--max-removals 1000` or raise `max_removals`.

**How old is an image?** The later of its build time (`Created`) and when it was last tagged
or pulled (`Metadata.LastTagTime`, reported by recent Docker Engine versions), so a freshly pulled old image is not "old".

**Does it work with Podman?** Podman's Docker-compatible socket mostly works, but it is not
tested yet. Reports are welcome.

**Swarm or Kubernetes?** Swarm-scoped networks are skipped. On a swarm node, run one
instance per node. Kubernetes nodes manage images with the kubelet's own garbage collection.

## Roadmap

Ideas are tracked as issues; the ones marked **good first issue** are a great start.

- `--only images,volumes` to limit a run (**good first issue**)
- Protect whole Compose projects by name (**good first issue**)
- Keep failed containers longer than successful ones (**good first issue**)
- Markdown report and a run summary to Slack or Telegram (**good first issue**)
- Prometheus textfile metrics (**good first issue**)
- Podman support and tests

## Contributing

Contributions are welcome, from a test for an odd timestamp to a new policy. Read
[CONTRIBUTING.md](CONTRIBUTING.md) for the setup, the project layout, the safety rules
for code changes and a list of good first issues. Please follow the
[Code of Conduct](CODE_OF_CONDUCT.md) and report safety or security problems privately as
described in [SECURITY.md](SECURITY.md).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .[dev]
pytest                                   # uses a fake Docker client, no daemon needed
ruff check . && ruff format --check .
docker build -t docker-janitor:dev .
```

## License

[MIT](LICENSE) © Saizul Amin
