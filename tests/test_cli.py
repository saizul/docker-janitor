import json
import os
import time

import pytest

from conftest import FakeClient, ago
from docker_janitor import cli, report


@pytest.fixture
def fake(monkeypatch, tmp_path):
    client = FakeClient()
    client.add_container("old", finished=ago(days=30))
    client.add_image("dangling", [], created=ago(days=10), size=5_000_000)
    monkeypatch.setattr(cli, "client_factory", lambda: client)
    monkeypatch.setenv("DOCKER_JANITOR_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.delenv("DOCKER_JANITOR_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    return client


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "docker-janitor 0.1.0" in capsys.readouterr().out


def test_plan_never_deletes(fake, capsys):
    assert cli.main(["plan", "--show-kept"]) == 0
    out = capsys.readouterr().out
    assert "Would remove 2 object(s)" in out
    assert "DRY RUN" in out
    assert fake.removed == []


def test_run_execute_after_first_dry_run(fake, capsys):
    assert cli.main(["run", "--execute"]) == 0
    assert "First run on this host" in capsys.readouterr().out
    assert fake.removed == []
    assert cli.main(["run", "--execute"]) == 0
    assert len(fake.removed) == 2


def test_run_skip_guard_and_json(fake, capsys):
    assert cli.main(["run", "--execute", "--skip-first-run-guard", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "execute"
    assert len(data["result"]["removed"]) == 2


def test_max_removals_flag(fake, capsys):
    assert cli.main(["run", "--execute", "--skip-first-run-guard", "--max-removals", "1"]) == 3
    assert "Safety stop" in capsys.readouterr().out
    assert fake.removed == []


def test_config_file_and_env(fake, tmp_path, capsys, monkeypatch):
    (tmp_path / "docker-janitor.yaml").write_text("images: {enabled: false}\n", encoding="utf-8")
    assert cli.main(["plan", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["plan"]["summary"]["images"] == 0
    monkeypatch.setenv("DOCKER_JANITOR_CONFIG", str(tmp_path / "missing.yaml"))
    assert cli.main(["plan"]) == 2
    assert "cannot read config" in capsys.readouterr().err


def test_check_config_hides_ping_url(fake, capsys, monkeypatch):
    monkeypatch.setenv("DOCKER_JANITOR_HEALTHCHECK_URL", "https://hc.example.invalid/secret-uuid")
    assert cli.main(["check-config"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["healthcheck"]["url"] == "<set>"
    assert "secret-uuid" not in captured.out
    assert len(data["next_runs"]) == 3
    assert "safe defaults" in captured.err


def test_df(fake, capsys):
    fake.disk_usage = {
        "LayersSize": 3_000_000_000,
        "Images": [
            {"Size": 1_000_000_000, "SharedSize": 0, "Containers": 1},
            {"Size": 2_000_000_000, "SharedSize": 0, "Containers": 0},
        ],
        "Containers": [{"State": "running", "SizeRw": 10}, {"State": "exited", "SizeRw": 1_000_000}],
        "Volumes": [{"UsageData": {"Size": 500, "RefCount": 0}}, {"UsageData": {"Size": -1, "RefCount": 1}}],
        "BuildCache": [{"Size": 7000, "InUse": False, "Shared": False}, {"Size": 1, "InUse": True}],
    }
    assert cli.main(["df"]) == 0
    out = capsys.readouterr().out
    assert "Images" in out and "2.0GB (66%)" in out
    assert cli.main(["df", "--json"]) == 0
    rows = {r["type"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["Images"]["reclaimable"] == 2_000_000_000
    assert rows["Containers"]["reclaimable"] == 1_000_000
    assert rows["Local Volumes"]["reclaimable"] == 500
    assert rows["Build Cache"]["reclaimable"] == 7000


def test_cannot_connect(monkeypatch, capsys, tmp_path):
    def boom():
        raise FileNotFoundError("no socket")

    monkeypatch.setattr(cli, "client_factory", boom)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["plan"]) == 1
    assert "cannot connect to the Docker Engine" in capsys.readouterr().err


def test_healthcheck(tmp_path, capsys):
    hb = tmp_path / "hb"
    assert cli.main(["healthcheck", "--file", str(hb)]) == 1
    hb.touch()
    assert cli.main(["healthcheck", "--file", str(hb)]) == 0
    old = time.time() - 1000
    os.utime(hb, (old, old))
    assert cli.main(["healthcheck", "--file", str(hb)]) == 1
    assert "unhealthy" in capsys.readouterr().err


def test_daemon_command(fake, monkeypatch):
    seen = {}

    def fake_run(self, *, run_now=False, max_runs=None):
        seen["run_now"] = run_now
        seen["outcome"] = self.job()

    monkeypatch.setattr(cli.Daemon, "run", fake_run)
    assert cli.main(["daemon", "--run-now"]) == 0
    assert seen["run_now"] is True
    assert seen["outcome"].mode == "dry-run"
    assert fake.removed == []


def test_table_and_plan_formatting(fake):
    from docker_janitor.config import parse_config
    from docker_janitor.planner import Planner

    plan = Planner(fake, parse_config({})).build()
    text = report.format_plan(plan, dry_run=True)
    assert "KIND" in text and "container" in text
    assert "Summary: 1 container(s), 1 image(s), 0 network(s), 0 volume(s)" in text
    empty = Planner(FakeClient(), parse_config({"build_cache": False})).build()
    assert "Nothing to remove" in report.format_plan(empty, dry_run=False)
