"""Applying a network change that undoes itself if it goes wrong.

The user presses Save, the page says "testing this, it will undo itself in
N seconds", the page finds the box again, and they press Keep. If they never
see that page again - because the change broke connectivity - the box comes
back on the old settings by itself and they try again.

Nothing here ever applies permanently in one step. Not even a change that
looks obviously safe.
"""

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from retrobox import configwrite, netconf, netprobation
from retrobox.netconf import NetworkError
from retrobox.netprobation import NetplanTry, Probation

REPO = Path(__file__).resolve().parent.parent


class FakeApply:
    """Stands in for `netplan apply`, which reconfigures every interface.

    Counted rather than ignored: putting the old file back and putting the old
    file *into effect* are two different things, and only one of them gets the
    customer's box back on the network.
    """

    def __init__(self):
        self.calls = 0
        self.fails = False

    def __call__(self):
        self.calls += 1
        if self.fails:
            raise NetworkError("netplan would not apply that")


@pytest.fixture(autouse=True)
def applied(monkeypatch):
    """No test in this file may reconfigure the machine running the suite."""
    fake = FakeApply()
    monkeypatch.setattr(netconf, "apply_plan", fake)
    return fake


class FakeTry:
    """Stands in for `netplan try` running under a pty."""

    def __init__(self):
        self.started = []
        self.confirmed = 0
        self.cancelled = 0
        self.alive = False
        self.fail_to_start = False

    def start(self, timeout):
        if self.fail_to_start:
            raise NetworkError("netplan refused the configuration")
        self.started.append(timeout)
        self.alive = True
        return "handle"

    def confirm(self, handle):
        self.confirmed += 1
        self.alive = False

    def cancel(self, handle):
        self.cancelled += 1
        self.alive = False

    def running(self, handle):
        return self.alive


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def written(tmp_path):
    """Capture what would have been written to /etc/netplan."""
    store = {}

    def write(path, content):
        store[path] = content

    def read(path):
        return store.get(path)

    write.store = store
    write.read = read
    return write


@pytest.fixture
def probation(tmp_path, written):
    clock = Clock()
    trier = FakeTry()
    p = Probation(
        state_path=tmp_path / "network-change.json",
        writer=written,
        reader=written.read,
        trier=trier,
        clock=clock,
        timeout=120,
    )
    p._test_clock = clock
    p._test_trier = trier
    return p


PLAN = "network:\n  version: 2\n  ethernets:\n    eth0:\n      dhcp4: true\n"

WIRED = "/etc/netplan/90-retrobox-wired.yaml"
WIFI = "/etc/netplan/91-retrobox-wifi.yaml"

#: What is actually in the wifi file: the customer's home network password,
#: in plain text, which is why that file is 0600 on the box.
PASSPHRASE = "the-household-passphrase"
WIFI_PLAN = (
    "network:\n  version: 2\n  wifis:\n    wlan0:\n      dhcp4: true\n"
    "      access-points:\n        Home Network:\n"
    f"          password: {PASSPHRASE}\n"
)


#: A stand-in for `netplan try` that is a real process on a real terminal.
#: It waits for the newline that commits the change, exactly as netplan try
#: does, and writes down whether one ever arrived - so a test can tell the
#: difference between "the dashboard pressed ENTER" and "the dashboard said
#: it had". None of the parts that were getting lost between requests (the
#: child, the pty, the file descriptor) are pretended here.
#:
#: The alarm is so that a test which ends badly cannot leave one of these
#: sitting on the machine running the suite.
WAITS_FOR_ENTER = (
    "import pathlib, signal, sys\n"
    "signal.alarm(15)\n"
    "line = sys.stdin.readline()\n"
    "pathlib.Path(sys.argv[1]).write_text('confirmed' if line else 'hung up')\n"
)

#: The same stand-in, for a netplan that reads the newline and then refuses the
#: configuration anyway. `netplan try` really does this: it applies, waits, and
#: on ENTER runs `netplan apply` - which can fail on a plan that passed
#: validation, so the newline arrives, the change is not kept, and the exit
#: status is the only thing that says so. This is the failure the whole module
#: exists for, so it needs a stand-in that actually produces it.
REFUSES_TO_KEEP_IT = (
    "import pathlib, signal, sys\n"
    "signal.alarm(15)\n"
    "line = sys.stdin.readline()\n"
    "pathlib.Path(sys.argv[1]).write_text('confirmed' if line else 'hung up')\n"
    "sys.exit(4)\n"
)

#: A netplan try that is over before anybody presses anything - which is what
#: the box finds once netplan's own timeout has fired.
ALREADY_GONE = "import sys\nsys.exit(0)\n"


def _requests_like_the_dashboard(tmp_path, written, script, cls=Probation):
    """Probation objects built the way the dashboard builds them.

    A brand new one - and so a brand new NetplanTry - for every single
    request, sharing nothing but the file on disk. That is the whole point:
    the netplan try started when somebody pressed Save has to still be
    findable from the request where they press Keep.
    """
    clock = Clock()
    marker = tmp_path / "pressed-enter"

    def request():
        return cls(
            state_path=tmp_path / "network-change.json",
            writer=written, reader=written.read,
            trier=NetplanTry(timeout_command=[
                sys.executable, "-c", script, str(marker),
            ]),
            clock=clock, timeout=120,
        )

    request.clock = clock
    request.marker = marker
    request.record = tmp_path / "network-change.json"
    return request


