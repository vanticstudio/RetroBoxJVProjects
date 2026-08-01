"""The boot splash: replaceable, but never able to strand the box.

The splash is the first thing anybody sees when a Retro Box is switched on, so
it is worth being able to change - and it is the worst possible place for a bad
file to end up. A clip that never reports end-of-file leaves a customer looking
at a black screen wondering what they have broken.

Two things protect against that, and they work at different layers.

**This module refuses a bad clip at the door.** It has to be short, it has to
have a picture in it, and - unusually for this codebase - "could not tell" is a
refusal rather than a shrug. Everywhere else an unprobeable file is the user's
business; here the cost of being wrong is a box that looks broken on every
power-up, and the shipped default is always sitting there as an alternative.

**And the television gives up on it regardless.** ``TVApp`` arms a deadline
from the clock the moment the splash starts, and nothing read out of the file
touches that number. Which means the guarantee does not depend on the checks
here being right: even a clip that gets past them cannot hold the box for more
than ``_SPLASH_TIMEOUT_SECONDS``. That is the property worth having, and it is
tested against an uploaded file rather than against the default.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .probe import MediaInfo, probe_media
from .static_gen import DEFAULT_ASSETS_DIR

log = logging.getLogger(__name__)

#: The clip that ships with the product. Replaceable, never absent.
DEFAULT_SPLASH_NAME = "boot_splash.mp4"

#: Where an uploaded splash lands, next to the generated filler.
CUSTOM_SPLASH_NAME = "custom_splash.mp4"

#: A splash is a station ident, not a programme. Anything past this is a fault
#: as far as somebody waiting for their television is concerned.
MAX_SPLASH_SECONDS = 15.0

#: Sanity bound on the upload itself; a 15-second ident is a few megabytes.
MAX_SPLASH_BYTES = 128 * 1024 * 1024


class BrandingError(Exception):
    """A splash we will not install, with something worth showing a person."""


def default_splash_path(assets_dir: Optional[Path] = None) -> Path:
    return Path(assets_dir or DEFAULT_ASSETS_DIR) / DEFAULT_SPLASH_NAME


def custom_splash_path(assets_dir: Optional[Path] = None) -> Path:
    return Path(assets_dir or DEFAULT_ASSETS_DIR) / CUSTOM_SPLASH_NAME


def check_splash(path: Path, *, max_seconds: float = MAX_SPLASH_SECONDS) -> MediaInfo:
    """Confirm a file is fit to be a boot splash, or say why it is not."""
    clip = Path(path)
    try:
        size = clip.stat().st_size
    except OSError:
        raise BrandingError("that file is not there") from None
    if size == 0:
        raise BrandingError("that file is empty")
    if size > MAX_SPLASH_BYTES:
        raise BrandingError(
            f"that is larger than {MAX_SPLASH_BYTES // (1024 * 1024)} MB - "
            f"a boot splash is a few seconds long"
        )

    info = probe_media(clip)

    if info.duration is None:
        # Deliberately stricter than the media uploader, which keeps a file it
        # cannot read and lets the user decide. A splash that will not play is
        # a black screen on every single boot, and the default is right there.
        raise BrandingError(
            "this box could not read that as a video, so it will not use it as "
            "a boot splash. The JV Projects default is still in place."
        )
    if info.has_video is False:
        raise BrandingError(
            "there is no picture in that file - a boot splash needs one"
        )
    if info.duration > max_seconds:
        raise BrandingError(
            f"that clip is {info.duration:.0f} seconds long. A boot splash has to "
            f"be under {max_seconds:.0f} seconds - anything longer and the "
            f"television looks broken while it plays."
        )
    return info


def describe(config_splash: Optional[Path], assets_dir: Optional[Path] = None) -> dict:
    """What splash is in use, for the page."""
    default = default_splash_path(assets_dir)
    custom = custom_splash_path(assets_dir)

    if config_splash is None:
        return {
            "enabled": False, "kind": "off", "path": None,
            "summary": "No boot splash - the box goes straight to a channel.",
            "custom_available": custom.is_file(),
        }

    resolved = Path(config_splash)
    if not resolved.is_absolute():
        resolved = Path(assets_dir or DEFAULT_ASSETS_DIR) / resolved
    is_custom = resolved.name == CUSTOM_SPLASH_NAME
    return {
        "enabled": True,
        "kind": "custom" if is_custom else "default",
        "path": str(resolved),
        "exists": resolved.is_file(),
        "summary": (
            "Your own clip plays at start-up." if is_custom
            else "The JV Projects clip plays at start-up."
        ),
        "custom_available": custom.is_file(),
        "default_available": default.is_file(),
    }


__all__ = [
    "CUSTOM_SPLASH_NAME",
    "DEFAULT_SPLASH_NAME",
    "MAX_SPLASH_BYTES",
    "MAX_SPLASH_SECONDS",
    "BrandingError",
    "check_splash",
    "custom_splash_path",
    "default_splash_path",
    "describe",
]
