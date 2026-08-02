"""Is there actually a television out there?

A Retro Box lives in a cabinet behind the set, and when the set is off it
keeps decoding video for nobody. That is not really about the eight watts -
it is about a fan that roars into an empty room, heat pumped into secondhand
hardware that has already had one life, and the plain fact that a box which
goes quiet when the telly goes off feels like a better product than one that
does not. This module answers the one question that decision rests on.

**How it knows.** Every Linux graphics driver publishes a directory per output
under ``/sys/class/drm``, each with a ``status`` file holding one of exactly
three words: ``connected``, ``disconnected`` or ``unknown``. It needs no
privilege, no package and no vendor tool, which is what makes it the right
answer for hardware we have never seen. The root is a parameter with the real
path as its default, because there is no ``/sys`` on the machine these tests
run on and every one of them builds the tree as files instead.

**What hotplug honestly means, which is less than people assume.** Plenty of
televisions keep the hotplug line asserted while they sit in standby and only
drop it when they are switched off at the wall; others drop it the moment the
screen goes dark; it varies by manufacturer and there is no standard to appeal
to. So this detects *unplugged* and *off at the wall* reliably, and standby on
some sets only. Nothing here - no docstring, no comment, and nothing the
dashboard renders - may promise more than that.

**Absent and cannot-tell are different answers.** A box with no connectors at
all, an unreadable status file, a driver that says ``unknown``, or a word no
kernel has printed yet, all mean *we do not know*. They must never be rounded
down to "no display", because the failure they would cause is a box asleep in
front of a working television belonging to somebody who cannot SSH in and can
only switch it off at the wall. Never sleep on a guess.

**It listens, it does not poll.** A change on the DRM subsystem arrives as a
kernel uevent on an ``AF_NETLINK`` socket, which the standard library can open
unaided. Waking a two-core Celeron every second to read a sysfs file is
exactly the waste this feature exists to remove - and reading ``status`` is
not free either, since on several drivers it forces a fresh probe of the
connector. Where the netlink route is unavailable - or fails at any point
afterwards, in any way at all - the watcher degrades to a slow poll and *says*
it has, because a poll described as event-driven is a lie that costs somebody a
fan spinning for a minute after they switched the telly off. "In any way at
all" is meant literally: a feed that fails and is not caught stops the watcher
reaching :meth:`DisplayWatcher.refresh`, and a watcher that has stopped
refreshing is stuck believing whatever it last believed - which, stuck at
"absent", is a paused picture that never comes back.

**Nothing in here may stop the box booting, playing or being managed.** Every
failure path ends in "cannot tell, stay awake", which is the same behaviour as
the feature being switched off.
"""

from __future__ import annotations

import logging
import os
import select
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

#: Where every Linux graphics driver publishes its outputs. Overridable so the
#: suite can build a tree of files on a machine that has no /sys at all.
DRM_ROOT = Path("/sys/class/drm")

#: A television is connected. Not "switched on" - see the caveat below.
PRESENT = "present"
#: Every output the box has says nothing is plugged into it.
ABSENT = "absent"
#: We do not know, and must therefore behave exactly as if a set were there.
UNKNOWN = "unknown"

STATES = (PRESENT, ABSENT, UNKNOWN)

#: Float arithmetic on a monotonic clock is not exact, and a debounce that
#: fires a microsecond late is a debounce that never fires when the caller
#: passes it exactly the deadline it was given.
_EPSILON = 1e-9

#: The three words a DRM driver writes into a connector's ``status`` file.
_CONNECTED = "connected"
_DISCONNECTED = "disconnected"
_KERNEL_UNKNOWN = "unknown"
#: Ours, not the kernel's: the file was there but could not be read.
_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Connector:
    """One video output and the last thing the kernel said about it."""

    name: str
    status: str


@dataclass(frozen=True)
class Reading:
    """What the connectors add up to, and enough detail to show a person why."""

    state: str
    connectors: Tuple[Connector, ...] = ()
    detail: str = ""

    @property
    def can_tell(self) -> bool:
        return self.state != UNKNOWN


