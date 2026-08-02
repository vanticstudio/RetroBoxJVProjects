"""Control the TV from the web dashboard, over a local Unix socket.

This is an input backend exactly like the Flirc/evdev one, the CEC one and the
stdin one: it reads from a source, translates what it reads into
:class:`~retrobox.actions.InputEvent` values, and pushes them onto the shared
queue. Everything downstream - ``app.handle_event`` and the whole state machine
- cannot tell a click in a browser from a button on the remote, which is the
point. There is no second command path.

The socket is a local filesystem socket, not a network one. The web server
(a separate process, running as the same user) connects and writes a line; the
network-facing surface is Flask's, not this.

Commands, one per connection, newline terminated::

    channel_up | channel_down | volume_up | volume_down | mute
    info | guide | menu | last | sleep | power | shutdown | reload
    channel <number>
    crt_preview <setting>=<value> [<setting>=<value> ...]
    crt_cancel
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..actions import Action, CrtSettings, InputEvent
from ..status import control_socket_path
from .base import InputBackend

log = logging.getLogger(__name__)

# Single-word commands that map straight onto an existing action.
_SIMPLE = {
    "channel_up": Action.CHANNEL_UP,
    "channel_down": Action.CHANNEL_DOWN,
    "volume_up": Action.VOLUME_UP,
    "volume_down": Action.VOLUME_DOWN,
    "mute": Action.MUTE,
    "info": Action.INFO,
    "guide": Action.GUIDE,
    "menu": Action.MENU,
    "last": Action.LAST_CHANNEL,
    "sleep": Action.SLEEP,
    "power": Action.POWER,
    "shutdown": Action.SHUTDOWN,
    # Not a remote button: the dashboard sends this after it rewrites
    # config.yaml, so the running TV picks the change up without a restart.
    "reload": Action.RELOAD,
    # Not remote buttons either. A box built on a bench with no screen has no
    # HDMI socket to choose, so it finds one at every start - and these two
    # let the dashboard ask for that again, and prove the answer out loud,
    # without anybody being sent to a terminal.
    "audio_setup": Action.AUDIO_SETUP,
    "test_tone": Action.TEST_TONE,
    # Not a remote button either. The box pauses the picture when it can see
    # that no television is watching; this brings it back, and holds it awake
    # for a while, for when that detection got it wrong. It is also the door a
    # live-stream viewer will hold the box awake through, sent over and over
    # as a heartbeat - so it is deliberately safe to send at any time, whether
    # the box had gone quiet or not.
    "wake": Action.WAKE,
    # Also not a remote button. Throw away whatever is being previewed and put
    # the last SAVED picture settings back - the dashboard's Cancel, and the
    # thing the box does for itself when a dashboard stops talking to it.
    "crt_cancel": Action.CRT_CANCEL,
}

#: The picture settings a preview may touch, and the range each one is allowed.
#:
#: Ranges are the same ones :func:`retrobox.config._parse_crt` enforces, and
#: they are here for the same reason they are there: a curvature of 40 is not a
#: picture, it is a headache, and this line arrives as untrusted text from an
#: unauthenticated socket. Out of range is REFUSED rather than clamped -
#: clamping is guessing what somebody meant, and a preview that silently shows
#: a different number from the one sent is worse than one that shows nothing.
_CRT_RANGES: Dict[str, Tuple[float, float]] = {
    "curvature": (0.0, 0.5),
    "corner_radius": (0.0, 0.3),
    "vignette": (0.0, 1.0),
    "scanline_intensity": (0.0, 1.0),
}

#: The on/off settings, and every word this box will accept for each answer.
#: A closed list, so "maybe" is refused rather than read as one or the other.
_CRT_FLAGS = ("enabled", "scanlines")
_TRUTH = {"on": True, "true": True, "yes": True, "1": True,
          "off": False, "false": False, "no": False, "0": False}


def _parse_crt_preview(parts: List[str]) -> List[InputEvent]:
    """Read ``curvature=0.23 scanlines=off`` into one preview event.

    Returns an empty list - the parser's "no" - on anything at all wrong with
    the line: an unknown setting, a value that is not a number, a number
    outside its range, a word that is neither yes nor no, the same setting
    given twice, or no settings at all. There is no partial success: half of a
    line somebody sent is not a picture anybody asked for.
    """
    if not parts:
        return []

    fields: Dict[str, object] = {}
    for token in parts:
        name, sep, raw = token.partition("=")
        if not sep or not name or not raw:
            return []
        if name in fields:                       # which of the two did they mean?
            return []
        if name in _CRT_FLAGS:
            if raw not in _TRUTH:
                return []
            fields[name] = _TRUTH[raw]
        elif name in _CRT_RANGES:
            try:
                value = float(raw)
            except ValueError:
                return []
            low, high = _CRT_RANGES[name]
            # Not-a-number fails this comparison rather than passing it, which
            # is exactly what we want: nan is not a curvature.
            if not low <= value <= high:
                return []
            fields[name] = value
        else:
            return []

    return [InputEvent(Action.CRT_PREVIEW, crt=CrtSettings(**fields))]


def parse_command(line: str) -> List[InputEvent]:
    """Turn one command line into the events a remote would have produced.

    ``channel 12`` becomes digit-1, digit-2, enter - the same sequence typing
    it on the number pad produces - rather than a bespoke "tune" path.

    Two commands are not keypresses, because no remote has the button:
    ``reload`` (the dashboard rewrote config.yaml) and ``crt_preview`` (the
    dashboard is dragging a slider and wants to see it on the television
    before anybody commits to it). ``crt_preview`` is the only command that
    carries values, so it is the only one that needs validating beyond its
    name - see :func:`_parse_crt_preview`.
    """
    parts = line.strip().lower().split()
    if not parts:
        return []

    verb = parts[0]
    if verb in _SIMPLE:
        # A bare word means a bare word. "mute now" is not a command this box
        # has, and running it as "mute" would be guessing.
        return [InputEvent(_SIMPLE[verb])] if len(parts) == 1 else []

    if verb == "channel" and len(parts) == 2 and parts[1].isdigit():
        digits = parts[1].lstrip("0") or "0"
        if len(digits) > 3:  # the app's own entry buffer keeps 3
            return []
        return [InputEvent.digit(int(d)) for d in digits] + [InputEvent(Action.ENTER)]

    if verb == "crt_preview":
        return _parse_crt_preview(parts[1:])

    log.debug("ignoring unknown control command: %r", line)
    return []


class WebBackend(InputBackend):
    """Listens on a Unix socket for commands from the web dashboard."""

    name = "web"

    def __init__(self, socket_path: Optional[Path] = None) -> None:
        super().__init__()
        self._path = Path(socket_path) if socket_path else control_socket_path()
        self._server: Optional[socket.socket] = None

    @staticmethod
    def is_available() -> bool:
        # AF_UNIX is absent on some Windows builds; the rest of the box needs
        # Linux anyway, but the check keeps dev machines honest.
        return hasattr(socket, "AF_UNIX")

    @property
    def socket_path(self) -> Path:
        return self._path

    def _run(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A socket left behind by a crash would make bind() fail.
        if self._path.exists():
            try:
                self._path.unlink()
            except OSError:
                log.warning("could not clear stale socket %s", self._path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.settimeout(0.5)  # so stop() is noticed promptly
        server.bind(str(self._path))
        server.listen(4)
        # Same user only. The dashboard runs as the same account.
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        self._server = server
        log.info("web control socket listening on %s", self._path)

        while not self.stopping:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopping:
                    break
                raise
            with conn:
                self._serve(conn)

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(1.0)
        try:
            data = conn.recv(256).decode("utf-8", "replace")
        except (OSError, socket.timeout):
            return
        for line in data.splitlines():
            for event in parse_command(line):
                self.emit(event)

    def _close(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError:
            pass


__all__ = ["WebBackend", "parse_command"]
