"""Channels: the folders of episodes and how they decide what to play.

A :class:`Channel` wraps one show (a folder of episode files) and knows how to
answer two questions:

* "I just tuned in - what should I play?" (:meth:`Channel.tune_in`)
* "The episode ended - what's next?" (:meth:`Channel.advance`)

The answer depends on the configured ``tune_in`` mode (see ``config.py``):
random, resume, or broadcast. :class:`ChannelLineup` holds all the channels and
provides the up/down/by-number navigation a remote needs.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AbstractSet, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# Patterns for pulling a season number out of a file/folder path.
_SEASON_PATTERNS = (
    re.compile(r"s(\d{1,2})[ ._-]?e\d{1,3}", re.IGNORECASE),   # S06E01, s6e1
    re.compile(r"\bseason[ ._-]*(\d{1,2})\b", re.IGNORECASE),  # Season 6
    re.compile(r"\b(\d{1,2})x\d{1,3}\b"),                       # 6x01
)

from .config import ChannelConfig, Config
from .daypart import Daypart, resolve
from .playlist import SequentialOrder, ShuffleBag, make_order
from .probe import DEFAULT_EPISODE_SECONDS, flush_cache, probe_duration

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlayRequest:
    """An instruction to the player: play ``path`` starting at ``start`` sec."""

    path: Path
    start: float = 0.0


def detect_season(text: str) -> Optional[int]:
    """Best-effort extraction of a season number from a path/filename."""
    for pattern in _SEASON_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def scan_episodes(
    root: Path,
    extensions: Sequence[str],
    *,
    recursive: bool = True,
    exclude: Sequence[str] = (),
    exclude_seasons: AbstractSet[int] = frozenset(),
) -> List[Path]:
    """Return a sorted list of episode files under ``root``.

    Sorting is natural-ish (case-insensitive by full path) so that, in the rare
    cases we present episodes in order, they are at least stable. Hidden files
    and typical sidecar files are ignored.

    ``exclude`` is a list of case-insensitive glob patterns; any episode whose
    relative path or filename matches one is dropped. ``exclude_seasons`` drops
    episodes whose detected season number is in the set.
    """
    if not root.exists():
        log.warning("channel folder does not exist: %s", root)
        return []
    exts = {e.lower() for e in extensions}
    patterns = [p.lower() for p in exclude]
    walker = root.rglob("*") if recursive else root.glob("*")
    episodes = [
        p
        for p in walker
        if p.is_file()
        and p.suffix.lower() in exts
        and not _is_hidden(p, root)
        and not _is_excluded(p, root, patterns, exclude_seasons)
    ]
    episodes.sort(key=lambda p: str(p).lower())
    return episodes


def _is_hidden(path: Path, root: Path) -> bool:
    """Is anything between the scanned folder and this file hidden?

    Every folder the box keeps its own machinery in is dot-prefixed - the
    upload spool, the welcome placeholder, and the trash the dashboard moves
    deleted episodes into. Testing only ``path.name`` missed all of them,
    because ``rglob`` walks happily into a dot-folder: a channel pointed at the
    library root itself picked deleted episodes back out of the trash and put
    them on the air, which is the one thing a trash must never do.

    The check is RELATIVE to the folder being scanned, and that matters: the
    installer seeds ``<media>/.welcome`` with the boot splash and points a
    channel straight at it (installer/provision.sh). An absolute rule would
    hide that clip and every new box would come up with nothing to play.
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:  # pragma: no cover - walker paths are always under root
        parts = (path.name,)
    return any(part.startswith(".") for part in parts)


def _is_excluded(
    path: Path,
    root: Path,
    patterns: Sequence[str],
    exclude_seasons: AbstractSet[int],
) -> bool:
    import fnmatch

    try:
        rel = path.relative_to(root).as_posix().lower()
    except ValueError:  # pragma: no cover - path always under root here
        rel = path.name.lower()
    name = path.name.lower()
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
    if exclude_seasons:
        season = detect_season(rel)
        if season is not None and season in exclude_seasons:
            return True
    return False


