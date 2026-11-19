"""A small, dependency-free cron expression parser (5 fields, Vixie cron semantics)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
_MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
MONTHS = {m: i for i, m in enumerate(_MONTH_NAMES, start=1)}
DAYS = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}


class CronError(ValueError):
    """Invalid cron expression."""


def _value(text: str, names: dict[str, int], lo: int, hi: int, field: str) -> int:
    key = text.lower()
    if key in names:
        return names[key]
    if not text.isdigit():
        raise CronError(f"{field}: {text!r} is not a number")
    number = int(text)
    if field == "day of week" and number == 7:
        number = 0
    if not lo <= number <= hi:
        raise CronError(f"{field}: {number} is outside {lo}-{hi}")
    return number


def _field(spec: str, lo: int, hi: int, field: str, names: dict[str, int] | None = None) -> frozenset[int]:
    names = names or {}
    values: set[int] = set()
    for part in spec.split(","):
        if not part:
            raise CronError(f"{field}: empty list item")
        rng, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronError(f"{field}: invalid step {step_text!r}")
            step = int(step_text)
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = _value(a, names, lo, hi, field), _value(b, names, lo, hi, field)
            if field == "day of week" and b == "7":  # "5-7" means Friday to Sunday
                end = 6
                values.add(0)
            if start > end:
                raise CronError(f"{field}: range {rng!r} is backwards")
        else:
            start = _value(rng, names, lo, hi, field)
            end = hi if step_text else start
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> Cron:
        text = " ".join(str(expression).split())
        expanded = ALIASES.get(text.lower(), text)
        parts = expanded.split(" ")
        if len(parts) != 5:
            raise CronError(f"cron expression {expression!r} needs 5 fields: minute hour day month weekday")
        minute, hour, dom, month, dow = parts
        return cls(
            expression=text,
            minutes=_field(minute, 0, 59, "minute"),
            hours=_field(hour, 0, 23, "hour"),
            days=_field(dom, 1, 31, "day of month"),
            months=_field(month, 1, 12, "month", MONTHS),
            weekdays=_field(dow, 0, 6, "day of week", DAYS),
            dom_restricted=dom != "*",
            dow_restricted=dow != "*",
        )

    def _day_matches(self, moment: datetime) -> bool:
        weekday = (moment.weekday() + 1) % 7  # Python: Monday=0; cron: Sunday=0
        dom_ok = moment.day in self.days
        dow_ok = weekday in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, moment: datetime) -> datetime:
        """The first matching minute strictly after ``moment`` (keeps ``moment``'s tzinfo)."""
        current = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = current + timedelta(days=366 * 5)
        while current < limit:
            if current.month not in self.months:
                year, month = (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
                current = current.replace(year=year, month=month, day=1, hour=0, minute=0)
                continue
            if not self._day_matches(current):
                current = (current + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if current.hour not in self.hours:
                current = (current + timedelta(hours=1)).replace(minute=0)
                continue
            if current.minute not in self.minutes:
                current += timedelta(minutes=1)
                continue
            return current
        raise CronError(f"cron expression {self.expression!r} never matches")
