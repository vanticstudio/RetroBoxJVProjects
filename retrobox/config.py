"""Configuration loading and validation.

The whole box is described by a single YAML file (see ``config.example.yaml``).
This module turns that file into validated :class:`Config` /
:class:`ChannelConfig` objects and fills in sensible defaults so a minimal
config still produces a working television.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .daypart import Daypart, DaypartError, parse_clock

log = logging.getLogger(__name__)


# Video containers we consider "an episode" when scanning a channel folder.
DEFAULT_VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mp4", ".mkv", ".avi", ".m4v", ".mov", ".webm", ".mpg", ".mpeg", ".ts",
)

#: Where this copy of the software is installed - the directory holding the
#: ``retrobox`` package. Worked out from where this file lives rather than
#: read from anywhere, for the reason the updater does the same: a path that
#: can be pointed somewhere else is a path that can be pointed anywhere.
INSTALL_ROOT: Path = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


# ===========================================================================
# The two kinds of value in this file that can hurt somebody
# ===========================================================================
# There are exactly two, and everything in this section exists to settle both
# of them HERE rather than in the dashboard:
#
#   (a) a value that becomes the argv of a subprocess - power_off_command,
#       input.cec_binary, input.cec_osd_name;
#   (b) a value that becomes a folder this box reads, writes or deletes
#       inside - media_root, a channel's path, a daypart's path, bumpers,
#       assets_dir, input.web_socket.
#
# config.yaml can be replaced wholesale by anyone who can reach the dashboard,
# which has no password by design. It can also arrive as a restored backup, a
# factory reset, or a file somebody edited by hand over the file share. A check
# that lives in a web route only covers the first of those, and has twice now
# been found to cover only the field somebody remembered. So the rule is
# enforced by the loader, which every one of those paths goes through.
#
# The shape of every check below is the same as the power_off_command one: ask
# "is this one of the things this box accepts", never "does this look
# dangerous". Spotting dangerous text is a game you lose once; a list you can
# read in full is not.
#
# A refused value is dropped and recorded on the Config rather than made fatal
# - see the note above `refusals` - because a box that will not boot is
# unrecoverable and one field falling back to its default is not.


#: Every suffix this box will agree is a video. Compare with ``in``.
#:
#: ``video_extensions`` decides what :func:`safepath.safe_media_name` will let
#: an unauthenticated upload write, so one extra entry on that list - ".py",
#: ".service", ".sh" - is an upload endpoint for that kind of file. A closed
#: table of real container formats is the only version of this check that
#: cannot be talked around: anything not spelled out here is refused, and a
#: format nobody thought of is a two-word patch rather than a break-in.
VIDEO_EXTENSIONS_ALLOWED: frozenset = frozenset({
    # The ones anybody actually has.
    ".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mts",
    ".mpg", ".mpeg", ".mpe", ".m1v", ".m2v", ".mpv", ".vob", ".ogv", ".ogm",
    # Older rips and camera files, still found in real libraries.
    ".divx", ".flv", ".f4v", ".wmv", ".asf", ".rm", ".rmvb", ".3gp", ".3g2",
    ".mxf", ".qt", ".dv", ".amv", ".nsv", ".mod", ".tod", ".dat",
})


def parse_video_extensions(raw: Any) -> tuple[str, ...]:
    """Turn a ``video_extensions`` config value into a tuple, or refuse it.

    One bad entry refuses the whole list. Keeping the good ones and dropping
    the rest would be a quiet correction, and quiet corrections are how
    somebody ends up running a setting they never chose.
    """
    if raw is None:
        return DEFAULT_VIDEO_EXTENSIONS
    if not isinstance(raw, list) or not raw:
        raise ConfigError("'video_extensions' must be a non-empty list")

    cleaned: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigError("'video_extensions' must be a list of text suffixes")
        suffix = item.strip().lower()
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        if suffix not in VIDEO_EXTENSIONS_ALLOWED:
            shown = suffix if len(suffix) <= 20 else suffix[:20] + "..."
            raise ConfigError(
                f"'video_extensions' contains {shown!r}, which is not a video "
                f"format this box knows. It has to be a container like "
                f"\".mp4\", \".mkv\" or \".avi\" - the upload endpoint uses this "
                f"list to decide what it is allowed to write to the disk."
            )
        if suffix not in cleaned:
            cleaned.append(suffix)
    return tuple(cleaned)


@lru_cache(maxsize=1)
def _sensitive_dirs() -> tuple:
    """Directories a folder of videos is never kept in.

    Deny by place rather than by trying to list everywhere a library is
    ALLOWED to be, because the allowed list is unknowable: an external drive
    turns up at /mnt or /media or /run/media, an NFS or SMB share is mounted
    wherever the customer felt like, and plenty of people keep the lot in
    ~/Videos. Enumerating those would ship a box whose library has vanished,
    and a box whose library has vanished is a van and an afternoon.
    """
    names = [
        # The operating system, and everywhere a program, a library or a
        # systemd unit lives. /etc covers sudoers.d and the system unit
        # directory; /usr and /lib cover the vendor unit directories.
        "/etc", "/boot", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
        "/usr", "/opt", "/root", "/run", "/proc", "/sys", "/dev",
        # Only the parts of /var that hold something executed or scheduled -
        # /var/spool/cron is a crontab, which is a shell. NOT the whole of
        # /var: some systems put the temporary directory under it, and
        # refusing those would reject configs that were always fine.
        "/var/lib", "/var/log", "/var/spool", "/var/cache", "/var/mail",
        "/var/backups", "/var/opt",
        # macOS equivalents, so a developer's machine refuses what the box
        # refuses instead of finding out on the hardware.
        "/System", "/Library", "/Applications",
    ]
    dirs = [Path(name) for name in names]
    # The software itself and the virtualenv it runs in. This is the whole
    # point of the exercise: a library root inside the checkout turns the
    # upload endpoint into an editor for the program that serves it.
    dirs.append(INSTALL_ROOT)
    dirs.append(Path(sys.prefix))
    dirs.append(Path(sys.base_prefix))

    resolved: List[Path] = []
    for item in dirs:
        try:
            real = item.resolve()
        except OSError:                       # pragma: no cover - hostile fs
            continue
        # "/" would deny every path there is, which would take the picture
        # away rather than protect it. It is refused by the containment rule
        # below instead, which is where it belongs.
        if real != real.parent and real not in resolved:
            resolved.append(real)
    return tuple(resolved)


def location_refusal(path: Any, *, allow: Sequence[Path] = ()) -> Optional[str]:
    """Why ``path`` is not a place this box may keep or write videos, or None.

    Resolves first, so a symlink planted under the media root is judged by
    where it actually goes rather than by what it is called.
    """
    try:
        resolved = Path(path).resolve()
    except OSError:                           # pragma: no cover - hostile fs
        return f"{path} cannot be resolved to a real location"

    for permitted in allow:
        if resolved == permitted or permitted in resolved.parents:
            return None

    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):           # pragma: no cover - no home at all
        home = None

    if home is not None:
        if resolved == home:
            return (
                f"{resolved} is the box user's home directory, which holds "
                f"their shell startup files and their .ssh - point this at a "
                f"folder inside it, such as {home / 'Videos'}"
            )
        if home in resolved.parents:
            # Dotfiles, and only under the home directory. This is not a
            # blanket "no hidden folders" rule: the installer deliberately
            # ships a channel pointed at a .welcome folder under the media
            # root, and hidden folders on a drive are ordinary.
            relative = resolved.relative_to(home)
            if any(part.startswith(".") for part in relative.parts):
                return (
                    f"{resolved} is among the box user's dotfiles, where their "
                    f"keys, shell startup files and user services live"
                )

    sensitive_dirs = _sensitive_dirs()
    for sensitive in sensitive_dirs:
        if resolved == sensitive:
            return f"{resolved} belongs to the system, not to the library"
        if sensitive in resolved.parents:
            return f"{resolved} is inside {sensitive}, which belongs to the system"

    # A path that CONTAINS one of these is exactly as wrong as one inside it:
    # media_root "/" makes /etc a channel, and "/home" makes every account on
    # the box one.
    accounts = [Path("/home"), Path("/Users")]
    if home is not None:
        accounts.append(home)
    for anchor in list(sensitive_dirs) + accounts:
        if resolved == anchor or resolved in anchor.parents:
            return f"{resolved} holds {anchor}, which belongs to the system"
    return None


def check_location(path: Any, *, field: str, allow: Sequence[Path] = ()) -> None:
    """Raise :class:`ConfigError` if ``field`` names somewhere it must not."""
    reason = location_refusal(path, allow=allow)
    if reason:
        raise ConfigError(f"'{field}' is not a folder this box will use: {reason}")


#: What ``input.cec_binary`` is allowed to be. libCEC's client is the only
#: program whose output the CEC backend can read, so this is the same closed
#: table :data:`POWER_OFF_COMMANDS` is - the value becomes argv[0] of a real
#: ``subprocess.Popen``. The version suffix is allowed because some distros
#: ship ``cec-client-6.0.2`` and symlink the plain name at it.
_CEC_BINARY_NAME = re.compile(r"cec-client(-[0-9][0-9.]*)?$")
_CEC_BINARY_DIRS = ("", "/usr/bin", "/bin", "/usr/local/bin", "/sbin", "/usr/sbin")


def parse_cec_binary(raw: Any) -> str:
    """Turn an ``input.cec_binary`` value into a program name, or refuse it."""
    if not isinstance(raw, str):
        raise ConfigError("'input.cec_binary' must be text")
    text = raw.strip()
    directory, _, name = text.rpartition("/")
    if (
        not text
        or not _CEC_BINARY_NAME.fullmatch(name)
        or directory not in _CEC_BINARY_DIRS
    ):
        shown = text if len(text) <= 60 else text[:60] + "..."
        raise ConfigError(
            f"'input.cec_binary' is not a program this box will run: {shown}. "
            f"It reads HDMI-CEC key presses, so it has to be libCEC's client - "
            f"\"cec-client\", or the full path to one."
        )
    return text


#: A CEC OSD name is what the TV shows in its input list. libCEC caps it at 14
#: characters. It becomes an element of the same argv, so it may not start
#: with "-" - an argument that can turn into a flag is an argument that can
#: turn into a different command.
_CEC_OSD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,13}$")


def parse_cec_osd_name(raw: Any) -> str:
    if not isinstance(raw, str) or not _CEC_OSD_NAME.fullmatch(raw):
        raise ConfigError(
            "'input.cec_osd_name' must be up to 14 plain characters starting "
            "with a letter or a digit - it is the name your TV shows for this "
            "box, and it is handed straight to the CEC client as an argument"
        )
    return raw


def parse_keyboard_devices(raw: Any) -> List[str]:
    """Pinned evdev device nodes. Only ever ``/dev/input``."""
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list):
        items = list(raw)
    else:
        raise ConfigError("'input.keyboard_devices' must be a list of device paths")
    devices: List[str] = []
    for item in items:
        text = str(item)
        resolved = Path(text).resolve()
        if not str(resolved).startswith("/dev/input/"):
            raise ConfigError(
                f"'input.keyboard_devices' may only name devices under "
                f"/dev/input, not {text}"
            )
        devices.append(text)
    if not devices:
        raise ConfigError("'input.keyboard_devices' cannot be empty")
    return devices


def parse_control_socket(raw: Any) -> str:
    """Where the TV listens for the dashboard's commands.

    ``input/web.py`` unlinks whatever this names before it binds, so a value
    nobody checked is a delete of any file the box user owns. Two rules: it
    has to be named like a socket, and its folder is held to the same rule a
    library folder is - which between them leave nothing worth deleting.

    The systemd runtime directory is allowed through explicitly, because that
    is where this socket belongs on a real box (``$XDG_RUNTIME_DIR/retrobox``,
    i.e. under /run) and /run is otherwise the system's.
    """
    import tempfile

    text = str(raw).strip()
    candidate = Path(os.path.expanduser(text))
    if not text or not candidate.is_absolute() or candidate.suffix != ".sock":
        raise ConfigError(
            "'input.web_socket' must be an absolute path ending in '.sock' - "
            "the box deletes whatever it names before it listens on it"
        )
    runtime = [Path("/run/user"), Path(tempfile.gettempdir())]
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        runtime.append(Path(xdg))
    check_location(candidate.parent, field="input.web_socket", allow=tuple(runtime))
    return text


#: An OSD font name is spliced into an ASS override block by overlay.py
#: (``\\fn<name>``), so a "}" or a backslash would close that block and open
#: another one - the font name could then rewrite the on-screen display. Real
#: font names are letters, digits, spaces and the odd hyphen.
_FONT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


def parse_font(raw: Any) -> str:
    if not isinstance(raw, str) or not _FONT_NAME.fullmatch(raw):
        raise ConfigError(
            "'ui.font' must be a plain font name - letters, digits, spaces, "
            "'.', '_' and '-' - because it is spliced into the on-screen "
            "display's own markup"
        )
    return raw


# How a channel behaves the moment you tune into it.
#   random    - start a fresh random episode (the default, and what most people
#               picture: flip to the channel, something is on). It starts a few
#               seconds in - see start_offset - so it never looks like you
#               pressed play. Episodes keep rolling on a shuffle after that.
#   resume    - remember where you were on that channel and pick up there,
#               so flipping away and back does not restart the episode.
#   broadcast - the channel behaves like a real station that is "always on":
#               a fixed shuffled running order advances in real time whether
#               or not anyone is watching, so you tune in partway through
#               whatever "would" be airing right now.
TUNE_IN_MODES = ("random", "resume", "broadcast")

# Effect shown briefly while changing channels.
#   none   - cut straight to the next channel (default)
#   glitch - a short burst of digital corruption
#   static - classic analog snow
TRANSITION_EFFECTS = ("glitch", "static", "none")

#: The clip that plays when the box is switched on. Shipped in
#: retrobox/assets, and ON by default - a Retro Box shows its own branding at
#: power-up without anybody having to ask for it. Set `boot_splash: false` in
#: config.yaml to go straight to a channel instead.
DEFAULT_BOOT_SPLASH = "boot_splash.mp4"


# What the sleep timer does when it runs out.
#   standby - blank the screen but leave the box running (press power to wake)
#   off     - shut the machine down cleanly, so it is safe to unplug
SLEEP_ACTIONS = ("standby", "off")


#: What the box runs to switch itself off, when the config does not say.
DEFAULT_POWER_OFF_COMMAND: tuple[str, ...] = ("sudo", "poweroff")


def _power_off_table() -> frozenset[tuple[str, ...]]:
    """Every argv this box will ever accept as "switch the machine off".

    ``power_off_command`` becomes the argument list of a real
    ``subprocess.Popen`` (see ``app.py``) - it is not the only one, see
    ``parse_cec_binary`` below - and the config it comes from can be replaced
    wholesale from a dashboard with no password on it. So this is a closed
    table and the check against it is a lookup - the
    question asked of a value is "is this one of the commands that turn a
    computer off", never "does this one look dangerous". Spotting dangerous
    text is a game you lose once; a list you can read in full is not.

    It is written as a cross product only because three short lists are easier
    to keep honest than 126 lines of tuples. Nothing here comes from a request:
    the program, where the distro put it, and whether sudo is in front are each
    fixed here in the source.
    """
    # The programs. Everything else about a shutdown - "-h now", "-p" - is
    # part of the entry, so no argument is ever accepted on its own.
    verbs = (
        ("poweroff",),
        ("halt", "-p"),
        ("systemctl", "poweroff"),
        ("shutdown", "-h", "now"),
        ("shutdown", "-P", "now"),
    )
    # sudo matches the literal command it is given and so does this table, so
    # every place a distro might keep these binaries is spelled out: /sbin on
    # older systems, /usr/sbin once /usr was merged, and the bare name for the
    # ordinary PATH lookup the shipped config uses.
    homes = ("", "/sbin/", "/usr/sbin/", "/bin/", "/usr/bin/")
    # "-n" so sudo fails instead of waiting for a password nobody will type.
    sudos = (
        (), ("sudo",), ("sudo", "-n"), ("/usr/bin/sudo",), ("/usr/bin/sudo", "-n"),
    )
    # The empty argv is in here on purpose: `power_off_command: []` means the
    # feature is off, which is how the test suite keeps from shutting a
    # developer's laptop down, and it is the one value that runs nothing.
    table = {()}
    for sudo in sudos:
        for verb in verbs:
            for home in homes:
                table.add(sudo + (home + verb[0],) + verb[1:])
    return frozenset(table)


#: The whitelist itself. Compare with ``in``; never pattern-match against it.
POWER_OFF_COMMANDS: frozenset[tuple[str, ...]] = _power_off_table()


def parse_power_off_command(raw: Any) -> tuple[str, ...]:
    """Turn a ``power_off_command`` config value into an argv, or refuse it.

    Raises :class:`ConfigError` for anything that is not in
    :data:`POWER_OFF_COMMANDS`. Callers decide what to do about that - the
    loader below keeps the box booting, the dashboard refuses the save.
    """
    if raw is None:
        return DEFAULT_POWER_OFF_COMMAND
    if isinstance(raw, str):
        command = tuple(raw.split())
    elif isinstance(raw, (list, tuple)):
        command = tuple(str(item) for item in raw)
    else:
        raise ConfigError("'power_off_command' must be a string or list of strings")

    if command not in POWER_OFF_COMMANDS:
        # Quoted back so somebody reading the dashboard or the journal can see
        # what was in the file, and clipped so a config full of junk cannot
        # push a wall of it into an error message or a log line.
        shown = " ".join(command)
        shown = (shown[:120] + "...") if len(shown) > 120 else shown
        raise ConfigError(
            f"'power_off_command' is not a command this box will run: {shown}. "
            f"It has to be a plain shutdown - e.g. [\"sudo\", \"poweroff\"], "
            f"[\"sudo\", \"systemctl\", \"poweroff\"] or "
            f"[\"sudo\", \"shutdown\", \"-h\", \"now\"] - or [] to disable it."
        )
    return command


@dataclass(frozen=True)
class UiConfig:
    """Look of the on-screen overlays (the green digital TV readouts)."""

    font: str = "VT323"             # bundled retro terminal font (OFL)
    color: str = "#4DFF5A"          # bright CRT phosphor green
    dim_color: str = "#123B18"      # unlit volume segment / dot colour
    glow: bool = True               # soft glow around text for that CRT bloom


@dataclass(frozen=True)
class CrtConfig:
    """The CRT picture effect applied to the 4:3 video via a GLSL shader."""

    enabled: bool = True
    curvature: float = 0.12         # barrel "bulge" amount (0 = perfectly flat)
    corner_radius: float = 0.065    # rounded-corner size (fraction of screen)
    vignette: float = 0.25          # darkening toward the edges
    scanlines: bool = True
    scanline_intensity: float = 0.12


@dataclass(frozen=True)
class DisplaySleepConfig:
    """Going quiet when there is no television watching.

    This is not about the eight watts. It is about a fan that roars into an
    empty room, heat pumped into secondhand hardware that has already had one
    life, and a box that plainly notices whether anybody is there.

    ``enabled`` off means the watcher is never even built, so the box behaves
    in every respect as it did before this existed.

    IT IS OFF BY DEFAULT, AND THAT IS PENDING THE DASHBOARD, NOT PERMANENT.
    The television half of this feature is finished; the dashboard half - the
    panel that shows a box has gone quiet and offers the Wake button that
    undoes it - is not. Defaulting it on would start every box already sold
    pausing its own picture the moment it updated, with nothing on screen and
    nothing on the dashboard connecting a black television to a box that
    thinks it is working perfectly. These are appliances with no SSH, switched
    off at the wall, owned by people who did not ask for a new behaviour.
    Anybody who wants it today writes ``enabled: true`` and gets the whole
    feature. WHOEVER SHIPS THE DASHBOARD PANEL FLIPS THIS DEFAULT, and the
    test named for it in tests/test_config.py with it.

    ``sleep_after_seconds`` is how long the video output has to have been gone
    before the box believes it. It is a debounce, not a countdown: HDMI
    switches, receivers and televisions drop and re-assert the line while they
    change input, and every one of those flaps looks like somebody switching
    the set off. The default is the one :mod:`retrobox.display` chose for
    itself, and waiting a few seconds costs nothing because by definition
    nobody is watching. Waking is never delayed - see display.py.

    ``non_broadcast`` is the choice the box cannot make for itself.

    A broadcast channel needs no setting: it works out what is airing from the
    wall clock, so after three hours asleep it comes back three hours further
    on, which is the entire illusion this product sells. A channel in `random`
    or `resume` mode has no schedule to recompute from, so there are two
    defensible answers and this picks between them:

    * ``resume``  - carry on exactly where the picture was paused. Exact,
      costs nothing, and cannot overshoot the end of a file.
    * ``advance`` - skip forward by however long the box slept, so television
      does not wait for you. More faithful to the idea, and it has to guess:
      an episode whose length is not already known can be seeked past its own
      end, in which case the channel simply rolls on to the next one.

    The default is ``resume`` because it is the answer that cannot be wrong.
    """

    # Off until the dashboard can show a box has gone quiet and wake it again.
    enabled: bool = False
    sleep_after_seconds: float = 8.0   # display.SLEEP_DEBOUNCE_SECONDS
    non_broadcast: str = "resume"      # resume | advance


NON_BROADCAST_WAKE = ("resume", "advance")


@dataclass(frozen=True)
class WebConfig:
    """Limits on what the LAN dashboard is allowed to write to the disk.

    Uploads are the one place an unauthenticated visitor can consume a
    resource that does not come back, so both of these are guard rails against
    a full filesystem - which on this box means a unit that will not boot.
    """

    max_upload_mb: int = 8192       # one very large film, and no more
    min_free_mb: int = 1024         # refuse an upload that would go below this

    # Chunked folder uploads. A whole series arrives as one session of many
    # files, each cut into chunks so a dropped connection resumes rather than
    # restarts. All four are caps rather than targets: an unbounded session
    # count is a trivial way to spoil someone's evening.
    chunk_mb: int = 8               # size of each piece a file is sent in
    max_files_per_upload: int = 500  # a series, not an entire library
    max_upload_sessions: int = 4    # how many uploads may run at once
    upload_expiry_hours: int = 24   # abandoned chunks are reclaimed after this


@dataclass(frozen=True)
class UpdateConfig:
    """Whether the box looks for new versions of itself, and what it may do.

    ``check`` and ``auto_apply`` are deliberately separate. Checking is
    harmless and useful - the dashboard can say an update is waiting. Applying
    is not: the day a fleet self-applies, one bad tag takes out every unit at
    once, in living rooms, with no way in to fix them because the dashboard
    you would fix them from is on the box that is down. So checking is on and
    applying is off, and turning applying on is a decision somebody makes
    knowingly after the rollback has been proven on real hardware.
    """

    check: bool = True
    auto_apply: bool = False
    check_interval_hours: int = 24


@dataclass(frozen=True)
class TimeConfig:
    """The clock, which on this box decides what the television shows.

    ``dayparts`` change a channel's name and its folder by the hour, so a box
    that does not know which timezone it is in plays the wrong thing at the
    wrong time and looks broken. Nothing ever tells a freshly installed box
    where it is, so on first start-up it works that out from its own internet
    address (see :mod:`retrobox.timekeeping`).

    ``detect_timezone`` is on by default, and it is the only setting here
    because it is the only one that sends anything anywhere. That lookup is
    the single outbound call this product makes about the clock: it carries
    the request and nothing else - no serial, no version, no identifier, no
    information about the box or what is watched on it - and it happens once,
    and again only if the box is moved to a different connection. It is on by
    default because a box with the wrong timezone is broken in a way its owner
    cannot diagnose; it is a setting at all because it is somebody else's box
    and they are entitled to say no.

    Turning it off never changes the timezone the box already has, and the
    owner can always pick one on the dashboard's System page - which is also
    what happens on a box with no internet.
    """

    detect_timezone: bool = True


@dataclass(frozen=True)
class ChannelConfig:
    """A single television channel backed by a folder of episodes."""

    number: int
    name: str
    path: Path
    shuffle: bool = True
    # Episodes to leave out. `exclude` is a list of case-insensitive glob
    # patterns matched against each file's path (and name); `exclude_seasons` is
    # a set of season numbers detected from the path (e.g. S06E01, "Season 6").
    exclude: tuple[str, ...] = ()
    exclude_seasons: frozenset[int] = frozenset()
    # Wall-clock windows that override this channel's name, folder, or put it
    # off the air entirely (see daypart.py). Tested in order; first match wins.
    dayparts: tuple[Daypart, ...] = ()
    # Whether station bumpers play between episodes on this channel. On by
    # default; a news or music channel is often better without them.
    bumpers: bool = True

    def __post_init__(self) -> None:
        if self.number < 0:
            raise ConfigError(f"channel number must be >= 0, got {self.number}")
        if not self.name:
            raise ConfigError(f"channel {self.number} is missing a name")


@dataclass(frozen=True)
class Config:
    """Top-level configuration for the whole nostalgia box."""

    channels: List[ChannelConfig]
    video_extensions: tuple[str, ...] = DEFAULT_VIDEO_EXTENSIONS
    tune_in: str = "random"
    start_channel: Optional[int] = None

    # Presentation / "feel" of the TV.
    force_4_3: bool = False                # if true, letterbox everything to 4:3;
                                          #   default keeps each show's own aspect
    # Start each episode a random number of seconds in (between min and max), so
    # channel switches land at varied points in the show.
    start_offset_min: float = 6.0
    start_offset_max: float = 10.0
    transition_effect: str = "none"       # channel-change effect: none|glitch|static
    transition_duration: float = 0.4      # length of the channel-change effect
    # When there's no transition effect, keep the current show playing this many
    # seconds while the next channel preloads, then cut over (avoids a frozen
    # frame on channel change). 0 = switch immediately.
    bridge_seconds: float = 0.8
    channel_bug_seconds: float = 4.0      # how long the channel banner lingers
    osd_duration: float = 2.0             # how long volume/message overlays linger
    guide_seconds: float = 8.0            # how long the on-screen guide stays up
    ui: UiConfig = field(default_factory=UiConfig)
    crt: CrtConfig = field(default_factory=CrtConfig)
    display_sleep: DisplaySleepConfig = field(default_factory=DisplaySleepConfig)
    web: WebConfig = field(default_factory=WebConfig)
    updates: UpdateConfig = field(default_factory=UpdateConfig)
    time: TimeConfig = field(default_factory=TimeConfig)

    # Audio.
    initial_volume: int = 70              # 0-100
    volume_step: int = 5
    audio_device: Optional[str] = None    # mpv audio device (e.g. HDMI); None = auto
    # Press volume-down once more when already at 0 to cleanly shut the machine
    # down (so it's safe to cut power). The command run to shut down:
    power_off_on_min_volume: bool = True
    power_off_command: tuple[str, ...] = DEFAULT_POWER_OFF_COMMAND
    # Set when the file asked for a power_off_command this box will not run and
    # the default was used instead. The TV ignores it - it has already been
    # given a safe command - but the dashboard reads it to refuse *saving* a
    # config like that, so nobody is quietly left with a setting they did not
    # get. Holds the complaint, ready to show; None when all was well.
    power_off_command_refused: Optional[str] = None

    # Sleep timer. Pressing the sleep button cycles through these durations (in
    # minutes) and then back to off; when the timer runs out the box does
    # `sleep_action`. An empty tuple disables the feature entirely.
    sleep_steps: tuple[int, ...] = (30, 60, 90)
    sleep_action: str = "standby"         # standby | off

    # Station bumpers: short clips (idents, "we'll be right back") played
    # between episodes, the way a late-night cable block stitched itself
    # together. None disables them.
    bumpers_dir: Optional[Path] = None
    bumper_chance: float = 1.0            # 0..1 probability per episode change
    bumper_max_seconds: float = 30.0      # hard cap so a stray long file can't stall

    # Where channel folders live. Kept on the config (not just used during
    # parsing) because auto_channels rescans it at every start-up.
    media_root: Optional[Path] = None
    first_channel_number: int = 2
    # Turn any new top-level folder under media_root into a channel on start-up
    # and remember it in config.yaml. Off by default: with it off, the lineup is
    # exactly what you or `retrobox --setup` put there.
    auto_channels: bool = False

    # Playback.
    scan_recursive: bool = True           # look in sub-folders for episodes
    shuffle_seed: Optional[int] = None    # set for deterministic ordering (tests)

    # Assets (generated by scripts/install.sh via retrobox.static_gen).
    assets_dir: Optional[Path] = None

    # A clip played once at start-up, before the first channel is tuned. Unset
    # by default. A bare filename is looked for in the assets directory, so
    # `boot_splash: boot_splash.mp4` finds the bundled one. A missing file is
    # skipped with a warning rather than being treated as an error.
    boot_splash: Optional[Path] = None

    # Options for the input backends (see input/manager.create_backends).
    input_options: Mapping[str, Any] = field(default_factory=dict)

    # Everything the loader threw away, in the words the customer should see.
    #
    # A value that would become argv, or a folder this box writes inside, is
    # dropped rather than made fatal. That is deliberate and it is the same
    # trade power_off_command already made: this box sits in a living room
    # with no SSH and no way back in, so a config.yaml that refuses to load
    # takes the picture away for good, which is far worse than one setting
    # falling back to its default with a loud line in the journal.
    #
    # The dashboard reads this and refuses to SAVE such a config, so nobody is
    # quietly left running settings they did not choose - the correction is
    # never silent, it is just never fatal either. Empty when all was well.
    refusals: tuple[str, ...] = ()

    def channel_numbers(self) -> List[int]:
        return [c.number for c in self.channels]

    def with_channels(self, channels: List[ChannelConfig]) -> "Config":
        return replace(self, channels=channels)


def _as_path(value: Any, base: Optional[Path]) -> Path:
    p = Path(os.path.expanduser(str(value)))
    if not p.is_absolute() and base is not None:
        p = (base / p)
    return p


def _discover_channels(
    media_root: Path,
    *,
    start_number: int,
    default_shuffle: bool,
    refusals: Optional[List[str]] = None,
) -> List[ChannelConfig]:
    """Turn every immediate sub-folder of ``media_root`` into a channel.

    This is the "just drop folders on the drive" workflow: a folder called
    ``Adult Swim`` becomes a channel named "Adult Swim". Channels
    are numbered sequentially starting at ``start_number`` in alphabetical
    order of the folder name.
    """
    if not media_root.is_dir():
        raise ConfigError(f"media_root does not exist or is not a directory: {media_root}")

    subdirs = sorted(
        (p for p in media_root.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )
    # A sub-folder can be a symlink, and is_dir() above followed it. So even a
    # media root that is somewhere perfectly ordinary can hold a door into
    # /etc, and the folder scanner would have walked straight through it.
    kept = []
    for folder in subdirs:
        reason = location_refusal(folder)
        if reason:
            note = f"the folder '{folder.name}' under media_root leads somewhere it must not: {reason}"
            log.warning("%s - not making it a channel", note)
            if refusals is not None:
                refusals.append(note)
            continue
        kept.append(folder)
    subdirs = kept

    channels: List[ChannelConfig] = []
    for offset, folder in enumerate(subdirs):
        channels.append(
            ChannelConfig(
                number=start_number + offset,
                name=_prettify_name(folder.name),
                path=folder,
                shuffle=default_shuffle,
            )
        )
    return channels


def _prettify_name(folder_name: str) -> str:
    """Turn a folder name like ``late_night`` into ``Late Night``."""
    cleaned = folder_name.replace("_", " ").replace("-", " ").strip()
    cleaned = " ".join(cleaned.split())
    return cleaned.title() if cleaned.islower() else cleaned


def _parse_channels(
    raw: Any,
    base: Optional[Path],
    default_shuffle: bool,
    refusals: Optional[List[str]] = None,
) -> List[ChannelConfig]:
    if not isinstance(raw, list):
        raise ConfigError("'channels' must be a list")
    channels: List[ChannelConfig] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"channel #{i} must be a mapping, got {type(entry).__name__}")
        if "path" not in entry:
            raise ConfigError(f"channel #{i} is missing required key 'path'")
        number = entry.get("number", i + 2)  # old TVs often started around ch. 2
        name = entry.get("name") or _prettify_name(Path(str(entry["path"])).name)
        folder = _as_path(entry["path"], base)

        # A channel folder is where /api/media lists, deletes and overwrites,
        # and where /api/uploads writes. One in the wrong place is dropped and
        # the rest of the lineup carries on: five channels and one refusal is
        # a television, and no channels at all is a service that fails to
        # start and a customer with a black screen.
        reason = location_refusal(folder)
        if reason:
            note = f"channel {number} ('{name}') was left out: {reason}"
            log.warning("%s", note)
            if refusals is not None:
                refusals.append(note)
            continue

        channels.append(
            ChannelConfig(
                number=int(number),
                name=str(name),
                path=folder,
                shuffle=bool(entry.get("shuffle", default_shuffle)),
                exclude=_parse_str_list(entry.get("exclude"), "exclude"),
                exclude_seasons=_parse_seasons(entry.get("exclude_seasons")),
                dayparts=_parse_dayparts(
                    entry.get("dayparts"), base, f"channel {number} ('{name}')",
                    refusals=refusals,
                ),
                bumpers=bool(entry.get("bumpers", True)),
            )
        )
    return channels


def _parse_dayparts(
    raw: Any,
    base: Optional[Path],
    label: str,
    refusals: Optional[List[str]] = None,
) -> tuple[Daypart, ...]:
    """Parse a channel's ``dayparts:`` list into :class:`Daypart` windows."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{label}: 'dayparts' must be a list")

    parts: List[Daypart] = []
    for i, entry in enumerate(raw):
        where = f"{label}: daypart #{i}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a mapping, got {type(entry).__name__}")
        if "from" not in entry or "to" not in entry:
            raise ConfigError(f"{where} needs both 'from' and 'to' (e.g. from: '22:00')")
        try:
            start = parse_clock(entry["from"], field=f"{where} 'from'")
            end = parse_clock(entry["to"], field=f"{where} 'to'")
        except DaypartError as exc:
            raise ConfigError(str(exc)) from exc

        off_air = bool(entry.get("off_air", False))
        path_raw = entry.get("path")
        if off_air and path_raw is not None:
            raise ConfigError(f"{where} cannot set both 'off_air' and 'path'")

        name_raw = entry.get("name")
        folder = _as_path(path_raw, base) if path_raw else None
        # A daypart's folder is played from exactly as a channel's is, so it
        # is held to exactly the same rule - and the whole block goes rather
        # than just its path, because a window that no longer changes anything
        # is more confusing than one that is not there.
        if folder is not None:
            reason = location_refusal(folder)
            if reason:
                note = f"{where} was left out: {reason}"
                log.warning("%s", note)
                if refusals is not None:
                    refusals.append(note)
                continue
        parts.append(
            Daypart(
                start=start,
                end=end,
                name=str(name_raw) if name_raw else None,
                path=folder,
                off_air=off_air,
            )
        )
    return tuple(parts)