@pytest.fixture
def dashboard(tmp_path, written):
    return _requests_like_the_dashboard(tmp_path, written, WAITS_FOR_ENTER)


@pytest.fixture
def netplan_says_no(tmp_path, written):
    """The same dashboard, against a netplan that will not keep the change."""
    return _requests_like_the_dashboard(tmp_path, written, REFUSES_TO_KEEP_IT)


def _wait_for(trier, handle, *, running, why):
    """Wait until a real stand-in process has got where the test needs it."""
    deadline = time.time() + 10
    while trier.running(handle) is not running and time.time() < deadline:
        time.sleep(0.01)
    assert trier.running(handle) is running, why


# ==========================================================================
# Nothing is ever applied in one step
# ==========================================================================
def test_a_change_is_applied_on_probation_not_committed(probation, written):
    probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="DHCP on eth0")

    assert probation._test_trier.started == [120], "it did not go through netplan try"
    assert probation._test_trier.confirmed == 0, "it committed without being asked"
    assert probation.state()["phase"] == "testing"


# The way back is checked in the record on disk rather than in what state()
# hands back, because it must not be in what state() hands back: it is the old
# netplan file, and for wifi that is the household's password, and the
# dashboard puts the reply straight into an unauthenticated GET. Recorded, yes
# - published, never. See the passphrase tests further down.
def test_the_previous_configuration_is_recorded_before_anything_changes(
    probation, tmp_path, written
):
    written.store["/etc/netplan/90-retrobox-wired.yaml"] = "the old one\n"
    probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="x")

    recorded = json.loads((tmp_path / "network-change.json").read_text())
    assert recorded["previous"]["/etc/netplan/90-retrobox-wired.yaml"] == "the old one\n"


def test_a_file_that_did_not_exist_is_recorded_as_absent(probation, tmp_path, written):
    probation.begin({"/etc/netplan/91-retrobox-wifi.yaml": PLAN}, note="x")

    recorded = json.loads((tmp_path / "network-change.json").read_text())
    assert recorded["previous"]["/etc/netplan/91-retrobox-wifi.yaml"] is None


def test_confirming_keeps_it(probation):
    probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="x")
    probation.confirm()

    assert probation._test_trier.confirmed == 1
    assert probation.state()["phase"] == "kept"


def test_confirming_when_nothing_is_on_probation_is_refused(probation):
    with pytest.raises(NetworkError):
        probation.confirm()


def test_a_second_change_while_one_is_on_probation_is_refused(probation):
    probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="x")
    with pytest.raises(NetworkError):
        probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="y")


# ==========================================================================
# Nobody confirms
# ==========================================================================
def test_an_unconfirmed_change_is_reported_as_reverted_once_the_window_passes(probation):
    probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="x")
    probation._test_clock.advance(121)
    probation._test_trier.alive = False        # netplan try has exited on its own

    assert probation.state()["phase"] == "reverted"


def test_the_window_is_still_open_before_the_timeout(probation):
    probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="x")
    probation._test_clock.advance(30)
    assert probation.state()["phase"] == "testing"
    assert probation.state()["seconds_left"] == pytest.approx(90, abs=1)


def test_the_files_are_put_back_when_the_window_closes(probation, written):
    # netplan try reverts what it applied, but our files are ours to undo.
    written.store["/etc/netplan/90-retrobox-wired.yaml"] = "the old one\n"
    probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="x")
    assert written.store["/etc/netplan/90-retrobox-wired.yaml"] == PLAN

    probation._test_clock.advance(121)
    probation._test_trier.alive = False
    probation.state()                          # noticing is what triggers it

    assert written.store["/etc/netplan/90-retrobox-wired.yaml"] == "the old one\n"


def test_a_file_that_did_not_exist_before_is_emptied_on_revert(probation, written):
    probation.begin({"/etc/netplan/91-retrobox-wifi.yaml": PLAN}, note="x")
    probation._test_clock.advance(121)
    probation._test_trier.alive = False
    probation.state()
    # An empty netplan document is inert, and the unprivileged process cannot
    # delete a root-owned file - so it is neutralised rather than removed.
    assert written.store["/etc/netplan/91-retrobox-wifi.yaml"].strip() in ("", "network: {}")


def test_reverting_by_hand_puts_it_back_immediately(probation, written):
    written.store["/etc/netplan/90-retrobox-wired.yaml"] = "the old one\n"
    probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="x")
    probation.revert()

    assert probation._test_trier.cancelled == 1
    assert written.store["/etc/netplan/90-retrobox-wired.yaml"] == "the old one\n"
    assert probation.state()["phase"] == "reverted"


