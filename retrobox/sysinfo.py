"""Answering "is my box alright" without a terminal.

Everything here runs on a machine nobody has ever seen. It may have no
temperature sensor, no separate media drive, no ``lspci``, no network and no
``vainfo``. All of those are normal, so every reading in this module is
best-effort and returns ``None`` rather than raising: a health page that
breaks when something is missing breaks exactly when somebody is trying to
find out what is wrong.

There is a deliberate distinction throughout between **absent** and **bad**.
No thermal sensor is ``None``, never 0 degrees; a disk we could not measure is
``None``, never "full". Reporting an absence as a reading is how a diagnostic
page starts lying to you.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from . import __version__, hwdetect

log = logging.getLogger(__name__)

#: Where Linux exposes thermal zones. Overridable so tests need no sensor.
THERMAL_ROOT = Path("/sys/class/thermal")

# What counts as "running out" differs by volume, so it is not one number.
#
# The root filesystem is small and only has to hold the system: a couple of
# gigabytes is comfortable, and under half a gigabyte is a box that will stop
# booting. The media volume is judged by a different question - "can I still
# put a film on it" - so its floor is roughly one large upload.
#
# Both also warn on a proportion, because a floor alone is wrong at both ends:
# 20 GB free is plenty on a 64 GB disk and nearly empty on a 2 TB one.
ROOT_LOW_BYTES = 2 * 1024**3
ROOT_CRITICAL_BYTES = 512 * 1024**2
MEDIA_LOW_BYTES = 10 * 1024**3
MEDIA_CRITICAL_BYTES = 2 * 1024**3
LOW_SPACE_FRACTION = 0.02

# Kept as the general-purpose names other code may reach for.
LOW_SPACE_BYTES = ROOT_LOW_BYTES
CRITICAL_SPACE_BYTES = ROOT_CRITICAL_BYTES

#: How long any one shelled-out reading may take. A health page that hangs is
#: no more use than one that crashes.
COMMAND_TIMEOUT = 5.0


def _run(cmd: Sequence[str], *, timeout: float = COMMAND_TIMEOUT) -> str:
    """Run a command, returning stdout, or "" for anything that goes wrong."""
    try:
        result = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
        return result.stdout or ""
    except Exception:  # noqa: BLE001 - a missing binary is an ordinary outcome
        log.debug("could not run %s", cmd, exc_info=True)
        return ""


# ==========================================================================
# Storage
# ==========================================================================
def _volume(
    path: Union[str, Path], *, low: int, critical: int
) -> Optional[Dict[str, Any]]:
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return None
    nearly_gone = usage.total and usage.free < usage.total * LOW_SPACE_FRACTION
    if usage.free < critical:
        state = "critical"
    elif usage.free < low or nearly_gone:
        state = "low"
    else:
        state = "ok"
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
        "state": state,
    }


def storage(media_root: Optional[Union[str, Path]]) -> Dict[str, Any]:
    """The root volume and the media volume, kept apart on purpose.

    They are frequently different disks, and averaging them hides the one that
    is about to cause trouble - a media drive with 2 TB free says nothing about
    a root filesystem with 200 MB left, and it is the root one that stops the
    box booting.
    """
    root = _volume("/", low=ROOT_LOW_BYTES, critical=ROOT_CRITICAL_BYTES)
    media = (
        _volume(media_root, low=MEDIA_LOW_BYTES, critical=MEDIA_CRITICAL_BYTES)
        if media_root is not None else None
    )

    if media is not None:
        media["same_disk_as_root"] = _same_device("/", media_root)

    warning = any(
        v is not None and v["state"] in ("low", "critical") for v in (root, media)
    )
    return {"root": root, "media": media, "warning": warning}


def _same_device(a: Union[str, Path], b: Union[str, Path]) -> bool:
    """Are these two paths on the same filesystem? Unknown counts as no."""
    try:
        return os.stat(str(a)).st_dev == os.stat(str(b)).st_dev
    except OSError:
        return False


# ==========================================================================
# Temperature and load
# ==========================================================================
def temperature(
    thermal_root: Optional[Union[str, Path]] = None,
) -> Optional[Dict[str, Any]]:
    """The warmest thermal zone, or None on a machine that has no sensor.

    ``thermal_root`` is injectable, the way ``sensors.py`` and ``display.py``
    take their sysfs roots as parameters, so a test can point this at a
    fixture tree it built itself instead of reading whatever ``/sys`` happens
    to contain on the machine running the suite - a test that reads real
    machine state is not testing anything repeatable, and would pass or fail
    differently on a Mac with no ``/sys`` at all, a Linux CI runner, and the
    box itself.

    The default stays ``None`` rather than binding straight to ``THERMAL_ROOT``
    so that code (and the handful of existing callers) that monkeypatches the
    module-level ``THERMAL_ROOT`` for the no-argument call keeps working: a
    bound default would freeze in the real path at import time and ignore a
    later monkeypatch of the module attribute.
    """
    root = Path(thermal_root) if thermal_root is not None else THERMAL_ROOT
    try:
        zones = sorted(root.glob("thermal_zone*"))
    except OSError:
        return None

    best: Optional[Dict[str, Any]] = None
    for zone in zones:
        try:
            raw = (zone / "temp").read_text().strip()
            celsius = int(raw) / 1000.0
        except (OSError, ValueError):
            continue
        if not -50.0 < celsius < 150.0:
            continue                      # a reading that cannot be real
        try:
            label = (zone / "type").read_text().strip()
        except OSError:
            label = zone.name
        if best is None or celsius > best["celsius"]:
            best = {"celsius": round(celsius, 1), "sensor": label}
    return best


def load() -> Optional[Dict[str, Any]]:
    """Load average alongside the core count, which is what makes it mean anything."""
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):
        return None
    cores = os.cpu_count() or 1
    return {
        "one_minute": round(one, 2),
        "five_minutes": round(five, 2),
        "fifteen_minutes": round(fifteen, 2),
        "cores": cores,
        "busy": one > cores,
    }


def uptime_seconds() -> Optional[float]:
    """How long the machine has been up, from /proc where there is one."""
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            return round(float(fh.read().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return None


# ==========================================================================
# Where the box is
# ==========================================================================
def addresses() -> Dict[str, Any]:
    """Hostname, every address the box answers on, and the URLs to type."""
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "retrobox"
    short = hostname.split(".")[0]

    found = [a for a in _run(["hostname", "-I"]).split() if a and ":" not in a]

    urls = [f"http://{short}.local/"]
    urls += [f"http://{address}/" for address in found]
    return {
        "hostname": short,
        "mdns_name": f"{short}.local",
        "addresses": found,
        "urls": urls,
        "dashboard_urls": [u + "dash" for u in urls],
    }


# ==========================================================================
# Hardware, in words
# ==========================================================================
def hardware() -> Dict[str, Any]:
    """What the box is made of, said plainly rather than dumped."""
    try:
        report = hwdetect.build_report(run_install=False)
    except Exception:  # noqa: BLE001 - no lspci, no aplay, an odd SBC
        log.debug("hardware detection failed", exc_info=True)
        return {
            "gpu_vendor": "unknown",
            "gpu_description": "could not detect the graphics hardware",
            "audio_devices": [],
            "decode": {
                "working": None,
                "summary": "Hardware decode: could not tell on this machine",
            },
        }

    if report.gpu_vendor == "unknown":
        # detect_gpu puts its reason in the description ("lspci not available,
        # install pciutils"). That is useful on its own line and nonsense in
        # the middle of a sentence about decoding.
        working: Optional[bool] = None
        summary = "Hardware decode: could not tell - no graphics detection on this machine"
    elif report.decode_packages:
        # A driver is expected for this GPU, so ask whether it actually works
        # rather than assuming the install took.
        try:
            working = bool(hwdetect.check_vaapi())
        except Exception:  # noqa: BLE001
            working = None
        if working:
            summary = f"Hardware decode: working, {report.gpu_description}"
        elif working is False:
            summary = (
                f"Hardware decode: not active on {report.gpu_description} - "
                f"software decode is being used, which is fine on most files"
            )
        else:
            summary = "Hardware decode: could not tell"
    else:
        # No VA-API driver for this GPU. Not a fault; say so without alarm.
        working = False
        summary = (
            f"Hardware decode: not available for {report.gpu_description}, "
            f"using software decode - which is fine"
        )

    return {
        "gpu_vendor": report.gpu_vendor,
        "gpu_description": report.gpu_description,
        "audio_devices": list(report.audio_devices),
        "decode": {"working": working, "summary": summary},
    }


# ==========================================================================
# The file share and the clock
# ==========================================================================
def share(media_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Whether the Samba share is up, and where it points."""
    active = _run(["systemctl", "is-active", "smbd"]).strip()
    return {
        "running": active == "active",
        "state": active or "unknown",
        "path": str(media_root) if media_root else None,
    }