def _parse_sleep_steps(raw: Any) -> tuple[int, ...]:
    """Parse the sleep-timer ladder in minutes. ``false``/``[]`` disables it."""
    if raw is None:
        return (30, 60, 90)
    if isinstance(raw, bool):
        return (30, 60, 90) if raw else ()
    if isinstance(raw, (int, float)):
        items: List[Any] = [raw]
    elif isinstance(raw, list):
        items = list(raw)
    else:
        raise ConfigError(
            "'sleep_timer' must be a number, a list of minutes, or false to disable"
        )
    steps: List[int] = []
    for item in items:
        minutes = _clamp_int(item, 1, 24 * 60, "sleep_timer")
        if minutes not in steps:  # de-duplicate so the cycle can't stall
            steps.append(minutes)
    return tuple(steps)


def _parse_str_list(raw: Any, name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(str(x) for x in raw)
    raise ConfigError(f"'{name}' must be a string or a list of strings")


def _parse_seasons(raw: Any) -> frozenset[int]:
    """Parse season numbers from an int, a 'start-end' range, or a list of those."""
    if raw is None:
        return frozenset()
    items = raw if isinstance(raw, list) else [raw]
    seasons: set[int] = set()
    for item in items:
        if isinstance(item, int):
            seasons.add(item)
        elif isinstance(item, str) and "-" in item:
            lo_s, hi_s = item.split("-", 1)
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise ConfigError(f"invalid season range '{item}'") from exc
            seasons.update(range(min(lo, hi), max(lo, hi) + 1))
        else:
            try:
                seasons.add(int(item))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"invalid season number '{item}'") from exc
    return frozenset(seasons)


