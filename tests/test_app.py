import pytest

from retrobox.actions import Action, InputEvent
from retrobox.app import TVApp
from retrobox.config import config_from_dict
from retrobox.input.manager import InputManager
from retrobox.player import END_EOF, MockPlayer
from tests.helpers import FakeClock, FakeWallClock, make_show


def build_app(tmp_path, *, assets_dir=None, wall_clock=None, channels=None, **overrides):
    for name in ("adultswim", "mtv", "latenight"):
        make_show(tmp_path, name, 4)
    data = {
        "shuffle_seed": 7,
        "start_channel": 2,
        "start_offset": 0,  # keep test assertions on start=0 unless overridden
        "power_off_command": [],  # no-op in tests (never actually shut down)
        "channels": channels
        or [
            {"number": 2, "name": "Adult Swim", "path": str(tmp_path / "adultswim")},
            {"number": 3, "name": "MTV Classic", "path": str(tmp_path / "mtv")},
            {"number": 4, "name": "Late Night", "path": str(tmp_path / "latenight")},
        ],
    }
    data.update(overrides)
    config = config_from_dict(data)
    clock = FakeClock()
    player = MockPlayer()
    app = TVApp(
        config,
        player,
        InputManager([]),
        clock=clock,
        wall_clock=wall_clock or FakeWallClock(23),
        assets_dir=assets_dir,
    )
    return app, player, clock


def send(app, action, value=None):
    app.handle_event(InputEvent(action, value))


