"""Configuration loading and validation.

The whole box is described by a single YAML file (see ``config.example.yaml``).
This module turns that file into validated :class:`Config` /
:class:`ChannelConfig` objects and fills in sensible defaults so a minimal
config still produces a working television.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .daypart import Daypart, DaypartError, parse_clock

log = logging.getLogger(__name__)


# Video containers we consider "an episode" when scanning a channel folder.
DEFAULT_VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mp4", ".mkv", ".avi", ".m4v", ".mov", ".webm", ".mpg", ".mpeg", ".ts",
)


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


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

    ``power_off_command`` is the one config value that becomes the argument
    list of a real ``subprocess.Popen`` (see ``app.py``), and the config it
    comes from can be replaced wholesale from a dashboard with no password on
    it. So this is a closed table and the check against it is a lookup - the
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
    web: WebConfig = field(default_factory=WebConfig)
    updates: UpdateConfig = field(default_factory=UpdateConfig)

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


def _parse_channels(raw: Any, base: Optional[Path], default_shuffle: bool) -> List[ChannelConfig]:
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
        channels.append(
            ChannelConfig(
                number=int(number),
                name=str(name),
                path=_as_path(entry["path"], base),
                shuffle=bool(entry.get("shuffle", default_shuffle)),
                exclude=_parse_str_list(entry.get("exclude"), "exclude"),
                exclude_seasons=_parse_seasons(entry.get("exclude_seasons")),
                dayparts=_parse_dayparts(
                    entry.get("dayparts"), base, f"channel {number} ('{name}')"
                ),
                bumpers=bool(entry.get("bumpers", True)),
            )
        )
    return channels


def _parse_dayparts(raw: Any, base: Optional[Path], label: str) -> tuple[Daypart, ...]:
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
        parts.append(
            Daypart(
                start=start,
                end=end,
                name=str(name_raw) if name_raw else None,
                path=_as_path(path_raw, base) if path_raw else None,
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

    exts = data.get("video_extensions")
    if exts is None:
        extensions = DEFAULT_VIDEO_EXTENSIONS
    else:
        if not isinstance(exts, list) or not exts:
            raise ConfigError("'video_extensions' must be a non-empty list")
        extensions = tuple(e if e.startswith(".") else f".{e}" for e in (s.lower() for s in exts))

    media_root_raw = data.get("media_root")
    media_root = _as_path(media_root_raw, base_dir) if media_root_raw else None
    first_channel_number = int(data.get("first_channel_number", 2))

    if "channels" in data:
        channels = _parse_channels(data["channels"], media_root or base_dir, default_shuffle)
    elif media_root is not None:
        channels = _discover_channels(
            media_root,
            start_number=first_channel_number,
            default_shuffle=default_shuffle,
        )
    else:
        raise ConfigError("configuration must define either 'channels' or 'media_root'")

    if not channels:
        raise ConfigError("no channels found - check 'channels' or the folders under 'media_root'")

    _ensure_unique_numbers(channels)

    tune_in = str(data.get("tune_in", "random")).lower()
    if tune_in not in TUNE_IN_MODES:
        raise ConfigError(f"'tune_in' must be one of {TUNE_IN_MODES}, got '{tune_in}'")

    assets_dir_raw = data.get("assets_dir")
    assets_dir = _as_path(assets_dir_raw, base_dir) if assets_dir_raw else None

    # Absent means "yes, the shipped one". `false`, `null` or an empty string
    # mean "no splash at all" - the same spelling `sleep_timer: false` uses.
    splash_raw = data.get("boot_splash", DEFAULT_BOOT_SPLASH)
    boot_splash = (
        None if splash_raw is False or splash_raw is None or splash_raw == ""
        else _as_path(splash_raw, base_dir)
    )

    start_channel = data.get("start_channel")
    start_channel = int(start_channel) if start_channel is not None else None

    initial_volume = _clamp_int(data.get("initial_volume", 70), 0, 100, "initial_volume")
    volume_step = _clamp_int(data.get("volume_step", 5), 1, 100, "volume_step")
    audio_device = data.get("audio_device")
    audio_device = str(audio_device) if audio_device else None

    sleep_steps = _parse_sleep_steps(data.get("sleep_timer"))
    sleep_action = str(data.get("sleep_action", "standby")).strip().lower()
    if sleep_action not in SLEEP_ACTIONS:
        raise ConfigError(
            f"'sleep_action' must be one of {SLEEP_ACTIONS}, got '{sleep_action}'"
        )

    bumpers_raw = data.get("bumpers")
    bumpers_dir = _as_path(bumpers_raw, media_root or base_dir) if bumpers_raw else None

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
        ui=_parse_ui(data.get("ui")),
        crt=_parse_crt(data.get("crt")),
        web=_parse_web(data.get("web")),
        updates=_parse_updates(data.get("updates")),
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
        input_options=dict(data.get("input") or {}),
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


def _parse_ui(raw: Any) -> UiConfig:
    if raw is None:
        return UiConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'ui' must be a mapping")
    defaults = UiConfig()
    return UiConfig(
        font=str(raw.get("font", defaults.font)),
        color=_valid_color(raw.get("color", defaults.color), "ui.color"),
        dim_color=_valid_color(raw.get("dim_color", defaults.dim_color), "ui.dim_color"),
        glow=bool(raw.get("glow", defaults.glow)),
    )


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
    "WebConfig",
    "UpdateConfig",
    "ConfigError",
    "load_config",
    "config_from_dict",
    "parse_power_off_command",
    "DEFAULT_BOOT_SPLASH",
    "DEFAULT_POWER_OFF_COMMAND",
    "DEFAULT_VIDEO_EXTENSIONS",
    "POWER_OFF_COMMANDS",
    "TUNE_IN_MODES",
    "TRANSITION_EFFECTS",
    "SLEEP_ACTIONS",
]
