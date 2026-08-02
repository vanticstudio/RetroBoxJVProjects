"""HDMI-CEC: the TV's own remote, and the TV's own word on whether it is on.

Many TVs can forward remote button presses to attached HDMI devices over CEC
(Samsung "Anynet+", LG "SimpLink", Sony "BRAVIA Sync", etc.). The easiest way
to receive those on Linux is libCEC's ``cec-client`` utility, which prints a
line like ``key pressed: up (1)`` for every button. Note that Intel machines
have no CEC hardware, so this needs a USB-CEC adapter; the backend is skipped
silently when nothing is listening. This backend spawns
``cec-client`` and turns those lines into actions - so you can drive the box with
the TV remote already in your hand, no separate remote required.

The *same stream* also carries the television's power state, and that is worth
having: CEC is the only signal on the box that actually KNOWS the screen is
off, because it is the television saying so rather than a wire being guessed
at. A television in standby very often keeps HDMI hotplug asserted, so hotplug
cannot tell you the room is empty; a CEC standby message can.

That makes CEC authoritative *when it is there*, and it usually is not. Almost
none of these boxes have a CEC adapter and most televisions ship with CEC
switched off, so this is an upgrade path and never a requirement. Everything
here is therefore built around keeping three things apart:

* **absent**  - no adapter, no cec-client, or the reader has stopped. Normal.
* **unknown** - listening, but the television has not said anything yet.
* **on / standby** - the television has actually said.

Only ``standby`` is allowed to make anything go quiet. "Absent" and "unknown"
are silence, and silence must never be mistaken for a dark screen: the failure
that matters here is blanking the picture of somebody who is watching
television.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Optional

from .base import InputBackend
from .keymap import cec_key_to_event

log = logging.getLogger(__name__)

_KEY_PRESSED_RE = re.compile(r"key pressed:\s*(.+?)\s*(?:\(|$)", re.IGNORECASE)

#: One incoming CEC frame as ``cec-client -d 8`` prints it, e.g.
#: ``TRAFFIC: [   8000]  >> 01:90:00``. The ``>>`` matters: ``<<`` is this box
#: talking, and what we say about power is not evidence about the television.
#: The first byte is initiator/destination nibbles, the second the opcode, and
#: anything after that the parameters.
_TRAFFIC_RE = re.compile(
    r">>\s+([0-9a-f]{2})((?::[0-9a-f]{2})*)\s*$", re.IGNORECASE
)

#: Longest line we will look at. cec-client frames are a few dozen characters;
#: anything vastly longer is a runaway or a broken pipe, not a message, and
#: pulling a "standby" out of the tail of a megabyte of noise would put the
#: room to sleep for no reason. Truncating also keeps a chatty adapter from
#: turning into real work on a two-core Celeron.
_MAX_LINE = 512

#: The television. Logical address 0 is the display, always.
_TV = 0x0
#: The "everybody" destination.
_BROADCAST = 0xF

# CEC opcodes we understand. Everything else is ignored on purpose.
_OP_STANDBY = 0x36           # "go to sleep" - broadcast as the TV switches off
_OP_REPORT_POWER = 0x90      # answer to "give device power status"
_OP_ROUTING_CHANGE = 0x80    # the TV moved between inputs, so it is lit
_OP_ACTIVE_SOURCE = 0x82     # somebody is on screen, so the TV is showing it
_OP_REQUEST_ACTIVE = 0x85    # the TV asks who is live - it does this on waking
_OP_SET_STREAM_PATH = 0x86   # the TV selecting an input

#: Parameter byte of "report power status". 0x02 is on its way up and 0x03 on
#: its way down; we round each towards where it is heading.
_POWER_VALUES = {0x00: True, 0x01: False, 0x02: True, 0x03: False}

#: How many state changes in a minute stop being worth writing down. A
#: television changing its mind a dozen times a minute is a broken adapter or a
#: noisy bus, not a household, and this box logs to eMMC that wears out. Past
#: this the state still changes and anyone watching is still told - the screen
#: must always be able to come back - it just stops filling the journal.
_LOG_CHANGES_PER_MINUTE = 12
_LOG_WINDOW = 60.0

#: Button names that mean "power" in some form. A television commonly forwards
#: the power key on its way *into* standby, so this one press is not evidence
#: that anybody is watching. Every other button is.
_POWER_KEYS = ("power", "standby")

# The four values display_power() can report.
CEC_ABSENT = "absent"
CEC_UNKNOWN = "unknown"
CEC_ON = "on"
CEC_STANDBY = "standby"


@dataclass(frozen=True)
class CecPower:
    """What HDMI-CEC currently knows about the television's power.

    ``at`` is the clock reading when the state last *changed*, so a caller can
    tell a standby heard a moment ago from one heard before the last power cut.
    ``detail`` is short plain words for a log line or the dashboard.
    """

    state: str
    at: Optional[float] = None
    detail: str = ""

    @property
    def says_off(self) -> bool:
        """True only when the television has actually said it is in standby."""
        return self.state == CEC_STANDBY

    @property
    def is_known(self) -> bool:
        """True when this is the television's word rather than silence."""
        return self.state in (CEC_ON, CEC_STANDBY)


