# Retro Box

**Turn a spare Linux box into the cable box you fell asleep in front of.**

Retro Box plays folders of video off a drive as if they were real TV
**channels**. Flip to a channel and something is already playing (starting a few
seconds in, like you just tuned in); when an episode ends, the next one rolls
automatically on an endless shuffle. Channels change with the clock, station
bumpers stitch the night together, a proper channel guide tells you what's on,
and a sleep timer turns the whole thing off when you don't. It boots straight to
the TV on power-up, is driven by a plain remote, sends audio over HDMI, and has
an authentic late-90s vibe — a green on-screen display and a curved "CRT"
picture. No menus, no apps, no accounts, no recommendations. Just a remote and
channels.

This guide has two parts:

1. [**What you need**](#1-what-you-need)
2. [**Step-by-step setup**](#2-step-by-step-setup) — flash it, install it, fill it with shows

---
## 1. What you need

Nothing exotic. This is meant to run on whatever old box you already have.

| | |
|---|---|
| **A machine** | Any x86 mini PC that will boot Linux — an ex-office desktop, a NUC, a Tiny/SFF thing off eBay — or a Raspberry Pi 4. 4 GB RAM is plenty, and a decade-old CPU is fine once hardware decode is on. |
| **An HDMI display** | A TV, ideally. Any monitor works. |
| **A [Flirc USB adapter](https://flirc.tv/products/flirc-usb-receiver) and a remote** | The Flirc teaches itself any IR remote and presents it to Linux as a plain keyboard. The remote can be anything — an old cable box remote, a cheap universal. |
| **Another computer** | Windows, Mac or Linux, to write the USB stick and SSH in. You won't need a keyboard on the box itself after first boot. |
| **A USB stick or SD card** | 8 GB+, to install from. |

For the remote, six buttons cover the basics (channel ±, volume ±, mute, power).
Four more unlock the rest — **guide**, **sleep**, **info** and **last channel** —
plus a number pad if you want to punch in channel numbers. Any junk-drawer TV
remote has all of them, which is rather the point.

---

## 2. Step-by-step setup

### Step 1 — Flash the OS

Install **Ubuntu Server** ([ubuntu.com/download/server](https://ubuntu.com/download/server))
on a PC, or **Raspberry Pi OS Lite** on a Pi. Debian works too. Write it with
[balenaEtcher](https://etcher.balena.io/) or Raspberry Pi Imager.

Whichever installer you use, do these three things during it:

- **Don't install a desktop.** Retro Box draws straight to the console via
  KMS/DRM, and a desktop environment will fight it for the screen.
- **Enable SSH**, so you can drive the rest from your own computer.
- **Set the hostname to `retrobox`** and pick a username and password.

> Raspberry Pi Imager hides hostname/SSH/user behind the gear icon before you
> write. Ubuntu Server's installer asks during setup.

### Step 2 — First boot and SSH in

Plug in HDMI, network and power, and let it boot to a text login prompt. The
box prints its IP address above that prompt — something like
`192.168.1.42`. Note it down, then, from your own computer:

```bash
ssh youruser@retrobox.local
# or, if .local doesn't resolve on your network:
ssh youruser@192.168.1.42
```

Accept the fingerprint the first time. The password prompt stays blank as you
type — that's normal, not a frozen terminal.

> `.local` needs mDNS. If it doesn't resolve, the IP always works, and
> `sudo apt install -y avahi-daemon` on the box fixes it for next time.

### Step 3 — Install

```bash
git clone <your-repo-url> ~/RetroBox
cd ~/RetroBox
./scripts/install.sh --service
```

That's the whole install. One script, no separate hardware step, no driver
hunting. It works out what it's running on and does the right thing:

- **Detects the platform** — Pi or PC — and installs the right `libmpv` for
  your distro, whose package name has changed across releases.
- **Detects the GPU on a PC** and installs the matching VA-API decode driver
  (Intel, AMD, or software decode on NVIDIA, which is the safe default).
  On a Pi it skips this entirely, since the Pi decodes through V4L2.
- **Sets up the LAN file share** so you can drag shows on from any PC
  (step 6). Pass `--no-share` if you'd rather it didn't.
- **Installs the on-screen font** and generates the static/glitch/colour-bar
  filler clips.
- **Creates a starter `config.yaml`** so nothing is left half-configured.

`--service` is optional but worth adding now: it installs the systemd unit so
the box boots straight to TV with no login. You can add it later with
`./scripts/install.sh --service`.

> **If `retrobox: command not found` in a later SSH session** — the command
> lives in the project's virtual environment, which a fresh shell doesn't know
> about. Either run `source ~/RetroBox/.venv/bin/activate` first, or call it by
> full path: `~/RetroBox/.venv/bin/retrobox`. The systemd service always uses
> the full path, so autostart is unaffected either way.

> No repo of your own yet? Copy the folder across instead:
> `rsync -av --exclude .venv RetroBox/ youruser@retrobox.local:~/RetroBox/`

### Step 4 — Build the channel lineup

```bash
retrobox --setup
```

The wizard asks for each channel's name and folder, counts the video files it
finds so you can catch a typo'd path immediately, and picks your HDMI audio
output on its own. It writes `config.yaml` for you.

Confirm it:

```bash
retrobox --check
```

That lists every channel with its episode count, any dayparts, your bumper
count and the sleep-timer ladder. A channel showing `NO EPISODES FOUND` has a
wrong path or an unrecognised file extension.

> Audio landed on the wrong output? `retrobox --list-audio` prints every device
> and you can paste the HDMI one into `audio_device` in `config.yaml`. You can
> also change it live from the on-screen menu.

### Step 5 — Get shows onto it

The installer already shared the library on your network, so this is drag and
drop.

**From Windows:** open File Explorer, click **Network** in the sidebar, and wait
up to a minute or two for **RETROBOX** to appear. Open it, open **Library**, and
drag your show folders straight in. No drive mapping, no path typing, no login
prompt.

**From Mac:** Finder → Go → Connect to Server → `smb://retrobox/Library`.

**From Linux:** `smb://retrobox/Library` in any file manager, or mount it with
`gio mount`.

One folder per channel is the whole convention:

```
/media/retrobox/
├── sitcoms/
│   ├── S01E01.mp4
│   └── S01E02.mp4
├── music-videos/
├── talk-shows/
└── movies/
```

Anything you drop lands in `/media/retrobox`, which is exactly where channels
are scanned from, so `retrobox --check` sees it straight away. See
[Dropping files on from another PC](#dropping-files-on-from-another-pc) if the
box doesn't show up under Network.

### Step 6 — Program the remote

This happens on **your computer**, not the box — the Flirc is programmed once
and remembers its mapping in firmware.

1. Unplug the Flirc from the box and plug it into your computer.
2. Install the Flirc app from [flirc.tv/downloads](https://flirc.tv/pages/downloads).
3. Choose the **Full Keyboard** controller.
4. Click a key on the on-screen keyboard, then press the remote button you want
   to use for it:

   | Click this key | Press this remote button | Does |
   |----------------|--------------------------|------|
   | **Up arrow (↑)**   | Channel-Up   | Channel up *(moves the highlight in the guide/menu)* |
   | **Down arrow (↓)** | Channel-Down | Channel down *(likewise)* |
   | **Right arrow (→)**| Volume-Up    | Volume up |
   | **Left arrow (←)** | Volume-Down  | Volume down |
   | **m**              | Mute         | Mute |
   | **p**              | Power        | Standby (blank the screen) |
   | **g**              | Guide        | On-screen channel guide |
   | **s**              | Sleep        | Cycle the sleep timer |
   | **i**              | Info         | Re-show the channel banner |
   | **l**              | Last / Back  | Jump to the previous channel |
   | **Enter**          | OK / Select  | Confirm |
   | **0**–**9**        | Number pad   | Type a channel number |

5. Plug the Flirc back into the box.

No config changes needed — those keys work as shipped. Any button can be
remapped later via `key_overrides` in `config.yaml`.

### Step 7 — Test it, and confirm autostart

```bash
retrobox
```

Channels should appear and respond to the remote. Press `q` on a keyboard, or
Ctrl+C over SSH, to stop.

If you passed `--service` back in step 3, autostart is already live — check it:

```bash
systemctl status retrobox
sudo reboot          # the real test: it should come up straight into TV mode
```

If you didn't, add it now:

```bash
cd ~/RetroBox && ./scripts/install.sh --service
```

Logs live in `journalctl -u retrobox -f` when something looks wrong.

### Step 8 — Things that work with no setup at all

Worth knowing about so you don't trip over them and assume something's broken:

- **The on-screen menu.** Press **Escape** on an attached keyboard for channels,
  volume, audio output, shutdown and an About screen. It pauses playback and
  resumes exactly where it left off. A mouse works while it's open — hover to
  highlight, click to pick — and the pointer is completely invisible and inert
  the rest of the time, whether or not a mouse is even plugged in. Escape used
  to quit the app; **`q` does that now.**
- **The boot splash.** A JV Projects clip ships in `retrobox/assets/`, played
  once at startup before the first channel. It's off until you ask for it —
  uncomment `boot_splash: boot_splash.mp4` in `config.yaml`. Any button skips it.

### Step 9 — Optional: read-only protection

Pulling the plug mid-write can corrupt the filesystem. Far less likely on an
SSD than a Pi's SD card, but if you want it to be impossible, make the root
read-only. The two platforms genuinely differ here, and `install.sh` prints
whichever one applies to the machine it ran on:

**On a PC (Debian/Ubuntu):**

```bash
sudo apt install overlayroot
# then set this line in /etc/overlayroot.conf and reboot:
#   overlayroot=tmpfs
```

Writes go to RAM and vanish on reboot. To make a real change later, run
`sudo overlayroot-chroot`, edit, and reboot.

**On a Raspberry Pi:**

```bash
sudo raspi-config     # Performance Options -> Overlay File System -> Enable
```

`raspi-config` doesn't exist on x86, and `overlayroot` isn't in Raspberry Pi OS
— use whichever matches your box.

**Done.** Power it on and it comes up as a television.

## Using it day to day

| Do this | On the remote |
|---------|---------------|
| Change channels | Channel up / down |
| Jump to a channel | Type its number |
| Adjust volume | Volume up / down |
| Mute | Mute |
| See what's on everywhere | **Guide** |
| Open the settings menu | **Escape** (keyboard) |
| Browse the guide | Channel up / down while it's open |
| Tune to a highlighted row | **OK** |
| Set a sleep timer | **Sleep** (cycles 30 → 60 → 90 → off) |
| Re-show the channel banner | Info |
| Bounce to the last channel | Last / Back |
| Standby (blank screen) | Power |
| **Turn off** (clean shutdown) | Volume-down again when already at 0 |

---

## The five things that make it feel like cable

### Dayparting — channels that change with the clock

Real cable never showed the same thing at 3pm and 3am. Give any channel a list
of wall-clock windows and it can rename itself, swap in a different folder, or
sign off entirely:

```yaml
  - number: 3
    name: "Music Videos"
    path: /media/retrobox/music-videos
    dayparts:
      - from: "22:00"
        to: "02:00"
        name: "120 MINUTES"                        # renames the channel...
        path: /media/retrobox/alt-rock-block   # ...and swaps the programming
      - from: "02:00"
        to: "06:00"
        off_air: true                              # colour bars, no programme
```

Windows use 24-hour local time and may wrap past midnight. They're tested in the
order you write them, so the first match wins; outside every window the channel
falls back to its own name and folder.

Boundaries take effect **immediately**, mid-episode — 22:00 arrives and the
channel changes under you, exactly like the real thing. A window that only
renames the channel (a `name` with no `path`) doesn't interrupt playback: the
episode keeps running and just the on-screen ident changes.

> Times are local to the box. Check it with `timedatectl`, and set it with
> `sudo timedatectl set-timezone Australia/Brisbane` (or wherever you are).

### Broadcast mode — channels that run whether you're watching or not

`tune_in` decides what happens the instant you land on a channel. The default,
`random`, starts a fresh episode a few seconds in. `resume` picks up where you
left that channel. `broadcast` is the one that feels like television:

```yaml
tune_in: broadcast
```

Each channel gets a fixed shuffled running order and a start time, and from then
on it advances against the wall clock — so a channel you haven't watched all
evening has still been "airing" the whole time, and tuning in drops you partway
through whatever would be on. Flip away and back ten minutes later and you've
missed ten minutes.

It needs `ffprobe` (the installer brings it in with ffmpeg) to measure how long
each episode runs. That measuring happens the first time you tune to a channel,
so a large channel takes a moment on its first visit; results are cached in
`~/.cache/retrobox/durations.json`, keyed on each file's size and modification
time, so it only ever happens once per file. Anything that can't be probed is
assumed to be a 22-minute slot and the schedule carries on regardless.

### Station bumpers — the glue between shows

Drop a folder of short clips (idents, "we'll be right back", station promos) and
one plays between episodes:

```yaml
bumpers: /media/retrobox/bumpers
bumper_chance: 1.0        # 0-1: how often one airs between episodes
bumper_max_seconds: 30    # hard cap so one long file can't stall the box
```

Bumpers are shuffled the same way episodes are, so you never get the same one
twice in a row, and they roll straight into the next show with no gap.

### The channel guide

Press **guide** for a green cable-box grid: every channel, its current name, and
what's on. Arrow up and down to browse it — the highlighted row is marked `>`,
and the channel still playing behind it is marked `*`. Press **OK** to tune to
the highlighted row. Off-air channels say so. The header carries the time and,
if one is running, the sleep timer. It clears itself after `guide_seconds`
(default 8), or when you press guide again.

### The on-screen menu

Press **Escape** on an attached keyboard for a settings overlay. (A remote's
`Menu` button works too if it has one, and `o` is a stdin-friendly alias for
when you're driving it over SSH.) It **pauses playback** and resumes exactly where it left off
when you close it.

| Row | Does |
|-----|------|
| **Channels** | The full lineup — jump straight to any channel, no stepping |
| **Volume** | Left/right adjusts; works from any row |
| **Audio output** | The HDMI outputs `hwdetect` finds. Applies **immediately, for this session** — it does not rewrite `config.yaml`, so a reboot returns to the configured device |
| **Shut down** | Confirms first, then shuts the machine down cleanly |
| **About** | JV Projects, and the installed version |

Drive it with the arrow keys and **OK**, or with a mouse — hover highlights a
row, click activates it. Back/Last steps out of a sub-screen; **Escape** closes
the menu from anywhere.

> **The box boots straight into channel mode with nothing on screen**, driven by
> the remote as always. The pointer is switched off and hidden from boot —
> whether or not a mouse is plugged in — and only becomes visible and clickable
> while the menu is open. Close the menu and it disappears completely.
>
> Escape used to quit the application. It now opens the menu; **`q` still
> quits.**

### The sleep timer

Press **sleep** to cycle 30 → 60 → 90 minutes → off. While it runs, a small
`SLEEP 29m` readout sits in the corner and counts down. When it runs out:

```yaml
sleep_timer: [30, 60, 90]   # your own ladder; `false` disables the button
sleep_action: standby       # standby (blank screen) | off (clean shutdown)
```

`sleep_action: off` is the good one for a box that lives next to a bed — it
shuts the machine down properly rather than leaving it idling all night.

---

## Dropping files on from another PC

The installer sets up an SMB share so the library is reachable from any machine
on the network, with no drive mapping and no login prompt.

**From Windows:** open File Explorer → **Network** → the box appears by hostname
→ open **Library** → drag folders in. That's it.

**From macOS:** Finder → Go → Connect to Server → `smb://retrobox/Library`.

Anything dropped in lands in `/media/retrobox`, which is where channels are
scanned from, so `retrobox --check` picks it up immediately.

```bash
./scripts/setup_lan_share.sh              # re-run any time
./scripts/setup_lan_share.sh --path /srv/media   # share somewhere else
./scripts/install.sh --no-share           # skip it at install time
```

> **The share is guest-writable** — that is what removes the login prompt.
> Anyone who can reach the box on the network can add and delete files in the
> library. That's the right trade on a home LAN; don't do it on a network you
> don't control.
>
## The web dashboard

The installer also puts a small dashboard on the box. Point any browser on the
network at it:

```
http://retrobox:8080/      (or http://<box-ip>:8080/)
```

You get what's playing now, the channel list with jump-to-channel, volume and
mute, standby, and a shutdown button — the same things the on-screen menu does,
in the same phosphor green, for when the remote is under a cushion.

It runs as its own service (`systemctl status retrobox-web`) and never touches
the running player: the TV writes a small status file every couple of seconds,
and the dashboard sends button presses back over a local socket that arrive as
ordinary remote events. If the TV process is stopped, the page says so rather
than lying to you.

> **Like the file share, there is no login.** Anyone who can reach the box on
> the network can change the channel or shut it down. Same trade, same caveat:
> fine on a home LAN, wrong anywhere you don't control. To turn it off:
> `sudo systemctl disable --now retrobox-web`.

> If the box never appears under **Network**, that's the discovery service, not
> the share: modern Windows dropped the old SMB1 browsing this used to rely on,
> so `wsdd` advertises the box instead. Typing `\\<box-ip>\Library` into the
> address bar will still work. Check with `systemctl status wsdd`.

## Updating later

There's no Git remote wired up yet. To update, either re-run the `rsync` from
[Step 3](#step-3--install), or set up your own repo once:

```bash
# on your computer, inside the project folder
git init && git add -A && git commit -m "Initial commit"
git remote add origin <your-repo-url> && git push -u origin main
```

...after which the box can just do:

```bash
cd ~/RetroBox
git pull
sudo systemctl restart retrobox
```

Either way, restart the service after updating. The install is editable, so code
changes take effect on restart with no reinstall.

---

## Configuration reference (highlights)

All settings live in `config.yaml`:

```yaml
tune_in: random          # random | resume | broadcast
start_channel: 2         # channel to power on to
start_offset: [6, 10]    # start each episode a random 6-10s in (or a fixed number)
transition: none         # channel-change effect: none | glitch | static
bridge_seconds: 0.8      # keep the current show playing while the next loads
channel_bug_seconds: 4   # how long the channel banner lingers
guide_seconds: 8         # how long the channel guide stays up
initial_volume: 70       # 0-100
audio_device: "..."      # force HDMI audio (see Part H)

bumpers: /path/to/clips  # station idents between episodes
sleep_timer: [30, 60, 90]
sleep_action: standby    # standby | off

ui:                      # the green on-screen display
  color: "#4DFF5A"       # lit text and volume bars
  dim_color: "#123B18"   # unlit volume dots
  glow: true
crt:                     # the CRT picture effect (curve, rounding, scanlines)
  enabled: true
  curvature: 0.12
```

Per-channel options:

```yaml
  - number: 3
    name: "Music Videos"
    path: /media/retrobox/music-videos
    shuffle: false              # play in filename order instead of shuffling
    exclude_seasons: ["6-25"]   # only air seasons 1-5
    exclude: ["*live aid*"]     # skip anything matching
```

Validate any changes with `retrobox --check`.

---

## Troubleshooting

- **`--check` shows 0 files for a channel** → the `path` is wrong, or the files
  use an extension not in `video_extensions`. If the media is on a USB drive,
  confirm it's mounted (`lsblk -f`).
- **No picture on the TV** → make sure you didn't install a desktop environment
  (it will hold the console). Check logs with `journalctl -u retrobox -f`.
  If mpv can't open a video output, try forcing DRM by hand to confirm:
  `mpv --vo=gpu --gpu-context=drm /path/to/a/file.mp4`.
- **Video stutters** → hardware decode probably isn't active. Run `vainfo`; if
  it reports no driver, install `intel-media-va-driver` (newer Intel) or
  `i965-va-driver` (pre-Skylake).
- **No sound** → see Part H; try the other `DEV=` numbers, or the
  `alsa/plughw:CARD=...` variant.
- **Remote does nothing** → confirm the Flirc is plugged into the box and was
  programmed (Part G). Restart the box after plugging it in.
- **The TV remote does nothing over CEC** → most x86 machines have no CEC
  hardware, but it costs nothing to check rather than assume. On the box run
  `echo scan | cec-client -s -d 1`. A device list back means CEC works and you
  can skip the Flirc entirely (`cec: true` under `input:` is already the
  default). "no device found", or a hang, means the HDMI port has no CEC pin
  wired, so you need a
  [USB-CEC adapter](https://www.pulse-eight.com/p/104/usb-hdmi-cec-adapter) or
  the Flirc. A Raspberry Pi passes CEC natively and needs neither.
- **A channel says OFF AIR when it shouldn't** → a daypart is signing it off.
  Run `retrobox --check` to see every window, and confirm the box's timezone
  with `timedatectl`.
- **Bumpers never play** → check `retrobox --check` reports a clip count,
  and that `bumper_chance` isn't 0.
- **`broadcast` mode is slow the first time** → it measures every file with
  ffprobe on first tune-in. Results are cached under
  `~/.cache/retrobox/durations.json`, so it only happens once per file.

---

## For the curious (how it works)

The project is plain Python. The "brains" (channel scanning, the shuffle, the
dayparting, the state machine) have no hardware dependencies and are fully
unit-tested; the hardware-facing parts (the mpv video player and the remote
input) are isolated behind small interfaces. You can drive the whole thing on a
laptop — macOS or Windows included — with a mock player:

```bash
pip install -e ".[dev]"
pytest                                                   # 212 tests, ~0.5s
python -m retrobox --dry-run --config config.yaml    # keyboard-controlled, no video
```

```
retrobox/
├── config.py      YAML -> validated config
├── menu.py        the on-screen menu (pure model, no display)
├── setup_wizard.py  interactive config builder (retrobox --setup)
├── hwdetect.py    GPU + HDMI audio detection (also runs standalone)
├── daypart.py     wall-clock windows (rename / swap / sign off)
├── playlist.py    shuffle bag + sequential order
├── channel.py     folder scanning, tune-in modes, channel navigation
├── probe.py       ffprobe duration lookup, cached to disk
├── player.py      mpv player (+ a mock for tests)
├── overlay.py     the green on-screen display and channel guide
├── crt.py         the CRT shader
├── input/         remote input (Flirc/keyboard, HDMI-CEC, keymap)
├── static_gen.py  ffmpeg-generated static/glitch/colour-bar clips
└── app.py         the TV state machine (channels, sleep timer, bumpers, guide)
```

The whole suite runs headless with no media, no display and no libmpv, so
`.github/workflows/ci.yml` exercises it on every push across Python 3.9–3.13
and shellchecks the installer scripts.

## License

MIT — see [`LICENSE`](LICENSE). Copyright © 2026 JV Projects.

This is a substantially modified derivative of the original Retro Box, whose
MIT notice is retained in `LICENSE` as that licence requires.

The bundled VT323 font (`retrobox/assets/fonts/`) is licensed separately
under the SIL Open Font License — see `OFL.txt` beside it, which must stay with
the font wherever it is redistributed.
