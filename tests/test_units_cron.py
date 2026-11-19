from datetime import UTC, datetime

import pytest

from docker_janitor.cron import Cron, CronError
from docker_janitor.units import format_duration, format_size, parse_duration, parse_size, parse_timestamp


@pytest.mark.parametrize(("text", "seconds"), [("30s", 30), ("12h", 43200), ("7d", 604800), ("2w", 1209600), (90, 90)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "7 days", "d7", None, True, -1])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


@pytest.mark.parametrize(
    ("text", "size"), [("5GB", 5_000_000_000), ("512MiB", 536870912), ("1.5g", 1_500_000_000), (100, 100), ("10", 10)]
)
def test_parse_size(text, size):
    assert parse_size(text) == size


@pytest.mark.parametrize("bad", ["lots", "5XB", -1, None])
def test_parse_size_rejects(bad):
    with pytest.raises(ValueError):
        parse_size(bad)


def test_formatting():
    assert format_size(None) == "-"
    assert format_size(999) == "999B"
    assert format_size(1_500_000) == "1.5MB"
    assert format_size(2_000_000_000) == "2.0GB"
    assert format_duration(None) == "-"
    assert format_duration(59) == "59s"
    assert format_duration(3 * 86400 + 5) == "3d"
    assert format_duration(15 * 86400) == "2w"


def test_parse_timestamp():
    ts = parse_timestamp("2026-01-02T03:04:05.123456789Z")
    assert ts == datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    assert parse_timestamp("2026-01-02T05:04:05+02:00") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert parse_timestamp("2026-01-02T03:04:05") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert parse_timestamp("0001-01-01T00:00:00Z") is None
    assert parse_timestamp("") is None
    assert parse_timestamp("garbage") is None
    assert parse_timestamp(1_700_000_000) == datetime.fromtimestamp(1_700_000_000, UTC)


@pytest.mark.parametrize(
    ("expr", "after", "expected"),
    [
        ("0 4 * * *", datetime(2026, 11, 19, 3, 59), datetime(2026, 11, 19, 4, 0)),
        ("0 4 * * *", datetime(2026, 11, 19, 4, 0), datetime(2026, 11, 20, 4, 0)),
        ("*/15 * * * *", datetime(2026, 11, 19, 10, 7, 30), datetime(2026, 11, 19, 10, 15)),
        ("@hourly", datetime(2026, 11, 19, 10, 7), datetime(2026, 11, 19, 11, 0)),
        ("@daily", datetime(2026, 12, 31, 23, 59), datetime(2027, 1, 1, 0, 0)),
        ("30 2 * * sun", datetime(2026, 11, 19, 0, 0), datetime(2026, 11, 22, 2, 30)),  # 2026-11-19 is a Thursday
        ("0 3 * * 7", datetime(2026, 11, 19, 0, 0), datetime(2026, 11, 22, 3, 0)),
        ("0 0 1 */3 *", datetime(2026, 11, 19), datetime(2027, 1, 1)),
        ("0 9 * * mon-fri", datetime(2026, 11, 21, 12, 0), datetime(2026, 11, 23, 9, 0)),
        ("0 0 13 * fri", datetime(2026, 11, 1), datetime(2026, 11, 6)),  # day 13 OR any Friday
        ("0 0 29 2 *", datetime(2026, 3, 1), datetime(2028, 2, 29)),
        ("0 12 * * 5-7", datetime(2026, 11, 19, 13, 0), datetime(2026, 11, 20, 12, 0)),
        ("5,35 1-2 * jan,nov *", datetime(2026, 11, 19, 1, 10), datetime(2026, 11, 19, 1, 35)),
    ],
)
def test_cron_next_after(expr, after, expected):
    assert Cron.parse(expr).next_after(after) == expected


@pytest.mark.parametrize(
    "bad",
    ["* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "5-1 * * * *", "a * * * *", "0 0 31 2 *", "1,,2 * * * *"],
)
def test_cron_rejects(bad):
    with pytest.raises(CronError):
        Cron.parse(bad).next_after(datetime(2026, 1, 1))
