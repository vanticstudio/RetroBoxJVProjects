"""Is there actually a television out there, and dare we act on the answer?

Every test in here builds a synthetic /sys/class/drm tree in tmp_path. The
machine this suite runs on has no /sys, no DRM and no netlink socket, and the
box we ship to is a secondhand mini PC whose graphics chip is unknown until it
is in somebody's hands - so nothing may be hardcoded to one machine's layout
and nothing may touch the real kernel. The event source is stubbed for the
same reason: opening a real netlink socket in a unit test would be both
impossible here and wrong there.

The distinction these tests exist to protect is "no display" versus "cannot
tell". A box that gets that wrong is asleep in front of a working television,
with an owner who cannot SSH in and can only switch it off at the wall.
"""

import os
import socket
import threading
import time

import pytest

from retrobox import display


# ==========================================================================
# Fixture builders - a fake /sys/class/drm
# ==========================================================================
def connector(root, name, status):
    """One DRM connector, laid out the way the kernel lays it out."""
    node = root / name
    node.mkdir(parents=True, exist_ok=True)
    if status is not None:
        (node / "status").write_text(status + "\n")
    return node


def drm_root(tmp_path, **connectors):
    """A /sys/class/drm containing the given ``name: status`` connectors."""
    root = tmp_path / "drm"
    root.mkdir(parents=True, exist_ok=True)
    for name, status in connectors.items():
        connector(root, name.replace("_", "-"), status)
    return root


# ==========================================================================
# Reading the connectors
# ==========================================================================
def test_a_connected_hdmi_connector_means_a_display_is_present(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")

    assert display.read_state(root).state == display.PRESENT


def test_any_connected_output_counts_even_when_another_is_disconnected(tmp_path):
    # A box may have HDMI and DisplayPort and only one of them in use. One
    # television is a television.
    root = drm_root(
        tmp_path,
        card0_HDMI_A_1="disconnected",
        card0_DP_1="connected",
    )

    assert display.read_state(root).state == display.PRESENT


def test_every_connector_disconnected_means_there_is_no_display(tmp_path):
    root = drm_root(
        tmp_path,
        card0_HDMI_A_1="disconnected",
        card0_DP_1="disconnected",
    )

    assert display.read_state(root).state == display.ABSENT


def test_a_box_with_no_connectors_at_all_cannot_tell_rather_than_reporting_no_display(tmp_path):
    root = tmp_path / "drm"
    root.mkdir()

    assert display.read_state(root).state == display.UNKNOWN


def test_a_missing_sysfs_tree_cannot_tell_rather_than_reporting_no_display(tmp_path):
    assert display.read_state(tmp_path / "not-here").state == display.UNKNOWN


def test_a_connector_the_kernel_itself_calls_unknown_prevents_a_confident_no_display(tmp_path):
    # DRM's third status is literally "unknown" - the driver could not probe
    # the connector. Counting that as disconnected is guessing.
    root = drm_root(
        tmp_path,
        card0_HDMI_A_1="disconnected",
        card0_DP_1="unknown",
    )

    assert display.read_state(root).state == display.UNKNOWN


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can read a file with no permission bits, so the fixture cannot be built",
)
def test_a_status_file_that_cannot_be_read_prevents_a_confident_no_display(tmp_path):
    root = drm_root(
        tmp_path,
        card0_HDMI_A_1="disconnected",
        card0_DP_1="disconnected",
    )
    (root / "card0-DP-1" / "status").chmod(0o000)

    assert display.read_state(root).state == display.UNKNOWN


def test_a_status_file_holding_a_word_no_kernel_prints_is_not_read_as_disconnected(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="disconnected", card0_DP_1="")

    assert display.read_state(root).state == display.UNKNOWN


def test_entries_with_no_status_file_are_not_mistaken_for_connectors(tmp_path):
    # /sys/class/drm also holds the card itself, the render node and a
    # version file. None of them is an output and none of them may turn a
    # perfectly good HDMI connector into a "some connector had no status"
    # cannot-tell.
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    (root / "card0").mkdir()
    (root / "renderD128").mkdir()
    (root / "version").write_text("drm 1.1.0 20060810\n")

    reading = display.read_state(root)

    assert reading.state == display.PRESENT
    assert [c.name for c in reading.connectors] == ["card0-HDMI-A-1"]