# ==========================================================================
# A keep is not undoable, so it has to be written down first
#
# `netplan try` commits the instant the newline reaches it and nothing after
# that can take it back. This module's promise runs both ways: no path ends
# with an unconfirmed change still in place, and no path takes away a change
# somebody confirmed. The second half is the one that breaks if the record is
# written only after the commit - a full disk or a root gone read-only (which
# is what overlayroot leaves) loses it, the next start-up reads a record that
# still says "testing", and the box quietly puts back settings the customer
# had explicitly kept. Reverting is not the safe direction here: the box is on
# the settings somebody asked for, looked at, and said yes to.
# ==========================================================================
def test_a_keep_netplan_has_already_taken_is_never_undone_at_the_next_start_up(
    tmp_path, written, monkeypatch
):
    clock = Clock()
    trier = FakeTry()
    record = tmp_path / "network-change.json"
    written.store[WIRED] = "the old one\n"
    first = Probation(state_path=record, writer=written, reader=written.read,
                      trier=trier, clock=clock, timeout=120)
    first.begin({WIRED: PLAN}, note="x")

    # The disk goes at the worst possible moment: after netplan has committed,
    # which cannot be taken back, and before the box has finished writing down
    # that it did.
    real = configwrite.atomic_write_text

    def the_disk_goes_once_it_is_kept(path, text, **kw):
        if '"kept"' in text:
            raise OSError("no space left on device")
        return real(path, text, **kw)

    monkeypatch.setattr(configwrite, "atomic_write_text",
                        the_disk_goes_once_it_is_kept)
    first.confirm()
    assert trier.confirmed == 1, "netplan never took it, so this proves nothing"

    # The dashboard comes back: a restart, or the wall switch and a boot.
    reborn = Probation(state_path=record, writer=written, reader=written.read,
                       trier=FakeTry(), clock=clock, timeout=120)
    settled = reborn.state()

    assert written.store[WIRED] == PLAN, (
        "the box undid a network change the customer had explicitly kept, at "
        "start-up, with nobody watching"
    )
    assert settled["phase"] != "reverted", settled


