# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

## What counts as a vulnerability

docker-janitor deletes things, so safety bugs are treated like security bugs:

- Removing an object that the documentation says is protected: anything labelled
  `janitor.keep=true`, matched by `protect`, in use, running, paused or restarting,
  among the newest `keep_latest` images, or younger than the configured age
- Deleting anything in a dry run, on the first run on a host, or above `max_removals`
- Removing volumes when `volumes.enabled` is false, or named volumes when
  `volumes.anonymous_only` is true
- Calling the Docker API with `force=True`
- A health check URL or other secret appearing in logs or command output
- Crafted object names or labels that break the plan output or JSON

## Reporting

Please **do not** open a public issue. Use
[GitHub private vulnerability reporting](https://github.com/saizul/docker-janitor/security/advisories/new)
with the version, your Docker Engine version, the config and the plan output. Do not
include registry credentials or health check URLs.

You will get an answer within 5 working days. Fixes are released as soon as
possible and credited in the changelog unless you prefer otherwise.

## Hardening checklist for operators

- Access to the Docker socket is equivalent to root on the host. Prefer a filtering
  socket proxy (see `examples/socket-proxy.yml`) over mounting `/var/run/docker.sock`.
- Keep the `/data` volume so the first-run guard and run history survive restarts.
- Label everything that must survive with `janitor.keep=true`, and review
  `docker-janitor plan --show-kept` after changing a policy.
