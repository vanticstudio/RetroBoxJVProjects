"""The handshake between the TV process and the web dashboard.

Two files in the runtime directory, and nothing else shared:

* ``status.json`` - the TV writes a small snapshot every couple of seconds.
  The dashboard reads it. The web server never reaches into the running player.
* ``control.sock`` - a Unix socket the TV listens on (see ``input/web.py``).
  The dashboard connects and writes one command line. Those become ordinary
  :class:`~retrobox.actions.InputEvent` values, identical to what the Flirc
  remote produces, so there is exactly one path into the state machine.

Both paths live under ``$XDG_RUNTIME_DIR/retrobox`` when systemd provides one
(the units set it), falling back to a temp directory otherwise. Both are
overridable by environment variable, which is what the tests use.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger(__name__)

#: Bumped if the shape of status.json ever changes incompatibly.
SCHEMA_VERSION = 1


def runtime_dir() -> Path:
    """Where the status file and control socket live."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "retrobox"


def status_path() -> Path:
    override = os.environ.get("RETROBOX_STATUS_PATH")
    return Path(override) if override else runtime_dir() / "status.json"


def control_socket_path() -> Path:
    override = os.environ.get("RETROBOX_CONTROL_SOCKET")
    return Path(override) if override else runtime_dir() / "control.sock"


def write_status(data: Dict[str, Any]) -> bool:
    """Write the snapshot atomically. Never raises - it is only telemetry."""
    path = status_path()
    payload = dict(data)
    payload["schema"] = SCHEMA_VERSION
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        # Atomic, so the dashboard can never read a half-written file.
        tmp.replace(path)
        return True
    except OSError:
        log.debug("could not write %s", path, exc_info=True)
        return False


def read_status() -> Dict[str, Any]:
    """Read the snapshot. Returns ``{}`` when the TV isn't running."""
    try:
        with status_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def send_command(line: str, *, timeout: float = 2.0) -> bool:
    """Send one command to the running TV. False if it isn't listening."""
    import socket

    path = control_socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall((line.strip() + "\n").encode("utf-8"))
        return True
    except (OSError, socket.timeout):
        log.debug("could not send %r to %s", line, path, exc_info=True)
        return False


__all__ = [
    "SCHEMA_VERSION",
    "control_socket_path",
    "read_status",
    "runtime_dir",
    "send_command",
    "status_path",
    "write_status",
]
