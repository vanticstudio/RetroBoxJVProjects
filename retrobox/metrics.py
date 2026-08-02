""""Is my box coping?" - answered with a sentence, then the numbers.

The System page used to answer that question with "LOAD 1.53 over 2 cores".
That is a queue length. It is a real and useful number, but it is not an
answer, and it is not a percentage: a box can sit at load 1.5 while barely
warm, or at load 0.9 while dropping every third frame. So this module measures
what is actually being asked about - how much of the processor is in use, how
much memory is left, and whether either is getting worse - and puts a plain
English verdict in front of it.

Five things shape every decision in here.

**It is somebody else's box.** A two-core Celeron, bought secondhand, sold on,
switched off at the wall by a customer who will never see a terminal. Nothing
in here may raise: a health page that breaks when a file is missing breaks
exactly when somebody is trying to find out what is wrong. Every reader
returns ``None`` for "could not tell", and ``None`` is never rendered as a
zero.

**A measurement is not a guess.** CPU use is a *delta between two reads* of
/proc/stat. With only one read there is no delta, so there is no percentage -
not 0, not 100, and not the since-boot average dressed up as "now". The first
answer is "measuring", and the page says so. A reading left over from before
the page was closed is the same problem wearing a hat: dividing by the whole
gap describes a stretch nobody was watching and presents it as this moment, so
past a certain age a previous reading counts as no previous reading at all.

**Judging heat is not this module's job.** Whether a reading is a problem
depends on which part it came from, and the part's own manufacturer publishes
the answer beside it. sensors.py reads those limits; this module is handed the
state it worked out and repeats it. There is no temperature written down
anywhere in this file, and a test greps to keep it that way.

**Measuring must not become the problem.** This box may already be doing
software video decode with nothing to spare. So sampling only happens while
somebody is actually looking at the System page, at most once every couple of
seconds, and the cost of collection is measured and published rather than
asserted. See :meth:`Collector.someone_is_watching`.

**Nothing is written to disk.** History lives in a fixed-size ring buffer in
memory. A metrics log ticking away on a customer's SSD for years, for a page
nobody is looking at, is a way to wear out somebody else's hardware. The ring
is not free, so it was weighed rather than guessed at: a full hour at the
shipped defaults is 1801 samples, and on a box with a thermal sensor those
samples hold **317,976 bytes - about 310 KB** (274,768, about 268 KB, on a box
with no sensor, where every sample shares one None). That is the whole ring
walked into, not ``sys.getsizeof`` on the deque, which reports the deque and
none of the four separately allocated floats in each sample - and which is how
the figure published before this was out by more than half. See
``test_an_hour_of_history_costs_what_the_manual_says_it_costs``.

Every path is injectable (``proc_root``, ``clock``, the three sources) because
none of this can be tested on the machine it is written on - a Mac has no
/proc - and because the awkward cases that matter most, a truncated file or a
counter that went backwards, cannot be produced on demand by a real box.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque, namedtuple
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

#: Where Linux keeps the counters. Overridable so tests need no kernel.
PROC_ROOT = Path("/proc")

#: How often a sample may be taken, at most. Video decode is the job; this is
#: not. Once every two seconds is plenty to catch a box heating up over an
#: hour, and is slow enough that the collection cost rounds to nothing.
SAMPLE_INTERVAL_SECONDS = 2.0

#: How long a viewer counts as present after their last poll. The System page
#: polls every couple of seconds, so this is several missed polls' grace - and
#: it is what makes a closed laptop lid stop the sampling on its own, with no
#: goodbye required.
WATCHER_TIMEOUT_SECONDS = 15.0

#: The longest any caller may claim to be watching for, however politely they
#: ask. A leaked watcher is a bug; a leaked watcher that samples for ever is a
#: bug that costs a customer frames.
MAX_WATCH_SECONDS = 60.0

#: How far back the trend looks. An hour, because that is the actual failure
#: story: a box in a closed cabinet behind a television climbs slowly and only
#: then throttles, so somebody checking the page cold sees a healthy number.
TREND_WINDOW_SECONDS = 3600.0

# ---------------------------------------------------------------------------
# What counts as trouble, and why each number is where it is
# ---------------------------------------------------------------------------
#: Busy enough to mention. A two-core box doing software decode of a 1080p
#: file sits comfortably in the 40-70% range while playing perfectly, so the
#: "working hard" line has to sit above that or it cries wolf on every box.
CPU_WORKING_PERCENT = 85.0

#: Busy enough to call it struggling. Above this there is no headroom left for
#: the next busy scene, and the decoder has nowhere to catch up.
CPU_STRUGGLING_PERCENT = 95.0

#: Load average per core at which work is genuinely queuing rather than merely
#: running. 1.0 per core is "fully used"; 2.0 means twice as much work wants
#: the processor as there is processor, which is felt.
LOAD_PER_CORE_STRUGGLING = 2.0

#: Memory tight enough to matter. Below 10% available the kernel starts
#: throwing away cached file data, and for a video player that means going
#: back to the disk for something it had a moment ago.
MEMORY_TIGHT_PERCENT = 90.0

#: There is no temperature limit in this file, and there must never be one.
#: What is safe depends entirely on which part is being measured - a drive, a
#: chipset and a processor sitting at the same reading are three different
#: situations - and the part's own manufacturer publishes the answer next to
#: the reading, in temp*_max and temp*_crit. sensors.py reads those and turns
#: them into fine / warm / hot / critical; these are simply which of its words
#: mean trouble. A part that publishes no limit is "unknown", and unknown
#: stays unspoken rather than being guessed at from a number chosen here.
TEMPERATURE_STRUGGLING_STATES = ("hot", "critical")
TEMPERATURE_WORKING_STATES = ("warm",)

#: Every state a sensor may hand over and be believed. Anything else - a
#: typo, a word from a future version of sensors.py, a caller inventing its
#: own vocabulary - is treated as "we were not told", which is the only safe
#: place for a word this module does not understand.
_KNOWN_TEMPERATURE_STATES = (
    ("fine",) + TEMPERATURE_WORKING_STATES + TEMPERATURE_STRUGGLING_STATES
)

#: Dropped frames per minute that a person would actually see. One or two a
#: minute is invisible and happens on a seek; one every ten seconds is a
#: picture that stutters.
FRAME_DROPS_PER_MINUTE_VISIBLE = 6.0

# How much a value has to move across the window before it is called a trend
# rather than noise. Both of these are percentages of a fixed thing - a
# processor that is 8 points busier is meaningfully busier on any box - so the
# band can be stated here. Heat is not: see :func:`_wobble_trend`.
_CPU_TREND_POINTS = 8.0
_MEMORY_TREND_POINTS = 3.0

#: Fewest readings before a direction is claimed at all. Two points make a
#: line out of anything.
_TREND_MINIMUM_SAMPLES = 4

#: Busy and idle jiffies for one processor line of /proc/stat.
CpuTicks = namedtuple("CpuTicks", "busy idle")

_Sample = namedtuple("_Sample", "at cpu_percent memory_percent celsius")


# ==========================================================================
# The readers. Small, pure, and none of them may raise.
# ==========================================================================
def _read_text(path: Path) -> Optional[str]:
    """Read a whole file, or None for any reason at all that it did not work.

    Missing, unreadable, a directory, gone between the check and the open, a
    /proc file that returned EIO because the driver behind it is unhappy. All
    of those are the same answer to the caller: we could not tell.
    """
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, ValueError):
        log.debug("could not read %s", path, exc_info=True)
        return None


def read_cpu_times(root: Path = PROC_ROOT) -> Optional[Dict[str, CpuTicks]]:
    """Busy and idle tick counts per processor, from /proc/stat.

    Returns ``{"cpu": ticks, "cpu0": ticks, ...}`` where ``cpu`` is the whole
    machine, or ``None`` if nothing usable could be read. These are cumulative
    counters since boot and mean nothing on their own - see
    :func:`busy_percent`, which is the only honest thing to do with them.

    Two kinds of damage are handled deliberately:

    * **A truncated line.** A short read can cut /proc/stat in half. A line
      with fewer than four numbers has no idle column, and treating whatever
      came last as idle time produces a confident, wrong percentage. Such a
      line is dropped; the whole lines before it are kept.
    * **Columns we did not expect.** The kernel has extended this line before
      and may again. Anything past the first eight fields is ignored, because
      fields nine and ten are guest time and the kernel has *already* counted
      those inside user and nice - adding them again reports a box as more
      than 100% busy.
    """
    text = _read_text(root / "stat")
    if text is None:
        return None

    times: Dict[str, CpuTicks] = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            continue
        numbers: List[int] = []
        for value in fields[1:9]:
            try:
                numbers.append(int(value))
            except ValueError:
                break                     # a half-written number ends the line
        if len(numbers) < 4:
            continue                      # no idle column: nothing to measure
        # user nice system idle iowait irq softirq steal. Waiting on the disk
        # is idle time as far as "is the processor busy" goes - the processor
        # is not doing anything, it is waiting for a spinning disk.
        idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
        busy = numbers[0] + numbers[1] + numbers[2] + sum(numbers[5:])
        times[fields[0]] = CpuTicks(busy=busy, idle=idle)

    return times or None


def busy_percent(before: Optional[CpuTicks], after: Optional[CpuTicks]) -> Optional[float]:
    """How much of the time between two reads was spent doing something.

    ``None`` whenever the question cannot be answered rather than a number
    that looks like it was measured:

    * either read is missing;
    * no time passed between them (two reads of the same instant) - 0% here
      would be a claim that the box was idle;
    * a counter went backwards, which /proc/stat's never do, so we are not
      comparing what we think we are comparing.
    """
    if before is None or after is None:
        return None
    busy = after.busy - before.busy
    idle = after.idle - before.idle
    if busy < 0 or idle < 0:
        return None
    total = busy + idle
    if total <= 0:
        return None
    # The guards above already make this a fraction of a positive total, so
    # the clamp cannot fire today. It stays because "0 to 100" is the promise
    # this function makes to the page, and a later change to how busy and idle
    # are derived should break a test rather than put 140% in front of a
    # customer.
    return round(max(0.0, min(100.0, busy * 100.0 / total)), 1)


def core_count(root: Path = PROC_ROOT) -> int:
    """How many cores this box has, counted from the file rather than assumed.

    Read from /proc/stat so it is the same source as everything else here, and
    so a test can have a two-core box on an eight-core Mac. Falls back to what
    Python thinks only when there is no file to count.
    """
    times = read_cpu_times(root)
    if times:
        cores = sum(1 for name in times if name != "cpu" and name[3:].isdigit())
        if cores:
            return cores
    return os.cpu_count() or 1


def _meminfo_bytes(text: str) -> Dict[str, int]:
    """Every ``Key: 1234 kB`` line, in bytes.

    A line without the unit is skipped rather than believed. /proc/meminfo
    writes "kB" on every size it reports, so a size with no unit is a line
    that got cut in half - and reading "2000" as 2000 bytes turns a truncated
    file into a box that appears to have run out of memory.
    """
    values: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) != 2 or fields[1].lower() != "kb":
            continue
        try:
            values[key.strip()] = int(fields[0]) * 1024
        except ValueError:
            continue
    return values


def read_memory(root: Path = PROC_ROOT) -> Optional[Dict[str, Any]]:
    """Memory and swap, as percentages *and* real numbers.

    ``None`` only when even the total is unreadable - with a total and nothing
    else there is still something true to say, so the missing parts come back
    as ``None`` rather than taking the whole reading down.

    "Available" is MemAvailable, the kernel's own estimate of what a new
    program could actually get. It is not MemFree: on a healthy Linux box
    MemFree is always small because the kernel fills spare memory with cached
    file data, and reporting that as "used" tells a customer their box is full
    when it is working exactly as intended.

    **Swap is reported only when some is in use.** On a box like this, swap in
    use means it has run out of memory and is using the disk as memory, which
    is slow enough to see. Untouched swap is not news, and a zero on the page
    only invites worry about a number that means nothing.
    """
    text = _read_text(root / "meminfo")
    if text is None:
        return None
    values = _meminfo_bytes(text)

    total = values.get("MemTotal")
    if not total:
        return None

    available = values.get("MemAvailable")
    if available is None:
        # An old kernel with no MemAvailable, or a file that got cut before
        # it. Free plus the reclaimable caches is the classic approximation.
        parts = [values.get(k) for k in ("MemFree", "Buffers", "Cached")]
        if any(p is not None for p in parts):
            available = sum(p for p in parts if p is not None)

    used = None if available is None else max(0, total - available)
    percent = None if used is None else round(used * 100.0 / total, 1)

    swap = None
    swap_total = values.get("SwapTotal") or 0
    swap_free = values.get("SwapFree")
    if swap_total > 0 and swap_free is not None:
        swap_used = max(0, swap_total - swap_free)
        if swap_used > 0:
            swap = {
                "total_bytes": swap_total,
                "used_bytes": swap_used,
                "free_bytes": swap_free,
                "percent_used": round(swap_used * 100.0 / swap_total, 1),
            }

    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "percent_used": percent,
        "swap": swap,
    }


def _load_summary(one: float, cores: int) -> str:
    """The load average said in words, because the number answers nothing.

    Load is a queue length, not a percentage, and "1.53" on its own has been
    mistaken for "153%" by more than one person. What it actually says is how
    many jobs wanted the processor, so that is what gets written down.
    """
    per_core = one / cores if cores else one
    if per_core < 0.7:
        return "Nothing much is waiting for the processor."
    if per_core < 1.0:
        return "The processor is busy, but nothing is waiting for it."
    if per_core < LOAD_PER_CORE_STRUGGLING:
        return "Slightly more work is arriving than the processor can start straight away."
    return "Work is queuing up faster than this box can get through it."


def read_load_average(
    root: Path = PROC_ROOT, *, cores: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """The load average, kept because it is genuinely useful - but explained.

    Read from /proc/loadavg rather than :func:`os.getloadavg` so it comes from
    the same injectable root as everything else, and so a test can describe a
    box under load without putting this machine under load.

    ``cores`` is accepted so a caller that has just read /proc/stat can hand
    over the count it already has, rather than making this open the same file
    a second time to work out what the number means.
    """
    text = _read_text(root / "loadavg")
    if text is None:
        return None
    fields = text.split()
    if len(fields) < 3:
        return None
    try:
        one, five, fifteen = (float(fields[i]) for i in range(3))
    except ValueError:
        return None

    if cores is None:
        cores = core_count(root)
    return {
        "one_minute": round(one, 2),
        "five_minutes": round(five, 2),
        "fifteen_minutes": round(fifteen, 2),
        "cores": cores,
        "per_core": round(one / cores, 2) if cores else None,
        "summary": _load_summary(one, cores),
    }


# ==========================================================================
# Trend
# ==========================================================================
def _trend(values: Sequence[Optional[float]], threshold: float) -> str:
    """"climbing", "falling", "steady", or an honest "unknown".

    The two halves of the window are averaged and compared. Averaging rather
    than taking first-and-last is what stops one noisy sample - a single scene
    change, a single garbage collection - from being reported as a direction.
    """
    series = [v for v in values if v is not None]
    if len(series) < _TREND_MINIMUM_SAMPLES:
        return "unknown"
    half = len(series) // 2
    earlier = sum(series[:half]) / half
    later = sum(series[half:]) / (len(series) - half)
    if later - earlier >= threshold:
        return "climbing"
    if earlier - later >= threshold:
        return "falling"
    return "steady"


def _wobble_trend(values: Sequence[Optional[float]]) -> str:
    """The same question for a reading whose scale we are not entitled to pick.

    The two series above are judged against a band written down here, which is
    fair enough for a percentage: eight points of processor is eight points of
    processor on every box. A temperature has no such band. How much a reading
    jitters depends on the part, on the sensor bolted to it and on where in
    the case it sits, and on the next box every one of those is different -
    so a number chosen here would be wrong in the same way a limit chosen here
    would be wrong.

    So the band is measured instead of chosen: the average step between
    neighbouring readings is how much this sensor, on this box, wobbles when
    nothing is happening, and a direction is only claimed when the window has
    moved further than that. A reading that never moves at all is steady, and
    is the one case the arithmetic cannot be asked about.

    Measuring the band costs one more walk of the window than a fixed band
    does - 0.1 ms over a full hour of samples on the machine this was written
    on, once per poll, and only while somebody has the page open. It happens
    in :meth:`Collector._snapshot` rather than in :meth:`Collector._sample`,
    so it is outside what the published collection cost covers.
    """
    series = [v for v in values if v is not None]
    if len(series) < _TREND_MINIMUM_SAMPLES:
        return "unknown"
    steps = [abs(later - earlier) for earlier, later in zip(series, series[1:])]
    wobble = sum(steps) / len(steps) if steps else 0.0
    if wobble <= 0:
        return "steady"
    return _trend(series, wobble)


def _call(source: Optional[Callable[[], Any]]) -> Optional[Any]:
    """Call one of the injected sources, treating any failure as "no reading".

    The temperature comes from sensors.py, the frame drops from the player and
    the uptime from the status file. None of those are this module's to fix,
    and none of them get to take the System page down when they misbehave.
    """
    if source is None:
        return None
    try:
        return source()
    except Exception:  # noqa: BLE001 - a source that broke is simply absent
        log.debug("a metrics source failed", exc_info=True)
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number      # NaN is not a reading


def _temperature_reading(value: Any) -> Optional[Dict[str, Any]]:
    """Whatever the temperature seam handed over, as a reading with a verdict.

    The verdict is not made here and must not be. sensors.py has already
    compared the reading against the limits the part's own manufacturer
    published beside it, and hands over its answer - fine, warm, hot, critical
    or unknown - along with the name of the part in words a customer would
    recognise. Anything shaped like one of its readings is taken at its word.

    A caller with only a number is still heard: the reading goes on the page,
    because it is true and hiding it would lose it. But it arrives as
    ``unknown``, because a temperature with no published limit beside it is a
    reading and not a diagnosis, and the difference between those two is the
    whole reason this seam changed shape.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        celsius = _as_float(value.get("celsius"))
        if celsius is None:
            return None
        state = value.get("state")
        name = value.get("name")
        return {
            "celsius": celsius,
            # A state we do not recognise is one we cannot act on, and acting
            # on it anyway is how a guess gets in.
            "state": state if state in _KNOWN_TEMPERATURE_STATES else "unknown",
            "name": str(name) if name else None,
        }
    celsius = _as_float(value)
    if celsius is None:
        return None
    return {"celsius": celsius, "state": "unknown", "name": None}


