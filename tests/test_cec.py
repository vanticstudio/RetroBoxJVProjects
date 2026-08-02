"""What the television itself says about whether it is switched on.

The box has no way of knowing whether anyone is watching. Hotplug stays
asserted by plenty of televisions that are fast asleep, so the only signal that
actually KNOWS is the television saying so over HDMI-CEC.

Almost no box has a CEC adapter and almost no television ships with CEC turned
on, so all of this is an upgrade path. The tests below care most about the
three states being kept apart - "no adapter", "listening but the television has
said nothing", and "the television said standby" - because only the last one is
allowed to make the room go quiet, and getting that wrong blanks the screen of
somebody who is watching television.
"""

import logging

import pytest

from retrobox.actions import Action, InputEvent
from retrobox.input import cec
from retrobox.input.base import InputBackend
from retrobox.input.cec import (
    CEC_ABSENT,
    CEC_ON,
    CEC_STANDBY,
    CEC_UNKNOWN,
    CecBackend,
    cec_display_power,
)


# --------------------------------------------------------------------------
# Stubs. Nothing here may spawn a real cec-client - this machine has none, and
# the box must not have its adapter poked by the test suite either.
# --------------------------------------------------------------------------
#: Put this in a line list to make the pipe break mid-stream.
BOOM = object()


