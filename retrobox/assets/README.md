# Assets

Two different kinds of thing live here, and the difference matters.

## `boot_splash.mp4` — committed, and shipped

The JV Projects clip every box plays when it is switched on. This one **is** in
git, because it is branding: a Retro Box shows its own mark at power-up whether
or not anybody ran a setup script.

A CRT waking up — the line snapping open, the beam settling, phosphor warming to
full, then a fade into channel one. Product green (`#4DFF5A`), the same colour as
the on-screen display, so the two match by construction rather than by eye.

**The spec, and it is not negotiable:**

| | |
|---|---|
| container | mp4 |
| codec | h264 |
| resolution | 1920 x 1080 |
| pixel format | `yuv420p` |
| frame rate | 30/1 |
| frames | 120 |
| duration | 4.000 s |
| size | 116 KB (ceiling: 1 MB) |

`yuv420p` is the load-bearing one. Anything else plays perfectly on a laptop and
fails on a hardware decoder — which is the only machine that matters, and the one
you cannot reach. `tests/test_boot_splash.py` asserts every row of that table
against the committed file, skipping cleanly where there is no ffprobe.

### Turning it off

It is **on by default**. To send a box straight to channel one:

```yaml
boot_splash: false        # in config.yaml
```

or use Branding → *Play no splash* in the dashboard, which writes the same thing.

### Swapping in a different clip

Point `boot_splash` at your own file — a bare filename is looked for in this
folder, an absolute path is taken as-is:

```yaml
boot_splash: /media/retrobox/my_splash.mp4
```

Encode it to the spec above or the box's decoder may refuse it:

```bash
ffmpeg -i source.mov -an \
  -c:v libx264 -pix_fmt yuv420p -r 30 -s 1920x1080 \
  -profile:v high -movflags +faststart \
  boot_splash.mp4
```

`-an` is deliberate: the shipped clip is silent, so a splash cannot blast a room
at whatever volume the last person left the box on.

> The generator that produced the shipped clip has not landed in this repo.
> Regenerate against the table above, not from a script that isn't here.

### The safety net

`app.py` gives the splash a hard 30-second deadline, set from the clock the
moment it starts and from nothing the file says. A clip that is truncated, or
that the player never reports as finished, still hands over to channel one. A
bad splash therefore cannot brick a unit. **Do not weaken it** — it is the only
thing between a bad encode and a customer with a dead TV.

## `static.mp4`, `glitch.mp4`, `colorbars.mp4` — generated, not committed

The short filler clips the TV uses in between things:

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

## `fonts/`

`VT323-Regular.ttf`, the on-screen display face, under the SIL Open Font
License (`fonts/OFL.txt`).