def _heat_reason(temperature: Dict[str, Any]) -> str:
    """The heat sentence, in the owner's words rather than the kernel's.

    Every one of these says whose limit it is, because that is the honest
    thing and because it is also the useful thing: "above the limit its
    manufacturer allows" tells somebody the box is outside what it was built
    for, where a bare number tells them nothing they can act on.
    """
    celsius = round(temperature["celsius"])
    part = temperature.get("name")
    state = temperature.get("state")
    if state in TEMPERATURE_WORKING_STATES:
        opening = "It is warm"
        tail = (" - still inside the limit its manufacturer sets, but worth "
                "checking there is air around it.")
    elif state == "critical":
        opening = "It is running hot"
        tail = (", past the limit its manufacturer sets, which is where a box "
                "like this starts slowing itself down to cool off.")
    else:
        opening = "It is running hot"
        tail = ", above the limit its manufacturer allows for that part."
    if part:
        return "{}: the {} is at {} C{}".format(opening, part, celsius, tail)
    return "{} ({} C){}".format(opening, celsius, tail)


# ==========================================================================
# The collector
# ==========================================================================
class Collector:
    """Samples the box on an interval, but only while somebody is looking.

    One of these is shared by the whole dashboard process (see
    :func:`collector`). It holds three things: the previous /proc/stat reading,
    so a delta is possible at all; a bounded ring buffer of recent samples, for
    the trend; and running peaks, which are kept outside the buffer so a peak
    an hour ago survives being scrolled out of it.

    Both the peak and the trend can only see samples that were taken, and
    samples are only taken while somebody has the System page open. So they
    are the highest reading and the direction *while this page has been open*
    - never "the peak since the television started", which this module has no
    way of knowing and must not be described as knowing.

    The optional sources are the seams to code this module does not own, and
    each is a plain callable so this stays testable without any of them:

    ``temperature_source``
        returns one of sensors.py's readings - a mapping with ``celsius``, the
        ``state`` it worked out from the limits the part's own manufacturer
        publishes, and the ``name`` of the part in words - or None. Wire it to
        ``sensors.hottest()``. It hands over a state and not just a number
        because a state is what the verdict actually needs: this module has no
        way of knowing whether it is looking at a drive, a chipset or a
        processor, and the same reading is fine on one and a fault on another.
        A bare number is still accepted from a caller that only has one, and
        is shown on the page, but nothing is claimed about what it means.
    ``frame_drop_source``
        returns the player's *cumulative* dropped-frame count. Cumulative
        rather than a rate because that is what mpv actually exposes, and
        because turning a counter into a rate is the same delta arithmetic the
        CPU already does here. Frame drops are the real health signal for a
        television - a box at 40% that stutters is not coping - so the verdict
        prefers this over every other number when it is present.
    ``tv_uptime_source``
        returns how long the television process has been up. When that goes
        backwards the television restarted, and the peaks from its previous
        run say nothing about this one, so they are cleared.
    """

    def __init__(
        self,
        *,
        proc_root: Path = PROC_ROOT,
        clock: Callable[[], float] = time.monotonic,
        interval: float = SAMPLE_INTERVAL_SECONDS,
        watcher_timeout: float = WATCHER_TIMEOUT_SECONDS,
        window_seconds: float = TREND_WINDOW_SECONDS,
        temperature_source: Optional[Callable[[], Any]] = None,
        frame_drop_source: Optional[Callable[[], Optional[float]]] = None,
        tv_uptime_source: Optional[Callable[[], Optional[float]]] = None,
    ) -> None:
        self.proc_root = Path(proc_root)
        self._clock = clock
        # Floors, not suggestions. A caller asking for a reading ten times a
        # second is asking this box to spend its afternoon measuring itself,
        # and a caller asking for a one-second watcher timeout would restart
        # the sampling on every poll for ever.
        self._interval = max(0.5, float(interval))
        self._watcher_timeout = max(1.0, float(watcher_timeout))
        # How old the previous reading may be and still be half of a
        # measurement of *now*. The watcher timeout is the honest boundary:
        # it is already this module's definition of "nobody is looking any
        # more", so a pair of readings further apart than that is a pair that
        # straddles a stretch with the page shut. The interval is a floor for
        # the same reason the interval has one - a caller must not be able to
        # configure every ordinary sample into being too old.
        self._previous_max_age = max(self._watcher_timeout, self._interval * 2.0)
        self.temperature_source = temperature_source
        self.frame_drop_source = frame_drop_source
        self.tv_uptime_source = tv_uptime_source

        # The ring buffer is sized by the window and the interval, so it is
        # bounded by design rather than by remembering to trim it. A box left
        # on for six months samples eight million times; this holds the last
        # hour and drops the rest on the floor, for ever, at no cost.
        self._history_limit = max(8, int(float(window_seconds) / self._interval) + 1)
        self._history: Deque[_Sample] = deque(maxlen=self._history_limit)

        # Collection cost is measured, not claimed. Kept short because only
        # the recent average is interesting.
        self._costs: Deque[float] = deque(maxlen=64)

        self._lock = threading.RLock()
        self._watch_until = 0.0
        self._reset_run(self._clock())

    # -- the run ------------------------------------------------------------
    def _reset_run(self, now: float) -> None:
        """Forget everything measured. Used at startup and on a TV restart."""
        self._history.clear()
        self._previous: Optional[Dict[str, CpuTicks]] = None
        self._previous_at: Optional[float] = None
        self._samples = 0
        self._started_at = now
        self._cpu_percent: Optional[float] = None
        self._cpu_state = "measuring"
        self._per_core: Optional[Dict[str, float]] = None
        self._cores: Optional[int] = None
        self._load: Optional[Dict[str, Any]] = None
        self._memory: Optional[Dict[str, Any]] = None
        self._celsius: Optional[float] = None
        self._heat_state = "unknown"
        self._heat_name: Optional[str] = None
        self._peak_cpu: Optional[float] = None
        self._peak_memory: Optional[float] = None
        self._peak_celsius: Optional[float] = None
        self._frames_total: Optional[float] = None
        self._frames_at: Optional[float] = None
        self._frames_per_minute: Optional[float] = None
        self._frames_known = False
        self._tv_uptime: Optional[float] = None

    # -- the watching gate --------------------------------------------------
    def someone_is_watching(self, timeout: Optional[float] = None) -> None:
        """Somebody has the System page open. Sampling may happen.

        Called on every poll of the metrics endpoint, so the deadline keeps
        being pushed forward while - and only while - the page is really
        there. Nothing here starts a thread: the poll that says "I am still
        watching" is the same call that takes the reading, so a page that
        stops asking stops the sampling by definition. There is nothing left
        running to leak.
        """
        window = self._watcher_timeout if timeout is None else float(timeout)
        window = max(0.0, min(MAX_WATCH_SECONDS, window))
        with self._lock:
            self._watch_until = self._clock() + window

    def nobody_is_watching(self) -> None:
        """The page was closed or navigated away from. Stop immediately."""
        with self._lock:
            self._watch_until = 0.0

    def is_watching(self) -> bool:
        """Is anybody still looking? Expires on its own, deliberately.

        A browser tab killed by a phone, a closed laptop lid or a dropped
        network connection never gets to send the goodbye. The deadline means
        none of them can leave this box sampling for ever.
        """
        with self._lock:
            return self._clock() < self._watch_until

    # -- sampling -----------------------------------------------------------
    def poll(self) -> Dict[str, Any]:
        """Take a reading if one is due and anybody is watching, then report.

        This is the whole API the dashboard needs: it is safe to call as fast
        as a page can poll, because the interval - not the caller - decides
        when the box is actually read.
        """
        with self._lock:
            if self.is_watching():
                now = self._clock()
                due = self._previous_at is None or now - self._previous_at >= self._interval
                if due:
                    self._sample(now)
            return self._snapshot()

    def sample(self) -> Dict[str, Any]:
        """Take a reading now, whatever the interval says. For tests and CLI."""
        with self._lock:
            self._sample(self._clock())
            return self._snapshot()

    def snapshot(self) -> Dict[str, Any]:
        """The last thing measured. Never reads anything - never costs anything."""
        with self._lock:
            return self._snapshot()

    def _sample(self, now: float) -> None:
        started = time.perf_counter()

        # Did the television restart underneath us? Its uptime going backwards
        # is the only evidence available, and it is enough.
        uptime = _as_float(_call(self.tv_uptime_source))
        if uptime is not None and self._tv_uptime is not None and uptime < self._tv_uptime:
            self._reset_run(now)
        self._tv_uptime = uptime

        times = read_cpu_times(self.proc_root)
        if times is not None:
            counted = sum(1 for n in times if n != "cpu" and n[3:].isdigit())
            self._cores = counted or os.cpu_count() or 1
        # A percentage is the difference between two readings, and it is only
        # a measurement of *now* if both of them are from now. The page can be
        # closed for an afternoon and reopened, and the reading left over from
        # before it closed would divide the whole afternoon into a confident
        # number describing a stretch nobody was watching. Past its useful age
        # the previous reading is worth exactly what no previous reading is
        # worth, and it is treated the same way: the answer is "measuring".
        previous = self._previous
        if self._previous_at is None or now - self._previous_at > self._previous_max_age:
            previous = None

        if times is None:
            # The file is gone or unreadable. That is not 0% and it is not the
            # last reading either - both would be stale claims.
            self._cpu_percent = None
            self._per_core = None
            self._cpu_state = "unknown"
        else:
            whole = busy_percent((previous or {}).get("cpu"), times.get("cpu"))
            if whole is None:
                # Either the first read of this run, or a delta that could not
                # be trusted. Both are "measuring", never a number.
                self._cpu_percent = None
                self._per_core = None
                self._cpu_state = "measuring"
            else:
                self._cpu_percent = whole
                self._cpu_state = "measured"
                per_core: Dict[str, float] = {}
                for name, ticks in times.items():
                    if name == "cpu":
                        continue
                    value = busy_percent((previous or {}).get(name), ticks)
                    if value is not None:
                        per_core[name] = value
                # One core pinned and one idle averages to exactly the same
                # number as both at half, and feels completely different.
                self._per_core = per_core or None
                self._peak_cpu = (
                    whole if self._peak_cpu is None else max(self._peak_cpu, whole)
                )
        self._previous = times
        self._previous_at = now

        self._load = read_load_average(self.proc_root, cores=self._cores)
        self._memory = read_memory(self.proc_root)
        memory_percent = (self._memory or {}).get("percent_used")
        if memory_percent is not None:
            self._peak_memory = (
                memory_percent if self._peak_memory is None
                else max(self._peak_memory, memory_percent)
            )

        reading = _temperature_reading(_call(self.temperature_source))
        self._celsius = reading["celsius"] if reading else None
        self._heat_state = reading["state"] if reading else "unknown"
        self._heat_name = reading["name"] if reading else None
        if self._celsius is not None:
            self._peak_celsius = (
                self._celsius if self._peak_celsius is None
                else max(self._peak_celsius, self._celsius)
            )

        self._sample_frames(now)

        self._history.append(
            _Sample(now, self._cpu_percent, memory_percent, self._celsius)
        )
        self._samples += 1
        self._costs.append((time.perf_counter() - started) * 1000.0)

    def _sample_frames(self, now: float) -> None:
        """Turn the player's cumulative drop counter into a rate.

        The counter restarts with every file mpv opens, so a smaller number
        than last time is a new episode, not a flood of dropped frames. That
        resets the baseline and reports no rate for this sample rather than
        putting "1800 dropped frames a minute" in front of a customer at the
        end of every programme.

        A count from before the page was closed is refused for the same reason
        the processor's is: spreading an afternoon's drops over an afternoon
        produces a number about a time nobody was watching, and the page would
        show it as the rate right now.
        """
        total = _as_float(_call(self.frame_drop_source))
        if total is None:
            self._frames_known = False
            self._frames_per_minute = None
            return
        self._frames_known = True
        stale = (
            self._frames_at is not None
            and now - self._frames_at > self._previous_max_age
        )
        if (self._frames_total is None or self._frames_at is None
                or total < self._frames_total or stale):
            self._frames_per_minute = None
        else:
            elapsed = now - self._frames_at
            self._frames_per_minute = (
                round((total - self._frames_total) / elapsed * 60.0, 1)
                if elapsed > 0 else None
            )
        self._frames_total = total
        self._frames_at = now

    # -- what the page is handed -------------------------------------------
    def _snapshot(self) -> Dict[str, Any]:
        """Format what was last measured. Opens no files, on purpose.

        The page may ask for this at any time, including when nobody is
        watching and no sample is due, so it has to be free. Anything in here
        that read /proc would be a cost that the interval does not gate and
        the cost measurement below does not see.
        """
        now = self._clock()
        cpu = {
            "percent": self._cpu_percent,
            "state": self._cpu_state,
            "message": _CPU_MESSAGES.get(self._cpu_state, ""),
            "per_core": dict(self._per_core) if self._per_core else None,
            "busiest_core_percent": max(self._per_core.values()) if self._per_core else None,
            "cores": self._cores,
            "peak_percent": self._peak_cpu,
            "trend": _trend([s.cpu_percent for s in self._history], _CPU_TREND_POINTS),
        }

        memory = None
        if self._memory is not None:
            memory = dict(self._memory)
            memory["peak_percent"] = self._peak_memory
            memory["trend"] = _trend(
                [s.memory_percent for s in self._history], _MEMORY_TREND_POINTS
            )

        temperature = None
        if self._celsius is not None:
            temperature = {
                "celsius": round(self._celsius, 1),
                # The sensor's own verdict, and the name of the part in words
                # the owner would recognise. Both come from sensors.py, which
                # read them off the part; neither is decided here.
                "state": self._heat_state,
                "name": self._heat_name,
                "peak_celsius": round(self._peak_celsius, 1) if self._peak_celsius else None,
                "trend": _wobble_trend([s.celsius for s in self._history]),
            }

        frames = {
            "known": self._frames_known,
            "dropped_total": self._frames_total if self._frames_known else None,
            "dropped_per_minute": self._frames_per_minute,
        }

        snapshot: Dict[str, Any] = {
            "verdict": self._verdict(cpu, memory, temperature, frames),
            "cpu": cpu,
            "load": dict(self._load) if self._load else None,
            "memory": memory,
            "temperature": temperature,
            "frames": frames,
            "collection": self._cost(),
            "watching": self._clock() < self._watch_until,
            "samples": self._samples,
            "watching_seconds": round(now - self._started_at, 1),
            "interval_seconds": self._interval,
        }
        return snapshot

    def _cost(self) -> Dict[str, Any]:
        """What this measurement cost, measured rather than asserted.

        Published on the page because a dashboard that steals cycles from the
        video it is reporting on shows up as the very frame drops it is
        measuring, and the only way to know it is not doing that is a number.

        For scale: on the Mac this was written on, one sample - three /proc
        files read and parsed, the deltas, the trend - measured **0.14 ms**,
        which at one sample every two seconds is 0.007% of one core. That Mac
        is emphatically not a two-core Celeron, and a synthetic /proc on a
        local disk is not procfs, so expect the real box to be some multiple
        of that. Even a twenty-fold multiple is under a tenth of one percent
        of one core, which is why this design is affordable at all - but the
        number that matters is the one this method reports on the box itself,
        not the one in this comment.
        """
        average = sum(self._costs) / len(self._costs) if self._costs else None
        return {
            "samples": self._samples,
            "last_ms": round(self._costs[-1], 3) if self._costs else None,
            "average_ms": round(average, 3) if average is not None else None,
            "percent_of_one_core": (
                round(average / (self._interval * 1000.0) * 100.0, 3)
                if average is not None else None
            ),
            "note": (
                "Time spent reading /proc, measured on this box, averaged over "
                "the last {} readings. Only measured while the System page is "
                "open.".format(len(self._costs) or 0)
            ),
        }

    # -- the sentence -------------------------------------------------------
    def _verdict(
        self,
        cpu: Dict[str, Any],
        memory: Optional[Dict[str, Any]],
        temperature: Optional[Dict[str, Any]],
        frames: Dict[str, Any],
    ) -> Dict[str, Any]:
        """One plain sentence, then the reasons behind it.

        The order matters. Frame drops come first because they are the only
        thing on this list a customer can actually see on the television, and
        because a box at 40% that stutters is not coping whatever the
        percentage says. Then swap, then processor, then memory, then heat.

        With no frame-drop signal wired up at all the verdict falls back to
        the processor and memory, which is what it has always had to do - the
        seam is there so the answer gets better when the player is wired in,
        not so it is useless until then.
        """
        struggling: List[str] = []
        working: List[str] = []

        drops = frames.get("dropped_per_minute")
        if drops is not None and drops >= FRAME_DROPS_PER_MINUTE_VISIBLE:
            struggling.append(
                "The television is dropping frames ({} a minute), which is the "
                "picture stuttering.".format(round(drops))
            )
        elif drops:
            working.append(
                "The television has dropped a few frames, not enough to see."
            )

        swap = (memory or {}).get("swap")
        if swap:
            struggling.append(
                "Memory has run out and this box is using the disk as memory, "
                "which is slow."
            )

        percent = cpu.get("percent")
        if percent is not None:
            if percent >= CPU_STRUGGLING_PERCENT:
                struggling.append(
                    "The processor is fully used, with nothing left over."
                )
            elif percent >= CPU_WORKING_PERCENT:
                working.append("The processor is working hard.")

        busiest = cpu.get("busiest_core_percent")
        if (
            busiest is not None
            and busiest >= CPU_STRUGGLING_PERCENT
            and percent is not None
            and percent < CPU_WORKING_PERCENT
        ):
            # One core flat out while the other sleeps is a real complaint on
            # a two-core box, and the average hides it completely.
            working.append(
                "One processor core is flat out while the other has little to do."
            )

        used = (memory or {}).get("percent_used")
        if used is not None and used >= MEMORY_TIGHT_PERCENT:
            struggling.append("There is very little memory left.")

        # Heat is the one thing on this list that this module is not entitled
        # to judge. The sensor was compared against the limits its own maker
        # published; all that is done here is deciding which of its words the
        # customer needs to hear about. A part that publishes no limit says
        # "unknown", and unknown gets no sentence - a number with nothing to
        # compare it against would only be frightening or falsely reassuring.
        heat = (temperature or {}).get("state")
        if heat in TEMPERATURE_STRUGGLING_STATES:
            struggling.append(_heat_reason(temperature))
        elif heat in TEMPERATURE_WORKING_STATES:
            working.append(_heat_reason(temperature))

        if struggling:
            return {
                "state": "struggling",
                "sentence": "This box is struggling to keep up with what it is playing.",
                "reasons": struggling + working,
            }

        # Nothing is wrong. Now: do we actually know that, or have we simply
        # not measured yet? Saying "coping fine" without a measurement behind
        # it is the exact lie this module exists to stop telling.
        if cpu.get("state") == "measuring":
            return {
                "state": "measuring",
                "sentence": "This box is still taking its first measurement.",
                "reasons": [
                    "Processor use is the difference between two readings a "
                    "moment apart, so the first answer arrives on the next one."
                ],
            }
        if cpu.get("state") == "unknown":
            return {
                "state": "unknown",
                "sentence": "This box could not measure itself just now.",
                "reasons": ["The processor counters could not be read."] + working,
            }
        if working:
            return {
                "state": "working",
                "sentence": "This box is working hard but keeping up.",
                "reasons": working,
            }
        return {
            "state": "coping",
            "sentence": "This box is coping fine.",
            "reasons": [],
        }

    # -- for the tests, and for anybody wondering about memory --------------
    @property
    def history_limit(self) -> int:
        """The most samples that will ever be held. Fixed at construction."""
        return self._history_limit

    @property
    def history_length(self) -> int:
        return len(self._history)