def config_from_dict(data: Dict[str, Any], *, base_dir: Optional[Path] = None) -> Config:
    """Build a :class:`Config` from an already-parsed mapping.

    ``base_dir`` is used to resolve relative paths (normally the directory the
    config file lives in).
    """
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a mapping")

    default_shuffle = bool(data.get("shuffle", True))

    # Everything this loader would not accept, collected as it goes. See the
    # note on Config.refusals for why these are dropped rather than fatal.
    refusals: List[str] = []

    def refuse(exc: ConfigError, *, instead: str) -> None:
        log.warning("%s - %s instead", exc, instead)
        refusals.append(str(exc))

    try:
        extensions = parse_video_extensions(data.get("video_extensions"))
    except ConfigError as exc:
        refuse(exc, instead="using the usual video formats")
        extensions = DEFAULT_VIDEO_EXTENSIONS

    media_root_raw = data.get("media_root")
    media_root = _as_path(media_root_raw, base_dir) if media_root_raw else None
    if media_root is not None:
        try:
            check_location(media_root, field="media_root")
        except ConfigError as exc:
            refuse(exc, instead="ignoring it")
            media_root = None
    first_channel_number = int(data.get("first_channel_number", 2))

    if "channels" in data:
        channels = _parse_channels(
            data["channels"], media_root or base_dir, default_shuffle, refusals
        )
    elif media_root is not None:
        channels = _discover_channels(
            media_root,
            start_number=first_channel_number,
            default_shuffle=default_shuffle,
            refusals=refusals,
        )
    elif refusals:
        # media_root was the only thing describing a lineup and it named a
        # place this box will not read. There is nothing left to build a
        # television from, so say which value did it rather than leaving
        # somebody to guess from "no channels".
        raise ConfigError(
            f"configuration must define either 'channels' or 'media_root' - "
            f"{refusals[0]}"
        )
    else:
        raise ConfigError("configuration must define either 'channels' or 'media_root'")

    if not channels:
        if refusals:
            raise ConfigError(
                f"no channels found - {refusals[0]}"
            )
        raise ConfigError("no channels found - check 'channels' or the folders under 'media_root'")

    _ensure_unique_numbers(channels)

    tune_in = str(data.get("tune_in", "random")).lower()
    if tune_in not in TUNE_IN_MODES:
        raise ConfigError(f"'tune_in' must be one of {TUNE_IN_MODES}, got '{tune_in}'")

    # assets_dir is a folder this box WRITES into: static_gen puts the filler
    # clips there and /api/branding/splash lands an uploaded video there under
    # a fixed name. Unchecked it is an arbitrary-directory write.
    #
    # The bundled folder is allowed through explicitly, because it lives
    # inside the installed software - which the rule above refuses - and it is
    # where the shipped clips actually are.
    assets_dir_raw = data.get("assets_dir")
    assets_dir = _as_path(assets_dir_raw, base_dir) if assets_dir_raw else None
    if assets_dir is not None:
        from .static_gen import DEFAULT_ASSETS_DIR

        try:
            check_location(assets_dir, field="assets_dir", allow=(DEFAULT_ASSETS_DIR,))
        except ConfigError as exc:
            refuse(exc, instead="using the assets that came with the box")
            assets_dir = None

    # Absent means "yes, the shipped one". `false`, `null` or an empty string
    # mean "no splash at all" - the same spelling `sleep_timer: false` uses.
    splash_raw = data.get("boot_splash", DEFAULT_BOOT_SPLASH)
    boot_splash = (
        None if splash_raw is False or splash_raw is None or splash_raw == ""
        else _as_path(splash_raw, base_dir)
    )
    # The splash becomes an mpv `loadfile`, so what matters is that it names a
    # video and not something else on the disk. Its FOLDER is deliberately not
    # checked the way a library root is: the shipped clip lives inside the
    # installed software, which is exactly where a library may not be, and
    # `boot_splash: boot_splash.mp4` has to keep meaning that file.
    if boot_splash is not None and boot_splash.suffix.lower() not in VIDEO_EXTENSIONS_ALLOWED:
        refuse(
            ConfigError(
                f"'boot_splash' must name a video file, not {boot_splash}"
            ),
            instead="showing no splash",
        )
        boot_splash = None

    start_channel = data.get("start_channel")
    start_channel = int(start_channel) if start_channel is not None else None

    initial_volume = _clamp_int(data.get("initial_volume", 70), 0, 100, "initial_volume")
    volume_step = _clamp_int(data.get("volume_step", 5), 1, 100, "volume_step")
    # An mpv option value rather than an argv element, so this is a much
    # smaller thing than the checks above - but a device name is "alsa/hdmi:
    # CARD=HDMI,DEV=0" and never contains a newline or a control character,
    # and something that does is not a device name.
    audio_device = data.get("audio_device")
    audio_device = str(audio_device) if audio_device else None
    if audio_device and (
        len(audio_device) > 200
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in audio_device)
    ):
        refuse(
            ConfigError("'audio_device' is not the name of an audio output"),
            instead="letting mpv choose",
        )
        audio_device = None

    sleep_steps = _parse_sleep_steps(data.get("sleep_timer"))
    sleep_action = str(data.get("sleep_action", "standby")).strip().lower()
    if sleep_action not in SLEEP_ACTIONS:
        raise ConfigError(
            f"'sleep_action' must be one of {SLEEP_ACTIONS}, got '{sleep_action}'"
        )

    # Every clip in here is played, so it is a folder the box reads inside and
    # gets the same treatment a channel's does.
    bumpers_raw = data.get("bumpers")
    bumpers_dir = _as_path(bumpers_raw, media_root or base_dir) if bumpers_raw else None
    if bumpers_dir is not None:
        try:
            check_location(bumpers_dir, field="bumpers")
        except ConfigError as exc:
            refuse(exc, instead="playing no bumpers")
            bumpers_dir = None

    # Deliberately not fatal, unlike every other bad value in this function.
    # This box sits in somebody's living room with no SSH and no way back in;
    # a config.yaml that refuses to load takes the picture away for good, and
    # that is a far worse outcome than a power button behaving like the
    # default one. So a command that is not on the whitelist is thrown away
    # here - it can never reach subprocess.Popen - the default is used, and
    # the refusal is carried on the Config so the dashboard (which does have
    # somebody looking at it) can refuse to save such a config at all.
    power_off_refused: Optional[str] = None
    try:
        power_off_command = parse_power_off_command(data.get("power_off_command"))
    except ConfigError as exc:
        power_off_refused = str(exc)
        power_off_command = DEFAULT_POWER_OFF_COMMAND
        log.warning("%s - using %s instead", exc, " ".join(power_off_command))
        refusals.append(power_off_refused)

    return Config(
        channels=channels,
        video_extensions=extensions,
        tune_in=tune_in,
        start_channel=start_channel,
        force_4_3=bool(data.get("force_4_3", False)),
        start_offset_min=_offset_range(data)[0],
        start_offset_max=_offset_range(data)[1],
        transition_effect=_valid_transition(data.get("transition", "none")),
        transition_duration=_clamp_float(data.get("transition_duration", 0.4), 0.0, 10.0, "transition_duration"),
        bridge_seconds=_clamp_float(data.get("bridge_seconds", 0.8), 0.0, 10.0, "bridge_seconds"),
        channel_bug_seconds=_clamp_float(data.get("channel_bug_seconds", 4.0), 0.0, 60.0, "channel_bug_seconds"),
        osd_duration=_clamp_float(data.get("osd_duration", 2.0), 0.0, 60.0, "osd_duration"),
        guide_seconds=_clamp_float(data.get("guide_seconds", 8.0), 0.0, 120.0, "guide_seconds"),
        ui=_parse_ui(data.get("ui"), refusals),
        crt=_parse_crt(data.get("crt")),
        display_sleep=_parse_display_sleep(data.get("display_sleep")),
        web=_parse_web(data.get("web")),
        updates=_parse_updates(data.get("updates")),
        time=_parse_time(data.get("time")),
        initial_volume=initial_volume,
        volume_step=volume_step,
        audio_device=audio_device,
        power_off_on_min_volume=bool(data.get("power_off_on_min_volume", True)),
        power_off_command=power_off_command,
        power_off_command_refused=power_off_refused,
        sleep_steps=sleep_steps,
        sleep_action=sleep_action,
        bumpers_dir=bumpers_dir,
        bumper_chance=_clamp_float(data.get("bumper_chance", 1.0), 0.0, 1.0, "bumper_chance"),
        bumper_max_seconds=_clamp_float(
            data.get("bumper_max_seconds", 30.0), 0.5, 300.0, "bumper_max_seconds"
        ),
        scan_recursive=bool(data.get("scan_recursive", True)),
        shuffle_seed=(int(data["shuffle_seed"]) if data.get("shuffle_seed") is not None else None),
        assets_dir=assets_dir,
        boot_splash=boot_splash,
        media_root=media_root,
        first_channel_number=first_channel_number,
        auto_channels=bool(data.get("auto_channels", False)),
        input_options=parse_input_options(data.get("input"), refusals),
        refusals=tuple(refusals),
    )


