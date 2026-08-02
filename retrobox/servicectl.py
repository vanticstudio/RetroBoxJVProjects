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

**Except that they can, and on one real box they did.** The rule is written
once, by the installer. Nothing rewrote it when this code grew a command it
had not needed before, and nothing noticed: the box installed cleanly, booted
cleanly, played video cleanly, and weeks later every privileged button in the
dashboard came back with ``sudo: a password is required`` - on a box whose
owner has no password to type, no keyboard attached and no way to read an
install log. So this module also answers three questions the dashboard and the
updater both need to ask, and answers them in words a customer can act on:

* :func:`check_privileges` - does the permission on THIS box cover what THIS
  code runs? It asks sudo, with ``-l``, which lists a command and never runs
  it. It deliberately does not look for a file: the fragment is 0440 root:root
  inside a directory an ordinary user cannot look into, so the dashboard could
  not read it to compare it, and "the file exists" is exactly the check that
  would have passed on the broken box.
* :func:`repair` - regenerate and reinstall the fragment, but only where that
  can be done honestly. The dashboard has no authentication, so it must not be
  able to write sudo's own configuration: a rule that let it would hand root
  to anyone on the LAN, which is a far worse bug than the one being fixed.
  Unprivileged, :func:`repair` changes nothing and hands back the exact
  command a person can type.
* :func:`explain_failure` - the plain-English translation, so the dashboard
  and the updater tell a customer the same thing, and neither of them ever
  shows them the word "sudo".
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)


class ServiceError(Exception):
    """A refused action, or one that failed, with something fit to show.

    ``str(error)`` keeps the machine's own words, which is what belongs in the
    journal. ``error.plain`` is the same failure written for the person in
    front of the television - see :func:`explain_failure`. Anything that puts
    text on a page uses ``.plain``; nothing puts ``str()`` on a page.
    """

    def __init__(self, message: str, *, plain: Optional[str] = None):
        super().__init__(message)
        self._plain = plain

    @property
    def plain(self) -> str:
        return self._plain or explain_failure(str(self))


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
            f"could not {action}: {output or 'the command failed'}",
            plain=explain_failure(output, action=action),
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
_MV_PATHS = ("/usr/bin/mv", "/bin/mv")
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
        raise ServiceError(
            f"could not set the timezone: {output or 'the command failed'}",
            plain=explain_failure(output, action="timezone"),
        )
    log.info("timezone set to %s", zone)
    return zone


#: The five things this box does to itself, in the words a customer would use
#: for them. Keyed by the groups :func:`required_privileges` hands back, so a
#: banner can say "saving network settings" rather than "/usr/bin/netplan".
GROUP_LABELS: Dict[str, str] = {
    "service": "the Power buttons",
    "timezone": "setting the clock",
    "network": "saving network settings",
    "scan": "looking for wifi networks",
    "hostname": "changing the box's name",
}


@dataclass(frozen=True)
class Privilege:
    """One command this box must be able to run as root, and where it lives."""

    group: str
    #: Every path the program is installed at on some distro. sudo matches the
    #: literal path it is given, so the rule names all of them and the check
    #: asks about whichever one this box actually has.
    paths: Tuple[str, ...]
    #: Everything after the program name, exactly as sudo will see it.
    arguments: str

    @property
    def specs(self) -> Tuple[str, ...]:
        return tuple(f"{path} {self.arguments}".rstrip() for path in self.paths)

    @property
    def label(self) -> str:
        return GROUP_LABELS.get(self.group, self.group)