class FakeStdout:
    """Stands in for the pipe cec-client writes its chatter to."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return self

    def __next__(self):
        if not self._lines:
            raise StopIteration
        line = self._lines.pop(0)
        if line is BOOM:
            raise RuntimeError("the pipe broke")
        return line


class FakeProc:
    def __init__(self, lines):
        self.stdout = FakeStdout(lines)
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


@pytest.fixture
def cec_client(monkeypatch):
    """An adapter that is present, and a cec-client that says what we choose."""
    started = {}

    def fake_which(name):
        return "/usr/bin/" + str(name)

    def fake_popen(cmd, **kwargs):
        proc = FakeProc(started.get("lines", []))
        started["cmd"] = cmd
        started["proc"] = proc
        return proc

    monkeypatch.setattr(cec.shutil, "which", fake_which)
    monkeypatch.setattr(cec.subprocess, "Popen", fake_popen)
    return started


def run_with(cec_client, lines, **kwargs):
    """Drive a whole cec-client session and return (backend, states seen)."""
    seen = []
    cec_client["lines"] = lines
    backend = CecBackend(power_observer=lambda power: seen.append(power.state), **kwargs)
    backend._run()
    return backend, seen


def feed(backend, *lines):
    for line in lines:
        backend._handle_line(line)
    return backend.display_power()


# Real cec-client traffic, as printed with `-d 8`.
STANDBY = "TRAFFIC: [            8000]     >> 0f:36\n"
POWER_ON = "TRAFFIC: [            8100]     >> 01:90:00\n"
POWER_STANDBY = "TRAFFIC: [            8200]     >> 01:90:01\n"


# ==========================================================================
# Three states, not two
# ==========================================================================
def test_a_box_with_no_cec_adapter_at_all_reports_absent():
    # Most boxes. This must never be mistaken for "the television is off".
    assert CecBackend().display_power().state == CEC_ABSENT


def test_an_adapter_that_is_listening_but_has_heard_nothing_is_not_absent(cec_client):
    _, seen = run_with(cec_client, [])
    assert seen[0] == CEC_UNKNOWN, "a live adapter that has heard nothing is its own state"


def test_the_television_saying_standby_is_the_only_state_that_may_sleep_the_box(cec_client):
    backend, _ = run_with(cec_client, [])
    assert feed(backend, STANDBY).state == CEC_STANDBY


def test_only_standby_ever_says_the_display_is_off(cec_client):
    backend, _ = run_with(cec_client, [])
    assert CecBackend().display_power().says_off is False, "no adapter is not off"
    backend._set_power(CEC_UNKNOWN, "listening")
    assert backend.display_power().says_off is False, "heard nothing is not off"
    assert feed(backend, POWER_ON).says_off is False
    assert feed(backend, STANDBY).says_off is True


def test_the_three_silent_states_are_all_told_apart(cec_client):
    absent = CecBackend().display_power()
    listening, _ = run_with(cec_client, [])
    listening._set_power(CEC_UNKNOWN, "listening")
    asleep, _ = run_with(cec_client, [])
    feed(asleep, STANDBY)

    states = {absent.state, listening.display_power().state, asleep.display_power().state}
    assert states == {CEC_ABSENT, CEC_UNKNOWN, CEC_STANDBY}


def test_a_known_state_is_marked_as_known_and_the_silent_ones_are_not(cec_client):
    backend, _ = run_with(cec_client, [])
    assert backend.display_power().is_known is False
    assert feed(backend, POWER_ON).is_known is True


# ==========================================================================
# Reading the television's own words
# ==========================================================================
def test_the_television_broadcasting_standby_turns_the_display_off(cec_client):
    backend, _ = run_with(cec_client, [])
    assert feed(backend, STANDBY).state == CEC_STANDBY


def test_a_television_reporting_power_on_puts_the_display_back_on(cec_client):
    backend, _ = run_with(cec_client, [])
    feed(backend, STANDBY)
    assert feed(backend, POWER_ON).state == CEC_ON


def test_a_television_reporting_standby_turns_the_display_off(cec_client):
    backend, _ = run_with(cec_client, [])
    assert feed(backend, POWER_STANDBY).state == CEC_STANDBY


def test_a_television_still_waking_up_counts_as_on(cec_client):
    # 0x02 is "in transition from standby to on" - it is coming back, so the
    # box must already be awake by the time a picture appears.
    backend, _ = run_with(cec_client, [])
    assert feed(backend, "TRAFFIC: [ 1] >> 01:90:02\n").state == CEC_ON


def test_a_television_on_its_way_to_standby_counts_as_standby(cec_client):
    backend, _ = run_with(cec_client, [])
    assert feed(backend, "TRAFFIC: [ 1] >> 01:90:03\n").state == CEC_STANDBY


def test_a_standby_sent_between_two_other_devices_is_not_the_television(cec_client):
    # Initiator 5 telling device 1 to sleep is somebody else's business. The
    # screen is only dark when the television says so, or when the whole room
    # is told to sleep at once.
    backend, _ = run_with(cec_client, [])
    feed(backend, POWER_ON)
    assert feed(backend, "TRAFFIC: [ 1] >> 51:36\n").state == CEC_ON


def test_a_power_report_from_something_that_is_not_the_television_is_ignored(cec_client):
    # A sound bar in standby says nothing about the screen.
    backend, _ = run_with(cec_client, [])
    feed(backend, POWER_ON)
    assert feed(backend, "TRAFFIC: [ 1] >> 51:90:01\n").state == CEC_ON


def test_the_television_asking_who_is_on_screen_means_it_is_awake(cec_client):
    # A television broadcasts "request active source" as it wakes.
    backend, _ = run_with(cec_client, [])
    feed(backend, STANDBY)
    assert feed(backend, "TRAFFIC: [ 1] >> 0f:85\n").state == CEC_ON


def test_another_device_claiming_the_screen_means_the_television_is_awake(cec_client):
    backend, _ = run_with(cec_client, [])
    feed(backend, STANDBY)
    assert feed(backend, "TRAFFIC: [ 1] >> 4f:82:10:00\n").state == CEC_ON


def test_the_television_switching_input_means_it_is_awake(cec_client):
    backend, _ = run_with(cec_client, [])
    feed(backend, STANDBY)
    assert feed(backend, "TRAFFIC: [ 1] >> 0f:86:20:00\n").state == CEC_ON


def test_a_remote_button_arriving_over_cec_means_the_television_is_awake(cec_client):
    # A television in standby does not forward its remote to HDMI devices.
    backend, _ = run_with(cec_client, [])
    feed(backend, STANDBY)
    assert feed(backend, "key pressed: up (1)\n").state == CEC_ON


def test_the_power_button_alone_does_not_claim_the_television_is_awake(cec_client):
    # Televisions send the power key on their way to standby. Believing it
    # would flip the box awake a moment before the screen goes dark.
    backend, _ = run_with(cec_client, [])
    feed(backend, STANDBY)
    assert feed(backend, "key pressed: power (40)\n").state == CEC_STANDBY


def test_what_this_box_sends_is_not_the_television_speaking(cec_client):
    # "<<" is outgoing. Only "TRAFFIC: >>" is somebody else talking.
    backend, _ = run_with(cec_client, [])
    feed(backend, POWER_ON)
    assert feed(backend, "TRAFFIC: [ 1] << 4f:36\n").state == CEC_ON


def test_a_poll_with_no_opcode_says_nothing_about_power(cec_client):
    # Televisions answer polls while fast asleep, which is exactly why a
    # bare ping must not be read as "awake".
    backend, _ = run_with(cec_client, [])
    feed(backend, STANDBY)
    assert feed(backend, "TRAFFIC: [ 1] >> 0f\n").state == CEC_STANDBY


def test_the_moment_the_state_changed_is_recorded(cec_client):
    # So the rest of the box can tell a standby heard ten seconds ago from one
    # heard before the last power cut.
    backend = CecBackend(clock=lambda: 4242.0)
    assert feed(backend, STANDBY).at == 4242.0


# ==========================================================================
# A chatty or broken cec-client must still leave the box playing television
# ==========================================================================
def test_an_unknown_opcode_leaves_the_last_known_state_alone(cec_client):
    backend, _ = run_with(cec_client, [])
    feed(backend, POWER_ON)
    assert feed(backend, "TRAFFIC: [ 1] >> 0f:32:65:6e\n").state == CEC_ON


def test_an_unknown_power_value_leaves_the_last_known_state_alone(cec_client):
    backend, _ = run_with(cec_client, [])
    feed(backend, POWER_ON)
    assert feed(backend, "TRAFFIC: [ 1] >> 01:90:7f\n").state == CEC_ON


def test_output_in_a_shape_nobody_expected_is_simply_ignored(cec_client):
    backend, _ = run_with(cec_client, [])
    feed(backend, POWER_ON)
    for junk in ("", "\n", ">>\n", ">> zz:36\n", "TRAFFIC: >> 0f:\n", "\x00\xff garbage"):
        assert feed(backend, junk).state == CEC_ON, junk


def test_a_line_that_never_ends_is_not_trusted(cec_client):
    # A runaway line is not a message. Reading a standby out of the tail of a
    # megabyte of noise would put the room to sleep for no reason.
    backend, _ = run_with(cec_client, [])
    feed(backend, POWER_ON)
    runaway = "x" * 4_000_000 + ">> 0f:36"
    assert feed(backend, runaway).state == CEC_ON


def test_the_next_line_after_a_runaway_one_is_still_understood(cec_client):
    backend, _ = run_with(cec_client, [])
    feed(backend, "y" * 200_000)
    assert feed(backend, STANDBY).state == CEC_STANDBY


def test_a_flood_of_traffic_does_not_wake_the_display_over_and_over(cec_client):
    # Two-core Celeron. Telling the rest of the box the same thing ten
    # thousand times is how a cheap machine ends up thrashing.
    backend, seen = run_with(cec_client, [POWER_ON] * 5000)
    assert seen.count(CEC_ON) == 1


def test_a_flood_of_traffic_does_not_keep_moving_the_timestamp(cec_client):
    backend, _ = run_with(cec_client, [])
    when = feed(backend, POWER_ON).at
    for _ in range(50):
        feed(backend, POWER_ON)
    assert backend.display_power().at == when, "the state never changed"


def test_an_adapter_that_flaps_does_not_fill_the_journal(cec_client, caplog):
    # A television that changes its mind forty times a minute is a broken
    # adapter, not a household - and this box writes its log to eMMC that
    # wears out. The flapping must still be *followed*, so the screen can
    # always come back; it just stops being written down every time.
    backend = CecBackend(clock=lambda: 1000.0)
    with caplog.at_level(logging.INFO, logger=cec.log.name):
        for _ in range(40):
            feed(backend, POWER_ON)
            feed(backend, STANDBY)

    written = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert len(written) < 20, "eighty state changes wrote eighty log lines"
    assert backend.display_power().state == CEC_STANDBY, "it stopped following"


def test_a_flapping_adapter_can_still_wake_the_display(cec_client):
    # Whatever the log does, the state must never stop tracking the last
    # thing the television said. Missing a wake leaves somebody staring at
    # a black screen with the television plainly switched on.
    seen = []
    backend = CecBackend(clock=lambda: 1000.0, power_observer=lambda p: seen.append(p.state))
    for _ in range(40):
        feed(backend, STANDBY)
        feed(backend, POWER_ON)

    assert seen.count(CEC_ON) == 40, "a wake was swallowed"
    assert backend.display_power().state == CEC_ON


def test_cec_client_dying_takes_its_standby_claim_with_it(cec_client):
    # If the reader stops, nothing will ever tell us the television came back.
    # Holding on to "standby" would leave the box asleep until the wall switch.
    backend, seen = run_with(cec_client, [STANDBY])
    assert seen == [CEC_UNKNOWN, CEC_STANDBY, CEC_ABSENT]
    assert backend.display_power().state == CEC_ABSENT


def test_cec_client_failing_to_start_leaves_the_box_with_no_cec(monkeypatch):
    monkeypatch.setattr(cec.shutil, "which", lambda name: "/usr/bin/cec-client")

    def explode(*a, **k):
        raise OSError("no such device")

    monkeypatch.setattr(cec.subprocess, "Popen", explode)
    backend = CecBackend()
    backend._run()
    assert backend.display_power().state == CEC_ABSENT


def test_a_reader_that_blows_up_mid_stream_still_gives_up_its_claim(cec_client):
    seen = []
    cec_client["lines"] = [STANDBY, BOOM]
    backend = CecBackend(power_observer=lambda power: seen.append(power.state))
    with pytest.raises(RuntimeError):
        backend._run()
    assert backend.display_power().state == CEC_ABSENT, "a dead reader cannot know"


def test_one_bad_line_does_not_stop_the_remote_working(cec_client, monkeypatch):
    backend, _ = run_with(cec_client, [])
    boom = {"first": True}
    real = cec.cec_key_to_event

    def sometimes(name):
        if boom["first"]:
            boom["first"] = False
            raise ValueError("unexpected")
        return real(name)

    monkeypatch.setattr(cec, "cec_key_to_event", sometimes)
    backend._handle_line("key pressed: up (1)\n")
    backend._handle_line("key pressed: down (2)\n")
    assert backend._queue.get(timeout=1.0) == InputEvent(Action.CHANNEL_DOWN)


def test_an_exploding_power_watcher_does_not_stop_the_remote_working(cec_client):
    def boom(power):
        raise RuntimeError("the display code is broken")

    backend = CecBackend(power_observer=boom)
    backend._handle_line(STANDBY)
    backend._handle_line("key pressed: up (1)\n")
    assert backend._queue.get(timeout=1.0) == InputEvent(Action.CHANNEL_UP)


# ==========================================================================
# The button presses this backend already existed to deliver
# ==========================================================================
def test_the_button_press_is_delivered_before_the_display_is_told_anything(cec_client):
    # The remote is the product. Whatever the display code does with "the
    # television is awake" - and it may well be slow - it happens after the
    # press is already on its way.
    queued = []
    backend = CecBackend(power_observer=lambda power: queued.append(backend._queue.qsize()))
    backend._handle_line("key pressed: up (1)\n")
    assert queued == [1], "the display watcher went first"


def test_button_presses_still_reach_the_queue(cec_client):
    backend, _ = run_with(cec_client, ["key pressed: up (1)\n"])
    assert backend._queue.get(timeout=1.0) == InputEvent(Action.CHANNEL_UP)


def test_a_traffic_line_is_not_mistaken_for_a_button(cec_client):
    backend, _ = run_with(cec_client, [STANDBY])
    assert backend._queue.empty()


# ==========================================================================
# The absence of CEC is normal, not a fault
# ==========================================================================
def test_no_adapter_is_never_reported_as_a_problem(monkeypatch, caplog):
    monkeypatch.setattr(cec.shutil, "which", lambda name: None)
    backend = CecBackend()
    with caplog.at_level(logging.DEBUG):
        backend._run()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert backend.display_power().state == CEC_ABSENT


# ==========================================================================
# What the rest of the box asks
# ==========================================================================
def test_a_watcher_can_be_attached_after_the_backend_was_built(cec_client):
    # create_backends() makes the backend, so the display code never gets to
    # pass anything to the constructor. It has to be able to ask afterwards.
    seen = []
    backend, _ = run_with(cec_client, [])
    backend.watch_power(lambda power: seen.append(power.state))
    feed(backend, STANDBY)
    assert seen[-1] == CEC_STANDBY


def test_attaching_a_watcher_tells_it_where_things_stand_right_away(cec_client):
    # Otherwise a watcher that arrives after the television already said
    # standby waits for a change that may not come for hours.
    backend, _ = run_with(cec_client, [])
    feed(backend, STANDBY)
    seen = []
    backend.watch_power(lambda power: seen.append(power.state))
    assert seen == [CEC_STANDBY]


def test_a_box_with_no_cec_backend_running_reads_as_absent():
    assert cec_display_power([]).state == CEC_ABSENT


def test_the_cec_backend_is_found_among_the_other_input_backends(cec_client):
    class Other(InputBackend):
        name = "keyboard"

        def _run(self):
            pass

    backend, _ = run_with(cec_client, [])
    feed(backend, STANDBY)
    assert cec_display_power([Other(), backend, Other()]).state == CEC_STANDBY
