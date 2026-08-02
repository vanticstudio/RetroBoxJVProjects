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
would throw away every comment in the example config. It also goes through
:mod:`retrobox.configwrite`, so a power cut mid-write cannot leave the box with
a truncated config it will not boot from.

Splicing, and not a whole-document rewrite
------------------------------------------
This runs at every start on a config a person may well have written and
commented. Loading the document and dumping it back - what
:mod:`retrobox.configstore` does for the dashboard - would silently strip every
comment out of a customer's file the first time a new folder appeared, without
anybody asking for anything. The dashboard can afford that because somebody
pressed a button and ``config.yaml.bak`` keeps the annotated original; a
background scan cannot. So the text is spliced, not rebuilt.

What splicing must not become is hand-written YAML. Channel names and paths
here come from FOLDER NAMES under the media root, and anybody on the LAN can
create a folder there - the dashboard has no password on it. A name with a
newline in it, pasted into the file, lands at column zero as a *top-level key*,
and ``power_off_command`` is a top-level key this box turns into an argv it
runs. So the entries themselves are built as data and handed to the YAML
serialiser (the same rule as :mod:`retrobox.netconf` and the factory reset),
and only the already-quoted result is spliced in.

Because the splice happens outside the serialiser's sight, the finished text is
then parsed back and compared against what it was meant to say: the old
document, plus exactly these channels, and nothing else. If it does not match -
a ``channels: [...]`` flow list is the real case - nothing is written at all.
Not persisting costs a rediscovery at the next start; writing a document that
means something else costs the customer their lineup.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .channel import scan_episodes
from .config import (
    ChannelConfig,
    Config,
    ConfigError,
    _prettify_name,
    location_refusal,
)
from .configstore import channel_dict
from .configwrite import write_config_text

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
        # is_dir() above followed symlinks, and this writes what it finds back
        # into config.yaml - so a link planted in the media root would become a
        # permanent channel pointed anywhere on the disk. Same rule the loader
        # applies to a channel folder, applied at the same place the loader
        # would have applied it.
        reason = location_refusal(folder)
        if reason:
            log.warning(
                "auto_channels: not making a channel of %s - %s", folder.name, reason
            )
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
        except (OSError, ConfigError):
            # Two different failures, one answer. The disk may be read-only, or
            # the file may be shaped in a way this cannot splice into faithfully
            # - and in both cases the honest thing is to leave the customer's
            # config exactly as they left it. The channels still work for this
            # session and are rediscovered at the next start.
            log.warning(
                "auto_channels: could not write %s; the new channels work for "
                "this session but will be rediscovered next start",
                config_path, exc_info=True,
            )
    return merged, new_channels


def write_channels(config_path: Path, channels: Sequence[ChannelConfig]) -> None:
    """Splice ``channels`` into the ``channels:`` block of a config file.

    Raises :class:`~retrobox.config.ConfigError` - having written nothing at
    all - if the result would not read back as the file that was there plus
    exactly these channels. See the module docstring.
    """
    if not channels:
        return
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    found = _channels_block(lines)
    if found is None:
        # No `channels:` key at all (a media_root-only config). Start one.
        lines.append("")
        lines.append("channels:")
        insert_at, indent = len(lines), "  "
    else:
        insert_at, indent = found

    # Data first, serialiser second. Everything a folder name could do to this
    # file - a double quote, a newline, ": ", a leading "-", a "#" - is a
    # quoting question, and the quoting questions belong to PyYAML, which knows
    # all of the answers. There is no f-string anywhere below.
    entries = [channel_dict(channel) for channel in channels]

    block: List[str] = []
    if MARKER not in text:
        block.append(indent + MARKER)
    for entry in entries:
        block.extend(_entry_lines(entry, indent))

    lines[insert_at:insert_at] = block
    updated = "\n".join(lines) + "\n"

    _refuse_unless_only_these_channels_were_added(text, updated, entries)

    # Never a plain write_text: that truncates the user's config the instant it
    # opens it, and this box gets switched off at the wall. See configwrite.
    write_config_text(config_path, updated)