def test_the_reading_names_every_connector_and_what_it_said(tmp_path):
    # The dashboard has to be able to show why the box thinks what it thinks.
    root = drm_root(
        tmp_path,
        card0_HDMI_A_1="connected",
        card0_DP_1="disconnected",
    )

    reading = display.read_state(root)

    assert {(c.name, c.status) for c in reading.connectors} == {
        ("card0-HDMI-A-1", "connected"),
        ("card0-DP-1", "disconnected"),
    }


def test_a_status_written_with_odd_spacing_or_case_is_still_understood(tmp_path):
    root = tmp_path / "drm"
    root.mkdir()
    node = root / "card0-HDMI-A-1"
    node.mkdir()
    (node / "status").write_text("  Connected  \n")

    assert display.read_state(root).state == display.PRESENT


def test_a_status_the_kernel_has_never_printed_is_treated_as_cannot_tell(tmp_path):
    # If a future kernel invents a fourth word, the box must not read it as
    # "no television" and go to sleep in front of one.
    root = drm_root(tmp_path, card0_HDMI_A_1="something-new")

    assert display.read_state(root).state == display.UNKNOWN


def test_the_real_kernel_path_is_the_default_so_the_box_needs_no_configuration():
    assert str(display.DRM_ROOT) == "/sys/class/drm"


def test_reading_the_default_root_is_possible_without_passing_anything(monkeypatch, tmp_path):
    # The signature must default to the real path rather than requiring the
    # caller to know it. Proven without touching a real /sys.
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    monkeypatch.setattr(display, "DRM_ROOT", root)

    assert display.read_state().state == display.PRESENT


# ==========================================================================
# Saying only what hotplug can honestly tell us
# ==========================================================================
def test_nothing_the_dashboard_renders_claims_standby_can_be_detected():
    # Many televisions hold the hotplug line up in standby and only drop it
    # at the wall switch; some drop it in standby; it varies by maker and
    # there is no standard. Anything that promises "the TV is off" is a lie.
    words = " ".join(
        [display.CAVEAT] + [display.describe(state) for state in display.STATES]
    ).lower()

    assert "standby" in display.CAVEAT.lower()
    assert "some" in display.CAVEAT.lower()
    for forbidden in ("guarantee", "always detects", "reliably detects standby"):
        assert forbidden not in words


def test_every_state_has_plain_language_a_non_technical_owner_can_read():
    for state in display.STATES:
        text = display.describe(state)
        assert text and text[0].isupper() and text.endswith(".")


# ==========================================================================
# Debounce: slow to sleep, instant to wake
# ==========================================================================
def filter_for(**kwargs):
    """A presence filter starting from a known state at t=0."""
    kwargs.setdefault("initial", display.PRESENT)
    kwargs.setdefault("sleep_after", 10.0)
    kwargs.setdefault("wake_after", 0.0)
    kwargs.setdefault("min_awake", 0.0)
    kwargs.setdefault("now", 0.0)
    return display.PresenceFilter(**kwargs)


def test_a_television_switched_off_is_not_believed_until_the_line_has_been_quiet(tmp_path):
    presence = filter_for(sleep_after=10.0)

    assert presence.observe(display.ABSENT, 0.0) is None
    assert presence.observe(display.ABSENT, 9.9) is None
    assert presence.state == display.PRESENT

    assert presence.observe(display.ABSENT, 10.0) == display.ABSENT
    assert presence.state == display.ABSENT


def test_a_line_that_flaps_during_handshake_never_reports_sleep_at_all():
    # HDMI switches, AV receivers and some televisions drop and re-assert the
    # line while they power up and negotiate. That is not somebody switching
    # the television off, and the box must not go quiet in the middle of it.
    presence = filter_for(sleep_after=10.0)

    for second in range(0, 120, 3):
        assert presence.observe(display.ABSENT, float(second)) is None
        assert presence.observe(display.PRESENT, float(second) + 1.0) is None

    assert presence.state == display.PRESENT


