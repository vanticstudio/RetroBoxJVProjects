"""Reading the systemd journal, for people who do not have a terminal.

The most common reason to SSH into any machine is to read a log, so this is
the endpoint that saves the most trips. It is also the easiest place to do
real damage: a journal has no end, and piping one into a browser is a fine way
to take the box down while trying to work out what is wrong with it.

So everything here is bounded twice - the cap is asked for on the command line
*and* enforced on what comes back - and the unit name, level and cursor are
all whitelisted, because they arrive from an unauthenticated network.

**Why journalctl and not python3-systemd.** The `systemd.journal` bindings are
the "proper interface", and if this were a general-purpose tool that is what
it would use. On this box the trade goes the other way: the bindings are a
compiled extension needing libsystemd-dev to build or a distro package to
install, on a product that installs onto both Debian/Ubuntu and Raspberry Pi
OS. ``journalctl -o json`` is a documented, stable, machine-readable interface
- it is not screen-scraping - it costs no dependency, and ``--lines`` gives
the bound this module needs for free. If a reason appears to need the
bindings' seek/tail semantics, this module is the one place that changes.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

#: Only this product's own units. The dashboard has no authentication, and
#: nothing about this box needs it to be a general-purpose journal reader.
UNITS = ("retrobox.service", "retrobox-web.service")

#: Hard ceiling on one request, whatever is asked for.
MAX_LINES = 2000
DEFAULT_LINES = 200

#: How long journalctl gets before we give up. A diagnostic that hangs is no
#: better than one that crashes.
TIMEOUT = 15.0

#: syslog levels, in the words a person uses for them.
LEVELS = {
    "emergency": 0, "alert": 1, "critical": 2, "error": 3,
    "warning": 4, "notice": 5, "info": 6, "debug": 7,
}
_LEVEL_NAMES = {value: name for name, value in LEVELS.items()}

#: A journal cursor is systemd's own opaque token, and it looks like
#: "s=<hex>;i=<hex>;b=<hex>;...". Match that shape rather than trusting it.
#: The leading character is pinned to alphanumeric specifically so a value
#: cannot start with "-": it goes onto a command line, and while it is passed
#: as one argv element and so cannot inject anything, an argument that looks
#: like a flag is not something to wave through.
_CURSOR = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9;=_.:+/-]{0,511}\Z")


def _run(cmd: Sequence[str], *, timeout: float = TIMEOUT) -> Optional[str]:
    """Run journalctl. ``None`` means there is no journal to read here."""
    try:
        result = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        log.debug("could not run %s", cmd, exc_info=True)
        return None
    if result.returncode != 0 and not result.stdout:
        return None
    return result.stdout or ""


def _decode(message: Any) -> str:
    """journalctl gives MESSAGE as a byte array when it is not valid UTF-8."""
    if isinstance(message, list):
        try:
            return bytes(int(b) & 0xFF for b in message).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return ""
    return "" if message is None else str(message)


def _timestamp(raw: Any) -> str:
    """The journal's microseconds-since-epoch, as something readable."""
    import datetime

    try:
        seconds = int(raw) / 1_000_000
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def read(
    *,
    unit: Optional[str] = None,
    level: Optional[str] = None,
    search: Optional[str] = None,
    lines: int = DEFAULT_LINES,
    after: Optional[str] = None,
) -> Dict[str, Any]:
    """A bounded page of the journal.

    ``unit`` must be one of this product's own units, or ``None`` for both.
    ``after`` is a cursor from a previous call, which is how the page walks
    forwards without re-reading what it has already shown.
    """
    if unit is not None and unit not in UNITS:
        raise ValueError(f"{unit!r} is not a Retro Box service")
    if level is not None and level not in LEVELS:
        raise ValueError(f"{level!r} is not a log level")
    if after is not None and not _CURSOR.match(after):
        raise ValueError("that is not a journal cursor")

    try:
        wanted = int(lines)
    except (TypeError, ValueError):
        wanted = DEFAULT_LINES
    if wanted <= 0:
        wanted = DEFAULT_LINES
    wanted = min(wanted, MAX_LINES)

    cmd: List[str] = ["journalctl", "--output=json", "--no-pager", f"--lines={wanted}"]
    for name in ((unit,) if unit else UNITS):
        cmd.append(f"--unit={name}")
    if level is not None:
        cmd.append(f"--priority={LEVELS[level]}")
    if after is not None:
        cmd.append(f"--after-cursor={after}")

    raw = _run(cmd)
    if raw is None:
        return {
            "entries": [], "cursor": None, "truncated": False, "available": False,
            "note": "no systemd journal on this machine, or it cannot be read",
        }

    needle = (search or "").strip().lower()
    entries: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue                       # a line we do not understand is not fatal
        if not isinstance(item, dict):
            continue

        message = _decode(item.get("MESSAGE"))
        # Plain substring, never a pattern: a search box that quietly accepts
        # regular expressions is one bad expression away from hanging the box.
        if needle and needle not in message.lower():
            continue

        try:
            priority = int(item.get("PRIORITY", 6))
        except (TypeError, ValueError):
            priority = 6
        entries.append({
            "time": _timestamp(item.get("__REALTIME_TIMESTAMP")),
            "level": _LEVEL_NAMES.get(priority, "info"),
            "unit": item.get("_SYSTEMD_UNIT") or item.get("SYSLOG_IDENTIFIER") or "",
            "message": message,
            "cursor": item.get("__CURSOR") or "",
        })

    # The cap again, on what actually arrived. Asking journalctl nicely is not
    # the same as being sure.
    truncated = len(entries) > wanted
    if truncated:
        entries = entries[-wanted:]

    return {
        "entries": entries,
        "cursor": entries[-1]["cursor"] if entries else None,
        "truncated": truncated,
        "available": True,
        "note": "",
    }


__all__ = ["DEFAULT_LINES", "LEVELS", "MAX_LINES", "UNITS", "read"]
