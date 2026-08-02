# Changelog

What's changed in Retro Box, written for the person who owns one.

This file and the box are the same words: each GitHub release carries its
version's section below as the release body, and the box reads that from the
release when it tells you an update is waiting. If a change isn't something
you'd notice, it isn't here.

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), newest
first.

## [2.1.0] - 2026-08-02

### Fixed

- **The television now has sound.** A box with nothing set for `audio_device`
  was playing into the 3.5 mm headphone socket — the analog jack — because
  that is what "let the computer choose" means on an Intel machine. Nothing
  was plugged into it. The box now works out which HDMI socket your
  television is actually in, by reading what the set itself sends back down
  the cable, and plays through that one. It does this **every time it starts**,
  so a box you set up on a desk with no screen attached finds its sound the
  first time you plug it into a television. Nothing to configure.
- **A 5.1 soundtrack is no longer silent on a stereo television.** Sending
  five channels to a set that only accepts two isn't an error — it just makes
  no noise, and nothing anywhere says why. The box now asks the television how
  many channels it takes and mixes down to fit.
- **The System page was telling you the opposite of the truth.** It reported
  "no HDMI audio found" and "software decode is being used" on a box whose
  television was playing with hardware decode at that moment. The page was
  asking questions it had no permission to ask — it could not open the sound
  card or the graphics chip — and stating the refusals as facts. It now
  reports what the television is **actually doing**, asked of the television,
  and the page has the access it needs to check the rest honestly.
- **"Nothing is playing" is now an answer.** The Watch tab used to call an
  idle box "software decode". It had not chosen anything yet.
- **The right graphics driver for your chip.** The box was installing the
  driver for pre-2015 Intel graphics alongside the modern one and hoping. It
  now picks by which chip you actually have.
- **Subtitles stay off.** An embedded subtitle track in an mkv could switch
  itself on. A 1998 cable box did not have subtitles.

### Added

- **A REPAIR button and a TEST SOUND button**, on the System page. Repair
  looks for your television's socket again, turns up anything that was muted
  and says what it changed. Test sound plays a two-second tone, so you can
  tell whether it is the box or the telly without touching a terminal. Both
  are safe to press twice.
- **The System page says what your box can and cannot do**, in full: the video
  formats it can decode in hardware, and the ones it will have to do in
  software. On this generation of Intel that means AV1 is software-only — no
  setting changes that, it is the chip.
- **The installer finishes by telling you what actually works** — picture and
  sound, each with what to do about it. It never fails an install over it: a
  box on software decode with no sound is still a box.

## [Unreleased]

> **One thing to do by hand, once.** If your box was set up before this version,
> SSH in and run:
>
> ```bash
> cd ~/RetroBox && ./scripts/install-service.sh
> ```
>
> It leaves your settings, channels and videos alone. It replaces the two
> service files that tell the box how to start itself — the old dashboard one
> stopped **Restart**, **Reboot**, **Shut down**, the clock and the whole
> **Network** page from working, and no amount of pressing the buttons fixes
> that. It also refreshes the short list of commands the box is allowed to run
> as root, which now includes the one that swaps a network file into place in a
> single step; until it has been run, saving anything on the **Network** page
> comes back with an error ending in *"this box may need
> scripts/install-service.sh run again"*, and the box goes on using the network
> settings it already has. (On a slightly older version that error reads
> `sudo: a password is required` and nothing else — same cause, same fix.)
> An update installed from the dashboard can't do it for you, because those
> files live outside the copy of the project it updates. Once it's done, it's
> done.

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
- **The picture sliders now change the television as you move them.** There is
  no correct amount of curvature, so the only way to set it is to watch the
  screen while you drag — and until now you had to save, look up, change it and
  save again to find the one you wanted. Nothing you are watching is disturbed:
  the programme carries on and simply looks different. What you see while
  dragging is a **preview** and is never written to the box. **Save picture
  settings** keeps it, **Put the saved picture back** undoes the lot in one
  press, and so does closing the browser, the wifi dropping mid-drag, or
  switching the box off at the wall. If the dashboard goes quiet for twenty
  seconds the television puts your saved picture back by itself — nobody can
  leave a half-finished experiment on somebody else's television by wandering
  off.
