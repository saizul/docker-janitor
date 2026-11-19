from conftest import ago, api_error, sha
from docker_janitor.config import parse_config
from docker_janitor.planner import Planner, execute, labels_match


def plan_for(client, **raw):
    return Planner(client, parse_config(raw)).build()


def names(items, kind=None):
    return sorted(i.name for i in items if kind is None or i.kind == kind)


def kept_reason(plan, name):
    return next(i.reason for i in plan.keep if i.name == name)


def test_labels_match():
    assert labels_match({"janitor.keep": "true"}, ["janitor.keep=true"]) == "janitor.keep=true"
    assert labels_match({"janitor.keep": "TRUE"}, ["janitor.keep=true"]) == "janitor.keep=true"
    assert labels_match({"janitor.keep": "false"}, ["janitor.keep=true"]) is None
    assert labels_match({"tier": "db"}, ["tier"]) == "tier"
    assert labels_match(None, ["tier"]) is None


# Containers ------------------------------------------------------------------


def test_containers_by_status_and_age(client):
    client.add_container("old-exited", finished=ago(days=10))
    client.add_container("fresh-exited", finished=ago(days=1))
    client.add_container("old-created", status="created", created=ago(days=30))
    client.add_container("dead", status="dead", finished=ago(days=8))
    client.add_container("running", status="running", created=ago(days=400))
    client.add_container("paused", status="paused", created=ago(days=400))
    plan = plan_for(client)
    assert names(plan.remove, "container") == ["dead", "old-created", "old-exited"]
    assert "stopped 1d ago" in kept_reason(plan, "fresh-exited")
    assert "running" not in names(plan.keep) + names(plan.remove)


def test_containers_protected_by_label_name_and_restart_policy(client):
    client.add_container("keep-me", finished=ago(days=30), labels={"janitor.keep": "true"})
    client.add_container("db-backup", finished=ago(days=30))
    client.add_container("stopped-service", finished=ago(days=30), restart="unless-stopped")
    client.add_container("other", finished=ago(days=30), labels={"team": "infra"})
    plan = plan_for(client, protect={"names": ["db-*"], "labels": ["team=infra"]})
    assert names(plan.remove) == []
    assert kept_reason(plan, "keep-me") == "protected by label janitor.keep=true"
    assert kept_reason(plan, "db-backup") == "protected by name pattern db-*"
    assert kept_reason(plan, "stopped-service") == "has restart policy unless-stopped"
    assert kept_reason(plan, "other") == "protected by label team=infra"
    plan = plan_for(client, containers={"keep_with_restart_policy": False})
    assert "stopped-service" in names(plan.remove)


def test_only_labels_allowlist(client):
    client.add_container("ci-job", finished=ago(days=30), labels={"ci": "true"})
    client.add_container("hand-made", finished=ago(days=30))
    plan = plan_for(client, protect={"only_labels": ["ci=true"]})
    assert names(plan.remove) == ["ci-job"]
    assert kept_reason(plan, "hand-made") == "not selected by protect.only_labels"


# Images --------------------------------------------------------------------------


def test_dangling_images(client):
    client.add_image("old-dangling", created=ago(days=3))
    client.add_image("new-dangling", created=ago(hours=2))
    plan = plan_for(client)
    removed = [i for i in plan.remove if i.kind == "image"]
    assert [i.id for i in removed] == [sha("i-old-dangling")]
    assert removed[0].reason == "dangling for 3d"
    assert removed[0].size == 100_000_000


def test_keep_latest_tags_per_repository(client):
    for n in range(1, 6):
        client.add_image(f"app{n}", [f"registry.example.com:5000/app:{n}"], created=ago(days=200 - n))
    client.add_image("other", ["redis:6"], created=ago(days=400))
    plan = plan_for(client, images={"keep_latest": 2, "unused_older_than": "30d"})
    assert names(plan.remove, "image") == [
        "registry.example.com:5000/app:1",
        "registry.example.com:5000/app:2",
        "registry.example.com:5000/app:3",
    ]
    assert kept_reason(plan, "redis:6") == "one of the 2 newest of redis"
    assert kept_reason(plan, "registry.example.com:5000/app:5") == (
        "one of the 2 newest of registry.example.com:5000/app"
    )
    plan = plan_for(client, images={"keep_latest": 0})
    assert len(plan.by_kind("image")) == 6