def test_waking_is_immediate_because_somebody_is_looking_at_a_black_screen():
    presence = filter_for(initial=display.ABSENT)

    assert presence.observe(display.PRESENT, 0.0) == display.PRESENT
    assert presence.state == display.PRESENT


def test_a_wake_arriving_midway_through_the_sleep_debounce_is_still_immediate():
    presence = filter_for(sleep_after=10.0)

    presence.observe(display.ABSENT, 0.0)
    presence.observe(display.ABSENT, 5.0)
    assert presence.observe(display.PRESENT, 6.0) is None  # never left PRESENT
    assert presence.state == display.PRESENT

    # And the abandoned countdown does not carry over into the next one.
    assert presence.observe(display.ABSENT, 7.0) is None
    assert presence.observe(display.ABSENT, 16.9) is None
    assert presence.observe(display.ABSENT, 17.0) == display.ABSENT


def test_the_box_will_not_go_back_to_sleep_until_it_has_been_awake_a_while():
    # Repeated sleep and wake in quick succession is worse than never
    # sleeping: the fan surges, the picture blinks, and the owner notices.
    presence = filter_for(initial=display.ABSENT, sleep_after=10.0, min_awake=60.0)

    assert presence.observe(display.PRESENT, 100.0) == display.PRESENT

    presence.observe(display.ABSENT, 101.0)
    assert presence.observe(display.ABSENT, 111.0) is None  # debounce satisfied
    assert presence.observe(display.ABSENT, 159.9) is None  # anti-thrash is not
    assert presence.observe(display.ABSENT, 160.0) == display.ABSENT


def test_the_anti_thrash_hold_never_delays_a_wake():
    presence = filter_for(initial=display.ABSENT, min_awake=600.0)

    assert presence.observe(display.PRESENT, 1.0) == display.PRESENT
    presence.observe(display.ABSENT, 2.0)
    presence.observe(display.ABSENT, 700.0)
    assert presence.observe(display.PRESENT, 700.1) == display.PRESENT


def test_cannot_tell_never_puts_the_box_to_sleep():
    presence = filter_for(sleep_after=10.0)

    assert presence.observe(display.UNKNOWN, 0.0) == display.UNKNOWN
    assert presence.state == display.UNKNOWN
    assert presence.should_be_awake(0.0) is True


def test_a_sleeping_box_wakes_the_moment_detection_stops_working():
    # Stuck asleep in front of a working television is the failure that
    # matters. Losing the ability to tell is a reason to wake, immediately.
    presence = filter_for(initial=display.ABSENT, min_awake=600.0)

    assert presence.observe(display.UNKNOWN, 0.0) == display.UNKNOWN
    assert presence.should_be_awake(0.0) is True


def test_an_unchanged_state_is_not_reported_as_a_change():
    presence = filter_for()

    assert presence.observe(display.PRESENT, 0.0) is None
    assert presence.observe(display.PRESENT, 1000.0) is None


def test_both_debounce_intervals_are_configurable():
    slow_to_wake = filter_for(initial=display.ABSENT, wake_after=4.0)

    assert slow_to_wake.observe(display.PRESENT, 0.0) is None
    assert slow_to_wake.observe(display.PRESENT, 4.0) == display.PRESENT

    quick_to_sleep = filter_for(sleep_after=1.0)
    assert quick_to_sleep.observe(display.ABSENT, 0.0) is None
    assert quick_to_sleep.observe(display.ABSENT, 1.0) == display.ABSENT


def test_the_filter_says_when_it_next_needs_looking_at():
    # So a watcher can wait exactly that long instead of waking up to poll.
    presence = filter_for(sleep_after=10.0)

    assert presence.deadline() is None
    presence.observe(display.ABSENT, 5.0)
    assert presence.deadline() == pytest.approx(15.0)
    presence.observe(display.PRESENT, 6.0)
    assert presence.deadline() is None


def test_only_a_confirmed_absence_lets_the_box_sleep():
    presence = filter_for(sleep_after=10.0)

    presence.observe(display.ABSENT, 0.0)
    assert presence.should_be_awake(5.0) is True
    presence.observe(display.ABSENT, 10.0)
    assert presence.should_be_awake(10.0) is False


