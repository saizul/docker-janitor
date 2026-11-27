from pathlib import Path

import pytest

from docker_janitor.config import KEEP_LABEL, ConfigError, apply_env, load_config, parse_config

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_defaults_are_safe():
    cfg = parse_config(None)
    assert cfg.dry_run is True
    assert cfg.first_run_dry_run is True
    assert cfg.volumes.enabled is False
    assert cfg.volumes.anonymous_only is True
    assert cfg.protect.labels == [KEEP_LABEL]
    assert cfg.containers.statuses == ["exited", "created", "dead"]
    assert cfg.containers.keep_with_restart_policy is True
    assert cfg.containers.failed_older_than is None
    assert cfg.images.keep_latest == 3
    assert cfg.max_removals == 100


def test_keep_label_cannot_be_removed():
    cfg = parse_config({"protect": {"labels": ["tier=db"]}})
    assert cfg.protect.labels == [KEEP_LABEL, "tier=db"]


def test_full_example_config():
    cfg = load_config(EXAMPLES / "docker-janitor.yaml", env={})
    assert cfg.schedule == "30 3 * * *"
    assert cfg.images.unused_older_than == 14 * 86400
    assert cfg.build_cache.keep_storage == 10_000_000_000
    assert cfg.dry_run is True


def test_sections_accept_booleans():
    cfg = parse_config({"volumes": True, "networks": False})
    assert cfg.volumes.enabled and not cfg.networks.enabled


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"prune_everything": True}, "unknown key"),
        ({"containers": {"statuses": ["running"]}}, "never removed"),
        ({"containers": {"statuses": []}}, "never removed"),
        ({"containers": {"older_than": "soon"}}, "older_than"),
        ({"containers": {"failed_older_than": "later"}}, "failed_older_than"),
        ({"images": {"keep_latest": -1}}, "keep_latest"),
        ({"images": {"colour": 1}}, "unknown key"),
        ({"build_cache": {"keep_storage": "huge"}}, "keep_storage"),
        ({"schedule": "every day"}, "schedule"),
        ({"max_removals": -5}, "max_removals"),
        ({"dry_run": "maybe"}, "dry_run"),
        ({"healthcheck": {"url": "ftp://x"}}, "http"),
        ({"healthcheck": {"style": "fancy"}}, "style"),
        ({"protect": {"labels": [""]}}, "list of strings"),
        ({"protect": "yes"}, "mapping"),
        ([1], "mapping"),
    ],
)
def test_config_errors(data, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_env_overrides():
    env = {
        "DOCKER_JANITOR_DRY_RUN": "false",
        "DOCKER_JANITOR_SCHEDULE": "@hourly",
        "DOCKER_JANITOR_HEALTHCHECK_URL": "https://hc.example.invalid/abc/",
        "DOCKER_JANITOR_MAX_REMOVALS": "7",
    }
    cfg = parse_config(apply_env({"healthcheck": {"timeout": 3}}, env))
    assert cfg.dry_run is False
    assert cfg.schedule == "@hourly"
    assert cfg.healthcheck.url == "https://hc.example.invalid/abc"
    assert cfg.healthcheck.timeout == 3
    assert cfg.max_removals == 7
    with pytest.raises(ConfigError, match="MAX_REMOVALS"):
        apply_env({}, {"DOCKER_JANITOR_MAX_REMOVALS": "lots"})


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("images: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad)
    assert load_config(None, env={}).dry_run is True


def test_failed_older_than():
    assert parse_config({"containers": {"failed_older_than": "30d"}}).containers.failed_older_than == 30 * 86400
    assert parse_config({"containers": {"failed_older_than": False}}).containers.failed_older_than is None
    assert parse_config({"containers": {"failed_older_than": None}}).containers.failed_older_than is None