def test_start_tunes_to_start_channel_and_plays(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    assert app.lineup.current.number == 2
    assert player.current is not None  # an episode is playing
    assert player.volume == 70
    assert player.overlays.get(1) and "Adult Swim" in player.overlays[1]


def test_channel_up_down_wraps(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 3
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 4
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 2  # wrapped
    send(app, Action.CHANNEL_DOWN)
    assert app.lineup.current.number == 4  # wrapped back


def test_volume_controls(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.VOLUME_UP)
    assert app.volume == 75 and player.volume == 75
    send(app, Action.VOLUME_DOWN)
    assert app.volume == 70
    # volume overlay was drawn
    assert "Volume" in player.overlays[2]


def test_volume_clamps(tmp_path):
    app, player, _ = build_app(tmp_path, initial_volume=98, volume_step=5)
    app.start()
    send(app, Action.VOLUME_UP)
    assert app.volume == 100
    for _ in range(30):
        send(app, Action.VOLUME_DOWN)
    assert app.volume == 0


def test_volume_down_at_zero_powers_off(tmp_path):
    app, player, _ = build_app(tmp_path, initial_volume=10, volume_step=5)
    app.start()
    send(app, Action.VOLUME_DOWN)   # 10 -> 5
    send(app, Action.VOLUME_DOWN)   # 5 -> 0
    assert app.volume == 0 and not app.powered_off
    send(app, Action.VOLUME_DOWN)   # one more at 0 -> power off
    assert app.powered_off is True
    assert app._running is False
    assert player.current is None   # playback stopped


def test_power_off_disabled(tmp_path):
    app, player, _ = build_app(
        tmp_path, initial_volume=0, power_off_on_min_volume=False
    )
    app.start()
    send(app, Action.VOLUME_DOWN)   # at 0, but feature disabled
    assert app.powered_off is False


def test_mute_toggle_and_unmute_on_volume(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.MUTE)
    assert app.muted and player.muted
    send(app, Action.VOLUME_UP)  # changing volume unmutes
    assert not app.muted and not player.muted


def test_direct_channel_entry_with_enter(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.DIGIT, 4)
    assert app.lineup.current.number == 2  # not committed yet
    send(app, Action.ENTER)
    assert app.lineup.current.number == 4


def test_direct_channel_entry_times_out(tmp_path):
    app, player, clock = build_app(tmp_path)
    app.start()
    send(app, Action.DIGIT, 3)
    assert app.lineup.current.number == 2
    clock.advance(2.1)  # past the entry timeout
    app.step()
    assert app.lineup.current.number == 3


def test_invalid_channel_entry_shows_message(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    assert app.select_channel_number(99) is False
    assert "NO CHANNEL" in player.overlays.get(4, "")
    assert app.lineup.current.number == 2  # unchanged


def test_last_channel_jump(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.CHANNEL_UP)  # now on 3, last=2
    assert app.lineup.current.number == 3
    send(app, Action.LAST_CHANNEL)
    assert app.lineup.current.number == 2
    send(app, Action.LAST_CHANNEL)  # bounces back to 3
    assert app.lineup.current.number == 3


def test_episode_advances_on_end(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    first = player.current
    player.finish_current(END_EOF)  # simulate the episode ending
    app._drain_playback_events()
    assert player.current is not None
    assert player.current != first  # rolled into the next shuffled episode


def test_standby_blanks_and_ignores_input(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.POWER)
    assert app.standby
    assert player.current is None  # screen blanked
    assert 3 in player.overlays  # standby overlay
    # input is ignored while in standby
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 2
    # power again wakes it up and resumes playback
    send(app, Action.POWER)
    assert not app.standby
    assert player.current is not None


def test_quit_stops_running(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    app._running = True
    send(app, Action.QUIT)
    assert app._running is False


def test_glitch_transition_then_episode(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "glitch.mp4").write_bytes(b"\x00")
    app, player, clock = build_app(tmp_path, assets_dir=assets, transition="glitch")
    app.start()
    send(app, Action.CHANNEL_UP)
    # A glitch->episode transition was issued (glitch clip + preloaded episode).
    assert player.transitions, "expected a transition on channel change"
    clip, target, _start = player.transitions[-1]
    assert clip == assets / "glitch.mp4"
    assert player.current == target  # the episode is what plays


def test_transition_none_cuts_straight(tmp_path):
    # bridge_seconds=0 -> switch immediately, no transition clip, no preload
    app, player, _ = build_app(tmp_path, transition="none", bridge_seconds=0)
    app.start()
    first = player.current
    send(app, Action.CHANNEL_UP)
    assert not player.transitions
    assert player.preloaded is None
    assert player.current is not None and player.current != first


def test_channel_change_bridges_current_until_next_ready(tmp_path):
    # With bridge_seconds>0 and no transition, the current show keeps playing
    # while the next channel preloads, then cuts over after the window.
    app, player, clock = build_app(tmp_path, bridge_seconds=0.8)
    app.start()
    first = player.current
    send(app, Action.CHANNEL_UP)
    assert player.current == first          # old show still playing...
    assert player.preloaded is not None     # ...next channel preloading
    clock.advance(1.0)
    app.step()                              # bridge window elapsed -> switch
    assert player.preloaded is None
    assert player.current is not None and player.current != first


def test_advance_within_channel_has_no_transition(tmp_path):
    # An episode ending should roll straight into the next one (no glitch burst).
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "glitch.mp4").write_bytes(b"\x00")
    app, player, _ = build_app(tmp_path, assets_dir=assets, transition="glitch")
    app.start()
    before = len(player.transitions)
    player.finish_current(END_EOF)
    app._drain_playback_events()
    assert len(player.transitions) == before  # no new transition
    assert player.current is not None


def test_start_offset_applied(tmp_path):
    app, player, _ = build_app(tmp_path, start_offset=5)
    app.start()
    # The episode should begin 5 seconds in, not at the very beginning.
    assert player.played[-1][1] == 5.0


def test_start_offset_range_applied(tmp_path):
    app, player, _ = build_app(tmp_path, start_offset=[6, 10])
    app.start()
    assert 6.0 <= player.played[-1][1] <= 10.0


def test_empty_channel_shows_no_signal(tmp_path):
    (tmp_path / "deadair").mkdir()
    make_show(tmp_path, "mtv", 2)
    config = config_from_dict(
        {
            "channels": [
                {"number": 2, "name": "Dead Air", "path": str(tmp_path / "deadair")},
                {"number": 3, "name": "MTV Classic", "path": str(tmp_path / "mtv")},
            ]
        }
    )
    app = TVApp(config, MockPlayer(), InputManager([]), clock=FakeClock())
    app.start()  # starts on ch 2 which is empty
    assert "NO SIGNAL" in app.player.overlays.get(4, "")


def test_channel_banner_deferred_until_switch(tmp_path):
    app, player, clock = build_app(tmp_path, bridge_seconds=0.8)
    app.start()
    player.overlays.pop(1, None)          # clear the power-on banner
    send(app, Action.CHANNEL_UP)
    assert 1 not in player.overlays       # banner NOT shown during the bridge
    clock.advance(1.0)
    app.step()                            # cut-over happens here
    assert "CH 03" in player.overlays.get(1, "")  # banner appears at the switch


def test_resume_mode_restarts_where_left(tmp_path):
    # bridge_seconds=0 keeps this test focused on resume (immediate switches)
    app, player, _ = build_app(tmp_path, tune_in="resume", bridge_seconds=0)
    app.start()
    playing = player.current
    player.time_pos = 42.0
    send(app, Action.CHANNEL_UP)  # leave ch 2, remembering position 42
    send(app, Action.CHANNEL_DOWN)  # back to ch 2 -> resume at 42
    assert player.current == playing
    assert player.played[-1] == (playing, 42.0)


# ==========================================================================
# Sleep timer
# ==========================================================================
def test_sleep_timer_cycles_through_the_ladder_then_off(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    assert app.sleep_remaining() is None

    send(app, Action.SLEEP)
    assert app.sleep_remaining() == pytest.approx(30 * 60)
    assert "SLEEP" in player.overlays.get(4, "")

    send(app, Action.SLEEP)
    assert app.sleep_remaining() == pytest.approx(60 * 60)
    send(app, Action.SLEEP)
    assert app.sleep_remaining() == pytest.approx(90 * 60)

    send(app, Action.SLEEP)  # one past the end of the ladder turns it off
    assert app.sleep_remaining() is None
    assert "OFF" in player.overlays.get(4, "")


def test_sleep_timer_counts_down_with_the_clock(tmp_path):
    app, _player, clock = build_app(tmp_path)
    app.start()
    send(app, Action.SLEEP)
    clock.advance(10 * 60)
    assert app.sleep_remaining() == pytest.approx(20 * 60)


def test_sleep_timer_expires_into_standby(tmp_path):
    app, player, clock = build_app(tmp_path)
    app.start()
    send(app, Action.SLEEP)  # 30 minutes

    clock.advance(29 * 60)
    app.step()
    assert not app.standby, "must not fire early"

    clock.advance(2 * 60)
    app.step()
    assert app.standby
    assert player.current is None      # screen blanked
    assert app.sleep_remaining() is None  # timer cleared, not left armed


def test_sleep_timer_can_power_the_box_off(tmp_path):
    app, player, clock = build_app(tmp_path, sleep_timer=[1], sleep_action="off")
    app.start()
    send(app, Action.SLEEP)
    clock.advance(61)
    app.step()
    assert app.powered_off is True
    assert app._running is False


def test_sleep_timer_can_be_disabled(tmp_path):
    app, player, clock = build_app(tmp_path, sleep_timer=False)
    app.start()
    send(app, Action.SLEEP)
    assert app.sleep_remaining() is None
    assert "SLEEP TIMER OFF" in player.overlays.get(4, "")
    clock.advance(10 * 60 * 60)
    app.step()
    assert not app.standby


# ==========================================================================
# Station bumpers
# ==========================================================================
def test_bumper_plays_between_episodes(tmp_path):
    make_show(tmp_path, "bumps", 3)
    app, player, _ = build_app(tmp_path, bumpers=str(tmp_path / "bumps"))
    app.start()

    player.finish_current(END_EOF)
    app.step()

    assert player.transitions, "expected a bumper before the next episode"
    bumper, target, _start = player.transitions[-1]
    assert bumper.parent.name == "bumps"
    assert target.parent.name == "adultswim"   # rolled into the next episode
    assert player.current == target


def test_bumpers_do_not_repeat_back_to_back(tmp_path):
    make_show(tmp_path, "bumps", 3)
    app, player, _ = build_app(tmp_path, bumpers=str(tmp_path / "bumps"))
    app.start()
    for _ in range(6):
        player.finish_current(END_EOF)
        app.step()
    aired = [clip.name for clip, _target, _start in player.transitions]
    assert len(aired) == 6
    assert all(a != b for a, b in zip(aired, aired[1:]))


def test_no_bumper_when_none_configured(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    player.finish_current(END_EOF)
    app.step()
    assert player.transitions == []


def test_bumper_chance_zero_never_airs_one(tmp_path):
    make_show(tmp_path, "bumps", 3)
    app, player, _ = build_app(
        tmp_path, bumpers=str(tmp_path / "bumps"), bumper_chance=0
    )
    app.start()
    for _ in range(5):
        player.finish_current(END_EOF)
        app.step()
    assert player.transitions == []
    assert player.current is not None  # episodes still roll on


def test_missing_bumper_folder_is_survivable(tmp_path):
    app, player, _ = build_app(tmp_path, bumpers=str(tmp_path / "nope"))
    app.start()
    player.finish_current(END_EOF)
    app.step()
    assert player.transitions == []
    assert player.current is not None


# ==========================================================================
# On-screen channel guide
# ==========================================================================
def test_guide_toggles_on_and_off(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    assert 5 not in player.overlays

    send(app, Action.GUIDE)
    assert app.overlay.guide_visible
    assert "CH 02" in player.overlays[5]
    assert "Adult Swim" in player.overlays[5]

    send(app, Action.GUIDE)
    assert not app.overlay.guide_visible
    assert 5 not in player.overlays


def test_guide_lists_the_whole_lineup_and_marks_the_current_channel(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    entries = app.build_guide()
    assert [e.number for e in entries] == [2, 3, 4]
    assert [e.name for e in entries] == ["Adult Swim", "MTV Classic", "Late Night"]

    send(app, Action.GUIDE)
    ass = player.overlays[5]
    assert ">CH 02" in ass          # marker on the channel we're watching
    assert " CH 03" in ass          # others are not marked


def test_guide_shows_what_is_playing_on_the_current_channel(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    playing = player.current
    entries = {e.number: e for e in app.build_guide()}
    assert entries[2].now_playing == playing.stem.replace("_", " ")
    # Channels we have not tuned to just report how much they carry.
    assert entries[3].now_playing == "4 episodes"


def test_guide_expires_on_its_own(tmp_path):
    app, player, clock = build_app(tmp_path, guide_seconds=5)
    app.start()
    send(app, Action.GUIDE)
    assert app.overlay.guide_visible

    clock.advance(6)
    app.step()
    assert not app.overlay.guide_visible
    assert 5 not in player.overlays


def test_guide_arrows_browse_without_changing_channel(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.GUIDE)
    assert ">CH 02" in player.overlays[5]

    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 2, "browsing the guide must not retune"
    ass = player.overlays[5]
    assert ">CH 03" in ass      # the highlight moved...
    assert "*CH 02" in ass      # ...and CH 02 is flagged as still playing

    send(app, Action.CHANNEL_DOWN)
    assert ">CH 02" in player.overlays[5]


def test_guide_highlight_wraps_around(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.GUIDE)
    for _ in range(3):                       # 2 -> 3 -> 4 -> back to 2
        send(app, Action.CHANNEL_UP)
    assert ">CH 02" in player.overlays[5]


def test_guide_enter_tunes_to_the_highlighted_channel(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.GUIDE)
    send(app, Action.CHANNEL_UP)
    send(app, Action.CHANNEL_UP)             # highlight CH 04
    send(app, Action.ENTER)
    assert app.lineup.current.number == 4
    assert not app.overlay.guide_visible
    assert 5 not in player.overlays


def test_tuning_by_number_dismisses_the_guide(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.GUIDE)
    app.select_channel_number(3)
    assert app.lineup.current.number == 3
    assert not app.overlay.guide_visible
    assert 5 not in player.overlays


def test_guide_header_shows_the_sleep_timer(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.SLEEP)     # 30 minutes
    send(app, Action.GUIDE)
    assert "SLEEP 30m" in player.overlays[5]


def test_guide_reports_off_air_channels(tmp_path):
    make_show(tmp_path, "signoff", 2)
    app, player, _ = build_app(
        tmp_path,
        wall_clock=FakeWallClock(3),  # 03:00, inside the sign-off window
        channels=[
            {"number": 2, "name": "Adult Swim", "path": str(tmp_path / "adultswim")},
            {
                "number": 3,
                "name": "Sign Off",
                "path": str(tmp_path / "signoff"),
                "dayparts": [{"from": "02:00", "to": "06:00", "off_air": True}],
            },
        ],
    )
    app.start()
    entries = {e.number: e for e in app.build_guide()}
    assert entries[3].off_air is True

    send(app, Action.GUIDE)
    assert "OFF AIR" in player.overlays[5]


def test_guide_uses_the_daypart_name(tmp_path):
    app, player, _ = build_app(
        tmp_path,
        wall_clock=FakeWallClock(23),
        channels=[
            {
                "number": 2,
                "name": "Talk",
                "path": str(tmp_path / "adultswim"),
                "dayparts": [{"from": "22:00", "to": "04:00", "name": "AFTER DARK"}],
            },
        ],
    )
    app.start()
    assert app.build_guide()[0].name == "AFTER DARK"
    send(app, Action.GUIDE)
    assert "AFTER DARK" in player.overlays[5]


# ==========================================================================
# Sleep indicator
# ==========================================================================
def test_sleep_indicator_appears_and_counts_down(tmp_path):
    app, player, clock = build_app(tmp_path)
    app.start()
    app.step()
    assert 6 not in player.overlays          # nothing shown when no timer

    send(app, Action.SLEEP)                  # 30 minutes
    app.step()
    assert "SLEEP 30m" in player.overlays[6]

    clock.advance(10 * 60)
    app.step()
    assert "SLEEP 20m" in player.overlays[6]


def test_sleep_indicator_clears_when_the_timer_is_cancelled(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.SLEEP)
    app.step()
    assert 6 in player.overlays

    for _ in range(3):                       # cycle past the end of the ladder
        send(app, Action.SLEEP)
    app.step()
    assert 6 not in player.overlays


def test_sleep_indicator_returns_after_standby(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.SLEEP)
    app.step()
    assert 6 in player.overlays

    send(app, Action.POWER)                  # standby wipes every overlay
    app.step()
    assert 6 not in player.overlays

    send(app, Action.POWER)                  # ...and it comes back on wake
    app.step()
    assert "SLEEP" in player.overlays.get(6, "")


# ==========================================================================
# Daypart boundaries crossing under a running episode
# ==========================================================================
def _daypart_app(tmp_path, wall_clock, dayparts):
    make_show(tmp_path, "afterdark", 3)
    return build_app(
        tmp_path,
        wall_clock=wall_clock,
        channels=[
            {
                "number": 2,
                "name": "Talk",
                "path": str(tmp_path / "adultswim"),
                "dayparts": dayparts,
            },
            {"number": 3, "name": "MTV Classic", "path": str(tmp_path / "mtv")},
        ],
    )


def test_crossing_into_a_new_pool_retunes_immediately(tmp_path):
    wall = FakeWallClock(21, 59)
    app, player, _ = _daypart_app(
        tmp_path,
        wall,
        [{"from": "22:00", "to": "02:00", "name": "AFTER DARK",
          "path": str(tmp_path / "afterdark")}],
    )
    app.start()
    assert player.current.parent.name == "adultswim"

    wall.set(22, 0)          # the window opens under the running episode
    app.step()
    assert player.current.parent.name == "afterdark"
    assert app.lineup.current.name == "AFTER DARK"


def test_crossing_into_off_air_cuts_to_no_signal(tmp_path):
    wall = FakeWallClock(1, 59)
    app, player, _ = _daypart_app(
        tmp_path, wall, [{"from": "02:00", "to": "06:00", "off_air": True}]
    )
    app.start()
    assert player.current is not None

    wall.set(2, 0)
    app.step()
    assert "NO SIGNAL" in player.overlays.get(4, "")


def test_rename_only_daypart_does_not_interrupt_playback(tmp_path):
    wall = FakeWallClock(21, 59)
    app, player, _ = _daypart_app(
        tmp_path, wall, [{"from": "22:00", "to": "02:00", "name": "AFTER DARK"}]
    )
    app.start()
    playing = player.current
    plays_before = len(player.played)

    wall.set(22, 0)
    app.step()
    # Same folder, so the episode keeps running - only the ident changes.
    assert player.current == playing
    assert len(player.played) == plays_before
    assert "AFTER DARK" in player.overlays[1]


def test_daypart_watcher_ignores_channels_without_windows(tmp_path):
    wall = FakeWallClock(21, 59)
    app, player, _ = _daypart_app(
        tmp_path, wall, [{"from": "22:00", "to": "02:00", "name": "AFTER DARK"}]
    )
    app.start()
    send(app, Action.CHANNEL_UP)             # CH 03 has no dayparts
    playing = player.current
    wall.set(22, 0)
    app.step()
    assert player.current == playing


def test_daypart_does_not_retune_while_in_standby(tmp_path):
    wall = FakeWallClock(21, 59)
    app, player, _ = _daypart_app(
        tmp_path,
        wall,
        [{"from": "22:00", "to": "02:00", "name": "AFTER DARK",
          "path": str(tmp_path / "afterdark")}],
    )
    app.start()
    send(app, Action.POWER)
    wall.set(22, 0)
    app.step()
    assert app.standby
    assert player.current is None


# ==========================================================================
# Clean power-off exit status
# ==========================================================================
def test_power_off_reports_a_distinct_exit_code(tmp_path):
    from retrobox.app import EXIT_POWERED_OFF

    app, _player, _ = build_app(tmp_path, initial_volume=0)
    app.start()
    send(app, Action.VOLUME_DOWN)             # at 0 -> clean power off
    assert app.powered_off
    assert EXIT_POWERED_OFF == 3


# ==========================================================================
# Boot splash
# ==========================================================================
def _splash_app(tmp_path, **overrides):
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "boot_splash.mp4").write_bytes(b"\x00")
    return build_app(tmp_path, assets_dir=assets, **overrides)


def test_no_splash_configured_starts_normally(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    assert player.current.parent.name == "adultswim"   # straight to a channel
    assert "CH 02" in player.overlays.get(1, "")


def test_splash_plays_once_before_any_channel(tmp_path):
    app, player, _ = _splash_app(tmp_path, boot_splash="boot_splash.mp4")
    app.start()
    assert player.current.name == "boot_splash.mp4"
    assert player.looping is None, "the splash must not loop"
    assert 1 not in player.overlays, "no channel banner over the splash"


def test_splash_falls_through_to_start_channel_when_it_ends(tmp_path):
    app, player, _ = _splash_app(tmp_path, boot_splash="boot_splash.mp4")
    app.start()
    player.finish_current(END_EOF)
    app.step()
    assert app.lineup.current.number == 2
    assert player.current.parent.name == "adultswim"
    assert "CH 02" in player.overlays.get(1, "")


def test_any_keypress_skips_the_splash(tmp_path):
    app, player, _ = _splash_app(tmp_path, boot_splash="boot_splash.mp4")
    app.start()
    assert player.current.name == "boot_splash.mp4"

    send(app, Action.CHANNEL_UP)
    assert player.current.parent.name == "adultswim"
    # The skipping press is consumed, not also applied as a channel change.
    assert app.lineup.current.number == 2


def test_quit_still_works_during_the_splash(tmp_path):
    app, player, _ = _splash_app(tmp_path, boot_splash="boot_splash.mp4")
    app.start()
    app._running = True
    send(app, Action.QUIT)
    assert app._running is False


def test_missing_splash_file_is_skipped_not_fatal(tmp_path):
    app, player, _ = _splash_app(tmp_path, boot_splash="nope.mp4")
    app.start()   # must not raise
    assert player.current.parent.name == "adultswim"


def test_splash_times_out_rather_than_hanging(tmp_path):
    app, player, clock = _splash_app(tmp_path, boot_splash="boot_splash.mp4")
    app.start()
    assert player.current.name == "boot_splash.mp4"

    clock.advance(31)          # clip never reported end-of-file
    app.step()
    assert player.current.parent.name == "adultswim"


def test_splash_absolute_path_is_used_as_given(tmp_path):
    clip = tmp_path / "custom_splash.mp4"
    clip.write_bytes(b"\x00")
    app, player, _ = build_app(tmp_path, boot_splash=str(clip))
    app.start()
    assert player.current == clip