def required_privileges() -> Tuple[Privilege, ...]:
    """Every privileged command this code can run, derived from the code.

    One list, two readers: :func:`sudoers_rule` turns it into the file that
    grants them, and :func:`check_privileges` asks sudo whether this box will
    actually run them. Adding a command to :data:`COMMANDS` therefore both
    widens the generated rule and widens what the check asks about, with
    nothing to remember and no second list to forget.
    """
    from .netconf import staging_for       # the name the code actually writes

    found: List[Privilege] = []
    for argv in COMMANDS.values():
        arguments = " ".join(argv[2:])       # everything after "sudo -n"
        rest = arguments.split(" ", 1)[1] if " " in arguments else ""
        found.append(Privilege("service", _SYSTEMCTL_PATHS, rest))

    found.append(Privilege("timezone", _TIMEDATECTL_PATHS, "set-timezone *"))

    # Network: fixed commands, fixed paths, content on stdin.
    found.append(Privilege("network", _NETPLAN_PATHS, "try --timeout=*"))
    found.append(Privilege("network", _NETPLAN_PATHS, "apply"))
    for target in _NETPLAN_FILES:
        # A netplan file is built in a staging file beside it and renamed over
        # it, because tee truncates before it writes and this box is switched
        # off at the wall - a cut in that window leaves a truncated document
        # and netplan generate then fails for the whole directory. Both ends
        # of the rename are named in full; the staging name comes from netconf
        # so the rule and the command cannot be spelled differently.
        staged = staging_for(target)
        found.append(Privilege("network", _TEE_PATHS, staged))
        found.append(Privilege("network", _CHMOD_PATHS, f"600 {staged}"))
        found.append(Privilege("network", _MV_PATHS, f"-f {staged} {target}"))
        found.append(Privilege("network", _CAT_PATHS, target))
        # The direct write the staging dance replaced. Kept because an update
        # that is rolled back puts the previous code back without touching
        # this file, and that code writes the live file directly: without
        # these two the Network page would come back from a rollback unable
        # to save anything at all. It permits nothing the rename does not.
        found.append(Privilege("network", _TEE_PATHS, target))
        found.append(Privilege("network", _CHMOD_PATHS, f"600 {target}"))

    found.append(Privilege("scan", _IW_PATHS, "dev * scan"))
    found.append(Privilege("hostname", _HOSTNAMECTL_PATHS, "set-hostname *"))
    return tuple(found)


def _specs(group: str) -> List[str]:
    """Every command in one group, spelled the way sudoers spells it."""
    return sorted({
        spec
        for privilege in required_privileges() if privilege.group == group
        for spec in privilege.specs
    })


def sudoers_rule(username: str) -> str:
    """The sudoers.d fragment granting exactly the commands above.

    Generated from :data:`COMMANDS` rather than written out by hand, so the
    file on disk cannot come to permit more - or less - than the code runs.
    """
    if not isinstance(username, str) or not _USERNAME.match(username):
        # This string is written into a file that grants privilege. A name
        # with a space, comma or newline in it changes what that file means.
        raise ValueError(f"{username!r} is not a plausible user name")

    lines = [
        "# Managed by Retro Box scripts/install-service.sh.",
        "#",
        "# Exactly the commands the dashboard's System page can run, and no",
        "# others. Deliberately NOT a blanket systemctl rule: the dashboard has",
        "# no authentication, so anyone on the LAN can press these buttons, and",
        "# 'restart the television' must not be spellable as 'stop the firewall'.",
        "",
        f"{username} ALL=(root) NOPASSWD: " + ", \\\n    ".join(_specs("service")),
        "",
        "# The one wildcard, and only on this subcommand. A timezone cannot be",
        "# enumerated in sudoers - there are some six hundred of them - so the",
        "# argument is checked in Python against the machine's own",
        "# `timedatectl list-timezones` before it is ever passed here.",
        "# `set-timezone` takes nothing but a zone name and validates it itself,",
        "# so the worst this wildcard can do is what the button already does:",
        "# set the clock to the wrong place.",
        f"{username} ALL=(root) NOPASSWD: " + ", \\\n    ".join(_specs("timezone")),
        "",
        "# Network configuration. The netplan file paths are named in full - the",
        "# dashboard writes those two files and no others - and their CONTENTS",
        "# arrive on stdin, so an SSID or a wifi password never occupies an argv",
        "# position. --timeout is wildcarded because it is a number this box",
        "# chooses, and set-hostname/iw take one name each, both checked against",
        "# the machine's own lists before they are used.",
        "#",
        "# The .retrobox-new files are where a new document is built before it is",
        "# renamed over the live one, so that a box switched off at the wall",
        "# mid-write cannot leave a truncated file in /etc/netplan - which would",
        "# take the network down on every interface, not just the one being",
        "# changed. Both ends of each mv are named in full.",
        f"{username} ALL=(root) NOPASSWD: " + ", \\\n    ".join(_specs("network")),
        "",
        f"{username} ALL=(root) NOPASSWD: " + ", \\\n    ".join(_specs("scan")),
        "",
        f"{username} ALL=(root) NOPASSWD: " + ", \\\n    ".join(_specs("hostname")),
        "",
    ]
    return "\n".join(lines)


# ==========================================================================
# Is the permission on this box the permission this code needs?
# ==========================================================================
#: Where the installer puts the fragment. Named here so the check and the
#: repair cannot disagree with each other about which file they mean.
SUDOERS_PATH = "/etc/sudoers.d/retrobox-system"

