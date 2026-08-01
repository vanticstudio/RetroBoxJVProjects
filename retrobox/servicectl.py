"""Restarting the box, without a terminal and without handing out root.

The dashboard has no authentication. Anyone on the home network can press
these buttons, and for a television that is the accepted trade - the worst a
neighbour can do is turn it off and on again, which is also what the power
switch does. What is *not* accepted is a sudoers rule broad enough to be worth
attacking. ``NOPASSWD: /usr/bin/systemctl`` with no arguments would let anyone
on the LAN start, stop or mask any unit on the machine, which is a very
different thing from a reboot button.

So the set of commands is closed and written out here in full. Nothing is
assembled from a request at call time: a request names one of four actions, or
it is refused. The sudoers rule is generated from the same table, so the file
on disk and the code cannot drift apart.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Dict, List, Sequence, Tuple

log = logging.getLogger(__name__)


class ServiceError(Exception):
    """A refused action, or one that failed, with something fit to show."""


# Both paths, because sudo matches the literal command it is given: modern
# distros have /usr/bin/systemctl, older ones /bin/systemctl, and a rule
# listing only one silently fails on the other.
_SYSTEMCTL_PATHS = ("/usr/bin/systemctl", "/bin/systemctl")

#: Every command this module may ever run, spelled out. "-n" on all of them
#: so sudo fails rather than sitting waiting for a password nobody will type.
COMMANDS: Dict[str, List[str]] = {
    "restart-tv": ["sudo", "-n", "systemctl", "restart", "retrobox.service"],
    # --no-block because the process running this *is* the one being
    # restarted: without it systemctl waits for a restart that kills it first,
    # and the browser gets a dropped connection instead of an answer. With it,
    # systemd has the job before we die, so the restart happens regardless.
    "restart-dashboard": [
        "sudo", "-n", "systemctl", "--no-block", "restart", "retrobox-web.service",
    ],
    "reboot": ["sudo", "-n", "systemctl", "reboot"],
    "shutdown": ["sudo", "-n", "systemctl", "poweroff"],
}

ACTIONS: Tuple[str, ...] = tuple(COMMANDS)

#: What each one does, in the words the confirmation dialog uses.
DESCRIPTIONS = {
    "restart-tv": "restart the television (the picture goes off for a few seconds)",
    "restart-dashboard": "restart this dashboard (this page will reconnect on its own)",
    "reboot": "restart the whole box",
    "shutdown": "shut the box down (you will need the power button to bring it back)",
}

_USERNAME = re.compile(r"\A[a-z_][a-z0-9_-]{0,31}\Z")


def _run(cmd: Sequence[str], *, timeout: float = 15.0) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return result.returncode, (result.stderr or result.stdout or "").strip()


def run(action: str) -> str:
    """Carry out one named action, or refuse.

    Returns the human description of what was done, for the page to echo back.
    """
    if not isinstance(action, str) or action not in COMMANDS:
        raise ServiceError(f"{action!r} is not something this box can be asked to do")

    cmd = COMMANDS[action]
    log.info("service control: %s", action)
    code, output = _run(cmd)
    if code != 0:
        raise ServiceError(
            f"could not {action}: {output or 'the command failed'}"
        )
    return DESCRIPTIONS[action]


_TIMEDATECTL_PATHS = ("/usr/bin/timedatectl", "/bin/timedatectl")

# Network configuration. Every one of these is a fixed command with a fixed
# path: the netplan file contents arrive on stdin, never as an argument, so an
# SSID or a wifi password has no argv position to reach. `iw dev * scan` is the
# one exception and takes only an interface name, which is checked against the
# kernel's own list before it is used.
_NETPLAN_PATHS = ("/usr/sbin/netplan", "/usr/bin/netplan", "/sbin/netplan")
_TEE_PATHS = ("/usr/bin/tee", "/bin/tee")
_CAT_PATHS = ("/usr/bin/cat", "/bin/cat")
_CHMOD_PATHS = ("/usr/bin/chmod", "/bin/chmod")
_IW_PATHS = ("/usr/sbin/iw", "/sbin/iw", "/usr/bin/iw")
_HOSTNAMECTL_PATHS = ("/usr/bin/hostnamectl", "/bin/hostnamectl")

#: The two netplan files this box writes. Constants, so the sudoers rule can
#: name them exactly rather than wildcarding a path.
_NETPLAN_FILES = (
    "/etc/netplan/90-retrobox-wired.yaml",
    "/etc/netplan/91-retrobox-wifi.yaml",
)

#: A zone name is one of the ~600 the machine already knows about. Checked
#: against that list before it is used, so the shape check is belt to that
#: whitelist's braces rather than the only defence.
_TIMEZONE = re.compile(r"\A[A-Za-z][A-Za-z0-9+_-]*(/[A-Za-z0-9+._-]+){0,2}\Z")


def set_timezone(zone: str, *, allowed: Sequence[str]) -> str:
    """Set the system timezone to one the machine already recognises.

    ``allowed`` is what ``timedatectl list-timezones`` returned. The zone must
    be in it: this is a whitelist, not a sanitiser, so there is nothing to
    escape and nothing to get wrong about escaping it.
    """
    if not isinstance(zone, str) or not _TIMEZONE.match(zone):
        raise ServiceError("that is not a timezone name")
    if zone not in set(allowed):
        raise ServiceError(f"this box does not know a timezone called {zone!r}")

    code, output = _run(["sudo", "-n", "timedatectl", "set-timezone", zone])
    if code != 0:
        raise ServiceError(f"could not set the timezone: {output or 'the command failed'}")
    log.info("timezone set to %s", zone)
    return zone


def sudoers_rule(username: str) -> str:
    """The sudoers.d fragment granting exactly the commands above.

    Generated from :data:`COMMANDS` rather than written out by hand, so the
    file on disk cannot come to permit more - or less - than the code runs.
    """
    if not isinstance(username, str) or not _USERNAME.match(username):
        # This string is written into a file that grants privilege. A name
        # with a space, comma or newline in it changes what that file means.
        raise ValueError(f"{username!r} is not a plausible user name")

    allowed = []
    for argv in COMMANDS.values():
        arguments = " ".join(argv[2:])       # everything after "sudo -n"
        rest = arguments.split(" ", 1)[1] if " " in arguments else ""
        for path in _SYSTEMCTL_PATHS:
            allowed.append(f"{path} {rest}".rstrip())

    timezone_commands = sorted(f"{path} set-timezone *" for path in _TIMEDATECTL_PATHS)

    # Network: fixed commands, fixed paths, content on stdin.
    network_exact = []
    for path in _NETPLAN_PATHS:
        network_exact.append(f"{path} try --timeout=*")
        network_exact.append(f"{path} apply")
    for target in _NETPLAN_FILES:
        for path in _TEE_PATHS:
            network_exact.append(f"{path} {target}")
        for path in _CAT_PATHS:
            network_exact.append(f"{path} {target}")
        for path in _CHMOD_PATHS:
            network_exact.append(f"{path} 600 {target}")
    scan_commands = sorted(f"{path} dev * scan" for path in _IW_PATHS)
    hostname_commands = sorted(
        f"{path} set-hostname *" for path in _HOSTNAMECTL_PATHS
    )

    lines = [
        "# Managed by Retro Box scripts/install-service.sh.",
        "#",
        "# Exactly the commands the dashboard's System page can run, and no",
        "# others. Deliberately NOT a blanket systemctl rule: the dashboard has",
        "# no authentication, so anyone on the LAN can press these buttons, and",
        "# 'restart the television' must not be spellable as 'stop the firewall'.",
        "",
        f"{username} ALL=(root) NOPASSWD: " + ", \\\n    ".join(sorted(set(allowed))),
        "",
        "# The one wildcard, and only on this subcommand. A timezone cannot be",
        "# enumerated in sudoers - there are some six hundred of them - so the",
        "# argument is checked in Python against the machine's own",
        "# `timedatectl list-timezones` before it is ever passed here.",
        "# `set-timezone` takes nothing but a zone name and validates it itself,",
        "# so the worst this wildcard can do is what the button already does:",
        "# set the clock to the wrong place.",
        f"{username} ALL=(root) NOPASSWD: " + ", \\\n    ".join(timezone_commands),
        "",
        "# Network configuration. The netplan file paths are named in full - the",
        "# dashboard writes those two files and no others - and their CONTENTS",
        "# arrive on stdin, so an SSID or a wifi password never occupies an argv",
        "# position. --timeout is wildcarded because it is a number this box",
        "# chooses, and set-hostname/iw take one name each, both checked against",
        "# the machine's own lists before they are used.",
        f"{username} ALL=(root) NOPASSWD: " + ", \\\n    ".join(sorted(set(network_exact))),
        "",
        f"{username} ALL=(root) NOPASSWD: " + ", \\\n    ".join(scan_commands),
        "",
        f"{username} ALL=(root) NOPASSWD: " + ", \\\n    ".join(hostname_commands),
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "ACTIONS",
    "COMMANDS",
    "DESCRIPTIONS",
    "ServiceError",
    "run",
    "sudoers_rule",
]
