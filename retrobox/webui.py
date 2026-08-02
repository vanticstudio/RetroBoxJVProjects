"""The web dashboard: the whole box, in a browser, over the LAN.

Runs as its own process (see ``scripts/retrobox-web.service``) and never
imports or touches the running player. It does exactly three things:

* reads ``status.json``, which the TV writes every couple of seconds
* writes command lines to ``control.sock``, which the TV's web input backend
  turns into ordinary :class:`~retrobox.actions.InputEvent` values
* reads and rewrites ``config.yaml`` through :mod:`retrobox.configstore`, then
  asks the TV to reload it

That split is the whole architecture. The Flask process never reaches into the
player's memory; it asks, over a local socket, and the TV cannot tell the
difference between a browser click and a button on the remote.

The rows it renders come from :class:`~retrobox.menu.MenuModel`, the same model
the on-screen menu uses, so the two cannot drift apart. The channel list comes
from ``config.py``'s loader rather than a second YAML parse.

There is no authentication, deliberately and consistently with the LAN file
share: anyone who can reach the box can change the channel or shut it down.
That is the right trade on a home network and the wrong one anywhere else. It
does mean every single value that arrives from the network is bounded and
checked before it is allowed anywhere near the disk - see
:mod:`retrobox.safepath` for the upload defences, which are the sharp end of it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__ as retrobox_version
from . import (
    branding, journal, library, netconf, netprobation, schedule, servicectl,
    static_gen, sysinfo, timekeeping, updates, webstyle,
)
from .config import ChannelConfig, Config, ConfigError, UpdateConfig, load_config
from .configstore import ConfigStore
from .configwrite import BACKUP_SUFFIX
from .daypart import Daypart
from .menu import SCREEN_CHANNELS, MenuContext, MenuModel
from .safepath import (
    UnsafePath,
    resolve_inside,
    safe_folder_name,
    safe_media_name,
)
from .status import read_status, send_command
from .branding import BrandingError
from .netconf import NetworkError
from .schedule import ScheduleError
from .netprobation import Probation
from .updater import UpdateError, Updater
from .uploads import (
    STATE_DONE,
    STATE_NO_VIDEO,
    UploadError,
    UploadLimits,
    UploadStore,
    UploadTarget,
    spool_for,
)

log = logging.getLogger(__name__)

# The phosphor green from the on-screen display, so the page reads as part of
# the same product rather than a bolted-on admin panel. Both live in webstyle
# now, next to the stylesheet that splices them in; re-exported here because
# this is where anything wanting the product colours has always looked.
GREEN = webstyle.GREEN
DIM = webstyle.DIM

# Bounds on everything that can arrive from the network.
MAX_CHANNEL_NUMBER = 999
MAX_CHANNEL_NAME = 48
MAX_AUDIO_DEVICE = 200
MAX_SLEEP_MINUTES = 24 * 60
UPLOAD_CHUNK_BYTES = 1024 * 1024      # stream in 1 MB bites, never in one gulp

#: Where a whole-file upload waits while it is still arriving, inside the
#: upload spool. Dotted and far too short to be mistaken for a session id, so
#: the chunked uploader's own sweep walks straight past it - but it is under
#: the spool, so the space it uses is counted and it is cleared up. See
#: ``_staging_dir``: the alternative is a multi-gigabyte orphan in a channel
#: folder that nothing on this box can see.
UPLOAD_STAGING_NAME = ".staging"
# A config file is a few kilobytes. Anything much larger is not one.
MAX_CONFIG_BYTES = 1024 * 1024

# The git clone the box runs from. Derived from where this file lives rather
# than configured: an update that could be pointed at another directory is an
# update that can be pointed anywhere.
REPO_DIR = Path(__file__).resolve().parent.parent

#: Update progress lives beside config.yaml so a reloaded page - or a
#: restarted dashboard - can pick the running update back up.
UPDATE_STATE_NAME = ".retrobox-update.json"

#: Network probation state, beside the config so the page can find it again
#: after the box has moved address and the browser has hunted it down. Named by
#: the module that owns the file: two copies of this string that drift apart are
#: a box that never settles an interrupted network change.
NETWORK_STATE_NAME = netprobation.STATE_NAME

#: What this box knows about its own clock - whether it woke up believing it
#: was 2011, and who chose the timezone it is running. Beside the config for
#: the same reasons as the two above, and named by the module that owns the
#: file so the two names cannot drift apart.
TIME_STATE_NAME = timekeeping.STATE_NAME

#: How many things one file-manager request may act on. Select-all on a folder
#: of six hundred episodes is an ordinary thing to do, so the number is
#: generous; it exists because a request that names a hundred thousand paths
#: is not a customer, and each one costs a walk of the disk.
MAX_LIBRARY_SELECTION = 1000

#: How full a disk has to be before the file manager stops treating free space
#: as a detail and starts leading with it. Below a tenth left, "this will not
#: free anything" is the most important sentence on the confirmation.
LIBRARY_DISK_TIGHT = 0.10

# Port 80, so the address a customer types has no port in it. The systemd
# unit grants CAP_NET_BIND_SERVICE so this works without running as root.
DEFAULT_PORT = 80
FALLBACK_PORT = 8080

# Settings the dashboard is allowed to write, and the subset of those that only
# take effect when the box next starts.
SETTABLE = ("audio_device", "initial_volume", "auto_channels", "sleep_timer")
RESTART_REQUIRED = ("auto_channels",)


# ==========================================================================
# Can this box still do the things this page offers?
#
# The permission to reboot, set the clock and write a network file is granted
# once, by the installer, and nothing rewrites it when this code grows a
# command it did not have before. One sold unit had only the older rule on it:
# it installed cleanly, booted cleanly, played video cleanly, and weeks later
# Shut Down worked while Restart, Reboot and the whole Network page came back
# with "sudo: interactive authentication is required" - to somebody with no
# keyboard, no SSH and no install log. servicectl answers whether that has
# happened; what follows is how this page says so, and what it is allowed to
# do about it.
# ==========================================================================
#: How long one answer is trusted before sudo is asked again.
#:
#: The check is twenty-one short-lived processes on a Raspberry Pi that is
#: playing video, and this page has no login - so whatever triggers it is
#: triggerable by anyone on the network, as fast as they can hold down F5.
#: This is the ceiling on that: one round of asking every half minute per
#: dashboard, however many people press the button. It is short enough that
#: somebody who has just typed the command on the box and come back to the
#: page is not left looking at yesterday's answer for long.
PRIVILEGE_CHECK_TTL = 30.0

#: The environment variable systemd sets for every service it starts, and the
#: only way this process can tell "the appliance is booting" from "somebody
#: imported this module".
#:
#: The start-up check is for the first. A checkout and the test suite build
#: this app object over and over, and asking sudo twenty-one questions each
#: time is a cost neither of them should pay for a fault neither of them can
#: have - the fault needs an installed box, and an installed box is one this
#: dashboard was started on by systemd (scripts/retrobox-web.service). It is
#: also the difference between a suite that runs and one that does not:
#: tests/conftest.py refuses any command naming systemctl against the real
#: checkout, and `sudo -n -l -- /usr/bin/systemctl reboot` names it.
#:
#: A dashboard started by hand on a real box loses nothing that matters: the
#: same check runs when somebody opens the System page, which is where the
#: answer is read.
STARTED_BY_SYSTEMD = "INVOCATION_ID"

#: What is actually printed on the buttons behind each group of privileges.
#:
#: servicectl says which GROUPS sudo refused, in the words a customer would
#: use for them ("the Power buttons"). Only this file knows what those buttons
#: are called on the screen, and "some features are unavailable" is not
#: something anybody standing in front of a television can act on.
PRIVILEGE_BUTTONS: Dict[str, Tuple[str, ...]] = {
    "service": (
        "RESTART THE TV", "RESTART DASHBOARD", "REBOOT THE BOX", "SHUT DOWN",
    ),
    "timezone": ("the Timezone picker under Clock",),
    "network": ("JOIN A WIFI NETWORK", "WIRED SETTINGS", "Keep, on a network change"),
    "scan": ("the list of networks JOIN A WIFI NETWORK looks for",),
    "hostname": ("changing what this box is called, under Network",),
}


def _for_a_customer(exc: Exception, *, action: Optional[str] = None) -> str:
    """Whatever went wrong, in words somebody in front of a television can use.

    Only sudo's refusals are rewritten - "git fetch failed: sudo: a password
    is required" becomes the sentence saying what actually fixes it - and
    everything else keeps its own words, because "could not read from remote
    repository" is genuinely the useful thing to say. The original always goes
    to the journal, which is where the paths and the machine's own words are
    of use to somebody.
    """
    said = str(exc)
    if not servicectl.is_permission_problem(said):
        return said
    # "a privileged command was refused", not "refused by sudo:" - this line
    # is read back through _without_sudos_own_words() on the way to the System
    # tab and the support bundle, and that scrubber cuts from the first
    # "sudo:" on the line. Writing our own prefix that way made this box's own
    # words the first casualty of its own scrubber.
    log.warning("a privileged command was refused: %s", said)
    return servicectl.permission_message(action)


def privilege_buttons(affected: Sequence[str]) -> List[str]:
    """The buttons that will fail, given the groups servicectl says are refused.

    ``affected`` arrives as the customer-facing group labels, so it is turned
    back into group names through servicectl's own table - one list, so a
    group added there cannot quietly go unnamed here.
    """
    groups = {label: group for group, label in servicectl.GROUP_LABELS.items()}
    named: List[str] = []
    for label in affected:
        for button in PRIVILEGE_BUTTONS.get(groups.get(label, ""), ()):
            if button not in named:
                named.append(button)
    return named


#: What a state means for the page, in one word, for the support bundle.
PRIVILEGE_STATES = {
    servicectl.PRIVILEGES_OK: "in place",
    servicectl.PRIVILEGES_MISSING: "never granted",
    servicectl.PRIVILEGES_STALE: "out of date",
    servicectl.PRIVILEGES_BLOCKED: "granted, but this box cannot act on it",
}

#: The state of a box that has not been asked, or could not be. It is not a
#: fault report: a check that fell over is this dashboard's problem, not
#: something to put a red banner about in front of a customer.
PRIVILEGES_UNKNOWN = "unknown"


def privilege_answer(check: Optional[servicectl.PrivilegeCheck]) -> Dict[str, Any]:
    """One privilege check, as the page is allowed to see it.

    ``.refused`` and ``.detail`` are deliberately not here. They carry the
    paths sudo was asked about and sudo's own words for refusing, which is
    exactly the text that made one customer's box incomprehensible. They go to
    the journal, where somebody who can read them is looking.
    """
    if check is None:
        return {
            "state": PRIVILEGES_UNKNOWN,
            "needs_repair": False,
            "repairable": False,
            "headline": "",
            "message": "",
            "affected": [],
            "buttons": [],
            "command": servicectl.FIX_COMMAND,
            "user": servicectl.current_user(),
        }
    return {
        "state": check.state,
        "needs_repair": check.needs_repair,
        # Re-generating the fragment is the fix for a grant that is missing or
        # too small. It is NOT the fix for a box where sudo cannot become root
        # at all - that is the service unit, a different job - so that state
        # gets the explanation and no button, rather than a button that sends
        # somebody round a loop which cannot end.
        "repairable": check.state in (
            servicectl.PRIVILEGES_MISSING, servicectl.PRIVILEGES_STALE
        ),
        "headline": check.headline if check.needs_repair else "",
        "message": check.message if check.needs_repair else "",
        "affected": list(check.affected),
        "buttons": privilege_buttons(check.affected),
        "command": check.command,
        # The account the rule has to name, and the one to be signed in as when
        # typing the command. Read from this process, never from a request.
        "user": servicectl.current_user(),
    }


class ApiError(Exception):
    """A bad request, with the status code to answer it with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _library_status(exc: Exception) -> int:
    """Which HTTP code a refusal from :mod:`retrobox.library` deserves.

    ``catastrophic`` is asked first and on purpose. Every exception that
    module raises inherits from ``LibraryError``, including ``HalfRenamed`` -
    the one state where a folder moved and config.yaml did not, which leaves a
    channel pointed at a name that is no longer on the disk. Branching on
    except-clause order would report that as a 400 "you asked for something
    silly", and a customer would go looking for their own mistake. It is a
    500, it is the server's fault, and its message is the only thing that
    connects a black channel to a rename, so it is passed through word for
    word and written to the log at error.
    """
    if getattr(exc, "catastrophic", False):
        log.error("%s", exc)
        return 500
    if isinstance(exc, library.LibraryNotFound):
        return 404
    if isinstance(exc, (library.LibraryConflict, library.LibraryBusy)):
        return 409
    return 400


#: Every knob the CRT shader is generated from: the name the browser sends,
#: the name the television's control socket knows it by, and the range
#: config.py allows. Numbers get a low and a high; the two switches get None.
#:
#: One table, read by both the Save route and the live-preview route, because
#: they are the same six settings taking the same six values and a bound that
#: meant one thing on the way to config.yaml and another on the way to the
#: screen would show a customer a picture they cannot then save.
_CRT_SETTINGS: Tuple[Tuple[str, str, Optional[float], Optional[float]], ...] = (
    ("crt_enabled",        "enabled",            None, None),
    ("curvature",          "curvature",          0.0,  0.5),
    ("corner_radius",      "corner_radius",      0.0,  0.3),
    ("vignette",           "vignette",           0.0,  1.0),
    ("scanlines",          "scanlines",          None, None),
    ("scanline_intensity", "scanline_intensity", 0.0,  1.0),
)


def _crt_from_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """The picture settings a request actually asked to change, checked.

    Only the ones that are present: a dashboard dragging one control sends the
    control that moved, and both the config merge and the television's own
    preview merge are built on that. Out of range is refused rather than
    clamped, where somebody is looking at the screen, rather than silently
    rounded on the next load.
    """
    settings: Dict[str, Any] = {}
    for key, name, low, high in _CRT_SETTINGS:
        if key not in body:
            continue
        raw = body[key]
        if low is None:
            if not isinstance(raw, bool):
                raise ApiError(f"{key} is true or false")
            settings[name] = raw
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ApiError(f"{key} must be a number")
        if not low <= float(raw) <= high:
            raise ApiError(f"{key} must be between {low} and {high}")
        settings[name] = float(raw)
    return settings


def stream_to_file(stream: Any, out: Any, *, declared: int, limit: int) -> int:
    """Copy ``stream`` into ``out`` a chunk at a time, and check what arrived.

    Never reads the whole body into memory: these are video files, and a
    multi-gigabyte upload buffered into RAM on a small box takes the box with
    it. Returns the byte count, or raises :class:`ApiError` if the upload was
    cut short or ran over the limit - in both cases the caller throws away what
    it has rather than passing a broken file off as an episode.
    """
    received = 0
    while True:
        try:
            chunk = stream.read(UPLOAD_CHUNK_BYTES)
        except Exception:  # noqa: BLE001 - the browser went away mid-transfer
            raise ApiError("the upload stopped before it finished") from None
        if not chunk:
            break
        received += len(chunk)
        if received > limit:
            raise ApiError("that upload is over the size limit", 413)
        out.write(chunk)
    out.flush()
    os.fsync(out.fileno())
    # A clean close that delivered less than promised is still a broken file.
    # Werkzeug raises above instead, but not every WSGI server does.
    if received != declared:
        raise ApiError("the upload stopped before it finished")
    return received


# ==========================================================================
# Reading the box
# ==========================================================================
def _context(config: Optional[Config], status: Dict[str, Any]) -> MenuContext:
    """Build the same MenuContext the on-screen menu uses, from the snapshot."""
    channels = [(c.number, c.name) for c in config.channels] if config else []
    current = (status.get("channel") or {}).get("number")
    return MenuContext(
        channels=channels,
        current_channel=current,
        volume=int(status.get("volume") or 0),
        muted=bool(status.get("muted")),
        audio_devices=(),          # switching outputs stays on the box itself
        current_audio=status.get("audio_device"),
        version=str(status.get("version") or ""),
    )