def test_a_box_that_cannot_write_down_a_keep_does_not_press_enter_at_all(
    probation, written, monkeypatch
):
    # The other side of the same ordering. If the record cannot be made
    # durable there is still one moment when nothing has happened yet, and
    # that is the moment to stop: netplan still has the old configuration to
    # go back to, which is the direction this module always takes when it is
    # unsure of anything.
    written.store[WIRED] = "the old one\n"
    probation.begin({WIRED: PLAN}, note="x")

    def the_root_went_read_only(path, text, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(configwrite, "atomic_write_text", the_root_went_read_only)

    with pytest.raises(NetworkError) as caught:
        probation.confirm()

    assert probation._test_trier.confirmed == 0, (
        "netplan committed a change this box had no way of writing down, so "
        "the next start-up will find a record saying it never happened and "
        "take it away again"
    )
    assert written.store[WIRED] == "the old one\n", \
        "it left an untested configuration on the disk for the next boot to keep"
    assert "previous" in str(caught.value).lower() or \
           "back" in str(caught.value).lower(), str(caught.value)


def test_a_keep_still_reads_as_kept_to_the_page_that_asked_for_it(probation):
    # None of the above may change what a working box tells the browser.
    probation.begin({WIRED: PLAN}, note="x")
    assert probation.confirm()["phase"] == "kept"
    assert probation.state()["phase"] == "kept"


def test_a_keep_the_box_stopped_halfway_through_still_stands_at_start_up(
    tmp_path, written
):
    # The record a box leaves when the power goes in the window between
    # writing down that it is keeping the change and netplan taking it. The
    # customer pressed Keep - they had the page in front of them, on the new
    # settings, to press it at all - so what is on the disk is what they asked
    # for, and start-up must not treat "unfinished" as "never happened".
    record = tmp_path / "network-change.json"
    written.store[WIRED] = PLAN
    record.write_text(json.dumps({
        "phase": "keeping", "note": "x", "handle": "a-trial-that-has-gone",
        "owner_pid": os.getpid(),
        "previous": {WIRED: "the old one\n"},
        "started_at": 1000.0, "timeout": 120,
    }))
    p = Probation(state_path=record, writer=written, reader=written.read,
                  trier=FakeTry(), clock=Clock(), timeout=120)

    settled = p.state()
    assert settled["phase"] == "kept", settled
    assert written.store[WIRED] == PLAN, "it took away a change somebody kept"
    assert PASSPHRASE not in record.read_text()
    assert "previous" not in json.loads(record.read_text())


def test_a_keep_settled_at_start_up_is_also_put_into_effect(
    tmp_path, written, applied
):
    """Marked kept is not the same as being used, and the gap has a customer in it.

    The record only says "keeping" if the box stopped between writing that
    down and netplan taking the newline. When it was the *dashboard* that
    stopped rather than the box, netplan try's terminal died with it and
    netplan has already put its own configuration back - so the settings the
    box is running are the old ones while the record now calls the new ones
    kept. Writing "kept" and stopping there leaves that gap open until
    somebody switches the box off at the wall, which on settings that took it
    off the network is a gap nobody can close.
    """
    record = tmp_path / "network-change.json"
    written.store[WIRED] = PLAN
    record.write_text(json.dumps({
        "phase": "keeping", "note": "x", "handle": "a-trial-that-has-gone",
        "owner_pid": os.getpid(),
        "previous": {WIRED: "the old one\n"},
        "started_at": 1000.0, "timeout": 120,
    }))
    p = Probation(state_path=record, writer=written, reader=written.read,
                  trier=FakeTry(), clock=Clock(), timeout=120)

    settled = p.state()
    assert settled["phase"] == "kept", settled
    assert applied.calls == 1, (
        "the box was told its settings were kept while it went on running "
        "the ones netplan put back"
    )

    # And only once: state() is what the page polls, so a settle that applied
    # every time round would take the network down every couple of seconds.
    p.state()
    assert applied.calls == 1


def test_a_keep_settled_at_start_up_that_would_not_apply_says_to_restart_the_box(
    tmp_path, written, applied
):
    # The box is marked kept and is not using the settings. That is worth
    # saying out loud, because switching it off and on again is the fix and
    # nothing else is going to tell anybody.
    applied.fails = True
    record = tmp_path / "network-change.json"
    written.store[WIRED] = PLAN
    record.write_text(json.dumps({
        "phase": "keeping", "note": "x", "handle": "a-trial-that-has-gone",
        "owner_pid": os.getpid(),
        "previous": {WIRED: "the old one\n"},
        "started_at": 1000.0, "timeout": 120,
    }))
    p = Probation(state_path=record, writer=written, reader=written.read,
                  trier=FakeTry(), clock=Clock(), timeout=120)

    settled = p.state()
    assert settled["phase"] == "kept", settled
    assert settled.get("in_effect") is False, (
        "nothing marks this record as one the page has to say something about"
    )
    assert "off and on" in settled["message"], settled["message"]


def test_a_keep_that_was_already_undone_is_never_reported_as_kept(
    tmp_path, written, applied
):
    """The record that says "keeping" over settings that were put back.

    "keeping" is written down before the newline that commits the change, so
    it is a statement of intent and not of fact. When the newline could not be
    delivered the undo runs - it stops the trial and puts the netplan files
    back - and only then writes down that it reverted. On a box whose media
    partition is full, that last write is exactly the one that fails, and the
    only record left on the disk is the "keeping" one from before.

    Reading that as kept tells the customer their settings were saved while
    the box sits on the old ones. The files are the thing that survived, so
    they are what gets believed.
    """
    record = tmp_path / "network-change.json"
    # The undo already put this back; only the record of it was lost.
    written.store[WIRED] = "the old one\n"
    record.write_text(json.dumps({
        "phase": "keeping", "note": "x", "handle": "a-trial-that-has-gone",
        "owner_pid": os.getpid(),
        "previous": {WIRED: "the old one\n"},
        "started_at": 1000.0, "timeout": 120,
    }))
    p = Probation(state_path=record, writer=written, reader=written.read,
                  trier=FakeTry(), clock=Clock(), timeout=120)

    settled = p.state()
    assert settled["phase"] == "reverted", settled
    assert "kept" not in settled["message"].lower(), (
        f"it told the customer their settings were saved while the box runs "
        f"the old ones: {settled['message']!r}"
    )
    assert written.store[WIRED] == "the old one\n"
    # And what is on the disk is what the box is actually running.
    assert applied.calls == 1


def test_a_keep_whose_files_cannot_be_read_puts_the_previous_ones_back(
    tmp_path, written, applied
):
    # Nothing can say which configuration is on the disk, so nothing can say
    # whether the change was kept. Unsure always means back on this box: the
    # previous settings are the ones that were known to work.
    record = tmp_path / "network-change.json"
    record.write_text(json.dumps({
        "phase": "keeping", "note": "x", "handle": "a-trial-that-has-gone",
        "owner_pid": os.getpid(),
        "previous": {WIRED: "the old one\n"},
        "started_at": 1000.0, "timeout": 120,
    }))
    p = Probation(state_path=record, writer=written,
                  reader=lambda path: None,       # sudo cat would not run
                  trier=FakeTry(), clock=Clock(), timeout=120)

    settled = p.state()
    assert settled["phase"] == "reverted", settled
    assert written.store[WIRED] == "the old one\n", \
        "it left the box on a configuration it could not account for"


# ==========================================================================
# Putting the file back is only half of putting it back
#
# netplan reads /etc/netplan at boot and when it is told to, and at no other
# time. `netplan try` reverts what it *applied* when its terminal dies, but
# nothing reverts what it applied when the dashboard was not there to hold
# that terminal - a box that came up on a bad configuration, say. Rewriting
# the file and stopping leaves that box running the settings nobody wanted
# for the whole session, with no dashboard to try again from if those settings
# are why it cannot be reached.
# ==========================================================================
def test_undoing_a_change_puts_the_old_settings_back_into_effect_too(
    probation, written, applied
):
    written.store[WIRED] = "the old one\n"
    probation.begin({WIRED: PLAN}, note="x")
    probation.revert()

    assert applied.calls == 1, (
        "the file went back but the box goes on running the configuration "
        "nobody wanted until somebody switches it off at the wall"
    )


def test_a_change_that_ran_out_of_time_is_put_back_into_effect_as_well(
    probation, written, applied
):
    written.store[WIRED] = "the old one\n"
    probation.begin({WIRED: PLAN}, note="x")
    probation._test_clock.advance(121)
    probation._test_trier.alive = False
    probation.state()                          # noticing is what triggers it

    assert applied.calls == 1


def test_settings_that_would_not_go_back_are_never_put_into_effect(tmp_path, applied):
    # The one case where applying would be the wrong move: a file that would
    # not go back still holds the new configuration, so applying would make
    # the untested one the live one - the exact opposite of an undo.
    store = {WIRED: "the old one\n"}
    jammed = {"now": False}

    def write(path, content):
        if jammed["now"]:
            raise NetworkError("could not write the network configuration")
        store[path] = content

    p = _probation_with(tmp_path, write, store.get, FakeTry())
    p.begin({WIRED: PLAN}, note="x")
    jammed["now"] = True
    p.revert()

    assert applied.calls == 0, (
        "it made the untested configuration the live one while telling "
        "somebody it had put the old one back"
    )


def test_an_undo_the_box_could_not_put_into_effect_says_so_and_still_stands(
    probation, written, applied
):
    # netplan refusing to apply does not un-restore the file, so the undo has
    # happened either way; the box just needs restarting to pick it up. It
    # must not come back as a failure, and it must not claim more than it did.
    applied.fails = True
    written.store[WIRED] = "the old one\n"
    probation.begin({WIRED: PLAN}, note="x")
    undone = probation.revert()

    assert undone["phase"] == "reverted"
    assert written.store[WIRED] == "the old one\n"
    assert "off and on" in undone["message"] or "restart" in undone["message"].lower(), \
        undone["message"]


def test_an_undo_the_box_cannot_write_down_is_not_performed_over_and_over(
    probation, written, monkeypatch, applied
):
    """A box that cannot record an undo must not go on performing it.

    Nothing takes an unconfirmed change out of the record except writing the
    record, so when that write fails the next look at the page finds the same
    change waiting and undoes it again - every couple of seconds, for as long
    as anybody has the page open. Rewriting a file twice is wasteful.
    Reconfiguring every interface twice a second is a box whose network is
    down more often than it is up, and nobody can reach a box like that to
    make it stop. So the disruptive half only happens once the box has
    written down that it happened.
    """
    written.store[WIRED] = "the old one\n"
    probation.begin({WIRED: PLAN}, note="x")

    def the_disk_filled_up(path, text, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(configwrite, "atomic_write_text", the_disk_filled_up)
    probation.revert()
    probation.state()
    probation.state()

    assert applied.calls == 0, (
        "every poll of the network page bounces every interface on the box"
    )
    assert written.store[WIRED] == "the old one\n", "the file did not go back"


def test_nothing_is_applied_when_a_change_never_got_as_far_as_the_files(
    tmp_path, applied
):
    # netplan refused it, so nothing was ever applied and nothing was ever
    # written. Bouncing every interface on the box to fix nothing at all is
    # not a free thing to do.
    store = {WIRED: "the old one\n"}
    trier = FakeTry()
    trier.fail_to_start = True
    p = _probation_with(tmp_path, lambda path, c: store.__setitem__(path, c),
                        store.get, trier)

    with pytest.raises(NetworkError):
        p.begin({WIRED: PLAN}, note="x")
    assert applied.calls == 0


# ==========================================================================
# It survives the page, and the process
# ==========================================================================
def test_the_state_is_on_disk_so_a_reconnecting_page_can_find_it(probation, tmp_path):
    probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN},
                    note="Static 192.168.1.50")
    saved = json.loads((tmp_path / "network-change.json").read_text())

    assert saved["phase"] == "testing"
    assert saved["note"] == "Static 192.168.1.50"
    assert saved["timeout"] == 120


