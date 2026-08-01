"""Writing config.yaml without ever being able to destroy it.

The box gets switched off at the wall. That is not a misuse of it, it is how
a television works, so every write to the config has to assume the power can
go at any instruction. A plain ``Path.write_text`` cannot: it truncates the
file to zero the moment it opens it, so a cut in that window leaves an empty
config.yaml and a unit that will not boot - on hardware that is already in
somebody's living room, with the dashboard you would fix it from living on
the box that is down.

So writes go the long way round:

    write a staging file next to the target -> fsync it -> rename it over the
    target -> fsync the directory

At no point does the old file stop being a complete file. The rename either
happened or it did not, and the directory fsync is what makes that answer
survive the power cut too. The staging file is deliberately created in the
same directory as the target, because ``os.replace`` is only atomic within a
filesystem - staging in /tmp would be a cross-device copy, and on a box where
/tmp is a tmpfs it would simply fail.

The first time anything in here modifies a config, the original is copied to
``config.yaml.bak`` and that copy is then never written again. It is the last
known good file from before any automation touched it, which is what somebody
restores by hand when all of this has still somehow gone wrong.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Optional, Union

log = logging.getLogger(__name__)

#: Suffix of the one-and-only backup - ``config.yaml`` -> ``config.yaml.bak``.
BACKUP_SUFFIX = ".bak"

PathLike = Union[str, "os.PathLike[str]"]


def atomic_write_bytes(path: PathLike, data: bytes) -> None:
    """Replace ``path`` with ``data``, all at once or not at all.

    Raises ``OSError`` if the write cannot be completed, in which case the
    existing file is left exactly as it was and no staging file is left behind.
    """
    target = Path(path)
    directory = target.parent if str(target.parent) else Path(".")
    mode = _mode_for(target)

    fd, staged_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(directory)
    )
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            # Flush the kernel's page cache for this file to the device. Until
            # this returns, the bytes exist only in memory.
            os.fsync(handle.fileno())
        # mkstemp is 0600 by design. Carry the target's real mode across so a
        # rewrite cannot quietly lock another service out of its own config.
        os.chmod(staged, mode)
        os.replace(staged, target)
    except BaseException:
        _remove_quietly(staged)
        raise

    # The data is durable but the name may not be: the directory entry that
    # points at it is itself just a cached write.
    _fsync_dir(directory)


def atomic_write_text(path: PathLike, text: str, *, encoding: str = "utf-8") -> None:
    """``atomic_write_bytes`` for text. No newline translation, ever."""
    return atomic_write_bytes(path, text.encode(encoding))


def backup_once(path: PathLike, *, suffix: str = BACKUP_SUFFIX) -> Optional[Path]:
    """Copy ``path`` alongside itself, the first time and only the first time.

    Returns the backup's path if this call is the one that created it, and
    ``None`` if there already was one or there is nothing to back up yet.

    It never overwrites an existing backup, and that is the entire point: the
    file is worth having precisely because it predates every automated edit, so
    the second run must not replace it with our own output. A backup made by
    hand counts too - if somebody put a config.yaml.bak there, it is theirs.
    """
    source = Path(path)
    backup = source.with_name(source.name + suffix)
    if backup.exists() or not source.is_file():
        return None

    # Through the same staging dance as everything else here. A half-written
    # backup would be worse than none: it is written once and never revisited,
    # so the corruption would be permanent.
    atomic_write_bytes(backup, source.read_bytes())
    os.chmod(backup, _mode_for(source))
    log.info("kept the pre-automation config at %s", backup)
    return backup


def write_config_text(path: PathLike, text: str, *, encoding: str = "utf-8") -> None:
    """Persist a config file: keep the original once, then write safely.

    This is the call site for anything that edits config.yaml - auto_channels
    now, the dashboard next. If either half fails it raises ``OSError`` and the
    config on disk is untouched, so callers can treat a failure as "not saved"
    rather than "saved, or possibly destroyed".
    """
    backup_once(path)
    atomic_write_text(path, text, encoding=encoding)


def _mode_for(target: Path) -> int:
    """The permission bits a rewrite of ``target`` should end up with."""
    try:
        return stat.S_IMODE(target.stat().st_mode)
    except OSError:
        # Brand new file: land on whatever a plain open() would have produced,
        # so going through here changes nothing an operator can see.
        current = os.umask(0)
        os.umask(current)
        return 0o666 & ~current


def _fsync_dir(directory: Path) -> None:
    """Best effort: the payload is already durable, the name is the bonus."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover - not every platform lets you open a dir
        log.debug("could not open %s to fsync it", directory, exc_info=True)
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - and not every one lets you fsync it
        log.debug("could not fsync %s", directory, exc_info=True)
    finally:
        os.close(fd)


def _remove_quietly(path: Path) -> None:
    """Clean up a staging file without ever masking the real error."""
    try:
        path.unlink()
    except OSError:  # pragma: no cover - already gone, or the disk is on fire
        pass


__all__ = [
    "BACKUP_SUFFIX",
    "atomic_write_bytes",
    "atomic_write_text",
    "backup_once",
    "write_config_text",
]
