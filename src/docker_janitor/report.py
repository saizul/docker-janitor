"""Human-readable tables for plans, results and disk usage."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from docker_janitor.planner import KINDS, Plan, Result
from docker_janitor.units import format_duration, format_size


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(h) for h in headers], *[[str(c) for c in row] for row in rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _truncate(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_plan(plan: Plan, *, dry_run: bool, show_kept: bool = False) -> str:
    out: list[str] = []
    verb = "Would remove" if dry_run else "Removing"
    if plan.remove:
        rows = [
            (
                item.kind,
                item.short_id,
                _truncate(item.name),
                format_duration(item.age),
                format_size(item.size),
                item.reason,
            )
            for item in plan.remove
        ]
        out.append(f"{verb} {len(plan.remove)} object(s):")
        out.append(table(("KIND", "ID", "NAME", "AGE", "SIZE", "REASON"), rows))
    else:
        out.append("Nothing to remove: every object is in use, protected or too new.")
    if plan.build_cache is not None:
        bc = plan.build_cache
        keep = f", keep {format_size(bc.keep_storage)}" if bc.keep_storage is not None else ""
        out.append(
            f"\nBuild cache: {'would prune' if dry_run else 'pruning'} entries unused for "
            f"{format_duration(bc.older_than)}{keep}{' (all)' if bc.all else ''}; "
            f"estimate {bc.estimated_entries} entr(ies), up to {format_size(bc.estimated_size)}"
        )
    if show_kept and plan.keep:
        rows = [(i.kind, i.short_id, _truncate(i.name), format_duration(i.age), i.reason) for i in plan.keep]
        out.append(f"\nKeeping {len(plan.keep)} object(s):")
        out.append(table(("KIND", "ID", "NAME", "AGE", "WHY KEPT"), rows))
    elif plan.keep:
        out.append(f"\n{len(plan.keep)} object(s) kept (use --show-kept to list them with the reason).")
    counts = ", ".join(f"{len(plan.by_kind(k))} {k}(s)" for k in KINDS)
    out.append(f"\nSummary: {counts}; up to {format_size(plan.estimated_size)} reclaimable.")
    out.extend(f"Warning: {w}" for w in plan.warnings)
    return "\n".join(out)


def format_result(result: Result) -> str:
    out = [f"Removed {len(result.removed)} object(s), reclaimed about {format_size(result.reclaimed)}."]
    if result.build_cache_reclaimed is not None:
        out.append(f"Build cache: reclaimed {format_size(result.build_cache_reclaimed)}.")
    if result.build_cache_error:
        out.append(f"Build cache prune failed: {result.build_cache_error}")
    if result.failed:
        out.append(f"{len(result.failed)} removal(s) failed (Docker refused; nothing was forced):")
        out.extend(f"  {o.item.kind} {o.item.name}: {o.error}" for o in result.failed)
    return "\n".join(out)


def disk_usage(df: dict[str, Any]) -> list[dict[str, Any]]:
    """Summarise ``client.df()`` like ``docker system df``."""
    images = df.get("Images") or []
    containers = df.get("Containers") or []
    volumes = df.get("Volumes") or []
    cache = df.get("BuildCache") or []
    image_total = int(df.get("LayersSize") or sum(int(i.get("Size") or 0) for i in images))
    image_active = [i for i in images if int(i.get("Containers") or 0) > 0]
    active_size = sum(int(i.get("Size") or 0) - int(i.get("SharedSize") or 0) for i in image_active)
    shared = max((int(i.get("SharedSize") or 0) for i in images), default=0)
    image_reclaim = max(0, image_total - active_size - (shared if image_active else 0))
    running = [c for c in containers if str(c.get("State", "")).lower() == "running"]
    container_size = sum(int(c.get("SizeRw") or 0) for c in containers)
    container_reclaim = sum(int(c.get("SizeRw") or 0) for c in containers if c not in running)

    def vsize(v: dict[str, Any]) -> int:
        return max(0, int((v.get("UsageData") or {}).get("Size") or 0))

    def vrefs(v: dict[str, Any]) -> int:
        return int((v.get("UsageData") or {}).get("RefCount") or 0)

    volume_size = sum(vsize(v) for v in volumes)
    volume_reclaim = sum(vsize(v) for v in volumes if vrefs(v) == 0)
    cache_size = sum(int(c.get("Size") or 0) for c in cache if not c.get("Shared"))
    cache_reclaim = sum(int(c.get("Size") or 0) for c in cache if not c.get("InUse") and not c.get("Shared"))
    return [
        {"type": "Images", "total": len(images), "active": len(image_active), "size": image_total,
         "reclaimable": image_reclaim},
        {"type": "Containers", "total": len(containers), "active": len(running), "size": container_size,
         "reclaimable": container_reclaim},
        {"type": "Local Volumes", "total": len(volumes), "active": sum(1 for v in volumes if vrefs(v) > 0),
         "size": volume_size, "reclaimable": volume_reclaim},
        {"type": "Build Cache", "total": len(cache), "active": sum(1 for c in cache if c.get("InUse")),
         "size": cache_size, "reclaimable": cache_reclaim},
    ]  # fmt: skip


def format_disk_usage(rows: list[dict[str, Any]]) -> str:
    def pct(row: dict[str, Any]) -> str:
        return (
            f"{format_size(row['reclaimable'])} ({row['reclaimable'] * 100 // row['size']}%)" if row["size"] else "0B"
        )

    return table(
        ("TYPE", "TOTAL", "ACTIVE", "SIZE", "RECLAIMABLE"),
        [(r["type"], r["total"], r["active"], format_size(r["size"]), pct(r)) for r in rows],
    )
