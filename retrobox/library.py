"""Browsing, deleting and renaming the media library, from an open dashboard.

Everything in here is asked for by somebody the box has not identified and
cannot identify - there is no password on the dashboard, deliberately, and an
attacker on the LAN can call any route with any body. So every path that
arrives from a request goes through :mod:`retrobox.safepath` before it becomes
a real path, and nothing here ever repairs a bad name into a good one. An
arbitrary file write or delete on this box is a systemd unit, and a systemd
unit is a file like any other.

Three ideas hold the module up.

**A listing is cheap or it is not a listing.** A channel can be six hundred
episodes and the box is a two-core Celeron. So :func:`browse` never runs
ffprobe - it shows the durations the probe cache happens to know already and
leaves the rest blank - and it never builds six hundred rich rows to hand back
twenty. It reads names, sorts those, slices the page, and only then asks the
filesystem and the cache about the twenty rows it is going to return.

**Deleting never unlinks.** It moves the file into ``.retrobox-trash`` inside
the media root. Inside the media root because that makes the move a rename:
instant, whatever the file's size, and no window in which half a series exists
in two places. Under a dotted name because channel discovery already skips
dot-folders, and because the customer who deletes the wrong series at nine on a
Sunday night has nobody to ring.

**A rename is the dangerous one.** ``config.yaml`` holds channel paths, so
renaming a folder underneath a channel orphans it - the box comes up with a
channel pointing at nothing and not one thing on the screen connects that to
the rename that caused it. So the folder and the config move together, through
:meth:`ConfigStore.update`, and if the config write fails the folder is put
back. If the folder cannot be put back either, the caller is told exactly what
state the box is in, in words worth reading, because a silent half-rename is
the worst outcome available here.

What this module does NOT do is decide anything. It has no idea what an HTTP
status code is. The routes ask it questions and it answers them or raises;
:class:`LibraryError` and its subclasses are the vocabulary.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import configstore, probe
from .config import Config, _as_path
from .safepath import (
    UnsafePath,
    resolve_inside,
    safe_folder_name,
    safe_media_name,
)
from .uploads import SPOOL_NAME, UploadSession, UploadStore, spool_for

log = logging.getLogger(__name__)

#: Where deleted files wait. Dotted, and inside the media root: dotted so
#: auto-discovery walks past it, inside so the delete is a rename.
TRASH_NAME = ".retrobox-trash"

#: How long the trash keeps something before reclaiming it on its own. About a
#: fortnight - long enough that "I deleted it last weekend" still has an
#: answer, short enough that a box does not quietly fill up with the past.
#: Callers may override it; if a ``web.trash_days`` setting is ever added to
#: config.yaml, this is the default it should carry.
DEFAULT_TRASH_DAYS = 14

#: Rows per page. The number exists because a page is rendered on a phone.
DEFAULT_PAGE_SIZE = 60
MAX_PAGE_SIZE = 500

#: A trash token is a path component, so it is matched against a pattern, not
#: cleaned up - the same rule an upload session id is held to.
TOKEN = re.compile(r"\A[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\Z")

#: The installer's boot-splash folder. Dotted on purpose, and a real channel
#: in the shipped config - so it is machinery from the library's point of view
#: without being machinery from the television's.
WELCOME_NAME = ".welcome"

#: Patterns that hide this module's machinery from ``scan_episodes``.
#:
#: ``scan_episodes`` skips hidden *files*, not hidden *folders*, so a channel
#: whose path is the media root itself - nothing creates one, but a person can
#: write one by hand - would see straight into the trash and put deleted
#: episodes back on the air. Passing these as that channel's ``exclude`` closes
#: it. tests/test_library.py proves both halves of that sentence.
#:
#: Harmless on any other channel: the patterns are matched against the path
#: relative to the channel's own folder, so the ``.welcome`` channel does not
#: exclude itself.
MACHINERY_GLOBS: Tuple[str, ...] = (
    f"{TRASH_NAME}/*",
    f"{SPOOL_NAME}/*",
    f"{WELCOME_NAME}/*",
)

_META = "item.json"
_PAYLOAD = "payload"

#: One lock per media root, shared by everything that changes it.
#:
#: Flask serves on threads and the dashboard has no password, so "two people
#: on two phones" is the ordinary case rather than the exotic one. Every
#: mutation here is a look-then-leap - is the new name free, is the
#: destination clear - and without this the two halves of that interleave: two
#: renames both find the name free, or a restore lands on a file that arrived
#: between the check and the move. Keyed on the real path so two callers who
#: built their own Path objects still meet on the same lock.
_LOCKS: Dict[str, Any] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(media_root: Path) -> Any:
    key = os.path.realpath(str(media_root))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            # Re-entrant: a restore that has to stash the occupant first calls
            # back into a function that takes this same lock.
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


#: What each piece of machinery is, in words fit to put on a screen next to it.
_SYSTEM_NOTES = {
    TRASH_NAME: "deleted files, kept for a while in case you want them back",
    SPOOL_NAME: "uploads still arriving",
    WELCOME_NAME: "the clip this box plays when it starts up",
}


# ==========================================================================
# what the routes catch
# ==========================================================================
class LibraryError(Exception):
    """Something we refuse, with a message fit to show a customer.

    ``catastrophic`` is false for every ordinary refusal: nothing happened,
    the customer is being told why, and a route may answer with a 4xx and the
    message. Only :class:`HalfRenamed` sets it, and it is an attribute rather
    than a matter of which ``except`` clause comes first because getting that
    ordering wrong would report the worst state this module can produce as a
    bad request.
    """

    catastrophic = False


class LibraryNotFound(LibraryError):
    """A file or folder that is not there."""


class LibraryConflict(LibraryError):
    """Something is already at the destination; we will not overwrite it."""


class LibraryBusy(LibraryError):
    """An upload is running into this folder right now."""


class HalfRenamed(LibraryError):
    """The folder moved, the config did not, and it would not move back.

    The one state this module can leave the box in that a person has to be
    told about in full. Its message names the old folder, the new folder and
    every channel now pointing at nothing, because nothing else on the box
    will connect the symptom to the cause.
    """

    catastrophic = True


class _NothingReferenced(Exception):
    """Internal: abort a :meth:`ConfigStore.update` that would change nothing.

    ``update`` rewrites the whole document through the YAML dumper, which
    throws away every comment in it. Somebody renaming a folder no channel
    depends on has not asked for that, so the mutator raises this instead and
    the file on disk is never touched.
    """


# ==========================================================================
# paths from the network
# ==========================================================================
def trash_dir(media_root: Path) -> Path:
    """Where deleted things wait, for a given media library."""
    return Path(media_root) / TRASH_NAME


def upload_spool(media_root: Path) -> Path:
    """Where in-progress uploads live. Re-exported so routes import one module."""
    return spool_for(Path(media_root))


def _machinery(media_root: Path) -> Tuple[Path, ...]:
    return (trash_dir(media_root).resolve(), upload_spool(media_root).resolve())


def _is_machinery(media_root: Path, target: Path) -> bool:
    """Is this resolved path the trash or the spool, or inside one of them?

    The name checks above already refuse a dotted component, so this only
    fires for a symlink pointing back at the machinery under an innocent name.
    Cheap, and the alternative is somebody restoring their own trash into a
    channel by way of a link they left on the file share.
    """
    for special in _machinery(media_root):
        if target == special or special in target.parents:
            return True
    return False


def _parts(relative: Any) -> List[str]:
    """Split a relative path from a request into validated folder components.

    Every component goes through :func:`safe_folder_name`, which is the same
    rule an uploaded folder is held to. Nothing is repaired: ``../etc`` is
    refused rather than quietly becoming ``etc``.
    """
    text = _text(relative)
    if not text:
        return []
    return [safe_folder_name(part) for part in text.split("/")]


def _text(relative: Any) -> str:
    """A relative path as text, refused rather than repaired.

    An absolute path is refused outright and never stripped down to a
    relative one: turning ``/etc`` into ``etc`` accepts the attack and merely
    changes where it lands. A trailing slash is refused for the same reason -
    it produces an empty component, and this module never has to guess.
    """
    if relative is None:
        return ""
    if not isinstance(relative, str):
        raise UnsafePath("that is not a path")
    text = relative.strip()
    if text.startswith("/") or text.startswith("\\"):
        raise UnsafePath("that path must be inside the library, not an absolute one")
    return text


def _last_component(name: str, *, allowed: Sequence[str]) -> str:
    """The final component of a path, which may name a folder or a file.

    Held to whichever of the two rules it can satisfy, and both refuse every
    way of spelling "somewhere else". Which one it actually had to satisfy is
    settled afterwards, once the filesystem has said what is really there -
    see :func:`_resolve`.
    """
    try:
        return safe_folder_name(name)
    except UnsafePath:
        # A file may hold a ':' where a folder may not, so a media name gets
        # its own go. If that refuses it too, the refusal stands.
        return safe_media_name(name, allowed=allowed)


def _resolve_folder(media_root: Path, relative: Any) -> Path:
    """A folder under the media root, named by a request. ``""`` is the root."""
    root = Path(media_root).resolve()
    parts = _parts(relative)
    if not parts:
        return root
    target = resolve_inside(root, root.joinpath(*parts))
    if _is_machinery(root, target):
        raise UnsafePath("that folder belongs to the box, not to the library")
    return target


def _resolve(media_root: Path, relative: Any, *, allowed: Sequence[str]) -> Path:
    """A file or folder under the media root, named by a request.

    The last component is checked twice: once as a string, before it is ever
    joined onto anything, and once against what the filesystem says it is. A
    real file has to be a video file this box accepts, which is what stops
    this route becoming a way to delete a unit file that happens to be sitting
    under the media root.
    """
    root = Path(media_root).resolve()
    text = _text(relative)
    if not text:
        raise LibraryError("nothing was named")

    head, _, tail = text.rpartition("/")
    parts = _parts(head)
    parts.append(_last_component(tail, allowed=allowed))

    target = resolve_inside(root, root.joinpath(*parts))
    if _is_machinery(root, target):
        raise UnsafePath("that belongs to the box, not to the library")

    if target.is_dir():
        safe_folder_name(target.name)
    elif target.exists():
        safe_media_name(target.name, allowed=allowed)
    else:
        raise LibraryNotFound(f"there is no '{tail}' here any more")
    return target


def _relative(media_root: Path, target: Path) -> str:
    """A path back into the ``/``-separated form the dashboard speaks."""
    root = Path(media_root).resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - callers resolve_inside first
        return target.name


# ==========================================================================
# 1. browsing
# ==========================================================================
def _system_note(name: str, is_dir: bool) -> Optional[str]:
    """Why this entry is the box's own machinery, or ``None`` if it is not."""
    if not name.startswith("."):
        return None
    return _SYSTEM_NOTES.get(name) or (
        "hidden - the box's own housekeeping, not part of the library"
    )