def test_a_confirmed_change_stays_confirmed_across_a_restart(probation, tmp_path, written):
    probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="x")
    probation.confirm()

    # A brand new object, as a restarted dashboard would produce.
    reborn = Probation(
        state_path=tmp_path / "network-change.json",
        writer=written, reader=written.read,
        trier=FakeTry(), clock=probation._test_clock, timeout=120,
    )
    assert reborn.state()["phase"] == "kept"
    assert written.store["/etc/netplan/90-retrobox-wired.yaml"] == PLAN


def test_a_dashboard_that_restarted_mid_probation_still_reverts(tmp_path, written):
    # The dashboard going away must not turn a probation into a commitment.
    clock = Clock()
    first = Probation(
        state_path=tmp_path / "network-change.json", writer=written,
        reader=written.read, trier=FakeTry(), clock=clock, timeout=120,
    )
    written.store["/etc/netplan/90-retrobox-wired.yaml"] = "the old one\n"
    first.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="x")

    clock.advance(200)
    reborn = Probation(
        state_path=tmp_path / "network-change.json", writer=written,
        reader=written.read, trier=FakeTry(), clock=clock, timeout=120,
    )
    assert reborn.state()["phase"] == "reverted"
    assert written.store["/etc/netplan/90-retrobox-wired.yaml"] == "the old one\n"


def test_a_missing_or_corrupt_state_file_reads_as_nothing_happening(tmp_path, written):
    (tmp_path / "network-change.json").write_text("{ truncated")
    p = Probation(
        state_path=tmp_path / "network-change.json", writer=written,
        reader=written.read, trier=FakeTry(), clock=Clock(), timeout=120,
    )
    assert p.state()["phase"] == "idle"


# ==========================================================================
# When netplan itself says no
# ==========================================================================
def test_a_configuration_netplan_refuses_puts_the_files_back(probation, written):
    written.store["/etc/netplan/90-retrobox-wired.yaml"] = "the old one\n"
    probation._test_trier.fail_to_start = True

    with pytest.raises(NetworkError):
        probation.begin({"/etc/netplan/90-retrobox-wired.yaml": PLAN}, note="x")

    assert written.store["/etc/netplan/90-retrobox-wired.yaml"] == "the old one\n"
    assert probation.state()["phase"] in ("idle", "failed")


