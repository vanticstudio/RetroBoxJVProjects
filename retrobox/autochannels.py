"""Turn new folders in the media root into channels, automatically.

With ``auto_channels: true`` the box scans the media root at start-up and adds
a channel for any top-level folder that isn't already one. This is the
counterpart to dropping a folder on over the LAN share: copy a show across,
restart, and it has a channel number.

It is deliberately additive and never destructive. A folder already claimed by
a channel is left completely alone - a name or number set by hand, or by
``retrobox --setup``, is never rewritten. Empty folders are skipped rather than
becoming dead channels.

Newly found channels are written back into config.yaml so they persist and turn
up in ``retrobox --check``. That write is surgical: the new entries are spliced
into the existing ``channels:`` block and the rest of the file - comments and
all - is left byte-for-byte intact, because round-tripping YAML through a dumper
would throw away every comment in the example config.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .channel import scan_episodes
from .config import ChannelConfig, Config, _prettify_name

log = logging.getLogger(__name__)

# Marker written above appended entries so it is obvious where they came from.
MARKER = "# --- added automatically by auto_channels ---"


def discover_new_channels(config: Config) -> List[ChannelConfig]:
    """Folders under the media root that deserve a channel and don't have one.

    Returns them numbered from the first free channel number upwards, in
    case-insensitive folder order, so two runs over the same tree agree.
    """
    root = config.media_root
    if root is None:
        log.debug("auto_channels is on but no media_root is configured")
        return []
    if not root.is_dir():
        log.warning("auto_channels: media_root does not exist: %s", root)
        return []

    claimed = {_key(ch.path) for ch in config.channels}
    # A daypart can point at its own folder; those are already "in use" too, so
    # they must not also become a channel of their own.
    for channel in config.channels:
        for part in channel.dayparts:
            if part.path is not None:
                claimed.add(_key(part.path))

    next_number = _next_free_number(config)
    found: List[ChannelConfig] = []

    for folder in sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    ):
        if _key(folder) in claimed:
            continue
        episodes = scan_episodes(
            folder, config.video_extensions, recursive=config.scan_recursive
        )
        if not episodes:
            log.info("auto_channels: skipping %s (no video files)", folder.name)
            continue
        found.append(
            ChannelConfig(
                number=next_number,
                name=_prettify_name(folder.name),
                path=folder,
            )
        )
        log.info(
            "auto_channels: new channel %d %r (%d episodes)",
            next_number, found[-1].name, len(episodes),
        )
        next_number += 1

    return found


def apply_auto_channels(
    config: Config, config_path: Optional[Path] = None
) -> Tuple[Config, List[ChannelConfig]]:
    """Discover new folders, fold them into ``config``, and persist them.

    Returns the (possibly unchanged) config and the list of what was added.
    Persisting is best-effort: on a read-only root the channels still work for
    this session, they just won't be remembered.
    """
    if not config.auto_channels:
        return config, []

    new_channels = discover_new_channels(config)
    if not new_channels:
        return config, []

    merged = config.with_channels(list(config.channels) + new_channels)

    if config_path is not None:
        try:
            write_channels(config_path, new_channels)
        except OSError:
            log.warning(
                "auto_channels: could not write %s; the new channels work for "
                "this session but will be rediscovered next start",
                config_path, exc_info=True,
            )
    return merged, new_channels


def write_channels(config_path: Path, channels: Sequence[ChannelConfig]) -> None:
    """Splice ``channels`` into the ``channels:`` block of a config file."""
    if not channels:
        return
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    block = []
    if MARKER not in text:
        block.append(f"  {MARKER}")
    for channel in channels:
        block.extend(
            [
                f"  - number: {channel.number}",
                f'    name: "{channel.name}"',
                f"    path: {channel.path}",
            ]
        )

    insert_at = _end_of_channels_block(lines)
    if insert_at is None:
        # No `channels:` key at all (a media_root-only config). Start one.
        lines.append("")
        lines.append("channels:")
        insert_at = len(lines)

    lines[insert_at:insert_at] = block
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _end_of_channels_block(lines: List[str]) -> Optional[int]:
    """Index just past the last entry of the top-level ``channels:`` list."""
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "channels:" and not line.startswith((" ", "\t")):
            start = i
            break
    if start is None:
        return None

    end = start + 1
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue  # blank lines and comments may sit inside the block
        if lines[i].startswith((" ", "\t")):
            end = i + 1
        else:
            break  # dedented to another top-level key: the list is over
    return end


def _next_free_number(config: Config) -> int:
    numbers = [ch.number for ch in config.channels]
    if not numbers:
        return config.first_channel_number
    return max(numbers) + 1


def _key(path: Path) -> str:
    """Compare folders by resolved path so ./x and /abs/x match."""
    try:
        return str(path.resolve())
    except OSError:  # pragma: no cover - resolve() is forgiving in practice
        return str(path)


__all__ = ["apply_auto_channels", "discover_new_channels", "write_channels", "MARKER"]