- **A Files tab: the whole library, from your phone.** Browse the folders, tick
  what you want and delete it — one episode, a whole show, or a mixed handful —
  and rename a folder without breaking the channel that plays from it. This is
  the tab that means you never have to plug a keyboard in.

  **Nothing you delete is destroyed.** It moves to a trash folder on the same
  disk, which means **deleting frees no space at all** until you empty the
  trash — so the box says so on every screen that mentions deleting, and puts
  **Empty the trash** right next to the button that doesn't free anything.
  Anything in the trash for a fortnight is cleared automatically, at start-up
  as well as on a timer, because this box spends most of its life switched off
  at the wall.

  Before anything moves you are told **how many files, how much space, and
  which channels are affected**. Delete a folder a channel plays from and it
  names the channel and says what the television will show instead — colour
  bars and `NO SIGNAL` — until you point it somewhere else or restore the
  folder. Restoring puts things back exactly where they came from, and if
  something has taken the name since, the box asks rather than choosing for
  you; say replace and the file that was there goes to the trash rather than
  away.

  Renaming a folder repoints the channel — and any scheduled block with its own
  folder — in the same step. Both, or neither. And the box's own folders are
  listed but can't be selected, so you can see where your space went without
  being able to delete the machinery by accident.
- **The System page now shows what the trash is holding.** It counts as used
  space on the library disk, and nothing else on the box would have explained
  it.
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
- **The box now notices when it has lost permission to look after itself, and
  says so in words you can act on.** Restarting, rebooting, shutting down,
  setting the clock and everything on the **Network** panel need permission
  that is granted once, when the box is set up. A box set up by an earlier
  version was granted a shorter list, and nothing ever refreshed it: one box
  reached its owner playing video perfectly with **Shut down** working and
  **Restart**, **Reboot** and the whole **Network** page failing, weeks later,
  with an error about a password on a box that has no password. The box now
  checks — every time it starts, and again whenever you open **System** — and
  puts a message at the top of that page naming the buttons that have stopped
  working, what has not happened to your videos or your channels, and the one
  command to run on the box, with your own folder and account already in it.
  There is a **TRY THE REPAIR FROM HERE** button, and it is honest: on a normal
  box it changes nothing and tells you why, because a page with no password on
  it that could grant itself root would be a box anyone on your home network
  could take over. The message tells three different faults apart, including
  one that re-running the installer would not fix, and does not offer the
  command for that one.

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

- **The dashboard no longer answers a button press with a sentence about
  `sudo`.** When a button the box is not allowed to press came back, what you
  were shown was the machine's own words for it — *"sudo: a password is
  required"* — on a box that has no password to type and no keyboard to type
  it on. Every one of those now comes back in English, saying what has stopped
  working, what has not been affected, and the one command that puts it right.
  That covers the Power buttons, the clock, the **Network** page, changing the
  box's name and an update that could not restart the television.
- **...and the Log panel and Copy for support no longer smuggle those words
  back in.** The last few hundred journal lines are what the log panel
  shows and what **Copy for support** puts on your clipboard, and `sudo`'s own
  sentences were sitting in them — so the exact wording the change above exists
  to keep off your screen arrived on it anyway, in the panel directly above the
  notice explaining it. Anything `sudo` wrote about itself is now taken out of
  both. What the box itself wrote around those words stays, and so do the file
  names: those are what tell us which permission is missing when you send the
  bundle in. The unedited line is still in the box's own journal.
- **The dashboard no longer says the television isn't running while it plainly
  is.** The television and the dashboard leave each other two small files in a
  folder the system is supposed to create for the account the box runs as. On
  a box set up by hand that folder is never created, so the files had nowhere
  to go: the dashboard opened perfectly, showed an empty status panel, and
  every button did nothing at all. The two of them now agree on somewhere they
  can both write — whichever one starts first decides for both — so it no
  longer matters whether that folder is there, or turns up later.
- **...and the installer now creates that folder properly, and proves the box
  works before it says so.** Setting the box up by hand never told the system
  to make that folder for an account nobody logs into, and the fault was
  invisible at the time because whoever installs it *is* logged in — so
  everything worked while they watched and stopped the moment they closed the
  window. The installer now does it, checks it, and then waits for the
  television to actually write its status and open the socket the dashboard
  talks to. If either doesn't happen, the install stops and names what is
  missing instead of printing "Done!" over a box whose dashboard can't reach
  its own television. It also refuses to finish over a config the television
  would not start with, checks that both services really stayed up rather than
  merely started, and puts the television ahead of the login prompt so a boot
  no longer flashes "retrobox login:" on the screen first.
