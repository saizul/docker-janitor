import json
import threading
from datetime import datetime, timedelta

from conftest import ago
from docker_janitor.config import parse_config
from docker_janitor.healthcheck import Pinger
from docker_janitor.job import Daemon, RunOutcome, run_once
from docker_janitor.state import State


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class Opener:
    def __init__(self, status=200, error=None):
        self.requests = []
        self.status, self.error = status, error

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return FakeResponse(self.status)

    @property
    def urls(self):
        return [r.full_url for r in self.requests]


def setup(client, tmp_path, **raw):
    raw.setdefault("state_file", str(tmp_path / "state.json"))
    config = parse_config(raw)
    client.add_container("old", finished=ago(days=30))
    return config


def run(client, config, opener=None, **kwargs):
    printed = []
    pinger = Pinger(config.healthcheck, opener=opener or Opener())
    outcome = run_once(client, config, printer=printed.append, pinger=pinger, **kwargs)
    return outcome, "\n".join(printed)


def test_default_is_a_dry_run(client, tmp_path):
    config = setup(client, tmp_path)
    outcome, text = run(client, config)
    assert outcome.mode == "dry-run" and outcome.exit_code == 0
    assert client.removed == []
    assert "Would remove 1 object(s)" in text
    assert "DRY RUN: nothing was removed" in text
    assert json.loads((tmp_path / "state.json").read_text())["runs"] == 1


def test_first_run_guard_turns_execute_into_dry_run(client, tmp_path):
    config = setup(client, tmp_path)
    outcome, text = run(client, config, execute_requested=True)
    assert outcome.mode == "dry-run"
    assert client.removed == []
    assert "First run on this host" in text
    outcome, text = run(client, config, execute_requested=True)
    assert outcome.mode == "execute"
    assert client.removed == [("container", "old")]
    assert "Removed 1 object(s)" in text
    state = State(config.state_file).data
    assert (state["runs"], state["last_mode"], state["last_removed"]) == (2, "execute", 1)


def test_dry_run_false_in_config_and_skip_guard(client, tmp_path):
    config = setup(client, tmp_path, dry_run=False)
    outcome, _ = run(client, config, skip_first_run_guard=True)
    assert outcome.mode == "execute"
    assert client.removed == [("container", "old")]


def test_first_run_guard_can_be_disabled(client, tmp_path):
    config = setup(client, tmp_path, first_run_dry_run=False)
    outcome, _ = run(client, config, execute_requested=True)
    assert outcome.mode == "execute"


def test_dry_run_flag_beats_config(client, tmp_path):
    config = setup(client, tmp_path, dry_run=False, first_run_dry_run=False)
    outcome, _ = run(client, config, execute_requested=False)
    assert outcome.mode == "dry-run"
    assert client.removed == []


def test_max_removals_safety_stop(client, tmp_path):
    config = setup(client, tmp_path, max_removals=2, first_run_dry_run=False)
    for n in range(3):
        client.add_container(f"extra{n}", finished=ago(days=30))
    opener = Opener()
    config.healthcheck.url = "https://hc.example.invalid/uuid"
    outcome, text = run(client, config, opener=opener, execute_requested=True)
    assert outcome.mode == "aborted" and outcome.exit_code == 3
    assert client.removed == []
    assert "Safety stop: the plan would remove 4 objects" in text
    assert opener.urls == ["https://hc.example.invalid/uuid/start", "https://hc.example.invalid/uuid/fail"]
    # A dry run is never stopped by the limit.
    outcome, _ = run(client, config)
    assert outcome.mode == "dry-run"


def test_healthcheck_pings_with_report(client, tmp_path):
    config = setup(client, tmp_path, healthcheck={"url": "https://hc.example.invalid/uuid"})
    opener = Opener()
    run(client, config, opener=opener)
    assert opener.urls == ["https://hc.example.invalid/uuid/start", "https://hc.example.invalid/uuid"]
    assert opener.requests[0].get_method() == "GET"
    assert opener.requests[1].get_method() == "POST"
    assert b"Would remove 1 object(s)" in opener.requests[1].data


def test_plain_style_and_failing_monitor(client, tmp_path):
    config = setup(
        client, tmp_path, healthcheck={"url": "https://kuma.example.invalid/api/push/x?status=up", "style": "plain"}
    )
    opener = Opener(error=OSError("unreachable"))
    outcome, _ = run(client, config, opener=opener)
    assert outcome.exit_code == 0  # a broken monitor never fails the cleanup
    assert opener.urls == ["https://kuma.example.invalid/api/push/x?status=up"]


def test_docker_error_pings_fail(client, tmp_path):
    config = setup(client, tmp_path, healthcheck={"url": "https://hc.example.invalid/u"})

    def broken(**kwargs):
        raise ConnectionError("socket gone")

    client.containers.list = broken
    opener = Opener()
    outcome, text = run(client, config, opener=opener)
    assert outcome.mode == "error" and outcome.exit_code == 1
    assert "could not read the Docker state" in text
    assert opener.urls[-1] == "https://hc.example.invalid/u/fail"


def test_json_output(client, tmp_path):
    config = setup(client, tmp_path)
    _, text = run(client, config, as_json=True)
    data = json.loads(text)
    assert data["mode"] == "dry-run"
    assert data["plan"]["summary"]["containers"] == 1


def test_unwritable_state_file_keeps_guard(client, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    config = setup(client, tmp_path, state_file=str(blocker / "state.json"))
    for _ in range(2):
        outcome, _ = run(client, config, execute_requested=True)
        assert outcome.mode == "dry-run"
    assert client.removed == []


def test_corrupt_state_file_is_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert State(str(path)).has_previous_run is False


class FakeNow:
    def __init__(self, start):
        self.value = start

    def __call__(self):
        return self.value


def test_daemon_runs_on_schedule(tmp_path):
    now = FakeNow(datetime(2026, 11, 19, 3, 59, 30))
    config = parse_config({"schedule": "0 4 * * *"})
    stop = threading.Event()
    calls = []

    def job():
        calls.append(now.value)
        return RunOutcome("dry-run", 0)

    daemon = Daemon(job, config, heartbeat_file=str(tmp_path / "hb"), now=now, stop_event=stop)

    def fake_wait(seconds):
        now.value += timedelta(seconds=seconds)
        return False

    stop.wait = fake_wait
    daemon.run(run_now=True, max_runs=3)
    assert calls[0] == datetime(2026, 11, 19, 3, 59, 30)
    assert calls[1] == datetime(2026, 11, 19, 4, 0)
    assert calls[2] == datetime(2026, 11, 20, 4, 0)
    assert (tmp_path / "hb").exists()


def test_daemon_survives_crashing_job():
    config = parse_config({"schedule": "* * * * *"})
    now = FakeNow(datetime(2026, 1, 1, 0, 0, 0))
    stop = threading.Event()

    def fake_wait(seconds):
        now.value += timedelta(seconds=seconds)
        return False

    stop.wait = fake_wait

    def job():
        raise RuntimeError("boom")

    daemon = Daemon(job, config, now=now, stop_event=stop)
    daemon.run(max_runs=2)
    assert daemon.runs == 2


def test_daemon_stops():
    config = parse_config({})
    stop = threading.Event()
    stop.set()
    daemon = Daemon(lambda: RunOutcome("dry-run", 0), config, stop_event=stop)
    daemon.run()
    assert daemon.runs == 0
