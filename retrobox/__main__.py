"""Command-line entry point: ``retrobox`` / ``python -m retrobox``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .channel import build_lineup, scan_episodes
from .config import Config, ConfigError, load_config

log = logging.getLogger("retrobox")

# Places we look for a config file when one isn't given explicitly.
_DEFAULT_CONFIG_LOCATIONS = (
    Path("config.yaml"),
    Path.home() / ".config" / "retrobox" / "config.yaml",
    Path("/etc/retrobox/config.yaml"),
)


def _find_config(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for candidate in _DEFAULT_CONFIG_LOCATIONS:
        if candidate.is_file():
            return candidate
    raise ConfigError(
        "no config file found. Pass --config PATH or create config.yaml "
        "(see config.example.yaml)."
    )


def _cmd_check(config: Config) -> int:
    """Validate the config and print the resulting channel lineup."""
    # Surface bad key_overrides here so typos are caught before running.
    from .input.keymap import parse_key_overrides

    try:
        overrides = parse_key_overrides(config.input_options.get("key_overrides"))
    except ValueError as exc:
        print(f"configuration error: {exc}")
        return 2

    lineup = build_lineup(config)
    print(f"Retro Box v{__version__} - configuration OK")
    print(f"tune-in mode: {config.tune_in}")
    if overrides:
        print(f"key overrides: {len(overrides)} configured")

    print(f"channels ({len(lineup)}):")
    total = 0
    for channel in lineup:
        count = len(channel.episodes)
        total += count
        dayparts = channel.config.dayparts
        # A channel can legitimately be empty by day if a daypart carries it.
        flag = "" if count or dayparts else "   <-- NO EPISODES FOUND"
        print(
            f"  CH {channel.number:>3}  {channel.config.name:<28}"
            f" {count:>4} episodes{flag}"
        )
        for index, part in enumerate(dayparts):
            label = part.name or channel.config.name
            if part.off_air:
                detail = f"{'-':>4} off air"
            else:
                found = len(channel.daypart_pool(index))
                if part.path is not None:
                    total += found
                detail = f"{found:>4} episodes"
                if not found:
                    detail += "   <-- NO EPISODES FOUND"
            print(f"      {part.label:<12} {label:<24} {detail}")
    print(f"total episodes: {total}")

    if config.bumpers_dir is not None:
        clips = scan_episodes(
            config.bumpers_dir, config.video_extensions, recursive=config.scan_recursive
        )
        flag = "" if clips else "   <-- NO CLIPS FOUND"
        print(f"station bumpers: {len(clips)} clips in {config.bumpers_dir}{flag}")

    if config.sleep_steps:
        ladder = " -> ".join(f"{m}m" for m in config.sleep_steps)
        print(f"sleep timer: {ladder} -> off  (on expiry: {config.sleep_action})")
    else:
        print("sleep timer: disabled")

    return 0 if total > 0 else 1


def _run_setup_wizard(config_path: Optional[str]) -> int:
    """Hand over to the interactive wizard, then exit.

    ``setup_wizard.main()`` builds its own argument parser and reads
    ``sys.argv`` directly, so it is given a private argv here rather than
    letting it trip over this CLI's own flags. ``-c/--config`` is forwarded as
    its ``--output`` so ``retrobox --setup -c /etc/retrobox/config.yaml``
    writes where you asked.
    """
    from . import setup_wizard

    argv = ["retrobox --setup"]
    if config_path:
        argv += ["--output", config_path]

    saved_argv = sys.argv
    try:
        sys.argv = argv
        setup_wizard.main()
    except SystemExit as exc:  # the wizard exits on its own for cancel/no-channels
        return int(exc.code or 0)
    except KeyboardInterrupt:
        print()
        log.info("setup cancelled")
        return 130
    finally:
        sys.argv = saved_argv
    return 0


def _list_audio_devices() -> int:
    """Print mpv's available audio output devices, one 'name  -  description' per line."""
    try:
        import mpv  # type: ignore
    except ImportError:
        print("python-mpv/libmpv not installed; on the box try: mpv --audio-device=help")
        return 1
    try:
        player = mpv.MPV(vo="null", idle=True)
        devices = player.audio_device_list or []
        print("Available audio devices (use the 'name' in config.yaml -> audio_device):\n")
        for dev in devices:
            name = dev.get("name", "?")
            desc = dev.get("description", "")
            print(f"  {name}\n      {desc}")
        print("\nFor a TV, pick an HDMI one, e.g. audio_device: \"alsa/hdmi:CARD=PCH,DEV=0\"")
        player.terminate()
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"could not list audio devices via libmpv ({exc}).")
        print("Try on the box instead: mpv --audio-device=help")
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="retrobox",
        description="A retro TV media player: fixed channels, shuffled episodes, dayparting.",
    )
    parser.add_argument("-c", "--config", help="path to the YAML config file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run without real hardware (mock player + keyboard/stdin control)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the config, list channels/episodes, and exit",
    )
    parser.add_argument(
        "--generate-assets",
        action="store_true",
        help="generate the static/colour-bars filler clips and exit",
    )
    parser.add_argument(
        "--list-audio",
        action="store_true",
        help="list available audio output devices (for the 'audio_device' setting) and exit",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="run the interactive setup wizard to write config.yaml, and exit",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging verbosity (default: INFO)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.generate_assets:
        from .static_gen import DEFAULT_ASSETS_DIR, main as gen_main

        return gen_main(["--assets-dir", str(DEFAULT_ASSETS_DIR)])

    if args.list_audio:
        return _list_audio_devices()

    if args.setup:
        return _run_setup_wizard(args.config)

    try:
        config_path = _find_config(args.config)
        log.info("loading config: %s", config_path)
        config = load_config(config_path)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    # Fold in any new folders under media_root before anything reads the
    # lineup, so --check and the TV itself see exactly the same channels.
    from .autochannels import apply_auto_channels

    config, added = apply_auto_channels(config, config_path)
    if added:
        log.info(
            "auto_channels: added %d channel(s): %s",
            len(added), ", ".join(f"{c.number} {c.name}" for c in added),
        )

    if args.check:
        return _cmd_check(config)

    from .app import run_from_config

    try:
        return run_from_config(config, dry_run=args.dry_run)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
