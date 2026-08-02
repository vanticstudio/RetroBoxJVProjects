"""The handshake between the TV process and the web dashboard.

Two files in the runtime directory, and nothing else shared:

* ``status.json`` - the TV writes a small snapshot every couple of seconds.
  The dashboard reads it. The web server never reaches into the running player.
* ``control.sock`` - a Unix socket the TV listens on (see ``input/web.py``).
  The dashboard connects and writes one command line. Those become ordinary
  :class:`~retrobox.actions.InputEvent` values, identical to what the Flirc
  remote produces, so there is exactly one path into the state machine.

Both paths live under ``$XDG_RUNTIME_DIR/retrobox`` when that directory really
exists and can be written to, falling back to a private temp directory when it
cannot. Both are overridable by environment variable, which is what the tests
use. See :func:`runtime_dir` for why the fallback is not as simple as it looks.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Sequence

log = logging.getLogger(__name__)

#: Bumped if the shape of status.json ever changes incompatibly.
SCHEMA_VERSION = 1


#: The two files that ARE the handshake. Either one existing is proof that a
#: process has already picked that directory, which is what keeps the TV and
#: the dashboard together - see runtime_dir().
_HANDSHAKE = ("status.json", "control.sock")


def _usable(directory: Path) -> bool:
    """Could this process actually create and write inside ``directory``?

    Asked about the directory itself when it is already there, and about the
    parent it would have to be created in when it is not. Deliberately does no
    mkdir: the dashboard asks this question on every page load, and a read must
    not leave directories scattered behind it.
    """
    probe = directory if directory.is_dir() else directory.parent
    try:
        info = probe.stat()
    except OSError:
        return False                     # not there, or not reachable
    if not stat.S_ISDIR(info.st_mode):
        return False
    # A handshake directory that already exists and belongs to somebody else is
    # not ours to use. /tmp is world-writable, so without this a second account
    # on the box could host the unauthenticated control socket in a directory
    # it owns. The parent is exempt: /tmp itself belongs to root, rightly.
    if probe == directory and info.st_uid != os.getuid():
        return False
    return os.access(probe, os.W_OK | os.X_OK)


def _candidates() -> List[Path]:
    """Every place the handshake could live, best first.

    A relative XDG_RUNTIME_DIR is discarded rather than resolved: it would
    resolve against the process's working directory, and the TV and the
    dashboard are not obliged to share one.
    """
    places: List[Path] = []
    xdg = (os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    if xdg and os.path.isabs(xdg):
        places.append(Path(xdg) / "retrobox")
    # The fallback carries the uid because /tmp is shared: a leftover
    # /tmp/retrobox owned by another account would otherwise lock us out of the
    # only place left to go.
    places.append(Path(tempfile.gettempdir()) / f"retrobox-{os.getuid()}")
    return places


def runtime_dir() -> Path:
    """Where the status file and control socket live.

    Both systemd units set XDG_RUNTIME_DIR=/run/user/<uid> unconditionally, and
    logind only creates that directory for a login session. An appliance nobody
    logs into has no session, so unless linger is enabled the directory simply
    never exists - and the old code, which fell back only when the variable was
    UNSET, walked straight into it. The TV could not write its snapshot, the TV
    could not bind its socket, and the dashboard reported a dead television
    while the television was plainly playing.

    THE HARD PART IS NOT THE FALLBACK, IT IS THE AGREEMENT.

    The TV and the dashboard are separate processes that answer this question
    independently, at whatever moment systemd happens to start each of them.
    /run/user can appear between those two moments - somebody plugs a keyboard
    in and logs into tty2, or linger gets enabled later - so "use /run/user
    when it is there, /tmp when it is not" would put the TV in /tmp at boot and
    the dashboard in /run/user seconds later. Same empty dashboard, new cause.

    So preference is not the first question asked. Occupancy is:

    1. If a candidate already holds a status file or a control socket, THAT is
       where the other process went, and we go there too. Whichever of them
       starts first decides for both, and neither can be talked out of it
       afterwards by a directory turning up late.
    2. Only when nothing is occupied does preference apply: the real runtime
       directory if it exists and can be written to, else the temp fallback.
    3. If neither is usable, the fallback path is returned anyway, so the two
       processes at least fail in the same place instead of in different ones.

    The awkward sequence, spelled out. The TV starts at boot, finds no
    /run/user/<uid>, and lands in /tmp/retrobox-<uid>, where it writes
    status.json and binds control.sock. logind then creates /run/user/<uid>,
    and the dashboard starts. The dashboard sees /run/user/<uid>/retrobox empty
    and /tmp/retrobox-<uid> occupied, so it follows the TV into /tmp. The TV,
    asking again on its next write, sees its own files in /tmp and stays put.
    Nothing moves while those files exist, which is until the box is switched
    off at the wall - /tmp is cleared on boot, so a box that later gets linger
    (or a login session at boot) goes back to /run/user of its own accord. The
    reverse case - the TV starting while /run/user is already there, so both
    use it - is the provisioned box, and behaves exactly as it always has.

    Rejected alternatives, and why. Creating the directory ourselves does not
    work: /run/user belongs to root, so an unprivileged process cannot make
    /run/user/<uid> (it CAN make the retrobox subdirectory once logind has made
    the parent, which is what _usable() tests for). Pinning the choice in a file
    needs a directory both processes already agree on, which is the problem
    restated. Refusing to start would turn a box that mostly works into a box
    that shows nothing, on hardware with no SSH and no console. Caching the
    answer for the life of the process would freeze a disagreement in place
    rather than heal it: because this is recomputed on every call, a dashboard
    that guessed wrong in the second before the TV's first write corrects
    itself on the very next poll.
    """
    places = _candidates()
    for place in places:
        if any((place / name).exists() for name in _HANDSHAKE):
            return place
    for place in places:
        if _usable(place):
            return place
    return places[-1]


def status_path() -> Path:
    override = os.environ.get("RETROBOX_STATUS_PATH")
    return Path(override) if override else runtime_dir() / "status.json"


def control_socket_path() -> Path:
    override = os.environ.get("RETROBOX_CONTROL_SOCKET")
    return Path(override) if override else runtime_dir() / "control.sock"


def ensure_runtime_dir(directory: Path) -> None:
    """Make the handshake directory, and keep it to ourselves.

    Only the box user has any business reading the status snapshot or reaching
    the control socket, and the fallback lives in a world-writable /tmp where
    that is not the default. The mode is also repaired rather than merely set,
    because the directory may already exist - left by an older release, or made
    moments earlier by the TV's socket listener - and would otherwise stay
    group- and world-readable for the life of the box. Directories a caller
    pointed us at
    with RETROBOX_STATUS_PATH are left exactly as the caller made them.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = directory.stat()
        if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077:
            directory.chmod(0o700)
    except OSError:
        log.debug("could not tighten %s", directory, exc_info=True)