class BroadcastSchedule:
    """A never-ending, always-running shuffled running order for a channel.

    Given episode durations and a fixed start epoch, it can report exactly what
    "would be airing" at any wall-clock moment - the illusion that the station
    kept broadcasting while nobody was watching. The running order is a single
    shuffle that loops forever.
    """

    def __init__(
        self,
        episodes: Sequence[Path],
        durations: Sequence[float],
        *,
        epoch: float,
        rng: random.Random,
        shuffle: bool = True,
    ) -> None:
        if len(episodes) != len(durations):
            raise ValueError("episodes and durations must be the same length")
        order = list(range(len(episodes)))
        if shuffle:
            rng.shuffle(order)
        self._episodes = [episodes[i] for i in order]
        self._durations = [max(1.0, float(durations[i])) for i in order]
        self._epoch = epoch
        self._cycle = sum(self._durations)

    def at(self, when: float) -> PlayRequest:
        """What is airing at wall-clock time ``when`` (and how far into it)."""
        elapsed = (when - self._epoch) % self._cycle
        for path, dur in zip(self._episodes, self._durations):
            if elapsed < dur:
                return PlayRequest(path=path, start=elapsed)
            elapsed -= dur
        # Floating point rounding safety net.
        return PlayRequest(path=self._episodes[-1], start=0.0)