def timezone() -> Dict[str, Any]:
    """The clock, and whether anything is keeping it honest.

    dayparting changes what a channel *is* by time of day, so a box whose
    clock has drifted produces wrong behaviour that looks like a bug in the
    schedule. A box with no internet has no NTP, which is worth saying out
    loud rather than leaving to be discovered.

    Three things are reported rather than one, because they fail separately
    and mean different things:

    * ``synchronised`` - the box's own opinion. True, False, or **None** for
      "cannot tell", which is not a polite way of saying no.
    * ``last_sync`` / ``last_sync_state`` - *when* it last actually worked.
      "Four minutes ago" and "never" are completely different states and the
      owner has to be able to tell them apart; ``unknown`` is the third honest
      answer and it is kept distinct from ``never`` on purpose, because
      "never synchronised" is what sends somebody off to buy a battery.
    * ``plausible`` - whether the date is one this software could be running
      on at all. A ten-year-old office mini PC with a flat CMOS cell comes up
      in 2011 every time it loses power, and *that* is the fault, not drift.
    """
    info: Dict[str, Any] = {
        "timezone": None,
        "local_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "synchronised": None,
        "ntp_active": None,
    }
    # Read once and hand the same text to timekeeping, rather than shelling
    # out to the same command twice for one page.
    shown = _run(["timedatectl", "show"])
    for line in shown.splitlines():
        key, _, value = line.partition("=")
        if key == "Timezone":
            info["timezone"] = value
        elif key == "NTPSynchronized":
            info["synchronised"] = value == "yes"
        elif key == "NTP":
            info["ntp_active"] = value == "yes"

    if info["timezone"] is None:
        # No timedatectl (a container, a non-systemd box). Fall back to what
        # Python knows rather than showing nothing at all.
        info["timezone"] = time.tzname[0] if time.tzname else None

    # Imported here rather than at the top because timekeeping reaches back
    # into this module for the machine's timezone list, and a health page must
    # not be the thing that discovers an import cycle.
    from . import timekeeping

    try:
        sync = timekeeping.sync_status(reader=lambda: shown)
    except Exception:  # noqa: BLE001 - a health page never breaks on a reading
        log.debug("could not read the time sync detail", exc_info=True)
        sync = {"last_sync": None, "last_sync_state": "unknown", "summary": None,
                "fix": None}
    info["last_sync"] = sync.get("last_sync")
    info["last_sync_state"] = sync.get("last_sync_state", "unknown")
    info["sync_summary"] = sync.get("summary")
    info["sync_fix"] = sync.get("fix")

    info["plausible"] = timekeeping.clock_is_plausible()
    # An implausible date is a louder problem than an unsynchronised one: it
    # is already producing the wrong television, not merely at risk of it.
    info["warning"] = info["synchronised"] is False or not info["plausible"]
    return info


def timezones() -> List[str]:
    """Every zone this machine will accept, for the picker."""
    zones = [z.strip() for z in _run(["timedatectl", "list-timezones"]).splitlines()]
    return [z for z in zones if z]


# ==========================================================================
# The whole picture
# ==========================================================================
def report(media_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Everything the System page shows, with nothing able to raise."""
    return {
        "version": __version__,
        "uptime_seconds": uptime_seconds(),
        "storage": storage(media_root),
        "addresses": addresses(),
        "hardware": hardware(),
        "temperature": temperature(),
        "load": load(),
        "share": share(media_root),
        "timezone": timezone(),
    }


__all__ = [
    "CRITICAL_SPACE_BYTES",
    "LOW_SPACE_BYTES",
    "THERMAL_ROOT",
    "addresses",
    "hardware",
    "load",
    "report",
    "share",
    "storage",
    "temperature",
    "timezone",
    "timezones",
    "uptime_seconds",
]