class CecBackend(InputBackend):
    """Reads TV-remote button presses, and TV power state, over HDMI-CEC."""

    name = "cec"

    def __init__(
        self,
        *,
        binary: str = "cec-client",
        osd_name: str = "Retro Box",
        extra_args: Optional[List[str]] = None,
        power_observer: Optional[Callable[[CecPower], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._binary = binary
        self._osd_name = osd_name
        self._extra_args = list(extra_args) if extra_args else []
        self._proc: Optional[subprocess.Popen] = None
        self._clock = clock
        # Power is read from other threads (the display logic, the dashboard)
        # while this backend's reader thread writes it, so it is guarded. The
        # value itself is immutable, so a reader only ever sees a whole state.
        self._power_lock = threading.Lock()
        self._power = CecPower(CEC_ABSENT, None, "HDMI-CEC is not running")
        self._power_observer = power_observer
        # Journal protection only - see _LOG_CHANGES_PER_MINUTE.
        self._log_window_start: Optional[float] = None
        self._log_window_changes = 0

    @staticmethod
    def is_available(binary: str = "cec-client") -> bool:
        return shutil.which(binary) is not None

    # -- what the television says about its own power ------------------------
    def display_power(self) -> CecPower:
        """The television's power state as CEC last reported it."""
        with self._power_lock:
            return self._power

    def watch_power(self, callback: Callable[[CecPower], None]) -> None:
        """Be told whenever the television's power state changes.

        ``create_backends`` builds this backend, so whatever cares about the
        screen never gets to pass a callback to the constructor - it has to be
        able to ask afterwards. The callback is handed the state as it stands
        straight away, because a watcher arriving after the television has
        already said standby would otherwise wait hours for a change.

        Polling :meth:`display_power` works just as well and needs no wiring;
        this only exists so the box can react the moment the television speaks
        rather than on the next tick.
        """
        with self._power_lock:
            self._power_observer = callback
            power = self._power
        self._notify(power)

    def _notify(self, power: CecPower) -> None:
        observer = self._power_observer
        if observer is None:
            return
        # Whoever is listening is downstream of the television, never in front
        # of it. A broken watcher must not stop the remote working.
        try:
            observer(power)
        except Exception:  # noqa: BLE001
            log.debug("CEC power observer failed", exc_info=True)

    def _set_power(self, state: str, detail: str) -> None:
        """Record a new power state, and tell anyone watching if it changed.

        Repeats are dropped: a television that reports "on" a thousand times
        has not changed anything, and waking the rest of the box a thousand
        times over is how a cheap machine ends up thrashing.
        """
        with self._power_lock:
            if self._power.state == state:
                return
            now = self._clock()
            power = CecPower(state, now, detail)
            self._power = power
            if self._log_window_start is None or now - self._log_window_start >= _LOG_WINDOW:
                self._log_window_start = now
                self._log_window_changes = 0
            self._log_window_changes += 1
            noisy = self._log_window_changes > _LOG_CHANGES_PER_MINUTE
        if noisy:
            log.debug("HDMI-CEC: %s (%s)", state, detail)
        else:
            log.info("HDMI-CEC: %s (%s)", state, detail)
        self._notify(power)

    def _run(self) -> None:
        if not self.is_available(self._binary):
            # Not an error. Most boxes are like this.
            log.info("%s not found; HDMI-CEC input disabled", self._binary)
            return
        cmd = [
            self._binary,
            "-t", "p",            # register as a Playback device
            "-o", self._osd_name,  # the name the TV shows for this device
            "-d", "8",            # log level: include the key-press traffic
            *self._extra_args,
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            log.warning("could not start %s: %s", self._binary, exc)
            return

        log.info("HDMI-CEC input active via %s", self._binary)
        # An adapter is listening but the television has not said anything.
        # That is emphatically not the same as the television being off.
        self._set_power(CEC_UNKNOWN, "listening; the television has not said")
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                if self.stopping:
                    break
                self._handle_line(line)
        finally:
            # The reader has stopped - cec-client died, was stopped, or the
            # pipe broke. Whatever it last told us about the television is now
            # a guess, and holding on to "standby" would leave the box asleep
            # with nothing left that could ever wake it.
            self._set_power(CEC_ABSENT, "cec-client is no longer reporting")

    def _handle_line(self, line: str) -> None:
        # Truncate before doing anything else: see _MAX_LINE.
        line = line[:_MAX_LINE]
        try:
            self._read_power(line)
            self._read_key(line)
        except Exception:  # noqa: BLE001
            # One line in a shape nobody expected must not end the stream and
            # take the TV remote with it.
            log.debug("could not make sense of a CEC line", exc_info=True)

    def _read_key(self, line: str) -> None:
        match = _KEY_PRESSED_RE.search(line)
        if not match:
            return
        name = match.group(1)
        # The press goes first. Whatever is listening for the power state may
        # be slow, and the remote is the product.
        event = cec_key_to_event(name)
        if event is not None:
            self.emit(event)
        # A television in standby does not forward its remote to HDMI devices,
        # so a button is proof the screen is lit - with the one exception of
        # the power key, which arrives on the way to standby just as often.
        if not any(word in name.lower() for word in _POWER_KEYS):
            self._set_power(CEC_ON, f"the remote sent {name.strip()}")

    def _read_power(self, line: str) -> None:
        match = _TRAFFIC_RE.search(line)
        if not match:
            return
        header = int(match.group(1), 16)
        initiator = header >> 4
        destination = header & 0xF
        payload = [int(b, 16) for b in match.group(2).split(":") if b]
        if not payload:
            # A bare poll with no opcode. Televisions answer polls while fast
            # asleep, which is exactly why this says nothing about power.
            return
        opcode, params = payload[0], payload[1:]

        if opcode == _OP_STANDBY:
            # Either the television telling the room to sleep, or a broadcast
            # standby, which means the same thing. A directed standby between
            # two other devices is somebody else's business.
            if initiator == _TV or destination == _BROADCAST:
                self._set_power(CEC_STANDBY, "the television said standby")
            return

        if opcode == _OP_REPORT_POWER:
            # Only the television's own answer counts. A sound bar in standby
            # says nothing at all about the screen.
            if initiator != _TV or not params:
                return
            awake = _POWER_VALUES.get(params[0])
            if awake is True:
                self._set_power(CEC_ON, "the television reports power on")
            elif awake is False:
                self._set_power(CEC_STANDBY, "the television reports standby")
            return

        if opcode in (
            _OP_ROUTING_CHANGE,
            _OP_ACTIVE_SOURCE,
            _OP_REQUEST_ACTIVE,
            _OP_SET_STREAM_PATH,
        ):
            # None of these happen with a dark screen: the television is
            # switching inputs, asking who is live as it wakes, or showing
            # somebody. Reading these as "on" is the safe direction to be
            # generous in - the expensive mistake is going dark too eagerly.
            self._set_power(CEC_ON, f"traffic only a lit television sends (0x{opcode:02x})")

    def _close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except OSError:
            pass
        self._proc = None


def cec_display_power(backends: Iterable[Any]) -> CecPower:
    """What CEC says about the screen, given the input backends in use.

    The app layer holds an :class:`~retrobox.input.manager.InputManager` and
    not a CEC backend, and on nearly every box there is no CEC backend at all -
    so this answers ``absent`` for a box without one, which is exactly the same
    answer as an adapter that is not running. Callers then have one thing to
    read and three states to tell apart, rather than a search and a None.
    """
    for backend in backends:
        if isinstance(backend, CecBackend):
            return backend.display_power()
    return CecPower(CEC_ABSENT, None, "this box has no HDMI-CEC adapter")


__all__ = [
    "CecBackend",
    "CecPower",
    "cec_display_power",
    "CEC_ABSENT",
    "CEC_UNKNOWN",
    "CEC_ON",
    "CEC_STANDBY",
]