def _page_bounds(total: int, page: Any, per_page: Any) -> Tuple[int, int, int, int]:
    try:
        size = int(per_page)
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    size = max(1, min(MAX_PAGE_SIZE, size))
    pages = max(1, -(-total // size))
    try:
        number = int(page)
    except (TypeError, ValueError):
        number = 1
    number = max(1, min(pages, number))
    return number, size, pages, (number - 1) * size


def browse(
    media_root: Path,
    relative: str = "",
    *,
    allowed: Sequence[str],
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
    sort: str = "name",
    order: str = "asc",
) -> Dict[str, Any]:
    """One page of a folder: names, sizes, dates, and durations we already knew.

    Deliberately in two passes. The first reads the directory and keeps only
    what it takes to sort - a name, whether it is a folder, and for a sort by
    size or date the one number that sort needs. The second pass runs over the
    twenty rows that are actually being returned and asks the filesystem and
    the duration cache about those. A channel with six hundred episodes is
    six hundred cheap tuples and twenty answers, not six hundred answers.

    Durations come from :func:`probe.cached_media`, which never shells out. A
    file nobody has tuned to yet has no duration here, and that is correct:
    forking ffprobe six hundred times to draw a list would take the picture
    away for a minute on a two-core box.

    Folders have no size. Measuring one means walking it, and walking every
    folder on the page to draw the page is the same mistake as probing. Ask
    :func:`deletion_plan` when somebody actually wants the number.
    """
    folder = _resolve_folder(media_root, relative)
    root = Path(media_root).resolve()
    sort = sort if sort in ("name", "size", "date") else "name"
    descending = str(order).lower() == "desc"

    rows: List[Tuple[int, Any, str, bool, Optional[str]]] = []
    try:
        with os.scandir(folder) as scan:
            for entry in scan:
                try:
                    is_dir = entry.is_dir()
                except OSError:  # pragma: no cover - vanished under us
                    continue
                note = _system_note(entry.name, is_dir)
                # Folders first, then files, then the machinery - whichever way
                # the page is sorted. A list that puts a folder halfway down
                # between two episodes is a list nobody can scan with their eye.
                group = 2 if note else (0 if is_dir else 1)
                if sort == "name":
                    key: Any = entry.name.lower()
                else:
                    try:
                        stat = entry.stat()
                    except OSError:  # pragma: no cover - vanished under us
                        continue
                    key = stat.st_size if sort == "size" else stat.st_mtime
                rows.append((group, key, entry.name, is_dir, note))
    except FileNotFoundError:
        raise LibraryNotFound(f"there is no '{relative}' here any more") from None
    except NotADirectoryError:
        raise LibraryError(f"'{relative}' is a file, not a folder") from None
    except OSError as exc:
        raise LibraryError(f"cannot read that folder: {exc}") from None

    # Two stable sorts rather than one composite key: the direction applies to
    # what was asked for, never to the folders-before-files grouping.
    rows.sort(key=lambda row: row[1], reverse=descending)
    rows.sort(key=lambda row: row[0])

    total = len(rows)
    number, size, pages, start = _page_bounds(total, page, per_page)
    here = _relative(root, folder) if folder != root else ""

    entries = [
        _row(folder, here, name, is_dir, note, allowed=allowed)
        for _, _, name, is_dir, note in rows[start:start + size]
    ]

    return {
        "path": here,
        "parent": _parent_of(here),
        "crumbs": _crumbs(here),
        "entries": entries,
        "total": total,
        "page": number,
        "pages": pages,
        "per_page": size,
        "sort": sort,
        "order": "desc" if descending else "asc",
        "counts": {
            "folders": sum(1 for r in rows if r[0] == 0),
            "files": sum(1 for r in rows if r[0] == 1),
            "system": sum(1 for r in rows if r[0] == 2),
        },
    }


def _row(
    folder: Path,
    here: str,
    name: str,
    is_dir: bool,
    note: Optional[str],
    *,
    allowed: Sequence[str],
) -> Dict[str, Any]:
    """One row of a listing, measured only because it is on the page."""
    path = folder / name
    try:
        stat = path.stat()
        size, modified = stat.st_size, stat.st_mtime
    except OSError:  # pragma: no cover - vanished between listing and stat
        size, modified = None, None

    video = (
        not is_dir
        and path.suffix.lower() in {str(e).lower() for e in allowed}
    )
    if note:
        kind = "system"
    elif is_dir:
        kind = "folder"
    else:
        kind = "video" if video else "other"

    duration = None
    if kind == "video":
        info = probe.cached_media(path)
        duration = info.duration if info else None

    return {
        "name": name,
        "path": f"{here}/{name}" if here else name,
        "kind": kind,
        # A folder's size is not free, so it is not on a listing. See browse().
        "bytes": None if is_dir else size,
        "modified": modified,
        "duration": duration,
        # Only real library content may be picked. Nobody deletes the
        # machinery by accident, and nobody renames it at all.
        "selectable": kind in ("folder", "video"),
        "note": note,
    }


def _parent_of(here: str) -> Optional[str]:
    if not here:
        return None                       # there is nothing above the media root
    return here.rpartition("/")[0]


def _crumbs(here: str) -> List[Dict[str, str]]:
    """The trail back to the root, for a header somebody can click."""
    trail = [{"name": "Library", "path": ""}]
    walked = ""
    for part in [p for p in here.split("/") if p]:
        walked = f"{walked}/{part}" if walked else part
        trail.append({"name": part, "path": walked})
    return trail


# ==========================================================================
# measuring
# ==========================================================================
def _measure(target: Path) -> Tuple[int, int, int]:
    """``(files, folders, bytes)`` under ``target``, following nothing."""
    if target.is_file():
        try:
            return 1, 0, target.stat().st_size
        except OSError:  # pragma: no cover - vanished under us
            return 1, 0, 0
    files = folders = total = 0
    for base, dirnames, filenames in os.walk(str(target)):
        folders += len(dirnames)
        for name in filenames:
            files += 1
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:  # pragma: no cover - vanished under us
                continue
    return files, folders, total


def deletion_plan(
    media_root: Path,
    relative: str,
    *,
    allowed: Sequence[str],
    config: Optional[Config] = None,
    uploads: Optional[UploadStore] = None,
) -> Dict[str, Any]:
    """Everything a confirmation dialog needs before anything is deleted.

    "Are you sure?" is not a question anybody can answer. "This folder holds
    142 episodes, 38 GB, and channel 4 plays from it" is. Getting the exact
    numbers costs a walk of the folder, which is why it happens here - once,
    when somebody has actually asked - and not on every row of every listing.
    """
    target = _resolve(media_root, relative, allowed=allowed)
    files, folders, total = _measure(target)
    refs = (
        folder_references(config, target) if config is not None
        else {"channels": [], "dayparts": [], "used": False}
    )
    busy = uploads_into(uploads, target) if uploads is not None else []
    return {
        "name": target.name,
        "relative": _relative(media_root, target),
        "kind": "folder" if target.is_dir() else "file",
        "files": files,
        "folders": folders,
        "bytes": total,
        "references": refs,
        "uploads": len(busy),
        # Said plainly because the disk gauge on the same screen will not move
        # and somebody has to be told why before they conclude it is broken.
        "frees_space": False,
        "note": (
            "Deleting moves this to the trash on the same disk, so it frees "
            "no space until the trash is emptied."
        ),
    }


def free_space(
    media_root: Path, *, uploads: Optional[UploadStore] = None
) -> Dict[str, Any]:
    """What is left on the disk, and what could be got back by emptying things.

    Somebody who has just "deleted" forty gigabytes and watched the free-space
    figure not move needs an answer on the same screen. This is that answer:
    the bytes are in the trash, here is how many, and here is the button.
    """
    root = Path(media_root)
    try:
        usage = shutil.disk_usage(str(root))
        total, used, free = usage.total, usage.used, usage.free
    except OSError:                       # pragma: no cover - cannot tell
        total = used = free = None

    trash = trash_usage(root)
    if uploads is not None:
        spool_bytes = uploads.reclaimable()
    else:
        spool_bytes = _measure(upload_spool(root))[2] if upload_spool(root).is_dir() else 0

    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "trash_bytes": trash["bytes"],
        "trash_items": trash["items"],
        "spool_bytes": spool_bytes,
        "reclaimable_bytes": trash["bytes"] + spool_bytes,
        "note": (
            "Deleting moves files to the trash, which is on this same disk, so "
            "it frees nothing. Empty the trash to get the space back."
        ),
    }


# ==========================================================================
# 2. the trash
# ==========================================================================
def _token(now: float) -> str:
    """A trash token: when it happened, plus enough randomness to be unique.

    The timestamp is there so a person reading the directory can see what is
    old. The random half is there because two files called ``ep1.mp4``
    deleted from two different channels in the same second must not land on
    each other - that is the whole reason each item gets a directory of its
    own rather than being dropped into one flat folder.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"{stamp}-{secrets.token_hex(4)}"


def _item_dir(media_root: Path, token: Any) -> Path:
    if not isinstance(token, str) or not TOKEN.match(token):
        raise UnsafePath("that is not something in the trash")
    trash = trash_dir(media_root)
    return resolve_inside(trash, trash / token)


def trashed_payload(media_root: Path, token: str) -> Path:
    """The file or folder itself, inside its trash item."""
    meta = _read_meta(_item_dir(media_root, token))
    if meta is None:
        raise LibraryNotFound("that is not in the trash any more")
    return _item_dir(media_root, token) / _PAYLOAD / meta["name"]


def _read_meta(item: Path) -> Optional[Dict[str, Any]]:
    """What we wrote down about a trashed item, or ``None`` if it is not readable.

    Anything unreadable is simply not an item. The trash sits on a filesystem
    the LAN share can write to and on a box that gets switched off at the
    wall, so a half-written or hand-made ``item.json`` is a matter of when,
    and it must never be able to raise out of a listing.
    """
    try:
        data = json.loads((item / _META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    data["token"] = item.name
    return data


def _write_meta(item: Path, payload: Dict[str, Any]) -> None:
    (item / _META).write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _move(source: Path, destination: Path) -> None:
    """Move, by rename where it can be and by copy where it cannot.

    Inside one media root a rename is what happens, which is the point: a
    forty-gigabyte series goes to the trash instantly and there is no moment
    where half of it exists twice. A mount point underneath the media root is
    the case where that is not available, and refusing to delete is a worse
    answer than taking a while over it.
    """
    try:
        os.rename(str(source), str(destination))
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    shutil.move(str(source), str(destination))


def _stash(
    media_root: Path, target: Path, *, now: Optional[float] = None
) -> Dict[str, Any]:
    """Put an already-validated path into the trash. The engine behind delete."""
    root = Path(media_root).resolve()
    when = time.time() if now is None else float(now)
    relative = _relative(root, target)
    files, folders, total = _measure(target)

    trash = trash_dir(root)
    # exist_ok=False on purpose, and retried rather than trusted: two deletes
    # in the same second could in principle draw the same random half, and
    # landing two payloads in one item directory would lose one of them.
    for attempt in range(5):
        item = trash / _token(when)
        payload = item / _PAYLOAD
        try:
            payload.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:  # pragma: no cover - 1 in 4 billion
            continue
    else:  # pragma: no cover - see above
        raise LibraryError("could not make room in the trash for that")

    meta = {
        "name": target.name,
        "relative": relative,
        "from": relative.rpartition("/")[0],
        "kind": "folder" if target.is_dir() else "file",
        "deleted": when,
        "bytes": total,
        "files": files,
        "folders": folders,
    }
    # Written before the move, so a power cut between the two leaves a note
    # about a file that is still in its channel - harmless - rather than a
    # payload nothing can explain or restore.
    _write_meta(item, meta)
    try:
        _move(target, payload / target.name)
    except OSError as exc:
        shutil.rmtree(item, ignore_errors=True)
        raise LibraryError(f"could not delete that: {exc}") from None

    meta["token"] = item.name
    log.info("moved %s to the trash as %s (%d bytes)", relative, item.name, total)
    return meta


def move_to_trash(
    media_root: Path,
    relative: str,
    *,
    allowed: Sequence[str],
    uploads: Optional[UploadStore] = None,
    cancel_uploads: bool = False,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Delete something by moving it into the trash. Never unlinks.

    ``uploads`` is the upload store; pass it and an upload landing in this
    folder blocks the delete rather than being left writing into a folder that
    is no longer there. ``cancel_uploads=True`` cancels those sessions
    instead, which takes their chunks with them - there is no third outcome
    where chunks are orphaned in the spool.
    """
    with _lock_for(media_root):
        target = _resolve(media_root, relative, allowed=allowed)
        _settle_uploads(uploads, target, cancel=cancel_uploads, verb="deleted")
        item = _stash(media_root, target, now=now)
        item["freed_bytes"] = 0           # it is on the same disk. See free_space.
        item["trash_bytes"] = trash_usage(media_root)["bytes"]
        return item


def list_trash(media_root: Path) -> List[Dict[str, Any]]:
    """Everything waiting in the trash, newest first."""
    trash = trash_dir(media_root)
    if not trash.is_dir():
        return []
    items = []
    for entry in trash.iterdir():
        if not entry.is_dir() or not TOKEN.match(entry.name):
            continue                      # junk in the trash is not an item
        meta = _read_meta(entry)
        if meta is not None:
            items.append(meta)
    # Through _when(), because a hand-written note can hold a string where a
    # timestamp should be and sorting that against a float raises - out of a
    # listing, which is the one place nothing may ever raise.
    items.sort(key=_when, reverse=True)
    return items


def _when(meta: Dict[str, Any]) -> float:
    """When an item was deleted, as a number, whatever the note actually says."""
    try:
        stamp = float(meta.get("deleted", 0))
    except (TypeError, ValueError):
        return 0.0
    return stamp if stamp == stamp else 0.0        # NaN sorts nowhere sensible


def trash_usage(media_root: Path) -> Dict[str, Any]:
    """How many things are in the trash and how much room they take.

    Measured off the disk rather than added up from the notes: the notes can
    be stale and the number is going next to a "free space" figure somebody is
    about to make a decision with.
    """
    trash = trash_dir(media_root)
    if not trash.is_dir():
        return {"items": 0, "bytes": 0, "oldest": None, "newest": None}
    items = list_trash(media_root)
    total = sum(
        _measure(trash / item["token"] / _PAYLOAD)[2] for item in items
    )
    stamps = [_when(item) for item in items if _when(item)]
    return {
        "items": len(items),
        "bytes": total,
        "oldest": min(stamps) if stamps else None,
        "newest": max(stamps) if stamps else None,
    }


def restore(
    media_root: Path, token: str, *, replace: bool = False,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Put a trashed item back exactly where it came from.

    If something is already at that name the restore refuses and changes
    nothing - the item stays in the trash and the file that is there is left
    alone, because the box cannot know which of the two the customer wants.
    With ``replace=True`` the occupant is moved into the trash first rather
    than being destroyed, so a restore never costs anybody a file either way.

    The folder it came out of is rebuilt if it has gone, which is the ordinary
    case after somebody deletes a whole channel and then changes their mind
    about one episode of it.
    """
    root = Path(media_root).resolve()
    with _lock_for(root):
        item = _item_dir(root, token)
        meta = _read_meta(item)
        if meta is None:
            raise LibraryNotFound("that is not in the trash any more")

        payload = item / _PAYLOAD / meta["name"]
        if not payload.exists():
            raise LibraryNotFound(
                "the file that item describes is not there any more"
            )

        # The note came off a disk the file share can write to, so it is
        # re-checked exactly as if it had arrived in the request. It did.
        parts = _parts(meta.get("from", ""))
        parts.append(_last_component(meta["name"], allowed=(payload.suffix,)))
        destination = resolve_inside(root, root.joinpath(*parts))
        if _is_machinery(root, destination):
            raise UnsafePath("that item claims to belong to the box's own folders")

        replaced = None
        if destination.exists():
            if not replace:
                raise LibraryConflict(
                    f"'{meta['name']}' is already back in that folder. Rename "
                    f"or delete the one that is there first, or restore over "
                    f"it - the file that is there now would go to the trash, "
                    f"not away."
                )
            replaced = _stash(root, destination, now=now)["token"]

        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            _move(payload, destination)
        except OSError as exc:
            raise LibraryError(f"could not put that back: {exc}") from None
        shutil.rmtree(item, ignore_errors=True)

        log.info("restored %s from the trash", meta.get("relative", meta["name"]))
        return {
            "ok": True,
            "name": meta["name"],
            "relative": _relative(root, destination),
            "kind": meta.get("kind", "file"),
            "replaced": replaced,
        }


def purge_trash(
    media_root: Path,
    *,
    token: Optional[str] = None,
    older_than_days: Optional[float] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Really delete: one item, everything older than an age, or the lot.

    This is the only function in the module that destroys anything, which is
    why it is the only one somebody has to ask for by name.
    """
    with _lock_for(media_root):
        # A purge and a restore can name the same item; whichever gets
        # here first wins outright rather than the two racing over one
        # directory tree.
        root = Path(media_root)
        when = time.time() if now is None else float(now)
        trash = trash_dir(root)
        # The token is judged before anything else, including whether there is a
        # trash at all: "there is nothing to purge" must never be the reason a
        # malformed path component goes unexamined.
        item_dir = _item_dir(root, token) if token is not None else None
        if not trash.is_dir():
            if token is not None:
                raise LibraryNotFound("that is not in the trash any more")
            return {"items": 0, "bytes": 0}

        if item_dir is not None:
            wanted = [_read_meta(item_dir)]
            if wanted[0] is None:
                raise LibraryNotFound("that is not in the trash any more")
        else:
            wanted = list_trash(root)
            if older_than_days is not None:
                cutoff = when - float(older_than_days) * 86400
                wanted = [m for m in wanted if _when(m) <= cutoff]

        items = freed = 0
        for meta in wanted:
            item = trash / meta["token"]
            freed += _measure(item / _PAYLOAD)[2]
            shutil.rmtree(item, ignore_errors=True)
            items += 1
            log.info("purged %s from the trash", meta.get("relative", meta["token"]))
        return {"items": items, "bytes": freed}


def sweep_trash(
    media_root: Path,
    *,
    days: float = DEFAULT_TRASH_DAYS,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Reclaim anything the trash has held longer than ``days``.

    Meant to be called at start-up and on a timer. Start-up matters on its
    own: this box gets switched off at the wall, so nothing runs a schedule
    while it is off, and a box that spends six days a week unplugged would
    otherwise never reach the age at which it tidies itself up.
    """
    return purge_trash(media_root, older_than_days=days, now=now)


# ==========================================================================
# uploads in progress
# ==========================================================================
def uploads_into(
    store: Optional[UploadStore], folder: Path
) -> List[UploadSession]:
    """Sessions whose files would land in, or around, ``folder``.

    Both directions count. A session aimed at a sub-folder is obviously
    affected by this folder moving; a session aimed at the *parent* is too,
    because the relative paths inside it can name this folder and it would
    land its episodes into a folder that no longer exists.
    """
    if store is None:
        return []
    target = Path(folder).resolve()
    busy = []
    for session in store.sessions():
        try:
            into = Path(session.target.folder).resolve()
        except OSError:  # pragma: no cover - hostile fs
            continue
        if into == target or target in into.parents or into in target.parents:
            busy.append(session)
    return busy


def cancel_uploads_into(store: UploadStore, folder: Path) -> int:
    """Cancel every upload aimed at ``folder``, chunks and all.

    :meth:`UploadStore.cancel` removes the session directory outright, so
    there is no version of this that leaves chunks stranded in the spool.
    """
    busy = uploads_into(store, folder)
    for session in busy:
        store.cancel(session.id)
        log.info("cancelled upload %s: its folder is being changed", session.id)
    return len(busy)


def _settle_uploads(
    store: Optional[UploadStore], folder: Path, *, cancel: bool, verb: str
) -> None:
    """Refuse, or cancel cleanly. There is no third answer.

    Carrying on regardless is the one option not offered: the upload would go
    on writing chunks aimed at a folder that has moved or gone, and either
    strand gigabytes in the spool or land episodes in a folder nothing plays
    from. Both of those are found weeks later, by a customer.
    """
    busy = uploads_into(store, folder)
    if not busy:
        return
    if not cancel:
        raise LibraryBusy(
            f"{len(busy)} upload(s) are still arriving into that folder, so it "
            f"cannot be {verb} yet. Wait for them to finish, or cancel them."
        )
    cancel_uploads_into(store, folder)


# ==========================================================================
# 3. renaming
# ==========================================================================
def _resolved(path: Any) -> Path:
    try:
        return Path(path).resolve()
    except OSError:  # pragma: no cover - resolve() is forgiving in practice
        return Path(path)


def _relocate(path: Any, folder: Path, target: Path) -> Optional[Path]:
    """What ``path`` becomes when ``folder`` is renamed to ``target``.

    ``None`` when it is not affected. Prefix comparison is done on resolved
    paths and whole components, so ``/media/show`` is never treated as
    containing ``/media/show-evil``.
    """
    here = _resolved(path)
    if here == folder:
        return target
    try:
        return target / here.relative_to(folder)
    except ValueError:
        return None


def folder_references(config: Optional[Config], folder: Path) -> Dict[str, Any]:
    """Which channels and dayparts play from this folder, or from inside it.

    Asked BEFORE anything moves. A folder can be referenced by a daypart as
    well as by a channel - a late-night block pointed at its own folder - and
    both orphan exactly as easily.
    """
    channels: List[int] = []
    dayparts: List[int] = []
    if config is None:
        return {"channels": [], "dayparts": [], "used": False, "detail": []}

    target = _resolved(folder)
    detail: List[Dict[str, Any]] = []
    for channel in config.channels:
        if _relocate(channel.path, target, target) is not None:
            channels.append(channel.number)
            detail.append({
                "channel": channel.number, "name": channel.name,
                "where": "channel", "path": str(channel.path),
            })
        for part in channel.dayparts:
            if part.path is None:
                continue
            if _relocate(part.path, target, target) is not None:
                if channel.number not in dayparts:
                    dayparts.append(channel.number)
                detail.append({
                    "channel": channel.number, "name": channel.name,
                    "where": "daypart", "path": str(part.path),
                })
    return {
        "channels": channels,
        "dayparts": dayparts,
        "used": bool(channels or dayparts),
        "detail": detail,
    }


def _rename_back(source: Path, destination: Path) -> None:
    """Undo a rename. Its own function so the failure can be tested for real."""
    os.rename(str(source), str(destination))


def _config_base(data: Dict[str, Any], config_dir: Path) -> Path:
    """What relative paths in config.yaml are relative TO.

    The same rule the loader applies: ``media_root`` if there is one, the
    config file's own directory otherwise. Getting this wrong would mean
    quietly failing to notice a channel that does reference the folder.
    """
    raw = data.get("media_root")
    return _as_path(raw, config_dir) if raw else Path(config_dir)


def rename_folder(
    store: "configstore.ConfigStore",
    media_root: Path,
    relative: str,
    new_name: str,
    *,
    uploads: Optional[UploadStore] = None,
    cancel_uploads: bool = False,
) -> Dict[str, Any]:
    """Rename a folder and repoint everything in config.yaml that played from it.

    Both, or neither. The order is: rename the folder, then rewrite the config
    through :meth:`ConfigStore.update` - which validates the whole document and
    writes it atomically - and if that raises for any reason at all, rename the
    folder back. The other order does not work: the config would name a folder
    that does not exist yet, and the loader refuses that.

    A folder nothing references does not touch config.yaml at all. That is not
    an optimisation: ``update`` rewrites the document through the YAML dumper
    and every comment in the customer's file goes with it, and nobody renaming
    a spare folder asked for that.

    Raises :class:`HalfRenamed` - and only ever this one - when the folder
    moved, the config did not, and the folder would not move back. Its message
    is the only thing that will ever connect the black channel to the rename,
    so it names both folders, the config file and every affected channel.
    """
    with _lock_for(media_root):
        # Held across the folder rename AND the config write. Without it a
        # second rename lands between the two and finds free a name this
        # one is already using.
        folder = _resolve_folder(media_root, relative)
        root = Path(media_root).resolve()
        if folder == root:
            raise LibraryError(
                "that is the media library itself, not a folder inside it"
            )

        wanted = safe_folder_name(new_name)
        if wanted == folder.name:
            return {
                "ok": True, "unchanged": True, "from": folder.name, "to": wanted,
                "relative": _relative(root, folder), "channels": [], "dayparts": [],
            }

        target = folder.parent / wanted
        # Belt to the name check's braces: where would it actually land?
        resolve_inside(root, target)
        if target.exists():
            raise LibraryConflict(
                f"there is already something called '{wanted}' in that folder"
            )

        _settle_uploads(uploads, folder, cancel=cancel_uploads, verb="renamed")

        before = folder_references(_safe_load(store), folder)
        changed_channels: List[int] = []
        changed_dayparts: List[int] = []

        def mutate(data: Dict[str, Any]) -> None:
            base = _config_base(data, store.path.parent)
            for entry in configstore.ConfigStore.channels_of(data):
                number = _number_of(entry)
                moved = _relocate(_as_path(entry.get("path"), base), folder, target) \
                    if entry.get("path") else None
                if moved is not None:
                    entry["path"] = str(moved)
                    changed_channels.append(number)
                for part in entry.get("dayparts") or []:
                    if not isinstance(part, dict) or not part.get("path"):
                        continue
                    shifted = _relocate(_as_path(part["path"], base), folder, target)
                    if shifted is not None:
                        part["path"] = str(shifted)
                        if number not in changed_dayparts:
                            changed_dayparts.append(number)
            if not changed_channels and not changed_dayparts:
                # Nothing plays from here. Abort the write rather than rewrite the
                # customer's annotated config to say exactly what it said before.
                raise _NothingReferenced()

        try:
            os.rename(str(folder), str(target))
        except OSError as exc:
            raise LibraryError(f"could not rename that folder: {exc}") from None

        try:
            store.update(mutate)
        except _NothingReferenced:
            pass
        except Exception as exc:              # noqa: BLE001 - anything at all undoes it
            changed_channels = changed_channels or before["channels"]
            changed_dayparts = changed_dayparts or before["dayparts"]
            try:
                _rename_back(target, folder)
            except OSError as undo:
                raise HalfRenamed(_half_renamed(
                    folder, target, store.path, changed_channels, changed_dayparts,
                    exc, undo,
                )) from exc
            raise LibraryError(
                f"nothing was changed. The folder is still called '{folder.name}' "
                f"because {store.path.name} could not be updated: {exc}"
            ) from exc

        log.info("renamed %s to %s (channels %s)", folder, target, changed_channels)
        return {
            "ok": True,
            "unchanged": False,
            "from": folder.name,
            "to": wanted,
            "relative": _relative(root, target),
            "channels": sorted(set(changed_channels)),
            "dayparts": sorted(set(changed_dayparts)),
        }


def _number_of(entry: Dict[str, Any]) -> int:
    try:
        return int(entry.get("number", -1))
    except (TypeError, ValueError):  # pragma: no cover - _addressable fills it in
        return -1


def _safe_load(store: "configstore.ConfigStore") -> Optional[Config]:
    """The current config, or ``None`` if it will not load.

    Used only to describe what is about to happen. A config too broken to
    parse must not be the reason somebody cannot rename a folder - the write
    itself validates properly, and it is the write that matters.
    """
    try:
        return store.load()
    except Exception:                     # noqa: BLE001 - describing, not deciding
        log.warning("could not read the config to check what uses that folder",
                    exc_info=True)
        return None


def _half_renamed(
    folder: Path,
    target: Path,
    config_path: Path,
    channels: Sequence[int],
    dayparts: Sequence[int],
    write_error: BaseException,
    undo_error: BaseException,
) -> str:
    """The message for the one state nobody else on the box can explain."""
    affected = sorted(set(list(channels) + list(dayparts)))
    listed = ", ".join(str(n) for n in affected) or "no channels"
    return (
        f"The folder '{folder.name}' has been renamed to '{target.name}', but "
        f"{config_path} could not be updated ({write_error}) and the folder "
        f"could not be renamed back either ({undo_error}).\n"
        f"The box is now in this state: the files are all at {target}, and "
        f"channel(s) {listed} still point at {folder}, which no longer exists. "
        f"Those channels will come up empty until this is put right.\n"
        f"To fix it: rename '{target.name}' back to '{folder.name}', or edit "
        f"{config_path} so those channels point at {target}."
    )


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_TRASH_DAYS",
    "MACHINERY_GLOBS",
    "MAX_PAGE_SIZE",
    "TRASH_NAME",
    "WELCOME_NAME",
    "HalfRenamed",
    "LibraryBusy",
    "LibraryConflict",
    "LibraryError",
    "LibraryNotFound",
    "browse",
    "cancel_uploads_into",
    "deletion_plan",
    "folder_references",
    "free_space",
    "list_trash",
    "move_to_trash",
    "purge_trash",
    "rename_folder",
    "restore",
    "sweep_trash",
    "trash_dir",
    "trash_usage",
    "trashed_payload",
    "upload_spool",
    "uploads_into",
]