def load_config(path: os.PathLike | str) -> Config:
    """Load and validate a YAML configuration file."""
    import yaml  # imported lazily so importing the package is cheap

    cfg_path = Path(path).expanduser()
    if not cfg_path.is_file():
        raise ConfigError(f"configuration file not found: {cfg_path}")
    try:
        with cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - passthrough of parser error
        raise ConfigError(f"could not parse YAML in {cfg_path}: {exc}") from exc

    return config_from_dict(data, base_dir=cfg_path.parent)


def _parse_ui(raw: Any, refusals: Optional[List[str]] = None) -> UiConfig:
    if raw is None:
        return UiConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'ui' must be a mapping")
    defaults = UiConfig()
    try:
        font = parse_font(raw.get("font", defaults.font))
    except ConfigError as exc:
        log.warning("%s - using %s instead", exc, defaults.font)
        if refusals is not None:
            refusals.append(str(exc))
        font = defaults.font
    return UiConfig(
        font=font,
        color=_valid_color(raw.get("color", defaults.color), "ui.color"),
        dim_color=_valid_color(raw.get("dim_color", defaults.dim_color), "ui.dim_color"),
        glow=bool(raw.get("glow", defaults.glow)),
    )


def parse_input_options(
    raw: Any, refusals: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Check the ``input:`` block, dropping anything the box will not act on.

    Three of these become part of a real command line and one becomes a file
    the box deletes, so the block gets the same treatment ``power_off_command``
    does: a value that is not on the list is thrown away here, where it can
    never reach ``subprocess.Popen`` or ``unlink``, and the backend falls back
    to its own default. Everything else in the block is a switch, a name
    filter or a key map and is checked where it is used.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("'input' must be a mapping")

    options = dict(raw)
    checkers = {
        "cec_binary": parse_cec_binary,
        "cec_osd_name": parse_cec_osd_name,
        "keyboard_devices": parse_keyboard_devices,
        "web_socket": parse_control_socket,
    }
    for key, check in checkers.items():
        if options.get(key) is None:
            continue
        try:
            options[key] = check(options[key])
        except ConfigError as exc:
            log.warning("%s - leaving input.%s at its default", exc, key)
            if refusals is not None:
                refusals.append(str(exc))
            options.pop(key)
    return options


def _parse_crt(raw: Any) -> CrtConfig:
    if raw is None:
        return CrtConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'crt' must be a mapping")
    d = CrtConfig()
    return CrtConfig(
        enabled=bool(raw.get("enabled", d.enabled)),
        curvature=_clamp_float(raw.get("curvature", d.curvature), 0.0, 0.5, "crt.curvature"),
        corner_radius=_clamp_float(raw.get("corner_radius", d.corner_radius), 0.0, 0.3, "crt.corner_radius"),
        vignette=_clamp_float(raw.get("vignette", d.vignette), 0.0, 1.0, "crt.vignette"),
        scanlines=bool(raw.get("scanlines", d.scanlines)),
        scanline_intensity=_clamp_float(
            raw.get("scanline_intensity", d.scanline_intensity), 0.0, 1.0, "crt.scanline_intensity"
        ),
    )


def _parse_display_sleep(raw: Any) -> DisplaySleepConfig:
    """Read the ``display_sleep:`` block.

    An unknown ``non_broadcast`` word falls back to the default rather than
    refusing to load. The rule this file already follows for anything that
    could take the picture away applies here as much as anywhere: a box in
    somebody's living room with a typo in it must still come up playing.
    """
    if raw is None:
        return DisplaySleepConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'display_sleep' must be a mapping")
    d = DisplaySleepConfig()
    wake = str(raw.get("non_broadcast", d.non_broadcast)).strip().lower()
    if wake not in NON_BROADCAST_WAKE:
        log.warning(
            "display_sleep.non_broadcast must be one of %s, not %r - using %s",
            ", ".join(NON_BROADCAST_WAKE), wake, d.non_broadcast,
        )
        wake = d.non_broadcast
    return DisplaySleepConfig(
        enabled=bool(raw.get("enabled", d.enabled)),
        # An hour is already far past the point of usefulness, and the floor
        # of zero means "act on the first confirmed absence" for somebody who
        # has a television that does not flap.
        sleep_after_seconds=_clamp_float(
            raw.get("sleep_after_seconds", d.sleep_after_seconds),
            0.0, 3600.0, "display_sleep.sleep_after_seconds",
        ),
        non_broadcast=wake,
    )


def _parse_web(raw: Any) -> WebConfig:
    if raw is None:
        return WebConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'web' must be a mapping")
    d = WebConfig()
    return WebConfig(
        max_upload_mb=_clamp_int(
            raw.get("max_upload_mb", d.max_upload_mb), 1, 128 * 1024, "web.max_upload_mb"
        ),
        min_free_mb=_clamp_int(
            raw.get("min_free_mb", d.min_free_mb), 64, 1024 * 1024, "web.min_free_mb"
        ),
        chunk_mb=_clamp_int(raw.get("chunk_mb", d.chunk_mb), 1, 128, "web.chunk_mb"),
        max_files_per_upload=_clamp_int(
            raw.get("max_files_per_upload", d.max_files_per_upload),
            1, 10_000, "web.max_files_per_upload",
        ),
        max_upload_sessions=_clamp_int(
            raw.get("max_upload_sessions", d.max_upload_sessions),
            1, 64, "web.max_upload_sessions",
        ),
        upload_expiry_hours=_clamp_int(
            raw.get("upload_expiry_hours", d.upload_expiry_hours),
            1, 24 * 30, "web.upload_expiry_hours",
        ),
    )


def _parse_updates(raw: Any) -> UpdateConfig:
    if raw is None:
        return UpdateConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'updates' must be a mapping")
    d = UpdateConfig()
    return UpdateConfig(
        check=bool(raw.get("check", d.check)),
        auto_apply=bool(raw.get("auto_apply", d.auto_apply)),
        check_interval_hours=_clamp_int(
            raw.get("check_interval_hours", d.check_interval_hours),
            1, 720, "updates.check_interval_hours",
        ),
    )


def _parse_time(raw: Any) -> TimeConfig:
    """The clock section.

    A value that is not a yes or a no falls back to the default rather than
    refusing to load, the same way ``updates`` and ``web`` treat theirs. This
    box has no SSH and sits in somebody's living room, so a config.yaml that
    will not load takes the television away for good, and "detection stayed
    on" is a far smaller problem than "there is no picture".
    """
    if raw is None:
        return TimeConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'time' must be a mapping")
    d = TimeConfig()
    return TimeConfig(detect_timezone=bool(raw.get("detect_timezone",
                                                   d.detect_timezone)))


def _offset_range(data: Dict[str, Any]) -> tuple[float, float]:
    """Resolve the (min, max) start-offset seconds from the config.

    Accepts ``start_offset`` as a single number or a ``[min, max]`` list, or
    explicit ``start_offset_min`` / ``start_offset_max`` keys.
    """
    if "start_offset_min" in data or "start_offset_max" in data:
        lo = _clamp_float(data.get("start_offset_min", 0.0), 0.0, 3600.0, "start_offset_min")
        hi = _clamp_float(data.get("start_offset_max", lo), 0.0, 3600.0, "start_offset_max")
    else:
        raw = data.get("start_offset", [6.0, 10.0])
        if isinstance(raw, (list, tuple)):
            if not raw:
                raise ConfigError("'start_offset' list cannot be empty")
            lo = _clamp_float(raw[0], 0.0, 3600.0, "start_offset")
            hi = _clamp_float(raw[1] if len(raw) > 1 else raw[0], 0.0, 3600.0, "start_offset")
        else:
            lo = hi = _clamp_float(raw, 0.0, 3600.0, "start_offset")
    return (lo, max(lo, hi))


def _valid_transition(value: Any) -> str:
    s = str(value).strip().lower()
    if s not in TRANSITION_EFFECTS:
        raise ConfigError(f"'transition' must be one of {TRANSITION_EFFECTS}, got '{value}'")
    return s


def _valid_color(value: Any, name: str) -> str:
    """Validate a ``#RRGGBB`` hex colour string."""
    import re

    s = str(value).strip()
    if not re.fullmatch(r"#?[0-9a-fA-F]{6}", s):
        raise ConfigError(f"'{name}' must be a hex colour like '#4DFF5A', got '{value}'")
    return s if s.startswith("#") else f"#{s}"


def _ensure_unique_numbers(channels: List[ChannelConfig]) -> None:
    seen: Dict[int, str] = {}
    for ch in channels:
        if ch.number in seen:
            raise ConfigError(
                f"duplicate channel number {ch.number} used by "
                f"'{seen[ch.number]}' and '{ch.name}'"
            )
        seen[ch.number] = ch.name


def _clamp_int(value: Any, lo: int, hi: int, name: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{name}' must be an integer") from exc
    return max(lo, min(hi, n))


def _clamp_float(value: Any, lo: float, hi: float, name: str) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{name}' must be a number") from exc
    return max(lo, min(hi, n))


__all__ = [
    "Config",
    "ChannelConfig",
    "UiConfig",
    "CrtConfig",
    "DisplaySleepConfig",
    "WebConfig",
    "UpdateConfig",
    "ConfigError",
    "load_config",
    "config_from_dict",
    "check_location",
    "location_refusal",
    "parse_cec_binary",
    "parse_cec_osd_name",
    "parse_control_socket",
    "parse_font",
    "parse_input_options",
    "parse_keyboard_devices",
    "parse_power_off_command",
    "parse_video_extensions",
    "DEFAULT_BOOT_SPLASH",
    "DEFAULT_POWER_OFF_COMMAND",
    "DEFAULT_VIDEO_EXTENSIONS",
    "INSTALL_ROOT",
    "POWER_OFF_COMMANDS",
    "TUNE_IN_MODES",
    "TRANSITION_EFFECTS",
    "SLEEP_ACTIONS",
    "NON_BROADCAST_WAKE",
    "VIDEO_EXTENSIONS_ALLOWED",
]
