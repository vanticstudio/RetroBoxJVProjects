# Two moments on the box worth designing

Findings from reading `retrobox/webui.py`, `updater.py`, `updates.py` and
`installer/provision.sh`. **Nothing here has been implemented.** It is written
down rather than built because another session was editing that code at the
time.

Everything below is evidence-first: file and line for each claim, and the exact
strings that exist today. Check them before acting — the code moves.

---

## The voice these must be written in

Anything added to the dashboard has to be indistinguishable from what is there.
The register is narrow and very consistent:

- **Two sentences, rarely three.** Fact first, consequence second. *"Nothing is
  correcting this clock, so it will drift. Channels that change with the time of
  day will start doing it at the wrong time."* (`webui.py:3640`)
- **No contractions anywhere in the interface.** Zero instances of `n't`, `'ll`,
  `'re` across ~4,655 lines. It writes `cannot`, `is not`, `does not`. The README
  and CHANGELOG do the opposite — that is the documentation dialect, not this one.
- **Server errors are lowercase with no full stop.** All 87 `ApiError` strings
  start lowercase. *"there is no channel {number}"*, *"the config file could not
  be read"*. The word "Error" never appears in one.
- **Front-end confirmations are capitalised, usually with a stop.** *"Kept."*,
  *"Put back."*, *"Channel removed. The video files were left alone."*
- **A spaced hyphen ` - `, never an em dash.** Zero em dashes in `webui.py` prose.
  The dash introduces the consequence or the rescue, never an aside.
- **Bad news is bounded in the same breath.** The sentence saying what went wrong
  is followed by one saying what did not. *"Nothing will be lost - you can simply
  try again."* (`webui.py:4184`)
- **British spelling.** colour, behaviour, synchronised, programme, cancelled.
- **Never**: exclamation marks, emoji, "please", "sorry", "oops", "successfully",
  "all set". None appear anywhere in the repo.
- **Four names, used precisely.** *the television* = the object in the room.
  *the TV* = the process. *this box* = the addressable unit in a fact row.
  *the box* = the unit as an actor.

Two existing slips not to copy: `"Analog snow"` (`webui.py:3932`) and `"Wi-Fi"`
(`webui.py:4132`), against `wifi` 24 times elsewhere.

---

## Moment 1 — the first channel is a placeholder wearing a channel's clothes

### What is there now

`installer/provision.sh:106` writes exactly one channel into a new box's config:

```yaml
channels:
  - number: 2
    name: "Retro Box"
    path: ${MEDIA_ROOT}/.welcome
```

with the comment *"Channel 2 is a placeholder so the box always has a valid
lineup, even with an empty library... Delete this entry once the box has real
content."* The hidden `.welcome` folder receives a copy of
`retrobox/assets/boot_splash.mp4` (`provision.sh:64`).

So a brand-new unit is **not** a zero-channel box. It is a one-channel box whose
only channel loops the JV Projects splash. On the television and on the phone the
owner sees `CH 02 Retro Box`, now playing *"boot splash"* — a title derived by
`_episode_title` (`app.py:1162`).

### Why it matters

This is the literal first frame of the product on both surfaces. Every other
empty state in the codebase is written for a zero-channel box that a shipped unit
never is. And the one thing the owner is looking at is a file they did not add,
presented as programming — on a product whose whole credibility rests on *"It
ships empty; you supply your own files."*

### What to do

Make the placeholder deliberate. It is already the first thing anyone sees; it
should say what it is and how to replace itself, then get out of the way.

- **Name it for what it is.** `"Retro Box"` reads as a channel of content. A name
  that describes its own job does not: `Setup`, or `Start Here`.
- **Say it on the dial.** The viewer at `/` has room under the channel line. When
  the only channel is the welcome one, the meta line should say so plainly.

  > This box is empty. Drop a folder of video on it and that folder becomes a
  > channel. This one goes away when you do.

