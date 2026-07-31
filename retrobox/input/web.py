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
    info | guide | menu | last | sleep | power | shutdown
    channel <number>
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path
from typing import List, Optional

from ..actions import Action, InputEvent
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
}


def parse_command(line: str) -> List[InputEvent]:
    """Turn one command line into the events a remote would have produced.

    ``channel 12`` becomes digit-1, digit-2, enter - the same sequence typing
    it on the number pad produces - rather than a bespoke "tune" path.
    """
    parts = line.strip().lower().split()
    if not parts:
        return []

    verb = parts[0]
    if verb in _SIMPLE:
        return [InputEvent(_SIMPLE[verb])]

    if verb == "channel" and len(parts) == 2 and parts[1].isdigit():
        digits = parts[1].lstrip("0") or "0"
        if len(digits) > 3:  # the app's own entry buffer keeps 3
            return []
        return [InputEvent.digit(int(d)) for d in digits] + [InputEvent(Action.ENTER)]

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
