# Changelog

What's changed in Retro Box, written for the person who owns one.

This file and the box are the same words: each GitHub release carries its
version's section below as the release body, and the box reads that from the
release when it tells you an update is waiting. If a change isn't something
you'd notice, it isn't here.

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), newest
first.

## [Unreleased]

> **One thing to do by hand, once.** If your box was set up before this version,
> SSH in and run:
>
> ```bash
> cd ~/RetroBox && ./scripts/install.sh --service
> ```
>
> It leaves your settings, channels and videos alone. It replaces the two
> service files that tell the box how to start itself — the old dashboard one
> stopped **Restart**, **Reboot**, **Shut down**, the clock and the whole
> **Network** page from working, and no amount of pressing the buttons fixes
> that. An update installed from the dashboard can't do it for you, because
> those files live outside the copy of the project it updates. Once it's done,
> it's done.

### Added

- **The dashboard is now the whole box.** Everything below is reachable from
  `http://retrobox.local/dash` — you shouldn't need to plug a keyboard in or
  use SSH for any of it.
- **A page showing what's on.** `http://retrobox.local` is now a viewer: the
  channel and programme, how far through it is, and what's on the other
  channels. Leave it open on a phone.
- **No port number in the address.** The box answers on `http://retrobox.local`
  and `http://retrobox.local/dash`.
- **Channels can be managed from the browser.** Rename, renumber, point at a
  different folder, reorder the dial, add and remove. Removing a channel never
  deletes your videos.
- **Upload videos from the browser.** Drag a folder of episodes onto the page
  and it becomes a channel, or drop files onto a channel you already have. Big
  uploads survive real life: if your wifi drops, your laptop sleeps or you
  close the tab, the parts already sent stay on the box and it carries on from
  there rather than starting again.
- **A System page.** Free space on each disk, temperature, whether hardware
  video decode is working, which remotes are live, the log, and a **Copy for
  support** button that puts everything we'd ask for on your clipboard in one
  go.
- **A remote test.** Press a button on your remote and watch it appear, so you
  can tell whether the Flirc took its programming without guessing at the
  television.
- **Restart and shutdown from the dashboard**, and a factory reset that clears
  settings and channels **without touching your video files**.
- **Timezone and clock.** It now warns you when nothing is keeping the clock
  correct, because channels that change with the time of day drift silently
  otherwise.
- **Config backup and restore.** Download a copy, put one back, or go back to
  the copy the box kept from before anything automatic edited it.
- **Software updates from the dashboard.** The box checks for a new version
  about once a day and tells you what changed. Installing is always your
  decision — it never updates itself. If an update doesn't come up cleanly, the
  box puts the previous version back on its own.

### Changed

- **Your config file can't be destroyed by a power cut any more.** Changes are
  written alongside it and swapped in with a single rename, and the first time
  anything automatic edits it your original is kept as `config.yaml.bak`.
- Channel changes made in the dashboard reach the television within a second,
  without restarting it and without interrupting what's playing.
- **Every box now shows the JV Projects clip when you switch it on**, instead of
  cutting straight to a channel. Press anything to skip it, or turn it off for
  good under Branding in the dashboard. If the clip ever fails to play, the box
  gives up after a few seconds and tunes in anyway — it can't get stuck on it.

### Fixed

- **A version that stops working overnight now puts itself back.** The box
  promised to undo an update that didn't come up, and it only ever checked once
  — while the box was still switched on and the picture was already back. If a
  new version came back that afternoon and then failed to start the next
  morning, the box stayed on it for ever, with a dashboard nobody could reach.
  A new version is now on trial for the next three times the box is switched
  on: it isn't called good until the television has actually come back after a
  cold start, and if it doesn't, the box puts the previous version back by
  itself and says so on **System → Software**.
- **An update interrupted by a power cut no longer blocks every update after
  it.** A box switched off at the wall part way through an update came back
  believing an update was still running, and refused every attempt from then on
  with no way for you to clear it. It now notices at start-up that the update
  was cut off, puts the previous version back, and accepts new updates
  normally.
- **A network change you press *Keep* on is actually kept.** Every change made
  from the Network page undid itself a couple of minutes later no matter what
  you did, because the box lost track of the trial the moment the page asked
  it anything else — so it treated a change nobody had confirmed as one nobody
  could confirm. *Keep* now means kept, and *Undo now* still undoes it
  immediately.
- **A network change interrupted by the wall switch is put back when the box
  starts again.** If the power went off while a new network setting was still
  on trial, the box came back up on that untested setting and kept it for good
  — and if the setting was the reason the box couldn't be reached, there was no
  page left to open to undo it. It now puts the previous settings back at
  start-up, without anybody having to ask.
- **Undoing a network change can no longer leave the box with no network.**
  After you renamed the box, an internal warning was being recorded as if it
  were part of the box's saved network settings. If a later change was then
  undone — including one the box undid by itself because nobody pressed *Keep*
  — it put that warning back into the settings file, and the next time the box
  was switched on at the wall it came up with no network at all and no way to
  reach it. The same fault made the Network page list no adapters on a box
  whose network was working perfectly well.
- **Your wifi password is never readable by anything but the box itself.** The
  file holding it used to be created readable by everyone and narrowed a moment
  later, and if that second step failed it stayed that way with your password
  in it. It is now made private before the password is written into it, and if
  the box can't make it private it doesn't write it at all. A failed save no
  longer repeats your password back in the error message either.
- **Restart, reboot, shut down, the clock and every network setting work again
  on a real box.** The dashboard's service was locked down in a way that stopped
  it becoming root for those specific jobs, so each of those buttons came back
  with an error no matter what you did. Replacing the service file is the one
  thing you have to do by hand — see the note at the top of this section.
