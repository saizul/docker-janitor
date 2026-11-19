## What does this pull request do?

<!-- One or two sentences. Link the issue: "Closes #123". -->

## Type

- [ ] New or changed cleanup policy
- [ ] Protection, safety or reporting change
- [ ] Bug fix
- [ ] Documentation

## Safety checklist

- [ ] Defaults stay safe: dry run by default, volumes opt-in, `janitor.keep=true` always respected
- [ ] Nothing is removed with `force=True`; running, paused and restarting containers are never touched
- [ ] New removal logic shows up in `plan` with a reason, for both removed and kept objects
- [ ] Tests cover what is removed **and** what must be kept (the fake client is in `tests/conftest.py`)

## Checklist

- [ ] `pytest` passes (tests never talk to a real Docker Engine)
- [ ] `ruff check . && ruff format --check .` passes
- [ ] README and `examples/docker-janitor.yaml` updated for new options
- [ ] CHANGELOG.md updated under "Unreleased"