def test_image_age_uses_last_tag_time(client):
    client.add_image("pulled", ["nginx:1.0"], created=ago(days=700), last_tag=ago(days=2))
    client.add_image("newer", ["nginx:1.1"], created=ago(days=600))
    plan = plan_for(client, images={"keep_latest": 0})
    assert names(plan.remove, "image") == ["nginx:1.1"]
    assert "age 2d" in kept_reason(plan, "nginx:1.0")


def test_images_in_use_are_kept_but_freed_by_removed_containers(client):
    used = client.add_image("used", ["web:1"], created=ago(days=100))
    freed = client.add_image("freed", ["job:1"], created=ago(days=100))
    client.add_image("job-newer", ["job:2"], created=ago(days=99))
    client.add_container("web", status="running", image=used.id)
    client.add_container("old-job", finished=ago(days=30), image=freed.id)
    plan = plan_for(client, images={"keep_latest": 0})
    assert "web:1" not in names(plan.remove)
    assert kept_reason(plan, "web:1") == "used by a container"
    assert {"job:1", "job:2"} <= set(names(plan.remove, "image"))


def test_image_protection_and_parents(client):
    client.add_image("base", ["base:1"], created=ago(days=100), labels={"janitor.keep": "true"})
    parent = client.add_image("parent", [], created=ago(days=100))
    client.add_image("child", [], created=ago(days=100), parent=parent.id)
    client.add_image("pg", ["postgres:13"], created=ago(days=100))
    plan = plan_for(client, images={"keep_latest": 0}, protect={"names": ["postgres:*"]})
    assert kept_reason(plan, "base:1") == "protected by label janitor.keep=true"
    assert kept_reason(plan, "postgres:13") == "protected by name pattern postgres:*"
    assert any(i.reason == "parent of another image" for i in plan.keep)
    assert [i.id for i in plan.remove] == [sha("i-child")]


def test_tagged_cleanup_can_be_disabled(client):
    client.add_image("old", ["x:1"], created=ago(days=400))
    client.add_image("dangling", [], created=ago(days=400))
    plan = plan_for(client, images={"unused_older_than": None, "keep_latest": 0})
    assert [i.id for i in plan.remove] == [sha("i-dangling")]
    plan = plan_for(client, images={"dangling_older_than": False, "keep_latest": 0})
    assert names(plan.remove) == ["x:1"]


# Networks and volumes ------------------------------------------------------------


def test_networks(client):
    for builtin in ("bridge", "host", "none"):
        client.add_network(builtin, driver=builtin if builtin != "none" else "null")
    client.add_network("old-unused")
    client.add_network("new-unused", created=ago(days=1))
    client.add_network("in-use")
    client.add_network("swarm-net", scope="swarm", driver="overlay")
    client.add_network("protected", labels={"janitor.keep": "true"})
    client.add_container("svc", status="running", networks=["in-use"])
    plan = plan_for(client)
    assert names(plan.remove, "network") == ["old-unused"]
    assert kept_reason(plan, "in-use") == "used by a container"
    assert "age 1d" in kept_reason(plan, "new-unused")
    assert "bridge" not in names(plan.keep)
    assert "swarm-net" not in names(plan.keep)


def test_volumes_are_off_by_default(client):
    client.add_volume("a" * 64)
    assert plan_for(client).by_kind("volume") == []


def test_volumes_anonymous_only_by_default(client):
    anon = "f" * 64
    client.add_volume(anon)
    client.add_volume("e" * 64, created=ago(days=2))
    client.add_volume("pgdata")
    client.add_volume("labelled-anon", labels={"com.docker.volume.anonymous": ""})
    client.add_volume("nfs", driver="nfs-plugin")
    client.add_volume("d" * 64)
    client.add_container("db", status="exited", finished=ago(days=1), volumes=["d" * 64])
    client.disk_usage["Volumes"] = [{"Name": anon, "UsageData": {"Size": 5000, "RefCount": 0}}]
    plan = plan_for(client, volumes={"enabled": True})
    removed = plan.by_kind("volume")
    assert sorted(v.name for v in removed) == sorted([anon, "labelled-anon"])
    assert next(v for v in removed if v.name == anon).size == 5000
    assert kept_reason(plan, "pgdata") == "named volume (volumes.anonymous_only is true)"
    assert kept_reason(plan, "nfs") == "driver nfs-plugin is not local"
    assert kept_reason(plan, "d" * 64) == "used by a container"
    plan = plan_for(client, volumes={"enabled": True, "anonymous_only": False})
    assert "pgdata" in names(plan.remove)