# ==========================================================================
# Reading the connectors
# ==========================================================================
def _read_status(node: Path) -> Optional[str]:
    """The one word in a connector's status file, or None if it isn't one.

    None means "this directory is not an output" - ``/sys/class/drm`` also
    holds the card itself, the render node and a ``version`` file, and none of
    those may be mistaken for a television that has gone missing.
    """
    status_file = node / "status"
    try:
        if not status_file.is_file():
            return None
    except OSError:
        return None
    try:
        text = status_file.read_text()
    except OSError:
        # It is a connector, and it would not give up its status. That is a
        # cannot-tell, not a disconnection.
        return _UNREADABLE
    word = text.strip().lower()
    if word in (_CONNECTED, _DISCONNECTED, _KERNEL_UNKNOWN):
        return word
    # A word no kernel has printed yet, or an empty file. Do not invent a
    # meaning for it, and above all do not read it as "no television".
    return _UNREADABLE


def read_state(root: Optional[PathLike] = None) -> Reading:
    """Ask every video output whether anything is plugged into it.

    Any single connected output is a display: a box with HDMI and DisplayPort
    where only one is in use still has a television on it.
    """
    base = Path(root) if root is not None else DRM_ROOT
    try:
        entries = sorted(base.iterdir())
    except OSError:
        # No /sys/class/drm at all: an unusual kernel, a container, or a
        # machine that is not the box. Not a reason to switch anything off.
        return Reading(UNKNOWN, (), f"{base} could not be read")

    connectors: List[Connector] = []
    for entry in entries:
        status = _read_status(entry)
        if status is None:
            continue
        connectors.append(Connector(entry.name, status))

    if not connectors:
        return Reading(UNKNOWN, (), f"no video outputs found under {base}")

    if any(c.status == _CONNECTED for c in connectors):
        connected = [c.name for c in connectors if c.status == _CONNECTED]
        return Reading(PRESENT, tuple(connectors), "connected: " + ", ".join(connected))

    unsure = [c for c in connectors if c.status != _DISCONNECTED]
    if unsure:
        return Reading(
            UNKNOWN,
            tuple(connectors),
            "could not read: " + ", ".join(c.name for c in unsure),
        )

    return Reading(ABSENT, tuple(connectors), "nothing connected to any output")


# ==========================================================================
# What we are allowed to say about it
# ==========================================================================
#: Shown wherever the state is. Deliberately admits the limit of the method
#: rather than letting somebody conclude their television is being watched
#: more closely than it is.
CAVEAT = (
    "Some televisions keep the HDMI connection alive while on standby and only "
    "drop it when switched off at the wall, and some drop it as soon as the "
    "screen goes dark. So this notices an unplugged cable or a set switched off "
    "at the wall, and notices standby on some sets but not others."
)

_DESCRIPTIONS = {
    PRESENT: "A television is connected.",
    ABSENT: "Nothing is connected to the video output.",
    UNKNOWN: "Cannot tell whether a television is connected, so the box stays awake.",
}


def describe(state: str) -> str:
    """Plain language for somebody who is not technical and is watching TV."""
    return _DESCRIPTIONS.get(state, _DESCRIPTIONS[UNKNOWN])


# ==========================================================================
# Deciding when to believe it
# ==========================================================================
# The two directions are deliberately not symmetrical, and the asymmetry is
# the whole of the user experience.
#
# Going to sleep is debounced because the hotplug line is not clean. HDMI
# switches, AV receivers and plenty of televisions drop and re-assert it while
# they power up, change input or renegotiate HDCP, and each of those flaps
# looks exactly like somebody switching the set off. Acting on the first one
# means the picture dies in the middle of an input change. Waiting a few
# seconds costs nothing at all: nobody is watching, by definition.
#
# Waking is not debounced, because somebody has just switched their television
# on and is staring at a black screen. Every second of debounce is a second of
# them wondering whether the box is broken. So the default is zero and the
# wake path takes precedence over everything else here.
#
# And thrash is worse than either. A box that sleeps and wakes repeatedly
# surges its fan and blinks the picture, which is more annoying than one that
# never slept at all - so once it wakes it refuses to sleep again for a while.
# That hold only ever delays sleeping. It must never delay a wake.
SLEEP_DEBOUNCE_SECONDS = 8.0
WAKE_DEBOUNCE_SECONDS = 0.0
MIN_AWAKE_SECONDS = 60.0

#: How long a hold lasts if whoever asked does not say how long. Short on
#: purpose: a hold is a heartbeat, not a switch, because a browser tab closed
#: on a train never gets to release anything.
HOLD_SECONDS = 30.0


