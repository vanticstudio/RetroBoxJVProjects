# Retro Box unattended installer

Flash two sticks, plug them into a bare mini PC, power it on, walk away. Come
back to a finished Retro Box that boots into the TV with the dashboard on the
LAN.

Base image: **Ubuntu Server 26.04 LTS amd64**. On 26.04 the *server* installer
picks HWE and OEM kernels from detected hardware by itself, which is the single
most useful thing a base OS can do for a product that gets flashed onto
secondhand mini PCs of unknown chipset.

Everything here runs on stock macOS. No Homebrew, no Docker, no xorriso.

---

## Build the media

```bash
# 1. Answer file. Asks for the console password, hashes it properly.
./installer/make-autoinstall.sh --ssh-key ~/.ssh/id_ed25519.pub

# 2. Boot stick: stock Ubuntu ISO + the `autoinstall` kernel argument.
diskutil list                                   # find the stick
./installer/make-boot-iso.sh ~/Desktop/ubuntu-26.04-live-server-amd64.iso \
    --write /dev/disk4

# 3. Config stick: the answer file on a volume labelled CIDATA.
./installer/make-cidata.sh --write /dev/disk5
```

Add `--wifi-ssid "Name" --wifi-password "secret"` to step 1 if the box will be
on wifi.

Both write steps show you the disk and make you type its identifier back before
erasing anything. They refuse internal disks outright.

## Install

1. Both sticks into the target. Ethernet if you have it. HDMI to a screen so you
   can watch (not required).
2. Power on, select USB boot.
3. Walk away. Roughly 15–25 minutes depending on the box and the network.
4. It powers itself off when done. Pull both sticks.

Boot it again and it comes up on the TV.

## What you get

- Boots straight into the player on HDMI, no login prompt
- Dashboard on `http://retrobox.local/` — **port 80, no port number in the URL**
- File share at `\\retrobox\Library`, no login
- SSH on key only
- Media library at `/media/retrobox`
- The product at `/opt/retrobox` (also `~retrobox/RetroBox`)

## Still manual, permanently

- **Programming the Flirc remote.** Needs the Flirc GUI and a human pressing
  buttons.
- **Getting show files onto the box.** Drag folders into `\\retrobox\Library`.
  Each folder becomes a channel on the next start-up.

---

## Why every unit shares one console password

Every Retro Box ships with the same username and the same password, and this
repository is public. That is safe because of one line in the answer file:

```yaml
ssh:
  allow-pw: false
```

The shared password is for **local console access only** — someone standing at
the box with a keyboard. Password authentication over SSH is off, so it buys a
network attacker nothing. Remote access is public-key only, and the key is
yours. Someone who reads this repo, learns the password, and is not physically
in the room with the unit has gained nothing they can use.

What is *not* in this repo is the answer file itself: `make-autoinstall.sh`
writes `installer/autoinstall.yaml`, which holds the password hash and your
public key, and it is gitignored. The gitignore rule and the template were
added in the same commit, so there is no version of this repo in which the
template exists without the rule protecting it.

The LAN share and the dashboard have no authentication either. That is a
settled decision for a media appliance on a home network — don't put one on a
network you don't control.

---

## When it fails

**It sat at a prompt instead of installing.** The `autoinstall` kernel argument
did not take. It has to be the bare word — `autoinstall=1` is parsed as a
key/value pair and silently does nothing. Check the boot stick was built with
`make-boot-iso.sh` and not just `dd`'d from a stock ISO.

**It installed but boots to a black screen.** SSH in, or press Ctrl+Alt+F2 for a
console. `getty` on tty1 is deliberately *not* masked, so if the TV fails to
start you still get a login prompt rather than nothing.

```bash
systemctl status retrobox retrobox-web
journalctl -u retrobox -b --no-pager | tail -50
/opt/retrobox/.venv/bin/retrobox --check --config /opt/retrobox/config.yaml
```

**The install itself failed.** The autoinstall aborts loudly on any non-zero
exit, so the installer will have stopped with an error on screen. Logs, on the
installed system:

