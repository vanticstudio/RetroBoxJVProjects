"""Deciding whether a name from the network is allowed to become a file.

The dashboard has no authentication. That is deliberate and settled - it is the
same trade as the LAN file share, and it is the right one on a home network -
but it means anyone who can reach port 8080 can reach the upload endpoint. The
only thing standing between them and an arbitrary file write is this module,
and an arbitrary file write on this box is a systemd unit, a cron job or an
``authorized_keys``.

So it is written as a whitelist and it refuses by default. Two separate jobs,
both of which have to pass:

* :func:`safe_media_name` decides whether a *name* is allowed to exist at all.
  It never repairs a bad name into a good one - a sanitiser that "cleans"
  ``../../etc/passwd`` into ``etcpasswd`` has already accepted an attack and
  merely changed where it lands.
* :func:`resolve_inside` decides whether the resulting *path* is where we think
  it is, after the filesystem has had its say. That is a separate question,
  because a symlink already sitting in the folder can redirect a perfectly
  innocent-looking filename anywhere on the disk.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence, Union

# Filesystems stop well before this; the limit is here so a pathological name
# cannot be used to probe for one.
MAX_NAME_LENGTH = 200

# How many folders deep an uploaded folder may nest, after its own top-level
# name is dropped. Season folders are two; nobody needs eight.
MAX_UPLOAD_DEPTH = 4


class UnsafePath(ValueError):
    """A name or path from the network that we refuse to act on."""


def safe_media_name(raw: str, *, allowed: Sequence[str]) -> str:
    """Return ``raw`` unchanged if it is a plain video filename, else raise.

    ``allowed`` is the whitelist of extensions - normally
    ``config.video_extensions``. Anything not on it is refused, which is what
    keeps this endpoint from writing a shell script or a unit file.
    """
    if not isinstance(raw, str):
        raise UnsafePath("filename must be text")

    name = raw.strip()
    if not name:
        raise UnsafePath("filename is empty")
    if len(name) > MAX_NAME_LENGTH:
        raise UnsafePath(f"filename is longer than {MAX_NAME_LENGTH} characters")

    # A null byte truncates the name inside any C library underneath us, so
    # "ok.mp4\0.sh" can pass an extension check here and land as "ok.mp4".
    if "\x00" in name:
        raise UnsafePath("filename contains a null byte")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise UnsafePath("filename contains a control character")

    # Every way of spelling "somewhere else". os.sep and os.altsep cover the
    # host; "/" and "\\" are checked outright so a Windows-style path is
    # refused on Linux too rather than becoming a bizarre single filename.
    separators = {"/", "\\", os.sep, os.altsep or "/"}
    if any(sep in name for sep in separators):
        raise UnsafePath("filename must not contain a path separator")
    if ".." in name:
        raise UnsafePath("filename must not contain '..'")
    if name.startswith((".", "~")):
        raise UnsafePath("filename must not start with '.' or '~'")

    # Belt and braces: after all of the above, the basename must be the name we
    # were given. If those ever disagree, something got through.
    if os.path.basename(name) != name:
        raise UnsafePath("filename must be a plain name, not a path")

    suffix = os.path.splitext(name)[1].lower()
    if not suffix or suffix not in {str(e).lower() for e in allowed}:
        raise UnsafePath(f"'{suffix or name}' is not a video file this box accepts")

    return name


def _safe_component(part: str, *, where: str) -> str:
    """One path segment of an uploaded folder, or raise."""
    if not part:
        raise UnsafePath(f"{where} has an empty folder name in it")
    if len(part) > MAX_NAME_LENGTH:
        raise UnsafePath(f"{where} has a folder name that is far too long")
    if "\x00" in part:
        raise UnsafePath(f"{where} contains a null byte")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in part):
        raise UnsafePath(f"{where} contains a control character")
    if part in (".", ".."):
        raise UnsafePath(f"{where} must not contain '.' or '..'")
    if ".." in part:
        raise UnsafePath(f"{where} must not contain '..'")
    if part.startswith((".", "~")):
        raise UnsafePath(f"{where} must not contain a folder starting with '.' or '~'")
    # A drive letter, or an NTFS alternate data stream. Neither means anything
    # here, and both are ways of writing a path that does not look like one.
    if ":" in part:
        raise UnsafePath(f"{where} must not contain ':'")
    return part


def safe_folder_name(raw: str) -> str:
    """A single folder name we may create under the media root, or raise.

    This is the name of a dropped folder, so it is chosen by whoever is on the
    network. It becomes a real directory, which is why it may be one name and
    nothing else - no separators, no climbing, no dots to hide it from the
    channel scanner.
    """
    if not isinstance(raw, str):
        raise UnsafePath("the folder name must be text")
    name = raw.strip()
    if not name:
        raise UnsafePath("the folder needs a name")
    separators = {"/", "\\", os.sep, os.altsep or "/"}
    if any(sep in name for sep in separators):
        raise UnsafePath("a folder name cannot contain a path separator")
    return _safe_component(name, where="the folder name")


def safe_relative_path(raw: str, *, allowed: Sequence[str]) -> str:
    """Validate a browser's ``webkitRelativePath`` into a path we may write.

    The browser hands this over for every file in a dropped folder, and it is
    exactly as much a string from the network as anything else - the fact that
    it arrived via a file picker means nothing.

    The folder's own top-level name is dropped, because the channel folder *is*
    that folder: keeping it would nest ``Disney/Disney/ep1.mp4``. Everything
    below it is kept, so season folders survive, and every component is checked
    individually. Nothing is ever repaired: ``../etc/passwd.mp4`` is refused
    outright rather than quietly turned into ``etc/passwd.mp4``, because a
    sanitiser that rewrites an attack has still accepted one.

    Returns a ``/``-separated relative path. Join it onto the channel folder and
    put the result through :func:`resolve_inside` before writing - this function
    checks the string, that one checks the filesystem.
    """
    if not isinstance(raw, str):
        raise UnsafePath("the file path must be text")
    text = raw.strip()
    if not text:
        raise UnsafePath("the file path is empty")
    if "\x00" in text:
        raise UnsafePath("the file path contains a null byte")

    # Windows pickers send backslashes. Understand them rather than letting a
    # backslash smuggle a separator past a check that only looks for "/".
    normalised = text.replace("\\", "/")
    if normalised.startswith("/"):
        raise UnsafePath("the file path must be relative, not absolute")

    parts = normalised.split("/")
    for part in parts:
        _safe_component(part, where="the file path")

    # Validate first, drop the folder's own name second: the other way round
    # would let a leading ".." be silently eaten.
    if len(parts) > 1:
        parts = parts[1:]

    if len(parts) - 1 > MAX_UPLOAD_DEPTH:
        raise UnsafePath(
            f"that folder is nested more than {MAX_UPLOAD_DEPTH} deep"
        )

    parts[-1] = safe_media_name(parts[-1], allowed=allowed)
    return "/".join(parts)


def resolve_inside(root: Union[str, Path], candidate: Union[str, Path]) -> Path:
    """Resolve ``candidate`` and confirm it really sits under ``root``.

    Resolution follows symlinks on purpose: the question is not what the path
    looks like, it is which file would actually be written. A symlink planted
    in the folder pointing at ``/etc/systemd/system`` looks like an ordinary
    filename right up until you open it.

    ``root`` itself is not a valid answer - you cannot write "the folder".
    """
    root_resolved = Path(root).resolve()
    target = Path(candidate).resolve()

    if target == root_resolved:
        raise UnsafePath("that is the folder itself, not a file in it")
    # Compare resolved parents rather than string prefixes, so /media/show
    # is not treated as containing /media/show-evil.
    if root_resolved not in target.parents:
        raise UnsafePath(f"{target} is outside {root_resolved}")
    return target


__all__ = [
    "MAX_NAME_LENGTH",
    "MAX_UPLOAD_DEPTH",
    "UnsafePath",
    "resolve_inside",
    "safe_media_name",
    "safe_relative_path",
]
