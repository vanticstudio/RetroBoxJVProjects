"""Dayparting: channels whose identity changes with the clock.

Real cable never showed the same thing at 3pm and 3am. A :class:`Daypart` is a
wall-clock window on a single channel that can swap in a different name, a
different folder of episodes, or take the channel off the air entirely (colour
bars and a dead carrier, the way stations used to sign off).

Windows are expressed in minutes since local midnight and may wrap past
midnight - ``22:00`` to ``04:00`` is the obvious late-night case, and is the
whole reason this module exists. Everything here is pure and clock-injectable
so the scheduling logic can be unit-tested without waiting for 2am.

**Why this module cares whether the clock is right.** Everything else on the
box either works or visibly does not. Dayparting is the exception: given a
wrong clock it raises nothing, logs nothing and plays the 3am block at
teatime, so the owner concludes the feature is broken rather than that the
clock is. And the hardware makes that likely - a flat CMOS battery is one of
the commonest faults on a ten-year-old office mini PC, and a box with one
comes up with an absurd clock every single time it is switched off at the
wall.

So the decision below can be *paused*. When the clock cannot be vouched for,
no daypart is applied and the channel plays as itself - its own name, its own
folder, which is what it is outside every window anyway. That is the only
fallback that is guaranteed to have something to play and the only one that
cannot be confidently wrong, and it deliberately never signs a channel off:
the worst thing a bad clock could do is put up colour bars nobody can explain.

The pause is reported rather than silent (:func:`resolve`, :func:`clock_report`)
because silence is the original bug.

The signal itself is *handed in*, never imported: whatever works out that the
clock is untrustworthy has to talk to the network, and this module has to stay
headless, instant and unit-testable. See :func:`set_clock_trust`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Union

log = logging.getLogger(__name__)

MINUTES_PER_DAY = 24 * 60

#: No RetroBox can be running before this, so a clock reading earlier than it
#: is not a clock that drifted - it is a clock that was never set. Deliberately
#: generous: it only has to sit above the dates a dead battery actually
#: produces (1970, 1980, 2000, and the BIOS build date of a 2010s board) and
#: below any date this software could really be running at. It is a floor, not
#: a precise test - the positive "never synchronised" signal handed in from
#: outside is what catches a clock that is merely a few years out.
IMPLAUSIBLE_BEFORE = 1_577_836_800          # 2020-01-01T00:00:00Z

#: And the same at the other end, for a battery that fails high.
IMPLAUSIBLE_AFTER = 4_102_444_800           # 2100-01-01T00:00:00Z


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


# ==========================================================================
# Whether the clock can be vouched for
# ==========================================================================
@dataclass(frozen=True)
class ClockVerdict:
    """Can the wall clock be believed, and if not, what does the owner do?

    ``reason`` is one sentence for the top of a page. ``detail`` is the bit
    that turns a complaint into a repair - on this hardware that is very often
    "the motherboard battery is flat", which costs two dollars and which
    nobody could possibly guess on their own.
    """

    trusted: bool
    reason: str = ""
    detail: str = ""


#: What a caller may hand in as the clock-trust signal. ``None`` means "no
#: opinion, carry on"; a bool is the answer outright; a callable is asked for
#: one of those (or for anything with a ``.trusted`` attribute) at the moment
#: the decision is made.
ClockTrust = Union[None, bool, Callable[[], Any], ClockVerdict]

_TRUSTED = ClockVerdict(trusted=True)

#: The box-wide signal, installed once at start-up. Left as ``None`` - and so
#: behaving exactly as this module always has - until something installs one.
_clock_trust: ClockTrust = None

#: The last reason announced, so a decision made several times a second does
#: not fill the journal with the same line. ``None`` means nothing announced.
_announced: Optional[str] = None


def set_clock_trust(source: ClockTrust) -> None:
    """Install the box-wide clock-trust signal.

    ``source`` is normally a zero-argument callable returning a
    :class:`ClockVerdict` (or a plain bool, or ``None`` for "don't know").

    It is called on the playback path, so it must be **cheap and
    non-blocking** - a cached answer that some background thread refreshes,
    never a lookup that goes near the network. Nothing here may ever be the
    reason a box stops playing television.

    Installing a source also clears the memory of what has already been
    announced, because a new source is a new opinion and deserves to be heard.
    """
    global _clock_trust, _announced
    _clock_trust = source
    _announced = None


def clock_trust_source() -> ClockTrust:
    """Whatever :func:`set_clock_trust` last installed."""
    return _clock_trust


def _ask(source: ClockTrust) -> ClockVerdict:
    """Turn whatever a caller handed in into a verdict, forgivingly."""
    if source is None:
        return _TRUSTED
    if source is True or source is False:
        return _TRUSTED if source else ClockVerdict(
            False, "The clock on this box is not being trusted right now."
        )

    if callable(source):
        try:
            answer = source()
        except Exception:
            # A broken clock-checker is not evidence that the clock is broken,
            # and it is certainly not a reason to stop dayparting.
            log.debug("clock trust source raised; carrying on", exc_info=True)
            return _TRUSTED
    else:
        answer = source

    if answer is None:
        # No opinion yet. Most boxes are offline and will never have one, so
        # "don't know" has to mean "carry on" - degrading on ignorance would
        # break dayparting for the majority to protect the few.
        return _TRUSTED
    if answer is True or answer is False:
        return _TRUSTED if answer else ClockVerdict(
            False, "The clock on this box is not being trusted right now."
        )

    # A dict as readily as an object: whatever works out the clock's health
    # reports in dictionaries, because that is what a dashboard renders, and it
    # should not have to build an object on the way past just to be understood.
    if isinstance(answer, Mapping):
        def field(name: str, fallback: Any) -> Any:
            return answer.get(name, fallback)
    else:
        def field(name: str, fallback: Any) -> Any:
            return getattr(answer, name, fallback)

    # Missing, unreadable, or an explicit "cannot tell" all mean "carry on".
    # Dayparting stopping is the expensive outcome, so it has to be something's
    # deliberate "no" - never a field this module failed to find, and never a
    # box that simply has no way of knowing (no timedatectl, or chrony rather
    # than timesyncd). Reading that silence as "the clock is wrong" would
    # switch dayparting off on boxes whose clocks are perfectly fine.
    verdict = field("trusted", True)
    if verdict is None or bool(verdict):
        return _TRUSTED
    return ClockVerdict(
        False,
        str(field("reason", "") or
            "The clock on this box is not being trusted right now."),
        str(field("detail", "") or ""),
    )


def _believed_date(epoch: float) -> str:
    """What the box thinks the date is, or a phrase saying it cannot even say."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
    except (OverflowError, OSError, ValueError):
        return ""


