# Generated assets

This folder holds the short filler clips the TV uses:

- `static.mp4` — analog "snow" shown briefly when changing channels
  (`transition: static`).
- `glitch.mp4` — a burst of digital corruption, the other channel-change effect
  (`transition: glitch`).
- `colorbars.mp4` — SMPTE colour bars, looped for channels that have nothing to
  play and for channels a daypart has taken off the air.

These are **generated with ffmpeg**, not committed to git. Create them with:

```bash
retrobox --generate-assets
# or
python -m retrobox.static_gen
```

`scripts/install.sh` runs this for you during setup. If the files are missing at
runtime the box still works — channel changes just cut straight over, and a
channel with nothing to play shows a green `NO SIGNAL` message over a black
screen instead of colour bars.