def wants_awake(
    *,
    holds: bool,
    wire_awake: bool,
    set_is_off: Optional[bool] = None,
) -> bool:
    """THE arbitration. There is one of these on purpose.

    "Should this box be awake?" is asked in two places - by the watcher, which
    knows about the cable and about holds, and by the television process,
    which also knows what HDMI-CEC heard the set say. Two implementations of
    one question drift, and the drift shows up as a box that goes quiet when
    it should not have. So both go through here.

    The precedence, once, in the order it is written:

    * ``holds`` outranks everything. Something watching another way - a
      browser, one day, or somebody who has just pressed Wake - keeps the box
      awake whatever any wire says.
    * ``set_is_off`` is the television's own word and outranks the cable in
      both directions, because a set in standby very commonly keeps the
      hotplug line asserted and the cable cannot see the difference. ``None``
      means the set has not spoken - no adapter, or one it has never talked
      to - and silence is ignored completely rather than read as either
      answer.
    * ``wire_awake`` is what is left: the cable, after debouncing. Anything
      other than a confirmed absence counts as awake, because cannot-tell is
      never permission to switch somebody's picture off.
    """
    if holds:
        return True
    if set_is_off is not None:
        return not set_is_off
    return wire_awake


class PresenceFilter:
    """Turns raw readings into a state the box is willing to act on.

    Kept apart from the watcher so the decisions can be tested against a fake
    clock, one observation at a time, with no threads and no sockets.
    """

    def __init__(
        self,
        *,
        initial: str = UNKNOWN,
        sleep_after: float = SLEEP_DEBOUNCE_SECONDS,
        wake_after: float = WAKE_DEBOUNCE_SECONDS,
        min_awake: float = MIN_AWAKE_SECONDS,
        now: float = 0.0,
    ) -> None:
        self._state = initial if initial in STATES else UNKNOWN
        self._sleep_after = max(0.0, float(sleep_after))
        self._wake_after = max(0.0, float(wake_after))
        self._min_awake = max(0.0, float(min_awake))
        self._changed_at = now
        # When the box was last in a state that is not "asleep", for the
        # anti-thrash hold. A box that starts up absent has never been awake,
        # so the clock starts now either way.
        self._awake_since = now
        # A state seen but not yet believed: (state, when it was first seen).
        self._pending: Optional[Tuple[str, float]] = None
        self._holds: Dict[str, float] = {}
        # Everything here can be touched from the watcher thread and read from
        # the app loop or a dashboard request at the same time.
        self._lock = threading.RLock()

    # -- what we currently believe -----------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def changed_at(self) -> float:
        return self._changed_at

    def should_be_awake(self, now: float) -> bool:
        """Half of the question, answered through the one arbitration.

        This half is the cable and the holds - everything this module can see.
        It has no opinion about the television's own word, because nothing in
        here can hear HDMI-CEC; the television process adds that and asks
        :func:`wants_awake` again. Which is why the answer is not computed
        here: see that function for the whole ladder and for why there is only
        one of it.
        """
        with self._lock:
            return wants_awake(
                holds=bool(self._live_holds(now)),
                wire_awake=self._state != ABSENT,
            )

    # -- feeding it --------------------------------------------------------
    def observe(self, state: str, now: float) -> Optional[str]:
        """Report one reading; returns the new state only when it changes."""
        if state not in STATES:
            state = UNKNOWN
        with self._lock:
            if state == self._state:
                # Back to where we were - abandon any countdown, and do not
                # let its elapsed time count towards the next one.
                self._pending = None
                return None

            if self._pending is None or self._pending[0] != state:
                self._pending = (state, now)

            if now + _EPSILON < self._due_at(state, self._pending[1]):
                return None

            self._state = state
            self._changed_at = now
            self._pending = None
            if state != ABSENT:
                self._awake_since = now
            log.info("display state -> %s", state)
            return state

    def deadline(self) -> Optional[float]:
        """When a pending change would become believable, so a watcher can
        wait exactly that long instead of waking up to check."""
        with self._lock:
            if self._pending is None:
                return None
            state, since = self._pending
            return self._due_at(state, since)

    def _due_at(self, state: str, since: float) -> float:
        if state != ABSENT:
            return since + self._wake_after
        return max(since + self._sleep_after, self._awake_since + self._min_awake)

    # -- the seam for anything else that needs the box awake ---------------
    # Nothing calls this yet. There is no live stream viewer - that work
    # stopped at a hardware gate - so this is the hook it will use rather
    # than a claim that anything is talking to us. Somebody watching in a
    # browser with the television off must not have the box go quiet
    # underneath them.
    def hold_awake(self, token: str, now: float, seconds: Optional[float] = None) -> float:
        """Keep the box awake for ``seconds``, and return when that runs out.

        Calling again with the same token extends the same hold rather than
        stacking a second one up, so a heartbeat is just the same call again.
        """
        expiry = now + (HOLD_SECONDS if seconds is None else max(0.0, float(seconds)))
        with self._lock:
            self._holds[token] = expiry
        return expiry

    def release_hold(self, token: str) -> None:
        """Give up a hold early. Harmless if there was never one."""
        with self._lock:
            self._holds.pop(token, None)

    def holds(self, now: float) -> Tuple[str, ...]:
        """Who is currently holding the box awake."""
        with self._lock:
            return self._live_holds(now)

    def _live_holds(self, now: float) -> Tuple[str, ...]:
        expired = [token for token, until in self._holds.items() if until <= now]
        for token in expired:
            del self._holds[token]
        return tuple(sorted(self._holds))