# ==========================================================================
# The seam a future viewer will use to hold the box awake
# ==========================================================================
def test_something_watching_the_box_another_way_can_hold_it_awake():
    # Nothing calls this yet - there is no live stream viewer. The seam is
    # here so that when one exists, watching in a browser with the television
    # off does not put the box to sleep underneath the viewer.
    presence = filter_for(initial=display.ABSENT)

    assert presence.should_be_awake(0.0) is False
    presence.hold_awake("viewer", now=0.0, seconds=30.0)
    assert presence.should_be_awake(0.0) is True


def test_a_hold_expires_on_its_own_when_whoever_asked_for_it_stops_asking():
    # A browser tab closed on a train has no chance to say goodbye, so the
    # hold has to be a heartbeat rather than a switch.
    presence = filter_for(initial=display.ABSENT)
    presence.hold_awake("viewer", now=0.0, seconds=30.0)

    assert presence.should_be_awake(29.9) is True
    assert presence.should_be_awake(30.0) is False


def test_a_repeated_hold_extends_it_rather_than_stacking_up():
    presence = filter_for(initial=display.ABSENT)
    presence.hold_awake("viewer", now=0.0, seconds=30.0)
    presence.hold_awake("viewer", now=20.0, seconds=30.0)

    assert presence.holds(40.0) == ("viewer",)
    assert presence.should_be_awake(49.9) is True
    assert presence.should_be_awake(50.0) is False


def test_releasing_a_hold_lets_the_box_sleep_again_straight_away():
    presence = filter_for(initial=display.ABSENT)
    presence.hold_awake("viewer", now=0.0, seconds=30.0)
    presence.release_hold("viewer")

    assert presence.holds(0.0) == ()
    assert presence.should_be_awake(0.0) is False


def test_a_hold_does_not_make_the_box_claim_a_television_is_connected():
    # The honest state and the awake decision are different questions, and
    # the dashboard shows the first one.
    presence = filter_for(initial=display.ABSENT)
    presence.hold_awake("viewer", now=0.0)

    assert presence.state == display.ABSENT
    assert presence.should_be_awake(0.0) is True


def test_releasing_a_hold_nobody_is_holding_is_harmless():
    presence = filter_for()
    presence.release_hold("never-existed")


# ==========================================================================
# The watcher - listening rather than polling, and saying which it is doing
# ==========================================================================
class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeSource:
    """Stands in for the kernel uevent feed. The suite opens no sockets."""

    def __init__(self, mode=None, detail="fake", events=(), fail_after=None):
        self.mode = mode or display.MODE_EVENTS
        self.detail = detail
        self.events = list(events)
        self.fail_after = fail_after
        self.waits = []
        self.closed = False
        self.interrupted = False

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.fail_after is not None and len(self.waits) > self.fail_after:
            raise OSError("the kernel feed went away")
        return self.events.pop(0) if self.events else False

    def interrupt(self):
        self.interrupted = True

    def close(self):
        self.closed = True


def watcher_for(root, source=None, clock=None, **kwargs):
    clock = clock or FakeClock()
    source = source if source is not None else FakeSource()
    watcher = display.DisplayWatcher(
        root=root, source=source, clock=clock, **kwargs
    )
    return watcher, source, clock