def _plausible(epoch: float) -> ClockVerdict:
    """The check this module can make on its own, with no help and no network."""
    try:
        # A NaN compares false against everything, so it fails this on its own.
        out_of_range = not (IMPLAUSIBLE_BEFORE <= float(epoch) <= IMPLAUSIBLE_AFTER)
    except (TypeError, ValueError, OverflowError):
        out_of_range = True
    if not out_of_range:
        return _TRUSTED

    believed = _believed_date(epoch)
    said = f"thinks it is {believed}" if believed else "cannot say what the date is"
    return ClockVerdict(
        False,
        f"The clock is wrong: this box {said}, which cannot be right, so "
        f"channels that change through the day are running as themselves "
        f"instead.",
        "A box whose clock resets like this every time it is switched off "
        "almost always has a flat CMOS battery on the motherboard - a "
        "two-dollar coin cell. Replacing it, or leaving the box on a network "
        "so it can fetch the time, fixes this.",
    )


def clock_verdict(
    epoch: Optional[float] = None, *, clock_trust: ClockTrust = None
) -> ClockVerdict:
    """Can ``epoch`` be used to decide what is on air?

    ``clock_trust=True`` is an explicit override meaning "trust this
    timestamp": the schedule editor uses it to ask hypotheticals ("what would
    be on at 3am?"), which must still be answerable on a box whose own clock
    is broken.
    """
    if clock_trust is True:
        return _TRUSTED

    epoch = time.time() if epoch is None else epoch

    # The plausibility floor comes first and wins. It is not an opinion about
    # the box, it is a fact about the number we are one line away from using.
    verdict = _plausible(epoch)
    if not verdict.trusted:
        return verdict

    return _ask(_clock_trust if clock_trust is None else clock_trust)


# ==========================================================================
# The decision
# ==========================================================================
@dataclass(frozen=True)
class DaypartDecision:
    """What is on air, and - just as importantly - why it is not.

    Handed back whole so that something can answer "why is this channel not
    changing at 10pm?" without guessing. A decision with ``clock_trusted``
    false is a decision that was never made: no window was tested, because the
    clock feeding them could not be believed.
    """

    part: Optional[Daypart]
    index: Optional[int] = None
    minute: Optional[int] = None
    clock_trusted: bool = True
    reason: str = ""
    detail: str = ""

    @property
    def dayparting(self) -> bool:
        """Is the schedule being applied at all right now?"""
        return self.clock_trusted