class Channel:
    """A single TV channel backed by a folder of episodes."""

    def __init__(
        self,
        config: ChannelConfig,
        episodes: Sequence[Path],
        *,
        tune_in: str = "random",
        start_offset_min: float = 0.0,
        start_offset_max: Optional[float] = None,
        rng: Optional[random.Random] = None,
        daypart_episodes: Optional[Mapping[int, Sequence[Path]]] = None,
        wall_clock: Callable[[], float] = time.time,
        shuffle: bool = True,
    ) -> None:
        self.config = config
        self.episodes: List[Path] = list(episodes)
        self.tune_in_mode = tune_in
        self.shuffle = shuffle
        # Start each episode a random number of seconds in (within this range) so
        # the picture appears already "in the show" and channel switches land at
        # varied points instead of always the same spot.
        self.start_offset_min = max(0.0, start_offset_min)
        self.start_offset_max = (
            self.start_offset_min
            if start_offset_max is None
            else max(self.start_offset_min, start_offset_max)
        )
        self._rng = rng or random.Random()
        self._wall_clock = wall_clock

        # Episode pools, keyed by daypart index - or None for the channel's own
        # folder, which is what plays outside every configured window. Only a
        # daypart that declared its own `path` gets a pool; a daypart that just
        # renames the channel keeps sharing the base pool (and its shuffle), so
        # the running order does not restart at 10pm.
        self._pools: Dict[Optional[int], List[Path]] = {None: list(episodes)}
        for index, pool in (daypart_episodes or {}).items():
            self._pools[index] = list(pool)

        # Play orders and broadcast schedules are built lazily, per pool.
        self._orders: Dict[Optional[int], "ShuffleBag[Path] | SequentialOrder[Path]"] = {}
        self._broadcasts: Dict[Optional[int], BroadcastSchedule] = {}

        # Resume state (used by the "resume" tune-in mode).
        self._resume_path: Optional[Path] = None
        self._resume_position: float = 0.0

    # -- identity -----------------------------------------------------------
    @property
    def number(self) -> int:
        return self.config.number

    @property
    def name(self) -> str:
        """The channel's name *right now* - a daypart may be renaming it."""
        return self.name_at()

    def name_at(self, now: Optional[float] = None) -> str:
        _, part = self._active(now)
        if part is not None and part.name:
            return part.name
        return self.config.name

    @property
    def is_empty(self) -> bool:
        """True when no pool on this channel has a single episode in it."""
        return not any(self._pools.values())

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Channel {self.number} {self.config.name!r} ({len(self.episodes)} eps)>"

    # -- dayparting ---------------------------------------------------------
    def _active(self, now: Optional[float] = None) -> Tuple[Optional[int], Optional[Daypart]]:
        """Which daypart (index, window) is in effect - (None, None) for the default.

        Asked of the engine rather than worked out here, so that there is one
        place that decides what is on air. That matters because the engine also
        declines to decide at all when the box's clock cannot be believed, and
        this is the only path playback takes - name, pool, off-air and resume
        all come through here. A second copy of the loop would be a second copy
        that dayparts a flat-battery box on a nonsense timestamp.
        """
        parts = self.config.dayparts
        if not parts:
            return None, None
        decision = resolve(parts, self._wall_clock() if now is None else now)
        return decision.index, decision.part

    def active_daypart(self, now: Optional[float] = None) -> Optional[Daypart]:
        return self._active(now)[1]

    def is_off_air(self, now: Optional[float] = None) -> bool:
        """True when a sign-off window is in effect (colour bars, no programme)."""
        part = self.active_daypart(now)
        return part is not None and part.off_air

    def _pool_key(self, index: Optional[int], part: Optional[Daypart]) -> Optional[int]:
        """The pool a daypart draws from: its own, or the channel's base folder."""
        if part is None or part.path is None or index not in self._pools:
            return None
        return index

    def daypart_index(self, now: Optional[float] = None) -> Optional[int]:
        """Index of the daypart in effect, or None when the base channel is on."""
        return self._active(now)[0]

    def pool_key(self, now: Optional[float] = None) -> Optional[int]:
        """Which episode pool is live right now - used to spot a daypart change."""
        index, part = self._active(now)
        return self._pool_key(index, part)

    def daypart_pool(self, index: int) -> List[Path]:
        """Episodes carried by daypart ``index`` (its own folder, or the base pool)."""
        parts = self.config.dayparts
        if not 0 <= index < len(parts):
            return []
        part = parts[index]
        if part.off_air:
            return []
        return self._pools.get(self._pool_key(index, part)) or []

    def episodes_at(self, now: Optional[float] = None) -> List[Path]:
        """The episodes this channel can currently draw from (empty when off air)."""
        index, part = self._active(now)
        if part is not None and part.off_air:
            return []
        return self._pools.get(self._pool_key(index, part)) or []

    # -- playback selection -------------------------------------------------
    def _next_episode(self, key: Optional[int], pool: Sequence[Path]) -> PlayRequest:
        order = self._orders.get(key)
        if order is None:
            order = make_order(pool, shuffle=self.shuffle, rng=self._rng)
            self._orders[key] = order
        if self.start_offset_max > self.start_offset_min:
            start = self._rng.uniform(self.start_offset_min, self.start_offset_max)
        else:
            start = self.start_offset_min
        return PlayRequest(path=order.next(), start=start)

    def tune_in(self, *, now: Optional[float] = None) -> Optional[PlayRequest]:
        """Decide what to play the instant a viewer switches to this channel."""
        now = self._wall_clock() if now is None else now
        index, part = self._active(now)
        if part is not None and part.off_air:
            return None

        key = self._pool_key(index, part)
        pool = self._pools.get(key) or []
        if not pool:
            return None

        # Only resume into an episode the active daypart actually carries -
        # otherwise flipping back at midnight would replay the daytime show.
        if (
            self.tune_in_mode == "resume"
            and self._resume_path is not None
            and self._resume_path in pool
        ):
            return PlayRequest(path=self._resume_path, start=self._resume_position)

        if self.tune_in_mode == "broadcast":
            schedule = self._ensure_broadcast(key, pool, epoch=now)
            if schedule is not None:
                return schedule.at(now)
            # Fall through to random if the schedule could not be built.

        return self._next_episode(key, pool)

    def advance(self, *, now: Optional[float] = None) -> Optional[PlayRequest]:
        """Decide what to play when the current episode ends naturally."""
        now = self._wall_clock() if now is None else now
        index, part = self._active(now)
        if part is not None and part.off_air:
            return None

        key = self._pool_key(index, part)
        pool = self._pools.get(key) or []
        if not pool:
            return None

        if self.tune_in_mode == "broadcast":
            schedule = self._broadcasts.get(key)
            if schedule is not None:
                # Roll straight into whatever airs next in the running order.
                return schedule.at(now)
        return self._next_episode(key, pool)

    def peek_now(self, now: Optional[float] = None) -> Optional[Path]:
        """What this channel is airing right now - only if we already know.

        Used by the on-screen guide, which must stay cheap: this never builds a
        broadcast schedule (that would ffprobe every file on the channel), so it
        returns ``None`` for channels the viewer has not visited yet.
        """
        now = self._wall_clock() if now is None else now
        index, part = self._active(now)
        if part is not None and part.off_air:
            return None
        schedule = self._broadcasts.get(self._pool_key(index, part))
        return schedule.at(now).path if schedule is not None else None

    def remember(self, path: Path, position: float) -> None:
        """Record where the viewer left off (for the "resume" mode)."""
        self._resume_path = path
        self._resume_position = max(0.0, position)

    # -- broadcast schedule -------------------------------------------------
    def _ensure_broadcast(
        self, key: Optional[int], pool: Sequence[Path], *, epoch: float
    ) -> Optional[BroadcastSchedule]:
        existing = self._broadcasts.get(key)
        if existing is not None:
            return existing
        if not pool:
            return None
        durations: List[float] = []
        for path in pool:
            dur = probe_duration(path)
            durations.append(dur if dur else DEFAULT_EPISODE_SECONDS)
        # Probing a whole channel is slow, so persist what we learned; the next
        # boot reads it back instead of shelling out to ffprobe per file.
        flush_cache()
        # Use a channel-stable epoch offset so different channels are out of
        # phase with each other, but keep it deterministic per run.
        schedule = BroadcastSchedule(
            pool, durations, epoch=epoch, rng=self._rng, shuffle=self.shuffle
        )
        self._broadcasts[key] = schedule
        return schedule