# ==========================================================================
# Saying, in words, whether the box is playing or has gone quiet
# ==========================================================================
# The sentence lives here rather than in the dashboard because the dashboard
# would otherwise have to invent it out of four booleans, and every place that
# invents it invents it slightly differently. It also has to be got right in a
# way that is easy to get wrong: a box that has gone quiet because nobody is
# watching is WORKING, and must never be worded as though something has gone
# wrong. There is no "no signal", no "not responding", and no red.
#
# The television process writes the finished sentence into status.json, so the
# dashboard renders a string it was given rather than a state it interpreted.
_PLAYING = "Playing."
_HELD = "Playing - something else is watching, so the box is staying awake."
_OFF = "Playing. Going quiet when the television is off is switched off."
_ASLEEP_NO_DISPLAY = (
    "Asleep - nothing is connected to the video output, so playback is paused."
)
_ASLEEP_STANDBY = (
    "Asleep - the television says it is in standby, so playback is paused."
)
_ASLEEP = "Asleep - nothing is watching, so playback is paused."


def display_summary(
    *,
    sleeping: bool,
    enabled: bool,
    state: str = "unknown",
    cec: str = "absent",
    holds: Sequence[str] = (),
) -> str:
    """One plain sentence about whether the picture is running, and why not.

    ``state`` is the display watcher's word ("present" / "absent" /
    "unknown"), ``cec`` the television's own ("absent" / "unknown" / "on" /
    "standby"), and ``holds`` whoever is asking the box to stay awake.
    """
    if sleeping:
        if cec == "standby":
            return _ASLEEP_STANDBY
        if state == "absent":
            return _ASLEEP_NO_DISPLAY
        return _ASLEEP
    if not enabled:
        return _OFF
    if holds:
        return _HELD
    return _PLAYING


def write_status(data: Dict[str, Any]) -> bool:
    """Write the snapshot atomically. Never raises - it is only telemetry."""
    path = status_path()
    payload = dict(data)
    payload["schema"] = SCHEMA_VERSION
    try:
        if path.parent == runtime_dir():
            ensure_runtime_dir(path.parent)
        else:
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
    "display_summary",
    "ensure_runtime_dir",
    "read_status",
    "runtime_dir",
    "send_command",
    "status_path",
    "write_status",
]