# ==========================================================================
# Being told, rather than asking over and over
# ==========================================================================
#: What the watcher is honestly doing. These two words end up in front of the
#: owner, so a poll is never allowed to describe itself as the other one.
MODE_EVENTS = "events"
MODE_POLL = "poll"
MODE_STOPPED = "stopped"

#: Only used when the kernel feed could not be opened. Deliberately slow: this
#: is the degraded mode, and a box checking every second is the waste the
#: whole feature exists to remove. Half a minute late to go quiet is fine.
POLL_INTERVAL_SECONDS = 30.0

#: In event mode the box still looks of its own accord occasionally. Not a
#: poll - it is a safety net for a uevent lost to a full socket buffer, which
#: would otherwise leave the box wrong about the television indefinitely. Five
#: minutes of that costs one sysfs read.
IDLE_RECHECK_SECONDS = 300.0

_NETLINK_KOBJECT_UEVENT = 15
#: Group 1 is the kernel's own uevent broadcast and generally needs privilege.
#: Group 2 is systemd-udevd's rebroadcast of the same events, which an
#: unprivileged process is allowed to hear - and the box's services run as an
#: ordinary user. Binding to both means whichever is permitted is the one used.
_KERNEL_GROUP = 1
_UDEV_GROUP = 2
_RECV_BUFFER_BYTES = 1 << 20
_MESSAGE_BYTES = 8192


def is_drm_event(payload: bytes) -> bool:
    """Is this uevent about the graphics subsystem?

    Both message formats - the kernel's own and the header-prefixed one
    systemd-udevd rebroadcasts - end in the same NUL-separated ``KEY=value``
    properties, so looking for the property itself reads both without having
    to parse either header. Exact match, because ``drm_dp_aux_dev`` is a
    different subsystem that chatters during handshakes.
    """
    try:
        return b"SUBSYSTEM=drm" in payload.split(b"\0")
    except Exception:  # noqa: BLE001 - rubbish on a socket is not a crash
        return False


def _open_uevent_socket() -> Tuple[Optional[object], str]:
    """The kernel's hotplug feed, or None and the reason why not."""
    family = getattr(socket, "AF_NETLINK", None)
    if family is None:
        # A Mac, a BSD, or anything else that is not the box.
        return None, "this platform has no netlink sockets"

    last = ""
    for groups, what in (
        (_KERNEL_GROUP | _UDEV_GROUP, "kernel and udev uevents"),
        (_UDEV_GROUP, "udev uevents"),
    ):
        sock = None
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM, _NETLINK_KOBJECT_UEVENT)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _RECV_BUFFER_BYTES)
            except OSError:
                # A smaller buffer only risks a dropped event, and the idle
                # recheck is there precisely for that. Not worth giving up on.
                pass
            sock.bind((0, groups))
            return sock, what
        except OSError as exc:
            last = str(exc)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    return None, f"the netlink socket was refused ({last})"