#: What a check found. Four answers, because they need four different things
#: said to a customer - and, for the last two, two different fixes.
PRIVILEGES_OK = "ok"
PRIVILEGES_MISSING = "missing"        # nothing was ever granted
PRIVILEGES_STALE = "stale"            # granted for an older, smaller table
PRIVILEGES_BLOCKED = "blocked"        # granted, but sudo cannot become root

#: Long enough for a loaded box, short enough that a wedged sudo cannot hold
#: a dashboard page open. Nothing here waits on a password: -n forbids it.
PROBE_TIMEOUT = 5.0

#: What a wildcard is filled in with when asking sudo about a rule that has
#: one. sudo only pattern-matches the argument - it never looks at it, and it
#: never runs the command - so the word is chosen to read sensibly in the
#: auth log rather than to mean anything.
_PROBE_TOKEN = "retrobox"


def _repo_directory() -> Path:
    return Path(__file__).resolve().parent.parent


def _fix_command() -> str:
    """The one command that puts all of this right, ready to be pasted.

    The path is worked out rather than assumed, because a customer reading it
    off a screen has no way to know where their box keeps the project.
    """
    repo = _repo_directory()
    if (repo / "scripts" / "install-service.sh").exists():
        return f"cd {repo} && ./scripts/install-service.sh"
    return "cd ~/RetroBox && ./scripts/install-service.sh"


#: Written once, at import, so every message quotes the same thing.
FIX_COMMAND = _fix_command()

#: What each button is asking to do, for the sentence below. Anything not on
#: this list gets the general form, which is true of all of them.
_PERMISSION_SUBJECTS = {
    "restart-tv": "restart the television",
    "restart-dashboard": "restart this dashboard",
    "reboot": "restart itself",
    "shutdown": "shut itself down",
    "timezone": "set its own clock",
    "network": "change its own network settings",
}


def permission_message(action: Optional[str] = None) -> str:
    """The one thing a customer is ever told about a refused privilege.

    "sudo: interactive authentication is required" is not a sentence anybody
    outside this file can act on, and it sends the owner of a box that has no
    password looking for a password. This is what they get instead, from here
    rather than from each page, so the dashboard and the updater cannot end up
    saying two different things about the same fault.
    """
    what = _PERMISSION_SUBJECTS.get(action or "", "restart itself")
    return (
        f"This box has not been given permission to {what}, which is why that "
        "came back with an error. Nothing has happened to your videos, your "
        "channels or your settings, and the television keeps playing. The "
        "dashboard cannot give itself this permission - if it could, anyone "
        "on your home network could take the box over - so it takes one "
        f"command, typed on the box itself:  {FIX_COMMAND}"
    )


#: The default wording, for callers with no particular action in hand.
PERMISSION_MESSAGE = permission_message()

#: Everything sudo says when it will not do something without a password.
#: Matched case-insensitively against whatever the command wrote.
_REFUSED_SIGNS = (
    "a password is required",
    "password is required",
    "interactive authentication",
    "authentication is required",
    "not allowed to execute",
    "is not allowed to run",
    "may not run",
    "sorry, user",
    "not in the sudoers file",
    "no tty present",
    "askpass",
)

#: And everything it says when it cannot become root AT ALL, whatever the
#: rules say - the shape a NoNewPrivileges= in a unit file makes. Same
#: sentence for a customer, different fault underneath: regenerating the
#: permission file would fix nothing, because the file is not the problem.
_CANNOT_BECOME_ROOT_SIGNS = (
    "no new privileges",
    "must be owned by uid 0",
    "effective uid is not 0",
    "setuid",
    "nosuid",
    "no such file or directory",       # there is no sudo on this box at all
)

#: sudo writes this on EVERY invocation, successful ones included, once the
#: hostname stops matching /etc/hosts - which the dashboard's own hostname
#: button causes. Reading it as a refusal would put a repair banner on a
#: perfectly healthy box, so it is thrown away before anything is decided.
_SUDO_NOISE = re.compile(r"^sudo: unable to resolve host .*$", re.MULTILINE)

#: How much of a machine's own words is worth repeating to a person.
_MAX_REASON = 300