# ==========================================================================
# The trial has to outlive the request that started it
#
# The dashboard builds a new Probation for every request, so anything the
# trial needs to stay alive - the netplan try process, and the terminal that
# commits it - cannot live on the object. These tests use the real NetplanTry
# against a real child on a real pty, because a shared stub trier hides
# exactly the bug they are here to catch.
# ==========================================================================
def test_a_trial_started_on_one_request_is_still_on_trial_on_the_next(dashboard, written):
    written.store[WIRED] = "the old one\n"
    dashboard().begin({WIRED: PLAN}, note="x")

    later = dashboard().state()
    assert later["phase"] == "testing", "the next request could not see the trial"
    assert later["seconds_left"] > 0
    assert written.store[WIRED] == PLAN, \
        "it put the settings back seconds after they were saved"

    # And a third request can undo the same trial, which is also what stops
    # this test leaving a stand-in netplan try running behind it.
    dashboard().revert()
    assert written.store[WIRED] == "the old one\n"


def test_pressing_keep_on_a_later_request_really_presses_enter(dashboard):
    dashboard().begin({WIRED: PLAN}, note="x")

    assert dashboard().confirm()["phase"] == "kept"
    assert dashboard.marker.exists(), \
        "it reported the change kept without ever confirming it"
    assert dashboard.marker.read_text() == "confirmed"


def test_a_trial_this_box_can_no_longer_reach_is_never_reported_as_kept(
    dashboard, written
):
    # A record left behind by a dashboard that has since restarted: the change
    # is on trial, but the netplan try testing it went with the old process,
    # so netplan has already put its own configuration back. Saying "kept" to
    # that is the one lie this module must never tell.
    written.store[WIRED] = PLAN
    dashboard.record.write_text(json.dumps({
        "phase": "testing", "note": "x", "handle": "netplan-try-that-has-gone",
        "owner_pid": os.getpid(),
        "previous": {WIRED: "the old one\n"},
        "started_at": dashboard.clock.now, "timeout": 120,
    }))

    with pytest.raises(NetworkError):
        dashboard().confirm()

    assert written.store[WIRED] == "the old one\n"
    assert dashboard().state()["phase"] == "reverted"


def test_a_change_left_behind_by_a_dashboard_that_has_gone_is_put_back(
    dashboard, written
):
    # The same record, with nobody pressing anything - just the next look at
    # the page, well inside the window. The safe answer is still to undo it.
    written.store[WIRED] = PLAN
    dashboard.record.write_text(json.dumps({
        "phase": "testing", "note": "x", "handle": "netplan-try-that-has-gone",
        "owner_pid": os.getpid(),
        "previous": {WIRED: "the old one\n"},
        "started_at": dashboard.clock.now, "timeout": 120,
    }))

    assert dashboard().state()["phase"] == "reverted"
    assert written.store[WIRED] == "the old one\n"


def test_a_trial_recorded_by_another_process_is_not_this_one_s_to_keep(
    dashboard, written
):
    # Belt and braces on top of the handle: whatever the trier says, a trial
    # that was started by a process which is no longer here is not ours to
    # commit, and the safe direction on any doubt is to put it back.
    written.store[WIRED] = "the old one\n"
    dashboard().begin({WIRED: PLAN}, note="x")

    record = json.loads(dashboard.record.read_text())
    record["owner_pid"] = os.getpid() + 1
    dashboard.record.write_text(json.dumps(record))

    with pytest.raises(NetworkError):
        dashboard().confirm()
    assert written.store[WIRED] == "the old one\n"


# ==========================================================================
# netplan reads the newline and then refuses the change anyway
#
# This is the failure the whole module exists to handle, and it is the one a
# stand-in that always exits 0 can never produce. `netplan try` does not just
# sit there: on ENTER it runs the apply, and an apply can fail on a plan that
# passed validation - a wifi network that will not associate, a static address
# already taken, a renderer that will not come back up. The newline arrives,
# the change is not kept, and the process's exit status is the only thing on
# the box that knows. Reading it as success means the page says "these are your
# settings now" to somebody whose box has quietly gone back to the old ones -
# or, worse, has no network at all and no dashboard left to say so from.
# ==========================================================================
def test_a_netplan_that_refuses_the_change_on_enter_is_never_reported_as_kept(
    netplan_says_no, written, applied
):
    written.store[WIRED] = "the old one\n"
    netplan_says_no().begin({WIRED: PLAN}, note="x")

    with pytest.raises(NetworkError):
        netplan_says_no().confirm()

    assert netplan_says_no.marker.read_text() == "confirmed", (
        "the newline never reached netplan, so this proves nothing about what "
        "the box does when netplan refuses"
    )
    assert written.store[WIRED] == "the old one\n", (
        "netplan would not keep that configuration and the box left it on the "
        "disk for the next boot to apply for good"
    )
    assert netplan_says_no().state()["phase"] == "reverted"
    assert applied.calls == 1, (
        "the file went back but the box goes on running the configuration "
        "netplan itself refused"
    )


def test_a_netplan_try_that_exits_badly_is_a_failure_even_though_enter_was_pressed(
    tmp_path
):
    # The same thing one level down, where the exit status is actually read.
    # Pressing ENTER is not the success; surviving it is.
    marker = tmp_path / "pressed-enter"
    trier = NetplanTry(timeout_command=[
        sys.executable, "-c", REFUSES_TO_KEEP_IT, str(marker),
    ])
    handle = trier.start(120)

    with pytest.raises(NetworkError) as caught:
        trier.confirm(handle)

    assert marker.read_text() == "confirmed"
    assert "keep" in str(caught.value).lower(), str(caught.value)


