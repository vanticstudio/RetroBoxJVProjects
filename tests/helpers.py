"""Shared test helpers."""

from __future__ import annotations

import time
from pathlib import Path
from typing import List

# A fixed, non-DST-transition day (2023-11-14) used as the base for building
# timestamps at a known *local* wall-clock time, so the dayparting tests give
# the same answer in every timezone.
_BASE_EPOCH = 1_700_000_000


def make_show(root: Path, name: str, episodes: int, ext: str = ".mp4") -> Path:
    """Create a show folder with ``episodes`` dummy episode files."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, episodes + 1):
        (folder / f"{name}_ep{i:02d}{ext}").write_bytes(b"\x00")
    return folder


class FakeClock:
    """A manually-advanced monotonic clock for deterministic timing tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def at_local(hour: int, minute: int = 0) -> float:
    """A POSIX timestamp whose *local* clock reads ``hour:minute``."""
    parts = list(time.localtime(_BASE_EPOCH))
    parts[3], parts[4], parts[5] = hour, minute, 0
    parts[8] = -1  # let mktime work out DST for us
    return time.mktime(tuple(parts))


class FakeWallClock:
    """A settable wall clock (POSIX seconds) for dayparting tests."""

    def __init__(self, hour: int = 12, minute: int = 0) -> None:
        self.set(hour, minute)

    def set(self, hour: int, minute: int = 0) -> None:
        self.now = at_local(hour, minute)

    def __call__(self) -> float:
        return self.now


def list_names(paths: List[Path]) -> List[str]:
    return [p.name for p in paths]
