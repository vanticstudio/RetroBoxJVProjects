"""Chunked, resumable uploads, for getting a whole show onto the box.

The LAN file share is still the better tool for seeding a brand new box with a
whole library from a desktop, and it is not going anywhere. This is for the
normal case - "here are ten new episodes" - and for people who would rather not
learn what SMB is.

A channel folder can easily be tens of gigabytes, and a single enormous POST
that dies at 90% and starts again from zero is not an upload feature. So a file
is cut into fixed-size chunks and each one is uploaded separately against a
session:

    POST   a session        say what you are about to send
    PUT    each chunk       in any order, as many times as you like
    POST   commit           assemble, check, and move into place

Three properties hold the whole thing up:

* **Nothing is remembered in memory.** Which chunks have arrived is derived by
  looking at which chunk files exist on disk, so a reboot mid-upload loses
  nothing: the browser asks what is missing and carries on. There is no
  in-memory index to go stale, and no manifest to fsync on every chunk.
* **A chunk counts as received only once it is whole.** Each one is written to
  a staging name and renamed into place, so a torn write is simply absent and
  gets re-sent rather than silently corrupting the middle of a film.
* **The channel folder never sees anything unfinished.** Chunks and assembly
  both happen in a spool directory that the episode scanner does not look in,
  and the finished file arrives in the channel by a single rename.

The spool sits under the media root on purpose: for a channel folder in the
library that is the same filesystem as the destination, which makes the final
move an atomic rename instead of a long cross-device copy. It is a dotted
directory, so channel discovery skips it (see ``_discover_channels``). A
channel added by hand can point anywhere though - /mnt/usb/Cartoons is a
perfectly ordinary thing for somebody to do - so the landing falls back to a
staged copy, and the free-space check measures both disks rather than assuming
there is only one.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import probe
from .configwrite import _fsync_dir, atomic_write_text
from .safepath import UnsafePath, resolve_inside, safe_relative_path

log = logging.getLogger(__name__)

#: Name of the spool directory under the media root. Dotted, so the channel
#: scanner and auto-discovery both skip straight past it.
SPOOL_NAME = ".retrobox-uploads"

#: A session id is a path component, so it is matched, not sanitised.
SESSION_ID = re.compile(r"\A[A-Za-z0-9_-]{16,64}\Z")

_MANIFEST = "session.json"
_CHUNK_SUFFIX = ".chunk"
_STAGING_SUFFIX = ".part"
_ASSEMBLED_SUFFIX = ".assembled"
_READ_SIZE = 1024 * 1024

#: What a file ended up as, once the session was committed.
STATE_DONE = "done"
STATE_SKIPPED = "skipped"
STATE_NO_VIDEO = "no video"
STATE_FAILED = "failed"


class UploadError(Exception):
    """An upload request we refuse, with a message fit for the dashboard."""


#: One lock per spool directory, shared by every store that points at it.
#:
#: The dashboard builds an ``UploadStore`` per request on purpose - nothing is
#: kept in memory, which is what makes a mid-upload reboot survivable - so a
#: lock created in ``__init__`` would be a different object on every request
#: and would serialise nothing whatsoever. Flask serves those requests on
#: threads, so "nothing" means two phones can both walk past the session cap,
#: and a sweep can delete a session in the instant between its directory being
#: made and its manifest being written. Keying on the spool is what makes two
#: stores built from the same config meet on the same lock.
_SPOOL_LOCKS: Dict[str, Any] = {}
_SPOOL_LOCKS_GUARD = threading.Lock()


def _lock_for(spool: Path) -> Any:
    """The one lock for this spool, making it the first time it is asked for."""
    key = os.path.realpath(str(spool))
    with _SPOOL_LOCKS_GUARD:
        lock = _SPOOL_LOCKS.get(key)
        if lock is None:
            # Reentrant, so a future caller that already holds it can call
            # through something that takes it again without wedging the box.
            lock = threading.RLock()
            _SPOOL_LOCKS[key] = lock
        return lock


@dataclass(frozen=True)
class UploadLimits:
    """The bounds that keep an unauthenticated endpoint from owning the box."""

    chunk_bytes: int
    max_file_bytes: int
    max_files: int
    max_sessions: int
    min_free_bytes: int
    expiry_seconds: float


@dataclass(frozen=True)
class UploadTarget:
    """Where a session is going to put its files."""

    kind: str                       # "channel" (exists) | "new" (create after)
    folder: Path
    channel_number: Optional[int] = None
    channel_name: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "folder": str(self.folder),
            "channel_number": self.channel_number,
            "channel_name": self.channel_name,
        }


@dataclass
class UploadFile:
    index: int
    relative: str
    size: int
    chunks: int
    action: str = "upload"          # upload | skip | replace
    duplicate: bool = False
    #: Written into the manifest just before this file is moved into the
    #: channel, so a commit that was cut off part way through can tell its own
    #: episodes from ones that were already there. See :meth:`UploadStore._land`.
    landed: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "relative": self.relative,
            "name": self.relative.rsplit("/", 1)[-1],
            "size": self.size,
            "chunks": self.chunks,
            "action": self.action,
            "duplicate": self.duplicate,
            "landed": self.landed,
        }


@dataclass
class UploadSession:
    id: str
    created: float
    target: UploadTarget
    files: List[UploadFile] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


@dataclass(frozen=True)
class FileResult:
    """What became of one file at commit time."""

    index: int
    relative: str
    state: str
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "relative": self.relative,
            "name": self.relative.rsplit("/", 1)[-1],
            "state": self.state,
            "detail": self.detail,
        }


class UploadStore:
    """Every in-progress upload on the box, and the spool they live in."""

    def __init__(
        self,
        spool: Path,
        limits: UploadLimits,
        *,
        allowed: Sequence[str],
        clock: Callable[[], float] = time.time,
        sweep_on_start: bool = False,
    ) -> None:
        self.spool = Path(spool)
        self.limits = limits
        self.allowed = tuple(allowed)
        self._clock = clock
        # Shared with every other store on this spool, because the dashboard
        # builds one of these per request - see _lock_for. It serialises
        # session creation against itself, so the cap cannot be raced past,
        # and against the sweep, so a session cannot be reclaimed in the
        # instant between its directory and its manifest.
        self._lock = _lock_for(self.spool)
        if sweep_on_start:
            # The box may have been powered off mid-upload; nothing runs a
            # timer while it is off, so start-up is the only chance to notice.
            self.sweep()

    # -- ids and paths ------------------------------------------------------
    def _session_dir(self, session_id: str) -> Path:
        """The directory for a session id, after proving the id is safe.

        The id arrives in a URL and is used as a path component, so it is
        matched against a whitelist pattern rather than cleaned up. Then the
        result is resolved and confirmed to be inside the spool, because being
        careful once is not the same as being careful.
        """
        if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
            raise UnsafePath("that is not a valid upload session")
        return resolve_inside(self.spool, self.spool / session_id)

    def _manifest_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / _MANIFEST

    def _file_dir(self, session_id: str, index: int) -> Path:
        return self._session_dir(session_id) / f"f{int(index)}"

    # -- creating -----------------------------------------------------------
    def create(
        self,
        target: UploadTarget,
        files: Iterable[Tuple[str, int]],
        *,
        actions: Optional[Dict[int, str]] = None,
    ) -> UploadSession:
        """Register what is about to be sent, or refuse the whole batch.

        Every path is validated here and nothing is written until the batch as
        a whole has been weighed against the free space - checking file by file
        would let a hundred individually-reasonable files fill the disk between
        them.
        """
        entries = list(files)
        if not entries:
            raise UploadError("there are no files to upload")
        if len(entries) > self.limits.max_files:
            raise UploadError(
                f"that is {len(entries)} files; this box takes "
                f"{self.limits.max_files} at a time"
            )

        actions = actions or {}
        planned: List[UploadFile] = []
        total = 0
        for index, (raw_path, raw_size) in enumerate(entries):
            relative = safe_relative_path(raw_path, allowed=self.allowed)
            size = self._whole_size(raw_size)
            if size > self.limits.max_file_bytes:
                raise UploadError(
                    f"'{relative}' is larger than this box accepts "
                    f"({self.limits.max_file_bytes // (1024 * 1024)} MB)"
                )
            # Belt to the string check's braces: where would it actually land?
            destination = resolve_inside(target.folder, target.folder / relative)
            total += size
            planned.append(
                UploadFile(
                    index=index,
                    relative=relative,
                    size=size,
                    chunks=self._chunk_count(size),
                    action=str(actions.get(index, "upload")),
                    duplicate=destination.exists(),
                )
            )

        self._require_space(total, folder=target.folder)

        with self._lock:
            live = self.sessions()
            if len(live) >= self.limits.max_sessions:
                raise UploadError(
                    f"there are already {len(live)} uploads running; finish or "
                    f"cancel one first"
                )
            session = UploadSession(
                id=secrets.token_urlsafe(24),
                created=self._clock(),
                target=target,
                files=planned,
            )
            directory = self._session_dir(session.id)
            directory.mkdir(parents=True, exist_ok=True)
            self._write_manifest(session)
            self._touch(session.id)

        log.info(
            "upload session %s: %d file(s), %d bytes -> %s",
            session.id, len(planned), total, target.folder,
        )
        return session

    def _whole_size(self, raw: Any) -> int:
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise UploadError("every file needs a size")
        try:
            size = int(raw)
        except (TypeError, ValueError):
            raise UploadError("every file needs a size") from None
        if size < 0:
            raise UploadError("a file cannot have a negative size")
        return size

    def _chunk_size(self, item: UploadFile, chunk: int) -> int:
        """How many bytes chunk ``chunk`` of this file must be, exactly."""
        if item.size <= 0:
            return 0
        start = chunk * self.limits.chunk_bytes
        return min(self.limits.chunk_bytes, item.size - start)

    def _chunk_count(self, size: int) -> int:
        if size <= 0:
            return 1                              # an empty file is one chunk
        return -(-size // self.limits.chunk_bytes)

    @staticmethod
    def _nearest_existing(path: Path) -> Optional[Path]:
        """The first directory at or above ``path`` that is really there.

        A new channel's folder is not created until its files land, and the
        spool is not created until the first session, so asking the filesystem
        about either of them directly would simply fail.
        """
        candidate = Path(path)
        for step in (candidate, *candidate.parents):
            if step.is_dir():
                return step
        return None

    def _require_space(self, wanted: int, *, folder: Optional[Path] = None) -> None:
        """Refuse anything that would take a disk below the safety margin.

        Two disks may be involved, not one. The chunks go into the spool and
        the finished files go into the channel folder, and a channel added by
        hand can point at a plugged-in drive. Measuring only the spool is how
        an upload aimed at a nearly-full external drive gets accepted and then
        fails at the very end, after the customer has waited for the whole
        transfer. When both are on the same filesystem the second check simply
        gives the same answer as the first, which costs nothing.
        """
        places = [(self._nearest_existing(self.spool), "")]
        if folder is not None:
            places.append((
                self._nearest_existing(folder),
                " on the drive that channel's folder is on",
            ))

        measured = set()
        for probe_dir, where in places:
            if probe_dir is None or str(probe_dir) in measured:
                continue
            measured.add(str(probe_dir))
            try:
                free = shutil.disk_usage(str(probe_dir)).free
            except OSError:
                continue                          # cannot tell; do not block
            if free - wanted < self.limits.min_free_bytes:
                raise UploadError(
                    f"not enough free space{where}: {free // (1024 * 1024)} MB "
                    f"left, this upload needs {wanted // (1024 * 1024)} MB and "
                    f"the box keeps "
                    f"{self.limits.min_free_bytes // (1024 * 1024)} MB in reserve"
                )

    # -- the manifest -------------------------------------------------------
    def _write_manifest(self, session: UploadSession) -> None:
        payload = {
            "id": session.id,
            "created": session.created,
            "target": session.target.as_dict(),
            "files": [f.as_dict() for f in session.files],
        }
        atomic_write_text(
            self._manifest_path(session.id), json.dumps(payload, indent=1)
        )

    def get(self, session_id: str) -> UploadSession:
        manifest = self._manifest_path(session_id)
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise UploadError("that upload session is not here any more") from None
        return self._from_manifest(data)

    @staticmethod
    def _from_manifest(data: Dict[str, Any]) -> UploadSession:
        raw_target = data.get("target") or {}
        target = UploadTarget(
            kind=str(raw_target.get("kind", "channel")),
            folder=Path(str(raw_target.get("folder", ""))),
            channel_number=raw_target.get("channel_number"),
            channel_name=raw_target.get("channel_name"),
        )
        files = [
            UploadFile(
                index=int(f["index"]),
                relative=str(f["relative"]),
                size=int(f["size"]),
                chunks=int(f["chunks"]),
                action=str(f.get("action", "upload")),
                duplicate=bool(f.get("duplicate", False)),
                landed=bool(f.get("landed", False)),
            )
            for f in data.get("files", [])
        ]
        return UploadSession(
            id=str(data["id"]),
            created=float(data.get("created", 0.0)),
            target=target,
            files=files,
        )

    def sessions(self) -> List[UploadSession]:
        """Every session on disk. Anything unreadable is simply not one."""
        if not self.spool.is_dir():
            return []
        found = []
        for entry in sorted(self.spool.iterdir()):
            if not entry.is_dir() or not SESSION_ID.match(entry.name):
                continue                          # junk in the spool is not a session
            try:
                found.append(self.get(entry.name))
            except (UploadError, UnsafePath, KeyError, TypeError, ValueError):
                log.debug("ignoring unreadable upload session %s", entry.name)
        return found

    # -- chunks -------------------------------------------------------------
    def received(self, session_id: str, index: int) -> List[int]:
        """Which chunks of a file are on disk. Derived, never remembered."""
        directory = self._file_dir(session_id, index)
        if not directory.is_dir():
            return []
        numbers = []
        for entry in directory.iterdir():
            if not entry.name.endswith(_CHUNK_SUFFIX):
                continue
            try:
                numbers.append(int(entry.name[: -len(_CHUNK_SUFFIX)]))
            except ValueError:
                continue
        return sorted(numbers)

    def missing(self, session_id: str) -> Dict[int, List[int]]:
        """Per file, the chunks still to come - what a resuming client asks for."""
        session = self.get(session_id)
        out = {}
        for item in session.files:
            if item.action == "skip":
                out[item.index] = []
                continue
            have = set(self.received(session_id, item.index))
            out[item.index] = [i for i in range(item.chunks) if i not in have]
        return out

    def put_chunk(self, session_id: str, index: int, chunk: int, stream: Any) -> int:
        """Take one chunk, or leave nothing behind having tried.

        Streamed to a staging name and renamed, so a chunk that arrives torn is
        absent rather than wrong - the client asks what is missing and sends it
        again.
        """
        session = self.get(session_id)
        item = self._file_of(session, index)
        if not isinstance(chunk, int) or isinstance(chunk, bool):
            raise UploadError("the chunk number must be a whole number")
        if not 0 <= chunk < item.chunks:
            raise UploadError(
                f"chunk {chunk} is not part of '{item.relative}'"
            )
        # Re-check the path on every chunk, not just at session creation: the
        # manifest is a file on disk, and this is the layer that writes.
        safe_relative_path(item.relative, allowed=self.allowed)
        self._require_space(self.limits.chunk_bytes)

        directory = self._file_dir(session_id, index)
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / f"{chunk}{_CHUNK_SUFFIX}"
        staged = Path(str(final) + _STAGING_SUFFIX)

        limit = self.limits.chunk_bytes
        expected = self._chunk_size(item, chunk)
        written = 0
        try:
            with staged.open("wb") as out:
                while True:
                    piece = stream.read(min(_READ_SIZE, limit - written + 1))
                    if not piece:
                        break
                    written += len(piece)
                    if written > limit:
                        raise UploadError(
                            f"a chunk may not be larger than {limit} bytes"
                        )
                    out.write(piece)
                out.flush()
                os.fsync(out.fileno())
            # Every chunk of a file of known size has a known exact length.
            # A short chunk accepted as complete is the one failure mode that
            # corrupts the middle of a film without anything looking wrong.
            if written != expected:
                raise UploadError(
                    f"chunk {chunk} arrived as {written} bytes, expected {expected}"
                )
            staged.replace(final)
        except BaseException:
            try:
                staged.unlink()
            except OSError:  # pragma: no cover - already gone
                pass
            raise

        self._touch(session_id)
        return written

    def _touch(self, session_id: str) -> None:
        """Mark a session as still alive, in the store's own time.

        The sweeper compares this against the same clock the store was given,
        so the two cannot disagree - stamping it with the filesystem's idea of
        "now" while deciding with an injected clock would mean the sweep only
        ever works by accident.
        """
        now = self._clock()
        try:
            os.utime(self._manifest_path(session_id), (now, now))
        except OSError:  # pragma: no cover - the session was just cancelled
            pass

    def _file_of(self, session: UploadSession, index: Any) -> UploadFile:
        if isinstance(index, bool) or not isinstance(index, int):
            raise UploadError("the file number must be a whole number")
        for item in session.files:
            if item.index == index:
                return item
        raise UploadError(f"file {index} is not part of this upload")

    def stray_files(self, session_id: str) -> List[str]:
        """Any staging leftovers in a session - should always be empty."""
        directory = self._session_dir(session_id)
        if not directory.is_dir():
            return []
        return sorted(
            str(p.relative_to(directory))
            for p in directory.rglob("*" + _STAGING_SUFFIX)
        )

    # -- finishing ----------------------------------------------------------
    def commit(self, session_id: str) -> List[FileResult]:
        """Assemble every file and move it into the channel folder.

        Nothing lands until the whole batch is complete. Landing nineteen of
        twenty episodes and *then* refusing the request reads to the person
        uploading as "it failed" while most of their files are already in the
        channel - and when they send the missing chunk and finish it again, the
        box meets its own episodes sitting at the destination and reports them
        as duplicates somebody else put there. So the batch is weighed first
        and either all of it goes in or none of it does.

        Assembly happens in the spool and each finished file arrives by a
        single rename, so the episode scanner only ever sees whole files.
        """
        session = self.get(session_id)
        outstanding = self.missing(session_id)
        short = [
            f"'{item.relative}' ({len(outstanding[item.index])} chunk(s))"
            for item in session.files
            if item.action != "skip" and outstanding.get(item.index)
        ]
        if short:
            listed = ", ".join(short[:3])
            if len(short) > 3:
                listed += f" and {len(short) - 3} more"
            raise UploadError(
                f"nothing was saved: this upload is still missing {listed}. "
                f"Send what is left and finish it again."
            )

        results: List[FileResult] = []
        for item in session.files:
            if item.action == "skip":
                results.append(FileResult(item.index, item.relative, STATE_SKIPPED,
                                          "left alone"))
                continue
            results.append(self._land(session, item))

        self.cancel(session_id)
        return results

    def _land(self, session: UploadSession, item: UploadFile) -> FileResult:
        folder = session.target.folder
        destination = resolve_inside(folder, folder / item.relative)

        if item.landed and item.action != "replace" and destination.exists():
            # This session already wrote that file and is being finished for a
            # second time - the first commit was cut off part way through. It
            # is their own episode sitting there, whole, so say so. Calling it
            # a duplicate would be a plain lie about what happened, and the one
            # the results screen is most likely to be believed about.
            return FileResult(item.index, item.relative, STATE_DONE)

        if destination.exists() and item.action != "replace":
            # Never overwrite somebody's episode on a guess. Without an
            # explicit "replace" the safe reading of a clash is "leave it" -
            # but say which kind of clash it was, because a file that turned up
            # after this upload started is not the duplicate they were shown
            # when they chose what to send.
            detail = (
                "a file with that name was already there"
                if item.duplicate else
                "a file with that name turned up while this was uploading, so "
                "it was left alone"
            )
            return FileResult(item.index, item.relative, STATE_SKIPPED, detail)

        session_dir = self._session_dir(session.id)
        # A commit that died mid-assembly leaves a copy of the file behind, and
        # on a box where one episode is a gigabyte a stale copy per attempt is
        # how the spool fills up. Each attempt gets its own name so two of them
        # can never write the same file at once, and takes the older ones with
        # it as it starts.
        for stale in session_dir.glob(f"f{item.index}.*{_ASSEMBLED_SUFFIX}"):
            self._remove(stale)
        attempt = secrets.token_hex(4)
        assembled = session_dir / f"f{item.index}.{attempt}{_ASSEMBLED_SUFFIX}"

        chunk_dir = self._file_dir(session.id, item.index)
        try:
            with assembled.open("wb") as out:
                for number in range(item.chunks):
                    part = chunk_dir / f"{number}{_CHUNK_SUFFIX}"
                    with part.open("rb") as fh:
                        shutil.copyfileobj(fh, out, _READ_SIZE)
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            self._remove(assembled)
            return FileResult(item.index, item.relative, STATE_FAILED, str(exc))

        # Probe before it becomes an episode, so "this will not play" is
        # something the person uploading finds out, not the person watching.
        info = probe.probe_media(assembled)

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(assembled, 0o644)
            # Write down that this file is ours *before* moving it, not after.
            # The box gets switched off at the wall; if that happens between
            # the two, the note is already on disk and the retry knows the file
            # at the destination is this upload's. The other order would leave
            # it looking like a stranger's duplicate.
            self._mark_landed(session, item)
            self._move_into_place(assembled, destination)
        except OSError as exc:
            self._remove(assembled)
            return FileResult(item.index, item.relative, STATE_FAILED, str(exc))

        if info.unplayable:
            # Flagged, not deleted. It is the user's file and their decision.
            return FileResult(
                item.index, item.relative, STATE_NO_VIDEO,
                "uploaded, but there is no video stream in it - it will play as "
                "a black screen. Delete it from the channel if that is wrong.",
            )
        return FileResult(item.index, item.relative, STATE_DONE)

    def _mark_landed(self, session: UploadSession, item: UploadFile) -> None:
        """Note in the manifest that this file is about to become an episode.

        Losing the note is survivable - the file lands either way, and the
        worst of it is that a retry after a power cut describes the file as one
        that turned up rather than one we wrote. Failing the request over it is
        not survivable in the same sense: a 500 after the episode is safely in
        the channel tells the customer their upload broke when it did not.
        """
        item.landed = True
        try:
            self._write_manifest(session)
            self._touch(session.id)
        except OSError:
            log.warning(
                "could not record that %s landed; the retry will describe it "
                "less precisely", item.relative, exc_info=True,
            )

    def _move_into_place(self, assembled: Path, destination: Path) -> None:
        """Put the finished file in the channel, on this disk or another one.

        A rename is atomic and instant, which is the whole reason the spool
        lives under the media root. But a channel added by hand can point at a
        plugged-in drive, and a rename cannot cross a filesystem: it comes back
        as EXDEV, which unguarded escapes the route as a bare 500 with no
        message, every uploaded gigabyte stranded in the hidden spool, and the
        same 500 on every retry.

        So there is a fallback, and it copies into a hidden staging name inside
        the destination folder and renames from *there*. That last rename is on
        one filesystem, so it is atomic: the one thing that must never happen
        is half a film appearing under the name of a playable episode.
        """
        try:
            assembled.replace(destination)
            _fsync_dir(destination.parent)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise

        staging = destination.parent / f".{destination.name}{_STAGING_SUFFIX}"
        try:
            with assembled.open("rb") as src, staging.open("wb") as out:
                shutil.copyfileobj(src, out, _READ_SIZE)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(staging, 0o644)
            staging.replace(destination)
        except BaseException:
            # A drive pulled out mid-copy leaves a hidden part-file, never an
            # episode. Take it with us so a retry is not fighting it.
            self._remove(staging)
            raise
        _fsync_dir(destination.parent)
        self._remove(assembled)

    # -- housekeeping -------------------------------------------------------
    def cancel(self, session_id: str) -> None:
        """Delete a session and every chunk it was holding."""
        directory = self._session_dir(session_id)
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)

    def reclaimable(self) -> int:
        """Bytes currently sitting in the spool, so the UI can show it."""
        if not self.spool.is_dir():
            return 0
        total = 0
        for path in self.spool.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:  # pragma: no cover - vanished under us
                continue
        return total

    def sweep(self) -> int:
        """Delete sessions nothing has touched for too long. Returns bytes freed.

        The clock starts at the last chunk, not at session creation, so a slow
        upload over a bad connection is not binned out from under someone.
        """
        if not self.spool.is_dir():
            return 0
        freed = 0
        # Held for the whole sweep, so a session cannot be created underneath
        # one - the directory exists for an instant before its manifest does.
        with self._lock:
            for entry in sorted(self.spool.iterdir()):
                if not entry.is_dir() or not SESSION_ID.match(entry.name):
                    continue
                if not self._is_abandoned(entry):
                    continue
                size = sum(
                    p.stat().st_size for p in entry.rglob("*") if p.is_file()
                )
                shutil.rmtree(entry, ignore_errors=True)
                freed += size
                log.info("reclaimed abandoned upload %s (%d bytes)", entry.name, size)
        return freed

    def _is_abandoned(self, entry: Path) -> bool:
        """Has nothing touched this session for longer than its expiry?

        The manifest's mtime is the answer whenever there is a manifest, and it
        is stamped with the store's own clock so the two cannot disagree.

        Where there is no manifest the honest answer is "not that we can tell".
        A session directory exists for one instruction before its manifest
        does, so reading a missing manifest as "last touched at the dawn of
        time" deletes somebody's upload out from under the request that is
        creating it - which then dies inside its own manifest write and hands
        them a 500 for nothing. Fall back to the directory's own age instead,
        measured against the filesystem's clock because that is the clock that
        stamped it. That still reclaims the case worth reclaiming: a power cut
        during a cancel can leave chunks behind with the manifest already gone,
        and those are real gigabytes.
        """
        expiry = self.limits.expiry_seconds
        try:
            return (entry / _MANIFEST).stat().st_mtime <= self._clock() - expiry
        except OSError:
            pass
        try:
            return entry.stat().st_mtime <= time.time() - expiry
        except OSError:  # pragma: no cover - it went away underneath us
            return False

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink()
        except OSError:  # pragma: no cover
            pass


def spool_for(media_root: Path) -> Path:
    """Where in-progress chunks live for a given media library."""
    return Path(media_root) / SPOOL_NAME


__all__ = [
    "SPOOL_NAME",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_NO_VIDEO",
    "STATE_SKIPPED",
    "FileResult",
    "UploadError",
    "UploadFile",
    "UploadLimits",
    "UploadSession",
    "UploadStore",
    "UploadTarget",
    "spool_for",
]