def test_volume_freed_by_removed_container(client):
    client.add_volume("c" * 64)
    client.add_container("old", finished=ago(days=30), volumes=["c" * 64])
    plan = plan_for(client, volumes={"enabled": True})
    assert names(plan.remove) == ["c" * 64, "old"]


# Build cache --------------------------------------------------------------------


def test_build_cache_estimate(client):
    client.disk_usage["BuildCache"] = [
        {"ID": "a", "Size": 1000, "InUse": False, "Shared": False, "LastUsedAt": ago(days=10)},
        {"ID": "b", "Size": 2000, "InUse": True, "Shared": False, "LastUsedAt": ago(days=10)},
        {"ID": "c", "Size": 4000, "InUse": False, "Shared": True, "LastUsedAt": ago(days=10)},
        {"ID": "d", "Size": 8000, "InUse": False, "Shared": False, "LastUsedAt": ago(days=1)},
    ]
    plan = plan_for(client, build_cache={"older_than": "7d"})
    bc = plan.build_cache
    assert (bc.estimated_entries, bc.estimated_size) == (1, 1000)
    assert bc.filters == {"until": "604800s"}
    plan = plan_for(client, build_cache={"older_than": "7d", "all": True, "keep_storage": "10kB"})
    assert (plan.build_cache.estimated_entries, plan.build_cache.estimated_size) == (2, 5000)
    assert plan_for(client, build_cache=False).build_cache is None


def test_disk_usage_errors_become_warnings(client):
    def broken():
        raise api_error("df is slow today", 500)

    client.df = broken
    plan = plan_for(client)
    assert plan.build_cache.estimated_size == 0
    assert plan.warnings == ["disk usage unavailable: df is slow today"]


def test_plan_to_dict(client):
    client.add_container("old", finished=ago(days=30))
    data = plan_for(client).to_dict()
    assert data["summary"]["containers"] == 1
    assert data["remove"][0]["name"] == "old"
    assert "obj" not in data["remove"][0]


# Execution ---------------------------------------------------------------------


def test_execute_removes_in_order_without_force(client):
    client.add_container("old", finished=ago(days=30))
    client.add_image("img", ["app:1", "app:latest"], created=ago(days=100))
    client.add_image("dangling", [], created=ago(days=100))
    client.add_network("net")
    client.add_volume("b" * 64)
    plan = plan_for(client, images={"keep_latest": 0}, volumes={"enabled": True})
    result = execute(client, plan)
    kinds = [kind for kind, _ in client.removed]
    assert kinds == ["container", "image", "image", "image", "network", "volume"]
    assert ("image", "app:1") in client.removed and ("image", "app:latest") in client.removed
    assert ("image", sha("i-dangling")) in client.removed
    for kind, _, kwargs in client.calls:
        assert kwargs.get("force", False) is False
        if kind == "container":
            assert kwargs == {"v": False, "force": False}
    assert len(result.removed) == 5
    assert result.build_cache_reclaimed == 1234
    assert client.api.prune_calls == [{"filters": {"until": "604800s"}, "keep_storage": None, "all": False}]
    assert result.reclaimed == 200_000_000 + 1234


def test_execute_reports_refusals_and_continues(client):
    import docker.errors

    client.add_container("raced", finished=ago(days=30))
    client.add_container("gone", finished=ago(days=30))
    client.add_network("net")
    client.errors["raced"] = api_error("container is running: stop the container before removing")
    client.errors["gone"] = docker.errors.NotFound("no such container")
    client.api.prune_error = api_error("builder busy", 500)
    plan = plan_for(client)
    result = execute(client, plan)
    assert [o.item.name for o in result.failed] == ["raced"]
    assert result.failed[0].error == "container is running: stop the container before removing"
    assert sorted(o.item.name for o in result.removed) == ["gone", "net"]
    assert result.build_cache_error == "builder busy"
    assert result.to_dict()["failed"][0]["ok"] is False