def _without_noise(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return _SUDO_NOISE.sub("", text).strip()


def is_permission_problem(text: str) -> bool:
    """Is this failure sudo refusing, rather than the command failing?"""
    said = _without_noise(text).lower()
    return any(sign in said for sign in _REFUSED_SIGNS + _CANNOT_BECOME_ROOT_SIGNS)


def _cannot_become_root(text: str) -> bool:
    said = _without_noise(text).lower()
    return any(sign in said for sign in _CANNOT_BECOME_ROOT_SIGNS)


def explain_failure(text: str, *, action: Optional[str] = None) -> str:
    """Whatever a command said, written for the person in front of the TV.

    A permission failure becomes :func:`permission_message` and the raw words
    are dropped entirely - they are not evidence a customer can use, and
    "sudo" is not a word they should have to learn. Anything else keeps its
    own words, because "Unit retrobox.service not found" is genuinely the
    useful thing to say, minus any line sudo wrote about itself.
    """
    cleaned = _without_noise(text)
    if is_permission_problem(cleaned):
        return permission_message(action)
    kept = [
        line for line in cleaned.splitlines()
        if line.strip() and not line.lstrip().lower().startswith("sudo:")
    ]
    said = " ".join(part.strip() for part in kept).strip()
    if not said:
        return (
            "The box could not carry that out and did not say why. Trying "
            "again is safe; if it keeps happening, restarting the box usually "
            "clears it."
        )
    return said[:_MAX_REASON]


def _first_existing(paths: Sequence[str]) -> Optional[str]:
    """The path this box actually has, out of the ones a distro might use.

    A box with no wifi hardware may have no ``iw`` at all. Asking sudo about a
    command that is not installed gets "command not found", which is not a
    permission problem, so the ones that are not here are not asked about.
    """
    for path in paths:
        if os.path.exists(path):
            return path
    return None


def _probe_argv(program: str, arguments: str) -> List[str]:
    """The question, never the command: ``sudo -l`` lists, it does not run."""
    argv = ["sudo", "-n", "-l", "--", program]
    if arguments:
        argv.extend(part.replace("*", _PROBE_TOKEN) for part in arguments.split(" "))
    return argv


def _fragment_state(path: Optional[str] = None) -> str:
    """"present", "absent", or "unknown" - and unknown is the normal answer.

    /etc/sudoers.d is root-only on Debian and Ubuntu, so an ordinary user
    cannot even look inside it and gets a refusal rather than an answer -
    which is why "unknown" is the answer this returns on a healthy box, and
    why it decides nothing. It never raises an alarm and it never picks a
    state; it goes into the journal line so that whoever eventually reads it
    knows whether the file was there, and nowhere else.
    """
    try:
        os.stat(path or SUDOERS_PATH)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "present"


@dataclass(frozen=True)
class PrivilegeCheck:
    """What this box will actually let its own dashboard do."""

    state: str
    headline: str
    message: str
    #: What stopped working, in the words a customer would use.
    affected: Tuple[str, ...] = ()
    #: What sudo refused, for the journal. Never shown on a page.
    refused: Tuple[str, ...] = ()
    detail: str = ""
    command: str = field(default_factory=lambda: FIX_COMMAND)

    @property
    def ok(self) -> bool:
        return self.state == PRIVILEGES_OK

    @property
    def needs_repair(self) -> bool:
        return not self.ok


def _affected_phrase(groups: Sequence[str]) -> str:
    labels = [GROUP_LABELS[group] for group in GROUP_LABELS if group in groups]
    if not labels:
        return "some of the buttons on this dashboard"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _tail() -> str:
    return (
        "Nothing has happened to your videos, your channels or your settings, "
        "and the television keeps playing. The dashboard cannot put this right "
        "by itself - if it could, anyone on your home network could take the "
        f"box over - so it takes one command, typed on the box itself:  "
        f"{FIX_COMMAND}"
    )


def check_privileges(*, timeout: float = PROBE_TIMEOUT) -> PrivilegeCheck:
    """Does the permission on THIS box cover what THIS code runs?

    It asks sudo, once per command, with ``-l``: sudo prints the command if it
    would run it without a password and fails if it would not. Nothing is
    executed - asking about ``systemctl reboot`` on a real box must not reboot
    it - and nothing is read from disk, because the fragment is 0440 root:root
    in a directory an ordinary user cannot look into.

    What that compares, then, is behaviour rather than text: for every command
    this code can run, *will this box run it, right now, without a password*.
    It therefore tolerates everything a byte-for-byte comparison against
    :func:`sudoers_rule` would trip over and a customer would not care about -
    a trailing newline, reordered lines, a comment somebody added, a different
    username, the grant arriving from a different file - and refuses to
    tolerate the only thing that matters, which is a command the code runs and
    sudo will not.

    Not free: it is one short-lived process per command, so it belongs on a
    page load or behind a button, not in a loop. It never raises.
    """
    checked = 0
    refused: List[str] = []
    groups: List[str] = []

    for privilege in required_privileges():
        program = _first_existing(privilege.paths)
        if program is None:
            continue
        checked += 1
        code, output = _run(_probe_argv(program, privilege.arguments), timeout=timeout)
        if code == 0:
            continue
        if _cannot_become_root(output):
            # No point asking about the other twenty: sudo has said it cannot
            # become root for anything, so this is not about the rules.
            log.warning("sudo cannot become root on this box: %s",
                        _without_noise(output)[:200])
            return PrivilegeCheck(
                state=PRIVILEGES_BLOCKED,
                headline="Something is stopping this box acting on its own settings",
                message=(
                    "This box may have the permission it needs, but the way "
                    "its software is started is stopping that permission being "
                    "used, so " + _affected_phrase(list(GROUP_LABELS)) +
                    " all come back with an error. " + _tail()
                ),
                affected=tuple(GROUP_LABELS.values()),
                refused=(f"{program} {privilege.arguments}".rstrip(),),
                detail=f"sudo cannot become root: {_without_noise(output)[:200]}",
            )
        refused.append(f"{program} {privilege.arguments}".rstrip())
        if privilege.group not in groups:
            groups.append(privilege.group)

    if not checked:
        # Not one of the programs is installed. That is not a permission
        # problem and there is nothing a customer could do about it, so it is
        # logged and nobody is shouted at.
        log.warning("none of the privileged commands exist on this box")
        return PrivilegeCheck(
            state=PRIVILEGES_OK,
            headline="This box can look after itself",
            message="",
            detail="nothing to check: none of the programs are installed",
        )

    if not refused:
        return PrivilegeCheck(
            state=PRIVILEGES_OK,
            headline="This box can look after itself",
            message=(
                "The Power buttons, the clock and the Network page all have "
                "the permission they need."
            ),
            detail=f"sudo permits all {checked} commands this box runs",
        )

    # Which of the two this is gets decided by what sudo will actually do -
    # nothing at all, or some of it - and not by whether the file is there.
    # Two reasons. On a real box /etc/sudoers.d is root-only, so the look
    # inside it below is refused rather than answered, and a state that leaned
    # on it would come out one way for the installer running as root and
    # another way for the page a customer is reading. And on the box this bug
    # was found on the answer would have been wrong anyway: a legacy file
    # granted the one command that kept Shut Down working while
    # retrobox-system had never been written, so "absent" would have said
    # "nothing was ever granted" to an owner looking at a button that worked.
    # The file is still read, but only to be written into the journal line.
    fragment = _fragment_state()
    where = _affected_phrase(groups)
    if len(refused) == checked:
        state = PRIVILEGES_MISSING
        headline = "This box has not been given permission to look after itself"
        message = (
            f"{where[0].upper() + where[1:]} all come back with an error, "
            "because the permission they need was never put in place on this "
            "box. " + _tail()
        )
    else:
        state = PRIVILEGES_STALE
        headline = "This box's permission is out of date"
        message = (
            "This box was set up by an earlier version of the software, and "
            "this version needs a little more than it was given, so " + where +
            " come back with an error while everything else works as normal. "
            + _tail()
        )
    log.warning("privilege check: %s - sudo refused %d of %d commands",
                state, len(refused), checked)
    return PrivilegeCheck(
        state=state,
        headline=headline,
        message=message,
        affected=tuple(GROUP_LABELS[group] for group in GROUP_LABELS if group in groups),
        refused=tuple(refused),
        detail=(f"{len(refused)} of {checked} commands refused; "
                f"{SUDOERS_PATH} is {fragment}"),
    )


# ==========================================================================
# Putting it back
# ==========================================================================
_VISUDO_PATHS = ("/usr/sbin/visudo", "/sbin/visudo", "/usr/bin/visudo")


@dataclass(frozen=True)
class RepairResult:
    """What a repair did, or honestly did not do."""

    applied: bool
    message: str
    detail: str = ""
    command: str = field(default_factory=lambda: FIX_COMMAND)


def _am_root() -> bool:
    return os.geteuid() == 0


def current_user() -> str:
    """Whoever this process is - the account the rule has to name."""
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:                                    # pragma: no cover
        import getpass
        return getpass.getuser()


def _cannot_repair_here() -> RepairResult:
    return RepairResult(
        applied=False,
        message=(
            "This is the one thing the dashboard is not allowed to fix by "
            "itself. Granting permission is how a box is taken over, so the "
            "dashboard has no way to do it - deliberately, and this page has "
            "no password on it. Nothing has been changed. Whoever set the box "
            f"up can put it right with one command on the box:  {FIX_COMMAND}"
        ),
        detail=(
            f"not root, so {SUDOERS_PATH} was not touched - and no rule exists "
            "or may exist that would let an unauthenticated page write it"
        ),
    )


def repair(username: Optional[str] = None) -> RepairResult:
    """Regenerate the fragment and put it back, or say why it cannot.

    Root - the installer, or a person - gets the whole job: the rule is
    written to a staging name sudo ignores, checked with ``visudo``, and only
    then renamed over the live file in one step. Nothing is ever half
    installed, and a rule that does not check out leaves the old one alone,
    because a bad file in /etc/sudoers.d breaks sudo for everything on a box
    whose only recovery story is sudo.

    Anything else - which is to say the dashboard, always - gets an honest no
    and the exact command to type. It does not try, and there is nothing for
    it to try: no sudoers rule grants writing sudoers rules, and adding one
    would hand root to anyone who can reach this box's web page.

    ``username`` defaults to the account this process is running as, which is
    the account that needs the grant. It is never taken from a request.
    """
    name = username or current_user()
    try:
        rule = sudoers_rule(name)
    except ValueError:
        log.warning("refusing to write a permission rule for %r", username)
        return RepairResult(
            applied=False,
            message=(
                "This box could not work out which account to give the "
                "permission to, so it has changed nothing. " + _tail()
            ),
            detail=f"{username!r} is not a plausible user name",
        )

    if not _am_root():
        return _cannot_repair_here()

    return _install_fragment(rule, SUDOERS_PATH)


def _install_fragment(text: str, path: str) -> RepairResult:
    """Stage, check, then rename. Never a half-installed rule."""
    directory = os.path.dirname(path) or "."
    # sudo ignores any name in an include directory that contains a dot, so
    # the file being built is never read as a rule while it is being built -
    # the same reason the netplan writes stage under a name netplan skips.
    staged = os.path.join(directory, "." + os.path.basename(path) + ".new")

    visudo = _first_existing(_VISUDO_PATHS)
    if visudo is None:
        return RepairResult(
            applied=False,
            message=(
                "This box cannot check a new permission file, so it has not "
                "written one - an unchecked one could stop the box being "
                "repaired at all. " + _tail()
            ),
            detail="visudo is not installed; refusing to install an unchecked rule",
        )

    try:
        handle = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w") as new:
            new.write(text)
    except OSError as exc:
        _discard(staged)
        return RepairResult(
            applied=False,
            message="This box could not write the new permission file. " + _tail(),
            detail=str(exc),
        )

    code, output = _run([visudo, "-c", "-f", staged], timeout=PROBE_TIMEOUT)
    if code != 0:
        _discard(staged)
        log.error("generated sudoers rule did not validate: %s", output[:300])
        return RepairResult(
            applied=False,
            message=(
                "This box checked the new permission file, found something "
                "wrong with it and left the one it already had alone. " + _tail()
            ),
            detail=f"visudo refused the generated rule: {output[:300]}",
        )

    try:
        # 0440 root:root, which is what sudo insists on before it will read a
        # file at all. It is owned by root already: only root gets this far.
        os.chmod(staged, 0o440)
        os.replace(staged, path)
    except OSError as exc:
        _discard(staged)
        return RepairResult(
            applied=False,
            message="This box could not put the new permission file in place. " + _tail(),
            detail=str(exc),
        )

    log.info("installed %s (checked with visudo)", path)
    return RepairResult(
        applied=True,
        message=(
            "This box has been given back the permission it needs. The Power "
            "buttons, the clock and the Network page work again."
        ),
        detail=f"wrote {path}",
    )


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


__all__ = [
    "ACTIONS",
    "COMMANDS",
    "DESCRIPTIONS",
    "FIX_COMMAND",
    "GROUP_LABELS",
    "PERMISSION_MESSAGE",
    "PRIVILEGES_BLOCKED",
    "PRIVILEGES_MISSING",
    "PRIVILEGES_OK",
    "PRIVILEGES_STALE",
    "Privilege",
    "PrivilegeCheck",
    "RepairResult",
    "SUDOERS_PATH",
    "ServiceError",
    "check_privileges",
    "current_user",
    "explain_failure",
    "is_permission_problem",
    "permission_message",
    "repair",
    "required_privileges",
    "run",
    "set_timezone",
    "sudoers_rule",
]
