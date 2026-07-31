"""Dayparting: channels whose identity changes with the clock.

Real cable never showed the same thing at 3pm and 3am. A :class:`Daypart` is a
wall-clock window on a single channel that can swap in a different name, a
different folder of episodes, or take the channel off the air entirely (colour
bars and a dead carrier, the way stations used to sign off).

Windows are expressed in minutes since local midnight and may wrap past
midnight - ``22:00`` to ``04:00`` is the obvious late-night case, and is the
whole reason this module exists. Everything here is pure and clock-injectable
so the scheduling logic can be unit-tested without waiting for 2am.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

MINUTES_PER_DAY = 24 * 60


class DaypartError(ValueError):
    """Raised when a daypart window cannot be parsed."""


def parse_clock(value: object, *, field: str = "time") -> int:
    """Parse a wall-clock time into minutes since midnight.

    Accepts ``"22:00"``, ``"9:30"``, ``"2200"``, and a bare hour (``22`` or
    ``"22"``). ``"24:00"`` is allowed as an end-of-day marker and resolves to
    1440. Anything else raises :class:`DaypartError`.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise DaypartError(f"{field} must be a clock time like '22:00', got {value!r}")

    if isinstance(value, int):
        hours, minutes = value, 0
    else:
        text = str(value).strip()
        if not text:
            raise DaypartError(f"{field} must be a clock time like '22:00', got {value!r}")
        if ":" in text:
            hh, _, mm = text.partition(":")
            # Tolerate "22:00:00" by ignoring anything past the minutes.
            mm = mm.split(":", 1)[0]
        elif text.isdigit() and len(text) == 4:
            hh, mm = text[:2], text[2:]
        else:
            hh, mm = text, "0"
        try:
            hours, minutes = int(hh), int(mm)
        except ValueError as exc:
            raise DaypartError(
                f"{field} must be a clock time like '22:00', got {value!r}"
            ) from exc

    total = hours * 60 + minutes
    if not 0 <= total <= MINUTES_PER_DAY:
        raise DaypartError(f"{field} must be between 00:00 and 24:00, got {value!r}")
    return total


def format_clock(minute: int) -> str:
    """Render minutes-since-midnight back as ``HH:MM`` (1440 renders as 24:00)."""
    minute = max(0, min(MINUTES_PER_DAY, int(minute)))
    return f"{minute // 60:02d}:{minute % 60:02d}"


@dataclass(frozen=True)
class Daypart:
    """One wall-clock window on a channel.

    ``start`` is inclusive and ``end`` is exclusive. When ``end`` is less than
    or equal to ``start`` the window wraps past midnight, so ``22:00``-``04:00``
    covers the six hours you would actually be awake for. A window whose start
    and end are equal covers the whole day.
    """

    start: int
    end: int
    name: Optional[str] = None
    path: Optional[Path] = None
    off_air: bool = False

    @property
    def wraps(self) -> bool:
        """True when this window runs past midnight into the next day."""
        return self.end <= self.start

    @property
    def label(self) -> str:
        return f"{format_clock(self.start)}-{format_clock(self.end)}"

    def contains(self, minute: int) -> bool:
        """Is ``minute`` (minutes since local midnight) inside this window?"""
        if self.wraps:
            return minute >= self.start or minute < self.end
        return self.start <= minute < self.end

    def minutes_until_end(self, minute: int) -> int:
        """How many minutes remain in this window from ``minute``."""
        if not self.contains(minute):
            return 0
        remaining = self.end - minute
        if remaining <= 0:
            remaining += MINUTES_PER_DAY
        return remaining


def minutes_since_midnight(epoch: Optional[float] = None) -> int:
    """Local-time minutes since midnight for a POSIX timestamp (default: now)."""
    local = time.localtime(time.time() if epoch is None else epoch)
    return local.tm_hour * 60 + local.tm_min


def active_daypart(
    dayparts: Sequence[Daypart], epoch: Optional[float] = None
) -> Optional[Daypart]:
    """Return the first daypart covering ``epoch``, or ``None`` for the default.

    Order matters: dayparts are tested in the order they were configured, so an
    earlier window wins any overlap. That keeps a hand-written config
    predictable instead of silently merging overlapping windows.
    """
    if not dayparts:
        return None
    minute = minutes_since_midnight(epoch)
    for part in dayparts:
        if part.contains(minute):
            return part
    return None


__all__ = [
    "Daypart",
    "DaypartError",
    "MINUTES_PER_DAY",
    "active_daypart",
    "format_clock",
    "minutes_since_midnight",
    "parse_clock",
]