```
/var/log/retrobox-install.log            our provisioning log, start here
/var/log/installer/subiquity-server-debug.log   which disk it picked, and why
/var/log/installer/curtin-install.log     partitioning and unpacking
/var/log/installer/installer-journal.txt  the whole boot
/var/log/installer/autoinstall-user-data  the config it actually used (mode 0400)
```

**A box that will not boot at all.** Boot any Linux live USB, mount the root
volume, and read the same paths. The install is LVM, so:

```bash
vgchange -ay && mkdir -p /mnt && mount /dev/ubuntu-vg/ubuntu-lv /mnt
less /mnt/var/log/retrobox-install.log
```

**The dashboard loads but every button does nothing.** That is the
`XDG_RUNTIME_DIR` failure: both units point at `/run/user/<uid>`, which only
exists if the user lingers. Check:

```bash
ls -d /run/user/$(id -u retrobox)      # should exist
loginctl enable-linger retrobox        # the fix
```

The installer does this already (by creating
`/var/lib/systemd/linger/retrobox`), so if you see it, something removed it.

---

## Single stick instead of two

```bash
./installer/bake-single-stick.sh ~/Desktop/ubuntu-26.04-live-server-amd64.iso
```

This bakes the answer file into the ISO at `/autoinstall.yaml`, so one stick
does everything. It genuinely rebuilds the image, which stock macOS cannot do —
you need `brew install xorriso` or a running Docker/OrbStack. The script finds
whichever you have and tells you if you have neither.

Prefer the two-stick route. It needs no tooling, and changing the password means
rewriting 4 MB instead of 3 GB.

If you use both, note that a CIDATA stick **outranks** the ISO-baked file. A
forgotten config stick will silently override a single-stick ISO.

---

## How the pieces fit

| File | What it does |
|---|---|
| `autoinstall.yaml.template` | The answer file, with placeholders. Committed. |
| `autoinstall.yaml` | The real one. Gitignored. Never commit. |
| `make-autoinstall.sh` | Template + password + key → answer file |
| `make-boot-iso.sh` | Adds `autoinstall` to a stock ISO, writes the stick |
| `make-cidata.sh` | Builds the CIDATA config stick |
| `bake-single-stick.sh` | Secondary: one stick, needs xorriso or Docker |
| `pin-release.sh` | Runs on the box: checks out the newest `vX.Y.Z` tag |
| `provision.sh` | Runs on the box: does the actual install |
| `lib/sha512crypt.py` | `$6$` hashing that works on macOS (`python3` it to self-test) |
| `lib/patch_iso.py` | In-place ISO editing with no third-party tools |
| `lib/render_autoinstall.py` | Template substitution |

### Two things worth knowing about the design

**The install happens at install time, not on first boot.** `late-commands`
clones the repo, pins a release, and runs `provision.sh` inside the target. A
first-boot installer would hand the customer a ten-minute `apt`+`pip` run and
would need the internet at *their* site instead of at your bench. "Come back to
a finished box" means finished.

**The generated `config.yaml` carries an explicit `channels:` list as well as
`media_root` and `auto_channels`.** That is not redundancy. With `media_root`
set and no `channels:` key, Retro Box discovers channels from the folders under
it — and a brand-new box has none, so the config raises `no channels found`,
exits 2, and `retrobox.service` fails five times in fifteen seconds and goes
*permanently* dead. Dropping media in later does not revive it. An explicit
channel list never touches the filesystem when it parses, so the box always
comes up; `auto_channels` still folds new folders in from channel 3 upwards.

---

## Release tags

`pin-release.sh` checks out the newest `vX.Y.Z` tag so a new box starts on a
known release. **The repo currently has no tags at all**, so boxes are built
from `main` and the script says so in the log.

That is harmless today — the update checker reads the GitHub *Releases* API,
and with no releases published the dashboard just reports "Up to date". To ship
pinned boxes:

```bash
git tag v1.0.3 && git push --tags
```

then publish it as a **Release** on GitHub. A bare tag with no Release object is
invisible to the update checker. Tags must be `vX.Y.Z` — the updater builds the
tag name as `"v" + version`, and a tag named `1.0.4` makes every box that
presses Update roll itself back.
