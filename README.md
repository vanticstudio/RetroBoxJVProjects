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

1. [**The hardware you'll need**](#1-hardware)
2. [**Step-by-step setup**](#2-step-by-step-setup) — Linux, the terminal, and the programming

---

## 1. Hardware

| Part | Notes |
|------|-------|
| **A Linux mini PC** | Anything with an HDMI out and 4 GB RAM: an Intel NUC, a ThinkCentre/OptiPlex Tiny, a small form-factor desktop, or a Raspberry Pi 4. Even a decade-old Intel box is comfortable at 1080p once hardware decode is on, which the installer sets up for you. |
| **[Flirc USB Receiver](https://flirc.tv/products/flirc-usb-receiver)** | Plugs into the box and lets **any** IR remote control it. |
| **A remote** | Any universal/learning remote. See the note below on how many buttons you want. |
| **HDMI cable** | Standard full-size HDMI, straight to the TV. |
| **A USB stick, 4 GB+** | To install Linux. You only need it once. |

> **On the remote:** the basics need six buttons (channel ±, volume ±, mute,
> power). To get the good stuff you want four more — **guide**, **sleep**,
> **info**, and **last channel** — plus a number pad if you want to type channel
> numbers. Almost any old cable/TV remote has all of these, which is the point.

**You'll also need:**

- A **TV with an HDMI port**.
- A **computer** (Mac or Windows) to write the USB stick, program the remote,
  and copy files across.
- Your **video files** (e.g. `.mp4`/`.mkv` you own). These can live on the
  box's internal disk or on a USB drive left plugged in.

> **HDMI-CEC note:** on a Raspberry Pi the TV's own remote works over CEC out of
> the box. Most x86 machines have no CEC hardware, so you generally need a
> [Pulse-Eight USB-CEC adapter](https://www.pulse-eight.com/p/104/usb-hdmi-cec-adapter)
> for that. It's optional — the Flirc route works without it. The installer
> still installs `cec-utils`, and Retro Box skips the CEC backend silently
> when no adapter is present.

---

## 2. Step-by-step setup

### Part A — Put Linux on the box

1. On your computer, download **Debian 12 (Bookworm)** —
   [debian.org/download](https://www.debian.org/download) — or **Ubuntu Server** —
   [ubuntu.com/download/server](https://ubuntu.com/download/server). Either works.
2. Write it to the USB stick with [balenaEtcher](https://etcher.balena.io/)
   (Mac/Windows) or Raspberry Pi Imager's "Use custom" option.
3. Plug the stick, keyboard and HDMI into the box and power it on. Tap the
   boot-menu key at the logo (**F10** on most NUCs, **F12** on Dell/Lenovo)
   and pick the USB stick. On a Raspberry Pi, flash Raspberry Pi OS Lite with
   Raspberry Pi Imager instead and skip to Part B.
4. Install, and when asked what software to include:
   - **Do NOT install a desktop environment.** Retro Box draws straight to
     the console via KMS/DRM; a desktop just gets in the way.
   - **Do** install the **SSH server** so you can drive it from your computer.
   - Set a hostname of `retrobox` and remember your username and password.
5. Remove the stick and let it reboot to a text login prompt.

### Part B — Connect to it from your computer

- **Mac:** open the **Terminal** app.
- **Windows:** open **PowerShell**.

```bash
ssh youruser@retrobox.local
```

- The first time, type `yes` to accept.
- Enter your password (the screen stays blank while you type — that's normal).

> If `retrobox.local` doesn't resolve, find the box's IP from your router
> and use `ssh youruser@THAT.IP.ADDRESS` instead. (`.local` needs mDNS; on
> Debian install it with `sudo apt install -y avahi-daemon`.)

### Part C — Copy Retro Box onto the box

From **your computer**, in the folder *containing* the RetroBox directory:

```bash
rsync -av --exclude .venv --exclude __pycache__ \
  RetroBox/ youruser@retrobox.local:~/RetroBox/
```

(No rsync on Windows? Use `scp -r RetroBox youruser@retrobox.local:~/`
or copy it over on the USB stick.)

Later, once you've pushed this to your own Git repo, you can swap that for a
plain `git clone <your-repo-url>` on the box — see
[Updating later](#updating-later).

### Part D — Install

Back in the SSH session on the box:

```bash
cd ~/RetroBox
./scripts/install.sh
```

The installer sets up everything: the media player (mpv), video tools (ffmpeg),
hardware video decode, the retro font, and all Python dependencies. It detects
whether it is on an x86 machine or a Raspberry Pi and installs accordingly, and
picks the right `libmpv` package for your distro automatically. It takes a few
minutes. It's done when you see **"==> Done!"**.

### Part E — Load your programming

Put each channel's content in its **own folder**, one folder per channel:

```
/media/retrobox/
├── sitcoms/
│   ├── S01E01.mp4
│   └── S01E02.mp4
├── music-videos/
├── talk-shows/
└── movies/
```

Copy them across the same way you copied the project:

```bash
rsync -av --progress /path/to/your/videos/ \
  youruser@retrobox.local:/media/retrobox/
```

Any common video format works (`.mp4`, `.mkv`, `.avi`, `.m4v`, …), and season
sub-folders are fine.

> Using a USB drive for media? Give it a permanent mount point in
> `/etc/fstab` (by `UUID=`, which you can read with `lsblk -f`), or the box may
> come up before the drive does and find empty channels.

### Part F — Set up your channels

The quickest way is the built-in wizard — it asks for each channel's name and
folder, counts the video files it finds, auto-detects your HDMI audio output,
and writes `config.yaml` for you:

```bash
retrobox --setup
```

Prefer to edit by hand? The installer already left a `config.yaml` from the
template:

```bash
nano config.yaml
```

A minimal example (see [`config.example.yaml`](config.example.yaml) for every
option):

```yaml
channels:
  - number: 2
    name: "Sitcoms"
    path: /media/retrobox/sitcoms
  - number: 3
    name: "Music Videos"
    path: /media/retrobox/music-videos

tune_in: random          # something is already playing when you flip over
start_offset: [6, 10]    # begin each show 6-10 seconds in (skips the intro)
```

Save in nano with **Ctrl+O**, Enter, then exit with **Ctrl+X**. Check it:

```bash
retrobox --check
```

This lists your channels, their dayparts, how many files it found in each, your
bumper count, and the sleep-timer ladder.

### Part G — Program the remote (Flirc)

The **Flirc** receiver learns your remote and turns its buttons into keys
Retro Box understands. Do this **on your computer**:

1. Unplug the Flirc from the box and plug it into your computer.
2. Install the **Flirc** app from [flirc.tv/downloads](https://flirc.tv/pages/downloads).
3. In the app, choose the **Full Keyboard** controller.
4. Click a key on the on-screen keyboard, then press the button on your remote
   you want to use for it. Map these:

   | Click this on-screen key | Press this remote button | Does |
   |--------------------------|--------------------------|------|
   | **Up arrow (↑)**   | Channel-Up     | Channel up *(moves the highlight when the guide is open)* |
   | **Down arrow (↓)** | Channel-Down   | Channel down *(likewise)* |
   | **Right arrow (→)**| Volume-Up      | Volume up |
   | **Left arrow (←)** | Volume-Down    | Volume down |
   | **m**              | Mute           | Mute |
   | **p**              | Power          | Standby (blank the screen) |
   | **g**              | Guide / EPG    | Show the channel guide |
   | **Escape**         | Menu           | Open/close the on-screen menu |
   | **s**              | Sleep          | Cycle the sleep timer |
   | **i**              | Info           | Re-show the channel banner |
   | **l**              | Last / Back    | Jump to the previous channel |
   | **Enter**          | OK / Select    | Tune to the highlighted guide row |
   | **0**–**9**        | Number pad     | Type a channel number |

5. Unplug the Flirc from your computer and plug it back into the box.

That's it — no config changes needed; these keys work out of the box. (Advanced:
remap any key via `key_overrides` in the config — see the example.)

### Part H — Get audio out the TV (HDMI)

Find your HDMI audio device:

```bash
retrobox --list-audio
```

Pick the HDMI entry from the list and paste it into `config.yaml` as
`audio_device`. The exact string differs per machine — that is the whole reason
the command exists rather than this guide hardcoding one. If several HDMI
entries are listed they correspond to the physical ports, so try them in turn.

```yaml
audio_device: "<paste the HDMI line from --list-audio here>"
```

> `retrobox --setup` does this step for you, and
> `python3 -m retrobox.hwdetect` prints the same detection on its own.
> `aplay -L` gives the raw ALSA names if you want to cross-check.

### Part I — Make it boot to TV on power-up

Test it first:

```bash
retrobox
```

Your channels should appear on the TV and respond to the remote. Press `q` on a
keyboard (or `Ctrl+C` in SSH) to stop. Happy with it? Turn on auto-start:

```bash
./scripts/install.sh --service
```

Now the box boots straight to TV whenever it's powered on — no login, no menus.

### Part J — Power loss protection

Two settings make it behave like an appliance rather than a computer:

- **Turn on with the wall switch.** In the BIOS (tap **F2** at the logo on most
  machines), find **Power → After Power Failure** (sometimes "Restore on AC
  Power Loss") and set it to **Power On**. Now the box comes up whenever it gets
  mains power, so it can live on a switched socket behind the TV. A Raspberry Pi
  already does this — it boots whenever it has power.
- **Turn off with the remote.** Turn the volume all the way down to 0, then
  press volume-down **once more** — the machine shuts down cleanly. The sleep
  timer can do this for you too (`sleep_action: off`).

**Read-only root (optional).** Pulling the plug mid-write can corrupt the
filesystem. It is far less likely on an SSD than on a Pi's SD card, but if you
want it to be impossible, make the root read-only. `install.sh` prints whichever
of these matches the machine it ran on:

- **x86 (Debian/Ubuntu):**

  ```bash
  sudo apt install overlayroot
  # then set this line in /etc/overlayroot.conf and reboot:
  #   overlayroot=tmpfs
  ```

  Writes now go to RAM and vanish on reboot. To make a real change later, run
  `sudo overlayroot-chroot`, edit, and reboot.

- **Raspberry Pi:**

  ```bash
  sudo raspi-config     # Performance Options -> Overlay File System -> Enable
  ```

  (`raspi-config` does not exist on x86 — use the overlayroot route above.)

**Done!** Power it on and enjoy your nostalgia box.

---

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

## The four things that make it feel like cable

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
> If the box never appears under **Network**, that's the discovery service, not
> the share: modern Windows dropped the old SMB1 browsing this used to rely on,
> so `wsdd` advertises the box instead. Typing `\\<box-ip>\Library` into the
> address bar will still work. Check with `systemctl status wsdd`.

## Updating later

There's no Git remote wired up yet. To update, either re-run the `rsync` from
[Part C](#part-c--copy-retrobox-onto-the-box), or set up your own repo once:

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