# ==========================================================================
# The trial that ends in the gap before the newline
#
# `confirm` looks to see whether the trial is still running and then writes the
# newline that commits it. Those are two steps, and between them sits `netplan
# try`'s own countdown, which belongs to netplan and not to this box. Somebody
# pressing Keep on the last second of the window lands in that gap, and there
# is no lock this process can take that netplan will respect. So the guard is
# reachable on a customer's box even though the check just above it said yes,
# and the only safe answer is the one that admits the change did not happen.
# ==========================================================================
class _TheTrialEndsInTheGap(Probation):
    """netplan's own timeout fires between the check and the newline."""

    def _still_running(self, data):
        alive = super()._still_running(data)
        if alive:
            trial = netprobation._TRIALS[data["handle"]]
            trial.process.kill()
            trial.process.wait(timeout=10)
        return alive


def test_a_trial_that_ends_in_the_gap_before_the_newline_is_not_reported_as_kept(
    tmp_path, written, applied
):
    written.store[WIRED] = "the old one\n"
    request = _requests_like_the_dashboard(
        tmp_path, written, WAITS_FOR_ENTER, cls=_TheTrialEndsInTheGap,
    )
    request().begin({WIRED: PLAN}, note="x")

    with pytest.raises(NetworkError):
        request().confirm()

    assert not request.marker.exists(), \
        "there was no terminal left, so nothing can have pressed ENTER on it"
    assert written.store[WIRED] == "the old one\n", (
        "it told somebody the change was kept while netplan was putting the "
        "old settings back underneath them"
    )
    assert request().state()["phase"] == "reverted"


def test_confirming_a_trial_that_has_already_finished_is_refused_not_passed_over(
    tmp_path
):
    # Silence here would be the module's one unforgivable lie: the caller takes
    # it for success, the page says the new settings are the box's settings
    # now, and netplan has already put the old ones back.
    trier = NetplanTry(timeout_command=[
        sys.executable, "-c", ALREADY_GONE, str(tmp_path / "unused"),
    ])
    handle = trier.start(120)
    _wait_for(trier, handle, running=False, why="the stand-in never exited")

    with pytest.raises(NetworkError) as caught:
        trier.confirm(handle)
    assert "previous" in str(caught.value) or "no longer" in str(caught.value), \
        str(caught.value)


def test_confirming_a_trial_this_process_never_started_is_refused(tmp_path):
    # What a record left behind by a dashboard that has since restarted looks
    # like from down here: a handle naming a trial that is not in this
    # process's table at all.
    trier = NetplanTry(timeout_command=[sys.executable, "-c", ALREADY_GONE])

    with pytest.raises(NetworkError):
        trier.confirm("netplan-try-from-a-dashboard-that-has-gone")


# ==========================================================================
# A change that fails halfway through
#
# write_plan writes the file with tee and then chmods it, so it can fail
# after the new configuration is already on disk - and with two files it can
# fail on the second one after the first has landed. Every one of those paths
# has to end with the box in a state somebody can describe.
# ==========================================================================
def _probation_with(tmp_path, writer, reader, trier):
    return Probation(
        state_path=tmp_path / "network-change.json",
        writer=writer, reader=reader, trier=trier, clock=Clock(), timeout=120,
    )


def test_a_write_that_fails_after_the_file_has_landed_still_puts_it_back(tmp_path):
    store = {WIRED: "the old one\n"}
    trier = FakeTry()

    def write(path, content):
        store[path] = content          # tee got there
        raise NetworkError("could not write the network configuration")

    p = _probation_with(tmp_path, write, store.get, trier)
    with pytest.raises(NetworkError):
        p.begin({WIRED: PLAN}, note="x")

    assert store[WIRED] == "the old one\n", \
        "an untested configuration was left on disk for the next boot to keep"
    assert trier.started == [], "it started a trial it never began"
    assert p.state()["phase"] in ("idle", "failed")


def test_a_pair_of_files_where_the_second_fails_puts_the_first_one_back(tmp_path):
    store = {WIRED: "the old wired one\n", WIFI: "the old wifi one\n"}
    trier = FakeTry()

    def write(path, content):
        if path == WIFI:
            raise NetworkError("could not write the network configuration")
        store[path] = content

    p = _probation_with(tmp_path, write, store.get, trier)
    with pytest.raises(NetworkError):
        p.begin({WIRED: PLAN, WIFI: WIFI_PLAN}, note="x")

    assert store[WIRED] == "the old wired one\n"
    assert trier.started == []
    assert p.state()["phase"] != "testing", "it claims a trial that never started"


def test_the_way_back_is_written_down_before_the_first_file_is_touched(tmp_path):
    # If the power goes between the first file landing and netplan try
    # starting, this record is the only thing that knows an untested
    # configuration is on disk. Without it the box simply boots into it.
    store = {WIRED: "the old one\n"}
    seen = {}

    def write(path, content):
        record = tmp_path / "network-change.json"
        seen.setdefault("at the first write",
                        record.read_text() if record.exists() else "")
        store[path] = content

    p = _probation_with(tmp_path, write, store.get, FakeTry())
    p.begin({WIRED: PLAN}, note="x")

    written_down = json.loads(seen["at the first write"] or "{}")
    assert written_down.get("previous", {}).get(WIRED) == "the old one\n", \
        "the netplan file was written before the way back was recorded"