def channel_rows(config: Optional[Config], status: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The channel list, straight off MenuModel's own channels screen."""
    model = MenuModel(_context(config, status))
    model.screen = SCREEN_CHANNELS
    current = (status.get("channel") or {}).get("number")
    by_number = {c.number: c for c in (config.channels if config else [])}
    rows = []
    for item in model.rows():
        if not item.key.startswith("ch:"):
            continue          # drops the trailing "Back" row
        number = int(item.key[3:])
        channel = by_number.get(number)
        rows.append(
            {
                "number": number,
                "label": item.label,
                "name": item.value.replace("   <", "").strip(),
                "current": number == current,
                "path": str(channel.path) if channel else "",
            }
        )
    return rows


# A snapshot older than this is not "now" any more - the TV writes one every
# couple of seconds, so a gap this long means the process is gone, not busy.
STALE_AFTER_SECONDS = 30.0


def _snapshot_is_stale() -> bool:
    """Has the TV stopped writing? Judged by the file, not by its contents."""
    from .status import status_path

    try:
        age = time.time() - status_path().stat().st_mtime
    except OSError:
        return False        # no file at all is "not running", not "stale"
    return age > STALE_AFTER_SECONDS


def _as_text(value: Any, limit: int = 200) -> str:
    """Anything from the snapshot, rendered as safe short text."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value)[:limit]


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_int(value: Any) -> Optional[int]:
    """A channel number is a whole number - 2, never 2.0."""
    number = _as_number(value)
    return None if number is None else int(number)


def _clean_lineup(raw: Any) -> List[Dict[str, Any]]:
    """The other channels, keeping only rows that are actually rows."""
    if not isinstance(raw, list):
        return []
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        number = _as_number(item.get("number"))
        if number is None:
            continue
        rows.append({
            "number": int(number),
            "name": _as_text(item.get("name")),
            "off_air": bool(item.get("off_air")),
            "now_playing": _as_text(item.get("now_playing")),
            "current": bool(item.get("current")),
        })
    return rows


def _mb(value: Optional[int]) -> str:
    if not value:
        return "unknown"
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GB"
    return f"{value / 1024**2:.0f} MB"


def _trash_line(trash: Optional[Dict[str, Any]]) -> str:
    """The trash, for the support bundle. Part of the storage block, not a note."""
    if not trash:
        return "  trash: not known"
    if not trash.get("items"):
        return "  trash: empty"
    return (
        f"  trash: {trash['items']} item(s), {_mb(trash.get('bytes'))} "
        f"- still on the disk until it is emptied"
    )


def _volume_line(label: str, volume: Optional[Dict[str, Any]]) -> str:
    if volume is None:
        return f"  {label}: could not be read"
    return (
        f"  {label}: {_mb(volume['free_bytes'])} free of "
        f"{_mb(volume['total_bytes'])} ({volume['percent_used']}% used)"
        f"{'  <-- ' + volume['state'].upper() if volume['state'] != 'ok' else ''}"
    )


#: Everything sudo writes about itself is prefixed with its own name. The
#: privilege check puts those words in the journal on purpose - somebody who
#: can act on "effective uid is not 0" reads them there - but those same
#: journal lines are read by the owner in two places that are not a journal:
#: the log panel on the System tab, and the tail of the support bundle, which
#: is a document a customer generates, reads and pastes into an email. Without
#: this, the exact sentence the permission banner exists to keep off a
#: customer's screen arrives on it by the back door.
#:
#: WHAT GOES: "sudo:" and everything after it to the end of that line. It runs
#: to the end of the line rather than stopping at the next comma or semicolon
#: because sudo's own sentences contain both - "a terminal is required to read
#: the password; either use the -S option..." is one message, and a scrubber
#: that guesses where a sentence ends is a scrubber that lets half of one out.
#:
#: WHAT STAYS: everything BEFORE "sudo:" on the line, which is this box's own
#: words for what it was doing; and every absolute path that was inside the
#: part removed. A path is a fact about this machine, not a sentence anybody
#: could act on wrongly, and "/etc/sudoers.d/retrobox-system" or
#: "/usr/bin/systemctl" is the half of the line that makes a support
#: conversation possible at all. Scrubbing to nothing would be safe and
#: useless.
_SUDO_SPEAKS = re.compile(r"\bsudo:\s*[^\n]*")
#: An absolute path: a slash, a letter, and then the rest of it. It ends on a
#: character a sentence would not, because a trailing full stop or comma
#: belongs to the prose around the path and not to the file name. Starting on
#: a letter keeps "12/25" and the tail of a URL out of it.
_A_PATH = re.compile(r"/[A-Za-z][A-Za-z0-9._/+-]*[A-Za-z0-9_+-]")
_SUDO_REMOVED = "[sudo's own words - see the box's journal]"


def _without_sudos_own_words(message: str) -> str:
    """A journal line with anything sudo said about itself taken back out.

    Used by the live log route and by the support bundle, so the two cannot
    drift into showing a customer different amounts of the same line.
    """
    if not isinstance(message, str):
        return ""

    def replace(match: "re.Match[str]") -> str:
        kept = _A_PATH.findall(match.group(0))
        return " ".join([_SUDO_REMOVED] + kept)

    return _SUDO_SPEAKS.sub(replace, message)


def _log_entries_for_reading(entries: Any) -> List[Dict[str, Any]]:
    """Journal entries with sudo's own words taken out of each message.

    The journal module hands back exactly what journalctl said, which is
    right: it is a log reader, and the only other caller is the box itself.
    The scrubbing belongs here, at the two doors a customer reads through.
    """
    if not isinstance(entries, list):
        return []
    cleaned = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cleaned.append(
            {**entry, "message": _without_sudos_own_words(entry.get("message", ""))}
        )
    return cleaned


def _support_bundle(report: Dict[str, Any], entries: Optional[List[Dict]] = None) -> str:
    """The system information and the recent log, as one block to paste.

    This exists so nobody has to be talked through journalctl over the phone.
    It is deliberately plain text: it has to survive being pasted into an
    email, a chat window or a forum post without turning into soup.
    """
    if entries is None:
        entries = journal.read(lines=200).get("entries", [])
    entries = _log_entries_for_reading(entries)

    where = report.get("addresses") or {}
    storage = report.get("storage") or {}
    hardware = report.get("hardware") or {}
    clock = report.get("timezone") or {}
    load = report.get("load")
    temperature = report.get("temperature")

    lines = [
        "RETRO BOX - JV PROJECTS - support information",
        "=" * 60,
        f"Version:      {report.get('version', 'unknown')}",
        f"Hostname:     {where.get('hostname', 'unknown')}",
        f"Addresses:    {', '.join(where.get('addresses') or []) or 'none'}",
        f"Box uptime:   {_duration(report.get('uptime_seconds'))}",
        f"TV running:   {'yes' if report.get('tv_running') else 'no'}",
        f"TV uptime:    {_duration(report.get('tv_uptime_seconds'))}",
        f"Channels:     {report.get('channel_count') if report.get('channel_count') is not None else 'unknown'}",
        f"Config:       {report.get('config_path', 'unknown')}",
        # Whether this box can still do the privileged half of its job, in the
        # words of the state and nothing else. Never what sudo said: this
        # block gets pasted into an email by a customer, so it is held to the
        # same rule as the page. It reports the last answer and never goes
        # asking for a fresh one - this is a GET on a page with no login.
        f"Permission:   {PRIVILEGE_STATES.get(str(report.get('privileges') or ''), 'not checked')}",
        "",
        "Storage",
        _volume_line("root ", storage.get("root")),
        _volume_line("media", storage.get("media")),
        # Counted in "used" on the media line above, and invisible everywhere
        # else. A support conversation that starts "the disk is full" needs
        # this number in the first block, not on the third phone call.
        _trash_line(report.get("trash")),
        "",
        "Hardware",
        f"  GPU:      {hardware.get('gpu_description', 'unknown')}",
        f"  {hardware.get('decode', {}).get('summary', 'Hardware decode: unknown')}",
        f"  Audio:    {', '.join(hardware.get('audio_devices') or []) or 'none found'}",
        f"  Temp:     {str(temperature['celsius']) + ' C' if temperature else 'no sensor'}",
        f"  Load:     {load['one_minute'] if load else 'unknown'}"
        f"{' over ' + str(load['cores']) + ' cores' if load else ''}",
        "",
        "Clock",
        f"  Timezone: {clock.get('timezone', 'unknown')}",
        f"  Local:    {clock.get('local_time', 'unknown')}",
        f"  Synced:   {clock.get('synchronised')}"
        f"{'   <-- dayparting will drift' if clock.get('warning') else ''}",
        "",
        f"Input backends: {', '.join((report.get('input') or {}).get('backends') or []) or 'none'}",
        f"File share:     {(report.get('share') or {}).get('state', 'unknown')}",
        "",
        "=" * 60,
        f"Recent log ({len(entries)} lines)",
        "=" * 60,
    ]
    for entry in entries:
        lines.append(
            f"{entry.get('time', '')}  {entry.get('level', ''):<8}"
            f"{entry.get('unit', '')}: "
            f"{entry.get('message', '')}"
        )
    if not entries:
        lines.append("(no log entries available - is this box running systemd?)")
    return "\n".join(lines) + "\n"


def _duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "unknown"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


_HOSTNAME = re.compile(r"\A[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?\Z")


def body_hostname(body: Dict[str, Any]) -> str:
    """A hostname, checked against RFC 1123 before it goes anywhere near sudo."""
    raw = body.get("hostname")
    name = raw.strip().lower() if isinstance(raw, str) else ""
    if not _HOSTNAME.match(name):
        raise ApiError(
            "a hostname is letters, digits and hyphens, up to 32 characters, "
            "and cannot start or end with a hyphen"
        )
    return name


def _installed_extras() -> str:
    """Which optional extras this box already has, so a reinstall keeps them.

    Reinstalling without them would quietly remove mpv or Flask from a working
    television, so it is worked out from what actually imports rather than
    assumed.
    """
    import importlib.util

    found = []
    if importlib.util.find_spec("mpv") or importlib.util.find_spec("evdev"):
        found.append("hardware")
    if importlib.util.find_spec("flask"):
        found.append("web")
    return ",".join(found)


def _audio_devices() -> List[str]:
    """Whatever the box can play out of. Never allowed to break the page."""
    from . import hwdetect

    try:
        return list(hwdetect.detect_audio())
    except Exception:  # noqa: BLE001 - detection is a nice-to-have, not a gate
        log.debug("audio detection failed", exc_info=True)
        return []


# ==========================================================================
# Validating what arrives from the network
# ==========================================================================
def _whole_number(raw: Any, *, field: str) -> int:
    # bool is an int in Python, and `True` becoming channel 1 helps nobody.
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ApiError(f"{field} must be a whole number")
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ApiError(f"{field} must be a whole number") from None


def _channel_number(raw: Any) -> int:
    number = _whole_number(raw, field="channel number")
    if not 0 <= number <= MAX_CHANNEL_NUMBER:
        raise ApiError(f"channel number must be between 0 and {MAX_CHANNEL_NUMBER}")
    return number


def _clean_text(raw: Any, *, field: str, limit: int) -> str:
    if not isinstance(raw, str):
        raise ApiError(f"{field} must be text")
    text = raw.strip()
    if not text:
        raise ApiError(f"{field} cannot be empty")
    if len(text) > limit:
        raise ApiError(f"{field} must be {limit} characters or fewer")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ApiError(f"{field} contains a control character")
    return text


def _channel_folder(config: Config, raw: Any) -> Path:
    """A folder a channel may point at: real, and inside the media root."""
    text = _clean_text(raw, field="folder", limit=1024)
    root = config.media_root
    if root is None:
        raise ApiError(
            "set media_root in config.yaml before managing channel folders here"
        )
    candidate = Path(os.path.expanduser(text))
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = resolve_inside(root, candidate)
    except UnsafePath:
        raise ApiError(f"the folder must be inside media_root ({root})") from None
    if not resolved.is_dir():
        raise ApiError(f"that folder does not exist: {resolved}")
    return resolved


def _daypart_folders(config: Config, parts: List[Daypart]) -> List[Daypart]:
    """A schedule's folders, held to exactly the rules a channel's folder is.

    :mod:`retrobox.schedule` deliberately does not do this - it knows about
    clocks, not about what is on this box's disk - so before this the route
    wrote whatever string arrived straight into config.yaml. Two ways that
    hurts: a typo'd folder name is a channel that plays nothing from 18:00 with
    nothing anywhere saying why, and on a dashboard with no password a path
    nobody checked is a path that can be anywhere at all.

    Returns the same blocks with each folder resolved to the real one it means,
    so what lands in config.yaml is a full path rather than what was typed.
    """
    checked: List[Daypart] = []
    for index, part in enumerate(parts):
        if part.path is None:
            checked.append(part)
            continue
        try:
            folder = _channel_folder(config, str(part.path))
        except ApiError as exc:
            # Which block, in the same words the editor labels them with.
            raise ApiError(
                f"block {index + 1} ({part.label}): {exc}", exc.status
            ) from None
        checked.append(replace(part, path=folder))
    return checked


# ==========================================================================
# The app
# ==========================================================================
def create_app(config_path: Optional[str] = None):
    """Build the Flask app. Imported lazily so Flask is only needed here."""
    from flask import Flask, jsonify, request

    app = Flask(__name__)
    store = ConfigStore(config_path or "config.yaml")

    # Looks for a newer release on a timer, off the main thread, and never
    # blocks anything. A box with no internet simply never gets an answer.
    _startup_config: Optional[Config] = None
    try:
        _startup_config = store.load()
        _update_settings = _startup_config.updates
    except Exception:  # noqa: BLE001 - an unreadable config must not stop the server
        _update_settings = UpdateConfig()
    checker = updates.UpdateChecker(
        current=retrobox_version,
        interval_seconds=_update_settings.check_interval_hours * 3600.0,
        enabled=_update_settings.check,
    )
    checker.start()

    # Reclaim anything the trash has been holding for a fortnight. At start-up
    # rather than on a timer, because this box is switched off at the wall: a
    # unit that spends six days a week unplugged would never reach the hour a
    # timer fired at, and would quietly fill up with things its owner deleted
    # last spring. Guarded completely - a box that cannot tidy up still has to
    # serve its dashboard.
    try:
        _media_root = _startup_config.media_root if _startup_config else None
        if _media_root is not None:
            _swept = library.sweep_trash(_media_root)
            if _swept["items"]:
                log.info(
                    "reclaimed %d item(s) the trash had held longer than %d days",
                    _swept["items"], library.DEFAULT_TRASH_DAYS,
                )
    except Exception:  # noqa: BLE001 - housekeeping never stops the server
        log.warning("could not sweep the trash at start-up", exc_info=True)

    # -- plumbing ----------------------------------------------------------
    @app.errorhandler(ApiError)
    def _handle_api_error(exc: ApiError):
        return jsonify({"ok": False, "error": str(exc)}), exc.status

    @app.errorhandler(ConfigError)
    def _handle_config_error(exc: ConfigError):
        # A rejected round trip: the config on disk was left alone.
        return jsonify({"ok": False, "error": str(exc)}), 400

    @app.errorhandler(UnsafePath)
    def _handle_unsafe_path(exc: UnsafePath):
        return jsonify({"ok": False, "error": str(exc)}), 400

    @app.errorhandler(library.LibraryError)
    def _handle_library_error(exc: library.LibraryError):
        return jsonify({"ok": False, "error": str(exc)}), _library_status(exc)

    @app.errorhandler(ScheduleError)
    def _handle_schedule_error(exc: ScheduleError):
        return jsonify({"ok": False, "error": str(exc)}), 400

    @app.errorhandler(BrandingError)
    def _handle_branding_error(exc: BrandingError):
        return jsonify({"ok": False, "error": str(exc)}), 400

    @app.errorhandler(NetworkError)
    def _handle_network_error(exc: NetworkError):
        # Most of these are somebody mistyping an address, and those keep
        # their own words. The one that does not is a write sudo refused: a
        # box set up before the netplan staging file existed has a rule that
        # names the live file and nothing else, so the first privileged step
        # of a save comes back as "sudo: a password is required" - which sends
        # the owner of a box that has no password looking for one. Translated
        # here, at the one place every network route's message becomes a page,
        # rather than in five routes that could each forget.
        return jsonify({"ok": False, "error": _for_a_customer(exc, action="network")}), 400

    @app.errorhandler(UploadError)
    def _handle_upload_error(exc: UploadError):
        message = str(exc)
        # "No room" is its own answer, so the page can say so plainly.
        status = 507 if "space" in message.lower() else 400
        return jsonify({"ok": False, "error": message}), status

    def _config() -> Optional[Config]:
        # Re-read per request: cheap, and it means auto_channels additions and
        # hand edits show up without restarting the dashboard.
        try:
            return store.load()
        except (ConfigError, OSError):
            log.warning("dashboard could not load the config", exc_info=True)
            return None

    def _need_config() -> Config:
        config = _config()
        if config is None:
            raise ApiError("the config file could not be read", 503)
        return config

    def _channel_or_404(number: int) -> Tuple[Config, ChannelConfig]:
        config = _need_config()
        for channel in config.channels:
            if channel.number == number:
                return config, channel
        raise ApiError(f"there is no channel {number}", 404)

    def _body() -> Dict[str, Any]:
        data = request.get_json(silent=True)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ApiError("expected a JSON object")
        return data

    def _confirmed() -> None:
        if request.args.get("confirm") != "yes":
            raise ApiError("add ?confirm=yes to confirm this")

    def _dispatch(command: str):
        ok = send_command(command)
        return jsonify({"ok": ok, "sent": command}), (200 if ok else 503)

    def _saved(config: Config, extra: Optional[Dict[str, Any]] = None, status: int = 200,
               *, applied_note: Optional[str] = None):
        """Answer a successful config change, and nudge the TV to reload it.

        ``applied_note`` is for a change the television acts on the instant it
        reloads - the CRT picture effect is the one that does. It is used only
        when the reload was actually delivered, because "look at the
        television" said to somebody whose television is not running is the
        same kind of lie as the restart warning it replaced.
        """
        applied = send_command("reload")
        body = {
            "ok": True,
            "applied": applied,
            "channels": [
                {"number": c.number, "name": c.name, "path": str(c.path)}
                for c in config.channels
            ],
        }
        if not applied:
            body["note"] = "saved - the TV is not running, so it will apply at next start"
        elif applied_note:
            body["note"] = applied_note
        body.update(extra or {})
        return jsonify(body), status

    # -- pages -------------------------------------------------------------
    @app.get("/")
    def index():
        """What is on TV. The address a customer actually types."""
        return VIEWER_PAGE

    @app.get("/dash")
    def dashboard():
        """The management console. Everything that changes the box lives here."""
        return PAGE

    @app.get("/api/now")
    def api_now():
        """The viewer's own read of the box, shaped and defended.

        ``status.json`` is written by another process and can be absent, stale
        after a crash, or - if a disk went bad mid-write - valid JSON of
        entirely the wrong shape. None of that may take this page down, so
        every field is checked on the way out rather than trusted.
        """
        raw = read_status()
        stale = _snapshot_is_stale()
        live = bool(raw) and not stale

        channel = raw.get("channel")
        channel = channel if isinstance(channel, dict) else {}
        config = _config()

        return jsonify({
            "online": live,
            "stale": stale,
            "version": _as_text(raw.get("version")),
            "channel": {
                "number": _as_int(channel.get("number")),
                "name": _as_text(channel.get("name")),
            },
            "now_playing": _as_text(raw.get("now_playing")),
            "position": _as_number(raw.get("position")) if live else None,
            "duration": _as_number(raw.get("duration")) if live else None,
            "off_air": bool(raw.get("off_air")) if live else False,
            "standby": bool(raw.get("standby")) if live else False,
            "lineup": _clean_lineup(raw.get("lineup")) if live else [],
            # So the page can still list the channels when the TV is off.
            "channels_configured": (
                [{"number": c.number, "name": c.name} for c in config.channels]
                if config else []
            ),
        })

    # -- read ---------------------------------------------------------------
    @app.get("/api/status")
    def api_status():
        status = read_status()
        return jsonify({"online": bool(status), **status})

    @app.get("/api/channels")
    def api_channels():
        return jsonify({"channels": channel_rows(_config(), read_status())})

    # -- the remote control -------------------------------------------------
    @app.post("/api/tune/<int:number>")
    def api_tune(number: int):
        if not 0 <= number <= MAX_CHANNEL_NUMBER:
            return jsonify({"ok": False, "error": "channel out of range"}), 400
        return _dispatch(f"channel {number}")

    @app.post("/api/volume/<direction>")
    def api_volume(direction: str):
        if direction not in ("up", "down"):
            return jsonify({"ok": False, "error": "use up or down"}), 400
        return _dispatch(f"volume_{direction}")

    @app.post("/api/mute")
    def api_mute():
        return _dispatch("mute")

    @app.post("/api/power")
    def api_power():
        return _dispatch("power")

    @app.post("/api/shutdown")
    def api_shutdown():
        if request.args.get("confirm") != "yes":
            return jsonify({"ok": False, "error": "add ?confirm=yes"}), 400
        return _dispatch("shutdown")

    # -- channel management -------------------------------------------------
    @app.post("/api/channels")
    def api_create_channel():
        body = _body()
        config = _need_config()
        name = _clean_text(body.get("name"), field="name", limit=MAX_CHANNEL_NAME)
        folder = _channel_folder(config, body.get("path"))
        wanted = body.get("number")
        created: Dict[str, Any] = {}

        def mutate(data: Dict[str, Any]) -> None:
            used = {int(e.get("number", -1)) for e in ConfigStore.channels_of(data)}
            if wanted is None:
                number = max(used) + 1 if used else config.first_channel_number
                if number > MAX_CHANNEL_NUMBER:
                    raise ApiError("there is no free channel number left")
            else:
                number = _channel_number(wanted)
                if number in used:
                    raise ApiError(f"channel {number} is already in use")
            data["channels"].append(
                {"number": number, "name": name, "path": str(folder)}
            )
            created.update({"number": number, "name": name, "path": str(folder)})

        return _saved(store.update(mutate), {"channel": created}, status=201)

    @app.patch("/api/channels/<int:number>")
    def api_update_channel(number: int):
        body = _body()
        config = _need_config()

        # Validate the shape of everything before touching the file, so a
        # typo cannot get as far as the write.
        new_name = (
            _clean_text(body["name"], field="name", limit=MAX_CHANNEL_NAME)
            if "name" in body else None
        )
        new_folder = _channel_folder(config, body["path"]) if "path" in body else None
        new_number = _channel_number(body["number"]) if "number" in body else None

        def mutate(data: Dict[str, Any]) -> None:
            entry = store.channel_entry(data, number)
            if entry is None:
                raise ApiError(f"there is no channel {number}", 404)
            if new_name is not None:
                entry["name"] = new_name
            if new_folder is not None:
                entry["path"] = str(new_folder)
            if new_number is not None and new_number != number:
                taken = {
                    int(e.get("number", -1))
                    for e in ConfigStore.channels_of(data)
                    if e is not entry
                }
                if new_number in taken:
                    raise ApiError(f"channel {new_number} is already in use")
                entry["number"] = new_number

        return _saved(store.update(mutate))

    @app.delete("/api/channels/<int:number>")
    def api_delete_channel(number: int):
        _confirmed()

        def mutate(data: Dict[str, Any]) -> None:
            entries = ConfigStore.channels_of(data)
            if not any(int(e.get("number", -1)) == number for e in entries):
                raise ApiError(f"there is no channel {number}", 404)
            if len(entries) == 1:
                raise ApiError(
                    "this is the last channel - the box needs at least one to start"
                )
            # Only the config entry goes. The folder and every video file in it
            # is the user's, and is never touched from here.
            data["channels"] = [
                e for e in entries if int(e.get("number", -1)) != number
            ]

        return _saved(store.update(mutate), {"deleted": number})

    @app.post("/api/channels/reorder")
    def api_reorder_channels():
        body = _body()
        raw = body.get("order")
        if not isinstance(raw, list) or not raw:
            raise ApiError("send an 'order' list of channel numbers")
        order = [_channel_number(n) for n in raw]
        if len(set(order)) != len(order):
            raise ApiError("the same channel is listed twice")
        config = _need_config()
        first = config.first_channel_number

        def mutate(data: Dict[str, Any]) -> None:
            entries = ConfigStore.channels_of(data)
            existing = {int(e.get("number", -1)): e for e in entries}
            if set(order) != set(existing):
                # Renumbering only part of the lineup would leave the rest with
                # numbers that collide, or silently drop them.
                raise ApiError(
                    "the order must list every channel exactly once "
                    f"({len(existing)} channels: {sorted(existing)})"
                )
            reordered = []
            for position, number in enumerate(order):
                entry = existing[number]
                entry["number"] = first + position
                reordered.append(entry)
            data["channels"] = reordered

        return _saved(store.update(mutate))

    # -- where a half-arrived file waits -------------------------------------
    def _staging_dir(config: Config, fallback: Path) -> Path:
        """Where an upload is written while it is still only half of one.

        In the upload spool, whenever this box has one. Not in the folder the
        file is going to end up in, which is where this used to put it, and
        which is fine right up until the power goes off - the normal way this
        appliance is switched off. Nothing tidies up after the wall switch:
        the ``except`` in the routes below only runs if the process is still
        alive to run it. What is left is a ``.part`` file the scanner skips,
        the media list filters out and the Settings page's spool total does
        not look at. Eight gigabytes of somebody's disk, gone, with nothing in
        the dashboard able to see it or get it back.

        The spool is the one place the box already accounts for and clears up
        on its own: it is counted by ``UploadStore.reclaimable`` and swept by
        ``_sweep_staging`` on the same schedule as abandoned chunk uploads.

        A box with no ``media_root`` has no spool and no better idea, so it
        keeps the old behaviour rather than losing the ability to upload.
        """
        if config.media_root is None:
            return fallback
        directory = spool_for(config.media_root) / UPLOAD_STAGING_NAME
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A media root that has gone away (an unplugged drive) must not
            # take the upload with it - it may not even be going there.
            log.warning("could not use the upload spool for staging", exc_info=True)
            return fallback
        return directory

    def _sweep_staging(config: Config) -> int:
        """Clear staging files that were abandoned. Returns bytes freed.

        "Abandoned" is measured from the last byte written, not from when the
        file was made, so an upload that is still arriving is never taken out
        from under itself - its staging file was written to a moment ago. It
        uses the same window as abandoned chunk uploads because it is the same
        promise to the customer: unfinished uploads are cleared after that
        long, and there is one number on the page.
        """
        if config.media_root is None:
            return 0
        directory = spool_for(config.media_root) / UPLOAD_STAGING_NAME
        if not directory.is_dir():
            return 0
        cutoff = time.time() - config.web.upload_expiry_hours * 3600
        freed = 0
        for entry in sorted(directory.iterdir()):
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
                if stat.st_mtime > cutoff:
                    continue
                entry.unlink()
            except OSError:          # pragma: no cover - it went away under us
                continue
            freed += stat.st_size
            log.info("reclaimed an abandoned upload staging file (%d bytes)",
                     stat.st_size)
        return freed

    def _land_upload(config: Config, staged: Path, destination: Path) -> None:
        """Move a finished upload into place, whatever disk it staged on.

        A rename is atomic and instant, and that is what happens whenever the
        spool and the destination are on the same filesystem - which is the
        normal case, because the spool lives under ``media_root``. But a
        channel added by hand can point at a plugged-in drive, and a rename
        cannot cross a filesystem.

        The chunked uploader has had to solve exactly that, so this borrows
        its answer rather than growing a second one that could drift from it:
        copy into a hidden staging name inside the destination folder and
        rename from *there*, so the last step is still atomic and half a film
        can never appear under the name of a playable episode.
        """
        if staged.parent == destination.parent:
            staged.replace(destination)
            return
        _store_for(config)._move_into_place(staged, destination)

    # -- media --------------------------------------------------------------
    def _media_files(config: Config, folder: Path) -> List[Dict[str, Any]]:
        allowed = {e.lower() for e in config.video_extensions}
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            raise ApiError(f"cannot read {folder}", 503) from None
        files = []
        for item in entries:
            if item.name.startswith(".") or item.suffix.lower() not in allowed:
                continue
            try:
                if not item.is_file():
                    continue
                files.append({"name": item.name, "bytes": item.stat().st_size})
            except OSError:      # vanished between listing and stat
                continue
        return files

    @app.get("/api/media/<int:number>")
    def api_media_list(number: int):
        config, channel = _channel_or_404(number)
        return jsonify({
            "channel": number,
            "folder": str(channel.path),
            "files": _media_files(config, channel.path),
        })

    @app.post("/api/media/<int:number>")
    def api_media_upload(number: int):
        config, channel = _channel_or_404(number)
        folder = channel.path

        # 1. The name has to be a plain video filename, or we do not proceed.
        try:
            name = safe_media_name(
                request.args.get("name", ""), allowed=config.video_extensions
            )
        except UnsafePath as exc:
            raise ApiError(str(exc)) from None

        # 2. And the path it produces has to genuinely land in this folder,
        #    after the filesystem has had its say about symlinks.
        try:
            destination = resolve_inside(folder, folder / name)
        except UnsafePath as exc:
            raise ApiError(str(exc)) from None
        if destination.exists():
            raise ApiError(f"'{name}' is already on this channel", 409)

        # 3. Nothing is accepted without knowing how big it claims to be.
        declared = request.content_length
        if not declared:
            raise ApiError("the upload must declare its size (Content-Length)", 411)
        limit = config.web.max_upload_mb * 1024 * 1024
        if declared > limit:
            raise ApiError(
                f"that file is larger than the {config.web.max_upload_mb} MB limit", 413
            )

        # 4. And not if it would fill the disk. A full filesystem on this box
        #    is a unit that will not boot. Two disks can be involved, because
        #    the bytes wait in the spool and then land in the channel folder,
        #    and a channel added by hand can point at a plugged-in drive. When
        #    they are the same filesystem the second measurement simply gives
        #    the same answer, which costs nothing.
        staging = _staging_dir(config, folder)
        margin = config.web.min_free_mb * 1024 * 1024
        free = None
        for place in {str(folder), str(staging)}:
            try:
                there = shutil.disk_usage(place).free
            except OSError:
                continue                          # cannot tell; do not block
            free = there if free is None else min(free, there)
        if free is not None and free - declared < margin:
            raise ApiError(
                f"not enough free space: {free // (1024*1024)} MB left and this box "
                f"keeps {config.web.min_free_mb} MB in reserve", 507,
            )

        # 5. Stream it, a megabyte at a time, into a staging file that the
        #    episode scanner cannot see (.part is not a video extension) and
        #    that this box can still find if the power goes off mid-upload.
        handle, staged_name = tempfile.mkstemp(
            prefix=name + ".", suffix=".part", dir=str(staging)
        )
        staged = Path(staged_name)
        try:
            with os.fdopen(handle, "wb") as out:
                received = stream_to_file(
                    request.stream, out, declared=declared, limit=limit
                )

            # Readable by anything else on the box, like a file copied in over
            # the share would be; mkstemp alone would leave it 0600.
            os.chmod(staged, 0o644)
            _land_upload(config, staged, destination)
        except BaseException:
            try:
                staged.unlink()
            except OSError:      # pragma: no cover - already gone
                pass
            raise

        log.info("uploaded %s (%d bytes) to channel %d", name, received, number)
        return jsonify({
            "ok": True, "name": name, "bytes": received,
            "files": _media_files(config, folder),
        }), 201

    @app.delete("/api/media/<int:number>/<name>")
    def api_media_delete(number: int, name: str):
        _confirmed()
        config, channel = _channel_or_404(number)
        try:
            safe = safe_media_name(name, allowed=config.video_extensions)
            target = resolve_inside(channel.path, channel.path / safe)
        except UnsafePath as exc:
            raise ApiError(str(exc)) from None
        if not target.is_file():
            raise ApiError(f"there is no '{safe}' on this channel", 404)
        try:
            target.unlink()
        except OSError as exc:
            raise ApiError(f"could not delete it: {exc}", 503) from None

        log.info("deleted %s from channel %d", safe, number)
        return jsonify({
            "ok": True, "deleted": safe,
            "files": _media_files(config, channel.path),
        })

    # -- the library: browse, rename, delete, and the trash -----------------
    #
    # Everything a customer would otherwise need SSH, Samba or a file manager
    # for. :mod:`retrobox.library` is the engine and holds every rule about
    # what may move where; these routes are the part that knows about HTTP,
    # about confirmations, and about telling the television.
    #
    # Nothing here trusts a path. There is no login on this dashboard, so a
    # path in a request body is a string an attacker on the LAN wrote, and
    # every one of them goes through safepath - inside the library module,
    # which is where the single shared guard lives. Not one of these routes
    # joins a request string onto the media root itself.
    def _library() -> Tuple[Config, Path]:
        """The config and the media root, or a refusal somebody can act on."""
        config = _need_config()
        if config.media_root is None:
            raise ApiError(
                "set media_root in config.yaml before managing files from here"
            )
        return config, Path(config.media_root)

    def _library_paths(body: Dict[str, Any]) -> List[str]:
        """The things a request asked to act on, de-duplicated, still hostile.

        Only the shape is settled here - that it is a list of strings and not
        an absurd number of them. What the strings mean is the library's
        business, and it refuses them one at a time.
        """
        raw = body.get("paths", body.get("path"))
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise ApiError("nothing was selected")
        if len(raw) > MAX_LIBRARY_SELECTION:
            raise ApiError(
                f"that is more than {MAX_LIBRARY_SELECTION} things in one go"
            )
        wanted: List[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise ApiError("that is not a path")
            if item not in wanted:
                wanted.append(item)
        return wanted

    def _same_folder(one: Optional[Path], two: Path) -> bool:
        try:
            return one is not None and os.path.realpath(str(one)) == os.path.realpath(str(two))
        except OSError:  # pragma: no cover - hostile filesystem
            return False

    def _teach_channels_to_ignore_the_machinery(config: Config, root: Path) -> List[int]:
        """Stop a channel pointed at the media root from playing the trash.

        ``scan_episodes`` skips hidden *files*, not hidden *folders*. Nothing
        on this box creates a channel whose folder is the media root itself,
        but a person can write one by hand - and such a channel walks straight
        into ``.retrobox-trash`` and puts every deleted episode back on the
        air. The library module publishes the patterns that close it and
        cannot apply them, because only config.yaml can, so this is the one
        place that can: it knows a delete is about to happen.

        Rewriting config.yaml costs the customer every comment in it, so this
        runs only when there is genuinely something to fix - the same rule
        ``library.rename_folder`` holds itself to. Returns the channel numbers
        that were changed, so the answer can say so out loud.
        """
        exposed = []
        for channel in config.channels:
            places = [channel.path] + [p.path for p in channel.dayparts]
            if not any(_same_folder(place, root) for place in places):
                continue
            if all(glob in channel.exclude for glob in library.MACHINERY_GLOBS):
                continue
            exposed.append(channel.number)
        if not exposed:
            return []

        def mutate(data: Dict[str, Any]) -> None:
            for entry in ConfigStore.channels_of(data):
                try:
                    number = int(entry.get("number", -1))
                except (TypeError, ValueError):  # pragma: no cover - loader fills it
                    continue
                if number not in exposed:
                    continue
                current = [str(p) for p in (entry.get("exclude") or [])]
                entry["exclude"] = current + [
                    glob for glob in library.MACHINERY_GLOBS if glob not in current
                ]

        store.update(mutate)
        log.warning(
            "channels %s play from the media root itself, so they could see the "
            "trash; they now exclude the box's own folders", exposed,
        )
        return exposed

    def _library_warnings(
        items: List[Dict[str, Any]], space: Dict[str, Any]
    ) -> List[str]:
        """The sentences a confirmation dialog must not be allowed to omit.

        Written here rather than in the browser because they are the whole
        point of the confirmation, and a page can be reloaded mid-edit,
        rewritten by the next feature, or read by a person who never sees the
        dialog at all. The route that knows the consequence says the
        consequence.
        """
        said: List[str] = []
        affected: Dict[int, Dict[str, Any]] = {}
        for item in items:
            for entry in item["references"]["detail"]:
                seen = affected.setdefault(
                    entry["channel"], {"name": entry["name"], "where": set()}
                )
                seen["where"].add(entry["where"])

        for number in sorted(affected):
            name = affected[number]["name"]
            if "channel" in affected[number]["where"]:
                # Not "this channel will be affected". A customer needs to know
                # what they will actually be looking at on the television
                # afterwards, which is colour bars - see app.py's _show_no_signal.
                said.append(
                    f"Channel {number} ({name}) plays from this. Deleting it "
                    f"leaves that channel with nothing at all: the television "
                    f"will show colour bars and "
                    f"\"CH {number:02d}  {name}  -  NO SIGNAL\" on channel "
                    f"{number} until you point it at another folder or restore "
                    f"this from the trash."
                )
            else:
                said.append(
                    f"A scheduled block on channel {number} ({name}) plays from "
                    f"this. That part of the day will be NO SIGNAL until you "
                    f"point it somewhere else or restore this from the trash."
                )

        busy = sum(item["uploads"] for item in items)
        if busy:
            said.append(
                f"{busy} upload(s) are still arriving into this. They have to be "
                f"cancelled before it can go."
            )

        free, total = space.get("free_bytes"), space.get("total_bytes")
        if free is not None and total and free < total * LIBRARY_DISK_TIGHT:
            said.append(
                f"This disk is nearly full - {_mb(free)} left of {_mb(total)} - "
                f"and deleting will not change that by one byte. "
                f"{_mb(space['reclaimable_bytes'])} can be got back by emptying "
                f"the trash."
            )
        return said

    def _library_plan(config: Config, root: Path, paths: List[str]) -> Dict[str, Any]:
        """The whole confirmation dialog, as one answer. See deletion_plan."""
        uploads = _store_for(config)
        items = [
            library.deletion_plan(
                root, path, allowed=config.video_extensions,
                config=config, uploads=uploads,
            )
            for path in paths
        ]
        space = library.free_space(root, uploads=uploads)
        channels: List[int] = []
        dayparts: List[int] = []
        for item in items:
            for number in item["references"]["channels"]:
                if number not in channels:
                    channels.append(number)
            for number in item["references"]["dayparts"]:
                if number not in dayparts:
                    dayparts.append(number)
        return {
            "ok": True,
            "items": items,
            "totals": {
                "files": sum(item["files"] for item in items),
                "folders": sum(item["folders"] for item in items),
                "bytes": sum(item["bytes"] for item in items),
            },
            "channels": sorted(channels),
            "dayparts": sorted(dayparts),
            "uploads": sum(item["uploads"] for item in items),
            # Both of these are the same fact said twice on purpose: the flag
            # is for the page's logic, the sentence is for the person reading it.
            "frees_space": False,
            "note": items[0]["note"],
            "warnings": _library_warnings(items, space),
            "space": space,
        }

    @app.get("/api/library")
    def api_library_browse():
        config, root = _library()
        return jsonify(library.browse(
            root,
            request.args.get("path", ""),
            allowed=config.video_extensions,
            page=request.args.get("page", 1),
            per_page=request.args.get("per_page", library.DEFAULT_PAGE_SIZE),
            sort=request.args.get("sort", "name"),
            order=request.args.get("order", "asc"),
        ))

    @app.post("/api/library/plan")
    def api_library_plan():
        """What deleting this selection would cost. Changes nothing."""
        config, root = _library()
        return jsonify(_library_plan(config, root, _library_paths(_body())))

    @app.post("/api/library/delete")
    def api_library_delete():
        _confirmed()
        config, root = _library()
        body = _body()
        paths = _library_paths(body)
        cancel = bool(body.get("cancel_uploads"))
        uploads = _store_for(config)

        # Before the first byte moves. A channel that can see into the trash
        # would put every one of these files straight back on the air, and a
        # customer would be left deleting the same episode every evening.
        taught = _teach_channels_to_ignore_the_machinery(config, root)
        if taught:
            config = _need_config()

        done: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for path in paths:
            try:
                item = library.move_to_trash(
                    root, path,
                    allowed=config.video_extensions,
                    uploads=uploads,
                    cancel_uploads=cancel,
                )
            except (UnsafePath, library.LibraryError) as exc:
                if getattr(exc, "catastrophic", False):  # pragma: no cover - not from here
                    raise
                # One bad name in a selection of forty must not lose the other
                # thirty-nine, and it must not be silent either.
                failed.append({
                    "path": path,
                    "error": str(exc),
                    "status": _library_status(exc),
                })
                continue
            done.append({
                "name": item["name"], "relative": item["relative"],
                "kind": item["kind"], "token": item["token"],
                "bytes": item["bytes"], "files": item["files"],
            })

        # Nothing moved at all: answer with the refusal itself rather than a
        # cheerful 200 carrying a failure list nobody reads.
        if not done:
            raise ApiError(failed[0]["error"], failed[0]["status"])

        space = library.free_space(root, uploads=uploads)
        return _saved(config, {
            "deleted": done,
            "failed": failed,
            # Zero, said out loud, every time. This is the number people go
            # looking for on the storage gauge and do not find.
            "freed_bytes": 0,
            "trash_bytes": space["trash_bytes"],
            "trash_items": space["trash_items"],
            "space": space,
            "note": space["note"],
            "channels_taught": taught,
        })

    @app.post("/api/library/rename")
    def api_library_rename():
        config, root = _library()
        body = _body()
        result = library.rename_folder(
            store, root,
            body.get("path", ""),
            body.get("name", ""),
            uploads=_store_for(config),
            cancel_uploads=bool(body.get("cancel_uploads")),
        )
        # Re-read: the rename may have rewritten every channel path in the file,
        # and the lineup this answers with has to be the one on the disk now.
        return _saved(_need_config(), result)

    @app.get("/api/library/space")
    def api_library_space():
        """The answer to "I deleted 40 GB and the disk is still full"."""
        config, root = _library()
        return jsonify(library.free_space(root, uploads=_store_for(config)))

    @app.get("/api/library/trash")
    def api_library_trash():
        config, root = _library()
        return jsonify({
            "items": library.list_trash(root),
            "usage": library.trash_usage(root),
            "keep_days": library.DEFAULT_TRASH_DAYS,
        })

    @app.post("/api/library/trash/restore")
    def api_library_restore():
        config, root = _library()
        body = _body()
        token = body.get("token")
        if not isinstance(token, str) or not token.strip():
            raise ApiError("nothing was chosen to put back")
        replace = bool(body.get("replace"))
        if replace:
            # Restoring over something moves that something to the trash. It is
            # not lost, but it does leave the folder, so it is confirmed.
            _confirmed()
        result = library.restore(root, token.strip(), replace=replace)
        space = library.free_space(root, uploads=_store_for(config))
        return _saved(config, {**result, "space": space})

    @app.delete("/api/library/trash")
    def api_library_empty_trash():
        """The only route on this box that destroys a video file for good."""
        _confirmed()
        config, root = _library()
        token = request.args.get("token") or None
        days = request.args.get("older_than_days")
        older: Optional[float] = None
        if days:
            try:
                older = float(days)
            except (TypeError, ValueError):
                raise ApiError("older_than_days is a number of days") from None
        result = library.purge_trash(root, token=token, older_than_days=older)
        space = library.free_space(root, uploads=_store_for(config))
        log.info("emptied %d item(s) from the trash", result["items"])
        return jsonify({
            "ok": True,
            "items": result["items"],
            "bytes": result["bytes"],
            "trash_bytes": space["trash_bytes"],
            "trash_items": space["trash_items"],
            "space": space,
        })

    # -- settings -----------------------------------------------------------
    @app.get("/api/settings")
    def api_settings():
        config = _need_config()
        try:
            uploads = _store_for(config)
            spool_bytes, session_count = uploads.reclaimable(), len(uploads.sessions())
        except (ApiError, OSError):
            spool_bytes, session_count = 0, 0
        return jsonify({
            "audio_device": config.audio_device,
            "audio_devices": _audio_devices(),
            "initial_volume": config.initial_volume,
            "auto_channels": config.auto_channels,
            "sleep_timer": list(config.sleep_steps),
            "media_root": str(config.media_root) if config.media_root else None,
            "max_upload_mb": config.web.max_upload_mb,
            "min_free_mb": config.web.min_free_mb,
            "chunk_mb": config.web.chunk_mb,
            "max_files_per_upload": config.web.max_files_per_upload,
            "upload_expiry_hours": config.web.upload_expiry_hours,
            "upload_spool_bytes": spool_bytes,
            "upload_sessions": session_count,
            "video_extensions": list(config.video_extensions),
            "restart_required_for": list(RESTART_REQUIRED),
        })

    @app.post("/api/settings")
    def api_save_settings():
        body = _body()
        if not body:
            raise ApiError("nothing to change")
        unknown = sorted(set(body) - set(SETTABLE))
        if unknown:
            raise ApiError(
                f"{', '.join(unknown)} cannot be changed from here - "
                f"edit config.yaml on the box"
            )

        changes: Dict[str, Any] = {}
        if "initial_volume" in body:
            volume = _whole_number(body["initial_volume"], field="initial_volume")
            if not 0 <= volume <= 100:
                raise ApiError("initial_volume must be between 0 and 100")
            changes["initial_volume"] = volume
        if "auto_channels" in body:
            if not isinstance(body["auto_channels"], bool):
                raise ApiError("auto_channels must be true or false")
            changes["auto_channels"] = body["auto_channels"]
        if "audio_device" in body:
            raw = body["audio_device"]
            changes["audio_device"] = (
                None if raw in (None, "")
                else _clean_text(raw, field="audio_device", limit=MAX_AUDIO_DEVICE)
            )
        if "sleep_timer" in body:
            raw = body["sleep_timer"]
            if not isinstance(raw, list):
                raise ApiError("sleep_timer must be a list of minutes, or [] for off")
            steps = []
            for item in raw:
                minutes = _whole_number(item, field="sleep_timer")
                if not 1 <= minutes <= MAX_SLEEP_MINUTES:
                    raise ApiError(
                        f"each sleep timer must be between 1 and {MAX_SLEEP_MINUTES} minutes"
                    )
                if minutes not in steps:
                    steps.append(minutes)
            changes["sleep_timer"] = steps

        def mutate(data: Dict[str, Any]) -> None:
            data.update(changes)

        needs_restart = [k for k in RESTART_REQUIRED if k in changes]
        return _saved(
            store.update(mutate),
            {"settings": changes, "restart_required": needs_restart},
        )

    @app.post("/api/reload")
    def api_reload():
        return _dispatch("reload")

    # -- chunked, resumable uploads -----------------------------------------
    def _store_for(config: Config) -> UploadStore:
        """A view of the spool for the current config. Holds no state itself.

        Which chunks have arrived is read off the disk every time, so building
        one of these per request costs nothing and cannot go stale - which is
        also what makes a mid-upload reboot survivable.
        """
        if config.media_root is None:
            raise ApiError(
                "set media_root in config.yaml before uploading from here", 400
            )
        web = config.web
        return UploadStore(
            spool_for(config.media_root),
            UploadLimits(
                chunk_bytes=web.chunk_mb * 1024 * 1024,
                max_file_bytes=web.max_upload_mb * 1024 * 1024,
                max_files=web.max_files_per_upload,
                max_sessions=web.max_upload_sessions,
                min_free_bytes=web.min_free_mb * 1024 * 1024,
                expiry_seconds=web.upload_expiry_hours * 3600.0,
            ),
            allowed=config.video_extensions,
        )

    def _session_summary(store: UploadStore, session) -> Dict[str, Any]:
        missing = store.missing(session.id)
        received = sum(
            len(store.received(session.id, f.index)) for f in session.files
        )
        return {
            "id": session.id,
            "created": session.created,
            "target": session.target.as_dict(),
            "files": [f.as_dict() for f in session.files],
            "missing": {str(k): v for k, v in missing.items()},
            "chunk_bytes": store.limits.chunk_bytes,
            "total_bytes": session.total_bytes,
            "received_bytes": received * store.limits.chunk_bytes,
            "complete": not any(missing.values()),
        }

    def _upload_target(body: Dict[str, Any], config: Config, store: UploadStore):
        """Work out where a session is going, refusing anything impossible."""
        if "channel" in body:
            number = _channel_number(body["channel"])
            for channel in config.channels:
                if channel.number == number:
                    return UploadTarget(
                        kind="channel", folder=channel.path, channel_number=number
                    )
            raise ApiError(f"there is no channel {number}", 404)

        raw = body.get("new_channel")
        if not isinstance(raw, dict):
            raise ApiError("send either 'channel' or 'new_channel'")

        root = config.media_root
        # Only fall back to the channel name when no folder was named at all.
        # An empty "folder" is a bad value, not an absent one, and quietly
        # substituting something else for it would be repairing bad input.
        wanted_folder = raw["folder"] if "folder" in raw else raw.get("name")
        try:
            folder_name = safe_folder_name(wanted_folder)
        except UnsafePath as exc:
            raise ApiError(str(exc)) from None
        name = _clean_text(
            raw.get("name") or folder_name, field="name", limit=MAX_CHANNEL_NAME
        )
        try:
            folder = resolve_inside(root, root / folder_name)
        except UnsafePath as exc:
            raise ApiError(str(exc)) from None
        if folder.exists():
            raise ApiError(
                f"there is already a folder called '{folder_name}' in your library - "
                f"add to that channel instead, or pick another name", 409,
            )

        used = {c.number for c in config.channels}
        if raw.get("number") is None:
            number = (max(used) + 1) if used else config.first_channel_number
            if number > MAX_CHANNEL_NUMBER:
                raise ApiError("there is no free channel number left")
        else:
            number = _channel_number(raw["number"])
            if number in used:
                raise ApiError(f"channel {number} is already in use")

        # The folder is NOT created here, and neither is the channel. Both wait
        # until files have actually landed: a channel pointing at an empty
        # folder is an entry on the dial that plays nothing.
        return UploadTarget(
            kind="new", folder=folder, channel_number=number, channel_name=name
        )

    @app.get("/api/uploads")
    def api_uploads_list():
        config = _need_config()
        store = _store_for(config)
        store.sweep()
        _sweep_staging(config)
        return jsonify({
            "sessions": [_session_summary(store, s) for s in store.sessions()],
            "reclaimable_bytes": store.reclaimable(),
            "chunk_bytes": store.limits.chunk_bytes,
            "max_files": store.limits.max_files,
            "max_sessions": store.limits.max_sessions,
            "expiry_hours": config.web.upload_expiry_hours,
        })

    @app.post("/api/uploads")
    def api_upload_start():
        body = _body()
        config = _need_config()
        store = _store_for(config)
        # Reclaiming here is what keeps the sweep honest without a background
        # thread: the moment somebody wants space is the moment it matters.
        store.sweep()
        _sweep_staging(config)

        raw_files = body.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ApiError("send a 'files' list of what you are about to upload")

        entries: List[Tuple[Any, Any]] = []
        actions: Dict[int, str] = {}
        for index, item in enumerate(raw_files):
            if not isinstance(item, dict):
                raise ApiError("each file must be an object with a path and a size")
            action = str(item.get("action", "upload"))
            if action not in ("upload", "skip", "replace"):
                raise ApiError("action must be upload, skip or replace")
            actions[index] = action
            entries.append((item.get("path"), item.get("size")))

        target = _upload_target(body, config, store)
        try:
            session = store.create(target, entries, actions=actions)
        except UnsafePath as exc:
            raise ApiError(str(exc)) from None

        return jsonify({
            "ok": True, "session": session.id, **_session_summary(store, session),
        }), 201

    @app.get("/api/uploads/<session_id>")
    def api_upload_status(session_id: str):
        store = _store_for(_need_config())
        return jsonify(_session_summary(store, store.get(session_id)))

    @app.put("/api/uploads/<session_id>/<int:index>/<int:chunk>")
    def api_upload_chunk(session_id: str, index: int, chunk: int):
        store = _store_for(_need_config())
        try:
            written = store.put_chunk(session_id, index, chunk, request.stream)
        except (UploadError, UnsafePath):
            raise
        except Exception:  # noqa: BLE001 - the browser went away mid-chunk
            raise ApiError("that chunk did not arrive in one piece") from None
        return jsonify({
            "ok": True, "index": index, "chunk": chunk, "bytes": written,
            "missing": store.missing(session_id)[index],
        })

    @app.post("/api/uploads/<session_id>/commit")
    def api_upload_commit(session_id: str):
        config = _need_config()
        store = _store_for(config)
        session = store.get(session_id)
        results = store.commit(session_id)

        created = None
        if session.target.kind == "new":
            landed = [r for r in results if r.state in (STATE_DONE, STATE_NO_VIDEO)]
            if not landed:
                _remove_if_empty(session.target.folder)
                raise ApiError(
                    "nothing was uploaded, so no channel was created", 400
                )
            created = _create_channel_for(session, config)

        applied = send_command("reload")
        return jsonify({
            "ok": True,
            "applied": applied,
            "results": [r.as_dict() for r in results],
            "channel": created,
        })

    def _create_channel_for(session, config: Config) -> Dict[str, Any]:
        """Add the channel now that its folder has real episodes in it."""
        wanted = session.target.channel_number
        name = session.target.channel_name or session.target.folder.name
        folder = str(session.target.folder)
        made: Dict[str, Any] = {}

        def mutate(data: Dict[str, Any]) -> None:
            used = {int(e.get("number", -1)) for e in ConfigStore.channels_of(data)}
            number = wanted
            if number is None or number in used:
                # Somebody added a channel while this was uploading. Take the
                # next free number rather than throwing the upload away.
                number = (max(used) + 1) if used else config.first_channel_number
            data["channels"].append(
                {"number": number, "name": name, "path": folder}
            )
            made.update({"number": number, "name": name, "path": folder})

        store.update(mutate)
        log.info("created channel %s from an upload", made.get("number"))
        return made

    @app.delete("/api/uploads/<session_id>")
    def api_upload_cancel(session_id: str):
        store = _store_for(_need_config())
        session = store.get(session_id)
        store.cancel(session_id)
        if session.target.kind == "new":
            _remove_if_empty(session.target.folder)
        return jsonify({"ok": True, "cancelled": session_id})

    # ==================================================================
    # System: health, logs, service control, backup, the clock
    # ==================================================================
    # -- can this box still do its job -------------------------------------
    # One answer, kept for PRIVILEGE_CHECK_TTL. The lock is not for the dict:
    # it is so that ten browsers landing on the System page at once cause one
    # round of asking sudo rather than ten. The dashboard is threaded.
    _privilege_lock = threading.Lock()
    _privilege_seen: Dict[str, Any] = {"asked": None, "check": None}

    def _privileges_remembered() -> Optional[servicectl.PrivilegeCheck]:
        """The last answer, however old. Never asks. Never spawns anything."""
        return _privilege_seen["check"]

    def _privileges() -> Optional[servicectl.PrivilegeCheck]:
        """What sudo will let this box do, asked at most once every TTL.

        Returns ``None`` if the question could not be answered, which is not
        the same as an answer of "broken" and is not shown to anybody: a check
        this dashboard could not run is this dashboard's problem.
        """
        with _privilege_lock:
            asked = _privilege_seen["asked"]
            if asked is not None and (time.monotonic() - asked) < PRIVILEGE_CHECK_TTL:
                return _privilege_seen["check"]
            # Recorded before the answer, and whatever the answer turns out to
            # be: a check that keeps failing must be rationed exactly as
            # firmly as one that succeeds, or a box where sudo is missing
            # entirely is one where this endpoint has no ceiling on it at all.
            _privilege_seen["asked"] = time.monotonic()
            try:
                check = servicectl.check_privileges()
            except AssertionError:
                # The test suite's guard against a command that would touch
                # the real machine. It stays loud rather than becoming a log
                # line - see tests/conftest.py.
                raise
            except Exception:  # noqa: BLE001 - a page that loads beats a right answer
                log.warning("could not check this box's permission", exc_info=True)
                _privilege_seen["check"] = None
                return None
            _privilege_seen["check"] = check
            if check.needs_repair:
                # The only place the raw refusals are ever written down. A
                # journal is read by somebody who can act on paths and sudo's
                # own words; a television owner is not.
                log.warning(
                    "privileges %s: %s (%s)",
                    check.state, check.detail, "; ".join(check.refused),
                )
            return check

    @app.get("/api/system/privileges")
    def api_system_privileges():
        """Does the permission on this box still cover what this code runs?

        Behind a page load, deliberately, and debounced on top of that: it is
        one short-lived sudo process per privileged command, on a box that is
        playing video, and there is no login on this page to stop somebody
        asking for it over and over.
        """
        return jsonify(privilege_answer(_privileges()))

    @app.post("/api/system/privileges/repair")
    def api_system_privileges_repair():
        """Put the permission back - as far as an unprivileged page ever can.

        **Nothing in the request is read.** Not the body, not the query
        string, not a header. This route re-installs the rule servicectl
        generates for the account this process is already running as, and
        there is no parameter that can influence which account that is, which
        commands the rule grants or which file it is written to. That is the
        whole of the safety argument for a button that anybody on the home
        network can press: the request chooses nothing, so there is nothing in
        it worth sending. A username from a request reaching sudoers_rule()
        would be a rule granting root to a name the attacker picked.

        In the dashboard this always answers "changed nothing", because the
        dashboard is not root and no sudoers rule lets it write sudoers rules
        - one that did would hand this box to anyone who can reach this page.
        What it hands back instead is the exact command to type on the box.
        """
        try:
            result = servicectl.repair()
        except AssertionError:
            raise
        except Exception:  # noqa: BLE001 - see servicectl: this should not happen
            log.warning("the permission repair fell over", exc_info=True)
            raise ApiError(
                "This box could not put its permission back just now, and "
                "nothing has been changed. Trying again is safe.", 503,
            ) from None

        if result.applied:
            # Root ran this - the installer, or a person. What was true a
            # moment ago no longer is, so the next look asks again.
            with _privilege_lock:
                _privilege_seen.update({"asked": None, "check": None})
        log.info("permission repair from the dashboard: applied=%s (%s)",
                 result.applied, result.detail)
        return jsonify({
            "applied": result.applied,
            "message": result.message,
            "command": result.command,
            "user": servicectl.current_user(),
        })

    def _system_report() -> Dict[str, Any]:
        config = _config()
        report = sysinfo.report(media_root=config.media_root if config else None)
        status = read_status()
        raw_input = status.get("input") if isinstance(status.get("input"), dict) else {}
        report["input"] = {
            "backends": raw_input.get("backends") or [],
            "recent": raw_input.get("recent") or [],
        }
        report["tv_running"] = bool(status) and not _snapshot_is_stale()
        report["tv_uptime_seconds"] = _as_number(status.get("uptime_seconds"))
        report["channel_count"] = _as_int(status.get("channel_count"))
        report["config_path"] = str(store.path)
        # The last answer about this box's permission, if there is one. The
        # word only, and never a fresh round of asking: this report is behind
        # a GET on a page with no login, and the check is twenty-one processes.
        remembered = _privileges_remembered()
        report["privileges"] = remembered.state if remembered else ""
        # The trash sits on the media disk and is counted in "used", so
        # without this line it is mystery usage: gigabytes gone with nothing
        # on any screen to explain them. It goes in the storage block on the
        # System page and in the support bundle, next to the free-space figure
        # it is the explanation for.
        report["trash"] = _trash_summary(config)
        _let_the_player_win(report, status)
        return report

    def _let_the_player_win(report: Dict[str, Any],
                            status: Dict[str, Any]) -> None:
        """Overlay what the television is DOING onto what a probe inferred.

        The probe forks ``vainfo`` and ``aplay`` from this process. The
        television opened the decoder and the audio device itself. When they
        disagree the player is right by construction - it is reporting a
        decision it made, not an inference about somebody else's - and the
        disagreement is a bug in the probe, so it is logged as one.

        This is not a tidy-up. On the bench box the Watch tab said
        ``hw decode: vaapi`` while the System page said "software decode is
        being used", at the same moment, about the same mpv.
        """
        hardware = report.get("hardware")
        if not isinstance(hardware, dict):
            return

        live_decode = status.get("decode") if isinstance(status.get("decode"), dict) else {}
        live_audio = status.get("audio") if isinstance(status.get("audio"), dict) else {}
        probe_decode = hardware.get("decode") if isinstance(
            hardware.get("decode"), dict) else {}

        # -- picture --------------------------------------------------------
        decode = dict(probe_decode)
        decode["probe_working"] = probe_decode.get("working")
        decode["source"] = "probe"
        if live_decode:
            if not live_decode.get("playing"):
                # Nothing is playing, so there is no decoder in use to report.
                # Passing the probe's guess off as live state is what started
                # all of this.
                decode["source"] = "idle"
                decode["working"] = None
                decode["summary"] = (
                    "Picture: nothing is playing, so there is no decoder in "
                    "use to report. "
                    + _capability_sentence(probe_decode))
            else:
                using = live_decode.get("hwdec")
                decode["source"] = "player"
                decode["working"] = bool(using)
                decode["hwdec"] = using
                decode["summary"] = (
                    f"Picture: the television is decoding with {using}"
                    if using else
                    "Picture: the television is decoding in software")
                if probe_decode.get("working") is False and using:
                    log.warning(
                        "hardware probe says VA-API is not active but the "
                        "television is decoding with %s - trusting the "
                        "player. The probe could not see the GPU; check that "
                        "this service has the 'render' and 'video' groups.",
                        using)
                    decode["disagreed"] = True
        hardware["decode"] = decode

        # -- sound ----------------------------------------------------------
        probe_audio = hardware.get("audio") if isinstance(
            hardware.get("audio"), dict) else {}
        sound = dict(probe_audio)
        sound["probe_working"] = probe_audio.get("working")
        sound["source"] = "probe"
        if live_audio:
            sound["source"] = "player"
            sound["device"] = live_audio.get("device")
            sound["channels"] = live_audio.get("channels")
            sound["setup"] = live_audio.get("summary")
            working = live_audio.get("working")
            sound["working"] = working
            if live_audio.get("has_track") is False:
                sound["summary"] = ("Sound: what is playing has no soundtrack "
                                    "in it - nothing is wrong")
            elif working is True:
                where = live_audio.get("device") or "its chosen output"
                sound["summary"] = f"Sound: the television is playing through {where}"
            elif working is False:
                # The setup line is already a whole sentence beginning
                # "Sound:", so it is spliced in rather than stacked on top of
                # a second prefix.
                why = (live_audio.get("summary") or "")
                if why.startswith("Sound: "):
                    why = why[len("Sound: "):]
                sound["summary"] = (
                    "Sound: the television could not open an audio output"
                    + (f" - {why}" if why else "."))
            else:
                sound["summary"] = (live_audio.get("summary")
                                    or probe_audio.get("summary") or "")
            if probe_audio.get("working") is False and working:
                log.warning(
                    "hardware probe found no audio outputs but the television "
                    "is playing through %s - trusting the player. The probe "
                    "could not see the sound card; check that this service "
                    "has the 'audio' group.", live_audio.get("device"))
                sound["disagreed"] = True
        hardware["sound"] = sound

    def _capability_sentence(probe_decode: Dict[str, Any]) -> str:
        """What the hardware CAN do, said as capability rather than as state."""
        profiles = probe_decode.get("profiles") or []
        if profiles:
            short = ", ".join(str(p).replace("VAProfile", "") for p in profiles[:6])
            return f"This box can hardware-decode: {short}."
        if probe_decode.get("working") is None:
            return "Whether this box can hardware-decode could not be checked."
        return "This box has no hardware decoder available."

    def _trash_summary(config: Optional[Config]) -> Dict[str, Any]:
        """What the trash is holding, or zeroes. Never raises: this is a GET."""
        empty = {"items": 0, "bytes": 0, "keep_days": library.DEFAULT_TRASH_DAYS}
        if config is None or config.media_root is None:
            return empty
        try:
            usage = library.trash_usage(config.media_root)
        except Exception:  # noqa: BLE001 - a health page must still render
            log.debug("could not measure the trash", exc_info=True)
            return empty
        return {
            "items": usage["items"],
            "bytes": usage["bytes"],
            "oldest": usage["oldest"],
            "keep_days": library.DEFAULT_TRASH_DAYS,
        }

    @app.get("/api/system")
    def api_system():
        return jsonify(_system_report())

    @app.post("/api/system/hardware/repair")
    def api_hardware_repair():
        """Re-run detection, re-probe the sound, and say what changed.

        Deliberately grants itself no new powers. This page has no login, so
        a route that could apt-install as root would be an unauthenticated
        root vector on every box on the network. Everything that actually
        went wrong on the bench box is fixable without one: the HDMI socket
        is chosen from the kernel's own ELD, the mixer is unmuted, and the
        television is asked to look again. Anything that genuinely needs a
        package is NAMED here and installed by the installer or the update,
        which is also why this never tells anybody to open a terminal.

        Safe to press twice: it re-reads and re-applies, and re-applying the
        same answer changes nothing.
        """
        from . import audioout, hwdetect

        changed: List[str] = []
        before = _system_report().get("hardware") or {}

        try:
            report = hwdetect.build_report(run_install=False)
        except Exception as exc:  # noqa: BLE001 - a repair must never 500
            log.warning("hardware repair could not detect", exc_info=True)
            return jsonify({"ok": False,
                            "error": _for_a_customer(exc, action="hardware")}), 200

        # Unmute whatever the card has. HDMI outputs are routinely muted at
        # zero from cold, and everything above looks healthy while silent.
        try:
            unmuted = audioout.unmute()
        except Exception:  # noqa: BLE001
            unmuted = []
        if unmuted:
            changed.append(f"turned the volume up on {', '.join(unmuted)}")

        # Ask the television to look for its socket again. It is the process
        # with the audio group, so its answer is the one that counts.
        if send_command("audio_setup"):
            changed.append("asked the television to look for the sound again")
        else:
            changed.append("the television is not running, so it was not asked")

        advice = report.audio_advice or ""
        if report.decode_working is False and report.decode_packages:
            advice = (advice + " The graphics driver "
                      f"({', '.join(report.decode_packages)}) is not decoding; "
                      "an update will reinstall it.").strip()

        after = _system_report().get("hardware") or {}
        return jsonify({
            "ok": True,
            "changed": changed,
            "advice": advice,
            "before": (before.get("sound") or {}).get("summary"),
            "hardware": after,
        })

    @app.post("/api/system/sound/test")
    def api_sound_test():
        """Play a short tone, so 'is it the box or the telly' is answerable."""
        if not send_command("test_tone"):
            return jsonify({
                "ok": False,
                "error": ("The television is not running, so it cannot play a "
                          "tone. Switch the box off at the wall and on again."),
            }), 200
        return jsonify({
            "ok": True,
            "note": "Listen to the television - a two second tone is playing.",
        })

    @app.get("/api/system/logs")
    def api_system_logs():
        try:
            page = journal.read(
                unit=request.args.get("unit") or None,
                level=request.args.get("level") or None,
                search=request.args.get("search") or None,
                lines=_whole_number(
                    request.args.get("lines", journal.DEFAULT_LINES), field="lines"
                ),
                after=request.args.get("after") or None,
            )
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        # This panel is on the same tab as the permission banner, and it is
        # read by the owner of the box, not by us. Held to exactly the same
        # rule as the bundle, through exactly the same function.
        page["entries"] = _log_entries_for_reading(page.get("entries"))
        return jsonify(page)

    @app.get("/api/system/support")
    def api_system_support():
        """One block of text a customer can paste to us.

        The alternative is talking somebody through journalctl on the phone,
        which is why this exists at all.
        """
        from flask import Response

        text = _support_bundle(_system_report())
        return Response(text, mimetype="text/plain")

    @app.post("/api/system/service/<action>")
    def api_system_service(action: str):
        if action not in servicectl.ACTIONS:
            raise ApiError(f"there is no '{action}' action", 404)
        _confirmed()
        try:
            did = servicectl.run(action)
        except servicectl.ServiceError as exc:
            # .plain, never str(exc). str(exc) is what the command said, which
            # for a refused button is "sudo: a password is required" - on a box
            # whose owner has no password to type, no keyboard to type it on
            # and no idea what sudo is. The machine's own words go to the log.
            log.warning("%s failed: %s", action, exc)
            raise ApiError(exc.plain, 503) from None
        return jsonify({"ok": True, "action": action, "message": f"Asked to {did}."})

    # -- the config file itself ---------------------------------------------
    @app.get("/api/system/config")
    def api_system_config_download():
        from flask import Response

        try:
            text = store.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ApiError(f"could not read the config: {exc}", 503) from None
        return Response(
            text, mimetype="text/yaml",
            headers={"Content-Disposition": 'attachment; filename="config.yaml"'},
        )

    @app.post("/api/system/config")
    def api_system_config_restore():
        """Replace the config with an uploaded one - if it will actually boot."""
        # This throws away every setting on the box in one unauthenticated
        # request, which is the same weight as the backup restore and the
        # factory reset next to it, so it asks the same way they do.
        _confirmed()
        raw = request.get_data(as_text=True)
        if not raw or not raw.strip():
            raise ApiError("that file is empty")
        if len(raw) > MAX_CONFIG_BYTES:
            raise ApiError("that is far too large to be a config file", 413)
        config = _validated_config(raw)
        # Through the store, never straight at the file: replacing the whole
        # document has to take the same lock an edit takes, or a rename that is
        # already half way through will write its stale copy back over this one.
        store.replace(raw)
        log.info("config replaced from an upload: %d channels", len(config.channels))
        return _saved(config, {"message": "Config restored."})

    @app.get("/api/system/config/backup")
    def api_system_config_backup():
        backup = store.path.with_name(store.path.name + BACKUP_SUFFIX)
        try:
            stat = backup.stat()
        except OSError:
            return jsonify({"exists": False, "path": str(backup)})
        return jsonify({
            "exists": True,
            "path": str(backup),
            "bytes": stat.st_size,
            "modified": stat.st_mtime,
            "note": "your config from before anything automatic edited it",
        })

    @app.post("/api/system/config/backup/restore")
    def api_system_config_backup_restore():
        _confirmed()
        backup = store.path.with_name(store.path.name + BACKUP_SUFFIX)
        try:
            raw = backup.read_text(encoding="utf-8")
        except OSError:
            raise ApiError("there is no backup on this box to restore", 404) from None

        config = _validated_config(raw)
        # Straight bytes, not a round trip: the whole point of this file is
        # that it is exactly what was there before, comments and all. Through
        # the store all the same, so it cannot land in the middle of somebody
        # else's edit and be thrown away by it.
        store.replace(raw)
        log.info("config restored from %s", backup)
        return _saved(config, {"message": "Your original config is back."})

    @app.post("/api/system/factory-reset")
    def api_system_factory_reset():
        _confirmed()
        body = _body()
        if body.get("understood") is not True:
            raise ApiError(
                "this clears your channels and settings. Your video files are "
                "NOT touched. Send understood=true to go ahead."
            )
        # A config that cannot be read at all is a different problem, and
        # answering it with 503 rather than quietly writing a new file over it
        # is what it has always done.
        _need_config()

        def mutate(data: Dict[str, Any]) -> None:
            # Everything goes except where the library lives. With only
            # media_root the loader rediscovers the channels from the folders,
            # so the box comes back as if it had just been installed - with all
            # the shows still on it.
            #
            # The library location is read again in here rather than taken from
            # the copy above, because this runs under the store's lock: what
            # gets written is what the file says at the moment it is replaced,
            # and no other edit can be half way through while it happens.
            root = store.load().media_root
            if root is None:
                raise ApiError(
                    "there is no media_root set, so there would be nothing left "
                    "to build a lineup from. Set one first, or restore a backup."
                )
            data.clear()
            # Handed to the YAML writer as a value, never pasted into the text.
            # A library folder called "Films #2" written by hand comes back as
            # "/media/Films" - YAML reads from the "#" on as a comment - and the
            # reset would then rebuild the entire lineup from whatever happens
            # to be in that other folder. A name containing ": " does not parse
            # at all, and the customer is told their own reset "will not load".
            data["media_root"] = str(root)

        applied = store.update(mutate)
        log.warning("factory reset: settings cleared, media untouched")
        return _saved(applied, {
            "message": (
                "Settings and channels are back to defaults. Your video files "
                "were not touched - the lineup was rebuilt from the folders."
            ),
        })

    def _validated_config(raw: str) -> Config:
        """Prove a config loads before it is allowed anywhere near the disk."""
        import tempfile as _tempfile

        directory = store.path.parent
        handle, staged_name = _tempfile.mkstemp(
            prefix=".config-check.", suffix=".yaml", dir=str(directory)
        )
        staged = Path(staged_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(raw)
            # The real loader, in the real directory, so relative paths and
            # media_root resolve exactly as they will on the next start.
            checked = load_config(staged)
            _validated_config_paths(checked)
            _validated_config_refusals(checked)
            return checked
        except ConfigError as exc:
            raise ApiError(f"that config will not load: {exc}") from None
        except OSError as exc:
            raise ApiError(f"could not check that config: {exc}", 503) from None
        finally:
            try:
                staged.unlink()
            except OSError:  # pragma: no cover
                pass

    def _validated_config_refusals(config: Config) -> None:
        """Nothing the loader threw away may be saved to the disk from here.

        Two kinds of value in config.yaml can hurt somebody, and the loader
        knows about both (see the long note at the top of config.py):

        * one that becomes the argv of a ``subprocess.Popen`` on the
          television - ``power_off_command``, which runs the next time anybody
          shuts the box down, and ``input.cec_binary``, which runs at every
          start-up;
        * one that becomes a folder this box reads, writes or deletes inside -
          ``media_root``, a channel's ``path``, a daypart's ``path``,
          ``bumpers``, ``assets_dir``, ``input.web_socket`` - plus
          ``video_extensions``, which decides what an upload is allowed to be.

        The loader has already dropped whichever of those it would not accept,
        so the television keeps running either way. This page has no password,
        so it refuses to SAVE such a document rather than quietly correcting
        it: a customer told their file was refused is better off than one
        running settings they never chose.
        """
        if config.refusals:
            shown = "; ".join(config.refusals[:3])
            more = (
                f" (and {len(config.refusals) - 3} more)"
                if len(config.refusals) > 3 else ""
            )
            raise ApiError(f"that config will not load: {shown}{more}")

    def _validated_config_paths(config: Config) -> None:
        """Every folder it names has to be there.

        A config that parses but points at nothing produces a box full of
        channels that play nothing, which looks far more broken than a
        rejected upload does.
        """
        missing = []
        if config.media_root is not None and not Path(config.media_root).is_dir():
            missing.append(str(config.media_root))
        for channel in config.channels:
            if not Path(channel.path).is_dir():
                missing.append(str(channel.path))
            # A folder named inside a schedule is exactly as load-bearing as the
            # channel's own, and worse to diagnose: the channel is fine all day
            # and then plays nothing from 18:00, on a box nobody can log in to.
            for part in channel.dayparts:
                if part.path is not None and not Path(part.path).is_dir():
                    missing.append(str(part.path))
        if missing:
            shown = ", ".join(missing[:3])
            more = f" (and {len(missing) - 3} more)" if len(missing) > 3 else ""
            raise ApiError(
                f"that config points at folders that are not on this box: {shown}{more}"
            )

    # -- the clock -----------------------------------------------------------
    @app.get("/api/system/timezones")
    def api_system_timezones():
        return jsonify({"timezones": sysinfo.timezones()})

    @app.post("/api/system/timezone")
    def api_system_set_timezone():
        wanted = _clean_text(_body().get("timezone"), field="timezone", limit=100)
        try:
            servicectl.set_timezone(wanted, allowed=sysinfo.timezones())
        except servicectl.ServiceError as exc:
            # Same rule as the Power buttons: the plain sentence to the page,
            # the machine's own words to the journal.
            log.warning("could not set the timezone: %s", exc)
            raise ApiError(exc.plain) from None
        # Written down only once the change actually happened, and this is the
        # whole difference between "nobody has ever told this box where it is"
        # and "somebody told it, and they meant it". Without it, the next boot
        # onto a new connection asks a lookup service where the box is and
        # quietly moves the zone its owner just picked.
        _remember_chosen_timezone(wanted)
        return jsonify({"ok": True, "timezone": wanted, **sysinfo.timezone()})

    def _time_state_path() -> Path:
        return store.path.with_name(TIME_STATE_NAME)

    def _remember_chosen_timezone(zone: str) -> None:
        """Never raises: a record that could not be written is not a failed save."""
        try:
            timekeeping.record_manual_timezone(_time_state_path(), zone)
        except AssertionError:
            raise
        except Exception:  # noqa: BLE001 - the zone is already set on the box
            log.warning("could not write down that %s was chosen by hand", zone,
                        exc_info=True)

    @app.get("/api/system/clock")
    def api_system_clock():
        """Is this clock right, is anything keeping it right, and where is it?

        Everything here is ready to render: ``alarm`` is true only when the
        clock is wrong AND nothing is going to correct it by itself, which is
        the one state somebody has to act on; ``headline`` and ``detail`` name
        the coin cell and say what a wrong clock does to dayparting.

        Behind a page load, never a timer. It shells out to ``timedatectl``.
        """
        config = _config()
        detect = True if config is None else bool(config.time.detect_timezone)
        return jsonify(timekeeping.report(
            state_path=_time_state_path(), detect_enabled=detect,
        ))

    @app.post("/api/system/clock/detection")
    def api_system_clock_detection():
        """The off switch for the one outbound call this product makes itself.

        No reload is sent: the television does not read this setting - the
        dashboard does, once, when it starts - so there is nothing for the
        player to apply and nothing to interrupt for it.
        """
        wanted = _body().get("enabled")
        if not isinstance(wanted, bool):
            raise ApiError("enabled is true or false")

        def mutate(data: Dict[str, Any]) -> None:
            existing = data.get("time")
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged["detect_timezone"] = wanted
            data["time"] = merged

        store.update(mutate)
        log.info("working the timezone out from this box's location is now %s",
                 "on" if wanted else "off")
        return jsonify({
            "ok": True,
            "detection": {
                "enabled": wanted,
                "what_is_sent": timekeeping.WHAT_IS_SENT,
            },
            "note": ("This box will work its timezone out from its internet "
                     "address the next time it starts on a new connection."
                     if wanted else
                     "This box will not ask anybody where it is."),
        })

    # ==================================================================
    # Updates
    # ==================================================================
    def _updater() -> Updater:
        config = _config()
        extras = _installed_extras()
        return Updater(
            repo_dir=REPO_DIR,
            state_path=store.path.with_name(UPDATE_STATE_NAME),
            extras=extras,
        )

    def _update_config():
        config = _config()
        return config.updates if config else UpdateConfig()

    @app.get("/api/updates")
    def api_updates():
        """What we know. Deliberately does not check - see below."""
        settings = _update_config()
        last = checker.last.as_dict() if checker.last else None
        return jsonify({
            "current": retrobox_version,
            "checking_enabled": settings.check,
            "auto_apply": settings.auto_apply,
            "check_interval_hours": settings.check_interval_hours,
            "last_check": last,
            "progress": _updater().state(),
            "repository": updates.REPOSITORY,
        })

    @app.post("/api/updates/check")
    def api_updates_check():
        settings = _update_config()
        if not settings.check:
            return jsonify({
                "available": False, "current": retrobox_version,
                "error": "update checking is turned off for this box",
                "releases": [],
            })
        result = checker.check_now()
        return jsonify(result.as_dict())

    @app.post("/api/updates/apply")
    def api_updates_apply():
        _confirmed()
        body = _body()
        wanted = body.get("version")
        # Only a version, and only one this box was actually offered. There is
        # no field here that can name where an update comes from: the
        # repository is a constant in retrobox/updates.py.
        offered = {r["version"] for r in (checker.last.releases if checker.last else [])}
        if not isinstance(wanted, str) or wanted not in offered:
            raise ApiError(
                "that is not a version this box has been offered - check for "
                "updates first"
            )

        updater = _updater()
        try:
            state = updater.apply(wanted)
        except UpdateError as exc:
            state = updater.state()
            # A filesystem that forgets is its own answer: nothing was changed
            # and nothing will be until somebody undoes overlayroot.
            status = 409 if state.get("phase") == "failed" and "root" in str(exc) else 500
            return jsonify({
                "ok": False, "error": _for_a_customer(exc), "progress": state,
            }), status
        return jsonify({"ok": True, "progress": state})

    @app.post("/api/updates/rollback")
    def api_updates_rollback():
        _confirmed()
        updater = _updater()
        try:
            state = updater.rollback_now()
        except UpdateError as exc:
            raise ApiError(_for_a_customer(exc)) from None
        return jsonify({"ok": True, "progress": state})

    # ==================================================================
    # Dayparting, filler and branding - what makes it a television
    # ==================================================================
    @app.get("/api/schedule/<int:number>")
    def api_schedule(number: int):
        """The day laid out, plus what is on right now."""
        config, channel = _channel_or_404(number)
        parts = list(channel.dayparts)
        now = schedule.now_minute()
        clock = sysinfo.timezone()
        return jsonify({
            "channel": number,
            "name": channel.name,
            "blocks": schedule.to_config(parts),
            "day": schedule.day_view(parts),
            "now": {"minute": now, **schedule.preview_at(parts, now)},
            # Right here rather than buried in a system page: a wrong clock
            # makes dayparting behave in a way that looks exactly like a bug.
            #
            # Two faults, not one, and they need different things doing. A
            # clock nothing is correcting will DRIFT - annoying, gradual, and
            # nothing is wrong yet. A clock reading a date from before this
            # software existed is ALREADY playing the wrong thing at every
            # hour of the day, and it means the coin cell on the motherboard
            # is flat. `plausible` is what tells them apart, and
            # `sync_summary` is the difference between "four minutes ago" and
            # "never", which is what sends somebody to buy the part.
            "clock": {
                "local_time": clock.get("local_time"),
                "timezone": clock.get("timezone"),
                "synchronised": clock.get("synchronised"),
                "warning": clock.get("warning"),
                "plausible": clock.get("plausible"),
                "sync_summary": clock.get("sync_summary"),
            },
        })

    @app.get("/api/schedule/<int:number>/preview")
    def api_schedule_preview(number: int):
        """What would be on at a given time, without waiting for it."""
        _config, channel = _channel_or_404(number)
        minute = _whole_number(request.args.get("minute", 0), field="minute")
        return jsonify(schedule.preview_at(list(channel.dayparts), minute))

    @app.put("/api/schedule/<int:number>")
    def api_schedule_save(number: int):
        body = _body()
        raw = body.get("blocks")
        if raw is None:
            raise ApiError("send a 'blocks' list, or [] to clear the schedule")
        # The clock first, then the folders. A block that names somewhere this
        # box has not got is refused here, where somebody is looking at the
        # screen, rather than at 18:00 tonight when the channel goes quiet.
        parts = _daypart_folders(_need_config(), schedule.validate(raw))
        blocks = schedule.to_config(parts)

        def mutate(data: Dict[str, Any]) -> None:
            entry = store.channel_entry(data, number)
            if entry is None:
                raise ApiError(f"there is no channel {number}", 404)
            if blocks:
                entry["dayparts"] = blocks
            else:
                entry.pop("dayparts", None)

        config = store.update(mutate)
        return _saved(config, {
            "blocks": blocks,
            "day": schedule.day_view(parts),
        })

    # -- filler ------------------------------------------------------------
    @app.get("/api/filler")
    def api_filler():
        config = _need_config()
        assets = config.assets_dir or static_gen.DEFAULT_ASSETS_DIR
        clips = []
        for label, name in (
            ("Colour bars", static_gen.COLORBARS_FILENAME),
            ("Static", static_gen.STATIC_FILENAME),
            ("Glitch", static_gen.GLITCH_FILENAME),
        ):
            path = Path(assets) / name
            clips.append({
                "name": name, "label": label,
                "exists": path.is_file(),
                "bytes": path.stat().st_size if path.is_file() else 0,
            })
        return jsonify({
            "clips": clips,
            "ffmpeg": static_gen.ffmpeg_available(),
            "assets_dir": str(assets),
            "bumpers_dir": str(config.bumpers_dir) if config.bumpers_dir else None,
            "bumper_chance": config.bumper_chance,
            "bumper_max_seconds": config.bumper_max_seconds,
            "transition": config.transition_effect,
            "channels": [
                {"number": c.number, "name": c.name, "bumpers": c.bumpers}
                for c in config.channels
            ],
        })

    @app.get("/api/filler/<name>")
    def api_filler_clip(name: str):
        """Serve one clip so the page can play it."""
        from flask import send_file

        config = _need_config()
        known = {
            static_gen.COLORBARS_FILENAME, static_gen.STATIC_FILENAME,
            static_gen.GLITCH_FILENAME, branding.DEFAULT_SPLASH_NAME,
            branding.CUSTOM_SPLASH_NAME,
        }
        if name not in known:
            raise ApiError("that is not a clip this box holds", 404)
        path = Path(config.assets_dir or static_gen.DEFAULT_ASSETS_DIR) / name
        if not path.is_file():
            raise ApiError("that clip has not been generated yet", 404)
        return send_file(str(path), mimetype="video/mp4")

    @app.post("/api/filler/generate")
    def api_filler_generate():
        config = _need_config()
        if not static_gen.ffmpeg_available():
            raise ApiError("ffmpeg is not installed on this box", 503)
        assets = Path(config.assets_dir or static_gen.DEFAULT_ASSETS_DIR)
        try:
            made = static_gen.generate_all(assets, force=True)
        except Exception as exc:  # noqa: BLE001 - ffmpeg can fail a hundred ways
            raise ApiError(f"could not generate the clips: {exc}", 503) from None
        return jsonify({"ok": True, "generated": [p.name for p in made]})

    @app.post("/api/filler/settings")
    def api_filler_settings():
        body = _body()
        allowed = {"bumper_chance", "bumper_max_seconds", "transition"}
        unknown = sorted(set(body) - allowed - {"channels"})
        if unknown:
            raise ApiError(f"{', '.join(unknown)} cannot be set here")

        changes: Dict[str, Any] = {}
        if "bumper_chance" in body:
            raw = body["bumper_chance"]
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                raise ApiError("how often must be a number between 0 and 1")
            if not 0.0 <= float(raw) <= 1.0:
                # Capped at the top as well as the bottom: a channel that is
                # one third idents is annoying rather than nostalgic.
                raise ApiError("how often must be between 0 and 1")
            changes["bumper_chance"] = float(raw)
        if "bumper_max_seconds" in body:
            seconds = _whole_number(body["bumper_max_seconds"], field="length")
            if not 1 <= seconds <= 300:
                raise ApiError("a bumper is between 1 and 300 seconds")
            changes["bumper_max_seconds"] = seconds
        if "transition" in body:
            effect = str(body["transition"])
            if effect not in ("none", "glitch", "static"):
                raise ApiError("the channel-change effect is none, glitch or static")
            changes["transition"] = effect

        per_channel = body.get("channels") or {}
        if not isinstance(per_channel, dict):
            raise ApiError("channels must be a mapping of number to true/false")

        def mutate(data: Dict[str, Any]) -> None:
            data.update(changes)
            for raw_number, wanted in per_channel.items():
                if not isinstance(wanted, bool):
                    raise ApiError("each channel is either true or false")
                entry = store.channel_entry(data, _channel_number(raw_number))
                if entry is None:
                    raise ApiError(f"there is no channel {raw_number}", 404)
                if wanted:
                    entry.pop("bumpers", None)
                else:
                    entry["bumpers"] = False

        return _saved(store.update(mutate), {"settings": changes})

    # -- branding ----------------------------------------------------------
    @app.get("/api/branding")
    def api_branding():
        config = _need_config()
        assets = config.assets_dir or static_gen.DEFAULT_ASSETS_DIR
        return jsonify({
            "splash": branding.describe(config.boot_splash, assets),
            "max_seconds": branding.MAX_SPLASH_SECONDS,
            # Every value the shader is generated from, so the page can show
            # what the picture is actually doing. All five are writable by the
            # POST below - a value that can be read and never changed is a
            # control nobody can find and a question nobody can answer.
            "crt": {
                "enabled": config.crt.enabled,
                "curvature": config.crt.curvature,
                "scanlines": config.crt.scanlines,
                "scanline_intensity": config.crt.scanline_intensity,
                "vignette": config.crt.vignette,
                "corner_radius": config.crt.corner_radius,
            },
            "osd": {
                "channel_bug_seconds": config.channel_bug_seconds,
                "guide_seconds": config.guide_seconds,
                "color": config.ui.color,
            },
        })

    @app.post("/api/branding/splash")
    def api_branding_splash():
        """Install an uploaded clip as the boot splash, if it is fit to be one."""
        config = _need_config()
        assets = Path(config.assets_dir or static_gen.DEFAULT_ASSETS_DIR)
        declared = request.content_length
        if not declared:
            raise ApiError("the upload must declare its size (Content-Length)", 411)
        if declared > branding.MAX_SPLASH_BYTES:
            raise ApiError("that is far too large for a boot splash", 413)

        assets.mkdir(parents=True, exist_ok=True)
        # Staged in the spool, not next to the television's own assets. That
        # folder is on the disk this box boots from and nothing on the
        # dashboard ever looks in it, so a clip interrupted by the wall switch
        # would sit there for good with no way to notice or remove it.
        staging = _staging_dir(config, assets)
        handle, staged_name = tempfile.mkstemp(
            prefix="splash.", suffix=".part", dir=str(staging)
        )
        staged = Path(staged_name)
        try:
            with os.fdopen(handle, "wb") as out:
                stream_to_file(
                    request.stream, out, declared=declared,
                    limit=branding.MAX_SPLASH_BYTES,
                )
            # Checked while it is still a temporary file with a name the
            # player never looks at, so a refused clip is never installed.
            info = branding.check_splash(staged)
            os.chmod(staged, 0o644)
            _land_upload(config, staged, assets / branding.CUSTOM_SPLASH_NAME)
        except BaseException:
            try:
                staged.unlink()
            except OSError:  # pragma: no cover
                pass
            raise

        def mutate(data: Dict[str, Any]) -> None:
            data["boot_splash"] = branding.CUSTOM_SPLASH_NAME

        saved = store.update(mutate)
        return _saved(saved, {
            "splash": branding.describe(saved.boot_splash, assets),
            "seconds": round(info.duration or 0, 1),
        })

    @app.post("/api/branding/splash/default")
    def api_branding_splash_default():
        config = _need_config()
        assets = Path(config.assets_dir or static_gen.DEFAULT_ASSETS_DIR)
        if not branding.default_splash_path(assets).is_file():
            raise ApiError("the JV Projects clip is not on this box", 404)

        def mutate(data: Dict[str, Any]) -> None:
            data["boot_splash"] = branding.DEFAULT_SPLASH_NAME

        saved = store.update(mutate)
        return _saved(saved, {"splash": branding.describe(saved.boot_splash, assets)})

    @app.post("/api/branding/splash/off")
    def api_branding_splash_off():
        def mutate(data: Dict[str, Any]) -> None:
            # An explicit false, not a deleted key. The key being absent now
            # means "play the shipped clip", so removing it would turn the
            # splash back ON - the opposite of what was asked for.
            data["boot_splash"] = False

        saved = store.update(mutate)
        return _saved(saved, {"splash": branding.describe(None)})

    @app.post("/api/branding/appearance")
    def api_branding_appearance():
        body = _body()
        allowed = {key for key, _, _, _ in _CRT_SETTINGS} | {
            "channel_bug_seconds", "guide_seconds"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise ApiError(f"{', '.join(unknown)} cannot be set here")

        def number(key, low, high):
            raw = body[key]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ApiError(f"{key} must be a number")
            if not low <= float(raw) <= high:
                raise ApiError(f"{key} must be between {low} and {high}")
            return float(raw)

        # The same table, and so the same bounds, as the live preview: what
        # the customer has been watching on the television is what saving it
        # writes down.
        crt: Dict[str, Any] = _crt_from_body(body)
        top: Dict[str, Any] = {}
        if "channel_bug_seconds" in body:
            top["channel_bug_seconds"] = number("channel_bug_seconds", 0.0, 60.0)
        if "guide_seconds" in body:
            top["guide_seconds"] = number("guide_seconds", 0.0, 120.0)

        def mutate(data: Dict[str, Any]) -> None:
            data.update(top)
            if crt:
                existing = data.get("crt")
                merged = dict(existing) if isinstance(existing, dict) else {}
                merged.update(crt)
                data["crt"] = merged

        saved = store.update(mutate)
        # Nothing here needs a restart any more. `_saved` asks the television
        # to reload, and its reload path hands the new settings to
        # `player.set_crt`, which puts a freshly compiled shader on the frame
        # that is already on screen - no loadfile, no seek, no black moment.
        # So by the time this reply reaches the browser the picture has
        # already changed, and the old note telling the customer to restart
        # was being toasted at the exact moment they could see that it had.
        return _saved(
            saved, {"restart_required": []},
            applied_note=("Saved - look at the television." if crt else "Saved."),
        )

    @app.post("/api/branding/preview")
    def api_branding_preview():
        """Put the channel banner up on the actual television for a moment.

        Reuses the remote's own INFO button rather than inventing a second
        way to draw an overlay - so what is previewed is exactly what a
        viewer sees.
        """
        return _dispatch("info")

    @app.post("/api/branding/preview/picture")
    def api_branding_preview_picture():
        """Show unsaved picture settings on the television that is playing.

        This is somebody dragging the curvature slider with the television in
        front of them. Curvature is a taste setting with no correct value, so
        that is the only way anybody sets it sensibly, and the alternative -
        save, look up, change it, save again - writes six versions of a
        setting to config.yaml to find one.

        NOTHING HERE IS SAVED. The line goes to the running player over the
        control socket and no further; only SAVE PICTURE SETTINGS writes to
        config.yaml. A browser that closes mid-drag says nothing at all, so
        the box gives a preview up by itself after
        :data:`retrobox.app.TVApp.CRT_PREVIEW_HOLD_SECONDS` of silence and
        puts the saved picture back - which is what makes it safe to drag a
        slider and walk away.
        """
        body = _body()
        unknown = sorted(set(body) - {key for key, _, _, _ in _CRT_SETTINGS})
        if unknown:
            raise ApiError(f"{', '.join(unknown)} cannot be previewed")
        settings = _crt_from_body(body)
        if not settings:
            raise ApiError("there is nothing to preview")
        # "on"/"off" are two of the words the socket's own closed list takes,
        # and :g is the shortest form of a float that reads back as the same
        # number - str() of 0.1 + 0.2 would put "0.30000000000000004" on a
        # line no slider ever meant.
        def word(value: Any) -> str:
            if isinstance(value, bool):
                return "on" if value else "off"
            return f"{value:g}"

        said = " ".join(f"{name}={word(value)}" for name, value in settings.items())
        return _dispatch(f"crt_preview {said}")

    @app.post("/api/branding/preview/cancel")
    def api_branding_preview_cancel():
        """Throw a preview away and put the last SAVED picture back.

        Both the Cancel button and what the page sends on its way out, so
        somebody who dragged a slider and changed their mind does not have to
        save to undo it. Safe to call when nothing is being previewed: the
        television treats that as nothing to do.
        """
        return _dispatch("crt_cancel")

    # ==================================================================
    # Network - the one thing here that can cut you off from the box
    # ==================================================================
    def _probation() -> Probation:
        return Probation(
            state_path=store.path.with_name(NETWORK_STATE_NAME),
            writer=netconf.write_plan,
            reader=netconf.read_plan,
        )

    @app.get("/api/network")
    def api_network():
        found = netconf.interfaces()
        return jsonify({
            "interfaces": found,
            "wireless": [i["name"] for i in found if i["wireless"]],
            "nameservers": netconf.nameservers(),
            "hostname": sysinfo.addresses(),
            "change": _probation().state(),
            "usable_interfaces": [i["name"] for i in found if i["up"] and i["addresses"]],
        })

    @app.get("/api/network/test")
    def api_network_test():
        return jsonify(netconf.connectivity())

    @app.get("/api/network/scan")
    def api_network_scan():
        wireless = netconf.wireless_interfaces()
        if not wireless:
            return jsonify({"networks": [], "note": "this box has no wireless adapter"})
        wanted = request.args.get("interface") or wireless[0]
        if wanted not in wireless:
            raise ApiError("that is not a wireless interface on this box")
        return jsonify({"interface": wanted, "networks": netconf.scan(wanted)})

    def _last_interface_warning(target: str) -> None:
        """Refuse to take away the only way in without being told to."""
        found = netconf.interfaces()
        working = [i["name"] for i in found if i["up"] and i["addresses"]]
        if working and working == [target] and request.args.get("understood") != "yes":
            raise ApiError(
                f"{target} is the only interface this box is reachable on. "
                f"Changing it may leave the box unreachable until somebody plugs "
                f"a keyboard into it. If both wired and wireless are up, change "
                f"one from the other instead. Add &understood=yes to go ahead.",
                409,
            )

    @app.post("/api/network/wired")
    def api_network_wired():
        body = _body()
        interface = _clean_text(body.get("interface"), field="interface", limit=16)
        _last_interface_warning(interface)

        if body.get("mode") == "dhcp":
            plan = netconf.dhcp_plan(interface=interface)
            note = f"{interface}: address from the router"
        else:
            plan = netconf.static_plan(
                interface=interface,
                address=body.get("address"), prefix=body.get("prefix"),
                gateway=body.get("gateway"), dns=body.get("dns") or [],
            )
            note = f"{interface}: {body.get('address')}/{body.get('prefix')}"
        return jsonify(_probation().begin({netconf.WIRED_FILE: plan}, note=note))

    @app.post("/api/network/wifi")
    def api_network_wifi():
        body = _body()
        wireless = netconf.wireless_interfaces()
        if not wireless:
            raise ApiError("this box has no wireless adapter")
        interface = body.get("interface") or wireless[0]
        if interface not in wireless:
            raise ApiError("that is not a wireless interface on this box")
        _last_interface_warning(interface)

        plan = netconf.wifi_plan(
            interface=interface,
            ssid=body.get("ssid"), password=body.get("password"),
            address=body.get("address"), prefix=body.get("prefix"),
            gateway=body.get("gateway"), dns=body.get("dns") or [],
        )
        # The SSID is echoed back to the page but never put on a command line.
        return jsonify(_probation().begin(
            {netconf.WIFI_FILE: plan}, note=f"wifi: {body.get('ssid')}"
        ))

    @app.post("/api/network/wifi/forget")
    def api_network_forget():
        _confirmed()
        wireless = netconf.wireless_interfaces()
        if not wireless:
            raise ApiError("this box has no wireless adapter")
        _last_interface_warning(wireless[0])
        # An empty document rather than a deletion: the unprivileged process
        # cannot remove a root-owned file, and empty contributes nothing.
        from .netprobation import EMPTY_PLAN

        return jsonify(_probation().begin(
            {netconf.WIFI_FILE: EMPTY_PLAN}, note="forget the wireless network"
        ))

    @app.post("/api/network/confirm")
    def api_network_confirm():
        return jsonify(_probation().confirm())

    @app.post("/api/network/revert")
    def api_network_revert():
        return jsonify(_probation().revert())

    @app.post("/api/network/hostname")
    def api_network_hostname():
        name = _clean_text(body_hostname(_body()), field="hostname", limit=32)
        try:
            code, output = servicectl._run(
                ["sudo", "-n", "hostnamectl", "set-hostname", name]
            )
        except Exception:  # noqa: BLE001
            raise ApiError("could not change the hostname", 503) from None
        if code != 0:
            # Whatever the command said, in words for the person in front of
            # the television. A refusal here is the stale-sudoers fault and
            # comes back as the sentence that says what to do about it; a real
            # failure ("Read-only file system") keeps its own words, minus
            # anything sudo wrote about itself.
            log.warning("could not change the hostname: %s", output[:200])
            raise ApiError(
                servicectl.explain_failure(output, action="network"), 503
            )
        return jsonify({
            "ok": True, "hostname": name,
            "message": (
                f"This box is now called {name}. Its address has changed to "
                f"http://{name}.local/ - the old one will stop working."
            ),
        })

    # -- start-up housekeeping ----------------------------------------------
    # Both of these are here because nothing runs a timer while a box is
    # switched off at the wall, and the wall switch is how this one is
    # switched off. Start-up is the first moment either question can be asked
    # again. Neither may stop the dashboard coming up: it is the only thing
    # somebody with a sick box can still reach.

    # A network change that was still on trial when the dashboard stopped is
    # put back now, rather than the next time somebody opens the network page.
    # That distinction is the whole point: netplan reverts what it applied
    # when its terminal dies, but our netplan files still hold the untested
    # configuration, and the next boot applies it for good. If that
    # configuration is why the box cannot be reached, nobody can open the
    # network page to trigger the undo - so it has to happen without them.
    # On an idle box this is one small file read.
    try:
        _probation().state()
    except AssertionError:
        # The test suite's guard against a command that would touch the real
        # machine. It must stay loud rather than becoming a log line.
        raise
    except Exception:  # noqa: BLE001 - never stop the dashboard starting
        log.warning("could not settle an interrupted network change", exc_info=True)

    # And anything an interrupted upload left in the spool.
    try:
        loaded = store.load()
        _store_for(loaded).sweep()
        _sweep_staging(loaded)
    except Exception:  # noqa: BLE001 - never let housekeeping stop the server
        log.debug("could not sweep abandoned uploads at start-up", exc_info=True)

    # And whether this box can still do the privileged half of its job. A box
    # nobody ever opens the dashboard on gets the fault into its journal this
    # way, and a box somebody does open has the answer waiting.
    #
    # On its own thread, and never joined: asking is up to twenty-one
    # short-lived processes, and a wedged sudo would otherwise hold the first
    # page open for as long as they all take to give up. A box with a broken
    # permission file is exactly the box that most needs its dashboard to come
    # up, so nothing here is allowed to be in the way of that.
    app.privilege_startup = None
    if os.environ.get(STARTED_BY_SYSTEMD):
        def _check_at_startup() -> None:
            try:
                _privileges()
            except Exception:  # noqa: BLE001 - a thread dying loudly helps nobody
                log.warning("the start-up permission check fell over", exc_info=True)

        app.privilege_startup = threading.Thread(
            target=_check_at_startup, name="privilege-check", daemon=True
        )
        app.privilege_startup.start()

    # And the clock. Two jobs, both of which can only be done at start-up.
    #
    # The first is writing down whether this box woke up believing it was
    # 2011, because the fix erases the evidence: a flat coin cell on a box
    # with internet is wrong for forty seconds and then perfectly normal, and
    # by the time anybody opens the dashboard there is nothing left to see.
    #
    # The second is working the timezone out from the box's address, once, on
    # a box nobody has ever told where it is - an installer's Etc/UTC makes
    # every daypart fire at the wrong hour, and nothing else on this box will
    # ever notice.
    #
    # Behind the same INVOCATION_ID gate as the check above, and for a
    # stronger reason: detection is an outbound request to a third party.
    # A checkout, a laptop, or this test suite is not a box that has booted,
    # and none of them should be phoning anybody.
    #
    # `timekeeping.start` returns immediately and its thread is a daemon.
    # Nothing joins it: a lookup that never answers must not be able to hold
    # up a page, and a box whose time record is corrupt still gets a working
    # dashboard.
    app.timekeeping_startup = None
    if os.environ.get(STARTED_BY_SYSTEMD):
        try:
            app.timekeeping_startup = timekeeping.start(
                state_path=store.path.with_name(TIME_STATE_NAME),
                config=_config(),
            )
        except AssertionError:
            raise
        except Exception:  # noqa: BLE001 - the dashboard outranks the clock
            log.warning("could not look at this box's clock at start-up",
                        exc_info=True)

    return app


def _remove_if_empty(folder: Path) -> None:
    """Take back a folder we made for an upload that never happened."""
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:  # pragma: no cover - somebody else is using it
        pass


VIEWER_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Retro Box</title>
<style>""" + webstyle.VIEWER_CSS + """</style></head><body><div class="wrap">

  <p class="mark">JV PROJECTS</p>
  <h1>RETRO BOX</h1>

  <div id="screen"></div>

  <div class="panel">
    <h2>Now playing</h2>
    <p class="ch" id="ch">&mdash;</p>
    <p class="show" id="show"></p>
    <p class="meta" id="meta">connecting&hellip;</p>
    <div class="meter" id="bar" hidden><i id="fill" style="width:0%"></i></div>
    <div class="times" id="times" hidden>
      <span id="elapsed"></span><span id="left"></span>
    </div>
  </div>

  <div class="panel">
    <h2>Also on</h2>
    <div id="lineup"><p class="empty">&hellip;</p></div>
  </div>

  <div class="foot">
    <span class="dim" id="version"></span>
    <a href="/dash">MANAGE THIS BOX &rarr;</a>
  </div>

</div>
<script>
"use strict";
const $ = s => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const pad = n => String(n ?? 0).padStart(2, '0');

function clock(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor(seconds % 3600 / 60);
  const s = seconds % 60;
  return (h ? h + ':' + pad(m) : String(m)) + ':' + pad(s);
}

/* The snapshot is rewritten every couple of seconds, so the timer is carried
   forward locally in between - otherwise the clock visibly stutters. */
let latest = null;
let takenAt = 0;

function draw() {
  if (!latest) return;
  const s = latest;

  if (!s.online) {
    $('#ch').textContent = '\\u2014';
    $('#show').textContent = '';
    $('#meta').textContent = s.stale
      ? 'the TV stopped responding'
      : 'the TV is not running';
    $('#bar').hidden = true;
    $('#times').hidden = true;
    drawLineup((s.channels_configured || []).map(c => ({...c, quiet: true})));
    return;
  }

  const ch = s.channel || {};
  $('#ch').textContent = s.standby
    ? 'STANDBY'
    : 'CH ' + pad(ch.number) + '   ' + (ch.name || '');
  $('#show').textContent = s.standby ? ''
    : (s.off_air ? 'OFF AIR' : (s.now_playing || ''));
  $('#show').className = 'show' + (s.off_air ? ' off' : '');

  const moved = (Date.now() - takenAt) / 1000;
  const at = s.position === null ? null : s.position + moved;

  if (at === null || s.standby || s.off_air) {
    $('#meta').textContent = s.standby ? 'the screen is blank' : '';
    $('#bar').hidden = true;
    $('#times').hidden = true;
  } else if (s.duration) {
    const done = Math.min(1, at / s.duration);
    $('#fill').style.width = (done * 100).toFixed(1) + '%';
    $('#bar').hidden = false;
    $('#times').hidden = false;
    $('#elapsed').textContent = clock(at) + ' in';
    $('#left').textContent = clock(s.duration - at) + ' left';
    $('#meta').textContent = '';
  } else {
    // No duration for this file - the box only reports one it already knew.
    $('#bar').hidden = true;
    $('#times').hidden = true;
    $('#meta').textContent = clock(at) + ' in';
  }

  drawLineup(s.lineup || []);
  $('#version').textContent = s.version ? 'v' + s.version : '';
}

function drawLineup(rows) {
  const host = $('#lineup');
  host.textContent = '';
  if (!rows.length) {
    host.append(el('p', 'empty', 'no channels'));
    return;
  }
  for (const c of rows) {
    const row = el('div', 'row' + (c.current ? ' on' : ''));
    row.append(el('span', 'led', pad(c.number)), el('span', 'grow', c.name || ''));
    if (c.off_air) row.append(el('span', 'tiny off', 'OFF AIR'));
    else if (c.now_playing) row.append(el('span', 'tiny', c.now_playing));
    host.append(row);
  }
}

async function poll() {
  try {
    const res = await fetch('/api/now');
    latest = await res.json();
    takenAt = Date.now();
  } catch (e) { /* keep showing the last good read */ }
  draw();
}

poll();
setInterval(poll, 3000);
setInterval(draw, 1000);      // keep the clock moving between polls
</script></body></html>
"""


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Retro Box &middot; Manage</title>
<style>""" + webstyle.CONSOLE_CSS + """</style></head><body><div class="wrap">

  <header>
    <p class="mark">JV PROJECTS</p>
    <h1>RETRO BOX</h1>
    <p class="sub" id="sub">connecting&hellip;</p>
  </header>

  <nav role="tablist">
    <button role="tab" aria-selected="true" data-tab="watch">WATCH</button>
    <button role="tab" aria-selected="false" data-tab="channels">CHANNELS</button>
    <button role="tab" aria-selected="false" data-tab="add">ADD</button>
    <button role="tab" aria-selected="false" data-tab="library">FILES</button>
    <button role="tab" aria-selected="false" data-tab="settings">SETTINGS</button>
    <button role="tab" aria-selected="false" data-tab="tv">TV</button>
    <button role="tab" aria-selected="false" data-tab="system">SYSTEM</button>
  </nav>

  <section id="tab-watch">
    <div class="panel">
      <p class="now" id="now">&mdash;</p>
      <p class="meta" id="meta"></p>
    </div>
    <div class="panel">
      <h2>Controls</h2>
      <div class="bar">
        <button data-cmd="/api/volume/down">VOL &minus;</button>
        <button data-cmd="/api/volume/up">VOL +</button>
        <button data-cmd="/api/mute">MUTE</button>
        <button data-cmd="/api/power">STANDBY</button>
        <button class="danger" id="shutdown">SHUT DOWN</button>
      </div>
    </div>
    <div class="panel">
      <h2>Tune</h2>
      <div id="tune"></div>
    </div>
  </section>

  <section id="tab-channels" hidden>
    <div class="panel">
      <h2>Lineup</h2>
      <div id="editor"></div>
      <div class="bar" style="margin-top:.8rem">
        <button id="add">+ ADD CHANNEL</button>
      </div>
    </div>
  </section>

  <section id="tab-add" hidden>
    <div class="panel">
      <h2>Add shows</h2>
      <div id="drop" class="drop">
        <p class="dropline">DROP A FOLDER OR FILES HERE</p>
        <p class="note" id="dropnote">&nbsp;</p>
        <div class="bar">
          <button id="pick-files">CHOOSE FILES</button>
          <button id="pick-folder">CHOOSE A FOLDER</button>
        </div>
      </div>
      <div id="plan"></div>
    </div>
    <div class="panel" id="queue-panel" hidden>
      <h2>Uploading</h2>
      <div id="overall"></div>
      <div id="queue"></div>
      <div class="bar" style="margin-top:.8rem">
        <button id="pause">PAUSE</button>
        <button class="danger" id="abandon">CANCEL UPLOAD</button>
      </div>
    </div>
    <div class="panel" id="resume-panel" hidden>
      <h2>Unfinished</h2>
      <div id="resume"></div>
    </div>
  </section>

  <!-- The file manager. This is the only way anybody who bought this box can
       see what is on its disk: there is no SSH, no keyboard and no screen but
       the television. So it shows the box's own folders too, greyed and
       unselectable, because a customer hunting forty missing gigabytes has to
       be able to find them. -->
  <section id="tab-library" hidden>
    <div class="panel">
      <h2>Files</h2>
      <p class="note" id="lib-space">&hellip;</p>
      <div class="crumbs" id="lib-crumbs"></div>
      <div id="lib-list"><p class="empty">&hellip;</p></div>
      <div class="pager" id="lib-pager" hidden>
        <button id="lib-prev" class="ghost">&larr; BACK</button>
        <span class="of" id="lib-of"></span>
        <button id="lib-next" class="ghost">MORE &rarr;</button>
      </div>
      <div id="lib-confirm"></div>
      <!-- Sticky, and always on the screen: SELECT ALL on a folder of six
           hundred episodes otherwise puts this six hundred rows down. -->
      <div class="libbar">
        <button id="lib-all" class="ghost">SELECT ALL</button>
        <span class="count" id="lib-count">nothing selected</span>
        <button id="lib-rename" class="ghost" disabled>RENAME</button>
        <button class="danger" id="lib-delete" disabled>DELETE</button>
      </div>
    </div>

    <div class="panel">
      <h2>Trash</h2>
      <p class="note">Deleting moves things here rather than destroying them.
        They stay on the same disk - still taking up the room - until the trash
        is emptied, and the box clears anything older than a fortnight by
        itself.</p>
      <div id="lib-trash"><p class="empty">&hellip;</p></div>
      <div id="lib-trash-confirm"></div>
      <div class="bar" style="margin-top:.8rem">
        <button class="danger ghost" id="lib-empty" disabled>EMPTY THE TRASH</button>
      </div>
    </div>
  </section>

  <section id="tab-tv" hidden>
    <div class="panel">
      <h2>Schedule</h2>
      <p class="note">A channel can be a different thing at different times of
        day - cartoons in the morning, something else after dark.</p>
      <div class="field"><label>Channel</label><select id="sched-channel"></select></div>
      <div id="sched-clock"></div>
      <div id="sched-day"></div>
      <div id="sched-now"></div>
      <div id="sched-blocks"></div>
      <div class="bar" style="margin-top:.8rem">
        <button id="sched-add">+ ADD A BLOCK</button>
        <button id="sched-save">SAVE THE SCHEDULE</button>
      </div>
      <div class="field"><label>What would be on at</label>
        <input id="sched-when" type="time" value="03:00"></div>
      <p class="note" id="sched-preview"></p>
    </div>

    <div class="panel">
      <h2>Filler and bumpers</h2>
      <div id="filler"><p class="empty">&hellip;</p></div>
    </div>

    <div class="panel">
      <h2>Start-up clip</h2>
      <div id="branding"><p class="empty">&hellip;</p></div>
    </div>

    <div class="panel">
      <h2>Picture</h2>
      <div id="appearance"><p class="empty">&hellip;</p></div>
    </div>
  </section>

  <section id="tab-settings" hidden>
    <div class="panel">
      <h2>Settings</h2>
      <div id="settings"></div>
    </div>
  </section>

  <section id="tab-system" hidden>
    <!-- Above Health, above Power, above everything: this is the panel that
         explains why the buttons further down do not work. A customer who
         reads it after pressing one has already had the bad experience. -->
    <div class="panel alarm" id="privileges" hidden></div>

    <div class="panel">
      <h2>Health</h2>
      <div id="health"><p class="empty">&hellip;</p></div>
      <div class="bar" style="margin-top:.8rem">
        <button id="detail-toggle" class="ghost">SHOW THE RAW DETAIL</button>
      </div>
      <pre id="detail" class="raw" hidden></pre>
    </div>

    <div class="panel">
      <h2>Remote test</h2>
      <p class="note">Press a button on the remote. It should appear here within
        a second. This watches the box; it does not take the remote over.</p>
      <div id="inputs"><p class="empty">nothing yet</p></div>
    </div>

    <div class="panel">
      <h2>Log</h2>
      <div class="split">
        <div class="field"><label>Service</label><select id="log-unit">
          <option value="">Both</option>
          <option value="retrobox.service">The television</option>
          <option value="retrobox-web.service">This dashboard</option>
        </select></div>
        <div class="field"><label>Level</label><select id="log-level">
          <option value="">Everything</option>
          <option value="warning">Warnings and worse</option>
          <option value="error">Errors only</option>
        </select></div>
      </div>
      <div class="field"><label>Search</label><input id="log-search"
        placeholder="plain text, not a pattern"></div>
      <div class="bar" style="margin-top:.6rem">
        <button id="log-refresh">REFRESH</button>
        <button id="log-follow" class="ghost">FOLLOW</button>
        <button id="log-copy" class="ghost">COPY FOR SUPPORT</button>
      </div>
      <pre id="log" class="raw"></pre>
    </div>

    <div class="panel">
      <h2>Clock</h2>
      <!-- The alarm goes above the readings, not under them: a box that
           thinks it is 2011 is already playing the wrong thing at every hour
           of the day, and that is not a footnote to what time it says it is. -->
      <div class="panel alarm" id="clock-alarm" hidden></div>
      <div id="clock"><p class="empty">&hellip;</p></div>
      <div id="clock-detect"></div>
    </div>

    <div class="panel">
      <h2>Config file</h2>
      <p class="note">Keep a copy somewhere safe before you change much.</p>
      <div class="bar">
        <button id="cfg-download">DOWNLOAD config.yaml</button>
        <button id="cfg-upload" class="ghost">RESTORE FROM A FILE</button>
      </div>
      <div id="cfg-backup"></div>
    </div>

    <div class="panel">
      <h2>Network</h2>
      <div id="net-probation" hidden></div>
      <div id="net"><p class="empty">&hellip;</p></div>
      <div class="bar" style="margin-top:.8rem">
        <button id="net-test" class="ghost">TEST THE CONNECTION</button>
        <button id="net-wifi" class="ghost">JOIN A WIFI NETWORK</button>
        <button id="net-wired" class="ghost">WIRED SETTINGS</button>
      </div>
      <div id="net-form"></div>
    </div>

    <div class="panel">
      <h2>Software</h2>
      <div id="update"><p class="empty">&hellip;</p></div>
    </div>

    <div class="panel">
      <h2>Power</h2>
      <div class="bar" id="service-buttons"></div>
      <div class="bar" style="margin-top:1rem">
        <button class="danger ghost" id="factory">FACTORY RESET</button>
      </div>
      <p class="note">A factory reset clears your channels and settings.
        <strong>Your video files are not touched</strong> - the lineup is
        rebuilt from the folders in your library.</p>
    </div>
  </section>

</div><div id="toast" role="status" aria-live="polite"></div>
<script>
"use strict";
const $ = s => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;   // never innerHTML for box data
  return n;
};
const pad = n => String(n ?? 0).padStart(2, '0');
const mb = b =>
  b >= 1073741824 ? (b/1073741824).toFixed(1) + ' GB'
  : b >= 1048576  ? Math.round(b/1048576) + ' MB'
  : Math.max(1, Math.round(b/1024)) + ' KB';