- **Have it retire itself.** `autochannels.py` already turns new folders on the
  share into channels. When the first real channel appears, the welcome channel
  has done its job — remove it, and say so once:

  > Channel 02 was the welcome channel. It has been taken off the dial now that
  > there is something else to watch.

- **Do not** let it look like supplied programming at any point. No episode
  count, no "now playing" that reads like a title.

### Where

`installer/provision.sh:106-112` (the name and the comment),
`webui.py:2019` and `2115` (the viewer's meta and lineup empty states),
`autochannels.py` (the retirement, when the first real folder lands).

---

## Moment 2 — the television is silent through its own update

### What is there now

The dashboard narrates the television's behaviour on the television's behalf:

| Where | String |
|---|---|
| `webui.py:4526` | *"The television will go quiet for a moment."* |
| `webui.py:4527` | *"That is expected; it comes back on its own."* |
| `updater.py:266` | *"Waiting for the television to come back."* |
| `updater.py:276` | *"The television is back on."* |

Meanwhile `updater.py:262` restarts `retrobox.service`. The picture stops, the
screen is black for up to ninety seconds, and then a splash or a channel appears.
A grep of `overlay.py`, `menu.py` and `crt.py` finds **no update-related string
at all**. The only version the television ever shows is inside the About screen
(`menu.py:149`).

### Why it matters

The one surface everybody in the room is looking at says nothing, and it is the
only surface a person who did *not* press the button can see. From the sofa, the
box has simply broken. Worse, anyone who opens `/` during the window gets the
generic failure face (`webui.py:2637`):

> the TV process is not running

in red, which knows nothing about the update that is deliberately in progress.

### What to do

Give the television a line, and teach the viewer page about updates.

- **On the television**, before the restart, draw the OSD in its own grammar and
  leave it up: green, centred, no more than two lines.

  > UPDATING
  > The picture comes back on its own.

  If the update fails and rolls back, the box should say that too rather than
  returning silently:

  > PUT BACK
  > The update did not come up cleanly, so the previous version is running.

- **On the viewer at `/`**, when an update is in progress, replace the red
  offline face with the calm one that already exists in the console. The state is
  knowable: `_updater().state()` is already returned by `/api/updates`
  (`webui.py:1405`) and the viewer polls every three seconds.

  > The television is updating. It goes quiet for a moment and comes back on its
  > own.

- **Do not** invent a progress bar. The updater reports stages, not percentages;
  a truthful stage name is better than a fake proportion.

### Where

`overlay.py` (a new OSD state), `app.py` (drawing it when the updater's state
file says so), `updater.py:239-276` (the stages that would trigger it),
`webui.py:2637` (the viewer's offline branch).

---

## A bug found on the way, unrelated to any of this

**Automatic rollback across reboots is written but never runs.**

`updater.on_boot()` (`updater.py:351`) returns immediately unless
`state["phase"] == "probation"`. Nothing in `retrobox/` ever writes that phase —
`apply()` writes only `running`, `failed`, `rolling_back`, `rolled_back` and
`success` (`updater.py:202, 212, 239, 274, 309, 322`). And `on_boot()` has no
caller anywhere in `retrobox/`; the only callers are in
`tests/test_updater.py:246-280`.

So `MAX_BOOT_ATTEMPTS = 3` (`updater.py:143`) and the boots counter at
`updater.py:362` never fire, and the string *"the television did not come up
after 3 restarts"* (`updater.py:386`) is unreachable. The panel has no branch for
a `probation` phase either (`webui.py:4425-4443`), so it would render nothing.

The single in-request health check at `updater.py:268` **does** work — it waits
ninety seconds and rolls back — so the product's headline claim still holds and
the website's wording is accurate. What is missing is the multi-boot net: a box
that dies on the *second* boot after a successful update has no way back, because
the phase is `success`, nothing is watching, and `previous_ref` is only reachable
from the dashboard on the box that is down.