class EventSource:
    """Something that says "go and look again", and can be interrupted."""

    mode = MODE_POLL
    detail = ""

    def wait(self, timeout: Optional[float]) -> bool:
        """Block for up to ``timeout`` seconds. True if something happened."""
        raise NotImplementedError

    def interrupt(self) -> None:
        """Return from :meth:`wait` now, because the box is shutting down."""

    def close(self) -> None:
        """Release whatever was opened."""


class UeventSource(EventSource):
    """The kernel telling us, over an AF_NETLINK socket, that DRM changed."""

    mode = MODE_EVENTS

    def __init__(self, sock, detail: str = "") -> None:
        self._sock = sock
        self.detail = detail
        # A pipe purely so stopping the box does not have to wait out a five
        # minute select. Shutdown has to be prompt: this thread stands between
        # systemd and a clean stop.
        self._wake_r, self._wake_w = os.pipe()
        self._closed = False

    def wait(self, timeout: Optional[float]) -> bool:
        readable, _, _ = select.select([self._sock, self._wake_r], [], [], timeout)
        if self._wake_r in readable:
            try:
                os.read(self._wake_r, 4096)
            except OSError:
                pass
        if self._sock not in readable:
            return False
        return self._drain()

    def _drain(self) -> bool:
        """Swallow the whole burst. A television powering up emits several
        events in a row and they are all one question: what is out there now?"""
        changed = False
        while True:
            try:
                payload = self._sock.recv(_MESSAGE_BYTES)
            except OSError:
                # Usually a full buffer, which means events were missed - so
                # the honest response is to go and look, not to give up.
                return True
            if is_drm_event(payload):
                changed = True
            more, _, _ = select.select([self._sock], [], [], 0)
            if not more:
                return changed

    def interrupt(self) -> None:
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for closer in (
            lambda: self._sock.close(),
            lambda: os.close(self._wake_r),
            lambda: os.close(self._wake_w),
        ):
            try:
                closer()
            except OSError:
                pass