let toastTimer;
function toast(message, bad) {
  const t = $('#toast');
  t.textContent = message;
  t.className = 'show' + (bad ? ' bad' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ''; }, 3600);
}

async function api(url, options) {
  const res = await fetch(url, options);
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const failure = new Error(data.error || ('request failed: ' + res.status));
    // The code as well as the words. Some refusals have a second answer the
    // page can offer - a 409 from a restore means something is already at
    // that name, and the honest reply is "replace it?" - and matching on the
    // wording of a sentence to find that out would break the day somebody
    // improves the sentence.
    failure.status = res.status;
    throw failure;
  }
  return data;
}
const send = url => api(url, {method:'POST'}).catch(e => toast(e.message, true));
const json = (url, method, body) => api(url, {
  method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
});

/* Two taps to do anything destructive: the second one is the confirmation. */
function arm(button, confirmLabel, run) {
  const original = button.textContent;
  let ready = false, timer;
  button.onclick = () => {
    if (!ready) {
      ready = true;
      button.textContent = confirmLabel;
      button.classList.add('armed');
      timer = setTimeout(() => {
        ready = false; button.textContent = original; button.classList.remove('armed');
      }, 4000);
      return;
    }
    clearTimeout(timer);
    ready = false; button.textContent = original; button.classList.remove('armed');
    run();
  };
  return button;
}

