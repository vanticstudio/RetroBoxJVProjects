"""Reading and rewriting config.yaml from the dashboard.

``autochannels`` persists by splicing text into the ``channels:`` block, which
keeps every comment in the file byte-for-byte intact. That works because it only
ever appends. It cannot express a rename, a delete or a renumber, so the
dashboard needs a real round trip: parse the YAML into a structure, change the
structure, write it back out.

The cost of that is comments and hand formatting: PyYAML has no idea a comment
was ever there, so a rewritten config comes back clean and unadorned. That is an
acceptable trade for a box that is now managed through a UI rather than an
editor, and it is exactly why the once-only ``config.yaml.bak`` from
:mod:`retrobox.configwrite` matters - it is the annotated original, kept from
before the first rewrite, forever.

Two rules hold everything else up:

* **Validate the text, not the structure.** What the box boots from is YAML
  text, so the text that is about to be written is dumped, parsed back and run
  through the ordinary config loader first. If that fails, nothing is written
  and the old file stays exactly where it is. A box that will not boot is not
  recoverable in the field.
* **One writer at a time.** Flask serves with threads, so the whole
  read-modify-write cycle is serialised. Without that, two people on two phones
  can each read the same config and the second write silently erases the first.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .config import ChannelConfig, Config, ConfigError, config_from_dict
from .configwrite import write_config_text

log = logging.getLogger(__name__)

Mutator = Callable[[Dict[str, Any]], Any]


def channel_dict(channel: ChannelConfig) -> Dict[str, Any]:
    """A channel as plain YAML-safe scalars, ready to be written back."""
    entry: Dict[str, Any] = {
        "number": channel.number,
        "name": channel.name,
        "path": str(channel.path),
    }
    if not channel.shuffle:
        entry["shuffle"] = False
    if channel.exclude:
        entry["exclude"] = list(channel.exclude)
    if channel.exclude_seasons:
        entry["exclude_seasons"] = sorted(channel.exclude_seasons)
    if not channel.bumpers:
        entry["bumpers"] = False
    if channel.dayparts:
        from .schedule import to_config

        entry["dayparts"] = to_config(channel.dayparts)
    return entry


def _yamlify(value: Any) -> Any:
    """Coerce a structure into things ``yaml.safe_dump`` will write.

    A ``Path`` is the easy mistake to make from a route, and safe_dump refuses
    it outright rather than writing a ``!!python/object`` tag that safe_load
    would then refuse to read back. Tuples and sets get the same treatment,
    since config.py hands those out for several settings.
    """
    if isinstance(value, dict):
        return {str(k): _yamlify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_yamlify(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


class ConfigStore:
    """The single place config.yaml is read and written by the dashboard."""

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path).expanduser()
        # Re-entrant so a mutator can call back into load() without deadlocking.
        self._lock = threading.RLock()

    # -- reading ------------------------------------------------------------
    def load(self) -> Config:
        """The validated config, exactly as the rest of the box sees it."""
        with self._lock:
            return config_from_dict(self._read(), base_dir=self.path.parent)

    def _read(self) -> Dict[str, Any]:
        import yaml

        if not self.path.is_file():
            raise ConfigError(f"configuration file not found: {self.path}")
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"could not parse YAML in {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("configuration root must be a mapping")
        return data

    # -- writing ------------------------------------------------------------
    def update(self, mutate: Mutator) -> Config:
        """Apply ``mutate`` to the config and persist it, or change nothing.

        ``mutate`` is handed the raw structure with every channel written out
        explicitly and addressable by number. Raising from it - or producing
        something that will not load - aborts the whole thing with the file on
        disk untouched.
        """
        import yaml

        with self._lock:
            data = self._addressable()
            mutate(data)

            text = yaml.safe_dump(
                _yamlify(data), sort_keys=False, allow_unicode=True,
                default_flow_style=False,
            )
            # Prove the *text* loads, not just the dict it came from: a name
            # like "No" or "22:00" can dump as a scalar that parses back as
            # something else entirely, and the box boots from the text.
            config = self._validate(text)

            write_config_text(self.path, text)
            log.info("config rewritten: %d channels", len(config.channels))
            return config

    def replace(self, text: str) -> Config:
        """Put a whole new document in place of the one on disk.

        Restoring a config from a file and the factory reset do not edit the
        config, they replace it outright, so there is nothing to read first and
        :meth:`update` does not fit. They still have to take the same lock.
        Without it: somebody restores config.yaml from a laptop at the moment
        somebody else renames a channel on a phone, the rename has already read
        the old file, and it writes that whole stale copy back over the restore
        a heartbeat later. The restored config is gone and both people were
        told it saved. That is the same lost update the lock has always been
        here for; it is just that a write that goes round the store cannot be
        stopped by it.

        The text is written exactly as given, byte for byte, because what these
        callers are putting back is somebody's own file - comments and all.
        """
        with self._lock:
            # The same rule as update(): prove the text loads before it is
            # allowed to become the file the box boots from.
            config = self._validate(text)
            write_config_text(self.path, text)
            log.info("config replaced wholesale: %d channels", len(config.channels))
            return config

    def _validate(self, text: str) -> Config:
        import yaml

        try:
            reparsed = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"the rewritten config is not valid YAML: {exc}") from exc
        return config_from_dict(reparsed, base_dir=self.path.parent)

    def _addressable(self) -> Dict[str, Any]:
        """The raw config with every channel present, numbered and named.

        Two things the loader does implicitly have to be made explicit before
        anything can edit one channel by number:

        * a config with only ``media_root`` has no ``channels:`` key at all -
          the lineup is discovered from the folders. Writing back a list with
          one edited channel in it would delete every other channel on the box.
        * a hand-written entry may omit ``number`` or ``name``, which the loader
          fills in from its position and its folder.
        """
        data = self._read()
        parsed = config_from_dict(data, base_dir=self.path.parent)
        raw = data.get("channels")

        if not isinstance(raw, list):
            data["channels"] = [channel_dict(c) for c in parsed.channels]
            return data

        for entry, channel in zip(raw, parsed.channels):
            if isinstance(entry, dict):
                entry.setdefault("number", channel.number)
                entry.setdefault("name", channel.name)
        return data

    # -- convenience for the routes ----------------------------------------
    def channel_entry(self, data: Dict[str, Any], number: int) -> Optional[Dict[str, Any]]:
        """The raw entry for a channel number, or None if there isn't one."""
        for entry in self.channels_of(data):
            if int(entry.get("number", -1)) == number:
                return entry
        return None

    @staticmethod
    def channels_of(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw = data.get("channels")
        return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


__all__ = ["ConfigStore", "channel_dict"]
