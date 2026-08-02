"""Applying a network change that undoes itself if nobody says it worked.

``netplan try`` is exactly this mechanism and it already exists, so this
module drives it rather than inventing a timer: it applies the configuration,
waits for somebody to press ENTER, and reverts on its own if that never
happens or if the process loses its terminal.

That last part is what makes it safe here. ``netplan try`` needs a controlling
terminal, so it is run under a pseudo-terminal; confirming is a newline
written to that terminal. If the dashboard dies, the box loses power, or the
browser never comes back, nobody writes that newline and netplan puts the old
configuration back by itself.

Our own netplan files are a separate matter - netplan reverts what it
*applied*, not what we wrote to disk - so the previous contents are recorded
before anything is written, restored when the window closes, and then *put
into effect*: netplan reads /etc/netplan at boot and when it is told to, so a
file put back and nothing more leaves the box running the settings nobody
wanted for the rest of the session.

The promise runs both ways, and the second half is as load-bearing as the
first. No path here ends with an unconfirmed change still in place - and no
path takes away a change somebody confirmed. That second one is why keeping
is written down *before* the newline that commits it: pressing ENTER cannot
be taken back, so a box that records it afterwards can lose the record to a
full disk and undo, at the next start-up, a change the customer explicitly
kept. A box that cannot write the record does not press ENTER at all.

Which leaves one record that is a statement of intent rather than of fact: a
box found saying "keeping" cannot prove the newline was ever delivered. It is
not guessed at either way. The netplan files are what survives, every path
that gets that far has finished with them, and so whichever configuration is
in ``/etc/netplan`` is the one that was settled on - the box is made to run
it and told which of the two it was. When they cannot be read, nothing can
say, and nothing that cannot say gets to call a change kept: unsure means
back on the previous settings. See :meth:`Probation._settle_interrupted`.

The whole flow, from the user's side:

    press Save -> "testing this, it will undo itself in N seconds"
               -> the page finds the box again
               -> press Keep

and if they never see that page again, the box is already back on the old
settings by the time they go looking.

One thing about how the dashboard is built shapes the whole of this module:
it makes a new :class:`Probation` for every single request. So nothing about
a trial in progress can live on the object. The record lives in a file, and
the running ``netplan try`` lives in a table keyed by the handle that is in
that file - which is what lets the request where somebody presses Keep find
the process that the request where they pressed Save started.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import netconf
from .netconf import NetworkError

log = logging.getLogger(__name__)

#: Long enough for a browser to notice the box moved, find it again and for a
#: person to read a sentence and press a button. Short enough that a box that
#: fell off the network is back within a minute or two.
DEFAULT_TIMEOUT = 120

#: An empty netplan document. Used to neutralise a file we created and cannot
#: delete - the unprivileged process has no way to remove a root-owned file,
#: and an empty document contributes nothing to the merge.
EMPTY_PLAN = "network: {}\n"

#: What the record is called, wherever it is kept. Named here because this
#: module owns the file; the dashboard only decides which directory it sits in.
STATE_NAME = ".retrobox-network.json"

#: The record holds the *previous* contents of the netplan files, and for
#: wireless that is the household's wifi password in plain text. The netplan
#: file itself is 0600 for exactly that reason, and so is this one.
STATE_MODE = 0o600

#: Never handed to a caller. ``previous`` is the way back, which is to say it
#: is the old wifi file, password and all - and the dashboard has no
#: authentication, by design, which is precisely why this list exists.
_PRIVATE_KEYS = ("previous",)

#: How long to wait for ``netplan try`` to finish once it has been told what
#: to do. It applies and exits promptly; this is here so that a netplan which
#: has wedged cannot wedge the dashboard along with it.
STOP_WAIT = 30


class _Trial:
    """One running ``netplan try``, and the terminal that commits it."""

    __slots__ = ("process", "master")

    def __init__(self, process: subprocess.Popen, master: int) -> None:
        self.process = process
        self.master = master


#: Every ``netplan try`` this process has started, by handle.
#:
#: It is module level, and that is the point rather than a shortcut. The
#: dashboard builds a fresh Probation - and so a fresh NetplanTry - for every
#: request, so a process held on the instance was already gone by the time
#: anybody pressed Keep: the trial was invisible to every request after the
#: one that started it. The file on disk carries the handle from one request
#: to the next; this carries the process the handle refers to.
#:
#: Process-wide is the right lifetime, not a compromise. The child holds the
#: far end of a pty this process owns, so if the dashboard dies the terminal
#: dies with it and netplan puts the old configuration back by itself. A
#: handle written by a dashboard that has since gone is simply not in here,
#: and an unknown handle reads as "not running" - which makes the box undo the
#: change rather than keep it. Every way of losing track of a trial lands on
#: revert, which is the only direction that is safe when nobody can reach the
#: box to fix it.
_TRIALS: Dict[str, _Trial] = {}

#: Makes each handle unique within this process, so a handle left over in an
#: old record can never be mistaken for the trial running now.
_HANDLES = itertools.count(1)

#: The dashboard is served threaded, so two changes can arrive at once - two
#: people, or one impatient person clicking twice. Two beginning together
#: would each record the *other's* freshly written file as the way back, and
#: then there would be no way back at all.
_CHANGE = threading.RLock()


#: Said when the files went back but the box could not be made to use them.
#: It is not a failure - the undo happened, and the next start-up picks it up -
#: so it is added to the message rather than replacing it.
NOT_IN_EFFECT = (
    "This box could not put them into effect straight away, so switch it off "
    "and on again if it still cannot be reached."
)

#: Said when a keep the box had to finish for itself is on the disk but is not
#: what the box is running yet. It replaces the plain "these are the box's
#: settings now" rather than being added to it, because that sentence and this
#: one cannot both be true - and the page renders this one, since switching
#: the box off and on again is the whole of the fix and nothing else is going
#: to say so.
KEPT_NOT_IN_EFFECT = (
    "Kept, but this box could not start using those settings straight away. "
    "Switch it off and on again if it is not on them yet."
)


def _put_into_effect() -> str:
    """Make the files that were just put back the ones the box is running.

    Never raises, and never turns a successful undo into a failure. netplan
    reads /etc/netplan at boot and when it is told to, so restoring the file
    is only half the job: without this the box keeps running the configuration
    nobody wanted until it is next switched off at the wall, which on a box
    that configuration has taken off the network means until somebody carries
    a keyboard to it.

    Returns what to add to the message, which is nothing at all when it worked.
    """
    try:
        netconf.apply_plan()
    except AssertionError:
        # The test suite's guard against a command that would touch the real
        # machine. It must stay loud rather than becoming a log line.
        raise
    except Exception:  # noqa: BLE001 - the files are back either way
        log.warning("could not put the restored network configuration into effect",
                    exc_info=True)
        return NOT_IN_EFFECT
    return ""


def _forget(handle: Optional[str]) -> None:
    """Drop a trial and hang up its terminal."""
    trial = _TRIALS.pop(handle, None)
    if trial is None:
        return
    try:
        os.close(trial.master)
    except OSError:
        pass


def _forget_finished() -> None:
    """Let go of trials that have already ended.

    A box can be up for months without the dashboard restarting, and every
    trial holds a file descriptor open until somebody closes it.
    """
    for handle, trial in list(_TRIALS.items()):
        if trial.process.poll() is not None:
            _forget(handle)


class NetplanTry:
    """Drives ``netplan try`` under a pseudo-terminal.

    The instance deliberately holds no trial of its own. Everything a trial
    needs in order to outlive the request that started it is in ``_TRIALS``,
    found again by the handle in the record on disk.
    """

    def __init__(self, timeout_command: Optional[list] = None) -> None:
        self._command = timeout_command or ["sudo", "-n", "netplan", "try"]

    def start(self, timeout: int) -> str:
        import pty

        _forget_finished()
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                self._command + [f"--timeout={int(timeout)}"],
                stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True,
            )
        except OSError as exc:
            os.close(master)
            os.close(slave)
            raise NetworkError(f"could not start netplan try: {exc}") from None
        os.close(slave)
        handle = f"netplan-try-{os.getpid()}.{process.pid}.{next(_HANDLES)}"
        _TRIALS[handle] = _Trial(process, master)
        return handle

    def confirm(self, handle: str) -> None:
        """Press ENTER for the user, which is how netplan try commits.

        Raises if there is no terminal left to press it on, and that is not a
        formality. Without the newline netplan puts the old configuration back
        at its own timeout, so a caller that took silence for success would
        tell somebody the change was kept while the box quietly returned to
        the settings it had before.
        """
        trial = _TRIALS.get(handle)
        if trial is None or trial.process.poll() is not None:
            _forget(handle)
            raise NetworkError(
                "this box can no longer confirm that change - netplan has "
                "already put the previous settings back"
            )
        try:
            os.write(trial.master, b"\n")
        except OSError as exc:
            _forget(handle)
            raise NetworkError(f"could not confirm the network change: {exc}") from None

        try:
            code = trial.process.wait(timeout=STOP_WAIT)
        except subprocess.SubprocessError:
            # Still going. The terminal stays open on purpose: closing it now
            # would hang up on netplan in the middle of applying, which is the
            # very revert we are trying not to cause. The next change tidies
            # it up.
            log.warning("netplan try has not finished after being confirmed",
                        exc_info=True)
            return
        _forget(handle)
        if code != 0:
            raise NetworkError("netplan would not keep that configuration")

    def cancel(self, handle: str) -> None:
        """Stop it now. netplan reverts what it applied when it is interrupted."""
        trial = _TRIALS.get(handle)
        if trial is None:
            return
        if trial.process.poll() is None:
            try:
                trial.process.send_signal(signal.SIGINT)
                trial.process.wait(timeout=STOP_WAIT)
            except (OSError, subprocess.SubprocessError):
                log.warning("could not stop netplan try", exc_info=True)
        _forget(handle)

    def running(self, handle: str) -> bool:
        trial = _TRIALS.get(handle)
        return trial is not None and trial.process.poll() is None


class Probation:
    """One network change at a time, on trial, with the way back recorded.

    Nothing about a change in flight is kept on the object: the dashboard
    makes a new one per request, so the record is the file and the running
    trial is found through the handle inside it.
    """

    def __init__(
        self,
        *,
        state_path: Path,
        writer: Callable[[str, str], None],
        reader: Callable[[str], Optional[str]],
        trier: Any = None,
        clock: Callable[[], float] = time.time,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.state_path = Path(state_path)
        self._write = writer
        self._read = reader
        self._trier = trier if trier is not None else NetplanTry()
        self._clock = clock
        self.timeout = int(timeout)

    # -- what is happening -------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"phase": "idle"}
        return data if isinstance(data, dict) else {"phase": "idle"}

    def _make_private(self) -> None:
        """0600 before anything is written into it, never after.

        The record holds the previous contents of the netplan files, so for a
        wireless change it holds the household's wifi password in plain text -
        which is why the netplan file itself is 0600. ``atomic_write_text``
        carries the target's existing mode across to the replacement, so
        creating the file 0600 here is what makes every later rewrite 0600
        too, with no moment in between where it is readable by anybody else.
        The chmod is for a file an older version of this left behind.
        """
        try:
            os.close(os.open(str(self.state_path),
                             os.O_CREAT | os.O_WRONLY, STATE_MODE))
            os.chmod(self.state_path, STATE_MODE)
        except OSError:
            log.warning("could not make %s private", self.state_path, exc_info=True)

    def _save(self, data: Dict[str, Any], *, must: bool = False) -> bool:
        """Write the record down. Says whether it landed.

        Callers that are about to do something disruptive use that answer: a
        box that cannot write the record is a box that will find the same
        change waiting the next time anybody looks, and do the same thing
        again, and again.
        """
        from .configwrite import atomic_write_text

        self._make_private()
        try:
            atomic_write_text(self.state_path, json.dumps(data, indent=1))
        except OSError:
            if must:
                # The way back is the whole of the safety here. A box that
                # cannot write it down must not make the change, because
                # nothing would then know how to undo it.
                raise NetworkError(
                    "this box could not write down how to undo a network "
                    "change, so it has not made one"
                ) from None
            log.warning("could not record the network change", exc_info=True)
            return False
        return True

    def state(self) -> Dict[str, Any]:
        """What is happening, and revert if the window has closed.

        Checking is what notices the timeout: this is called by the page every
        couple of seconds while a change is on trial, and by anything else
        that asks. A dashboard that restarted mid-probation reverts the moment
        somebody looks, which is the first opportunity it has.
        """
        with _CHANGE:
            data = self._load()
            if data.get("phase") == "keeping":
                # The box stopped between writing down that it was keeping the
                # change and netplan taking it - or the write that should have
                # recorded what happened next never landed. Either way this
                # record is a statement of intent, not of fact.
                return self._public(self._settle_interrupted(data))
            if data.get("phase") != "testing":
                return self._public(data)

            elapsed = self._clock() - float(data.get("started_at") or 0)
            if elapsed < self.timeout and self._still_running(data):
                return self._public(data)

            # Either the window closed or netplan try has already gone. Either
            # way nobody confirmed, so put our files back the way they were.
            log.warning(
                "network change %r was not confirmed in %ss - putting it back",
                data.get("note"), self.timeout,
            )
            return self._undo(data, (
                "The new network settings were not confirmed, so this box put "
                "the previous ones back by itself. Nothing was lost - try again."
            ))

    def _still_running(self, data: Dict[str, Any]) -> bool:
        """Is the trial this record describes actually still on trial?

        A record left behind by a dashboard that has since restarted is not,
        even well inside its window. ``netplan try`` held the far end of a pty
        that died with that process, so netplan has already put its own
        configuration back and our files are the only ones left to undo -
        answering "no" here is what undoes them.

        The owner is checked as well as the handle because it is the one
        answer that does not depend on the trier: a trial started by a process
        that is no longer here is not this one's to keep, whatever anything
        else says.
        """
        owner = data.get("owner_pid")
        if owner is not None and owner != os.getpid():
            log.warning(
                "the network change on trial was started by a dashboard that has "
                "gone - treating it as unconfirmed"
            )
            return False
        return bool(self._trier.running(data.get("handle")))

    def _public(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """What everything outside this module is allowed to see.

        Two jobs, and the second matters more. The countdown, so the page can
        show it - and the way back taken out, because the way back is the
        previous contents of the netplan files. The dashboard puts this
        straight into a GET that has no authentication by design, and the
        household's wifi password is not something to hand to anybody who can
        reach the box.
        """
        out = {k: v for k, v in data.items() if k not in _PRIVATE_KEYS}
        if out.get("phase") == "testing":
            elapsed = self._clock() - float(out.get("started_at") or 0)
            out["seconds_left"] = max(0, round(self.timeout - elapsed))
        else:
            out["seconds_left"] = 0
        return out

    # -- the change --------------------------------------------------------
    def begin(self, files: Dict[str, str], *, note: str) -> Dict[str, Any]:
        """Write the new configuration and put it on trial."""
        with _CHANGE:
            if self.state().get("phase") == "testing":
                raise NetworkError(
                    "a network change is already being tested - keep it or wait for "
                    "it to undo itself first"
                )

            # Recorded before anything is written. It is the only way back.
            previous = {path: self._read(path) for path in files}

            # And written down before anything is written, too. Writing a
            # netplan file is not one step - it is a tee and then a chmod, per
            # file - so the power can go with a new configuration on the disk
            # and no trial running. This record is then the only thing that
            # knows an untested configuration is sitting there; without it the
            # box simply boots into it and keeps it for good. A box that
            # cannot write the record down does not make the change at all.
            data: Dict[str, Any] = {
                "phase": "testing",
                "note": note,
                "handle": None,
                "owner_pid": os.getpid(),
                "previous": previous,
                "started_at": self._clock(),
                "timeout": self.timeout,
                "message": (
                    f"Testing the new settings. If this box cannot be reached, it "
                    f"will put the old ones back by itself within {self.timeout} seconds."
                ),
            }
            self._save(data, must=True)

            try:
                for path, content in files.items():
                    self._write(path, content)
                data["handle"] = self._trier.start(self.timeout)
            except Exception as exc:  # noqa: BLE001 - they all end the same way
                # netplan would not take it, or a file half landed, or the
                # chmod after the tee failed, or the second file failed after
                # the first one was already written. Every one of those leaves
                # a configuration on the disk that nothing has tested and the
                # next boot would apply for good, so it goes back before the
                # error does.
                stuck = self._restore(previous)
                failed: Dict[str, Any] = {
                    "phase": "failed", "note": note,
                    "message": (
                        "That configuration was not applied, and this box put its "
                        "previous network files back. Nothing changed."
                    ),
                }
                if stuck:
                    # Nothing here can tell whether tee landed the new content
                    # before the failure, so "nothing changed" would be a guess
                    # dressed up as an answer. The way back stays in the record
                    # too: it is now the only copy of what those files said.
                    failed["previous"] = previous
                    failed["message"] = (
                        f"That configuration was not applied, and this box could "
                        f"not put {', '.join(stuck)} back the way it was. Check "
                        f"the network settings here before relying on them."
                    )
                self._save(failed)
                if isinstance(exc, NetworkError):
                    raise
                # Something nobody expected. It belongs in the journal, not in
                # a response body on a dashboard with no authentication.
                log.exception("could not put a network change on trial")
                raise NetworkError(
                    "this box could not apply that network configuration, so it "
                    "put the previous one back. Nothing changed."
                ) from None

            # The clock that matters is netplan's, and it starts here rather
            # than before a couple of sudo calls that each take a moment.
            data["started_at"] = self._clock()
            self._save(data)
            return self._public(data)

    def confirm(self) -> Dict[str, Any]:
        with _CHANGE:
            data = self._load()
            if data.get("phase") != "testing":
                raise NetworkError("there is no network change waiting to be confirmed")

            # Said once, because it is the same answer however the trial was
            # lost, and it has to be the truth in every one of those cases.
            gone = (
                "This box could not confirm the new network settings, so the "
                "previous ones are back. Nothing was lost - try again."
            )
            if not self._still_running(data):
                # There is no terminal left to press ENTER on, so netplan has
                # already put its own configuration back or is about to at its
                # own timeout. Reporting this as kept would leave the page
                # saying these are the box's settings now while the box runs
                # the old ones - the one lie this module must never tell.
                self._undo(data, gone)
                raise NetworkError(gone)

            # Written down BEFORE the newline goes to netplan, and it has to
            # land. Pressing ENTER commits the configuration and cannot be
            # taken back, so a record written afterwards is a record that can
            # be lost after the fact - a full disk, a read-only root - leaving
            # one that still says "testing" over a change netplan has already
            # made permanent. The next start-up reads that and puts back
            # settings the customer explicitly kept, which is this module
            # breaking its own promise in the direction nobody is watching.
            committing = dict(data)
            committing["phase"] = "keeping"
            committing["message"] = "Keeping these settings."
            try:
                self._save(committing, must=True)
            except NetworkError:
                # It cannot be recorded, so it must not be done. Nothing has
                # happened yet: netplan still has the old configuration to go
                # back to, and unsure always means back on this box.
                cannot_write = (
                    "This box could not write down that you kept those "
                    "settings, so it has put the previous ones back rather "
                    "than make a change it has no record of. Try again."
                )
                self._undo(data, cannot_write)
                raise NetworkError(cannot_write) from None

            try:
                self._trier.confirm(data.get("handle"))
            except NetworkError:
                log.warning("could not confirm the network change", exc_info=True)
                # `data` still carries the way back; `committing` is only the
                # copy that went to the disk.
                self._undo(data, gone)
                raise NetworkError(gone) from None

            return self._public(self._settle_kept(
                committing, "Kept. These are the box's settings now."
            ))

    def _settle_interrupted(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Finish a keep that stopped part-way, by reading the disk not guessing.

        "keeping" is written down *before* the newline that commits the
        change, so a record that still says it cannot prove the newline was
        ever delivered. Both guesses do harm. Calling it kept tells somebody
        their settings were saved when the box may have put the old ones back -
        which is what happens when the newline could not be delivered, because
        the undo that follows puts the netplan files back and only then writes
        down that it did, and on a box whose disk is full that last write is
        exactly the one that fails. Calling it reverted takes away a change
        somebody explicitly asked for, which is the thing writing "keeping"
        early exists to prevent.

        So it does not guess. Every path that reaches here has already
        finished with the netplan files, and the files are what survives a box
        going off at the wall: a keep that went through left the new
        configuration in /etc/netplan, and an undo left the previous one. So
        whichever is there is the one that was settled on - and the box is
        made to actually run it, because netplan reads /etc/netplan at boot
        and when it is told to and at no other time.

        When the files cannot be read, nothing can say which of the two it is,
        and there is no reading of "cannot tell" that justifies telling
        somebody a change was kept. Unsure means back on the previous
        settings, always: they are the ones known to work.
        """
        previous = data.get("previous") or {}
        if not self._new_configuration_is_still_there(previous):
            log.warning(
                "this box stopped part-way through keeping a network change "
                "and its netplan files are not holding it - putting the "
                "previous settings back"
            )
            return self._undo(data, (
                "This box stopped part-way through keeping those settings, so "
                "it has gone back to the previous ones rather than claim a "
                "change it cannot account for. Nothing was lost - try again."
            ))

        log.warning(
            "this box stopped while keeping a network change - its netplan "
            "files hold the new configuration, so it stands as kept"
        )
        return self._settle_kept(
            data, "Kept. These are the box's settings now.", into_effect=True,
        )

    def _new_configuration_is_still_there(
        self, previous: Dict[str, Optional[str]]
    ) -> bool:
        """Is /etc/netplan still holding the change, rather than the way back?

        Only ever true when every file could be read and at least one of them
        differs from what the record says was there before. A file that would
        not read, or a record with no way back in it at all, answers no.
        """
        if not previous:
            return False
        changed = False
        for path, was in previous.items():
            try:
                now = self._read(path)
            except Exception:  # noqa: BLE001 - unreadable is unreadable
                log.warning("could not read %s to settle a keep", path, exc_info=True)
                return False
            if now is None:
                return False
            # A file that did not exist before is put back by emptying it -
            # an unprivileged process cannot delete a root-owned one - so an
            # empty document is what "it was not there" looks like afterwards.
            if now != (was if was is not None else EMPTY_PLAN):
                changed = True
        return changed

    def _settle_kept(self, data: Dict[str, Any], message: str, *,
                     into_effect: bool = False) -> Dict[str, Any]:
        """Finish a keep: it is in place, and nothing may take it away now.

        The save here is deliberately not ``must``. The record already says
        the change is being kept, and that is the answer a later start-up
        needs; this only tidies the wording and drops the way back. A box that
        cannot write it is a box that says "keeping" for ever, which is
        untidy - and untidy is a great deal better than a box that undoes what
        somebody asked it to keep.

        ``into_effect`` is for the keep this box had to finish for itself.
        After a confirmed trial netplan has already applied the configuration,
        so there is nothing to do; but a keep settled from a stranded record
        has no running ``netplan try`` behind it - its terminal died with
        whatever stopped - so netplan has put its own configuration back and
        the box is running the old settings under a record that says kept.
        """
        data.update({
            "phase": "kept", "finished_at": self._clock(), "message": message,
        })
        # There is nothing left to go back to, so the copy of the old wifi
        # file - password and all - has no reason to stay on the disk.
        data.pop("previous", None)
        recorded = self._save(data)
        if into_effect and recorded:
            # Only once the record says "kept", for the same reason the undo
            # waits: state() is what the page polls every couple of seconds,
            # and a settle nothing could write down would reconfigure every
            # interface on the box every time round.
            note = _put_into_effect()
            if note:
                # The two sentences cannot both be true, so this replaces
                # rather than adds - and the page has a branch for it.
                data["message"] = KEPT_NOT_IN_EFFECT
                data["in_effect"] = False
                self._save(data)
        return data

    def revert(self) -> Dict[str, Any]:
        with _CHANGE:
            data = self._load()
            if data.get("phase") != "testing":
                raise NetworkError("there is no network change to undo")
            return self._undo(data, "Undone. The box is back on its previous settings.")

    def _undo(self, data: Dict[str, Any], message: str) -> Dict[str, Any]:
        """Stop the trial, put our files back, and record that it happened.

        One function because it is one answer: there is no path through this
        module that ends with a change nobody confirmed still in place.
        """
        self._trier.cancel(data.get("handle"))
        previous = data.get("previous") or {}
        stuck = self._restore(previous)
        data.update({
            "phase": "reverted", "finished_at": self._clock(), "message": message,
        })
        if stuck:
            # Saying the box is back on its old settings when a file would not
            # go back is the same lie in a quieter voice, so it says which one
            # and keeps the way back - it is the only copy of it left.
            #
            # And nothing is applied here, deliberately: a file that would not
            # go back still holds the new configuration, so telling netplan to
            # apply now would make the untested one the live one. That is the
            # opposite of an undo.
            data["message"] = (
                f"This box could not put {', '.join(stuck)} back the way it was. "
                f"Check the network settings here before relying on them."
            )
            self._save(data)
            return self._public(data)

        # The way back has been taken, so the copy of the old wifi file stops
        # being useful and starts being only a password in a file.
        data.pop("previous", None)

        # Written down before the disruptive half, and the disruptive half
        # only happens if it landed. Nothing takes this change out of the
        # record except this write, so a box that cannot manage it finds the
        # same change waiting at the next poll of the page and undoes it
        # again - and applying netplan every couple of seconds is a box whose
        # network is down more often than it is up. The files are back either
        # way, so the worst this costs is a restart.
        recorded = self._save(data)
        if previous and recorded:
            # Every file is back, so what is on the disk is the configuration
            # this box is supposed to be running - and now it actually runs
            # it. Only when something really was put back: reconfiguring every
            # interface to apply a file nothing changed is a cost with nothing
            # bought.
            note = _put_into_effect()
            if note:
                data["message"] = f"{data['message']} {note}"
                self._save(data)
        return self._public(data)

    def _restore(self, previous: Dict[str, Optional[str]]) -> list:
        """Put the files back, and say which ones would not go."""
        stuck = []
        for path, content in (previous or {}).items():
            try:
                # A file that did not exist before cannot be deleted from an
                # unprivileged process, so it is emptied instead - an empty
                # netplan document contributes nothing to the merge.
                self._write(path, content if content is not None else EMPTY_PLAN)
            except Exception:  # noqa: BLE001 - a failed restore must still try the rest
                log.error("could not restore %s", path, exc_info=True)
                stuck.append(path)
        return stuck


__all__ = [
    "DEFAULT_TIMEOUT",
    "EMPTY_PLAN",
    "KEPT_NOT_IN_EFFECT",
    "NOT_IN_EFFECT",
    "STATE_MODE",
    "STATE_NAME",
    "STOP_WAIT",
    "NetplanTry",
    "Probation",
]