/* -- tabs ---------------------------------------------------------------- */
const TABS = ['watch', 'channels', 'add', 'library', 'tv', 'settings', 'system'];
document.querySelectorAll('nav button').forEach(tab => {
  tab.onclick = () => {
    TABS.forEach(name => {
      const chosen = name === tab.dataset.tab;
      $('#tab-' + name).hidden = !chosen;
      document.querySelector('[data-tab="' + name + '"]').setAttribute('aria-selected', chosen);
    });
    if (tab.dataset.tab === 'channels') loadEditor();
    if (tab.dataset.tab === 'settings') loadSettings();
    if (tab.dataset.tab === 'add') loadUnfinished();
    if (tab.dataset.tab === 'library') loadLibrary();
    if (tab.dataset.tab === 'system') loadSystem();
    if (tab.dataset.tab === 'tv') loadTelevision();
  };
});
document.querySelectorAll('[data-cmd]').forEach(b => {
  b.onclick = () => send(b.dataset.cmd).then(() => setTimeout(refresh, 250));
});
arm($('#shutdown'), 'TAP AGAIN TO SHUT DOWN', () => {
  send('/api/shutdown?confirm=yes');
  $('#sub').textContent = 'shutting down\\u2026';
});

/* -- watch --------------------------------------------------------------- */
async function refresh() {
  try {
    const s = await (await fetch('/api/status')).json();
    const sub = $('#sub');
    if (!s.online) {
      sub.textContent = 'the TV process is not running';
      sub.className = 'sub offline';
      $('#now').textContent = '\\u2014';
      $('#meta').textContent = 'channel changes will be saved and applied when it starts';
    } else {
      sub.className = 'sub';
      sub.textContent = 'v' + (s.version||'') + ' \\u00b7 up ' + uptime(s.uptime_seconds);
      const ch = s.channel || {};
      $('#now').textContent =
        s.standby ? 'STANDBY'
        : s.off_air ? ('CH ' + pad(ch.number) + '  OFF AIR')
        : ('CH ' + pad(ch.number) + '  ' + (ch.name||''));
      const bits = [s.muted ? 'muted' : ('volume ' + s.volume)];
      if (s.now_playing) bits.push(s.now_playing);
      if (s.sleep_minutes) bits.push('sleep ' + s.sleep_minutes + 'm');
      /* An idle television has not chosen a decoder, so it is not "software
         decode" - it is nothing yet. Calling it software decode here is the
         same mistake the System page used to make in the other direction. */
      const d = s.decode || {};
      if (d.playing === false) bits.push('nothing playing');
      else bits.push(s.hwdec ? ('hw decode: ' + s.hwdec) : 'software decode');
      $('#meta').textContent = bits.join('  \\u00b7  ');
    }
  } catch (e) { /* keep the last good render */ }

  try {
    const {channels} = await (await fetch('/api/channels')).json();
    const list = $('#tune');
    list.textContent = '';
    if (!channels.length) { list.append(el('p','empty','No channels yet. Add one under CHANNELS.')); return; }
    for (const c of channels) {
      const row = el('button', 'row' + (c.current ? ' on' : ''));
      row.type = 'button';
      row.append(el('span', 'led', pad(c.number)), el('span', 'grow', c.name));
      row.onclick = () => send('/api/tune/' + c.number).then(() => setTimeout(refresh, 250));
      list.append(row);
    }
  } catch (e) { /* keep the last good render */ }
}
function uptime(s) {
  s = Math.floor(s||0);
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60);
  return h ? (h + 'h ' + m + 'm') : (m + 'm');
}