def resolve(
    dayparts: Sequence[Daypart],
    epoch: Optional[float] = None,
    *,
    clock_trust: ClockTrust = None,
) -> DaypartDecision:
    """Work out which daypart is on air, or say why none is being applied.

    Order matters: dayparts are tested in the order they were configured, so an
    earlier window wins any overlap. That keeps a hand-written config
    predictable instead of silently merging overlapping windows.

    When the clock cannot be vouched for, nothing is tested at all and the
    channel falls back to being itself. This function never raises - whatever
    the hardware hands it, the answer is a channel rather than a traceback.
    """
    verdict = clock_verdict(epoch, clock_trust=clock_trust)
    _announce(verdict)
    if not verdict.trusted:
        return DaypartDecision(
            part=None, clock_trusted=False,
            reason=verdict.reason, detail=verdict.detail,
        )

    minute = _minute_of_day(epoch)
    if minute is None:
        # The timestamp passed the floor but the C library still would not turn
        # it into a local time. Same fallback, same reason to say so.
        unusable = ClockVerdict(
            False,
            "The clock on this box reads a time that cannot be turned into an "
            "hour of the day, so channels that change through the day are "
            "running as themselves instead.",
            "This is usually a flat CMOS battery on the motherboard.",
        )
        _announce(unusable)
        return DaypartDecision(
            part=None, clock_trusted=False,
            reason=unusable.reason, detail=unusable.detail,
        )

    if not dayparts:
        return DaypartDecision(part=None, minute=minute)
    for index, part in enumerate(dayparts):
        if part.contains(minute):
            return DaypartDecision(part=part, index=index, minute=minute)
    return DaypartDecision(part=None, minute=minute)


def active_daypart(
    dayparts: Sequence[Daypart],
    epoch: Optional[float] = None,
    *,
    clock_trust: ClockTrust = None,
) -> Optional[Daypart]:
    """The daypart covering ``epoch``, or ``None`` for the channel's default.

    ``None`` now means one of two things - no window matched, or no window was
    tested because the clock could not be believed. Both play the channel as
    itself, which is why they can share an answer; use :func:`resolve` when you
    need to tell them apart.
    """
    return resolve(dayparts, epoch, clock_trust=clock_trust).part


def _minute_of_day(epoch: Optional[float]) -> Optional[int]:
    """:func:`minutes_since_midnight`, but ``None`` instead of an exception."""
    try:
        return minutes_since_midnight(epoch)
    except (OverflowError, OSError, ValueError):
        return None


def _announce(verdict: ClockVerdict) -> None:
    """Log a change of clock trust once, not once per frame.

    The player asks this several times a second. A warning per tick is the
    same as no warning at all - it buries the one line that mattered.
    """
    global _announced
    if verdict.trusted:
        if _announced is not None:
            log.info("clock trusted again - dayparting resumed")
            _announced = None
        return
    if _announced == verdict.reason:
        return
    _announced = verdict.reason
    log.warning("dayparting paused: %s %s", verdict.reason, verdict.detail)


def clock_report(
    epoch: Optional[float] = None, *, clock_trust: ClockTrust = None
) -> Dict[str, Any]:
    """Is dayparting running at all right now, and if not, what does one do?

    The one call a dashboard needs. Deliberately takes no channel: whether the
    clock can be believed is a fact about the box, not about a channel, and
    the page that says so should be able to say it without a lineup in hand.
    """
    epoch = time.time() if epoch is None else epoch
    verdict = clock_verdict(epoch, clock_trust=clock_trust)
    return {
        "trusted": verdict.trusted,
        "dayparting": verdict.trusted,
        "reason": verdict.reason,
        "detail": verdict.detail,
        "local_time": _believed_date(epoch) or "unreadable",
    }


__all__ = [
    "ClockTrust",
    "ClockVerdict",
    "Daypart",
    "DaypartDecision",
    "DaypartError",
    "IMPLAUSIBLE_AFTER",
    "IMPLAUSIBLE_BEFORE",
    "MINUTES_PER_DAY",
    "active_daypart",
    "clock_report",
    "clock_trust_source",
    "clock_verdict",
    "format_clock",
    "minutes_since_midnight",
    "parse_clock",
    "resolve",
    "set_clock_trust",
]