class ChannelLineup:
    """An ordered set of channels with remote-style navigation."""

    def __init__(self, channels: Sequence[Channel]) -> None:
        if not channels:
            raise ValueError("a lineup needs at least one channel")
        # Present channels in ascending channel-number order, like a real tuner.
        self._channels: List[Channel] = sorted(channels, key=lambda c: c.number)
        self._by_number: Dict[int, Channel] = {c.number: c for c in self._channels}
        self._index = 0

    def __len__(self) -> int:
        return len(self._channels)

    def __iter__(self):
        return iter(self._channels)

    @property
    def current(self) -> Channel:
        return self._channels[self._index]

    @property
    def numbers(self) -> List[int]:
        return [c.number for c in self._channels]

    def has_number(self, number: int) -> bool:
        return number in self._by_number

    def index_of(self, number: int) -> Optional[int]:
        for i, ch in enumerate(self._channels):
            if ch.number == number:
                return i
        return None

    def up(self) -> Channel:
        self._index = (self._index + 1) % len(self._channels)
        return self.current

    def down(self) -> Channel:
        self._index = (self._index - 1) % len(self._channels)
        return self.current

    def select_number(self, number: int) -> Optional[Channel]:
        idx = self.index_of(number)
        if idx is None:
            return None
        self._index = idx
        return self.current

    def select_index(self, index: int) -> Channel:
        self._index = index % len(self._channels)
        return self.current


def build_lineup(
    config: Config,
    *,
    rng: Optional[random.Random] = None,
    wall_clock: Callable[[], float] = time.time,
) -> ChannelLineup:
    """Scan every configured channel folder (and daypart folder) into a lineup."""
    channels: List[Channel] = []
    for i, ch_cfg in enumerate(config.channels):

        def scan(root: Path) -> List[Path]:
            return scan_episodes(
                root,
                config.video_extensions,
                recursive=config.scan_recursive,
                exclude=ch_cfg.exclude,
                exclude_seasons=ch_cfg.exclude_seasons,
            )

        episodes = scan(ch_cfg.path)

        # A daypart that points at its own folder gets its own episode pool.
        daypart_episodes: Dict[int, List[Path]] = {}
        for index, part in enumerate(ch_cfg.dayparts):
            if part.path is None:
                continue
            found = scan(part.path)
            daypart_episodes[index] = found
            if not found:
                log.warning(
                    "channel %s (%s) daypart %s has no playable episodes in %s",
                    ch_cfg.number, ch_cfg.name, part.label, part.path,
                )

        if not episodes and not any(daypart_episodes.values()):
            log.warning(
                "channel %s (%s) has no playable episodes in %s",
                ch_cfg.number, ch_cfg.name, ch_cfg.path,
            )
        # Give each channel its own RNG stream so they shuffle independently
        # but reproducibly when a seed is configured.
        if config.shuffle_seed is not None:
            # Derive a distinct-but-deterministic integer seed per channel.
            ch_rng = random.Random(hash((config.shuffle_seed, ch_cfg.number, i)) & 0xFFFFFFFF)
        else:
            ch_rng = rng or random.Random()
        channels.append(
            Channel(
                ch_cfg,
                episodes,
                tune_in=config.tune_in,
                start_offset_min=config.start_offset_min,
                start_offset_max=config.start_offset_max,
                rng=ch_rng,
                daypart_episodes=daypart_episodes,
                wall_clock=wall_clock,
                shuffle=ch_cfg.shuffle,
            )
        )
    return ChannelLineup(channels)


__all__ = [
    "Channel",
    "ChannelLineup",
    "PlayRequest",
    "BroadcastSchedule",
    "scan_episodes",
    "detect_season",
    "build_lineup",
]