- **A box set up by hand no longer waits for a network it hasn't got.** Carried
  to a friend's house and switched on with no cable in it, it could sit showing
  a red *"A start job is running for Wait for Network to be Configured"*
  counting to two minutes on the television before any picture appeared, on
  every cold start. The unattended installer has always prevented that; the
  documented one now does too, and checks it rather than assuming it.
- **A box set up by hand now has the library folder the instructions describe,
  and a starter setup that matches them.** "Anything dropped in becomes a
  channel" was simply not true on that path: the folder was only created as a
  side effect of setting up the file share (so `--no-share` produced a box with
  no library at all), and the starter settings pointed at five folders that did
  not exist, with the "watch this folder" feature switched off. Dragging a
  folder of shows in did nothing, silently, and the dashboard's upload page
  refused to accept anything.
- **An update can no longer leave the box running something it isn't allowed
  to run.** The box may only use a short, named list of commands as root, and
  that list is written down once, when the box is first set up. Nothing ever
  refreshed it — so the first version to add something new to it would have
  installed perfectly, played video perfectly, and left the new button dead, on
  every box in the world on the same day, with nothing in the dashboard to say
  why. An update now asks the box, after the new version is unpacked and before
  the television is restarted into it, whether it is still allowed to do
  everything that version needs. If it isn't, the update is undone, the version
  you had comes back, and **System → Software** shows you the one command to run
  on the box — after which the update installs normally. (That command has to be
  typed on the box itself. The dashboard has no password on it, so it is not
  allowed to hand itself new permissions; if it were, so could anyone else on
  your home network.)
- **A failed rollback no longer tells you the box is working normally.** When
  an update didn't work and the box put the previous version back, it said "It
  is working normally and nothing was lost" whether or not any of that had
  actually worked — a sentence people read off a television and use to decide
  the box can be left until the morning. It now checks each step, names the one
  that didn't finish, and tells you to switch the box off at the wall and on
  again, which really does take the job up again: an unfinished rollback is now
  finished at the next start-up instead of sitting there. The reassuring wording
  is still there for when it is true.
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
- **A network change you kept can no longer be taken away at the next start-up.**
  The box wrote down that you'd kept a change *after* it had already made it
  permanent. If it couldn't finish writing that down — a full disk, a read-only
  filesystem — the note left behind still said the change was on trial, and the
  next start-up dutifully put the old settings back over the ones you'd chosen,
  with nobody watching. It now writes down that it is keeping the change before
  the change is made, and if it can't write that down it doesn't make the change
  at all: you get the previous settings back and a message saying so, which is
  the one direction you can always try again from.
- **A box switched off in the second between pressing *Keep* and it taking
  effect no longer has to guess what you wanted.** It used to simply stand by
  the change. It now reads its own network files at the next start-up and goes
  with whatever is actually in them: if they hold your new settings the change
  stands as kept and the box starts using them there and then; if they hold the
  old ones — which is what a box that had already begun undoing the change looks
  like — it goes back to those and says so. If it cannot read them at all it
  goes back to the previous settings, because a box that cannot tell does not
  get to announce that your change was saved. In every case the **Network** page
  says which of the two happened rather than leaving you to work it out.
- **The one message the Network page could not draw now appears.** If the box
  keeps a change but cannot start using it straight away, the page now says so
  in plain words — *"Kept, but this box could not start using those settings
  straight away. Switch it off and on again if it is not on them yet"* — instead
  of showing nothing at all. Switching it off and on again is the whole of the
  fix, so something had to say it.
- **Settings put back at start-up are now actually used, not just filed.** A box
  that came up on a network setting nobody confirmed rewrote the good settings
  to disk and stopped there — so it went on running the bad ones for the whole
  session, and if those were why you couldn't reach it, there was no page to
  open to try again. It now puts them into effect as it starts, so the box is
  back on the network during that start-up instead of the one after it. If
  netplan won't take them, it says so and the settings still take effect at the
  next restart.