class PollSource(EventSource):
    """The degraded mode: no kernel feed, so look again every so often.

    It exists so that a box without netlink still goes quiet eventually,
    rather than the feature silently doing nothing. It never claims to be the
    other mode.
    """

    mode = MODE_POLL

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        self._wake = threading.Event()

    def wait(self, timeout: Optional[float]) -> bool:
        interrupted = self._wake.wait(timeout)
        self._wake.clear()
        return not interrupted  # timed out: it is time to go and look

    def interrupt(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._wake.set()


def open_event_source() -> EventSource:
    """The kernel feed if it can be had, a slow poll if it cannot."""
    sock, detail = _open_uevent_socket()
    if sock is None:
        log.info("no kernel uevent feed (%s); falling back to a slow poll", detail)
        return PollSource(f"no kernel event feed ({detail}); checking on a timer instead")
    log.info("watching for display changes via %s", detail)
    return UeventSource(sock, detail)


# ==========================================================================
# What the app layer and the dashboard are handed
# ==========================================================================
@dataclass(frozen=True)
class DisplaySnapshot:
    """Everything anybody needs to know, taken at one instant."""

    state: str
    awake: bool
    mode: str
    mode_detail: str
    detail: str
    connectors: Tuple[Connector, ...] = ()
    holds: Tuple[str, ...] = ()
    changed_at: float = 0.0

    def as_dict(self) -> Dict[str, object]:
        """Plain types, for the dashboard's JSON and for its template."""
        return {
            "state": self.state,
            "awake": self.awake,
            "description": describe(self.state),
            "caveat": CAVEAT,
            "mode": self.mode,
            "mode_detail": self.mode_detail,
            "detail": self.detail,
            "connectors": [{"name": c.name, "status": c.status} for c in self.connectors],
            "holds": list(self.holds),
            "changed_at": self.changed_at,
        }


class DisplayWatcher:
    """Watches the video outputs and says when the answer really changed.

    Everything it touches is injectable - the sysfs root, the reader, the
    event source and the clock - because none of them exist on the machine
    the suite runs on, and because a box in somebody's living room is not a
    place to find out that one of them behaves differently than assumed.
    """

    def __init__(
        self,
        *,
        root: Optional[PathLike] = None,
        source: Optional[EventSource] = None,
        source_factory: Callable[[], EventSource] = open_event_source,
        reader: Callable[..., Reading] = read_state,
        on_change: Optional[Callable[[DisplaySnapshot], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep_after: float = SLEEP_DEBOUNCE_SECONDS,
        wake_after: float = WAKE_DEBOUNCE_SECONDS,
        min_awake: float = MIN_AWAKE_SECONDS,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        idle_recheck: float = IDLE_RECHECK_SECONDS,
        initial: str = UNKNOWN,
    ) -> None:
        self._root = root
        self._reader = reader
        self._source = source
        self._source_factory = source_factory
        self._clock = clock
        #: Called on the watcher's own thread whenever the believed state
        #: changes. The app layer must treat it like a player callback: put
        #: the snapshot on its queue and act on its own loop, never do work
        #: here, and never assume it arrives on the main thread.
        self.on_change = on_change
        self.presence = PresenceFilter(
            initial=initial,
            sleep_after=sleep_after,
            wake_after=wake_after,
            min_awake=min_awake,
            now=clock(),
        )
        self._poll_interval = max(1.0, float(poll_interval))
        self._idle_recheck = max(0.0, float(idle_recheck)) or None
        self._reading = Reading(initial, (), "the outputs have not been read yet")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Whether the source is ours to close and forget, or the caller's.
        self._opened_here = False

    # -- what it currently thinks ------------------------------------------
    @property
    def state(self) -> str:
        return self.presence.state

    @property
    def mode(self) -> str:
        source = self._source
        return source.mode if source is not None else MODE_STOPPED

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def should_be_awake(self) -> bool:
        return self.presence.should_be_awake(self._clock())

    def snapshot(self) -> DisplaySnapshot:
        now = self._clock()
        reading = self._reading
        source = self._source
        return DisplaySnapshot(
            # The state is the *believed* one, after debouncing; the
            # connectors are the raw last reading behind it. Those two can
            # disagree for a few seconds while a change settles, and showing
            # both is how somebody sees that happening.
            state=self.presence.state,
            awake=self.presence.should_be_awake(now),
            mode=self.mode,
            mode_detail=source.detail if source is not None else "not started",
            detail=reading.detail,
            connectors=reading.connectors,
            holds=self.presence.holds(now),
            changed_at=self.presence.changed_at,
        )

    # -- holds, for anything that needs the box awake without a television --
    def hold_awake(self, token: str, seconds: Optional[float] = None) -> float:
        return self.presence.hold_awake(token, self._clock(), seconds)

    def release_hold(self, token: str) -> None:
        self.presence.release_hold(token)

    # -- doing the watching -------------------------------------------------
    def refresh(self) -> Optional[str]:
        """Read the outputs now. Returns the new state, or None if unchanged."""
        try:
            reading = self._reader(self._root)
        except Exception:  # noqa: BLE001 - a failed read is a cannot-tell
            log.warning("could not read the video outputs", exc_info=True)
            reading = Reading(UNKNOWN, (), "the video outputs could not be read")
        self._reading = reading

        changed = self.presence.observe(reading.state, self._clock())
        if changed is None:
            return None
        callback = self.on_change
        if callback is not None:
            try:
                callback(self.snapshot())
            except Exception:  # noqa: BLE001 - video first, always
                log.exception("display change callback failed")
        return changed

    def poll_once(self) -> Optional[str]:
        """Wait to be told (or for a pending debounce), then look."""
        source = self._ensure_source()
        try:
            source.wait(self._next_timeout())
        except Exception as exc:  # noqa: BLE001 - see below; this is the safety net
            # DELIBERATELY EVERYTHING, not just OSError. select() on a socket
            # somebody else has closed raises ValueError, and anything that
            # escapes this line goes out of poll_once into the thread's
            # catch-all - which means refresh() is never reached again and the
            # believed state freezes at whatever it last was. Frozen at
            # "absent" is a picture paused for ever, on a box with no SSH.
            # Falling back to a timer is always survivable; freezing is not.
            self._degrade(exc)
        return self.refresh()

    def start(self) -> None:
        """Begin watching on a background thread."""
        if self.running:
            return
        self._stop.clear()
        self._ensure_source()
        # Read once up front so the app never has to wait for an event to
        # find out what is out there.
        self.refresh()
        self._thread = threading.Thread(
            target=self._run, name="display-watch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        source = self._source
        if source is not None:
            source.interrupt()
        thread = self._thread
        came_back = True
        if thread is not None:
            thread.join(timeout=2.0)
            came_back = not thread.is_alive()
            if came_back:
                self._thread = None
        # Only now, and only if the thread has actually come back. Closing a
        # socket while that thread is still inside select() on it is the bug,
        # not a tidy-up: the next select raises ValueError, poll_once stops
        # reaching refresh(), and the watcher freezes believing whatever it
        # believed last - which, frozen at "absent", is a picture paused for
        # ever on a box nobody can log into. A descriptor left open on a box
        # that is stopping anyway costs nothing by comparison. Re-read the
        # attribute rather than reusing the one interrupted above, because a
        # mid-run degrade may have replaced it in the meantime.
        source = self._source
        if source is not None and came_back:
            source.close()
        elif source is not None:
            log.warning(
                "the display watcher thread has not come back; leaving its "
                "event feed open rather than closing one it may still be reading"
            )
        if self._opened_here and came_back:
            # A closed source must not be handed to a second start(): the box
            # would come back up holding a dead socket and never hear about
            # the television again. One we were given belongs to the caller.
            self._source = None
            self._opened_here = False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - never take the box down
                log.exception("display watcher stumbled")
                # And never spin hot on a repeating failure: on a two-core
                # box that would cost more than the feature saves.
                self._stop.wait(self._poll_interval)

    def _ensure_source(self) -> EventSource:
        if self._source is None:
            self._source = self._source_factory()
            self._opened_here = True
        return self._source

    def _degrade(self, exc: Exception) -> None:
        """The kernel feed failed: fall back to a slow poll, not to nothing."""
        log.warning("the kernel display event feed failed (%s); polling instead", exc)
        old = self._source
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                log.debug("closing the failed event source failed", exc_info=True)
        self._source = PollSource(
            f"the kernel event feed failed ({exc}); checking on a timer instead"
        )

    def _next_timeout(self) -> Optional[float]:
        """How long to wait before looking again, if nothing tells us to.

        In event mode that is the safety net, or the rest of a debounce that
        is already counting down - whichever comes first.
        """
        base = self._poll_interval if self.mode == MODE_POLL else self._idle_recheck
        deadline = self.presence.deadline()
        if deadline is None:
            return base
        remaining = max(0.0, deadline - self._clock())
        return remaining if base is None else min(base, remaining)


# ==========================================================================
# Running it by hand, which is the only place any of this can be proven
# ==========================================================================
# None of this can be checked on a developer's Mac: there is no /sys, no DRM
# and no netlink there. So the box itself needs to be able to answer, in one
# command, "what do you see, and are you being told or are you guessing?".
#
#     python3 -m retrobox.display            # one look
#     python3 -m retrobox.display --watch    # follow it while switching the TV
def main(argv: Optional[List[str]] = None) -> int:
    watching = "--watch" in (argv if argv is not None else sys.argv[1:])
    source = open_event_source()
    watcher = DisplayWatcher(source=source)
    watcher.refresh()
    _print_snapshot(watcher.snapshot())
    print()
    print(CAVEAT)
    if not watching:
        watcher.stop()
        return 0

    print()
    print("Watching. Switch the television off, then on, and see what appears.")
    try:
        while True:
            if watcher.poll_once() is not None:
                _print_snapshot(watcher.snapshot())
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
    return 0


def _print_snapshot(snapshot: DisplaySnapshot) -> None:
    print(f"state:  {snapshot.state} - {describe(snapshot.state)}")
    print(f"awake:  {'yes' if snapshot.awake else 'no'}")
    print(f"how:    {snapshot.mode} ({snapshot.mode_detail})")
    print(f"why:    {snapshot.detail}")
    for connector in snapshot.connectors:
        print(f"  {connector.name}: {connector.status}")


__all__ = [
    "ABSENT",
    "CAVEAT",
    "Connector",
    "DRM_ROOT",
    "DisplaySnapshot",
    "DisplayWatcher",
    "EventSource",
    "HOLD_SECONDS",
    "MODE_EVENTS",
    "MODE_POLL",
    "MODE_STOPPED",
    "PRESENT",
    "PollSource",
    "PresenceFilter",
    "Reading",
    "STATES",
    "UNKNOWN",
    "UeventSource",
    "describe",
    "is_drm_event",
    "open_event_source",
    "main",
    "read_state",
    "wants_awake",
]


if __name__ == "__main__":
    raise SystemExit(main())
