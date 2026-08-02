"""Making dayparting editable by somebody who has never seen the config file.

:mod:`retrobox.daypart` already does all of the work. This module changes none
of it - it is the layer between that engine and a visual editor, and it does
three things the engine deliberately does not.

**It refuses overlaps.** The engine resolves an overlap by order, first window
wins, which is exactly right for a hand-written file: it is predictable and it
never silently merges anything. In a visual editor it is exactly wrong -
somebody who has dragged two blocks over each other has made a mistake, not
expressed a preference. So the editor rejects overlaps and says which two.
A hand-written config that already has one keeps working unchanged, and the
preview below still agrees with what the engine does with it.

**It refuses a whole-day block.** ``start == end`` means "covers everything" to
the engine. In an editor it is a block that swallows the rest of the day, which
is never what anybody drew.

**It lays the day out flat.** A window that wraps past midnight is one block to
the engine and two bars on a timeline, and the gaps between blocks are as
important to see as the blocks - that is when the channel is simply itself.

The preview is built *on top of* :func:`~retrobox.daypart.active_daypart`
rather than beside it, so it cannot drift from what the television does. The
test suite still checks the two against each other minute by minute, because
the arithmetic that turns "11:30" into a timestamp is the part that can be
wrong.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .daypart import (
    MINUTES_PER_DAY,
    Daypart,
    DaypartError,
    active_daypart,
    clock_report,
    format_clock,
    parse_clock,
)

#: More than this on one channel is somebody fighting the editor rather than
#: scheduling a day. The engine has no limit; this is a UI sanity bound.
MAX_BLOCKS = 24


class ScheduleError(ValueError):
    """A schedule the editor refuses, with something worth showing a person."""


def _clock(value: Any, *, field: str) -> int:
    """``parse_clock``, plus the one thing an editor has to be stricter about.

    The engine accepts "6:70" and resolves it to 07:10, which is reasonable
    for a hand-written file - it is only doing arithmetic. Somebody typing
    into a time field has made a typo, and turning it into a different time
    without saying so is how a schedule ends up quietly wrong. The engine is
    unchanged; this is the editor being fussier at the door.
    """
    if isinstance(value, str) and ":" in value:
        minutes = value.split(":", 1)[1].split(":", 1)[0].strip()
        if minutes.isdigit() and int(minutes) > 59:
            raise ScheduleError(
                f"{field} is not a time - there is no minute {int(minutes)}"
            )
    try:
        return parse_clock(value, field=field)
    except DaypartError as exc:
        raise ScheduleError(str(exc)) from None


def _one(entry: Any, index: int) -> Daypart:
    where = f"block {index + 1}"
    if not isinstance(entry, dict):
        raise ScheduleError(f"{where} is not a time block")

    start = _clock(entry.get("from"), field=f"{where} start")
    end = _clock(entry.get("to"), field=f"{where} end")

    if start == end:
        raise ScheduleError(
            f"{where} starts and ends at the same time. A block from "
            f"{format_clock(start)} to {format_clock(end)} would cover the whole "
            f"day and hide everything else."
        )

    off_air = bool(entry.get("off_air"))
    raw_path = entry.get("path")
    path = Path(str(raw_path)) if raw_path else None
    if off_air and path is not None:
        raise ScheduleError(
            f"{where} is both off air and pointed at a folder - it can be one "
            f"or the other"
        )

    name = entry.get("name")
    return Daypart(
        start=start % MINUTES_PER_DAY,
        end=end if end == MINUTES_PER_DAY else end % MINUTES_PER_DAY,
        name=str(name) if name else None,
        path=path,
        off_air=off_air,
    )


def _spans(part: Daypart) -> List[Any]:
    """A window as one or two flat (start, end) spans on a single day."""
    if part.wraps:
        return [(part.start, MINUTES_PER_DAY), (0, part.end)]
    return [(part.start, part.end)]


def validate(entries: Sequence[Any]) -> List[Daypart]:
    """Turn raw blocks into dayparts, or refuse the whole schedule."""
    if entries is None:
        return []
    if not isinstance(entries, (list, tuple)):
        raise ScheduleError("a schedule is a list of time blocks")
    if len(entries) > MAX_BLOCKS:
        raise ScheduleError(
            f"that is {len(entries)} blocks on one channel; {MAX_BLOCKS} is the most"
        )

    parts = [_one(entry, index) for index, entry in enumerate(entries)]

    # Flattened to spans first, so a window that wraps past midnight is
    # compared against the early morning as well as the late evening.
    for i, first in enumerate(parts):
        for j, second in enumerate(parts[i + 1:], start=i + 1):
            for a_start, a_end in _spans(first):
                for b_start, b_end in _spans(second):
                    if a_start < b_end and b_start < a_end:
                        raise ScheduleError(
                            f"blocks {i + 1} ({first.label}) and {j + 1} "
                            f"({second.label}) overlap. Two blocks cannot both be "
                            f"on at once - shorten one of them."
                        )
    return parts


def blocks_from_config(raw: Any) -> List[Daypart]:
    """The same thing, for what is already in config.yaml.

    Deliberately does NOT reject overlaps: a hand-written file may have one and
    the engine copes with it. The editor refuses to *create* them; it does not
    refuse to display what is already there.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    parts = []
    for index, entry in enumerate(raw):
        try:
            parts.append(_one(entry, index))
        except ScheduleError:
            continue           # a block we cannot read is not a block we show
    return parts