- **Switching the box off at the wall while it saves a network setting can no
  longer leave it with no network at all.** The network file was written
  straight over the live one, which empties it first — and a power cut in that
  moment left half a file in `/etc/netplan`, which stops the box configuring
  *any* adapter, not just the one being changed. That is a box that comes up
  with no network and no dashboard. The new setting is now built in a file
  beside the real one and swapped in as a single step, so the power can go at
  any point and the box comes up on either the old settings or the new ones,
  never on neither.

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
- **Replacing your whole config now has to be asked for deliberately**, the same
  way the backup restore and the factory reset already did. Uploading a config
  throws away every setting on the box in one go, and that shouldn't have been
  something a single unmarked request from anywhere on the network could do. On
  the dashboard nothing changes: choosing the file on **System → Config file**
  is what confirms it, so pick carefully — that one does not ask twice the way
  the buttons next to it do.
- **Your wifi password is no longer handed out over the network.** While a
  network change was on trial, the box kept a note of the settings it would put
  back if you didn't confirm — which, on a wifi box, includes the password. That
  note was going out in full to anything that asked the dashboard what the
  network was set to, and the dashboard has no login by design. It is stripped
  out of every answer now, the note itself can only be read by the box, and it's
  deleted the moment the change is kept or undone. If you'd rather not take the
  word of a changelog, change your wifi password once after updating.
- **A settings file can no longer point the box at somewhere it has no business
  being.** `config.yaml` says where your library is, where the station idents
  are, and where each channel's folder is — and the dashboard, which has no
  password, will accept a whole new one from anybody on your wifi. Those folders
  were taken exactly as written, so a file uploaded from another device could
  aim the box's own upload page at the operating system's directories, or at the
  folder Retro Box itself is installed in, and start writing into it. The box now
  refuses any folder that is part of the system (`/etc`, `/usr`, `/boot` and the
  rest), the software's own folder, your hidden dotfile folders such as `~/.ssh`,
  or your home directory itself. A shortcut left in your media folder is judged
  by where it actually leads, so one dropped over the file share can't quietly
  become a channel pointing somewhere else — including when the box is the one
  turning that folder into a channel for you. Ordinary places are all still
  fine: an external drive, a network share, `~/Videos`.
- **"What counts as a video" is now a fixed list.** The upload page uses that
  setting to decide what it is allowed to write to the disk, so a config that
  added `.py` or `.service` to it turned the page into a way of putting one of
  those on the box. Only real video formats are accepted now — about thirty-five
  of them, generously chosen — and a list with anything else in it is ignored
  in full rather than half-obeyed. Four smaller settings are checked the same
  way: the start-up clip has to name a video, the on-screen font has to be a
  plain font name, the HDMI-CEC helper has to be the standard one, and the audio
  device name has to look like an audio device name.
- **A setting that doesn't pass is ignored, never fatal, and never silent.**
  Whichever of the above is refused, the box drops just that one setting, writes
  the reason in its log, and carries on — a refused channel leaves the rest of
  the lineup playing, because a television that won't start is far worse than
  one channel missing. The dashboard refuses to *save* a config with a refused
  setting in it and tells you exactly what it refused, so nobody ends up
  quietly running a setting they never chose.
- **The name of a folder can no longer rewrite your settings file.** With
  "turn new folders into channels" switched on, the box writes what it finds
  back into `config.yaml` — and anyone on your home network can create a folder
  in your library, over the file share or from the upload page, neither of which
  asks for a password. The folder's name and location were being typed straight
  into the settings file, where several ordinary characters mean something: a
  `#` in a name cut the rest of the line off, a `:` could make the file
  unreadable, and a name containing a line break could add a *whole new setting*
  of its own — including the one that says what the box runs when you switch it
  off, which is exactly the thing that was locked down above. In the worst case
  a folder dropped on the box could put a command of its choosing behind your
  power button, or delete every channel you had set up, with nothing on screen
  to show for it. Names and folders are now written by the same machinery that
  writes the rest of the file, so `Films #2` and `News: at ten` come back as
  themselves; and the box re-reads the finished file before saving it, refusing
  to save anything that doesn't still say what it said plus the new channels.
  Your comments and layout in `config.yaml` are untouched, as before.

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
