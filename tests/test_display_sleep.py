"""Going quiet when there is no television watching.

The box lives in a cabinet behind the set. When the set is off it has been
decoding video for nobody: a fan roaring into an empty room, heat pumped into
secondhand hardware, and a machine that plainly does not notice whether anyone
is there. This is the half of that feature that lives in the television
process - the part that pauses the picture and, far more importantly, the part
that brings it back in the right place.

Nothing here reads a real /sys tree or opens a real netlink socket. The
watcher's own module has its tests for that; these build a tree of files, hand
the watcher an event source that only ever waits, and drive the clock by hand.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import pytest

from retrobox import display
from retrobox.actions import Action, InputEvent
from retrobox.app import TVApp, make_display_watcher
from retrobox.config import config_from_dict
from retrobox.input.cec import CEC_ON, CEC_STANDBY, CEC_UNKNOWN, CecPower
from retrobox.input.manager import InputManager
from retrobox.player import MockPlayer
from tests.helpers import FakeClock, at_local, make_show


# ==========================================================================
# The stand-ins
# ==========================================================================
class StubWatcher:
    """Stands in for :class:`retrobox.display.DisplayWatcher`.

    The real one opens a netlink socket and reads /sys/class/drm, and this
    machine has neither. What the television process actually depends on is
    five methods, so those are what this offers - and ``see`` lets a test say
    "the television was switched off" in one line.
    """

    def __init__(self, state: str = display.UNKNOWN) -> None:
        self._state = state
        self.on_change = None
        self.starts = 0
        self.stops = 0
        self.mode = display.MODE_EVENTS
        self.held: dict = {}
        #: What the player was doing at the instant the watcher was started,
        #: so a test can prove nothing waits in front of the first video.
        self.playing_when_started = None
        self.player = None

    # -- the surface the app uses ------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    def start(self) -> None:
        self.starts += 1
        if self.player is not None:
            self.playing_when_started = self.player.current

    def stop(self) -> None:
        self.stops += 1
        self.mode = display.MODE_STOPPED

    def should_be_awake(self) -> bool:
        return bool(self.held) or self._state != display.ABSENT

    def hold_awake(self, token: str, seconds=None) -> float:
        self.held[token] = seconds
        return 0.0

    def release_hold(self, token: str) -> None:
        self.held.pop(token, None)

    def snapshot(self) -> display.DisplaySnapshot:
        return display.DisplaySnapshot(
            state=self._state,
            awake=self.should_be_awake(),
            mode=self.mode,
            mode_detail="a stub, so nothing was opened",
            detail=f"the stub says {self._state}",
            connectors=(),
            holds=tuple(sorted(self.held)),
            changed_at=0.0,
        )

    # -- what a test says ---------------------------------------------------
    def see(self, state: str) -> None:
        """The television came or went, as far as the watcher is concerned."""
        self._state = state
        if self.on_change is not None:
            self.on_change(self.snapshot())


class FakeWall:
    """A wall clock a test can push forward by hours."""

    def __init__(self, hour: int = 20) -> None:
        self.now = at_local(hour, 0)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_app(tmp_path, *, watcher=None, cec=None, episodes=4, factory=None, **overrides):
    """A one-channel television with the display-sleep feature wired up."""
    make_show(tmp_path, "adultswim", episodes)
    data = {
        "shuffle_seed": 7,
        "boot_splash": False,
        "start_channel": 2,
        "start_offset": 0,
        "power_off_command": [],
        # Asked for explicitly, because it is off by default until the
        # dashboard half of the feature ships. These are its tests, so they
        # are the boxes that have asked for it.
        "display_sleep": {"enabled": True},
        "channels": [
            {"number": 2, "name": "Adult Swim", "path": str(tmp_path / "adultswim")},
        ],
    }
    sleep_overrides = overrides.pop("display_sleep", None)
    data.update(overrides)
    if sleep_overrides is not None:
        # Merged, not replaced, so a test tuning one knob does not silently
        # switch the whole feature back off underneath itself.
        data["display_sleep"] = {"enabled": True, **sleep_overrides}
    config = config_from_dict(data)
    clock = FakeClock()
    wall = FakeWall()
    player = MockPlayer()
    watcher = StubWatcher() if watcher is None else watcher
    if isinstance(watcher, StubWatcher):
        watcher.player = player
    if factory is None and watcher is not None:
        def factory(_config, _watcher=watcher):
            return _watcher
    app = TVApp(
        config,
        player,
        InputManager([]),
        clock=clock,
        wall_clock=wall,
        display_factory=factory,
    )
    if cec is not None:
        app._cec_power = lambda: cec()  # noqa: SLF001 - the box has no adapter here
    return app, player, clock, wall, watcher


@pytest.fixture(autouse=True)
def a_status_file_of_our_own(tmp_path, monkeypatch):
    """Keep the snapshot these tests write out of the real runtime directory."""
    monkeypatch.setenv("RETROBOX_STATUS_PATH", str(tmp_path / "status.json"))


@pytest.fixture(autouse=True)
def episodes_of_a_known_length(monkeypatch):
    """Half an hour each, without ffprobe and without real media.

    The broadcast schedule is arithmetic on durations, so the durations have to
    be something a test can do arithmetic with too.
    """
    import retrobox.channel as channel_mod

    monkeypatch.setattr(channel_mod, "probe_duration", lambda path: 1800.0)


def go_to_sleep(app, watcher, clock, *, seconds: float = 60.0):
    """Take the television away and let the box notice."""
    watcher.see(display.ABSENT)
    clock.advance(seconds)
    app.step()


# ==========================================================================
# The part that matters most: coming back in the right place
# ==========================================================================
def test_a_broadcast_channel_comes_back_where_the_broadcast_got_to_not_where_it_paused(
    tmp_path,
):
    app, player, clock, wall, watcher = build_app(tmp_path, tune_in="broadcast")
    app.start()
    was_playing = player.current

    go_to_sleep(app, watcher, clock)
    assert app.display_asleep

    # Three hours and ten minutes of nobody watching. The station kept
    # broadcasting: four half-hour episodes is a two-hour loop, so the running
    # order has moved on two whole episodes and is ten minutes into the second
    # of them.
    wall.advance(3 * 3600 + 600)
    clock.advance(3 * 3600 + 600)
    watcher.see(display.PRESENT)
    app.step()

    assert not app.display_asleep
    assert not player.paused
    path, start = player.played[-1]
    assert start == pytest.approx(600.0)
    assert path != was_playing
    # And it is exactly what that channel is airing at this moment.
    assert path == app.lineup.current.peek_now(wall())


def test_a_broadcast_channel_is_retuned_rather_than_unpaused(tmp_path):
    """Unpausing would resume three hours behind, which is the whole point."""
    app, player, clock, wall, watcher = build_app(tmp_path, tune_in="broadcast")
    app.start()
    plays_before = len(player.played)

    go_to_sleep(app, watcher, clock)
    wall.advance(3 * 3600)
    clock.advance(3 * 3600)
    watcher.see(display.PRESENT)
    app.step()

    assert len(player.played) == plays_before + 1


# ==========================================================================
# Pausing, not stopping
# ==========================================================================
def test_going_quiet_pauses_the_player_and_never_tears_it_down(tmp_path):
    app, player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    stops_before = player.stops
    playing = player.current

    go_to_sleep(app, watcher, clock)

    assert player.paused is True
    assert player.stops == stops_before   # mpv is alive and still holds the file
    assert player.closed is False
    assert player.current == playing


def test_waking_a_non_broadcast_channel_carries_on_from_where_it_paused(tmp_path):
    app, player, clock, wall, watcher = build_app(
        tmp_path, tune_in="resume", display_sleep={"non_broadcast": "resume"}
    )
    app.start()
    player.time_pos = 300.0
    playing = player.current
    plays_before = len(player.played)

    go_to_sleep(app, watcher, clock)
    wall.advance(3600)
    clock.advance(3600)
    watcher.see(display.PRESENT)
    app.step()

    assert player.paused is False
    assert player.current == playing
    assert len(player.played) == plays_before   # nothing was reloaded


def test_a_non_broadcast_channel_can_be_told_to_advance_by_the_time_it_slept(tmp_path):
    app, player, clock, wall, watcher = build_app(
        tmp_path, tune_in="random", display_sleep={"non_broadcast": "advance"}
    )
    app.start()
    player.time_pos = 120.0
    playing = player.current

    go_to_sleep(app, watcher, clock)
    wall.advance(600.0)
    clock.advance(600.0)
    watcher.see(display.PRESENT)
    app.step()

    assert player.current == playing
    assert player.played[-1] == (playing, pytest.approx(720.0))


def test_a_channel_told_to_advance_rolls_into_the_next_episode_when_it_runs_out(
    tmp_path, monkeypatch,
):
    app, player, clock, wall, watcher = build_app(
        tmp_path, tune_in="random", display_sleep={"non_broadcast": "advance"}
    )
    # The episode is half an hour long and the box slept for two hours.
    monkeypatch.setattr(
        "retrobox.app.TVApp._known_duration", lambda self: 1800.0
    )
    app.start()
    player.time_pos = 120.0
    playing = player.current

    go_to_sleep(app, watcher, clock)
    wall.advance(7200.0)
    clock.advance(7200.0)
    watcher.see(display.PRESENT)
    app.step()

    assert player.current != playing
    assert player.played[-1][1] == pytest.approx(0.0)


# ==========================================================================
# Never letting this break the box
# ==========================================================================
def test_a_box_that_boots_with_no_display_at_all_still_starts_and_plays_first(tmp_path):
    """Asleep is a valid state to end up in. It is never a state to start in."""
    watcher = StubWatcher(display.ABSENT)
    app, player, clock, _wall, _w = build_app(tmp_path, watcher=watcher)

    app.start()

    # Video first: the channel was tuned and playing before anything looked at
    # the display, and the watcher was only started once it was.
    assert player.current is not None
    assert watcher.playing_when_started is not None
    assert not app.display_asleep


def test_a_box_that_boots_with_no_display_goes_quiet_on_its_own_afterwards(tmp_path):
    watcher = StubWatcher(display.ABSENT)
    app, player, clock, _wall, _w = build_app(tmp_path, watcher=watcher)
    app.start()

    clock.advance(60.0)
    app.step()

    assert app.display_asleep
    assert player.paused is True
    # Still manageable: the dashboard's snapshot is still being written.
    assert app.build_status()["display"]["sleeping"] is True


def test_display_detection_that_cannot_be_started_disables_the_feature_and_says_so_once(
    tmp_path, caplog,
):
    def refuses(_config):
        raise OSError("no /sys on this machine")

    app, player, clock, _wall, _w = build_app(tmp_path)
    app._display_factory = refuses  # noqa: SLF001

    with caplog.at_level(logging.WARNING):
        app.start()
        for _ in range(5):
            clock.advance(60.0)
            app.step()

    assert not app.display_asleep
    assert player.paused is False
    complaints = [r for r in caplog.records if "display" in r.getMessage().lower()]
    assert len(complaints) == 1


def test_a_box_that_ends_up_asleep_with_a_display_connected_wakes_itself(
    tmp_path, caplog,
):
    """Stuck asleep in front of a working television is the worst outcome
    there is, so it is checked for rather than reasoned about."""
    app, player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    go_to_sleep(app, watcher, clock)

    # The state machine is now wrong: a television is plainly connected, but
    # something is still insisting the box may sleep.
    watcher._state = display.PRESENT  # noqa: SLF001
    watcher.should_be_awake = lambda: False
    with caplog.at_level(logging.ERROR):
        app.step()

    assert not app.display_asleep
    assert player.paused is False
    assert any("connected" in r.getMessage() for r in caplog.records)


def test_nothing_goes_quiet_while_the_box_is_in_standby(tmp_path):
    app, player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    app.handle_event(InputEvent(Action.POWER))
    assert app.standby
    plays_before = len(player.played)

    go_to_sleep(app, watcher, clock)
    watcher.see(display.PRESENT)
    app.step()

    assert not app.display_asleep
    assert len(player.played) == plays_before   # standby was left exactly alone


def test_nothing_goes_quiet_while_the_menu_is_open(tmp_path):
    """The menu already pauses the picture; there is nothing left to save."""
    app, player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    app.open_menu()

    go_to_sleep(app, watcher, clock)

    assert not app.display_asleep


def test_an_episode_that_ends_while_the_box_is_quiet_does_not_start_the_next_one(
    tmp_path,
):
    """Rolling the next episode would start decoding for nobody, which is the
    whole thing that was just stopped."""
    app, player, clock, _wall, watcher = build_app(tmp_path, tune_in="resume")
    app.start()
    go_to_sleep(app, watcher, clock)
    plays_before = len(player.played)

    player.finish_current()
    app.step()

    assert len(player.played) == plays_before
    assert app.display_asleep


def test_an_episode_that_ended_while_the_box_slept_is_picked_up_on_the_way_back(
    tmp_path,
):
    app, player, clock, _wall, watcher = build_app(tmp_path, tune_in="resume")
    app.start()
    go_to_sleep(app, watcher, clock)
    player.finish_current()
    app.step()

    watcher.see(display.PRESENT)
    app.step()

    assert not app.display_asleep
    assert player.current is not None      # not left on a finished file


def test_a_daypart_opening_in_an_empty_room_does_not_start_the_box_up(tmp_path):
    make_show(tmp_path, "afterdark", 3)
    app, player, clock, wall, watcher = build_app(
        tmp_path,
        tune_in="resume",
        channels=[{
            "number": 2,
            "name": "Talk",
            "path": str(tmp_path / "adultswim"),
            "dayparts": [{
                "from": "22:00", "to": "02:00", "name": "AFTER DARK",
                "path": str(tmp_path / "afterdark"),
            }],
        }],
    )
    wall.now = at_local(21, 59)
    app.start()
    go_to_sleep(app, watcher, clock)
    plays_before = len(player.played)

    # Ten o'clock arrives and the channel becomes something else entirely.
    wall.now = at_local(23, 0)
    app.step()

    assert len(player.played) == plays_before
    assert app.display_asleep

    # And it is picked up the moment somebody is watching again.
    watcher.see(display.PRESENT)
    app.step()
    assert len(player.played) > plays_before


# ==========================================================================
# The switch
# ==========================================================================
def test_switching_the_feature_off_means_nothing_is_watched_at_all(tmp_path):
    built = []

    def factory(config):
        built.append(config)
        return StubWatcher(display.ABSENT)

    app, player, clock, _wall, _w = build_app(
        tmp_path, display_sleep={"enabled": False},
    )
    app._display_factory = factory  # noqa: SLF001

    app.start()
    for _ in range(3):
        clock.advance(120.0)
        app.step()

    assert built == []                     # nothing was even constructed
    assert not app.display_asleep
    assert player.paused is False
    assert app.build_status()["display"]["enabled"] is False


def test_switching_the_feature_off_while_the_box_is_asleep_wakes_it(tmp_path):
    app, player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    go_to_sleep(app, watcher, clock)

    import dataclasses

    from retrobox.config import DisplaySleepConfig

    app.config = dataclasses.replace(
        app.config, display_sleep=DisplaySleepConfig(enabled=False)
    )
    app.apply_display_sleep_setting()

    assert not app.display_asleep
    assert player.paused is False
    assert watcher.stops == 1


# ==========================================================================
# The seam for anything else that is watching
# ==========================================================================
def test_something_else_watching_holds_the_box_awake(tmp_path):
    app, player, clock, _wall, watcher = build_app(tmp_path)
    app.start()

    app.hold_awake("a-fake-viewer", seconds=300)
    go_to_sleep(app, watcher, clock)

    assert not app.display_asleep
    assert player.paused is False


def test_the_box_goes_quiet_once_the_last_watcher_lets_go(tmp_path):
    app, player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    app.hold_awake("a-fake-viewer", seconds=300)
    go_to_sleep(app, watcher, clock)

    app.release_hold("a-fake-viewer")
    clock.advance(1.0)
    app.step()

    assert app.display_asleep


def test_a_watcher_arriving_while_the_box_sleeps_wakes_it(tmp_path):
    app, player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    go_to_sleep(app, watcher, clock)

    app.hold_awake("a-fake-viewer", seconds=300)
    clock.advance(1.0)
    app.step()

    assert not app.display_asleep
    assert player.paused is False


def test_holding_the_box_awake_is_harmless_when_the_feature_is_off(tmp_path):
    app, _player, _clock, _wall, _w = build_app(
        tmp_path, display_sleep={"enabled": False},
    )
    app.start()

    assert app.hold_awake("a-fake-viewer") is False
    app.release_hold("a-fake-viewer")


# ==========================================================================
# The television's own word, when there is one
# ==========================================================================
def test_a_television_that_says_it_is_in_standby_puts_the_box_to_sleep(tmp_path):
    """The case hotplug cannot see: the cable is live, the screen is not."""
    power = CecPower(CEC_STANDBY, 0.0, "the television said standby")
    app, player, clock, _wall, watcher = build_app(tmp_path, cec=lambda: power)
    app.start()
    watcher.see(display.PRESENT)

    clock.advance(60.0)
    app.step()
    assert app.display_asleep

    # And it stays asleep. A live cable is exactly what a set in standby
    # leaves behind, so the guard against being stuck asleep must not read it
    # as a television that is watching.
    clock.advance(60.0)
    app.step()
    assert app.display_asleep
    assert player.paused is True


def test_a_television_that_says_it_is_on_outranks_a_disconnected_cable(tmp_path):
    power = CecPower(CEC_ON, 0.0, "the television reports power on")
    app, player, clock, _wall, watcher = build_app(tmp_path, cec=lambda: power)
    app.start()

    go_to_sleep(app, watcher, clock)

    assert not app.display_asleep


def test_a_box_with_no_cec_adapter_is_decided_by_the_cable_alone(tmp_path):
    power = CecPower(CEC_UNKNOWN, None, "listening; the television has not said")
    app, player, clock, _wall, watcher = build_app(tmp_path, cec=lambda: power)
    app.start()

    go_to_sleep(app, watcher, clock)

    assert app.display_asleep


def test_a_television_saying_standby_never_outranks_something_still_watching(tmp_path):
    power = CecPower(CEC_STANDBY, 0.0, "the television said standby")
    app, player, clock, _wall, watcher = build_app(tmp_path, cec=lambda: power)
    app.start()
    app.hold_awake("a-fake-viewer", seconds=300)

    clock.advance(60.0)
    app.step()

    assert not app.display_asleep


# ==========================================================================
# Waking it by hand, for when detection got it wrong
# ==========================================================================
def test_the_dashboard_can_wake_a_sleeping_box_by_hand(tmp_path):
    app, player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    go_to_sleep(app, watcher, clock)

    app.handle_event(InputEvent(Action.WAKE))

    assert not app.display_asleep
    assert player.paused is False


def test_a_box_woken_by_hand_does_not_go_straight_back_to_sleep(tmp_path):
    app, _player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    go_to_sleep(app, watcher, clock)

    app.handle_event(InputEvent(Action.WAKE))
    clock.advance(60.0)
    app.step()

    assert not app.display_asleep


def test_the_wake_command_is_something_the_dashboard_can_actually_send():
    from retrobox.input.web import parse_command

    assert parse_command("wake") == [InputEvent(Action.WAKE)]


def test_pressing_a_button_on_a_sleeping_box_wakes_it_rather_than_doing_nothing(
    tmp_path,
):
    app, player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    go_to_sleep(app, watcher, clock)

    app.handle_event(InputEvent(Action.VOLUME_UP))

    assert not app.display_asleep
    assert player.paused is False


# ==========================================================================
# What the dashboard is told
# ==========================================================================
def test_a_sleeping_box_reads_as_asleep_and_not_as_a_fault(tmp_path):
    app, _player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    go_to_sleep(app, watcher, clock)

    block = app.build_status()["display"]

    assert block["sleeping"] is True
    assert block["state"] == display.ABSENT
    assert block["summary"] == (
        "Asleep - nothing is connected to the video output, so playback is paused."
    )
    assert not any(
        word in block["summary"].lower() for word in ("error", "fail", "problem")
    )
    assert block["can_wake"] is True


def test_a_playing_box_says_so_plainly(tmp_path):
    app, _player, _clock, _wall, _watcher = build_app(tmp_path)
    app.start()

    block = app.build_status()["display"]

    assert block["sleeping"] is False
    assert block["summary"] == "Playing."


def test_the_snapshot_says_why_the_box_is_quiet_when_the_television_said_so(tmp_path):
    power = CecPower(CEC_STANDBY, 0.0, "the television said standby")
    app, _player, clock, _wall, watcher = build_app(tmp_path, cec=lambda: power)
    app.start()
    watcher.see(display.PRESENT)
    clock.advance(60.0)
    app.step()

    block = app.build_status()["display"]

    assert block["summary"] == (
        "Asleep - the television says it is in standby, so playback is paused."
    )
    assert block["cec"]["state"] == CEC_STANDBY


def test_the_snapshot_carries_the_caveat_so_the_dashboard_never_overclaims(tmp_path):
    app, _player, _clock, _wall, _watcher = build_app(tmp_path)
    app.start()

    block = app.build_status()["display"]

    assert block["caveat"] == display.CAVEAT
    assert block["mode"] in (display.MODE_EVENTS, display.MODE_POLL)


def test_a_box_held_awake_by_something_else_says_who(tmp_path):
    app, _player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    app.hold_awake("live-viewer", seconds=300)
    go_to_sleep(app, watcher, clock)

    block = app.build_status()["display"]

    assert block["holds"] == ["live-viewer"]
    assert block["summary"] == "Playing - something else is watching, so the box is staying awake."


# ==========================================================================
# Lifecycle
# ==========================================================================
def test_the_watcher_is_only_started_once_the_television_is_already_playing(tmp_path):
    app, player, _clock, _wall, watcher = build_app(tmp_path)

    app.start()

    assert watcher.starts == 1
    assert watcher.playing_when_started is not None


def test_the_boot_splash_still_goes_first(tmp_path):
    splash = tmp_path / "assets"
    splash.mkdir()
    (splash / "boot_splash.mp4").write_bytes(b"\x00")
    app, player, _clock, _wall, watcher = build_app(
        tmp_path, boot_splash=str(splash / "boot_splash.mp4"),
    )

    app.start()

    assert player.current == splash / "boot_splash.mp4"
    assert watcher.playing_when_started == splash / "boot_splash.mp4"


def test_a_splash_still_running_is_never_paused_underneath_itself(tmp_path):
    splash = tmp_path / "assets"
    splash.mkdir()
    (splash / "boot_splash.mp4").write_bytes(b"\x00")
    app, player, clock, _wall, watcher = build_app(
        tmp_path, boot_splash=str(splash / "boot_splash.mp4"),
    )
    app.start()

    go_to_sleep(app, watcher, clock, seconds=1.0)

    assert player.paused is False
    assert not app.display_asleep


def test_shutting_down_stops_watching_the_display(tmp_path):
    app, _player, _clock, _wall, watcher = build_app(tmp_path)
    app.start()

    app.shutdown()

    assert watcher.stops == 1


# ==========================================================================
# The real watcher, against a tree of files and an event source that only waits
# ==========================================================================
def drm_root(tmp_path, **connectors) -> Path:
    root = tmp_path / "drm"
    root.mkdir(exist_ok=True)
    for name, status in connectors.items():
        node = root / name.replace("_", "-")
        node.mkdir(exist_ok=True)
        (node / "status").write_text(status + "\n")
    return root


def test_the_real_watcher_puts_a_real_app_to_sleep_and_brings_it_back(tmp_path):
    root = drm_root(tmp_path, card0_HDMI_A_1="connected")
    clock = FakeClock()

    def factory(config):
        return make_display_watcher(
            config,
            root=root,
            # PollSource waits on an Event, so the background thread sits
            # still: the suite opens no sockets and reads no real sysfs.
            source=display.PollSource("a test source"),
            clock=clock,
            poll_interval=3600.0,
        )

    make_show(tmp_path, "adultswim", 4)
    config = config_from_dict({
        "shuffle_seed": 7,
        "boot_splash": False,
        "start_channel": 2,
        "start_offset": 0,
        "power_off_command": [],
        "display_sleep": {"enabled": True, "sleep_after_seconds": 5},
        "channels": [
            {"number": 2, "name": "Adult Swim", "path": str(tmp_path / "adultswim")},
        ],
    })
    player = MockPlayer()
    app = TVApp(
        config, player, InputManager([]),
        clock=clock, wall_clock=FakeWall(), display_factory=factory,
    )
    app.start()
    try:
        assert not app.display_asleep

        # Past the watcher's own anti-thrash hold, which refuses to let a box
        # that has only just woken go quiet again.
        clock.advance(120.0)
        (root / "card0-HDMI-A-1" / "status").write_text("disconnected\n")
        app._display_watcher.refresh()  # noqa: SLF001 - stands in for a uevent
        clock.advance(4.0)
        app.step()
        assert not app.display_asleep, "the configured wait had not elapsed"

        clock.advance(2.0)
        app._display_watcher.refresh()  # noqa: SLF001
        app.step()
        assert app.display_asleep
        assert player.paused is True

        (root / "card0-HDMI-A-1" / "status").write_text("connected\n")
        app._display_watcher.refresh()  # noqa: SLF001
        app.step()
        assert not app.display_asleep
        assert player.paused is False
    finally:
        app.shutdown()


# ==========================================================================
# Giving up on detection, which must never leave the picture paused
# ==========================================================================
class BreakingFactory:
    """Builds one working watcher and then cannot build another.

    The failure it stands for is ordinary rather than exotic: this is a
    two-core box, and `threading.Thread.start()` on a loaded one answers
    "RuntimeError: can't start new thread". Every saved setting rebuilds the
    watcher, so every saved setting is a chance to meet it.
    """

    def __init__(self, watcher, error=None):
        self._watcher = watcher
        self._error = error or RuntimeError("can't start new thread")
        self.calls = 0

    def __call__(self, _config):
        self.calls += 1
        if self.calls == 1:
            return self._watcher
        raise self._error


class NoWatcherFactory(BreakingFactory):
    """The other way the rebuild fails: it returns nothing at all."""

    def __call__(self, _config):
        self.calls += 1
        return self._watcher if self.calls == 1 else None


def save_a_setting(app):
    """What the owner does on the dashboard, as the app sees it.

    Saving ANY setting ends in :meth:`reload_config`, which calls this. The
    setting changed here is a display-sleep one purely so a new watcher is
    actually needed - which is the case the rebuild can fail in.
    """
    app.config = replace(
        app.config,
        display_sleep=replace(app.config.display_sleep, sleep_after_seconds=20.0),
    )
    app.apply_display_sleep_setting()


def test_giving_up_on_display_detection_wakes_a_box_that_was_already_asleep(tmp_path):
    """The log line promises the box will stay awake. Make it true.

    The box goes quiet, the owner saves a setting, the watcher rebuild fails.
    Giving up used to drop the watcher and say so WHILE THE PICTURE WAS STILL
    PAUSED, and with the watcher gone there was nothing left that ever
    un-paused it.
    """
    watcher = StubWatcher()
    app, player, clock, _wall, _w = build_app(
        tmp_path, watcher=watcher, factory=BreakingFactory(watcher),
    )
    app.start()
    go_to_sleep(app, watcher, clock)
    assert app.display_asleep

    save_a_setting(app)

    assert not app.display_asleep
    assert player.paused is False


def test_a_rebuild_that_returns_no_watcher_at_all_also_wakes_the_box(tmp_path):
    watcher = StubWatcher()
    app, player, clock, _wall, _w = build_app(
        tmp_path, watcher=watcher, factory=NoWatcherFactory(watcher),
    )
    app.start()
    go_to_sleep(app, watcher, clock)

    save_a_setting(app)

    assert not app.display_asleep
    assert player.paused is False


def test_a_box_that_gave_up_on_detection_stays_awake_for_good(tmp_path):
    """Ten minutes of ticks, because "stuck" is a thing you prove over time."""
    watcher = StubWatcher()
    app, player, clock, _wall, _w = build_app(
        tmp_path, watcher=watcher, factory=BreakingFactory(watcher),
    )
    app.start()
    go_to_sleep(app, watcher, clock)
    save_a_setting(app)

    for _ in range(50):
        clock.advance(12.0)
        app.step()

    assert not app.display_asleep
    assert player.paused is False


def test_a_paused_picture_always_offers_the_wake_button(tmp_path):
    """Being unable to offer the fix exactly when the fix is needed.

    The dashboard is told whether to show Wake. Tying that to the watcher
    existing meant the one moment the button matters - paused, with detection
    gone - is the one moment it was hidden, leaving a physical remote as the
    only way back.
    """
    app, player, clock, _wall, watcher = build_app(tmp_path)
    app.start()
    go_to_sleep(app, watcher, clock)
    # However the watcher came to be gone, the picture is still paused.
    app._display_watcher = None  # noqa: SLF001

    block = app.build_status()["display"]

    assert block["sleeping"] is True
    assert block["can_wake"] is True
    # And the button behind it really does work without a watcher.
    app.handle_event(InputEvent(Action.WAKE))
    assert not app.display_asleep
    assert player.paused is False


def test_a_box_that_is_not_watching_and_is_not_paused_offers_nothing_to_wake(tmp_path):
    """Wake is for a paused picture. Offering it otherwise is just noise."""
    app, _player, _clock, _wall, _w = build_app(
        tmp_path, display_sleep={"enabled": False},
    )
    app.start()

    block = app.build_status()["display"]

    assert block["sleeping"] is False
    assert block["can_wake"] is False


# ==========================================================================
# One answer to "should this box be awake?", not two
# ==========================================================================
def test_a_hold_survives_the_watcher_being_rebuilt_when_a_setting_is_saved(tmp_path):
    """A hold is a promise, and rebuilding the watcher used to forget it.

    Somebody presses Wake, which takes a half-hour hold. Somebody saves a
    setting. The watcher is thrown away and a new one built - and the new one
    has never heard of the hold, so the next tick puts the box straight back
    to sleep in front of the person who just asked it not to.
    """
    watchers = []

    def factory(_config):
        watcher = StubWatcher()
        watchers.append(watcher)
        return watcher

    app, player, clock, _wall, _w = build_app(tmp_path, watcher=None, factory=factory)
    app.start()
    app.hold_awake("a-fake-viewer", seconds=300)

    save_a_setting(app)
    assert len(watchers) == 2, "the watcher was not rebuilt, so this proves nothing"

    watchers[-1].see(display.ABSENT)
    clock.advance(60.0)
    app.step()

    assert not app.display_asleep
    assert player.paused is False


def test_a_hold_keeps_the_box_awake_even_when_the_watcher_cannot_describe_itself(
    tmp_path,
):
    """Whose books the holds are kept in decides whether they can be lost.

    They were read back out of the watcher's snapshot, so a watcher that could
    not produce one dropped every live hold on the floor and the box went
    quiet. The app takes the holds, so the app is where they live.
    """
    class MuteWatcher(StubWatcher):
        def snapshot(self):
            raise RuntimeError("this watcher cannot describe itself")

    power = CecPower(CEC_STANDBY, 0.0, "the television said standby")
    watcher = MuteWatcher(display.PRESENT)
    app, player, clock, _wall, _w = build_app(
        tmp_path, watcher=watcher, cec=lambda: power,
    )
    app.start()
    app.hold_awake("a-fake-viewer", seconds=300)

    clock.advance(60.0)
    app.step()

    assert not app.display_asleep
    assert player.paused is False


def test_the_app_and_the_watcher_never_disagree_about_staying_awake(tmp_path):
    """Two implementations of one question drift; this fails the day they do.

    With no television speaking over CEC there is nothing to arbitrate, so the
    app's answer must be exactly the watcher's - for every state it can be in,
    held awake and not.
    """
    silent = CecPower(CEC_UNKNOWN, None, "this box has no CEC adapter")
    app, _player, _clock, _wall, watcher = build_app(tmp_path, cec=lambda: silent)
    app.start()

    for state in (display.PRESENT, display.ABSENT, display.UNKNOWN):
        for held in (False, True):
            watcher._state = state  # noqa: SLF001
            app.release_hold("a-fake-viewer")
            if held:
                app.hold_awake("a-fake-viewer", seconds=300)
            assert app._wants_awake(watcher, silent) == watcher.should_be_awake(), (  # noqa: SLF001
                f"state={state} held={held}"
            )
    app.release_hold("a-fake-viewer")


def test_a_hold_outranks_a_television_that_says_it_is_in_standby(tmp_path):
    """The precedence, stated once: holds beat the set, the set beats the cable."""
    power = CecPower(CEC_STANDBY, 0.0, "the television said standby")
    app, _player, _clock, _wall, watcher = build_app(tmp_path, cec=lambda: power)
    app.start()

    assert app._wants_awake(watcher, power) is False  # noqa: SLF001
    app.hold_awake("a-fake-viewer", seconds=300)
    assert app._wants_awake(watcher, power) is True  # noqa: SLF001