def test_a_change_that_cannot_be_put_back_says_so_rather_than_nothing_changed(tmp_path):
    # The write failed and putting the file back failed too. Nothing here can
    # tell whether tee landed the new content before the failure, so "nothing
    # changed" would send somebody looking in the wrong place - and dropping
    # the record would take the only copy of what the file used to say with it.
    store = {WIRED: "the old one\n"}

    def write(path, content):
        raise NetworkError("could not write the network configuration")

    p = _probation_with(tmp_path, write, store.get, FakeTry())
    with pytest.raises(NetworkError):
        p.begin({WIRED: PLAN}, note="x")

    settled = p.state()
    assert "nothing changed" not in settled["message"].lower()
    assert WIRED in settled["message"]
    recorded = json.loads((tmp_path / "network-change.json").read_text())
    assert recorded.get("previous", {}).get(WIRED) == "the old one\n", \
        "it threw away the only copy of what that file used to say"


def test_an_undo_that_could_not_finish_keeps_the_way_back(tmp_path):
    store = {WIRED: "the old one\n"}
    jammed = {"now": False}

    def write(path, content):
        if jammed["now"]:
            raise NetworkError("could not write the network configuration")
        store[path] = content

    p = _probation_with(tmp_path, write, store.get, FakeTry())
    p.begin({WIRED: PLAN}, note="x")
    jammed["now"] = True
    undone = p.revert()

    assert WIRED in undone["message"], "it said the box was back on the old settings"
    recorded = json.loads((tmp_path / "network-change.json").read_text())
    assert recorded.get("previous", {}).get(WIRED) == "the old one\n"


def test_a_box_that_cannot_write_down_the_way_back_does_not_change_anything(tmp_path):
    store = {WIRED: "the old one\n"}
    trier = FakeTry()
    p = Probation(
        state_path=tmp_path / "no-such-directory" / "network-change.json",
        writer=lambda path, content: store.__setitem__(path, content),
        reader=store.get, trier=trier, clock=Clock(), timeout=120,
    )

    with pytest.raises(NetworkError):
        p.begin({WIRED: PLAN}, note="x")

    assert store[WIRED] == "the old one\n", \
        "it changed the network with no way of recording how to undo it"
    assert trier.started == []


# ==========================================================================
# The rollback copy is the wifi password
#
# `previous` is the *previous contents* of the netplan files, and for wifi
# that is the household's passphrase in plain text. The dashboard has no
# authentication by design, so this must not leave the module in a response
# body and must not sit in a world-readable file.
# ==========================================================================
def test_the_state_file_is_readable_only_by_this_box(probation, tmp_path, written):
    written.store[WIFI] = WIFI_PLAN
    probation.begin({WIFI: WIFI_PLAN}, note="wifi: Home Network")

    mode = stat.S_IMODE((tmp_path / "network-change.json").stat().st_mode)
    assert mode & 0o077 == 0, \
        f"the wifi password sits in a file anybody can read (mode {mode:o})"


def test_a_state_file_left_readable_by_an_older_version_is_locked_down(
    probation, tmp_path, written
):
    record = tmp_path / "network-change.json"
    record.write_text('{"phase": "idle"}')
    os.chmod(record, 0o644)

    written.store[WIFI] = WIFI_PLAN
    probation.begin({WIFI: WIFI_PLAN}, note="wifi: Home Network")

    assert stat.S_IMODE(record.stat().st_mode) & 0o077 == 0


def test_the_wifi_password_never_appears_in_what_the_dashboard_is_given(
    probation, written
):
    written.store[WIFI] = WIFI_PLAN
    on_trial = probation.begin({WIFI: WIFI_PLAN}, note="wifi: Home Network")

    assert PASSPHRASE not in json.dumps(on_trial), "begin handed back the passphrase"
    assert PASSPHRASE not in json.dumps(probation.state()), \
        "GET /api/network would hand the passphrase to anybody on the LAN"
    assert PASSPHRASE not in json.dumps(probation.confirm())


def test_the_wifi_password_is_not_in_what_an_undo_hands_back(probation, written):
    written.store[WIFI] = WIFI_PLAN
    probation.begin({WIFI: WIFI_PLAN}, note="wifi: Home Network")
    assert PASSPHRASE not in json.dumps(probation.revert())


def test_the_rollback_copy_is_dropped_once_the_change_is_settled(
    probation, tmp_path, written
):
    record = tmp_path / "network-change.json"
    written.store[WIFI] = WIFI_PLAN
    probation.begin({WIFI: WIFI_PLAN}, note="wifi: Home Network")
    probation.confirm()

    assert PASSPHRASE not in record.read_text(), \
        "the passphrase stayed on disk after there was any use for it"


def test_the_rollback_copy_is_dropped_once_a_change_is_undone(
    probation, tmp_path, written
):
    record = tmp_path / "network-change.json"
    written.store[WIFI] = WIFI_PLAN
    probation.begin({WIFI: WIFI_PLAN}, note="wifi: Home Network")
    probation.revert()

    assert PASSPHRASE not in record.read_text()


def test_the_network_state_file_is_not_something_this_repository_commits():
    # It holds the previous contents of the netplan files. This repo is public.
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert netprobation.STATE_NAME in [line.strip() for line in ignored]