def to_config(parts: Sequence[Daypart]) -> List[Dict[str, Any]]:
    """Back into the shape config.yaml holds."""
    out = []
    for part in parts:
        entry: Dict[str, Any] = {
            "from": format_clock(part.start),
            "to": format_clock(part.end),
        }
        if part.name:
            entry["name"] = part.name
        if part.path is not None:
            entry["path"] = str(part.path)
        if part.off_air:
            entry["off_air"] = True
        out.append(entry)
    return out


# ==========================================================================
# The day, laid out
# ==========================================================================
def day_view(entries: Sequence[Any]) -> List[Dict[str, Any]]:
    """Midnight to midnight as a flat list of bars, gaps included.

    A wrapping window appears twice - once at each end of the day - because
    that is what it looks like on a timeline, and the alternative is a bar
    that runs off the right-hand side and reappears on the left.
    """
    parts = (
        entries if entries and isinstance(entries[0], Daypart)
        else blocks_from_config(entries)
    )

    bars: List[Dict[str, Any]] = []
    for part in parts:
        for start, end in _spans(part):
            bars.append({
                "kind": "block", "start": start, "end": end,
                "minutes": end - start,
                "name": part.name, "off_air": part.off_air,
                "path": str(part.path) if part.path else None,
                "label": part.label,
            })
    bars.sort(key=lambda b: b["start"])

    out: List[Dict[str, Any]] = []
    cursor = 0
    for bar in bars:
        if bar["start"] > cursor:
            out.append(_gap(cursor, bar["start"]))
        out.append(bar)
        cursor = max(cursor, bar["end"])
    if cursor < MINUTES_PER_DAY:
        out.append(_gap(cursor, MINUTES_PER_DAY))
    return out


def _gap(start: int, end: int) -> Dict[str, Any]:
    return {
        "kind": "gap", "start": start, "end": end, "minutes": end - start,
        "label": "the channel runs as usual all day" if end - start == MINUTES_PER_DAY
                 else "the channel runs as usual",
        "name": None, "off_air": False, "path": None,
    }


# ==========================================================================
# What is on at a given minute
# ==========================================================================
def _epoch_for(minute: int) -> float:
    """A timestamp whose LOCAL clock reads ``minute`` minutes past midnight.

    Built by asking the C library rather than by arithmetic on a Unix time,
    because minutes-since-midnight is a *local* idea and the offset between
    that and UTC is not constant - it changes twice a year.
    """
    parts = list(time.localtime())
    parts[3] = (minute // 60) % 24
    parts[4] = minute % 60
    parts[5] = 0
    parts[8] = -1                      # let mktime work out DST for us
    return time.mktime(tuple(parts))


def preview_at(parts: Sequence[Daypart], minute: int) -> Dict[str, Any]:
    """What this channel would be at ``minute``, straight from the engine."""
    minute = int(minute) % MINUTES_PER_DAY
    # ``clock_trust=True`` because this is a hypothetical, not a reading. The
    # engine pauses dayparting when it cannot vouch for the box's clock, which
    # is right for the television and wrong here: the editor is asking "what
    # would be on at 3am", and a box with a broken clock is exactly the box
    # whose owner needs that screen to keep working.
    part = active_daypart(list(parts), _epoch_for(minute), clock_trust=True)
    if part is None:
        return {
            "active": False, "at": format_clock(minute), "name": None,
            "off_air": False, "path": None, "label": None, "minutes_left": None,
            "summary": "Nothing scheduled - the channel is simply itself.",
        }

    left = part.minutes_until_end(minute)
    if part.off_air:
        summary = f"Off air until {format_clock(part.end)}."
    else:
        summary = f"{part.name or 'This block'} until {format_clock(part.end)}."
    return {
        "active": True,
        "at": format_clock(minute),
        "name": part.name,
        "off_air": part.off_air,
        "path": str(part.path) if part.path else None,
        "label": part.label,
        "minutes_left": left,
        "summary": summary,
    }


def now_minute() -> int:
    """Minutes since local midnight, for "what is on right now"."""
    local = time.localtime()
    return local.tm_hour * 60 + local.tm_min


def clock_note(epoch: Optional[float] = None) -> Dict[str, Any]:
    """Whether the schedule above this is actually being applied to the box.

    The timeline can be perfect and the television can be ignoring all of it,
    because the engine pauses dayparting when the clock cannot be believed. A
    schedule editor that cannot say so is the silent failure all over again -
    somebody would drag blocks around for an hour wondering why nothing
    changed at 10pm.

    Thin on purpose: :func:`retrobox.daypart.clock_report` is the real answer,
    and this exists so the editor does not have to reach past the module it is
    already talking to.
    """
    return clock_report(epoch)


__all__ = [
    "MAX_BLOCKS",
    "ScheduleError",
    "blocks_from_config",
    "clock_note",
    "day_view",
    "now_minute",
    "preview_at",
    "to_config",
    "validate",
]