#: What each CPU state means, in the words the page shows.
_CPU_MESSAGES = {
    "measuring": (
        "Measuring - processor use is the difference between two readings, so "
        "the first number arrives a moment from now"
    ),
    "measured": "",
    "unknown": "Could not read the processor counters on this box",
}


# ==========================================================================
# The one the dashboard uses
# ==========================================================================
_shared: Optional[Collector] = None
_shared_lock = threading.Lock()


def collector() -> Collector:
    """The single collector this process shares.

    One, because the delta and the history only mean anything if the same
    object keeps them, and because two of them would sample the box twice.
    """
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Collector()
        return _shared


def configure(**kwargs: Any) -> Collector:
    """Build the shared collector with the sources this box actually has.

    Called once by the dashboard at start-up to hand over the temperature,
    frame-drop and television-uptime seams. Replaces any existing collector,
    so anything already measured is dropped - which is right, because the
    thing measuring has just changed.
    """
    global _shared
    with _shared_lock:
        _shared = Collector(**kwargs)
        return _shared


def reset_collector() -> None:
    """Throw the shared collector away. For tests, and for a config reload."""
    global _shared
    with _shared_lock:
        _shared = None


def someone_is_watching(timeout: Optional[float] = None) -> None:
    """Somebody opened the System page."""
    collector().someone_is_watching(timeout)


def nobody_is_watching() -> None:
    """Somebody closed it."""
    collector().nobody_is_watching()


def snapshot() -> Dict[str, Any]:
    """Everything the health block shows: sample if due, then report."""
    return collector().poll()


__all__ = [
    "CPU_STRUGGLING_PERCENT",
    "CPU_WORKING_PERCENT",
    "FRAME_DROPS_PER_MINUTE_VISIBLE",
    "LOAD_PER_CORE_STRUGGLING",
    "MAX_WATCH_SECONDS",
    "MEMORY_TIGHT_PERCENT",
    "PROC_ROOT",
    "SAMPLE_INTERVAL_SECONDS",
    "TEMPERATURE_STRUGGLING_STATES",
    "TEMPERATURE_WORKING_STATES",
    "TREND_WINDOW_SECONDS",
    "WATCHER_TIMEOUT_SECONDS",
    "Collector",
    "CpuTicks",
    "busy_percent",
    "collector",
    "configure",
    "core_count",
    "nobody_is_watching",
    "read_cpu_times",
    "read_load_average",
    "read_memory",
    "reset_collector",
    "snapshot",
    "someone_is_watching",
]