- The version the box reports is now the version it's actually running. It
  previously reported 1.0.0 no matter which release was installed.
- **A good video is no longer refused as having "no picture" in it.** When the
  box could measure how long a file runs but couldn't tell whether there was a
  picture in it, it wrote down "there is no picture" — and then remembered that
  answer for that file for ever. Uploading the same clip again gave the same
  refusal, and a boot splash that was perfectly fine could never be installed.
  The box now records "couldn't tell" as its own answer, and after this update
  it looks again at any file it had previously written off.
- **An upload that's short a piece no longer half-lands.** If one file in a
  batch was missing a piece, the box moved the files ahead of it into the
  channel and *then* said the upload had failed. Sending the missing piece and
  finishing again reported those episodes as duplicates that were already
  there — the ones the box had just written itself. Now nothing is moved until
  the whole batch is complete, and an upload interrupted part way through
  finishing knows its own episodes on the second attempt.
- **An upload the power cut off no longer eats your disk invisibly.** A video
  or a boot splash sent from the browser was written straight into the channel
  folder while it arrived. If the box was switched off at the wall part way
  through — the normal way it gets switched off — what was left was a
  part-file that nothing on the dashboard could see, that the television
  ignored, and that no page would ever offer to clear up: a film's worth of
  space quietly gone. Half-arrived uploads now wait in the same temporary
  space as everything else, so **Settings** counts them and they're cleared
  after `upload_expiry_hours` like any other unfinished upload.
- **Uploading to a channel on a plugged-in drive works.** If a channel pointed
  somewhere outside your media library — an external drive, say — finishing the
  upload failed with no message at all, every transferred gigabyte stayed stuck
  in the box's temporary space, and retrying failed the same way. It now copies
  the file across safely, and a copy that's interrupted never leaves half a
  file behind looking like an episode. The free-space check looks at the drive
  the files are actually going to, so an upload aimed at a full drive is
  refused up front instead of after the whole transfer.
- **Two people uploading at once can't tread on each other.** With two phones
  uploading at the same time, the limit on how many uploads may run at once
  could be walked straight past, and one upload starting could delete the other
  one a split second after it began, which showed up as an error out of nowhere.
- **A schedule can no longer be saved pointing at a folder you haven't got.**
  A time block that swaps in a different folder took whatever was typed. A
  mistyped name saved cleanly, drew its bar on the timeline, and the only sign
  anything was wrong came hours later when that channel played nothing all
  evening. The folder is now checked as you save it — it has to be a real
  folder inside your library — and a restored config file is checked the same
  way instead of only having its channels' folders looked at.
- **Changing a time on the schedule no longer deletes the folder that block
  swaps in.** If you'd written a time block into `config.yaml` by hand — cartoons
  in the morning out of a different folder — then nudged that channel's times in
  the editor, the folder was silently dropped on Save and the block quietly
  became a rename with no programming behind it. The editor keeps the folder now.
  One consequence worth knowing: a hand-written block pointing outside your media
  library (an external drive, say) is refused with a message when you save that
  channel's times, rather than being thrown away without one.
- **A factory reset keeps the library you actually have.** If your media folder
  had a `#` in its name, the reset wrote the name in a way the box read back as
  a *different* folder, and rebuilt every channel from that one instead if it
  happened to exist. A name with a colon in it left the box telling you your own
  factory reset "will not load". Both are written properly now.
- **Restoring a config while somebody else is editing no longer loses one of
  them.** Putting a config file back from a laptop at the same moment as
  somebody renamed a channel on a phone could throw the restored file away a
  heartbeat after it landed, with both people told it had saved. Replacing the
  whole file now waits its turn like every other change.

### Security

- **A config file can no longer tell the box to run something that isn't a
  shutdown.** `power_off_command` in `config.yaml` says what the box runs when
  you switch it off — the dashboard's shutdown button, the sleep timer, or
  turning the volume down past zero. It was taken exactly as written, and the
  dashboard's **restore a config** button will accept a file from anyone on
  your home network without a password. Between them, that meant a config
  uploaded from another device on your wifi could put any command it liked
  behind your power button and have the box run it the next time anybody
  switched it off. The box now recognises only the ordinary ways to switch a
  machine off (`sudo poweroff` and its handful of cousins, or nothing at all to
  disable it), and refuses to save a config that asks for anything else. A file
  already on the box that asks for something else is ignored rather than
  obeyed, so the box still starts and the power button still works — it just
  behaves like the standard one.
- **Replacing your whole config now asks first**, the same way the backup
  restore and the factory reset already did. Uploading a config throws away
  every setting on the box in one go, and that shouldn't have been a single
  unconfirmed request.
- **Your wifi password is no longer handed out over the network.** While a
  network change was on trial, the box kept a note of the settings it would put
  back if you didn't confirm — which, on a wifi box, includes the password. That
  note was going out in full to anything that asked the dashboard what the
  network was set to, and the dashboard has no login by design. It is stripped
  out of every answer now, the note itself can only be read by the box, and it's
  deleted the moment the change is kept or undone. If you'd rather not take the
  word of a changelog, change your wifi password once after updating.

## [1.0.3] - 2026-07-31

### Added

- The LAN web dashboard: what's playing, the channel list, volume, mute,
  standby and shutdown, in the same phosphor green as the on-screen display.

## [1.0.1] - 2026-07-31

### Added

- Station bumpers, the on-screen channel guide and the sleep timer.

### Fixed

- Audio output selection on boxes with more than one HDMI port.

## [1.0.0] - 2026-07-30

The first release. Channels from folders, a shuffle that never stops,
dayparting, a remote, and a CRT picture.