def _entry_lines(entry: Dict[str, Any], indent: str) -> List[str]:
    """One channel as YAML list-item lines, indented to sit in the block."""
    import yaml

    try:
        dumped = yaml.safe_dump(
            [entry], sort_keys=False, allow_unicode=True, default_flow_style=False,
            # Never wrap. A line the dumper folded is still correct YAML, but it
            # is correct relative to the indentation the dumper chose, and the
            # next step shifts all of that sideways. Keeping every scalar on one
            # line keeps the shift a shift.
            width=10 ** 9,
        )
    except yaml.YAMLError as exc:
        # safe_dump refuses anything it cannot write plainly - a Path is the
        # easy mistake, which is why configstore has _yamlify - and this runs
        # while the box is starting up. A caller's bad channel has to come back
        # as "not saved", which is survivable, rather than as an exception out
        # of the boot path, which is a black screen in somebody's living room.
        raise ConfigError(
            f"this channel cannot be written to the config: {exc}"
        ) from exc
    # Shift the whole item right to line up with the entries already in the
    # block. Blank lines stay blank: inside a quoted scalar an empty line *is*
    # the newline in the value, and padding it with spaces would be editing it.
    return [indent + line if line.strip() else line for line in dumped.splitlines()]


def _refuse_unless_only_these_channels_were_added(
    before_text: str, after_text: str, entries: Sequence[Dict[str, Any]]
) -> None:
    """Read the finished document back and check it says what we meant.

    The serialiser guarantees each entry; nothing guarantees that pasting those
    entries at a chosen line joins them onto the right list. A ``channels:``
    written as a one-line flow list is the case that bites: the block-finder
    does not recognise it, the entries get appended under a second ``channels:``
    key, the last key wins, and the channel the customer set up by hand is gone
    from the box with no error anywhere. So compare, and refuse.
    """
    before = _mapping(before_text, "the config on disk")
    after = _mapping(after_text, "the config this was about to write")

    existing = before.get("channels")
    if existing is not None and not isinstance(existing, list):
        raise ConfigError(
            "'channels' in this config is not a list, so new channels cannot be "
            "added to it"
        )

    expected = dict(before)
    expected["channels"] = list(existing or []) + list(entries)
    if after != expected:
        raise ConfigError(
            "adding these channels would have changed the rest of the config, "
            "so nothing was written"
        )


def _mapping(text: str, what: str) -> Dict[str, Any]:
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{what} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{what} is not a mapping")
    return data


def _channels_block(lines: List[str]) -> Optional[Tuple[int, str]]:
    """Where new entries go, and the indentation they have to be written at.

    A block sequence may sit at the same column as its ``channels:`` key or
    further in, but every item of one list has to agree with the others, so
    the indentation already in the file is the only correct answer. Both
    spellings turn up in hand-written configs, and "further in" cannot be
    assumed: a list at the left margin is ordinary YAML, and adding an indented
    entry to it produces a document that will not parse at all.

    The indentation is read off the *first* item, not the last, because a
    channel entry can contain lists of its own - ``exclude:``, ``dayparts:`` -
    at a deeper indent, and those are not this list.
    """
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "channels:" and not line.startswith((" ", "\t")):
            start = i
            break
    if start is None:
        return None

    end = start + 1
    indent: Optional[str] = None
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue  # blank lines and comments may sit inside the block
        body = lines[i].lstrip(" \t")
        lead = lines[i][: len(lines[i]) - len(body)]
        is_item = body == "-" or body.startswith("- ")

        if indent is None:
            if not is_item:
                break  # not a block sequence at all; nothing here to add to
            indent = lead
        elif is_item and lead == indent:
            pass  # the next channel along
        elif len(lead) > len(indent) and lead.startswith(indent):
            pass  # still inside the entry above - its name, its dayparts
        else:
            break  # dedented back out: the list is over
        end = i + 1

    # An empty `channels:` with nothing under it yet has no house style to
    # follow, so use the one the rest of the shipped config is written in.
    return end, indent if indent is not None else "  "


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