/* -- channel editor ------------------------------------------------------ */
let openChannel = null;

async function loadEditor() {
  const host = $('#editor');
  let channels;
  try {
    channels = (await api('/api/channels')).channels;
  } catch (e) { host.textContent = ''; host.append(el('p','empty',e.message)); return; }

  host.textContent = '';
  if (!channels.length) { host.append(el('p','empty','No channels yet.')); return; }

  channels.forEach((c, index) => {
    const head = el('button', 'row');
    head.type = 'button';
    head.setAttribute('aria-expanded', String(openChannel === c.number));
    head.append(el('span', 'led', pad(c.number)), el('span', 'grow', c.name),
                el('span', 'tiny', openChannel === c.number ? 'CLOSE' : 'EDIT'));
    head.onclick = () => { openChannel = openChannel === c.number ? null : c.number; loadEditor(); };
    host.append(head);
    if (openChannel === c.number) host.append(channelEditor(c, index, channels.length));
  });
}

function channelEditor(channel, index, total) {
  const box = el('div', 'edit');

  const name = el('input'); name.value = channel.name; name.maxLength = 48;
  const number = el('input'); number.type = 'number'; number.min = 0; number.max = 999;
  number.value = channel.number;
  const folder = el('input'); folder.value = channel.path;

  const split = el('div', 'split');
  const numField = el('div', 'field narrow');
  numField.append(el('label', null, 'Channel'), number);
  const nameField = el('div', 'field');
  nameField.append(el('label', null, 'Name'), name);
  split.append(numField, nameField);

  const folderField = el('div', 'field');
  folderField.append(el('label', null, 'Folder'), folder);

  const save = el('button', null, 'SAVE');
  save.onclick = async () => {
    try {
      await json('/api/channels/' + channel.number, 'PATCH', {
        name: name.value, number: Number(number.value), path: folder.value,
      });
      toast('Saved channel ' + pad(number.value));
      openChannel = Number(number.value);
      loadEditor(); refresh();
    } catch (e) { toast(e.message, true); }
  };

  const up = el('button', 'ghost', '\\u2191');
  up.title = 'Move up'; up.disabled = index === 0;
  const down = el('button', 'ghost', '\\u2193');
  down.title = 'Move down'; down.disabled = index === total - 1;
  const move = async delta => {
    try {
      const list = (await api('/api/channels')).channels.map(c => c.number);
      const to = index + delta;
      [list[index], list[to]] = [list[to], list[index]];
      await json('/api/channels/reorder', 'POST', {order: list});
      openChannel = null;
      toast('Lineup renumbered');
      loadEditor(); refresh();
    } catch (e) { toast(e.message, true); }
  };
  up.onclick = () => move(-1);
  down.onclick = () => move(1);

  const remove = arm(el('button', 'danger ghost', 'DELETE CHANNEL'),
    'TAP AGAIN TO DELETE', async () => {
      try {
        await api('/api/channels/' + channel.number + '?confirm=yes', {method:'DELETE'});
        toast('Channel removed. The video files were left alone.');
        openChannel = null;
        loadEditor(); refresh();
      } catch (e) { toast(e.message, true); }
    });

  const bar = el('div', 'bar'); bar.style.marginTop = '.7rem';
  bar.append(save, up, down, remove);
  box.append(split, folderField, bar, mediaPanel(channel.number));
  return box;
}

/* -- files on a channel --------------------------------------------------- */
function mediaPanel(number) {
  const box = el('div');
  box.style.marginTop = '1rem';
  box.append(el('h2', null, 'Files'));
  const list = el('div');
  const status = el('p', 'note');
  box.append(list, status);

  const draw = files => {
    list.textContent = '';
    if (!files.length) { list.append(el('p','empty','Nothing on this channel yet.')); return; }
    for (const f of files) {
      const row = el('div', 'row');
      row.append(el('span', 'grow', f.name), el('span', 'tiny', mb(f.bytes)));
      row.append(arm(el('button', 'danger ghost', 'DELETE'), 'TAP AGAIN', async () => {
        try {
          const data = await api('/api/media/' + number + '/' +
            encodeURIComponent(f.name) + '?confirm=yes', {method:'DELETE'});
          toast('Deleted ' + f.name);
          draw(data.files);
        } catch (e) { toast(e.message, true); }
      }));
      list.append(row);
    }
  };
  api('/api/media/' + number).then(d => draw(d.files)).catch(e => status.textContent = e.message);

  const picker = el('input');
  picker.type = 'file';
  picker.accept = 'video/*';
  picker.style.display = 'none';

  const meter = el('div', 'meter');
  const fill = el('i'); fill.style.width = '0%';
  meter.append(fill);
  const pct = el('span', null, '0%');
  const progress = el('div', 'progress');
  progress.append(meter, pct);
  progress.hidden = true;

  const choose = el('button', null, 'UPLOAD A VIDEO');
  choose.onclick = () => picker.click();
  picker.onchange = () => {
    const file = picker.files[0];
    if (!file) return;
    progress.hidden = false;
    fill.style.width = '0%'; pct.textContent = '0%';
    choose.disabled = true;

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/media/' + number + '?name=' + encodeURIComponent(file.name));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const done = Math.round(e.loaded / e.total * 100);
      fill.style.width = done + '%';
      pct.textContent = done + '%';
    };
    xhr.onload = () => {
      choose.disabled = false;
      progress.hidden = true;
      picker.value = '';
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* empty */ }
      if (xhr.status === 201) { toast('Uploaded ' + data.name); draw(data.files || []); }
      else toast(data.error || ('Upload failed: ' + xhr.status), true);
    };
    xhr.onerror = () => {
      choose.disabled = false; progress.hidden = true; picker.value = '';
      toast('Upload failed - the box could not be reached', true);
    };
    xhr.send(file);
  };

  const bar = el('div', 'bar'); bar.style.marginTop = '.6rem';
  bar.append(choose);
  box.append(bar, progress, picker);
  return box;
}

/* -- adding a channel ----------------------------------------------------- */
$('#add').onclick = () => {
  const host = $('#editor');
  const box = el('div', 'edit');
  const name = el('input'); name.placeholder = 'Cartoons'; name.maxLength = 48;
  const folder = el('input'); folder.placeholder = 'folder inside the media library';

  const nameField = el('div', 'field');
  nameField.append(el('label', null, 'Name'), name);
  const folderField = el('div', 'field');
  folderField.append(el('label', null, 'Folder'), folder);

  const create = el('button', null, 'CREATE');
  create.onclick = async () => {
    try {
      const data = await json('/api/channels', 'POST', {name: name.value, path: folder.value});
      toast('Added channel ' + pad(data.channel.number));
      loadEditor(); refresh();
    } catch (e) { toast(e.message, true); }
  };
  const cancel = el('button', 'ghost', 'CANCEL');
  cancel.onclick = () => loadEditor();
  const bar = el('div', 'bar'); bar.style.marginTop = '.7rem';
  bar.append(create, cancel);

  box.append(nameField, folderField, bar);
  host.append(box);
  name.focus();
};

/* -- settings ------------------------------------------------------------- */
async function loadSettings() {
  const host = $('#settings');
  let s;
  try { s = await api('/api/settings'); }
  catch (e) { host.textContent = ''; host.append(el('p','empty',e.message)); return; }

  host.textContent = '';

  const audio = el('select');
  const auto = el('option', null, 'Let the box choose');
  auto.value = '';
  audio.append(auto);
  const devices = s.audio_devices.slice();
  if (s.audio_device && !devices.includes(s.audio_device)) devices.push(s.audio_device);
  devices.forEach(d => { const o = el('option', null, d); o.value = d; audio.append(o); });
  audio.value = s.audio_device || '';

  const volume = el('input');
  volume.type = 'number'; volume.min = 0; volume.max = 100; volume.value = s.initial_volume;

  const sleep = el('input');
  sleep.value = s.sleep_timer.join(', ');
  sleep.placeholder = '30, 60, 90 (blank for no sleep timer)';

  const autoChannels = el('select');
  [['false','Off - the lineup is exactly what you set'],
   ['true','On - new folders become channels at start-up']].forEach(([v, label]) => {
    const o = el('option', null, label); o.value = v; autoChannels.append(o);
  });
  autoChannels.value = String(s.auto_channels);

  const fields = [
    ['Audio output', audio],
    ['Volume at power-on', volume],
    ['Sleep timer, in minutes', sleep],
    ['New folders', autoChannels],
  ];
  fields.forEach(([label, input]) => {
    const field = el('div', 'field');
    field.append(el('label', null, label), input);
    host.append(field);
  });

  const save = el('button', null, 'SAVE SETTINGS');
  save.onclick = async () => {
    const minutes = sleep.value.split(',').map(x => x.trim()).filter(Boolean);
    if (minutes.some(x => !/^\\d+$/.test(x))) {
      toast('Sleep timer takes whole minutes, separated by commas', true);
      return;
    }
    try {
      const data = await json('/api/settings', 'POST', {
        audio_device: audio.value || null,
        initial_volume: Number(volume.value),
        sleep_timer: minutes.map(Number),
        auto_channels: autoChannels.value === 'true',
      });
      toast('Settings saved');
      if (data.restart_required.length) {
        note.textContent = data.restart_required.join(', ') +
          ' takes effect the next time the box starts up.';
        note.className = 'note warn';
      } else {
        note.textContent = '';
      }
      refresh();
    } catch (e) { toast(e.message, true); }
  };
  const bar = el('div', 'bar'); bar.style.marginTop = '.9rem';
  bar.append(save);
  host.append(bar);

  const note = el('p', 'note', '');
  host.append(note);

  const where = el('p', 'note',
    'Library: ' + (s.media_root || 'not set - add media_root to config.yaml') +
    '  \\u00b7  uploads up to ' + s.max_upload_mb + ' MB, keeping ' + s.min_free_mb +
    ' MB of the disk free.');
  host.append(where);
}

/* ========================================================================
   Adding shows: drop a folder, get a channel; drop files, top one up.
   ======================================================================== */
const CAN_PICK_FOLDERS = 'webkitdirectory' in document.createElement('input');
const Up = {
  extensions: ['.mp4', '.mkv', '.avi', '.m4v', '.mov', '.webm', '.mpg', '.mpeg', '.ts'],
  chunkBytes: 8 * 1024 * 1024,
  maxFiles: 500,
  picked: [],          // {file, path}
  session: null,       // {id, chunkBytes, files:[...]}
  jobs: [],            // one per file, mirrors the session
  paused: false,
  running: false,
  abandoned: false,
  startedAt: 0,
  baseline: 0,         // bytes already on the box when this run started
};

function ext(name) {
  const dot = name.lastIndexOf('.');
  return dot < 0 ? '' : name.slice(dot).toLowerCase();
}

/* -- picking files ------------------------------------------------------- */
function fromInput(input) {
  return [...input.files].map(f => ({file: f, path: f.webkitRelativePath || f.name}));
}

/* A dropped folder only reveals its contents through the entries API. It is
   a desktop browser feature; on a phone there is no drag and drop at all, so
   the file picker is the whole story there. */
async function fromDrop(dataTransfer) {
  const roots = [...dataTransfer.items]
    .map(i => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null))
    .filter(Boolean);
  if (!roots.length) {
    return [...dataTransfer.files].map(f => ({file: f, path: f.name}));
  }
  const out = [];
  const walk = async (entry, prefix) => {
    if (entry.isFile) {
      const file = await new Promise((ok, no) => entry.file(ok, no));
      out.push({file, path: prefix + entry.name});
      return;
    }
    const reader = entry.createReader();
    for (;;) {
      const batch = await new Promise((ok, no) => reader.readEntries(ok, no));
      if (!batch.length) break;
      for (const child of batch) await walk(child, prefix + entry.name + '/');
    }
  };
  for (const root of roots) await walk(root, '');
  return out;
}

