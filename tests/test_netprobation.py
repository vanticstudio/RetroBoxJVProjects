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
from pathlib import Path

import pytest

from retrobox import netprobation
from retrobox.netconf import NetworkError
from retrobox.netprobation import NetplanTry, Probation

REPO = Path(__file__).resolve().parent.parent


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


@pytest.fixture
def dashboard(tmp_path, written):
    """Probation objects built the way the dashboard builds them.

    A brand new one - and so a brand new NetplanTry - for every single
    request, sharing nothing but the file on disk. That is the whole point:
    the netplan try started when somebody pressed Save has to still be
    findable from the request where they press Keep.
    """
    clock = Clock()
    marker = tmp_path / "pressed-enter"

    def request():
        return Probation(
            state_path=tmp_path / "network-change.json",
            writer=written, reader=written.read,
            trier=NetplanTry(timeout_command=[
                sys.executable, "-c", WAITS_FOR_ENTER, str(marker),
            ]),
            clock=clock, timeout=120,
        )

    request.clock = clock
    request.marker = marker
    request.record = tmp_path / "network-change.json"
    return request


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
