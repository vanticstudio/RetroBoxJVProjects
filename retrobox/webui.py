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
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__ as retrobox_version
from . import (
    branding, journal, netconf, netprobation, schedule, servicectl, static_gen,
    sysinfo, updates,
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
# the same product rather than a bolted-on admin panel.
GREEN = "#4DFF5A"
DIM = "#123B18"

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

# Port 80, so the address a customer types has no port in it. The systemd
# unit grants CAP_NET_BIND_SERVICE so this works without running as root.
DEFAULT_PORT = 80
FALLBACK_PORT = 8080

# Settings the dashboard is allowed to write, and the subset of those that only
# take effect when the box next starts.
SETTABLE = ("audio_device", "initial_volume", "auto_channels", "sleep_timer")
RESTART_REQUIRED = ("auto_channels",)


class ApiError(Exception):
    """A bad request, with the status code to answer it with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


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


def _volume_line(label: str, volume: Optional[Dict[str, Any]]) -> str:
    if volume is None:
        return f"  {label}: could not be read"
    return (
        f"  {label}: {_mb(volume['free_bytes'])} free of "
        f"{_mb(volume['total_bytes'])} ({volume['percent_used']}% used)"
        f"{'  <-- ' + volume['state'].upper() if volume['state'] != 'ok' else ''}"
    )


def _support_bundle(report: Dict[str, Any], entries: Optional[List[Dict]] = None) -> str:
    """The system information and the recent log, as one block to paste.

    This exists so nobody has to be talked through journalctl over the phone.
    It is deliberately plain text: it has to survive being pasted into an
    email, a chat window or a forum post without turning into soup.
    """
    if entries is None:
        entries = journal.read(lines=200).get("entries", [])

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
        "",
        "Storage",
        _volume_line("root ", storage.get("root")),
        _volume_line("media", storage.get("media")),
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
            f"{entry.get('unit', '')}: {entry.get('message', '')}"
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

    @app.errorhandler(ScheduleError)
    def _handle_schedule_error(exc: ScheduleError):
        return jsonify({"ok": False, "error": str(exc)}), 400

    @app.errorhandler(BrandingError)
    def _handle_branding_error(exc: BrandingError):
        return jsonify({"ok": False, "error": str(exc)}), 400

    @app.errorhandler(NetworkError)
    def _handle_network_error(exc: NetworkError):
        return jsonify({"ok": False, "error": str(exc)}), 400

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

    def _saved(config: Config, extra: Optional[Dict[str, Any]] = None, status: int = 200):
        """Answer a successful config change, and nudge the TV to reload it."""
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
        return report

    @app.get("/api/system")
    def api_system():
        return jsonify(_system_report())

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
            raise ApiError(str(exc), 503) from None
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
            _validated_config_commands(checked)
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

    def _validated_config_commands(config: Config) -> None:
        """Nothing that arrives here may name a command for the box to run.

        ``power_off_command`` is the one config value that ends up as the argv
        of a ``subprocess.Popen`` on the television, and it runs the next time
        anybody shuts the box down - this dashboard's own button, the sleep
        timer, or volume-down past zero. This page has no password, so a
        document naming anything other than a plain shutdown is refused
        outright rather than quietly corrected: the loader has already thrown
        the value away (see config.py), and a customer who is told their file
        was refused is better off than one running settings they never chose.
        """
        if config.power_off_command_refused:
            raise ApiError(f"that config will not load: {config.power_off_command_refused}")

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
            raise ApiError(str(exc)) from None
        return jsonify({"ok": True, "timezone": wanted, **sysinfo.timezone()})

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
                "ok": False, "error": str(exc), "progress": state,
            }), status
        return jsonify({"ok": True, "progress": state})

    @app.post("/api/updates/rollback")
    def api_updates_rollback():
        _confirmed()
        updater = _updater()
        try:
            state = updater.rollback_now()
        except UpdateError as exc:
            raise ApiError(str(exc)) from None
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
            "clock": {
                "local_time": clock.get("local_time"),
                "timezone": clock.get("timezone"),
                "synchronised": clock.get("synchronised"),
                "warning": clock.get("warning"),
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
            "crt": {
                "enabled": config.crt.enabled,
                "curvature": config.crt.curvature,
                "scanlines": config.crt.scanlines,
                "scanline_intensity": config.crt.scanline_intensity,
                "vignette": config.crt.vignette,
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
        allowed = {"crt_enabled", "curvature", "scanlines", "scanline_intensity",
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

        crt: Dict[str, Any] = {}
        top: Dict[str, Any] = {}
        if "crt_enabled" in body:
            if not isinstance(body["crt_enabled"], bool):
                raise ApiError("crt_enabled is true or false")
            crt["enabled"] = body["crt_enabled"]
        if "curvature" in body:
            crt["curvature"] = number("curvature", 0.0, 0.5)
        if "scanlines" in body:
            if not isinstance(body["scanlines"], bool):
                raise ApiError("scanlines is true or false")
            crt["scanlines"] = body["scanlines"]
        if "scanline_intensity" in body:
            crt["scanline_intensity"] = number("scanline_intensity", 0.0, 1.0)
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
        return _saved(saved, {
            "restart_required": ["crt"] if crt else [],
            "note": (
                "The CRT picture effect is set up when the television starts, so "
                "a change to it needs a restart." if crt else ""
            ),
        })

    @app.post("/api/branding/preview")
    def api_branding_preview():
        """Put the channel banner up on the actual television for a moment.

        Reuses the remote's own INFO button rather than inventing a second
        way to draw an overlay - so what is previewed is exactly what a
        viewer sees.
        """
        return _dispatch("info")

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
            raise ApiError(f"could not change the hostname: {output[:200]}", 503)
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
<style>
  :root {
    --green:""" + GREEN + """; --dim:""" + DIM + """;
    --bg:#05080a; --line:rgba(77,255,90,.18); --fill:rgba(77,255,90,.04);
    --red:#ff6b5a;
    --mono:"VT323",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; }
  [hidden] { display:none !important; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; padding:1.2rem .9rem 3rem; background:var(--bg); color:var(--green);
         font-family:var(--mono); font-size:19px; line-height:1.5;
         text-shadow:0 0 6px rgba(77,255,90,.4); }
  body::after { content:""; position:fixed; inset:0; pointer-events:none; z-index:50;
    background:repeating-linear-gradient(0deg,rgba(0,0,0,.22) 0 1px,transparent 1px 3px); }
  @media (prefers-contrast: more) { body::after { display:none; } }
  .wrap { max-width:44rem; margin:0 auto; }

  .mark { font-size:.7rem; letter-spacing:.42em; opacity:.5; margin:0 0 .1rem; }
  h1 { font-size:1.5rem; margin:0 0 1rem; letter-spacing:.08em; font-weight:normal; }

  /* The picture goes here. Until there is a stream to put in it this stays
     empty and takes no space; a <video> dropped straight in needs no other
     change to this page. */
  #screen:empty { display:none; }
  #screen { margin:0 0 1.1rem; border:1px solid var(--line); border-radius:3px;
    overflow:hidden; background:#000; aspect-ratio:4/3; }
  #screen video, #screen img { width:100%; height:100%; object-fit:contain;
    display:block; }

  .panel { border:1px solid var(--line); border-radius:3px; padding:1rem;
           margin-bottom:1rem; background:var(--fill); }
  h2 { font-size:.72rem; text-transform:uppercase; letter-spacing:.2em; opacity:.55;
       margin:0 0 .7rem; font-weight:normal; display:flex; gap:.6rem; align-items:center; }
  h2::after { content:""; flex:1; height:1px; background:var(--line); }

  .ch { font-size:2.6rem; letter-spacing:.04em; margin:0; line-height:1.1; }
  .show { font-size:1.15rem; margin:.35rem 0 0; opacity:.9; }
  .meta { opacity:.55; font-size:.85rem; margin:.2rem 0 0; }

  .meter { height:.7rem; border:1px solid var(--line); border-radius:1px; margin-top:.9rem;
    background:repeating-linear-gradient(90deg,var(--dim) 0 6px,transparent 6px 9px); }
  .meter i { display:block; height:100%; background:var(--green);
    box-shadow:0 0 8px rgba(77,255,90,.6);
    -webkit-mask:repeating-linear-gradient(90deg,#000 0 6px,transparent 6px 9px);
    mask:repeating-linear-gradient(90deg,#000 0 6px,transparent 6px 9px); }
  .times { display:flex; justify-content:space-between; font-size:.8rem;
    opacity:.6; margin-top:.35rem; font-variant-numeric:tabular-nums; }

  .row { display:flex; gap:.7rem; align-items:baseline; padding:.4rem .1rem;
    border-bottom:1px solid rgba(77,255,90,.12); }
  .row:last-child { border-bottom:0; }
  .row.on { background:rgba(77,255,90,.14); }
  .led { font-size:1rem; border:1px solid var(--line); border-radius:2px;
    padding:.02rem .45rem; background:rgba(77,255,90,.07); min-width:3rem;
    text-align:center; flex:none; }
  .grow { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap; }
  .tiny { font-size:.75rem; opacity:.5; flex:none; }
  .off { color:var(--red); opacity:.85; }
  .empty { opacity:.45; font-size:.85rem; }

  .foot { display:flex; justify-content:space-between; align-items:center;
    margin-top:1.2rem; font-size:.8rem; }
  a { color:var(--green); text-decoration:none; border:1px solid var(--line);
      border-radius:2px; padding:.6rem 1rem; letter-spacing:.12em;
      display:inline-block; min-height:2.8rem; }
  a:hover, a:focus-visible { background:rgba(77,255,90,.16); outline:none; }
  a:focus-visible { outline:2px solid var(--green); outline-offset:1px; }
  .dim { opacity:.45; letter-spacing:.06em; }
</style></head><body><div class="wrap">

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
<style>
  :root {
    --green:""" + GREEN + """; --dim:""" + DIM + """;
    --bg:#05080a; --line:rgba(77,255,90,.18); --fill:rgba(77,255,90,.04);
    --red:#ff6b5a; --amber:#ffc14d;
    --mono:"VT323",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; }
  /* Every display: rule below would otherwise beat the browser's own
     [hidden] { display:none }, leaving hidden panels on screen. */
  [hidden] { display:none !important; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; padding:1.2rem .9rem 4rem; background:var(--bg); color:var(--green);
         font-family:var(--mono); font-size:19px; line-height:1.5;
         text-shadow:0 0 6px rgba(77,255,90,.4); }
  /* The scanlines the on-screen display has, at a strength you can read through. */
  body::after { content:""; position:fixed; inset:0; pointer-events:none; z-index:50;
    background:repeating-linear-gradient(0deg,rgba(0,0,0,.22) 0 1px,transparent 1px 3px); }
  @media (prefers-contrast: more) { body::after { display:none; } }
  .wrap { max-width:44rem; margin:0 auto; }

  /* -- masthead -------------------------------------------------------- */
  .mark { font-size:.7rem; letter-spacing:.42em; opacity:.5; margin:0 0 .1rem; }
  h1 { font-size:2.1rem; margin:0; letter-spacing:.08em; font-weight:normal; }
  .sub { opacity:.6; margin:.1rem 0 1.1rem; font-size:.9rem; }
  .sub.offline { color:var(--red); opacity:1; text-shadow:0 0 6px rgba(255,107,90,.4); }

  /* -- tabs, as the service menu's top row ------------------------------ */
  nav { display:flex; gap:.4rem; margin-bottom:1.1rem; }
  nav button { flex:1; min-height:3rem; border:1px solid var(--line); background:transparent;
    color:var(--green); font:inherit; font-size:.85rem; letter-spacing:.16em;
    text-shadow:inherit; border-radius:2px; cursor:pointer; }
  nav button[aria-selected="true"] { background:rgba(77,255,90,.16); border-color:var(--green); }

  /* -- panels ----------------------------------------------------------- */
  .panel { border:1px solid var(--line); border-radius:3px; padding:.9rem;
           margin-bottom:1rem; background:var(--fill); }
  h2 { font-size:.72rem; text-transform:uppercase; letter-spacing:.2em; opacity:.55;
       margin:0 0 .7rem; font-weight:normal; display:flex; gap:.6rem; align-items:center; }
  h2::after { content:""; flex:1; height:1px; background:var(--line); }
  .now { font-size:1.55rem; margin:0 0 .2rem; }
  .meta { opacity:.6; font-size:.85rem; margin:0; }

  /* -- the LED channel number, straight off the front panel -------------- */
  .led { font-size:1.15rem; letter-spacing:.06em; color:var(--green);
    border:1px solid var(--line); border-radius:2px; padding:.05rem .45rem;
    background:rgba(77,255,90,.07); min-width:3.1rem; text-align:center; flex:none; }

  /* -- rows ------------------------------------------------------------- */
  .row { display:flex; gap:.7rem; align-items:center; width:100%; min-height:3.1rem;
    padding:.35rem .2rem; border:0; border-bottom:1px solid rgba(77,255,90,.12);
    background:transparent; color:inherit; font:inherit; text-shadow:inherit;
    text-align:left; cursor:pointer; }
  .row:last-child { border-bottom:0; }
  .row:hover, .row:focus-visible { background:rgba(77,255,90,.12); outline:none; }
  .row.on { background:rgba(77,255,90,.2); }
  .grow { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .tiny { font-size:.75rem; opacity:.5; }

  /* -- controls --------------------------------------------------------- */
  button, select, input { font:inherit; color:var(--green); background:transparent;
    border:1px solid var(--line); border-radius:2px; text-shadow:inherit; }
  button { min-height:3rem; padding:0 .9rem; cursor:pointer; letter-spacing:.08em; }
  button:hover, button:focus-visible { background:rgba(77,255,90,.16); outline:none; }
  button:focus-visible, .row:focus-visible { outline:2px solid var(--green); outline-offset:1px; }
  button[disabled] { opacity:.35; cursor:not-allowed; }
  .bar { display:flex; gap:.45rem; flex-wrap:wrap; }
  .bar button { flex:1 1 auto; min-width:5.2rem; }
  .ghost { min-height:2.4rem; font-size:.75rem; letter-spacing:.14em; padding:0 .7rem; }
  .danger { border-color:var(--red); color:var(--red); text-shadow:0 0 6px rgba(255,107,90,.35); }
  .danger:hover, .danger:focus-visible { background:rgba(255,107,90,.14); }
  .danger.armed { background:rgba(255,107,90,.22); }
  input, select { width:100%; min-height:3rem; padding:0 .6rem; }
  input::placeholder { color:var(--green); opacity:.3; }
  label { display:block; font-size:.72rem; letter-spacing:.16em; text-transform:uppercase;
    opacity:.55; margin:.8rem 0 .25rem; }
  .field { margin-bottom:.2rem; }

  /* -- the editor ------------------------------------------------------- */
  .edit { border:1px solid var(--line); border-radius:2px; padding:.75rem;
    margin:.4rem 0 .8rem; background:rgba(77,255,90,.03); }
  .split { display:flex; gap:.5rem; }
  .split .field { flex:1; }
  .split .field.narrow { flex:0 0 6.5rem; }

  /* -- upload meter, drawn like the volume bars on the TV ---------------- */
  .meter { height:.85rem; border:1px solid var(--line); border-radius:1px; flex:1;
    background:repeating-linear-gradient(90deg,var(--dim) 0 6px,transparent 6px 9px); }
  .meter i { display:block; height:100%; background:var(--green);
    box-shadow:0 0 8px rgba(77,255,90,.6);
    -webkit-mask:repeating-linear-gradient(90deg,#000 0 6px,transparent 6px 9px);
    mask:repeating-linear-gradient(90deg,#000 0 6px,transparent 6px 9px); }
  .progress { display:flex; gap:.6rem; align-items:center; margin-top:.5rem; }
  .progress span { font-variant-numeric:tabular-nums; min-width:3.4rem; text-align:right; }

  .note { font-size:.8rem; opacity:.6; margin:.5rem 0 0; }
  .note.warn { color:var(--amber); opacity:.9; }
  .note.bad { color:var(--red); opacity:.95; }
  .empty { opacity:.45; font-size:.85rem; padding:.5rem .2rem; }

  /* -- the drop zone ---------------------------------------------------- */
  .drop { border:1px dashed var(--line); border-radius:3px; padding:1.4rem 1rem;
    text-align:center; transition:background .15s, border-color .15s; }
  .drop.over { border-color:var(--green); border-style:solid;
    background:rgba(77,255,90,.12); }
  .dropline { margin:0 0 .3rem; letter-spacing:.12em; font-size:.95rem; }
  @media (prefers-reduced-motion: reduce) { .drop { transition:none; } }

  /* -- the upload queue -------------------------------------------------- */
  .job { display:flex; gap:.6rem; align-items:center; padding:.4rem .2rem;
    border-bottom:1px solid rgba(77,255,90,.12); }
  .job:last-child { border-bottom:0; }
  .job .grow { font-size:.9rem; }
  .state { font-size:.7rem; letter-spacing:.12em; text-transform:uppercase;
    opacity:.6; min-width:5.4rem; text-align:right; flex:none; }
  .state.done { color:var(--green); opacity:1; }
  .state.failed { color:var(--red); opacity:1; }
  .state.warn { color:var(--amber); opacity:1; }
  .totals { display:flex; justify-content:space-between; font-size:.8rem;
    opacity:.65; margin-top:.4rem; }

  /* -- system ------------------------------------------------------------ */
  .fact { display:flex; gap:.7rem; align-items:baseline; padding:.32rem .1rem;
    border-bottom:1px solid rgba(77,255,90,.1); font-size:.92rem; }
  .fact:last-child { border-bottom:0; }
  .fact .key { opacity:.5; min-width:9.5rem; flex:none; font-size:.78rem;
    text-transform:uppercase; letter-spacing:.1em; }
  .fact .val { flex:1; min-width:0; word-break:break-word; }
  .fact .val.bad { color:var(--red); }
  .fact .val.warn { color:var(--amber); }
  .raw { background:#040a05; border:1px solid var(--line); border-radius:2px;
    padding:.7rem; font-size:.75rem; line-height:1.45; max-height:26rem;
    overflow:auto; white-space:pre-wrap; word-break:break-word; margin:.7rem 0 0; }
  .press { display:flex; gap:.7rem; align-items:baseline; padding:.3rem .1rem;
    border-bottom:1px solid rgba(77,255,90,.1); }
  .press:last-child { border-bottom:0; }
  .press .who { font-size:.7rem; opacity:.5; min-width:5.5rem; flex:none;
    text-transform:uppercase; letter-spacing:.1em; }
  .press .what { flex:1; letter-spacing:.06em; }
  .press.fresh { background:rgba(77,255,90,.22); }

  /* -- updates ----------------------------------------------------------- */
  .notes { border:1px solid var(--line); border-radius:2px; padding:.2rem .9rem;
    margin:.6rem 0; background:rgba(77,255,90,.03); max-height:22rem;
    overflow:auto; }
  .notes h3, .notes h4, .notes h5, .notes h6 { font-size:.8rem; margin:.9rem 0 .3rem;
    letter-spacing:.12em; text-transform:uppercase; opacity:.65; font-weight:normal; }
  .notes ul { margin:.2rem 0 .7rem; padding-left:1.1rem; }
  .notes li { margin:.15rem 0; font-size:.9rem; }
  .notes p { font-size:.9rem; margin:.4rem 0; }
  .notes code { background:rgba(77,255,90,.12); padding:0 .25rem; border-radius:2px; }
  .rel { border-bottom:1px solid var(--line); }
  .rel:last-child { border-bottom:0; }
  .rel h3:first-child { margin-top:.5rem; }
  .relhead { display:flex; gap:.7rem; align-items:baseline; margin:.8rem 0 0; }
  .relhead .v { font-size:1.15rem; letter-spacing:.04em; }
  .relhead .d { font-size:.75rem; opacity:.5; }
  .stages { display:flex; flex-wrap:wrap; gap:.35rem; margin:.6rem 0; }
  .stage { font-size:.66rem; letter-spacing:.1em; text-transform:uppercase;
    border:1px solid var(--line); border-radius:2px; padding:.25rem .5rem; opacity:.35; }
  .stage.on { opacity:1; background:rgba(77,255,90,.2); border-color:var(--green); }
  .stage.done { opacity:.7; }

  /* -- network ----------------------------------------------------------- */
  .probation { border:1px solid var(--amber); border-radius:3px; padding:.9rem;
    margin-bottom:1rem; background:rgba(255,193,77,.08); color:var(--amber); }
  .probation .count { font-size:2rem; letter-spacing:.04em; }
  .netlist .row { cursor:pointer; }
  .bars { display:inline-flex; gap:2px; align-items:flex-end; height:.9rem;
    flex:none; }
  .bars i { width:3px; background:var(--green); opacity:.25; }
  .bars i.lit { opacity:1; }

  /* -- the day, laid out -------------------------------------------------- */
  .day { display:flex; height:2.6rem; border:1px solid var(--line); border-radius:2px;
    overflow:hidden; margin:.8rem 0 .2rem; }
  .day .seg { display:flex; align-items:center; justify-content:center;
    font-size:.62rem; letter-spacing:.06em; overflow:hidden; white-space:nowrap;
    border-right:1px solid rgba(77,255,90,.18); padding:0 .1rem; }
  .day .seg:last-child { border-right:0; }
  .day .seg.block { background:rgba(77,255,90,.24); }
  .day .seg.gap { background:rgba(77,255,90,.03); opacity:.4; }
  .day .seg.off { background:rgba(255,107,90,.22); color:var(--red); }
  .day .seg.on { outline:1px solid var(--green); outline-offset:-1px; }
  .hours { display:flex; font-size:.6rem; opacity:.4; margin-bottom:.9rem; }
  .hours span { flex:1; text-align:left; }
  .blockrow { display:flex; gap:.4rem; align-items:center; margin:.35rem 0; }
  .blockrow input { min-height:2.6rem; }
  .blockrow .t { flex:0 0 6.5rem; }
  .blockrow .n { flex:1; }

  /* -- toast ------------------------------------------------------------ */
  #toast { position:fixed; left:50%; bottom:1rem; transform:translateX(-50%);
    max-width:calc(100% - 2rem); border:1px solid var(--green); border-radius:2px;
    background:#071109; padding:.6rem 1rem; z-index:60; font-size:.85rem;
    opacity:0; transition:opacity .18s; pointer-events:none; }
  #toast.show { opacity:1; }
  #toast.bad { border-color:var(--red); color:var(--red); }
  @media (prefers-reduced-motion: reduce) { #toast { transition:none; } }
</style></head><body><div class="wrap">

  <header>
    <p class="mark">JV PROJECTS</p>
    <h1>RETRO BOX</h1>
    <p class="sub" id="sub">connecting&hellip;</p>
  </header>

  <nav role="tablist">
    <button role="tab" aria-selected="true" data-tab="watch">WATCH</button>
    <button role="tab" aria-selected="false" data-tab="channels">CHANNELS</button>
    <button role="tab" aria-selected="false" data-tab="add">ADD</button>
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
      <div id="clock"><p class="empty">&hellip;</p></div>
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
  if (!res.ok) throw new Error(data.error || ('request failed: ' + res.status));
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
const TABS = ['watch', 'channels', 'add', 'tv', 'settings', 'system'];
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
      bits.push(s.hwdec ? ('hw decode: ' + s.hwdec) : 'software decode');
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
   System: is my box alright, what is it saying, and the buttons that
   only belong here.
   ======================================================================== */
const Sys = {report: null, follow: null, lastPress: 0};

const fact = (key, value, tone) => {
  const row = el('div', 'fact');
  row.append(el('span', 'key', key), el('span', 'val' + (tone ? ' ' + tone : ''), value));
  return row;
};

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

  const hw = s.hardware || {};
  host.append(fact('Picture', (hw.decode || {}).summary || 'unknown',
    (hw.decode || {}).working === false ? 'warn' : ''));
  host.append(fact('Sound', (hw.audio_devices || []).join(', ') || 'no HDMI audio found'));
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
    } catch (e) { toast(e.message, true); }
  };
  const field = el('div', 'field');
  field.append(el('label', null, 'Timezone'), picker);
  host.append(field);
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
  if (c.warning) {
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
  const curve = el('input');
  curve.type = 'range'; curve.min = 0; curve.max = 50; curve.step = 1;
  curve.value = String(Math.round((crt.curvature || 0) * 100));

  const banner = el('input');
  banner.type = 'number'; banner.min = 0; banner.max = 60; banner.step = 1;
  banner.value = String(osd.channel_bug_seconds || 4);

  for (const [label, input] of [['CRT picture effect', enabled],
                                ['How curved', curve],
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
        channel_bug_seconds: Number(banner.value),
      });
      toast(body.note || 'Saved.');
      loadBranding();
    } catch (e) { toast(e.message, true); }
  };
  const preview = el('button', 'ghost', 'SHOW IT ON THE TV');
  preview.onclick = async () => {
    try {
      await api('/api/branding/preview', {method: 'POST'});
      toast('Look at the television.');
    } catch (e) { toast('The TV is not running', true); }
  };
  const bar = el('div', 'bar');
  bar.style.marginTop = '.8rem';
  bar.append(save, preview);
  host.append(bar);
  host.append(el('p', 'note',
    'The CRT effect is set up when the television starts, so a change to it ' +
    'needs a restart before you see it.'));
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
    if (!res.ok) { toast(data.error || 'That was refused', true); return; }
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
    } catch (e) { toast(e.message, true); }
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
