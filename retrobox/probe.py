"""Best-effort media duration probing via ffprobe, with an on-disk cache.

Only the optional "broadcast" tune-in mode needs to know how long each episode
runs (so it can pretend the channel has been airing continuously). Probing is
done lazily the first time you tune to a channel and is entirely best-effort: if
ffprobe is missing or a file cannot be read, we fall back to an assumed episode
length so the box still works.

Probing a large channel means one ffprobe process per file, which is slow enough
to notice, so successful results are cached to disk keyed by the file's
modification time and size. The cache is pure optimisation: a missing,
unreadable or stale entry just means we probe again, and a read-only disk (see
the read-only-root note in the README) simply means nothing is ever written.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# A half-hour slot minus ad breaks is about 22 minutes; used when we cannot probe.
DEFAULT_EPISODE_SECONDS = 22 * 60.0

# Overridable so tests never touch the real user cache.
CACHE_PATH = Path(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
) / "retrobox" / "durations.json"

# path -> [mtime, size, duration]; loaded lazily, written by flush_cache().
_cache: Optional[Dict[str, List[float]]] = None
_dirty = False


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _load_cache() -> Dict[str, List[float]]:
    global _cache
    if _cache is None:
        try:
            with CACHE_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            _cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _fingerprint(path: Path) -> Optional[List[float]]:
    """(mtime, size) for ``path`` - changes whenever the file is replaced."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return [int(stat.st_mtime), stat.st_size]


def flush_cache() -> None:
    """Persist newly-probed durations. Never raises - the disk may be read-only."""
    global _dirty
    if not _dirty or _cache is None:
        return
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_name(CACHE_PATH.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(_cache, fh)
        tmp.replace(CACHE_PATH)  # atomic, so a power cut can't truncate it
        _dirty = False
    except OSError:
        log.debug("could not write the duration cache to %s", CACHE_PATH, exc_info=True)


def reset_cache() -> None:
    """Drop the in-memory cache (used by tests and after CACHE_PATH changes)."""
    global _cache, _dirty
    _cache = None
    _dirty = False


def probe_duration(
    path: Path, *, timeout: float = 15.0, use_cache: bool = True
) -> Optional[float]:
    """Return the duration of ``path`` in seconds, or ``None`` on failure."""
    if not ffprobe_available():
        return None

    key = str(path)
    fingerprint = _fingerprint(path) if use_cache else None
    if fingerprint is not None:
        entry = _load_cache().get(key)
        if isinstance(entry, list) and len(entry) == 3 and entry[:2] == fingerprint:
            return float(entry[2])

    duration = _run_ffprobe(path, timeout)

    # Only successes are cached: caching a failure would make a transient error
    # (or a missing ffprobe) stick around forever.
    if duration is not None and fingerprint is not None:
        global _dirty
        _load_cache()[key] = [fingerprint[0], fingerprint[1], duration]
        _dirty = True
    return duration


def _run_ffprobe(path: Path, timeout: float) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "{}")
        duration = data.get("format", {}).get("duration")
        if duration is None:
            return None
        value = float(duration)
        return value if value > 0 else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


__all__ = [
    "probe_duration",
    "ffprobe_available",
    "flush_cache",
    "reset_cache",
    "CACHE_PATH",
    "DEFAULT_EPISODE_SECONDS",
]