/* -- the plan, shown before a single byte moves --------------------------- */
function showPlan(items) {
  const host = $('#plan');
  host.textContent = '';
  const good = [], skipped = [];
  for (const item of items) {
    if (!Up.extensions.includes(ext(item.file.name))) {
      skipped.push([item.path, 'not a video file this box plays']);
    } else if (item.file.size === 0) {
      skipped.push([item.path, 'the file is empty']);
    } else {
      good.push(item);
    }
  }
  if (good.length > Up.maxFiles) {
    for (const item of good.slice(Up.maxFiles)) {
      skipped.push([item.path, `over the ${Up.maxFiles}-file limit for one go`]);
    }
    good.length = Up.maxFiles;
  }
  Up.picked = good;

  const total = good.reduce((n, i) => n + i.file.size, 0);
  const summary = el('div', 'edit');
  summary.append(el('p', 'now', `${good.length} file${good.length === 1 ? '' : 's'}`));
  summary.append(el('p', 'meta', mb(total) + ' in total'));

  if (skipped.length) {
    const list = el('div');
    list.append(el('p', 'note warn', `${skipped.length} will be skipped:`));
    for (const [name, why] of skipped.slice(0, 8)) {
      const row = el('div', 'job');
      row.append(el('span', 'grow', name), el('span', 'state warn', why));
      list.append(row);
    }
    if (skipped.length > 8) list.append(el('p', 'note', `...and ${skipped.length - 8} more`));
    summary.append(list);
  }
  host.append(summary);
  if (!good.length) return;

  // Where is it going? A folder drop defaults to making a channel of its own.
  const folder = guessFolder(good);
  const choose = el('div', 'edit');
  const where = el('select');
  const asNew = el('option', null,
    folder ? `New channel from "${folder}"` : 'New channel');
  asNew.value = 'new';
  where.append(asNew);

  const field = el('div', 'field');
  field.append(el('label', null, 'Add to'), where);

  const nameBox = el('input');
  nameBox.value = folder || '';
  nameBox.maxLength = 48;
  nameBox.placeholder = 'Channel name';
  const nameField = el('div', 'field');
  nameField.append(el('label', null, 'Channel name'), nameBox);

  const numberBox = el('input');
  numberBox.type = 'number'; numberBox.min = 0; numberBox.max = 999;
  numberBox.placeholder = 'next free';
  const numberField = el('div', 'field narrow');
  numberField.append(el('label', null, 'Channel'), numberBox);

  const newBits = el('div', 'split');
  newBits.append(numberField, nameField);

  api('/api/channels').then(({channels}) => {
    for (const c of channels) {
      const option = el('option', null, `CH ${pad(c.number)}  ${c.name}`);
      option.value = String(c.number);
      where.append(option);
    }
  }).catch(() => {});

  const toggle = () => { newBits.hidden = where.value !== 'new'; };
  where.onchange = toggle;
  toggle();

  const go = el('button', null, 'START UPLOAD');
  go.onclick = () => beginUpload({
    mode: where.value,
    name: nameBox.value.trim() || folder,
    folder: folder,
    number: numberBox.value.trim(),
  });
  const clear = el('button', 'ghost', 'CLEAR');
  clear.onclick = () => { Up.picked = []; $('#plan').textContent = ''; };

  const bar = el('div', 'bar'); bar.style.marginTop = '.8rem';
  bar.append(go, clear);
  choose.append(field, newBits, bar);
  host.append(choose);
}

function guessFolder(items) {
  const tops = new Set(items.map(i => i.path.includes('/') ? i.path.split('/')[0] : ''));
  return tops.size === 1 ? [...tops][0] : '';
}

/* -- running the upload --------------------------------------------------- */
async function beginUpload(choice) {
  const files = Up.picked.map(i => ({path: i.path, size: i.file.size}));
  let actions = {};

  if (choice.mode !== 'new') {
    // Ask about clashes before creating anything, so the question is asked
    // once and nobody's episode is quietly replaced.
    try {
      const existing = new Set(
        (await api('/api/media/' + choice.mode)).files.map(f => f.name)
      );
      const clashes = Up.picked
        .map((item, index) => ({index, name: item.file.name}))
        .filter(x => existing.has(x.name));
      if (clashes.length) {
        const names = clashes.slice(0, 5).map(c => c.name).join(', ');
        const replace = confirmReplace(clashes.length, names);
        for (const c of clashes) actions[c.index] = replace ? 'replace' : 'skip';
      }
    } catch (e) { /* if we cannot check, the box refuses to overwrite anyway */ }
  }

  files.forEach((f, i) => { if (actions[i]) f.action = actions[i]; });

  const body = choice.mode === 'new'
    ? {new_channel: {
        name: choice.name || choice.folder,
        folder: choice.folder || choice.name,
        ...(choice.number ? {number: Number(choice.number)} : {}),
      }, files}
    : {channel: Number(choice.mode), files};

  let started;
  try {
    started = await json('/api/uploads', 'POST', body);
  } catch (e) { toast(e.message, true); return; }

  Up.session = started;
  Up.chunkBytes = started.chunk_bytes;
  Up.jobs = started.files.map(f => ({
    ...f,
    file: Up.picked[f.index].file,
    sent: 0,
    state: f.action === 'skip' ? 'skipped' : 'queued',
    detail: '',
  }));
  Up.abandoned = false;
  Up.paused = false;
  Up.startedAt = Date.now();
  Up.baseline = 0;
  $('#plan').textContent = '';
  $('#queue-panel').hidden = false;
  drawQueue();
  pump();
}

function confirmReplace(count, names) {
  // One question, not one per file. Cancel means keep what is on the box.
  return window.confirm(
    count + ' file' + (count === 1 ? ' is' : 's are') + ' already on this channel (' +
    names + ').\\n\\nOK = replace them.\\nCancel = keep what is on the box and skip.'
  );
}

