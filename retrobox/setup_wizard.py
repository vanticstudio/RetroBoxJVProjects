"""
Interactive setup wizard for Retro Box.

Walks through adding channels and picking the audio device, then writes
config.yaml directly as plain YAML. Deliberately doesn't go through any
internal config classes, so it works regardless of how config.py is
structured, drop it in and wire up a CLI flag to call
setup_wizard.main() whenever that's convenient.

Usage:
    python3 -m retrobox.setup_wizard [--output config.yaml]
"""

import argparse
import os
import sys

import yaml

try:
    from retrobox import hwdetect
except ImportError:
    hwdetect = None

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".m4v", ".mov")


def count_videos(path):
    total = 0
    for _, _, files in os.walk(path):
        total += sum(1 for f in files if f.lower().endswith(VIDEO_EXTENSIONS))
    return total


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer if answer else default


def collect_channels():
    channels = []
    next_number = 2
    print("\nAdd your channels one at a time. Leave the name blank when done.\n")

    while True:
        name = ask(f"Channel {next_number} name (blank to finish)")
        if not name:
            break

        number = ask("Channel number", default=str(next_number))
        path = ask("Folder path for this show")

        if not path or not os.path.isdir(path):
            print(f"  Can't find that folder ({path}), skipping, check the path and try again.")
            continue

        found = count_videos(path)
        print(f"  Found {found} video file(s) in that folder.")
        if found == 0:
            confirm = ask("  No videos found, add it anyway? y/n", default="n")
            if confirm.lower() != "y":
                continue

        channels.append({
            "number": int(number),
            "name": name,
            "path": path,
        })
        next_number = int(number) + 1

    return channels


def pick_audio_device():
    if hwdetect is None:
        print("\nCouldn't import the hardware detection module, skipping audio auto detect.")
        return ask("Audio device (leave blank to use the system default)", default=None)

    devices = hwdetect.detect_audio()
    if not devices:
        print("\nNo HDMI audio device found automatically.")
        return ask("Audio device (leave blank to use the system default)", default=None)

    print("\nHDMI audio device(s) found:")
    for i, device in enumerate(devices):
        print(f"  {i + 1}. {device}")

    choice = ask(f"Pick one (1-{len(devices)}), or blank to use #1", default="1")
    try:
        return devices[int(choice) - 1]
    except (ValueError, IndexError):
        return devices[0]


def build_config(channels, audio_device):
    config = {
        "channels": channels,
        "tune_in": "random",
        "start_channel": channels[0]["number"] if channels else 2,
        "start_offset": [6, 10],
        "transition": "none",
        "bridge_seconds": 0.8,
        "channel_bug_seconds": 4,
        "initial_volume": 70,
    }
    if audio_device:
        config["audio_device"] = audio_device
    return config


def main():
    parser = argparse.ArgumentParser(description="Interactive Retro Box setup wizard")
    parser.add_argument("--output", default="config.yaml", help="Where to write the config file")
    args = parser.parse_args()

    if os.path.exists(args.output):
        confirm = ask(f"{args.output} already exists, overwrite it? y/n", default="n")
        if confirm.lower() != "y":
            print("Left the existing file alone.")
            sys.exit(0)

    channels = collect_channels()
    if not channels:
        print("\nNo channels added, nothing to write.")
        sys.exit(1)

    audio_device = pick_audio_device()
    config = build_config(channels, audio_device)

    with open(args.output, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False)

    print(f"\nWrote {len(channels)} channel(s) to {args.output}.")
    print("Run 'retrobox --check' next to confirm everything scans correctly.")


if __name__ == "__main__":
    main()
