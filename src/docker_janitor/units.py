"""Durations (``7d``, ``12h``), sizes (``5GB``) and Docker timestamps."""

from __future__ import annotations

import re
from datetime import UTC, datetime

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(s|m|h|d|w)")
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "m": 1000**2,
    "mb": 1000**2,
    "g": 1000**3,
    "gb": 1000**3,
    "t": 1000**4,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def parse_duration(value: object, *, field: str = "duration") -> float:
    """Seconds from ``30s``, ``12h``, ``7d``, ``2w``, ``1d12h`` or a number of seconds."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field}: expected a duration such as '7d', got {value!r}")
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError(f"{field}: must not be negative")
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected a duration such as '7d', got {value!r}")
    text = value.strip().lower().replace(" ", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    pos, total = 0, 0.0
    for match in _DURATION_PART.finditer(text):
        if match.start() != pos:
            break
        total += float(match.group(1)) * _DURATION_UNITS[match.group(2)]
        pos = match.end()
    if not text or pos != len(text):
        raise ValueError(f"{field}: cannot parse duration {value!r} (use e.g. 12h, 7d, 2w)")
    return total


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{max(seconds, 0)}s"
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"  # pragma: no cover


def parse_size(value: object, *, field: str = "size") -> int:
    """Bytes from ``5GB``, ``512MiB``, ``1.5g`` or a number of bytes."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field}: expected a size such as '5GB', got {value!r}")
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError(f"{field}: must not be negative")
        return int(value)
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected a size such as '5GB', got {value!r}")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]*)", value.strip())
    if not match or match.group(2).lower() not in _SIZE_UNITS:
        raise ValueError(f"{field}: cannot parse size {value!r} (use e.g. 500MB, 5GB, 2GiB)")
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2).lower()])


def format_size(size: int | float | None) -> str:
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(value) < 1000 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1000
    return f"{value:.1f}TB"  # pragma: no cover


_FRACTION = re.compile(r"\.(\d+)")


def parse_timestamp(value: object) -> datetime | None:
    """Parse Docker's RFC 3339 timestamps (nanoseconds, ``Z`` or offsets) or Unix seconds.

    Returns ``None`` for missing values and Docker's zero time ``0001-01-01T00:00:00Z``.
    """
    if value is None or value == "":
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC) if value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("0001-01-01"):
        return None
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