def test_the_watcher_knows_the_state_as_soon_as_it_starts(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    watcher, _, _ = watcher_for(root)

    watcher.refresh()

    assert watcher.state == display.PRESENT


def test_the_watcher_looks_again_when_the_kernel_says_something_changed(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    watcher, source, clock = watcher_for(root, source=FakeSource(events=[True]))
    watcher.refresh()

    (root / "card0-HDMI-A-1" / "status").write_text("disconnected\n")
    clock.now = 1000.0
    watcher.poll_once()          # the event brings it to look, and it looks
    assert source.waits          # it waited to be told, it did not spin

    clock.now = 2000.0
    watcher.poll_once()          # and the absence, once settled, is believed

    assert watcher.state == display.ABSENT


def test_the_watcher_tells_the_app_only_when_the_believed_state_changes(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    seen = []
    watcher, _, clock = watcher_for(root, on_change=seen.append)

    watcher.refresh()
    watcher.refresh()
    (root / "card0-HDMI-A-1" / "status").write_text("disconnected\n")
    clock.now = 1000.0
    watcher.refresh()            # absence seen, not yet believed: no callback
    assert [snapshot.state for snapshot in seen] == [display.PRESENT]

    clock.now = 2000.0
    watcher.refresh()            # believed now: one callback
    watcher.refresh()            # and nothing has changed since: no more

    assert [snapshot.state for snapshot in seen] == [display.PRESENT, display.ABSENT]


def test_a_callback_that_raises_never_stops_the_watcher(tmp_path):
    # Video first, always. A dashboard or app-layer bug may not take the
    # display watcher down with it.
    def explode(_snapshot):
        raise RuntimeError("the app layer has a bug")

    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    watcher, _, _ = watcher_for(root, on_change=explode)

    watcher.refresh()

    assert watcher.state == display.PRESENT


def test_a_sysfs_read_that_blows_up_leaves_the_box_awake_rather_than_crashing(tmp_path):
    def explode(_root):
        raise OSError("sysfs went away mid-read")

    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    watcher, _, _ = watcher_for(root, reader=explode)

    watcher.refresh()

    assert watcher.state == display.UNKNOWN
    assert watcher.should_be_awake() is True


def test_the_watcher_waits_exactly_as_long_as_a_pending_debounce_needs(tmp_path):
    # Not a fixed tick. Waking the processor to check a file it was going to
    # be told about is the waste this whole feature exists to remove.
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    watcher, source, clock = watcher_for(
        root, sleep_after=10.0, min_awake=0.0, idle_recheck=300.0
    )
    watcher.refresh()

    (root / "card0-HDMI-A-1" / "status").write_text("disconnected\n")
    clock.now = 100.0
    watcher.poll_once()          # sees the absence, starts the countdown
    watcher.poll_once()          # and now waits only for the rest of it

    assert source.waits[-1] == pytest.approx(10.0)
    assert watcher.state == display.PRESENT


def test_the_watcher_waits_a_long_time_when_nothing_is_pending(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    watcher, source, _ = watcher_for(root, idle_recheck=300.0)

    watcher.poll_once()

    assert source.waits[-1] == pytest.approx(300.0)


def test_an_event_driven_watcher_says_so(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    watcher, _, _ = watcher_for(root, source=FakeSource(mode=display.MODE_EVENTS))

    assert watcher.mode == display.MODE_EVENTS
    assert watcher.snapshot().mode == display.MODE_EVENTS


def test_a_slow_poll_is_never_dressed_up_as_event_driven(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    source = FakeSource(mode=display.MODE_POLL, detail="netlink refused")
    watcher, _, _ = watcher_for(root, source=source)

    snapshot = watcher.snapshot()

    assert snapshot.mode == display.MODE_POLL
    assert snapshot.mode != display.MODE_EVENTS
    assert "netlink refused" in snapshot.mode_detail
    assert snapshot.as_dict()["mode"] == display.MODE_POLL


def test_a_poll_fallback_checks_slowly_rather_than_every_second(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    watcher, source, _ = watcher_for(
        root, source=FakeSource(mode=display.MODE_POLL), poll_interval=30.0
    )

    watcher.poll_once()

    assert source.waits[-1] == pytest.approx(30.0)


def test_a_kernel_feed_that_dies_while_running_degrades_to_a_poll_not_to_nothing(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    source = FakeSource(mode=display.MODE_EVENTS, fail_after=0)
    watcher, _, _ = watcher_for(root, source=source)

    watcher.poll_once()

    assert source.closed is True
    assert watcher.mode == display.MODE_POLL
    assert watcher.state == display.PRESENT   # and it kept working


class BrokenSource(FakeSource):
    """A feed that fails with something that is not an OSError.

    Not a hypothetical: ``select.select()`` on a socket somebody else has
    closed raises ``ValueError``, and closing the socket out from under the
    watcher thread is exactly what stopping the box used to do.
    """

    def __init__(self, exc, **kwargs):
        super().__init__(**kwargs)
        self._exc = exc

    def wait(self, timeout):
        self.waits.append(timeout)
        raise self._exc


def test_a_kernel_feed_failing_with_anything_at_all_degrades_to_a_poll(tmp_path):
    """The promise is "degrades to a poll when netlink fails mid-run".

    It only covered OSError, so a ValueError - which is what select() on a
    closed socket raises - went straight past the fallback, out of poll_once
    and into the thread's catch-all. refresh() was then never reached again
    and the believed state froze at whatever it last was.
    """
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    source = BrokenSource(ValueError("file descriptor cannot be a negative integer"))
    watcher, _, _ = watcher_for(root, source=source)

    watcher.poll_once()

    assert source.closed is True
    assert watcher.mode == display.MODE_POLL
    assert watcher.state == display.PRESENT   # and it kept working


def test_a_feed_that_fails_that_way_never_freezes_the_box_at_no_television(tmp_path):
    """Frozen at "absent" is a picture paused for ever with nobody to un-pause it."""
    root = drm_root(tmp_path, card0_HDMI_A_1="disconnected")
    source = BrokenSource(ValueError("I/O operation on closed file"))
    watcher, _, _ = watcher_for(
        root, source=source, sleep_after=0.0, min_awake=0.0, wake_after=0.0,
        poll_interval=1.0,
    )
    watcher.refresh()
    assert watcher.state == display.ABSENT

    # The television comes back on. One turn of the loop the watcher thread
    # runs has to notice it.
    connector(root, "card0-HDMI-A-1", "connected")

    assert watcher.poll_once() == display.PRESENT
    assert watcher.should_be_awake() is True


class StubbornSource:
    """A feed whose thread does not come back the moment it is interrupted.

    Which is the ordinary case, not a pathological one: the real thread can be
    anywhere between two lines of :meth:`wait` when the box is told to stop.
    """

    mode = display.MODE_EVENTS

    def __init__(self):
        self.detail = "a feed that takes its time coming back"
        self.detained = threading.Event()
        self.released = threading.Event()
        self.closed = False

    def wait(self, timeout):
        self.detained.set()
        # A ceiling, purely so a broken test cannot hang the whole suite.
        self.released.wait(30.0)
        return True

    def interrupt(self):
        pass

    def close(self):
        self.closed = True


def test_stopping_never_closes_a_feed_the_watcher_thread_is_still_reading(tmp_path):
    """Closing a socket a live thread is inside select() on IS the frozen state.

    stop() joined with a timeout and then closed the socket regardless. A
    thread that had not come back yet was left selecting on a closed
    descriptor, which raises ValueError for ever after - so the watcher stops
    reaching refresh() and the box keeps believing whatever it believed last.
    A leaked descriptor on a box that is shutting down anyway is the cheap
    mistake; this is the expensive one.
    """
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    source = StubbornSource()
    watcher, _, _ = watcher_for(root, source=source, clock=time.monotonic)
    watcher.start()
    assert source.detained.wait(5.0), "the watcher thread never reached the feed"

    watcher.stop()

    assert source.closed is False
    source.released.set()


def test_stopping_the_watcher_closes_the_event_source(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    watcher, source, _ = watcher_for(root)

    watcher.start()
    watcher.stop()

    assert source.closed is True
    assert source.interrupted is True
    assert watcher.running is False


def test_a_started_watcher_leaves_no_thread_behind(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    watcher, _, _ = watcher_for(root, clock=time.monotonic)

    watcher.start()
    watcher.stop()

    assert not [t for t in threading.enumerate() if t.name.startswith("display-")]


def test_the_snapshot_carries_everything_the_dashboard_has_to_show(tmp_path):
    root = drm_root(
        tmp_path,
        card0_HDMI_A_1="connected",
        card0_DP_1="disconnected",
    )
    watcher, _, _ = watcher_for(root)
    watcher.refresh()

    data = watcher.snapshot().as_dict()

    assert data["state"] == display.PRESENT
    assert data["awake"] is True
    assert data["description"] == display.describe(display.PRESENT)
    assert data["caveat"] == display.CAVEAT
    assert data["mode"] in (display.MODE_EVENTS, display.MODE_POLL)
    assert {"name": "card0-DP-1", "status": "disconnected"} in data["connectors"]
    assert data["holds"] == []


def test_a_hold_taken_through_the_watcher_keeps_the_box_awake(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="disconnected")
    watcher, _, clock = watcher_for(root, sleep_after=0.0, min_awake=0.0)
    watcher.refresh()
    assert watcher.should_be_awake() is False

    watcher.hold_awake("web-viewer", seconds=30.0)

    assert watcher.should_be_awake() is True
    assert watcher.snapshot().as_dict()["holds"] == ["web-viewer"]

    watcher.release_hold("web-viewer")
    assert watcher.should_be_awake() is False


# ==========================================================================
# The kernel uevent feed itself
# ==========================================================================
def test_a_drm_change_message_from_the_kernel_is_recognised():
    message = b"change@/devices/pci0000:00/0000:00:02.0/drm/card0\0ACTION=change\0DEVPATH=/devices/pci0000:00/0000:00:02.0/drm/card0\0SUBSYSTEM=drm\0HOTPLUG=1\0"

    assert display.is_drm_event(message) is True


def test_a_drm_message_relayed_by_udev_is_recognised_too():
    # systemd-udevd rebroadcasts with a binary header in front of the same
    # NUL separated properties, and that is the feed an unprivileged process
    # is most likely to be allowed to hear.
    message = b"libudev\0\xfe\xed\xca\xfe" + b"\0" * 20 + b"ACTION=change\0SUBSYSTEM=drm\0HOTPLUG=1\0"

    assert display.is_drm_event(message) is True


def test_an_event_from_another_subsystem_is_ignored():
    message = b"add@/devices/usb1\0ACTION=add\0SUBSYSTEM=usb\0"

    assert display.is_drm_event(message) is False


def test_a_subsystem_that_merely_starts_with_drm_is_not_mistaken_for_drm():
    message = b"add@/devices/x\0ACTION=add\0SUBSYSTEM=drm_dp_aux_dev\0"

    assert display.is_drm_event(message) is False


def test_rubbish_on_the_socket_is_ignored_rather_than_raising():
    assert display.is_drm_event(b"") is False
    assert display.is_drm_event(b"\xff\xfe\x00\x01") is False


def test_the_box_falls_back_to_a_poll_when_there_is_no_netlink_at_all(monkeypatch):
    # This machine is a Mac and has none. So does a container with the
    # socket denied. Neither may end with the box not watching at all.
    monkeypatch.setattr(display, "_open_uevent_socket", lambda: (None, "no netlink here"))

    source = display.open_event_source()

    assert source.mode == display.MODE_POLL
    assert "no netlink here" in source.detail
    source.close()


def test_the_kernel_feed_is_used_when_the_socket_does_open(monkeypatch):
    opened = {}

    class FakeSocket:
        def close(self):
            opened["closed"] = True

    monkeypatch.setattr(display, "_open_uevent_socket", lambda: (FakeSocket(), "kernel uevents"))

    source = display.open_event_source()

    assert source.mode == display.MODE_EVENTS
    source.close()
    assert opened.get("closed") is True


def test_the_kernel_feed_reports_a_drm_message_the_moment_it_arrives():
    # A socketpair, not a netlink socket: this exercises our own select and
    # drain logic on a machine that has no netlink at all, and the suite
    # still opens nothing that talks to a kernel subsystem.
    ours, theirs = socket.socketpair()
    source = display.UeventSource(ours, "socketpair")
    try:
        theirs.send(b"change@/devices/x\0ACTION=change\0SUBSYSTEM=drm\0HOTPLUG=1\0")

        assert source.wait(1.0) is True
    finally:
        source.close()
        theirs.close()


def test_the_kernel_feed_swallows_a_whole_burst_as_one_question():
    # A television powering up emits several events in a row, and they are
    # all the same question: what is out there now?
    ours, theirs = socket.socketpair()
    source = display.UeventSource(ours, "socketpair")
    try:
        for _ in range(5):
            theirs.send(b"change@/devices/x\0ACTION=change\0SUBSYSTEM=drm\0")
        theirs.send(b"add@/devices/y\0ACTION=add\0SUBSYSTEM=usb\0")

        assert source.wait(1.0) is True
        assert source.wait(0.05) is False   # nothing left queued up
    finally:
        source.close()
        theirs.close()


def test_traffic_from_another_subsystem_does_not_wake_the_box_up_to_look():
    ours, theirs = socket.socketpair()
    source = display.UeventSource(ours, "socketpair")
    try:
        theirs.send(b"add@/devices/usb1\0ACTION=add\0SUBSYSTEM=usb\0")

        assert source.wait(1.0) is False
    finally:
        source.close()
        theirs.close()


def test_the_kernel_feed_can_be_interrupted_without_waiting_out_its_timeout():
    # Shutdown has to be prompt: this thread stands between systemd and a
    # clean stop of the box.
    ours, theirs = socket.socketpair()
    source = display.UeventSource(ours, "socketpair")
    try:
        source.interrupt()
        started = time.monotonic()

        assert source.wait(30.0) is False
        assert time.monotonic() - started < 5.0
    finally:
        source.close()
        theirs.close()


def test_a_poll_source_can_be_interrupted_too():
    source = display.PollSource("no netlink")
    source.interrupt()
    started = time.monotonic()

    assert source.wait(30.0) is False
    assert time.monotonic() - started < 5.0


def test_a_poll_source_that_waited_out_its_interval_says_go_and_look():
    source = display.PollSource("no netlink")

    assert source.wait(0.01) is True


def test_a_watcher_told_never_to_recheck_waits_only_for_an_event(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    watcher, source, _ = watcher_for(root, idle_recheck=0.0)

    watcher.poll_once()

    assert source.waits[-1] is None


# ==========================================================================
# Running it by hand on the box, which is the only place it can be proven
# ==========================================================================
def test_the_module_can_be_run_by_hand_to_show_what_the_box_sees(tmp_path, monkeypatch, capsys):
    root = drm_root(
        tmp_path,
        card0_HDMI_A_1="connected",
        card0_DP_1="disconnected",
    )
    monkeypatch.setattr(display, "DRM_ROOT", root)
    monkeypatch.setattr(display, "open_event_source", lambda: display.PollSource("no netlink"))

    code = display.main([])
    out = capsys.readouterr().out

    assert code == 0
    assert display.PRESENT in out
    assert "card0-HDMI-A-1" in out
    assert "card0-DP-1" in out


def test_running_it_by_hand_says_which_mode_the_box_would_be_in(tmp_path, monkeypatch, capsys):
    # This is the check that tells a human whether the box on their bench is
    # really being told about changes or quietly falling back to a timer.
    monkeypatch.setattr(display, "DRM_ROOT", tmp_path / "nothing")
    monkeypatch.setattr(
        display, "open_event_source", lambda: display.PollSource("netlink refused")
    )

    display.main([])
    out = capsys.readouterr().out

    assert display.MODE_POLL in out
    assert "netlink refused" in out
    assert "standby" in out.lower()   # the caveat travels with the answer


def test_watching_by_hand_stops_cleanly_when_the_person_presses_control_c(tmp_path, monkeypatch, capsys):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    monkeypatch.setattr(display, "DRM_ROOT", root)
    stopped = []

    class StubWatcher:
        state = display.PRESENT

        def __init__(self, **kwargs):
            pass

        def snapshot(self):
            return display.DisplaySnapshot(
                state=display.PRESENT, awake=True, mode=display.MODE_EVENTS,
                mode_detail="stub", detail="stub",
            )

        def poll_once(self):
            raise KeyboardInterrupt

        def refresh(self):
            return None

        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(display, "DisplayWatcher", StubWatcher)

    code = display.main(["--watch"])

    assert code == 0
    assert stopped == [True]


def test_starting_again_after_a_stop_opens_a_fresh_feed_rather_than_a_closed_one(tmp_path):
    # Not an everyday path - the box starts this at boot and stops it at
    # shutdown - but a watcher that silently comes back up holding a closed
    # socket is a watcher that never notices the television again.
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    opened = []

    def factory():
        source = FakeSource()
        opened.append(source)
        return source

    watcher = display.DisplayWatcher(
        root=root, source_factory=factory, clock=FakeClock()
    )

    watcher.start()
    watcher.stop()
    watcher.start()
    watcher.stop()

    assert len(opened) == 2
    assert all(source.closed for source in opened)