async function pump() {
  if (Up.running) return;
  Up.running = true;
  try {
    for (const job of Up.jobs) {
      if (Up.abandoned) return;
      if (job.state === 'skipped' || job.state === 'done') continue;
      job.state = 'uploading';
      const missing = (Up.session.missing[String(job.index)] || []).slice();
      // Anything already on the box is not sent again - that is the whole
      // point of chunking, and it is what makes a resume worth having.
      Up.baseline += (job.chunks - missing.length) * Up.chunkBytes;
      job.sent = (job.chunks - missing.length) * Up.chunkBytes;

      for (const chunk of missing) {
        while (Up.paused && !Up.abandoned) await sleep(200);
        if (Up.abandoned) return;
        const from = chunk * Up.chunkBytes;
        const blob = job.file.slice(from, from + Up.chunkBytes);
        try {
          await putChunk(job.index, chunk, blob);
        } catch (e) {
          job.state = 'failed';
          job.detail = e.message;
          drawQueue();
          toast(`${job.name}: ${e.message}`, true);
          return;
        }
        job.sent = Math.min(job.size, job.sent + blob.size);
        drawQueue();
      }
      job.state = 'assembling';
      drawQueue();
    }
    if (Up.abandoned) return;

    for (const job of Up.jobs) {
      if (job.state === 'assembling') { job.state = 'checking'; }
    }
    drawQueue();

    let done;
    try {
      done = await json(`/api/uploads/${Up.session.session}/commit`, 'POST', {});
    } catch (e) {
      toast(e.message, true);
      for (const job of Up.jobs) if (job.state === 'checking') job.state = 'failed';
      drawQueue();
      return;
    }
    for (const result of done.results) {
      const job = Up.jobs.find(j => j.index === result.index);
      if (!job) continue;
      job.state = result.state === 'no video' ? 'no picture' : result.state;
      job.detail = result.detail || '';
    }
    Up.session = null;
    drawQueue();
    toast(done.channel
      ? `Channel ${pad(done.channel.number)} "${done.channel.name}" is ready`
      : 'Upload finished');
    refresh();
    loadUnfinished();
  } finally {
    Up.running = false;
  }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

function putChunk(index, chunk, blob) {
  return new Promise((ok, no) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/api/uploads/${Up.session.session}/${index}/${chunk}`);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.onload = () => {
      if (xhr.status === 200) return ok();
      let error = 'upload failed (' + xhr.status + ')';
      try { error = JSON.parse(xhr.responseText).error || error; } catch (e) {}
      no(new Error(error));
    };
    xhr.onerror = () => no(new Error('the box could not be reached'));
    xhr.send(blob);
  });
}

function drawQueue() {
  const host = $('#queue');
  host.textContent = '';
  let sent = 0, total = 0;
  for (const job of Up.jobs) {
    total += job.size;
    sent += job.state === 'skipped' ? job.size : job.sent;

    const row = el('div', 'job');
    const bar = el('div', 'meter');
    const fill = el('i');
    fill.style.width = (job.size ? Math.round(job.sent / job.size * 100) : 100) + '%';
    bar.append(fill);
    const label = el('span', 'grow', job.name);
    const state = el('span', 'state ' + stateClass(job.state), job.state);
    if (job.detail) state.title = job.detail;
    row.append(label, bar, state);
    host.append(row);
    if (job.detail) host.append(el('p', 'note warn', `${job.name}: ${job.detail}`));
  }

  const overall = $('#overall');
  overall.textContent = '';
  const bar = el('div', 'meter');
  const fill = el('i');
  fill.style.width = (total ? Math.round(sent / total * 100) : 0) + '%';
  bar.append(fill);
  const wrap = el('div', 'progress');
  wrap.append(bar, el('span', null, (total ? Math.round(sent / total * 100) : 0) + '%'));
  overall.append(wrap);

  const totals = el('div', 'totals');
  totals.append(el('span', null, `${mb(sent)} of ${mb(total)}`), el('span', null, eta(sent, total)));
  overall.append(totals);
}

function stateClass(state) {
  if (state === 'done') return 'done';
  if (state === 'failed') return 'failed';
  if (state === 'no picture' || state === 'skipped') return 'warn';
  return '';
}

function eta(sent, total) {
  const moved = sent - Up.baseline;
  const seconds = (Date.now() - Up.startedAt) / 1000;
  if (Up.paused) return 'paused';
  if (moved <= 0 || seconds < 2) return 'estimating\\u2026';
  const rate = moved / seconds;
  const left = Math.max(0, total - sent) / rate;
  if (!isFinite(left)) return '';
  const m = Math.floor(left / 60), s = Math.round(left % 60);
  return (m ? m + 'm ' : '') + s + 's left  \\u00b7  ' + mb(rate) + '/s';
}

$('#pause').onclick = () => {
  Up.paused = !Up.paused;
  $('#pause').textContent = Up.paused ? 'RESUME' : 'PAUSE';
  drawQueue();
};

arm($('#abandon'), 'TAP AGAIN TO CANCEL', async () => {
  Up.abandoned = true;
  Up.paused = false;
  const session = Up.session && Up.session.session;
  Up.session = null;
  $('#queue-panel').hidden = true;
  $('#pause').textContent = 'PAUSE';
  if (session) {
    try { await api('/api/uploads/' + session, {method: 'DELETE'}); } catch (e) {}
  }
  toast('Upload cancelled');
  loadUnfinished();
});

/* -- unfinished uploads, after a reload ----------------------------------- */
async function loadUnfinished() {
  let data;
  try { data = await api('/api/uploads'); } catch (e) { return; }
  Up.chunkBytes = data.chunk_bytes;
  Up.maxFiles = data.max_files;

  const host = $('#resume');
  host.textContent = '';
  const live = Up.session ? Up.session.session : null;
  const stale = data.sessions.filter(s => s.id !== live);
  $('#resume-panel').hidden = stale.length === 0;
  if (!stale.length) return;

  host.append(el('p', 'note',
    'These stopped part way. The parts already on the box are kept - choose ' +
    'the same files again and only what is missing gets sent. Anything left ' +
    'untouched is cleared off the disk after ' + (data.expiry_hours || 24) +
    ' hours.'));

  for (const session of stale) {
    const box = el('div', 'edit');
    const where = session.target.kind === 'new'
      ? `new channel "${session.target.channel_name}"`
      : `channel ${pad(session.target.channel_number)}`;
    const have = session.total_bytes
      ? Math.round(session.received_bytes / session.total_bytes * 100) : 0;
    box.append(el('p', 'grow', `${session.files.length} file(s) for ${where}`));
    box.append(el('p', 'meta', `${have}% already on the box - ${mb(session.total_bytes)} in total`));

    const picker = el('input');
    picker.type = 'file';
    picker.multiple = true;
    if (CAN_PICK_FOLDERS) picker.webkitdirectory = true;
    picker.style.display = 'none';
    picker.onchange = () => resumeSession(session, fromInput(picker));

    const carry = el('button', null, 'CHOOSE THE FILES AGAIN');
    carry.onclick = () => picker.click();
    const drop = arm(el('button', 'danger ghost', 'DISCARD'), 'TAP AGAIN', async () => {
      try {
        await api('/api/uploads/' + session.id, {method: 'DELETE'});
        toast('Discarded, and the space is back');
        loadUnfinished();
        loadSettings();
      } catch (e) { toast(e.message, true); }
    });
    const bar = el('div', 'bar'); bar.style.marginTop = '.6rem';
    bar.append(carry, drop);
    box.append(bar, picker);
    host.append(box);
  }
}

function resumeSession(session, items) {
  // Match by name and size: the browser will not hand back a File it gave us
  // before a reload, so the person has to point at the same files again.
  const byKey = new Map(items.map(i => [i.file.name + ':' + i.file.size, i.file]));
  const jobs = [];
  const missingFiles = [];
  for (const item of session.files) {
    const file = byKey.get(item.name + ':' + item.size);
    if (!file) { missingFiles.push(item.name); continue; }
    jobs.push({...item, file, sent: 0, state: 'queued', detail: ''});
  }
  if (missingFiles.length) {
    toast(`Could not match ${missingFiles.length} file(s) - pick the same ones`, true);
    if (!jobs.length) return;
  }
  Up.session = {session: session.id, missing: session.missing, chunk_bytes: session.chunk_bytes};
  Up.chunkBytes = session.chunk_bytes;
  Up.jobs = jobs;
  Up.abandoned = false;
  Up.paused = false;
  Up.startedAt = Date.now();
  Up.baseline = 0;
  $('#resume-panel').hidden = true;
  $('#queue-panel').hidden = false;
  drawQueue();
  pump();
}

/* -- wiring the drop zone ------------------------------------------------- */
(function wireDropZone() {
  const zone = $('#drop');
  const note = $('#dropnote');
  const filePicker = el('input');
  filePicker.type = 'file';
  filePicker.multiple = true;
  filePicker.accept = 'video/*';
  filePicker.style.display = 'none';
  filePicker.onchange = () => showPlan(fromInput(filePicker));

  const folderPicker = el('input');
  folderPicker.type = 'file';
  folderPicker.multiple = true;
  folderPicker.style.display = 'none';
  folderPicker.onchange = () => showPlan(fromInput(folderPicker));

  $('#pick-files').onclick = () => filePicker.click();

  if (CAN_PICK_FOLDERS) {
    folderPicker.webkitdirectory = true;
    $('#pick-folder').onclick = () => folderPicker.click();
    note.textContent = 'A whole folder becomes a channel of its own.';
  } else {
    // iOS Safari has no directory picker. Say so, rather than leaving a
    // button that looks like it works and does nothing.
    $('#pick-folder').remove();
    note.className = 'note warn';
    note.textContent =
      'This browser cannot pick a whole folder - that is a phone limitation, ' +
      'not the box. Choose the files instead, or use a computer for a big lot.';
  }
  document.body.append(filePicker, folderPicker);

  for (const name of ['dragenter', 'dragover']) {
    zone.addEventListener(name, e => {
      e.preventDefault();
      zone.classList.add('over');
    });
  }
  for (const name of ['dragleave', 'drop']) {
    zone.addEventListener(name, e => {
      e.preventDefault();
      zone.classList.remove('over');
    });
  }
  zone.addEventListener('drop', async e => {
    try { showPlan(await fromDrop(e.dataTransfer)); }
    catch (err) { toast('Could not read what was dropped', true); }
  });
  // Dropping anywhere else on the page must not make the browser navigate to
  // the file, which is the default and looks exactly like a crash.
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => e.preventDefault());
})();

/* ========================================================================
   Files: browse the disk, rename a folder, delete things, change your mind.

   Two rules shape all of it.

   1. Nothing is deleted. Everything selected is moved to a trash folder on
      the same disk, which frees not one byte - so every screen that mentions
      deleting says so, and the button that does free space is offered right
      beside the one that does not.
   2. The confirmation is the feature. "Are you sure?" is not a question
      anybody can answer; "142 episodes, 38 GB, and channel 4 goes to NO
      SIGNAL" is. Those sentences are written by the route, not here, because
      they are the part that must not quietly stop being true.
   ======================================================================== */
const Lib = {
  path: '', page: 1, pages: 1, sort: 'name', order: 'asc',
  // path -> the row it came from, so the count, the rename and the delete all
  // read from one place and a selection survives a redraw of the list.
  picked: new Map(),
  space: null, trashItems: 0, busy: false,
};

/* Deletes go up in batches so a select-all over six hundred episodes shows
   real movement rather than a frozen page and a spinning phone. */
const DELETE_BATCH = 25;

function libBusy(on) {
  Lib.busy = on;
  ['#lib-all', '#lib-rename', '#lib-delete', '#lib-empty'].forEach(id => {
    const button = $(id);
    if (button) button.disabled = on;
  });
  if (!on) libCount();
}

function libCount() {
  const n = Lib.picked.size;
  $('#lib-count').textContent = n
    ? (n + (n === 1 ? ' item selected' : ' items selected'))
    : 'nothing selected';
  if (Lib.busy) return;
  $('#lib-delete').disabled = !n;
  const only = n === 1 ? Lib.picked.values().next().value : null;
  $('#lib-rename').disabled = !(only && only.kind === 'folder');
  $('#lib-empty').disabled = !Lib.trashItems;
}

function libWhen(stamp) {
  if (!stamp) return '';
  const d = new Date(stamp * 1000);
  return d.toLocaleDateString() + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function libRow(entry) {
  const row = el('div', 'lib' + (entry.kind === 'system' ? ' system' : ''));

  const tick = el('input');
  tick.type = 'checkbox';
  tick.checked = Lib.picked.has(entry.path);
  /* The box's own folders are listed and cannot be picked. Seeing them is how
     somebody finds where their disk went; picking them is how they break the
     television. */
  tick.disabled = !entry.selectable;
  tick.setAttribute('aria-label', 'select ' + entry.name);
  if (tick.checked) row.classList.add('on');
  tick.onchange = () => {
    if (tick.checked) Lib.picked.set(entry.path, entry);
    else Lib.picked.delete(entry.path);
    row.classList.toggle('on', tick.checked);
    libCount();
  };
  row.append(tick);

  if (entry.kind === 'folder') {
    const open = el('button', 'name', entry.name);
    open.type = 'button';
    open.onclick = () => libGo(entry.path);
    row.append(open);
  } else {
    const name = el('span', 'name', entry.name);
    if (entry.note) name.title = entry.note;
    row.append(name);
  }

  row.append(el('span', 'size', entry.kind === 'folder' ? '' : mb(entry.bytes || 0)));
  row.append(el('span', 'kind',
    entry.kind === 'system' ? 'THE BOX'
    : entry.kind === 'folder' ? 'FOLDER'
    : entry.kind === 'video' ? (entry.duration ? duration(entry.duration) : 'VIDEO')
    : 'FILE'));
  return row;
}

function libGo(path) {
  Lib.path = path;
  Lib.page = 1;
  // A selection you can no longer see is a trap, so walking into a folder
  // drops it rather than carrying it along invisibly.
  Lib.picked.clear();
  $('#lib-confirm').textContent = '';
  loadLibrary();
}

async function loadLibrary() {
  const host = $('#lib-list');
  let data;
  try {
    data = await api('/api/library?path=' + encodeURIComponent(Lib.path) +
      '&page=' + Lib.page + '&sort=' + Lib.sort + '&order=' + Lib.order);
  } catch (e) {
    host.textContent = '';
    host.append(el('p', 'empty', e.message));
    return;
  }

  Lib.path = data.path;
  Lib.page = data.page;
  Lib.pages = data.pages;

  const trail = $('#lib-crumbs');
  trail.textContent = '';
  data.crumbs.forEach((crumb, i) => {
    if (i) trail.append(el('span', 'sep', '/'));
    const step = el('button', null, crumb.name);
    step.type = 'button';
    step.onclick = () => libGo(crumb.path);
    trail.append(step);
  });

  host.textContent = '';
  if (!data.entries.length) host.append(el('p', 'empty', 'There is nothing in here.'));
  data.entries.forEach(entry => host.append(libRow(entry)));

  $('#lib-pager').hidden = data.pages < 2;
  $('#lib-of').textContent = 'page ' + data.page + ' of ' + data.pages +
    '  \\u00b7  ' + data.total + ' items';
  $('#lib-prev').disabled = data.page <= 1;
  $('#lib-next').disabled = data.page >= data.pages;
  $('#lib-all').textContent = 'SELECT ALL';

  libCount();
  loadTrash();
  loadSpace();
}

async function loadSpace() {
  let s;
  try { s = await api('/api/library/space'); } catch (e) { return; }
  Lib.space = s;
  const line = $('#lib-space');
  const tight = s.total_bytes && s.free_bytes !== null &&
                s.free_bytes < s.total_bytes * 0.1;
  line.textContent = mb(s.free_bytes || 0) + ' free of ' + mb(s.total_bytes || 0) +
    (s.trash_bytes
      ? ('  \\u00b7  ' + mb(s.trash_bytes) + ' of that is sitting in the trash ' +
         'and comes back the moment you empty it')
      : '  \\u00b7  the trash is empty');
  line.className = 'note' + (tight ? ' warn' : '');
}

async function loadTrash() {
  const host = $('#lib-trash');
  let data;
  try { data = await api('/api/library/trash'); }
  catch (e) { host.textContent = ''; host.append(el('p', 'empty', e.message)); return; }

  Lib.trashItems = data.usage.items;
  if (!Lib.busy) $('#lib-empty').disabled = !Lib.trashItems;

  host.textContent = '';
  if (!data.items.length) {
    host.append(el('p', 'empty', 'The trash is empty.'));
    return;
  }
  data.items.forEach(item => {
    const row = el('div', 'lib');
    row.append(el('span', 'name',
      item.name + (item.from ? ('   from ' + item.from) : '')));
    row.append(el('span', 'size', mb(item.bytes || 0)));
    row.append(el('span', 'when', libWhen(item.deleted)));
    const back = el('button', 'ghost', 'RESTORE');
    back.type = 'button';
    back.onclick = () => restoreFromTrash(item, back);
    row.append(back);
    host.append(row);
  });
  host.append(el('p', 'note', data.usage.items + ' item(s), ' +
    mb(data.usage.bytes) + ', still using the disk. The box clears anything ' +
    'older than ' + data.keep_days + ' days by itself.'));
}

async function restoreFromTrash(item, button) {
  button.disabled = true;
  button.textContent = 'PUTTING IT BACK\\u2026';
  try {
    await json('/api/library/trash/restore', 'POST', {token: item.token});
    toast(item.name + ' is back in ' + (item.from || 'the library') + '.');
  } catch (e) {
    // 409 is the one refusal with a second answer worth offering.
    if (e.status === 409) askToReplace(item);
    else toast(e.message, true);
  }
  button.disabled = false;
  button.textContent = 'RESTORE';
  loadLibrary();
}

function askToReplace(item) {
  const host = $('#lib-trash-confirm');
  host.textContent = '';
  const box = el('div', 'peril');
  box.append(el('h3', null, 'SOMETHING IS ALREADY CALLED THAT'));
  box.append(el('p', 'cost',
    'There is already something called "' + item.name + '" in ' +
    (item.from || 'the library') + '. Putting this one back would move that ' +
    'one into the trash - it would not be destroyed, and you could put it ' +
    'back the same way.'));
  const bar = el('div', 'bar');
  const no = el('button', 'ghost', 'LEAVE IT');
  no.type = 'button';
  no.onclick = () => { host.textContent = ''; };
  const yes = el('button', 'danger', 'REPLACE IT');
  yes.type = 'button';
  yes.onclick = async () => {
    yes.disabled = true;
    yes.textContent = 'REPLACING\\u2026';
    try {
      await json('/api/library/trash/restore?confirm=yes', 'POST',
                 {token: item.token, replace: true});
      toast(item.name + ' is back. The one that was there went to the trash.');
    } catch (e) { toast(e.message, true); }
    host.textContent = '';
    loadLibrary();
  };
  bar.append(no, yes);
  box.append(bar);
  host.append(box);
}

/* -- the confirmation, which is the whole point --------------------------- */
$('#lib-delete').onclick = async () => {
  if (!Lib.picked.size) return;
  const host = $('#lib-confirm');
  host.textContent = '';
  host.append(el('p', 'note', 'Working out what that would cost\\u2026'));
  let plan;
  try {
    plan = await json('/api/library/plan', 'POST',
                      {paths: Array.from(Lib.picked.keys())});
  } catch (e) {
    host.textContent = '';
    toast(e.message, true);
    return;
  }
  drawDeleteConfirmation(plan);
};

function drawDeleteConfirmation(plan) {
  const host = $('#lib-confirm');
  host.textContent = '';
  const box = el('div', 'peril');
  const n = plan.items.length;
  box.append(el('h3', null, 'DELETE ' + n + (n === 1 ? ' ITEM?' : ' ITEMS?')));

  box.append(el('p', 'cost',
    plan.totals.files + (plan.totals.files === 1 ? ' file' : ' files') +
    (plan.totals.folders ? (' across ' + plan.totals.folders + ' folders') : '') +
    ', ' + mb(plan.totals.bytes) + '.'));
  // The sentence people go looking for on the storage gauge and do not find.
  box.append(el('p', 'cost', plan.note));

  if (plan.warnings.length) {
    const list = el('ul');
    plan.warnings.forEach(line => list.append(el('li', null, line)));
    box.append(list);
  }

  const bar = el('div', 'bar');
  const no = el('button', 'ghost', 'KEEP THEM');
  no.type = 'button';
  no.onclick = () => { host.textContent = ''; };
  const yes = el('button', 'danger', plan.uploads
    ? 'CANCEL THOSE UPLOADS AND DELETE'
    : 'MOVE TO THE TRASH');
  yes.type = 'button';
  yes.onclick = () => runDelete(plan, yes);
  bar.append(no, yes);

  /* Offered here, next to the bad news. "This frees nothing" with no way to
     free anything is how somebody ends up binning forty gigabytes twice. */
  if (plan.space.reclaimable_bytes) {
    const empty = el('button', 'danger ghost',
      'EMPTY THE TRASH (' + mb(plan.space.reclaimable_bytes) + ')');
    empty.type = 'button';
    empty.onclick = () => askToEmpty();
    bar.append(empty);
  }
  box.append(bar);
  host.append(box);
  box.scrollIntoView({block: 'nearest'});
}

async function runDelete(plan, button) {
  const host = $('#lib-confirm');
  // The route's own idea of each path, not the browser's: it has already
  // resolved and checked them, and sending its answers back keeps the two
  // halves of this from disagreeing about what was named.
  const paths = plan.items.map(item => item.relative);

  const meter = el('div', 'meter');
  const fill = el('i');
  fill.style.width = '0%';
  meter.append(fill);
  const pct = el('span', null, '0%');
  const progress = el('div', 'progress');
  progress.append(meter, pct);
  host.append(progress);

  libBusy(true);
  button.disabled = true;
  button.textContent = 'MOVING TO THE TRASH\\u2026';

  const failed = [];
  let moved = 0, done = 0;
  for (let i = 0; i < paths.length; i += DELETE_BATCH) {
    const batch = paths.slice(i, i + DELETE_BATCH);
    try {
      const out = await json('/api/library/delete?confirm=yes', 'POST',
        {paths: batch, cancel_uploads: plan.uploads > 0});
      moved += (out.deleted || []).length;
      (out.failed || []).forEach(bad => failed.push(bad));
    } catch (e) {
      batch.forEach(path => failed.push({path: path, error: e.message}));
    }
    done += batch.length;
    const share = Math.round(done * 100 / paths.length);
    fill.style.width = share + '%';
    pct.textContent = share + '%';
  }

  libBusy(false);
  host.textContent = '';
  Lib.picked.clear();
  if (failed.length) {
    toast(failed.length + ' could not be deleted: ' + failed[0].error, true);
  } else {
    toast(moved + ' moved to the trash. This freed no space - empty the ' +
          'trash for that.');
  }
  loadLibrary();
}

function askToEmpty() {
  const host = $('#lib-trash-confirm');
  host.textContent = '';
  const held = Lib.space || {};
  const box = el('div', 'peril');
  box.append(el('h3', null, 'EMPTY THE TRASH?'));
  box.append(el('p', 'cost',
    'This is the one thing on this box that destroys a video for good - ' +
    (held.trash_items || 0) + ' item(s), ' + mb(held.trash_bytes || 0) +
    '. It is also the step that actually gives the space back.'));
  const bar = el('div', 'bar');
  const no = el('button', 'ghost', 'NOT YET');
  no.type = 'button';
  no.onclick = () => { host.textContent = ''; };
  const yes = el('button', 'danger', 'EMPTY IT FOR GOOD');
  yes.type = 'button';
  yes.onclick = async () => {
    yes.disabled = true;
    yes.textContent = 'EMPTYING\\u2026';
    try {
      const out = await api('/api/library/trash?confirm=yes', {method: 'DELETE'});
      toast('Emptied ' + out.items + ' item(s) and got back ' + mb(out.bytes) + '.');
    } catch (e) { toast(e.message, true); }
    host.textContent = '';
    loadLibrary();
  };
  bar.append(no, yes);
  box.append(bar);
  host.append(box);
  box.scrollIntoView({block: 'nearest'});
}

/* -- renaming a folder, which repoints whatever plays from it ------------- */
$('#lib-rename').onclick = () => {
  if (Lib.picked.size !== 1) return;
  const entry = Lib.picked.values().next().value;
  const host = $('#lib-confirm');
  host.textContent = '';

  const box = el('div', 'edit');
  const field = el('div', 'field');
  field.append(el('label', null, 'A new name for ' + entry.name));
  const input = el('input');
  input.value = entry.name;
  input.maxLength = 200;
  field.append(input);
  box.append(field);
  box.append(el('p', 'note', 'Any channel or scheduled block playing from ' +
    'this folder is repointed at the new name in the same step.'));

  const bar = el('div', 'bar');
  const no = el('button', 'ghost', 'CANCEL');
  no.type = 'button';
  no.onclick = () => { host.textContent = ''; };
  const yes = el('button', null, 'RENAME');
  yes.type = 'button';
  yes.onclick = async () => {
    yes.disabled = true;
    yes.textContent = 'RENAMING\\u2026';
    try {
      const out = await json('/api/library/rename', 'POST',
                             {path: entry.path, name: input.value});
      toast(out.unchanged
        ? 'That is already what it is called.'
        : ('Renamed to ' + out.to + (out.channels.length
            ? ('. Channel ' + out.channels.join(', ') + ' now plays from it.')
            : '.')));
      host.textContent = '';
      Lib.picked.clear();
      loadLibrary();
    } catch (e) {
      toast(e.message, true);
      yes.disabled = false;
      yes.textContent = 'RENAME';
    }
  };
  bar.append(no, yes);
  box.append(bar);
  host.append(box);
  input.focus();
};

$('#lib-all').onclick = () => {
  const boxes = Array.from(
    document.querySelectorAll('#lib-list .lib input[type=checkbox]')
  ).filter(tick => !tick.disabled);
  const turningOn = boxes.some(tick => !tick.checked);
  boxes.forEach(tick => {
    if (tick.checked !== turningOn) { tick.checked = turningOn; tick.onchange(); }
  });
  $('#lib-all').textContent = turningOn ? 'CLEAR THE SELECTION' : 'SELECT ALL';
};

$('#lib-prev').onclick = () => { Lib.page = Math.max(1, Lib.page - 1); loadLibrary(); };
$('#lib-next').onclick = () => { Lib.page = Math.min(Lib.pages, Lib.page + 1); loadLibrary(); };
$('#lib-empty').onclick = () => askToEmpty();

/* ========================================================================
   System: is my box alright, what is it saying, and the buttons that
   only belong here.
   ======================================================================== */
const Sys = {report: null, follow: null, lastPress: 0};

const fact = (key, value, tone) => {
  const row = el('div', 'fact');
  row.append(el('span', 'key', key), el('span', 'val' + (tone ? ' ' + tone : ''), value));
  return row;
};

/* The two buttons that turn a diagnosis into something a customer can act
   on. REPAIR re-runs detection, unmutes the outputs and asks the television
   to look for its HDMI socket again; TEST SOUND puts a two second tone
   through whatever it chose, which is the only way to answer "is it the box
   or is it the telly" from a sofa. Both are safe to press twice. */
function repairRow() {
  const row = el('div', 'fact');
  row.append(el('span', 'key', ''));
  const holder = el('span', 'val');
  const repair = el('button', 'small', 'REPAIR PICTURE AND SOUND');
  const tone = el('button', 'small', 'TEST SOUND');
  const said = el('span', 'note', '');
  const busy = (on) => { repair.disabled = on; tone.disabled = on; };

  repair.onclick = async () => {
    busy(true);
    said.textContent = ' checking the hardware...';
    try {
      const done = await api('/api/system/hardware/repair', {method: 'POST'});
      if (!done.ok) { said.textContent = ' ' + (done.error || 'could not repair'); }
      else {
        const bits = (done.changed || []);
        if (done.advice) bits.push(done.advice);
        said.textContent = ' ' + (bits.join('; ') || 'nothing needed changing');
        loadSystem();
      }
    } catch (e) { said.textContent = ' ' + e.message; }
    busy(false);
  };

  tone.onclick = async () => {
    busy(true);
    said.textContent = ' playing a tone...';
    try {
      const done = await api('/api/system/sound/test', {method: 'POST'});
      said.textContent = ' ' + (done.ok ? done.note : (done.error || 'no tone'));
    } catch (e) { said.textContent = ' ' + e.message; }
    busy(false);
  };

  holder.append(repair, tone, said);
  row.append(holder);
  return row;
}

function duration(seconds) {
  if (!seconds) return 'unknown';
  seconds = Math.floor(seconds);
  const d = Math.floor(seconds / 86400);
  const h = Math.floor(seconds % 86400 / 3600);
  const m = Math.floor(seconds % 3600 / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
}

function volumeLine(host, label, v) {
  if (!v) { host.append(fact(label, 'could not be read')); return; }
  const tone = v.state === 'critical' ? 'bad' : (v.state === 'low' ? 'warn' : '');
  const note = v.state === 'ok' ? '' : '   \\u2190 ' + v.state.toUpperCase();
  host.append(fact(label, `${mb(v.free_bytes)} free of ${mb(v.total_bytes)}` +
    `  (${v.percent_used}% used)${note}`, tone));
}

async function loadSystem() {
  loadPrivileges();          // first, and not awaited: it is the top of the page
  let s;
  try { s = await api('/api/system'); }
  catch (e) { $('#health').textContent = ''; $('#health').append(el('p', 'empty', e.message)); return; }
  Sys.report = s;

  const host = $('#health');
  host.textContent = '';
  host.append(fact('Version', s.version || 'unknown'));
  host.append(fact('Television', s.tv_running ? 'playing, up ' + duration(s.tv_uptime_seconds)
                                              : 'not running', s.tv_running ? '' : 'bad'));
  host.append(fact('Box uptime', duration(s.uptime_seconds)));

  const where = s.addresses || {};
  host.append(fact('This box is at', (where.urls || []).join('   ') || 'no network'));

  volumeLine(host, 'Root disk', (s.storage || {}).root);
  if ((s.storage || {}).media) {
    volumeLine(host, 'Media disk', s.storage.media);
    if (s.storage.media.same_disk_as_root) {
      host.append(fact('', 'the library is on the same disk as the system'));
    }
  }
  /* Right here in the storage block, not in a footnote. The trash is counted
     in "used" above, so without this line it is gigabytes nothing on this box
     can account for - which is exactly how it becomes a support call. */
  const bin = s.trash || {};
  host.append(fact('Trash', bin.items
    ? (bin.items + (bin.items === 1 ? ' item, ' : ' items, ') + mb(bin.bytes) +
       '   \\u2190 still using this space until it is emptied')
    : 'empty', bin.items ? 'warn' : ''));

  const hw = s.hardware || {};
  /* Both lines now say what the TELEVISION is doing, not what a probe run
     from this process inferred. The probe kept its job - what the hardware
     can do and what is installed - but it lost the right to state live
     facts, having once told a customer their box was decoding in software
     while the Watch tab showed vaapi on the same box at the same moment. */
  const dec = hw.decode || {}, snd = hw.sound || {};
  host.append(fact('Picture', dec.summary || 'unknown',
    dec.working === false ? 'warn' : ''));
  host.append(fact('Sound', snd.summary ||
    (hw.audio_devices || []).join(', ') || 'no HDMI audio found',
    snd.working === false ? 'warn' : ''));
  if (snd.setup && snd.setup !== snd.summary) host.append(fact('', snd.setup));
  if (snd.advice) host.append(fact('', snd.advice, 'warn'));
  /* A diagnosis with no action is the same as broken, for somebody who does
     not have - and must never be asked for - a terminal. */
  host.append(repairRow());
  if (s.temperature) host.append(fact('Temperature', s.temperature.celsius + ' \\u00b0C'));
  if (s.load) {
    host.append(fact('Load', s.load.one_minute + ' over ' + s.load.cores + ' cores',
      s.load.busy ? 'warn' : ''));
  }
  host.append(fact('Remote inputs', ((s.input || {}).backends || []).join(', ') || 'none live'));
  const share = s.share || {};
  host.append(fact('File share', (share.running ? 'running' : share.state) +
    (share.path ? '  \\u00b7  ' + share.path : '')));

  $('#detail').textContent = JSON.stringify(s, null, 1);
  drawClock(s.timezone || {});
  loadClock();               // not awaited: it shells out, and it is a panel
  drawPresses((s.input || {}).recent || []);
  drawBackupInfo();
  loadUpdates();
  loadNetwork();
}

$('#detail-toggle').onclick = () => {
  const raw = $('#detail');
  raw.hidden = !raw.hidden;
  $('#detail-toggle').textContent = raw.hidden ? 'SHOW THE RAW DETAIL' : 'HIDE THE RAW DETAIL';
};

/* -- can this box still do what these buttons ask? ------------------------
   Asked when this page opens, and when somebody presses CHECK AGAIN. Never
   on a timer: the answer costs one short-lived process per privileged
   command, on a box that is playing video, and there is no login here to
   stop a browser left open on this tab asking for it all night. */
async function loadPrivileges() {
  let state;
  try { state = await api('/api/system/privileges'); }
  catch (e) { return; }        // a check that could not run is not a fault
  drawPrivileges(state);
}

function drawPrivileges(state, note) {
  const box = $('#privileges');
  box.textContent = '';
  if (!state || !state.needs_repair) { box.hidden = true; return; }
  box.hidden = false;

  box.append(el('p', 'now', state.headline));
  box.append(el('p', null, state.message));

  /* Named one by one, in the words printed on them. "Some features are
     unavailable" is what the box may as well not say at all. The box knows
     which GROUPS of commands were refused rather than which single button, so
     this is what the fault affects rather than a promise about every one of
     them - on the unit this was found on, Shut Down was the one that still
     worked while the rest of the same group did not. */
  if ((state.buttons || []).length) {
    box.append(el('p', null, 'This affects:'));
    const list = el('ul');
    for (const button of state.buttons) list.append(el('li', null, button));
    box.append(list);
  }

  /* The answer to whatever just happened, if something did: the honest "this
     is not something the dashboard is allowed to do", in its own words. */
  if (note) box.append(el('p', null, note));

  /* The only place in this entire product where anybody is told to type a
     command. It is here because the alternative is a page with no password on
     it being able to write sudo's own configuration, which would hand the box
     to anyone on the network - so the command is shown for the faults typing
     it actually fixes, and never for the one it does not. */
  if (state.repairable) {
    box.append(el('p', null,
      'Type this on the box itself, signed in as ' + state.user + ':'));
    const command = el('code', 'typeit', state.command);
    box.append(command);

    const bar = el('div', 'bar');
    const copy = el('button', 'ghost', 'COPY THE COMMAND');
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(state.command);
        toast('Copied.');
      } catch (e) {
        // Clipboard needs a secure context in some browsers and this page is
        // plain HTTP by design. Highlighting it beats failing silently.
        const range = document.createRange();
        range.selectNodeContents(command);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        toast('Highlighted it - copy it from there.');
      }
    };
    /* One click, and an honest answer either way. Unprivileged - which is
       what the dashboard always is on a real box - this changes nothing and
       says so, which is the whole point: the reply is the sentence explaining
       why, not a spinner and a lie. */
    const fix = el('button', null, 'TRY THE REPAIR FROM HERE');
    fix.onclick = async () => {
      fix.disabled = true;
      try {
        const done = await api('/api/system/privileges/repair', {method: 'POST'});
        if (done.applied) {
          toast('The permission is back.');
          loadPrivileges();
          return;
        }
        drawPrivileges(state, done.message);
      } catch (e) {
        drawPrivileges(state, e.message);
      }
    };
    const again = el('button', 'ghost', 'CHECK AGAIN');
    again.onclick = () => loadPrivileges();
    bar.append(fix, copy, again);
    box.append(bar);
  }
}

/* -- the remote test ------------------------------------------------------ */
function drawPresses(rows) {
  const host = $('#inputs');
  host.textContent = '';
  if (!rows.length) { host.append(el('p', 'empty', 'nothing yet - press a button')); return; }
  const newest = rows[rows.length - 1];
  for (const press of rows.slice().reverse()) {
    const row = el('div', 'press' + (press.at > Sys.lastPress ? ' fresh' : ''));
    row.append(el('span', 'who', press.backend));
    row.append(el('span', 'what', press.action +
      (press.value === null || press.value === undefined ? '' : '  ' + press.value)));
    host.append(row);
  }
  Sys.lastPress = newest ? newest.at : 0;
}

/* -- the log -------------------------------------------------------------- */
async function loadLog() {
  const query = new URLSearchParams();
  if ($('#log-unit').value) query.set('unit', $('#log-unit').value);
  if ($('#log-level').value) query.set('level', $('#log-level').value);
  if ($('#log-search').value.trim()) query.set('search', $('#log-search').value.trim());
  query.set('lines', '300');

  try {
    const page = await api('/api/system/logs?' + query.toString());
    const box = $('#log');
    if (!page.available) { box.textContent = page.note; return; }
    box.textContent = page.entries.length
      ? page.entries.map(e => `${e.time}  ${e.level.padEnd(8)}${e.message}`).join('\\n')
      : '(nothing matching)';
    box.scrollTop = box.scrollHeight;
  } catch (e) { $('#log').textContent = e.message; }
}

$('#log-refresh').onclick = loadLog;
$('#log-unit').onchange = loadLog;
$('#log-level').onchange = loadLog;
$('#log-search').oninput = () => { clearTimeout(Sys.searchTimer);
  Sys.searchTimer = setTimeout(loadLog, 300); };

$('#log-follow').onclick = () => {
  if (Sys.follow) {
    clearInterval(Sys.follow); Sys.follow = null;
    $('#log-follow').textContent = 'FOLLOW';
    return;
  }
  Sys.follow = setInterval(loadLog, 3000);
  $('#log-follow').textContent = 'STOP FOLLOWING';
  loadLog();
};

$('#log-copy').onclick = async () => {
  try {
    const text = await (await fetch('/api/system/support')).text();
    await navigator.clipboard.writeText(text);
    toast('Copied. Paste that to JV Projects.');
  } catch (e) {
    // Clipboard needs a secure context in some browsers, and this is plain
    // HTTP by design. Falling back to "here it is, select it" beats failing.
    window.open('/api/system/support', '_blank');
  }
};

/* -- the clock ------------------------------------------------------------ */
function drawClock(clock) {
  const host = $('#clock');
  host.textContent = '';
  host.append(fact('Time here', clock.local_time || 'unknown'));
  host.append(fact('Timezone', clock.timezone || 'unknown'));
  host.append(fact('Kept in sync', clock.synchronised === null ? 'cannot tell'
    : (clock.synchronised ? 'yes' : 'NO'), clock.warning ? 'warn' : ''));
  if (clock.warning) {
    host.append(el('p', 'note warn',
      'Nothing is correcting this clock, so it will drift. Channels that ' +
      'change with the time of day will start doing it at the wrong time.'));
  }

  const picker = el('select');
  picker.append(el('option', null, 'Change the timezone\\u2026'));
  picker.onfocus = async () => {
    if (picker.dataset.loaded) return;
    picker.dataset.loaded = '1';
    try {
      const {timezones} = await api('/api/system/timezones');
      for (const zone of timezones) {
        const option = el('option', null, zone);
        option.value = zone;
        if (zone === clock.timezone) option.selected = true;
        picker.append(option);
      }
    } catch (e) { toast(e.message, true); }
  };
  picker.onchange = async () => {
    if (!picker.value) return;
    try {
      await json('/api/system/timezone', 'POST', {timezone: picker.value});
      toast('Timezone set to ' + picker.value);
      loadSystem();
    } catch (e) {
      toast(e.message, true);
      /* Setting the clock is one of the things this box needs root for, so a
         refusal here is the same fault the banner exists for. The toast is
         gone in four seconds; the fault is not, and the banner is the only
         thing on the page that says what actually fixes it. */
      loadPrivileges();
    }
  };
  const field = el('div', 'field');
  field.append(el('label', null, 'Timezone'), picker);
  host.append(field);
}

/* The other half of the clock: whether it is right at all, and the one
   outbound request this product makes on its own.

   Asked when this page opens and after a change, never on a timer - it shells
   out to timedatectl, and this page has no login on it to stop a browser left
   open on this tab asking all night. */
async function loadClock() {
  let clock;
  try { clock = await api('/api/system/clock'); }
  catch (e) { return; }        // a reading that failed is not a fault to shout
  drawClockAlarm(clock);
  drawDetection(clock.detection || {});
}

function drawClockAlarm(clock) {
  const box = $('#clock-alarm');
  box.textContent = '';
  /* headline is set for the quiet case too - a clock the network has already
     corrected, which will be wrong again after the next power cut and which
     nobody would otherwise ever learn about. alarm is the loud one: wrong
     now, and nothing is going to fix it by itself. */
  if (!clock || !clock.headline) { box.hidden = true; return; }
  box.hidden = false;
  box.append(el('p', 'now', clock.headline));
  if (clock.detail) box.append(el('p', null, clock.detail));
  const sync = clock.sync || {};
  if (sync.fix) box.append(el('p', null, sync.fix));
}

function drawDetection(detection) {
  const host = $('#clock-detect');
  host.textContent = '';
  if (detection.enabled === undefined) return;

  const picker = el('select');
  for (const [v, label] of [['yes', 'On'], ['no', 'Off']]) {
    const option = el('option', null, label);
    option.value = v;
    if ((v === 'yes') === detection.enabled) option.selected = true;
    picker.append(option);
  }
  picker.onchange = async () => {
    try {
      const body = await json('/api/system/clock/detection', 'POST',
                              {enabled: picker.value === 'yes'});
      toast(body.note || 'Saved.');
      loadSystem();
    } catch (e) { toast(e.message, true); loadClock(); }
  };
  const field = el('div', 'field');
  field.append(el('label', null, 'Work out where this box is'), picker);
  host.append(field);

  /* Printed beside the switch and never behind a "learn more". This is the
     one feature in the product that sends anything to anybody, and the
     sentence that says exactly what leaves the box is the reason somebody can
     make an informed decision about the switch above. */
  host.append(el('p', 'note', detection.what_is_sent || ''));
}

/* -- the config file ------------------------------------------------------ */
$('#cfg-download').onclick = () => { window.location.href = '/api/system/config'; };

const cfgPicker = el('input');
cfgPicker.type = 'file';
cfgPicker.accept = '.yaml,.yml,text/yaml';
cfgPicker.style.display = 'none';
cfgPicker.onchange = async () => {
  const file = cfgPicker.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const res = await fetch('/api/system/config?confirm=yes', {
      method: 'POST', headers: {'Content-Type': 'text/yaml'}, body: text,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'that config was refused');
    toast('Config restored.');
    loadSystem(); refresh();
  } catch (e) { toast(e.message, true); }
  cfgPicker.value = '';
};
document.body.append(cfgPicker);
$('#cfg-upload').onclick = () => cfgPicker.click();

async function drawBackupInfo() {
  const host = $('#cfg-backup');
  host.textContent = '';
  let info;
  try { info = await api('/api/system/config/backup'); } catch (e) { return; }
  if (!info.exists) {
    host.append(el('p', 'note',
      'Nothing automatic has edited your config yet, so there is no ' +
      'before-and-after copy to go back to.'));
    return;
  }
  host.append(el('p', 'note',
    'A copy of your config from before anything automatic touched it is kept ' +
    'on the box (' + mb(info.bytes) + '). That is the one to go back to when ' +
    'everything has gone wrong.'));
  const bar = el('div', 'bar');
  bar.append(arm(el('button', 'danger ghost', 'PUT THAT ONE BACK'),
    'TAP AGAIN TO RESTORE IT', async () => {
      try {
        await json('/api/system/config/backup/restore?confirm=yes', 'POST', {});
        toast('Your original config is back.');
        loadSystem(); refresh();
      } catch (e) { toast(e.message, true); }
    }));
  host.append(bar);
}

/* ========================================================================
   TV: the schedule, the filler, the start-up clip and the picture.
   ======================================================================== */
const Tv = {channel: null, blocks: []};

const hhmm = m => String(Math.floor(m / 60)).padStart(2, '0') + ':' +
                  String(m % 60).padStart(2, '0');

async function loadTelevision() {
  const picker = $('#sched-channel');
  if (!picker.dataset.filled) {
    try {
      const {channels} = await api('/api/channels');
      for (const c of channels) {
        const option = el('option', null, 'CH ' + pad(c.number) + '  ' + c.name);
        option.value = String(c.number);
        picker.append(option);
      }
      picker.dataset.filled = '1';
      picker.onchange = () => { Tv.channel = picker.value; loadSchedule(); };
      Tv.channel = channels.length ? String(channels[0].number) : null;
    } catch (e) { /* the config may be unreadable; the rest still works */ }
  }
  loadSchedule();
  loadFiller();
  loadBranding();
}

/* -- the schedule --------------------------------------------------------- */
async function loadSchedule() {
  if (!Tv.channel) return;
  let data;
  try { data = await api('/api/schedule/' + Tv.channel); }
  catch (e) { $('#sched-day').textContent = ''; return; }

  Tv.blocks = data.blocks || [];
  drawDay(data.day || [], data.now || {});

  // The clock, right here. Dayparting on a box whose time is wrong behaves
  // in a way that looks exactly like a bug in the schedule.
  const clock = $('#sched-clock');
  clock.textContent = '';
  const c = data.clock || {};
  clock.append(fact('Box time', (c.local_time || '') + '   ' + (c.timezone || ''),
                    c.warning ? 'warn' : ''));
  if (c.sync_summary) clock.append(fact('Clock last set', c.sync_summary));
  /* Two different faults, said in the order they matter. A date from before
     this software existed is not drift - every daypart on this page is
     already firing at the wrong hour, and the cause is a two-dollar part. */
  if (c.plausible === false) {
    clock.append(el('p', 'note warn',
      'This box thinks it is a completely different day, so every time on ' +
      'this page is already being read against the wrong clock. Connect the ' +
      'box to the internet and it will correct itself within a minute. See ' +
      'SYSTEM for what to do if it keeps coming back wrong.'));
  } else if (c.warning) {
    clock.append(el('p', 'note warn',
      'Nothing is keeping this clock correct, so it will drift - and a ' +
      'schedule is only as right as the clock it runs on. Fix that under ' +
      'SYSTEM before trusting these times.'));
  }

  const now = $('#sched-now');
  now.textContent = '';
  now.append(el('p', 'note', 'Right now: ' + (data.now.summary || '')));

  drawBlocks();
}

function drawDay(day, now) {
  const strip = $('#sched-day');
  strip.textContent = '';
  const bar = el('div', 'day');
  for (const seg of day) {
    const piece = el('div', 'seg ' + (seg.kind === 'gap' ? 'gap' : 'block') +
                             (seg.off_air ? ' off' : '') +
                             (now.active && seg.name === now.name ? ' on' : ''));
    piece.style.flex = String(seg.minutes);
    piece.textContent = seg.minutes >= 90
      ? (seg.off_air ? 'OFF AIR' : (seg.name || '')) : '';
    piece.title = hhmm(seg.start) + '-' + hhmm(seg.end) + '  ' +
                  (seg.off_air ? 'off air' : (seg.name || seg.label));
    bar.append(piece);
  }
  strip.append(bar);

  const hours = el('div', 'hours');
  for (let h = 0; h < 24; h += 3) hours.append(el('span', null, hhmm(h * 60)));
  strip.append(hours);
}

function drawBlocks() {
  const host = $('#sched-blocks');
  host.textContent = '';
  if (!Tv.blocks.length) {
    host.append(el('p', 'empty',
      'No schedule - this channel is the same thing all day.'));
    return;
  }
  Tv.blocks.forEach((block, index) => {
    const row = el('div', 'blockrow');
    const from = el('input', 't'); from.type = 'time'; from.value = block.from;
    const to = el('input', 't'); to.type = 'time'; to.value = block.to;
    const name = el('input', 'n');
    name.placeholder = block.off_air ? 'off air' : 'what this block is called';
    name.value = block.name || '';
    name.disabled = !!block.off_air;

    from.onchange = () => { block.from = from.value; };
    to.onchange = () => { block.to = to.value; };
    name.onchange = () => { block.name = name.value; };

    const off = el('button', 'ghost' + (block.off_air ? ' danger' : ''),
                   block.off_air ? 'OFF AIR' : 'ON AIR');
    off.onclick = () => {
      block.off_air = !block.off_air;
      if (block.off_air) block.name = null;
      drawBlocks();
    };
    const remove = el('button', 'danger ghost', '\\u00d7');
    remove.onclick = () => { Tv.blocks.splice(index, 1); drawBlocks(); };

    row.append(from, to, name, off, remove);
    host.append(row);
  });
}

$('#sched-add').onclick = () => {
  Tv.blocks.push({from: '06:00', to: '12:00', name: '', off_air: false});
  drawBlocks();
};

$('#sched-save').onclick = async () => {
  const blocks = Tv.blocks.map(b => {
    const out = {from: b.from, to: b.to};
    if (b.off_air) out.off_air = true;
    else {
      if (b.name) out.name = b.name;
      // A block can also swap in a different folder. That is written by hand
      // in config.yaml and there is no field for it here, so it has to be
      // handed straight back - otherwise nudging a time by ten minutes in this
      // editor silently deletes it out of somebody's config.
      if (b.path) out.path = b.path;
    }
    return out;
  });
  try {
    await json('/api/schedule/' + Tv.channel, 'PUT', {blocks});
    toast('Schedule saved.');
    loadSchedule();
    refresh();
  } catch (e) { toast(e.message, true); }
};

$('#sched-when').onchange = async () => {
  const [h, m] = $('#sched-when').value.split(':').map(Number);
  try {
    const r = await api(`/api/schedule/${Tv.channel}/preview?minute=${h * 60 + m}`);
    $('#sched-preview').textContent = 'At ' + r.at + ': ' + r.summary;
  } catch (e) { $('#sched-preview').textContent = e.message; }
};

/* -- filler --------------------------------------------------------------- */
async function loadFiller() {
  const host = $('#filler');
  let data;
  try { data = await api('/api/filler'); }
  catch (e) { host.textContent = ''; host.append(el('p', 'empty', e.message)); return; }

  host.textContent = '';
  for (const clip of data.clips) {
    const row = el('div', 'fact');
    row.append(el('span', 'key', clip.label));
    if (clip.exists) {
      const play = el('button', 'ghost', 'PLAY');
      play.onclick = () => {
        const video = el('video');
        video.src = '/api/filler/' + clip.name;
        video.controls = true;
        video.autoplay = true;
        video.style.maxWidth = '100%';
        row.parentElement.insertBefore(video, row.nextSibling);
        play.disabled = true;
      };
      const value = el('span', 'val', mb(clip.bytes) + '   ');
      value.append(play);
      row.append(value);
    } else {
      row.append(el('span', 'val warn', 'not generated yet'));
    }
    host.append(row);
  }

  const make = el('button', null, 'GENERATE THE FILLER CLIPS');
  make.disabled = !data.ffmpeg;
  make.onclick = async () => {
    make.disabled = true;
    make.textContent = 'GENERATING\\u2026 (this takes a moment)';
    try {
      const r = await api('/api/filler/generate', {method: 'POST'});
      toast('Made ' + r.generated.length + ' clips.');
    } catch (e) { toast(e.message, true); }
    make.textContent = 'GENERATE THE FILLER CLIPS';
    make.disabled = false;
    loadFiller();
  };
  const bar = el('div', 'bar');
  bar.style.margin = '.8rem 0';
  bar.append(make);
  host.append(bar);
  if (!data.ffmpeg) {
    host.append(el('p', 'note warn', 'ffmpeg is not installed, so these cannot be made here.'));
  }

  const often = el('input');
  often.type = 'range'; often.min = 0; often.max = 100; often.step = 5;
  often.value = String(Math.round((data.bumper_chance || 0) * 100));
  const oftenLabel = el('span', 'val', often.value + '% of episode changes');
  often.oninput = () => { oftenLabel.textContent = often.value + '% of episode changes'; };
  const oftenField = el('div', 'field');
  oftenField.append(el('label', null, 'How often a bumper plays'), often, oftenLabel);
  host.append(oftenField);

  const effect = el('select');
  for (const [v, label] of [['none', 'Cut straight over'],
                            ['static', 'Analog snow'], ['glitch', 'Digital glitch']]) {
    const option = el('option', null, label);
    option.value = v;
    if (v === data.transition) option.selected = true;
    effect.append(option);
  }
  const effectField = el('div', 'field');
  effectField.append(el('label', null, 'When the channel changes'), effect);
  host.append(effectField);

  host.append(el('p', 'note', 'Channels that play bumpers:'));
  const opts = {};
  for (const c of data.channels) {
    const row = el('div', 'fact');
    const toggle = el('select');
    for (const [v, label] of [['yes', 'Yes'], ['no', 'No']]) {
      const option = el('option', null, label);
      option.value = v;
      if ((v === 'yes') === c.bumpers) option.selected = true;
      toggle.append(option);
    }
    opts[c.number] = toggle;
    row.append(el('span', 'key', 'CH ' + pad(c.number) + ' ' + c.name));
    const value = el('span', 'val');
    value.append(toggle);
    row.append(value);
    host.append(row);
  }

  const save = el('button', null, 'SAVE FILLER SETTINGS');
  save.onclick = async () => {
    const channels = {};
    for (const [number, toggle] of Object.entries(opts)) {
      channels[number] = toggle.value === 'yes';
    }
    try {
      await json('/api/filler/settings', 'POST', {
        bumper_chance: Number(often.value) / 100,
        transition: effect.value,
        channels,
      });
      toast('Saved.');
      loadFiller();
    } catch (e) { toast(e.message, true); }
  };
  const saveBar = el('div', 'bar');
  saveBar.style.marginTop = '.8rem';
  saveBar.append(save);
  host.append(saveBar);
}

/* -- the start-up clip and the picture ------------------------------------ */
async function loadBranding() {
  let data;
  try { data = await api('/api/branding'); } catch (e) { return; }

  const host = $('#branding');
  host.textContent = '';
  const splash = data.splash || {};
  host.append(fact('Now playing at start-up', splash.summary || ''));

  if (splash.enabled && splash.exists) {
    const video = el('video');
    video.src = '/api/filler/' + (splash.kind === 'custom'
      ? 'custom_splash.mp4' : 'boot_splash.mp4');
    video.controls = true;
    video.style.maxWidth = '100%';
    video.style.marginTop = '.6rem';
    host.append(video);
  }

  host.append(el('p', 'note',
    'A start-up clip has to be under ' + data.max_seconds + ' seconds. Whatever ' +
    'you put here, the television gives up on it and starts anyway if it has ' +
    'not finished - it can never leave you looking at a black screen.'));

  const picker = el('input');
  picker.type = 'file';
  picker.accept = 'video/*';
  picker.style.display = 'none';
  picker.onchange = async () => {
    const file = picker.files[0];
    if (!file) return;
    try {
      const res = await fetch('/api/branding/splash', {
        method: 'POST', headers: {'Content-Type': 'application/octet-stream'},
        body: file,
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || 'that clip was refused');
      toast('Start-up clip set (' + body.seconds + 's).');
      loadBranding();
    } catch (e) { toast(e.message, true); }
    picker.value = '';
  };

  const upload = el('button', null, 'USE MY OWN CLIP');
  upload.onclick = () => picker.click();
  const restore = el('button', 'ghost', 'BACK TO JV PROJECTS');
  restore.onclick = async () => {
    try {
      await api('/api/branding/splash/default', {method: 'POST'});
      toast('The JV Projects clip is back.');
      loadBranding();
    } catch (e) { toast(e.message, true); }
  };
  const off = el('button', 'ghost', splash.enabled ? 'TURN IT OFF' : 'TURNED OFF');
  off.disabled = !splash.enabled;
  off.onclick = async () => {
    try {
      await api('/api/branding/splash/off', {method: 'POST'});
      toast('No start-up clip.');
      loadBranding();
    } catch (e) { toast(e.message, true); }
  };
  const bar = el('div', 'bar');
  bar.style.marginTop = '.8rem';
  bar.append(upload, restore, off);
  host.append(bar, picker);

  drawAppearance(data);
}

/* ------------------------------------------------------------------------
   The live picture preview.

   The sliders below change the television as they move, because curvature has
   no correct value and the only way anybody sets it is by looking at the
   screen while they drag. Nothing here saves anything: the box puts the saved
   picture back on its own after twenty seconds of silence (app.py,
   CRT_PREVIEW_HOLD_SECONDS), so a tab closed mid-drag cannot leave somebody's
   television permanently curved.

   THE THROTTLE. Every preview that lands makes mpv compile a new shader, on a
   two-core Celeron that is also decoding video. A slider fires an event per
   pixel of travel, so at most one message goes out per PREVIEW_EVERY_MS while
   a control is moving - and one more when it is let go, which is the whole
   point: the throttle will have swallowed the last few pixels, and the value
   the customer stopped on is the one they expect to be looking at. The box
   has a throttle of its own (app.py, _flush_crt) for callers who are not this
   page; this one is here to stop the messages being sent at all.
   ------------------------------------------------------------------------ */
const PREVIEW_EVERY_MS = 400;
/* And the other half of it: the box gives a preview up after twenty seconds
   with nothing heard, which is what makes walking away safe - but somebody who
   drags a slider and then just LOOKS at the television for half a minute has
   not gone anywhere, and having the picture snap back to saved underneath them
   while the sliders still show their value would be baffling. So while the
   Picture panel is actually in front of somebody, the value it is showing is
   re-sent. An unchanged value costs the box nothing: app.py compares it to
   what is on the screen and compiles no shader. */
const PREVIEW_HEARTBEAT_MS = 8000;
const Preview = {last: 0, timer: null, read: null, sent: null, live: false};

/* "In front of somebody" means both of these, and neither is a courtesy: a
   phone that locked its screen and a tab left on another section are exactly
   the cases where the picture must go back on its own. */
const watchingThePicture = () =>
  document.visibilityState === 'visible' && !$('#tab-tv').hidden;

/* `again` is the heartbeat, which re-sends a value that has not changed on
   purpose. Everything else skips an unchanged panel: letting go of a slider
   fires pointerup and change together, and a picture that is already right is
   not worth two more messages. */
async function sendPreview(again) {
  if (!Preview.read) return;
  const body = JSON.stringify(Preview.read());
  if (!again && body === Preview.sent) return;
  Preview.last = Date.now();
  Preview.sent = body;
  Preview.live = true;
  /* Silent on failure. A television that is not running answers every one of
     these, and a toast per slider pixel is worse than no picture change at
     all - SAVE PICTURE SETTINGS still says so, in one sentence, once. The
     value is forgotten so the next nudge tries again rather than deciding
     nothing has changed. */
  try { await json('/api/branding/preview/picture', 'POST', JSON.parse(body)); }
  catch (e) { Preview.sent = null; }
}

/* `settled` is a control being let go, which always sends. */
function previewSoon(settled) {
  if (Preview.timer) { clearTimeout(Preview.timer); Preview.timer = null; }
  if (settled) { sendPreview(false); return; }
  const wait = Math.max(0, PREVIEW_EVERY_MS - (Date.now() - Preview.last));
  Preview.timer = setTimeout(
    () => { Preview.timer = null; sendPreview(false); }, wait);
}

setInterval(() => {
  if (Preview.live && watchingThePicture()) sendPreview(true);
}, PREVIEW_HEARTBEAT_MS);

/* Leaving the page puts the saved picture back, rather than making somebody
   wait out the box's own timeout wondering what they have done. sendBeacon
   because a fetch started during pagehide is not guaranteed to leave. */
addEventListener('pagehide', () => {
  if (Preview.live) navigator.sendBeacon('/api/branding/preview/cancel');
});

function drawAppearance(data) {
  const host = $('#appearance');
  host.textContent = '';
  const crt = data.crt || {}, osd = data.osd || {};

  const enabled = el('select');
  for (const [v, label] of [['yes', 'On'], ['no', 'Off']]) {
    const option = el('option', null, label);
    option.value = v;
    if ((v === 'yes') === crt.enabled) option.selected = true;
    enabled.append(option);
  }
  /* Every knob the shader is generated from, on the same slider scale: the
     API takes fractions, a person drags whole numbers. The three that used to
     be missing - scanlines, how strong they are, and how dark the edges go -
     were already accepted by the endpoint with nothing on the page sending
     them, which is a setting that exists only for whoever reads the source. */
  const scanlines = el('select');
  for (const [v, label] of [['yes', 'On'], ['no', 'Off']]) {
    const option = el('option', null, label);
    option.value = v;
    if ((v === 'yes') === crt.scanlines) option.selected = true;
    scanlines.append(option);
  }

  function slider(value, max, fallback) {
    const input = el('input');
    input.type = 'range'; input.min = 0; input.max = max; input.step = 1;
    input.value = String(Math.round(
      (value === undefined || value === null ? fallback : value) * 100));
    return input;
  }
  const curve = slider(crt.curvature, 50, 0);
  const lines = slider(crt.scanline_intensity, 100, 0);
  const edges = slider(crt.vignette, 100, 0);
  const corners = slider(crt.corner_radius, 30, 0);

  const banner = el('input');
  banner.type = 'number'; banner.min = 0; banner.max = 60; banner.step = 1;
  banner.value = String(osd.channel_bug_seconds || 4);

  /* The whole panel, in the API's own words. Sent as one on every preview:
     the television merges what arrives onto what it is already showing, and
     six named settings is one short line either way.

     `sent` is forgotten here rather than kept, because this runs after a save
     and after PUT THE SAVED PICTURE BACK - both of which change what is on
     the television without going through sendPreview. Remembering the old
     value across one of those would skip the next preview of it. */
  Preview.sent = null;
  Preview.read = () => ({
    crt_enabled: enabled.value === 'yes',
    curvature: Number(curve.value) / 100,
    corner_radius: Number(corners.value) / 100,
    vignette: Number(edges.value) / 100,
    scanlines: scanlines.value === 'yes',
    scanline_intensity: Number(lines.value) / 100,
  });
  for (const input of [curve, corners, edges, lines]) {
    /* Dragging: throttled. Let go: sent, whatever the throttle was doing, so
       the value under the finger when it lifted is the value on the screen.
       `change` covers the keyboard and the touch cases `pointerup` misses. */
    input.oninput = () => previewSoon(false);
    input.onpointerup = () => previewSoon(true);
    input.onchange = () => previewSoon(true);
  }
  for (const picker of [enabled, scanlines]) {
    picker.onchange = () => previewSoon(true);   // one press, one message
  }

  for (const [label, input] of [['CRT picture effect', enabled],
                                ['How curved', curve],
                                ['Rounded corners', corners],
                                ['Darkened edges', edges],
                                ['Scanlines', scanlines],
                                ['How strong the scanlines are', lines],
                                ['Channel banner, seconds', banner]]) {
    const field = el('div', 'field');
    field.append(el('label', null, label), input);
    host.append(field);
  }

  const save = el('button', null, 'SAVE PICTURE SETTINGS');
  save.onclick = async () => {
    try {
      const body = await json('/api/branding/appearance', 'POST', {
        crt_enabled: enabled.value === 'yes',
        curvature: Number(curve.value) / 100,
        corner_radius: Number(corners.value) / 100,
        vignette: Number(edges.value) / 100,
        scanlines: scanlines.value === 'yes',
        scanline_intensity: Number(lines.value) / 100,
        channel_bug_seconds: Number(banner.value),
      });
      /* Saved, so there is no preview left to put back: the television's
         reload promotes what is on the screen to what is in config.yaml. */
      Preview.live = false;
      toast(body.note || 'Saved.');
      loadBranding();
    } catch (e) { toast(e.message, true); }
  };
  const undo = el('button', 'ghost', 'PUT THE SAVED PICTURE BACK');
  undo.onclick = async () => {
    try {
      await api('/api/branding/preview/cancel', {method: 'POST'});
      Preview.live = false;
      toast('Back to your saved picture.');
      loadBranding();                 // and the sliders go back with it
    } catch (e) { toast('The TV is not running', true); }
  };
  /* This button used to say SHOW IT ON THE TV, sitting under sliders that did
     nothing until it was pressed - and it never sent the sliders anywhere: it
     asked for the channel banner. The sliders are live now, so that label
     would be a lie about the panel it is in, but the button still has a real
     job. The banner length above it is the one setting here that no slider
     can show you, and INFO is exactly how a viewer sees it. So it keeps the
     job it always did and finally says so. */
  const bannerPreview = el('button', 'ghost', 'SHOW THE CHANNEL BANNER');
  bannerPreview.onclick = async () => {
    try {
      await api('/api/branding/preview', {method: 'POST'});
      toast('Look at the television.');
    } catch (e) { toast('The TV is not running', true); }
  };
  const bar = el('div', 'bar');
  bar.style.marginTop = '.8rem';
  bar.append(save, undo, bannerPreview);
  host.append(bar);
  host.append(el('p', 'note',
    'The sliders change the television as you move them. Nothing is kept '
    + 'until you press SAVE PICTURE SETTINGS, and if you walk away the box '
    + 'puts your saved picture back on its own.'));
  /* There was a note here saying the CRT effect needed a restart. It does not:
     the reload the save triggers puts a new shader on the picture that is
     already showing. The note was printed directly underneath the button that
     had just changed the television, which is the worst place in the product
     to be wrong. */
}

/* ========================================================================
   Network. The one page here that can cut you off from the box, so every
   change is on trial and the page has to be able to find the box again.
   ======================================================================== */
// Built rather than written out, so the page stays free of any absolute
// address and follows whatever scheme it was actually served over.
const MDNS_ORIGIN = location.protocol + '//retrobox.local';
const Net = {timer: null, hunting: false};

async function loadNetwork() {
  let data;
  try { data = await api('/api/network'); }
  catch (e) { return; }

  drawProbation(data.change || {});

  const host = $('#net');
  host.textContent = '';
  const usable = data.usable_interfaces || [];
  for (const iface of data.interfaces || []) {
    const row = el('div', 'fact');
    const what = iface.wireless ? 'Wi-Fi' : 'Wired';
    row.append(el('span', 'key', what + ' · ' + iface.name));
    const value = iface.addresses.length ? iface.addresses.join(', ')
                                         : (iface.up ? 'up, no address' : 'not connected');
    row.append(el('span', 'val' + (iface.addresses.length ? '' : ' warn'), value));
    host.append(row);
  }
  if (data.nameservers && data.nameservers.length) {
    host.append(fact('DNS servers', data.nameservers.join(', ')));
  }
  host.append(fact('This box is called', (data.hostname || {}).mdns_name || ''));

  if (usable.length > 1) {
    host.append(el('p', 'note',
      'Both wired and wireless are up. That is the safest time to change ' +
      'either one - if the change goes wrong, the other keeps you connected.'));
  } else if (usable.length === 1) {
    host.append(el('p', 'note warn',
      usable[0] + ' is the only way this box can be reached. A change here ' +
      'could leave it unreachable until somebody plugs a keyboard into it.'));
  }
}

/* -- the trial ------------------------------------------------------------ */
function drawProbation(change) {
  const box = $('#net-probation');
  if (change.phase !== 'testing') {
    box.hidden = true;
    clearInterval(Net.timer);
    Net.timer = null;
    if (change.phase === 'reverted' && change.message) {
      box.hidden = false;
      box.textContent = '';
      box.append(el('p', 'now', 'Put back'), el('p', null, change.message));
      const bar = el('div', 'bar');
      const ok = el('button', 'ghost', 'DISMISS');
      ok.onclick = () => { box.hidden = true; };
      bar.append(ok);
      box.append(bar);
    } else if (change.phase === 'kept' && change.in_effect === false
               && change.message) {
      // A keep this box had to finish for itself, on settings netplan would
      // not start using. It is the only state where the box is telling you
      // one thing and doing another, switching it off and on again is the
      // whole of the fix, and this panel is the only place it gets said - so
      // an ordinary Keep, which has nothing to add, deliberately shows
      // nothing here at all.
      box.hidden = false;
      box.textContent = '';
      box.append(el('p', 'now', 'Kept, but not in use yet'),
                 el('p', null, change.message));
      const bar = el('div', 'bar');
      const ok = el('button', 'ghost', 'DISMISS');
      ok.onclick = () => { box.hidden = true; };
      bar.append(ok);
      box.append(bar);
    }
    return;
  }

  box.hidden = false;
  box.textContent = '';
  box.className = 'probation';
  box.append(el('p', 'now', 'Testing the new network settings'));
  box.append(el('p', null, change.note || ''));
  const counter = el('p', 'count', change.seconds_left + 's');
  box.append(counter);
  box.append(el('p', null,
    'If this box cannot be reached on the new settings it will put the old ' +
    'ones back by itself. Nothing will be lost - you can simply try again.'));

  const bar = el('div', 'bar');
  const keep = el('button', null, 'KEEP THESE SETTINGS');
  keep.onclick = async () => {
    try {
      await api('/api/network/confirm', {method: 'POST'});
      toast('Kept.');
    } catch (e) { toast(e.message, true); }
    // Redrawn either way. A Keep can fail because the box can no longer confirm
    // the trial, in which case the old settings are already back - and leaving
    // the "testing" panel up with its clock running would tell somebody the
    // opposite of what the box just did.
    loadNetwork();
  };
  const undo = el('button', 'ghost', 'UNDO NOW');
  undo.onclick = async () => {
    try {
      await api('/api/network/revert', {method: 'POST'});
      toast('Put back.');
      loadNetwork();
    } catch (e) { toast(e.message, true); }
  };
  bar.append(keep, undo);
  box.append(bar);

  clearInterval(Net.timer);
  Net.timer = setInterval(() => {
    const left = Math.max(0, Number(counter.textContent.replace('s', '')) - 1);
    counter.textContent = left + 's';
    if (left <= 0) { clearInterval(Net.timer); loadNetwork(); }
  }, 1000);
}

/* After a change the box may have moved. Look for it where it was, where it
   is going, and at its name, rather than leaving a dead page. */
async function findTheBoxAgain(candidates) {
  Net.hunting = true;
  const box = $('#net-probation');
  box.hidden = false;
  box.textContent = '';
  box.append(el('p', 'now', 'Looking for the box'));
  const where = el('p', null, 'Trying: ' + candidates.join(', '));
  box.append(where);

  for (let attempt = 0; attempt < 40; attempt++) {
    for (const base of candidates) {
      try {
        const res = await fetch(base + '/api/network', {cache: 'no-store'});
        if (res.ok) {
          if (base && !location.href.startsWith(base)) {
            where.textContent = 'Found it at ' + base + ' - taking you there.';
            await sleep(1200);
            location.href = base + '/dash';
            return;
          }
          Net.hunting = false;
          loadNetwork();
          return;
        }
      } catch (e) { /* not there yet */ }
    }
    await sleep(1500);
  }
  where.textContent =
    'Could not find the box. If it does not come back on its own in a minute, ' +
    'the settings were undone and it is on the old address again.';
  Net.hunting = false;
}

/* -- the forms ------------------------------------------------------------ */
function netForm(title, fields, onSubmit) {
  const host = $('#net-form');
  host.textContent = '';
  const box = el('div', 'edit');
  box.append(el('h2', null, title));
  const inputs = {};
  for (const [key, label, kind, placeholder] of fields) {
    const input = kind === 'select' ? el('select') : el('input');
    if (kind === 'password') input.type = 'password';
    if (kind === 'number') { input.type = 'number'; }
    if (placeholder) input.placeholder = placeholder;
    inputs[key] = input;
    const field = el('div', 'field');
    field.append(el('label', null, label), input);
    box.append(field);
  }
  const go = el('button', null, 'TEST THESE SETTINGS');
  go.onclick = () => onSubmit(inputs);
  const cancel = el('button', 'ghost', 'CANCEL');
  cancel.onclick = () => { host.textContent = ''; };
  const bar = el('div', 'bar');
  bar.style.marginTop = '.7rem';
  bar.append(go, cancel);
  box.append(bar);
  host.append(box);
  return inputs;
}

async function submitNetwork(url, payload, describe) {
  try {
    const res = await fetch(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.status === 409) {
      // The last-way-in warning. Say it, then let them insist.
      if (window.confirm(data.error + '\\n\\nGo ahead anyway?')) {
        return submitNetwork(url + '?understood=yes', payload, describe);
      }
      return;
    }
    if (!res.ok) {
      toast(data.error || 'That was refused', true);
      /* Writing netplan is privileged too, so the same rule as the clock and
         the Power buttons: a refusal leaves the banner up rather than a toast
         that has vanished by the time anybody reads it. */
      loadPrivileges();
      return;
    }
    $('#net-form').textContent = '';
    drawProbation(data);
    if (describe) findTheBoxAgain(describe);
  } catch (e) { toast('Lost contact - the box may be moving address', true); }
}

$('#net-wifi').onclick = async () => {
  let networks = [];
  try { networks = (await api('/api/network/scan')).networks || []; } catch (e) {}

  const inputs = netForm('Join a wifi network', [
    ['ssid', 'Network', 'select'],
    ['password', 'Password', 'password', 'leave blank for an open network'],
  ], (fields) => {
    const ssid = fields.ssid.value === '__manual__'
      ? window.prompt('Network name (SSID)') : fields.ssid.value;
    if (!ssid) return;
    submitNetwork('/api/network/wifi',
      {ssid, password: fields.password.value || null},
      [location.origin, MDNS_ORIGIN]);
  });

  for (const n of networks) {
    const label = n.ssid + (n.secured ? '  (locked)' : '  (open)') +
                  '  ' + Math.max(0, Math.min(100, 2 * (n.signal + 100))) + '%';
    const option = el('option', null, label);
    option.value = n.ssid;
    inputs.ssid.append(option);
  }
  const manual = el('option', null, 'A hidden network (type the name)');
  manual.value = '__manual__';
  inputs.ssid.append(manual);
};

$('#net-wired').onclick = () => {
  const inputs = netForm('Wired settings', [
    ['mode', 'How', 'select'],
    ['address', 'Address', 'text', '192.168.1.50'],
    ['prefix', 'Netmask bits', 'number', '24'],
    ['gateway', 'Router', 'text', '192.168.1.1'],
    ['dns', 'DNS servers', 'text', '1.1.1.1, 8.8.8.8'],
  ], (fields) => {
    const mode = fields.mode.value;
    const payload = {interface: fields.interface_name, mode};
    if (mode === 'static') {
      payload.address = fields.address.value.trim();
      payload.prefix = Number(fields.prefix.value);
      payload.gateway = fields.gateway.value.trim() || null;
      payload.dns = fields.dns.value.split(',').map(s => s.trim()).filter(Boolean);
    }
    const going = mode === 'static' && payload.address
      ? [location.protocol + '//' + payload.address, location.origin, MDNS_ORIGIN]
      : [location.origin, MDNS_ORIGIN];
    if (mode === 'static' && payload.address) {
      if (!window.confirm(
        'This box will move to ' + payload.address + '.\\n\\n' +
        'This page will look for it there. If it cannot be found, the box ' +
        'undoes the change by itself and comes back on its current address.'
      )) return;
    }
    submitNetwork('/api/network/wired', payload, going);
  });

  for (const [value, label] of [['dhcp', 'Get an address from the router'],
                                ['static', 'Use a fixed address']]) {
    const option = el('option', null, label);
    option.value = value;
    inputs.mode.append(option);
  }
  api('/api/network').then(d => {
    const wired = (d.interfaces || []).filter(i => !i.wireless);
    inputs.interface_name = wired.length ? wired[0].name : 'eth0';
  }).catch(() => { inputs.interface_name = 'eth0'; });
};

$('#net-test').onclick = async () => {
  const button = $('#net-test');
  button.disabled = true;
  button.textContent = 'TESTING…';
  try {
    const r = await api('/api/network/test');
    const host = $('#net-form');
    host.textContent = '';
    const box = el('div', 'edit');
    box.append(fact('Network', r.link ? 'connected' : 'nothing connected',
                    r.link ? '' : 'bad'));
    box.append(fact('Internet', r.internet ? 'reachable' : 'not reachable',
                    r.internet ? '' : 'bad'));
    box.append(fact('DNS', r.dns ? 'resolving' : 'not resolving',
                    r.dns ? '' : 'bad'));
    box.append(el('p', 'note' + (r.ok ? '' : ' warn'), r.summary));
    host.append(box);
  } catch (e) { toast(e.message, true); }
  button.disabled = false;
  button.textContent = 'TEST THE CONNECTION';
};

/* -- software updates ------------------------------------------------------ */
const STAGE_WORDS = {
  checking: 'Checking', preparing: 'Preparing', downloading: 'Downloading',
  installing: 'Installing', restarting: 'Restarting', health: 'Checking it came back',
  done: 'Done',
};
const STAGE_ORDER = ['checking', 'preparing', 'downloading', 'installing',
                     'restarting', 'health', 'done'];

function when(stamp) {
  if (!stamp) return 'never';
  const seconds = Math.max(0, Date.now() / 1000 - stamp);
  if (seconds < 90) return 'just now';
  if (seconds < 3600) return Math.round(seconds / 60) + ' minutes ago';
  if (seconds < 86400) return Math.round(seconds / 3600) + ' hours ago';
  return Math.round(seconds / 86400) + ' days ago';
}

async function loadUpdates() {
  const host = $('#update');
  let data;
  try { data = await api('/api/updates'); }
  catch (e) { host.textContent = ''; host.append(el('p', 'empty', e.message)); return; }

  host.textContent = '';
  const progress = data.progress || {};
  const last = data.last_check || {};

  host.append(fact('This box is running', data.current));
  if (progress.previous_ref) {
    host.append(fact('Previously', String(progress.previous_ref).replace(/^v/, '')));
  }

  // An update that is happening right now takes over the panel.
  if (progress.phase === 'running' || progress.phase === 'rolling_back') {
    host.append(drawStages(progress));
    host.append(el('p', 'note',
      'Leave this page open if you like - it is safe to close it, and this ' +
      'will still be here when you come back.'));
    clearTimeout(Sys.updateTimer);
    Sys.updateTimer = setTimeout(loadUpdates, 2000);
    return;
  }

  // The outcomes, said plainly.
  if (progress.phase === 'rolled_back') {
    host.append(el('p', 'note bad', progress.message || 'The update was undone.'));
  } else if (progress.phase === 'failed') {
    host.append(el('p', 'note warn', progress.message || 'The update did not start.'));
  } else if ((progress.phase === 'success' || progress.phase === 'probation')
             && progress.to_version) {
    // "probation" is a finished update that is still on trial for the next few
    // start-ups. It is an outcome, not a stage, so it belongs here rather than
    // taking the panel over - and without this branch the panel says nothing at
    // all after an update, which reads like one that never happened.
    host.append(el('p', 'note', progress.message ||
      ('Updated to ' + progress.to_version + '.')));
  }

  if (!data.checking_enabled) {
    host.append(el('p', 'note', 'This box does not check for updates.'));
    return;
  }

  host.append(fact('Last checked', when(last.checked_at)));
  if (last.error) host.append(el('p', 'note warn', last.error));

  if (last.available) {
    const head = el('div', 'relhead');
    head.append(el('span', 'v', 'Version ' + last.latest + ' is available'));
    host.append(head);
    host.append(el('p', 'note',
      'Read what has changed, then decide. This box will not install anything ' +
      'on its own.'));

    const notes = el('div', 'notes');
    for (const rel of last.releases || []) {
      const block = el('div', 'rel');
      const line = el('div', 'relhead');
      line.append(el('span', 'v', rel.version), el('span', 'd', rel.published || ''));
      block.append(line);
      const body = el('div');
      // Server-rendered from a tiny allow-list of tags; see updates.render_notes.
      body.innerHTML = rel.notes_html || '';
      block.append(body);
      notes.append(block);
    }
    host.append(notes);

    const go = el('button', null, 'UPDATE TO ' + last.latest);
    arm(go, 'TAP AGAIN TO UPDATE', () => startUpdate(last.latest));
    const bar = el('div', 'bar');
    bar.append(go);
    host.append(bar);
  } else if (!last.error && last.checked_at) {
    host.append(el('p', 'note', 'Up to date.'));
  }

  const bar = el('div', 'bar');
  bar.style.marginTop = '.8rem';
  const check = el('button', 'ghost', 'CHECK NOW');
  check.onclick = async () => {
    check.disabled = true;
    check.textContent = 'CHECKING…';
    try { await api('/api/updates/check', {method: 'POST'}); }
    catch (e) { toast(e.message, true); }
    check.disabled = false;
    check.textContent = 'CHECK NOW';
    loadUpdates();
  };
  bar.append(check);

  if (progress.previous_ref) {
    bar.append(arm(el('button', 'danger ghost', 'GO BACK A VERSION'),
      'TAP AGAIN TO GO BACK', async () => {
        try {
          await api('/api/updates/rollback?confirm=yes', {method: 'POST'});
          toast('Going back to the previous version.');
          loadUpdates();
        } catch (e) { toast(e.message, true); }
      }));
  }
  host.append(bar);
}

function drawStages(progress) {
  const box = el('div');
  box.append(el('p', 'now', progress.phase === 'rolling_back'
    ? 'Putting the previous version back' : 'Updating'));
  box.append(el('p', 'meta', progress.message || ''));

  const strip = el('div', 'stages');
  const at = STAGE_ORDER.indexOf(progress.stage);
  STAGE_ORDER.forEach((name, index) => {
    const chip = el('span', 'stage' + (index === at ? ' on' : (index < at ? ' done' : '')),
                    STAGE_WORDS[name]);
    strip.append(chip);
  });
  box.append(strip);
  box.append(el('p', 'note',
    'The television will go quiet for a moment while this happens. That is ' +
    'expected; it comes back on its own.'));
  return box;
}

async function startUpdate(version) {
  toast('Starting the update. The television will restart.');
  loadUpdates();
  try {
    const res = await fetch('/api/updates/apply?confirm=yes', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({version}),
    });
    const data = await res.json();
    if (!res.ok) toast(data.error || 'The update did not finish', true);
    else toast('Updated to ' + version + '.');
  } catch (e) {
    // The dashboard may have restarted underneath us; the state file knows.
    toast('Lost contact during the update - checking what happened…', true);
  }
  loadUpdates();
  loadSystem();
}

/* -- power ---------------------------------------------------------------- */
const SERVICES = [
  ['restart-tv', 'RESTART THE TV', 'TAP AGAIN', false],
  ['restart-dashboard', 'RESTART DASHBOARD', 'TAP AGAIN', false],
  ['reboot', 'REBOOT THE BOX', 'TAP AGAIN TO REBOOT', true],
  ['shutdown', 'SHUT DOWN', 'TAP AGAIN TO SHUT DOWN', true],
];
for (const [action, label, confirmLabel, danger] of SERVICES) {
  const button = el('button', danger ? 'danger' : null, label);
  arm(button, confirmLabel, async () => {
    try {
      const data = await json(`/api/system/service/${action}?confirm=yes`, 'POST', {});
      toast(data.message);
      if (action === 'restart-dashboard') waitForDashboard();
    } catch (e) {
      toast(e.message, true);
      // A button that just came back refused is the best possible moment to
      // find out whether this box has lost its permission - and the toast is
      // gone in four seconds, where the banner stays until it is fixed.
      loadPrivileges();
    }
  });
  $('#service-buttons').append(button);
}

/* The page we are served from is about to go away. Say so, then poll until
   it answers again rather than leaving a dead tab. */
async function waitForDashboard() {
  $('#sub').textContent = 'restarting the dashboard\\u2026';
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    try {
      const res = await fetch('/api/status', {cache: 'no-store'});
      if (res.ok) {
        $('#sub').textContent = 'back';
        toast('The dashboard is back.');
        refresh(); loadSystem();
        return;
      }
    } catch (e) { /* still down, keep waiting */ }
  }
  $('#sub').textContent = 'the dashboard did not come back - reload the page';
}

arm($('#factory'), 'TAP AGAIN - CHANNELS AND SETTINGS ONLY', async () => {
  try {
    const data = await json('/api/system/factory-reset?confirm=yes', 'POST',
                            {understood: true});
    toast(data.message);
    loadSystem(); refresh(); loadEditor();
  } catch (e) { toast(e.message, true); }
});

api('/api/settings').then(s => {
  if (s.video_extensions) Up.extensions = s.video_extensions;
  if (s.max_files_per_upload) Up.maxFiles = s.max_files_per_upload;
  if (s.chunk_mb) Up.chunkBytes = s.chunk_mb * 1024 * 1024;
}).catch(() => {});

refresh();
setInterval(refresh, 3000);
</script></body></html>
"""


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m retrobox.webui`` - used by the systemd unit."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="retrobox-web", description="Retro Box web dashboard"
    )
    parser.add_argument("--host", default="0.0.0.0", help="bind address")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"port (default {DEFAULT_PORT}, so the URL needs no port in it)",
    )
    parser.add_argument("-c", "--config", help="path to config.yaml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        app = create_app(args.config)
    except ImportError:
        print("Flask is not installed. Run: pip install -e '.[web]'")
        return 1

    chosen = args.port
    try:
        app.run(host=args.host, port=chosen, threaded=True)
    except PermissionError:
        if chosen != DEFAULT_PORT:
            # They asked for this port specifically. Quietly serving somewhere
            # else would be worse than saying we could not.
            print(f"Could not bind port {chosen}: permission denied.")
            return 1
        # Port 80 needs CAP_NET_BIND_SERVICE, which the systemd unit grants.
        # Started by hand, or on a distro where that did not take, a box with
        # no dashboard at all is far worse than one on a different port.
        print(
            f"Could not bind port {DEFAULT_PORT} (that needs CAP_NET_BIND_SERVICE, "
            f"which scripts/retrobox-web.service grants).\n"
            f"Falling back to {FALLBACK_PORT} - reach the box at "
            f"http://retrobox.local:{FALLBACK_PORT}/"
        )
        log.warning("port %d refused; falling back to %d", DEFAULT_PORT, FALLBACK_PORT)
        app.run(host=args.host, port=FALLBACK_PORT, threaded=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
