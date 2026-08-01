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

That "just probe again" is the rule the whole file leans on. Nothing in the
cache is ever worth defending, so anything the box cannot read back with
confidence - a truncated file, an entry from a version it does not know, a
verdict it cannot be sure of - is dropped rather than guessed at.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# A half-hour slot minus ad breaks is about 22 minutes; used when we cannot probe.
DEFAULT_EPISODE_SECONDS = 22 * 60.0

# Overridable so tests never touch the real user cache.
CACHE_PATH = Path(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
) / "retrobox" / "durations.json"

#: Which shape the file on disk is in.
#:
#: Version 1 was a bare ``{path: [mtime, size, duration, has_video]}`` mapping
#: whose fourth field was written through ``bool()``. That collapsed the three
#: answers below into two: "we could not tell" landed on disk as ``false``,
#: which reads back as the verdict "there is positively no picture" - and since
#: the entry is keyed on the file's size and modification time, that verdict
#: stuck to a perfectly good file for ever. Version 2 writes the three states as
#: ``true`` / ``false`` / ``null`` and says which format it is at the top of the
#: file, so a box meeting a shape it does not recognise can throw it away
#: instead of misreading it.
CACHE_VERSION = 2

# path -> [mtime, size, duration, has_video]; loaded lazily, written by
# flush_cache(). Everything in here has been through _clean_entry(), so the
# readers below can index it without checking it again.
_cache: Optional[Dict[str, list]] = None
_dirty = False


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _load_cache() -> Dict[str, list]:
    global _cache
    if _cache is None:
        try:
            with CACHE_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        _cache = _entries_from_disk(data)
    return _cache


def _entries_from_disk(data) -> Dict[str, list]:
    """Whatever is in the cache file, reduced to entries we actually trust."""
    if not isinstance(data, dict):
        return {}

    version = data.get("version")
    if isinstance(version, int):
        if version != CACHE_VERSION:
            # Written by a build we are not - most likely an update that got
            # rolled back. We do not know what its fields mean, so we ignore
            # the lot and probe again rather than guess at somebody's files.
            return {}
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return {}
        return _clean_entries(entries)

    # No version at the top: the old flat mapping, from a box updating to this
    # build for the first time.
    return _clean_entries(data, legacy=True)


def _clean_entries(entries: dict, *, legacy: bool = False) -> Dict[str, list]:
    cleaned = {}
    for key, entry in entries.items():
        if not isinstance(key, str):
            continue
        usable = _clean_entry(entry, legacy=legacy)
        if usable is not None:
            cleaned[key] = usable
    return cleaned


def _clean_entry(entry, *, legacy: bool) -> Optional[list]:
    """One stored entry as ``[mtime, size, duration, has_video]``, or ``None``.

    ``None`` means "we know nothing about that file", which is a state every
    caller already handles: it probes again. This box is switched off at the
    wall, so a half-written or hand-edited cache file is a matter of when, not
    if, and json.load will happily hand back a string where a number should be.
    A cache entry must never be able to raise out of a channel change.
    """
    if not isinstance(entry, list) or len(entry) < 3:
        return None
    mtime, size, duration = entry[0], entry[1], entry[2]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) for v in (mtime, size, duration)):
        return None
    if duration <= 0:
        # Nothing plays for no time at all, and an episode that never ends
        # would leave a broadcast channel stuck on one show for good.
        return None

    has_video = entry[3] if len(entry) >= 4 else None
    if legacy:
        # The old format wrote bool(has_video), so a stored true could only ever
        # have come from a real video stream and is kept. A stored false might
        # have meant "there is no picture" or it might have meant "we could not
        # tell", and nothing can separate them now - so the entry goes, and the
        # file is looked at once more. One ffprobe run is a cheap price for not
        # refusing somebody's boot splash for ever.
        if has_video is None:
            return [mtime, size, duration, None]
        return [mtime, size, duration, True] if has_video is True else None
    if has_video is True or has_video is False or has_video is None:
        return [mtime, size, duration, has_video]
    return None


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
            json.dump({"version": CACHE_VERSION, "entries": _cache}, fh)
        tmp.replace(CACHE_PATH)  # atomic, so a power cut can't truncate it
        _dirty = False
    except OSError:
        log.debug("could not write the duration cache to %s", CACHE_PATH, exc_info=True)


