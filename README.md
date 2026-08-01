# Retro Box

**Turn a spare Linux box into the cable box you fell asleep in front of.**

**[The project site →](https://vanticstudio.github.io/RetroBoxJVProjects/)** —
what it is and what it feels like, in one page. MIT licensed. It ships empty;
you supply your own files.

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
## What it looks like

The television itself is on the television. Everything else happens in a browser
on your phone or laptop, at `http://retrobox.local` — no app, no account, no
password.

| | |
|---|---|
| **What's on now** — leave it open on a phone. The channel, the episode, how far through it is, and what's playing on every other channel.<br><br><img src="docs/screenshots/viewer.png" alt="The Retro Box viewer page showing channel 3, Late Night Movies, and what is on the other four channels"> | **The remote** — change channel, volume, standby and shut down from the sofa, without hunting for the actual remote.<br><br><img src="docs/screenshots/console-watch.png" alt="The WATCH tab of the management console, with volume and standby controls and the channel list"> |
| **Channels** — rename, renumber, reorder the dial, point a channel at a different folder, or delete an episode you're done with. Removing a channel never deletes your videos.<br><br><img src="docs/screenshots/console-channels.png" alt="The CHANNELS tab with a channel expanded, showing its number, name, folder and episode list"> | **Adding shows** — drag a folder of episodes onto the page and it becomes a channel. Big uploads survive a dropped wifi connection or a laptop going to sleep.<br><br><img src="docs/screenshots/console-add.png" alt="The ADD tab, a drop zone reading DROP A FOLDER OR FILES HERE"> |
| **Dayparting** — a channel can be a different thing at different times of day. Cartoons in the morning, reruns in the afternoon, a hard sign-off overnight.<br><br><img src="docs/screenshots/console-schedule.png" alt="The TV tab showing a 24-hour schedule bar for channel 2 with SATURDAY CARTOONS, AFTERNOON RERUNS and an OFF AIR block"> | **Settings** — audio output, the volume it powers on at, and the sleep-timer ladder.<br><br><img src="docs/screenshots/console-settings.png" alt="The SETTINGS tab showing audio output, power-on volume, sleep timer and new-folder handling"> |

Plus updates over the air, with a rollback if a new version doesn't come back
healthy, and the buttons you hope never to need:

<img src="docs/screenshots/console-system.png" alt="The SYSTEM tab showing the installed version with a CHECK NOW button, and restart, reboot, shut down and factory reset controls" width="620">

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

- **The box in a browser.** `http://retrobox.local` shows what's on;
  `http://retrobox.local/dash` manages it. No port number, no app, nothing to
  set up — the installer handles the name and the port. See
  [The box in a browser](#the-box-in-a-browser).
- **The on-screen menu.** Press **Escape** on an attached keyboard for channels,
  volume, audio output, shutdown and an About screen. It pauses playback and
  resumes exactly where it left off. A mouse works while it's open — hover to
  highlight, click to pick — and the pointer is completely invisible and inert
  the rest of the time, whether or not a mouse is even plugged in. Escape used
  to quit the app; **`q` does that now.**
- **The boot splash.** A JV Projects clip ships in `retrobox/assets/` and plays
  once at power-on, before the first channel — **on by default**, so a box shows
  its own branding without being asked. Any button skips it, and a hard
  30-second timeout means a clip that never finishes still hands over to channel
  one. Set `boot_splash: false` (or Branding → *Play no splash* in the console)
  to go straight to a channel; see [`retrobox/assets/README.md`](retrobox/assets/README.md)
  to swap in your own.

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

The same measurement is what tells the box whether a file has a picture in it,
so that cache stores three answers and not two: yes, no, and *couldn't tell*.
That last one matters — it's the difference between "this would play as a black
screen" and "we don't know", and only the first is ever held against a file.
The cache is only ever a shortcut: delete the file and the box simply measures
everything again, and if it can't make sense of an entry — after an update, or
because the power went out mid-write — it re-measures that file rather than
believing it.

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
## The box in a browser

Point any browser on the network at the box. No port number, no app to install:

```
http://retrobox.local          what's on right now
http://retrobox.local/dash     the management console
```

**`http://retrobox.local` is what's on TV.** Channel and programme, how far
through it is, and what's on the other channels — the page to leave open on a
phone on the arm of the sofa. It only reads; nothing on it can change the
channel by accident.

**`http://retrobox.local/dash` is the console.** Six tabs, and between them
they cover everything you would otherwise have to SSH in for.

> **If `retrobox.local` doesn't resolve**, use the box's IP address instead —
> `http://192.168.1.42/` and `http://192.168.1.42/dash`. The box prints its IP
> above the login prompt, and `hostname -I` on the box shows it too.
>
> `.local` works over mDNS, which macOS, iOS, Android and Windows 10+ all speak
> without any setup — the installer puts Avahi on the box to answer it. Some
> corporate and guest networks block multicast, which is when you need the IP.
> Bare `http://retrobox` with no `.local` works only if your router registers
> DHCP hostnames into its own DNS; plenty do, plenty don't, so it's a bonus
> rather than the address to write down.

**Watch** — what's on now, the channel list with jump-to-channel, volume and
mute, standby, and a shutdown button. The same things the on-screen menu does,
in the same phosphor green, for when the remote is under a cushion.

**Channels** — the lineup, editable. Tap a channel to rename it, give it a
different number, point it at another folder, move it up or down the dial, or
remove it. Removing a channel only takes it off the dial; **your video files are
never deleted by it**. There's an "add channel" button for a folder you've
already dropped on over the share.

**Files** — inside each channel, the episodes on it, with their sizes. Upload a
video straight from your phone, with a progress bar, and delete one you don't
want.

**Add** — the bulk one. Drag a folder of episodes onto the page and it becomes a
channel named after the folder, filled with what was in it. Drag files onto an
existing channel and they get added to it. Before anything moves it shows you
what it found, what it will skip and why, and lets you set the channel's name and
number.

Uploads are sent in pieces, so they survive real life: if your wifi drops, your
laptop sleeps, you close the tab or the box loses power, the parts already
transferred stay on the box. Come back, pick the same files again, and it carries
on from where it stopped instead of starting over. Unfinished uploads are listed
under **Add**, and anything left untouched for a day is cleared off the disk on
its own so it can't quietly eat your space.

Anything half-arrived — including a single video or a boot splash sent while the
power went off — waits in that same temporary space rather than in the channel
folder. That's what makes it visible: **Settings** counts it in the space
unfinished uploads are holding, and the same one-day clean-up takes it away. The
television never sees it, so a part-file can't turn up on the dial.

Finishing an upload is all or nothing. If any file is still short a piece,
nothing at all is moved into the channel and the box names the file that's
short — you never end up with half a series on the dial underneath a message
saying the upload failed. Send the rest and finish it again. And if a commit is
interrupted part way through, the box knows which episodes it had already
written: the second attempt finishes the job and reports them as uploaded,
rather than as duplicates of somebody else's files.

Channels don't have to live in your media library — one can point at a
plugged-in drive. Uploading to a channel like that works exactly the same way,
and the box checks *that* drive has room before it accepts the upload rather
than only looking at the card it boots from.

**TV** — the things that make it a television rather than a video player:

- **Schedule.** A channel can be a different thing at different times of day —
  cartoons in the morning, something else after dark, colour bars overnight.
  You get the day laid out as a bar you can read at a glance, including blocks
  that run past midnight, and a "what would be on at 3am" box so you don't have
  to wait until 3am to find out. Overlapping blocks are refused rather than
  guessed at. A block that swaps in a different folder has that folder checked
  the moment you save it — it has to be a real folder inside your library — so a
  typed-wrong name is refused there and then, instead of turning into a channel
  that goes quiet at six o'clock this evening for no visible reason. Editing a
  block's times keeps its folder, which it didn't always: nudging a time used to
  quietly drop a folder swap you'd written into `config.yaml` by hand. If such a
  block points somewhere outside your library — a plugged-in drive, say — the
  editor now says so and refuses to save rather than throwing it away; move the
  folder into the library or remove the block. The box's clock is shown right
  there, because a schedule is only as right as the clock underneath it.
- **Filler and bumpers.** Generate the colour bars, static and glitch clips,
  play them back, set how often station idents air between episodes, and turn
  them off per channel — a news channel with idents between items is wrong.
- **Start-up clip.** Preview the JV Projects clip, replace it with your own, or
  turn it off. An uploaded clip has to be short and actually playable. Whatever
  you put there, **the television gives up on it and starts anyway** if it
  hasn't finished — it can never leave you looking at a black screen.
- **Picture.** The CRT effect and how long the channel banner stays up, with a
  button that puts the banner on the actual television so you can see it.

**Settings** — audio output, the volume the box powers on at, the sleep-timer
ladder, and whether new folders automatically become channels. Anything that
only takes effect on the next start-up says so when you save it.

**System** — the answer to "is my box alright", and the last of the reasons
anyone had to open a terminal on it:

- **Health.** Version, uptime, the addresses it answers on, free space on the
  system disk and the library disk *separately* (they're often different disks,
  and it's the system one that stops the box booting), temperature and load
  where the hardware reports them, whether hardware video decode is actually
  working, which remotes are live, and whether the file share is up. Written as
  plain answers — "Hardware decode: working, Intel UHD 630" — with the raw
  detail behind a toggle for when you actually want it.
- **Remote test.** Press a button on your remote and watch it appear, with
  which receiver saw it and what it did. Programming the Flirc is the one step
  that can't be automated away, and this turns checking it from pointing at the
  telly and guessing into something with feedback. It watches; it doesn't take
  the remote over.
- **Log.** The last few hundred lines from the TV and the dashboard, filtered by
  service and level, with a plain-text search. **Copy for support** puts the log
  and the system information on your clipboard as one block of text — so nobody
  has to be talked through `journalctl` over the phone.
- **Clock.** Timezone, the time, and whether anything is actually keeping it
  correct. It warns when nothing is: channels that change with the time of day
  drift silently on a box with no internet, and that's miserable to diagnose.
- **Config file.** Download a copy before you change much, or restore one — an
  uploaded config is loaded and checked before it's allowed to replace the live
  one, so a bad file is refused rather than leaving you with a box that won't
  start. That check covers every folder it names, including the ones inside a
  schedule, and the one setting that names a command to run: a config whose
  `power_off_command` isn't a plain shutdown is refused outright, because this
  page has no login and anyone on your wifi can upload to it. Putting a whole
  config back asks twice, like the factory reset does. The once-only
  `config.yaml.bak` is offered here too. A restore takes
  the same lock every other edit takes, so it can't land in the middle of
  somebody renaming a channel on their phone and quietly lose one of the two.
- **Power.** Restart the TV, restart the dashboard, reboot, shut down. Every one
  asks twice. Restarting the dashboard tells you it's going and reconnects on
  its own.
- **Factory reset.** Clears channels and settings back to defaults. **It does
  not touch your video files** — the lineup is rebuilt from the folders in your
  library, so you get a clean box with all your shows still on it. The new file
  is written by the same writer as every other change, so a library folder with
  a `#` or a `:` in its name comes back pointing at the folder you actually
  have, rather than at whatever is next to it.
- **Network.** Join a wifi network, switch between wired and wireless, or set
  a fixed address — from the browser. **Every network change is applied on
  trial:** the box tries it, and if it can't be reached on the new settings it
  puts the old ones back by itself within two minutes. You press *Keep* to make
  it permanent. If a change moves the box to a new address, the page tells you
  where it's going and then goes looking for it there, at the old address, and
  at `retrobox.local`. There's a connection test that answers three separate
  questions — is it connected, can it reach the internet, does DNS work —
  because those are three different problems with three different fixes.
  **Nothing is ever kept by accident:** if the dashboard restarts while a change
  is on trial — you restarted it, or the power went — the box puts the old
  settings back rather than keeping settings nobody confirmed, and if you press
  *Keep* after that it says so plainly instead of pretending it worked.
- **Software updates.** The box checks for a new version about once a day and
  shows you what changed — in plain words, for every version you'd be skipping,
  not just the newest. Then it asks. **It never installs anything on its own.**
  If an update doesn't come up cleanly, the box puts the previous version back
  by itself and tells you so; you don't end up with a dead television and no way
  in. **And it keeps watching after the update finishes:** a new version is on
  trial for the next three times the box is switched on, so if it comes back
  today and then fails to start tomorrow morning, the box puts the old one back
  on its own without anybody asking it to. There's a "go back a version" button
  too.

Every destructive button asks twice: the first tap arms it, the second does it.

> **Picking a whole folder needs a computer.** Chrome, Edge, Firefox and Safari
> on a desktop can all hand over a folder. iOS Safari cannot — that's the
> browser, not the box — so on an iPhone or iPad you get multi-file selection
> instead, and the page says so rather than showing a folder button that does
> nothing. Dragging and dropping a folder also needs a desktop.

> **For a whole library at once, still use the file share.** Dragging 50 GB
> through a browser means keeping the tab open for the duration, and a native
> file copy over [the network share](#dropping-files-on-from-another-pc) is
> faster and easier to walk away from. The browser uploader is the better tool
> for the normal case — a new series, or ten episodes that just arrived — and it
> is what stops you ever needing SSH. Both stay; use whichever suits the job.

Changes are written to `config.yaml` and the running TV is told to re-read it,
so a channel you rename changes on the TV within a second — without restarting
and without interrupting whatever is playing. If the TV process isn't running,
the change is still saved and applies when it next starts; the page tells you
which of the two happened.

It runs as its own service (`systemctl status retrobox-web`) and never touches
the running player: the TV writes a small status file every couple of seconds,
and the dashboard sends button presses back over a local socket that arrive as
ordinary remote events. If the TV process is stopped, the page says so rather
than lying to you.

> **One thing to know about editing from the browser.** The first time the
> dashboard writes to `config.yaml` it rewrites the whole file, which drops any
> comments and hand-formatting you had in there. Your original is kept, once and
> forever, as `config.yaml.bak`.

> **Like the file share, there is no login.** Anyone who can reach the box on
> the network can change the channel, edit the lineup, upload and delete videos,
> or shut it down. Same trade as the share, and the same caveat: fine on a home
> LAN, wrong anywhere you don't control — don't port-forward it to the internet.
> To turn it off: `sudo systemctl disable --now retrobox-web`.
>
> One thing to know before you do: the dashboard's service is also what asks, at
> every start-up, whether the version this box was updated to actually came back
> — and what puts a network change back if the power went off while it was still
> on trial. Switch the dashboard off and nobody asks those questions, so a bad
> update stays and an unconfirmed network setting sticks. It's a fair trade if
> the box lives somewhere you don't control; it isn't a free one.
>
> Uploads only accept video files, only land inside your media library, and are
> refused if they'd fill the disk. You can tighten the limits under `web:` in
> the configuration reference below.

> **How the box gets root, and how far that goes.** The dashboard runs as an
> ordinary user, not as root. It's handed exactly one kernel capability — the
> one that lets an ordinary process answer on port 80, so the address you type
> has no port number in it. The handful of things that genuinely need root —
> restart, reboot, shut down, set the clock, change the network settings — go
> through `sudo` to one specifically named command from
> `/etc/sudoers.d/retrobox-*`, and that list is generated from the code itself
> so the file and the program can't drift apart. There's no blanket `systemctl`
> rule: "restart the television" isn't spellable as "stop the firewall". That
> list is the boundary, and it's deliberately the only one. The service units
> don't set `NoNewPrivileges=` or `CapabilityBoundingSet=`, because either of
> them stops `sudo` becoming root at all — every one of those buttons would
> fail on a box you can't SSH into to find out why.
>
> **Your wifi password** lives in `/etc/netplan/91-retrobox-wifi.yaml`, which
> only root can read — from the instant it exists, not a moment later. The file
> is made private *before* the password is written into it, so there's no window
> where another account on the box could read it, and if the box can't make it
> private it doesn't write the password at all rather than leaving it lying
> about. The same goes for the copy it keeps while a network change is on
> trial: to put the old settings back the box has to remember what they were,
> password and all, so that note is private from the moment it exists, it is
> never part of anything the dashboard hands out over the network, and it is
> deleted the second the change is kept or undone.

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
./scripts/install.sh --service
```

`install.sh --service` is safe to re-run — it leaves your `config.yaml`, your
channels and your videos alone. It reinstalls the package (which is what
refreshes the start-up check that decides whether the last update came up
healthy) and rewrites the two service files in `/etc/systemd/system` and the
`sudo` rules that go with them. If you'd rather do the minimum, `git pull &&
sudo systemctl restart retrobox` still picks up code changes, since the install
is editable.

> **Boxes installed before this version must run the installer once.** The
> dashboard's service file was locked down in a way that stopped the box
> becoming root for the jobs that need it, so **Restart**, **Reboot**, **Shut
> down**, the clock and every setting on the **Network** page came back with an
> error however many times you pressed them. Only re-running the installer
> replaces that file:
>
> ```bash
> cd ~/RetroBox && ./scripts/install.sh --service
> ```
>
> An update installed from the dashboard **cannot** do this part for you — it
> updates the code in your copy of the project, and the service files live
> outside it, in the system. It's a one-off: unless a future release says
> otherwise here, updates from the dashboard need nothing from you.

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

power_off_on_min_volume: true          # volume-down past 0 switches the box off
power_off_command: ["sudo", "poweroff"]  # what "off" runs; [] disables it

ui:                      # the green on-screen display
  color: "#4DFF5A"       # lit text and volume bars
  dim_color: "#123B18"   # unlit volume dots
  glow: true
crt:                     # the CRT picture effect (curve, rounding, scanlines)
  enabled: true
  curvature: 0.12
updates:                 # software updates
  check: true            # look for new versions (default: on)
  auto_apply: false      # install them without asking (default: OFF, deliberately)
  check_interval_hours: 24

web:                     # limits on what the dashboard may write to the disk
  max_upload_mb: 8192    # biggest single upload it will accept
  min_free_mb: 1024      # never fill the disk below this much free space
  chunk_mb: 8            # size of each piece a file is sent in
  max_files_per_upload: 500   # a series at a time, not a whole library
  max_upload_sessions: 4      # how many uploads may run at once
  upload_expiry_hours: 24     # abandoned uploads are cleared off after this
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

> **`power_off_command` is the one setting that names a command**, so it is the
> one setting the box won't take on trust. It has to be an ordinary way of
> switching a machine off — `poweroff`, `systemctl poweroff`, `shutdown -h now`
> or `halt -p`, on their own or behind `sudo` — or `[]` to disable the feature.
> Anything else is ignored, the box uses `sudo poweroff` instead and says so in
> the log, and the dashboard refuses to save a config that asks for it at all.
> The dashboard has no password by design, and this value would otherwise be
> run by the box the next time anyone pressed the power button.

### Your config is safe from the power switch

The box edits this file itself when it needs to — turning a new folder you drop
on the share into a channel, or saving a change you made in the dashboard. It
never writes over your config in place:
the new version is written alongside it and swapped in with a single rename, so
pulling the plug mid-write can't leave you with half a file and a box that won't
start.

The first time anything automatic touches it, your config is copied to
`config.yaml.bak` and that copy is never written again. It's the file exactly as
you left it, from before the box edited anything — so if a config ever goes bad,
that's what you restore.

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
- **A config change broke something** → `retrobox --check` names the offending
  line. If the file itself looks mangled rather than just wrong, put your
  original back with `cp config.yaml.bak config.yaml` — that's your config from
  before the box ever edited it.
- **An upload says "no video stream"** → it uploaded fine, but there's no picture
  in it, so it would play as a black screen. Usually an audio file with a video
  extension, or a download that didn't finish. The box keeps it and leaves the
  decision to you: delete it from the channel's file list if it's not wanted.
  The box only says this when it has actually looked and found no picture — if
  it couldn't tell, it says nothing and plays the file.
- **An upload says "nothing was saved: still missing…"** → one of the files
  never arrived in full, usually a tab closed or a phone locked part way
  through. Nothing was moved into the channel, and nothing you'd already sent
  was thrown away. Pick the same files again and it carries on from where it
  stopped, then finish it.
- **Restart, reboot, shut down, the clock or the Network page all give an
  error** → the box's service file is from before this version and stops the
  dashboard becoming root for those specific jobs. It affects every one of those
  buttons and nothing else, and pressing them again won't help. Fix it once, over
  SSH: `cd ~/RetroBox && ./scripts/install.sh --service`. See
  [Updating later](#updating-later).
- **Something is wrong and you don't know what** → open **System** on the
  dashboard. It answers most of it on one page, and **Copy for support** puts
  the log and the details on your clipboard in one go to send to us.
- **The remote isn't working** → **System → Remote test**. Press a button; if
  nothing appears, the Flirc didn't take the programming (Part G) or isn't
  plugged in. If it appears with the wrong action, reprogram that button.
- **An update didn't work** → the box puts the previous version back on its
  own and says so on **System → Software**. Nothing is lost; your channels,
  settings and videos are untouched. If it says the root filesystem is
  read-only, that's `overlayroot` from [Step 9](#step-9--optional-read-only-protection)
  — updates can't stick while it's on, so turn it off, update, turn it back on.
- **The television stopped coming on after an update** → switch the box off at
  the wall and on again, and give it a couple of minutes each time. A new
  version is on trial for three start-ups: after the third one without a
  picture the box puts the previous version back by itself and **System →
  Software** explains what happened. You don't have to do anything but wait.
- **The box lost power in the middle of an update** → switch it on and leave
  it. It notices at start-up that an update was cut off, puts the previous
  version back, and is a television again. Nothing is left half-installed and
  the next update works normally.
- **The box vanished after a network change** → wait two minutes. It undoes
  the change by itself and comes back on the settings it had before. If it
  doesn't, the change was confirmed — plug a keyboard and monitor in. If the
  power went off mid-change instead, switch it back on and leave it: it puts
  the previous settings back as it starts, before it serves a single page.
- **The disk is fuller than it should be** → check **Settings**; it shows how
  much space unfinished uploads are holding, including anything a power cut
  left half-arrived. Discard them under **Add**, or leave them and they're
  cleared automatically after `upload_expiry_hours`.

---

## For the curious (how it works)

The project is plain Python. The "brains" (channel scanning, the shuffle, the
dayparting, the state machine) have no hardware dependencies and are fully
unit-tested; the hardware-facing parts (the mpv video player and the remote
input) are isolated behind small interfaces. You can drive the whole thing on a
laptop — macOS or Windows included — with a mock player:

```bash
pip install -e ".[dev]"
pytest                                                   # 1288 tests, ~16s
python -m retrobox --dry-run --config config.yaml    # keyboard-controlled, no video
```

```
retrobox/
├── config.py      YAML -> validated config
├── configwrite.py crash-safe config writes (+ the one-off config.yaml.bak)
├── configstore.py the dashboard's read-modify-write of config.yaml
├── safepath.py    what an upload from the network is allowed to be called
├── sysinfo.py     health facts, every one degrading to "unknown"
├── journal.py     bounded, filtered reads of the systemd journal
├── servicectl.py  the four things the box may be asked to do, and the sudoers rule
├── updates.py     is there a newer release, and what did it change
├── updater.py     applying one, and getting back if it goes wrong
├── netconf.py     netplan documents, built as data and validated
├── netprobation.py  network changes that undo themselves if unconfirmed
├── schedule.py    the dayparting editor: a day laid out, overlaps refused
├── branding.py    what may become a boot splash, and what may not
├── uploads.py     chunked resumable uploads (spool, assembly, reclaiming)
├── autochannels.py  new folders on the share -> new channels
├── webui.py       the viewer at / and the console at /dash (Flask, its own service)
├── webservice.py  what `retrobox-web` actually runs: did the last update come up?
├── status.py      status.json + the control socket the dashboard talks to
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