def reset_cache() -> None:
    """Drop the in-memory cache (used by tests and after CACHE_PATH changes)."""
    global _cache, _dirty
    _cache = None
    _dirty = False


@dataclass(frozen=True)
class MediaInfo:
    """What one ffprobe run could work out about a file.

    ``has_video`` is deliberately three-valued. ``False`` means ffprobe looked
    and there is genuinely no video stream; ``None`` means we could not tell -
    no ffprobe on the box, an unreadable file, output we did not understand.
    Only the first of those is a verdict, which is why :attr:`unplayable` is
    not simply ``not has_video``: a box without ffmpeg installed must not
    condemn every file uploaded to it.
    """

    duration: Optional[float] = None
    has_video: Optional[bool] = None

    @property
    def unplayable(self) -> bool:
        """True only when we positively established there is no picture."""
        return self.has_video is False


def probe_duration(
    path: Path, *, timeout: float = 15.0, use_cache: bool = True
) -> Optional[float]:
    """Return the duration of ``path`` in seconds, or ``None`` on failure."""
    return probe_media(path, timeout=timeout, use_cache=use_cache).duration


def probe_media(
    path: Path, *, timeout: float = 15.0, use_cache: bool = True
) -> MediaInfo:
    """Duration *and* whether the file has a picture, from one ffprobe run.

    Both answers come out of the same call and share the same cache entry: a
    freshly uploaded folder of 200 episodes is 200 processes, and doing it
    twice to ask two questions about the same file would be 400.
    """
    if not ffprobe_available():
        return MediaInfo()

    key = str(path)
    fingerprint = _fingerprint(path) if use_cache else None
    if fingerprint is not None:
        entry = _load_cache().get(key)
        if entry is not None and entry[:2] == fingerprint:
            return MediaInfo(duration=float(entry[2]), has_video=entry[3])

    info = _run_probe(path, timeout)

    # Only successes are cached: caching a failure would make a transient error
    # (or a missing ffprobe) stick around forever. A success with no verdict on
    # the picture is still a success - the duration is worth keeping, and the
    # "we could not tell" goes in as itself rather than as a "no".
    if info.duration is not None and fingerprint is not None:
        global _dirty
        _load_cache()[key] = [
            fingerprint[0], fingerprint[1], info.duration, info.has_video
        ]
        _dirty = True
    return info


def cached_media(path: Path) -> Optional[MediaInfo]:
    """What we already know about ``path``, or ``None``. Never runs ffprobe.

    For callers that are on a timer rather than answering a question - the
    status snapshot the dashboard reads is rewritten every couple of seconds,
    and probing on a miss there would fork a process per tick.
    """
    fingerprint = _fingerprint(path)
    if fingerprint is None:
        return None
    entry = _load_cache().get(str(path))
    if entry is None or entry[:2] != fingerprint:
        return None
    return MediaInfo(duration=float(entry[2]), has_video=entry[3])


def _parse_probe(stdout: str) -> MediaInfo:
    """Pure parsing of ffprobe's JSON, kept apart from running it.

    Anything we do not understand comes back as "do not know" rather than as a
    negative finding.
    """
    try:
        data = json.loads(stdout or "{}")
    except ValueError:
        return MediaInfo()
    if not isinstance(data, dict):
        return MediaInfo()

    duration = None
    raw = data.get("format", {}).get("duration") if isinstance(data.get("format"), dict) else None
    if raw is not None:
        try:
            value = float(raw)
            duration = value if value > 0 else None
        except (TypeError, ValueError):
            duration = None

    streams = data.get("streams")
    if not isinstance(streams, list):
        return MediaInfo(duration=duration, has_video=None)
    has_video = any(
        isinstance(s, dict) and s.get("codec_type") == "video" for s in streams
    )
    return MediaInfo(duration=duration, has_video=has_video)


def _run_probe(path: Path, timeout: float) -> MediaInfo:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return MediaInfo()
        return _parse_probe(result.stdout or "")
    except (subprocess.SubprocessError, ValueError, OSError):
        return MediaInfo()


__all__ = [
    "MediaInfo",
    "cached_media",
    "probe_duration",
    "probe_media",
    "ffprobe_available",
    "flush_cache",
    "reset_cache",
    "CACHE_PATH",
    "DEFAULT_EPISODE_SECONDS",
]
