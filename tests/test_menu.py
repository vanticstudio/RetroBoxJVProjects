import pytest

from retrobox import hwdetect
from retrobox.actions import Action
from retrobox.menu import (
    SCREEN_ABOUT,
    SCREEN_AUDIO,
    SCREEN_CHANNELS,
    SCREEN_MAIN,
    SCREEN_SHUTDOWN,
    MenuContext,
    MenuModel,
)
from retrobox.overlay import CANVAS_H, CANVAS_W, OverlayManager, menu_row_at
from retrobox.overlay import _MENU_ROW_H, _MENU_TOP, _MENU_X
from retrobox.player import MockPlayer
from tests.helpers import FakeClock, make_show
from tests.test_app import build_app, send

_MENU_ID = 7


# ==========================================================================
# The model (pure, no display)
# ==========================================================================
def _ctx(**over):
    base = dict(
        channels=[(2, "Sitcoms"), (3, "Music Videos"), (4, "Movies")],
        current_channel=3,
        volume=70,
        muted=False,
        audio_devices=["alsa/hdmi:CARD=PCH,DEV=0", "alsa/hdmi:CARD=PCH,DEV=1"],
        current_audio="alsa/hdmi:CARD=PCH,DEV=0",
        version="1.0.0",
    )
    base.update(over)
    return MenuContext(**base)


def test_main_screen_has_the_five_items():
    model = MenuModel(_ctx())
    assert [r.key for r in model.rows()] == [
        "channels", "volume", "audio", "shutdown", "about",
    ]
    assert model.title == "MENU"


def test_main_screen_shows_live_values():
    rows = {r.key: r.value for r in MenuModel(_ctx()).rows()}
    assert rows["channels"] == "CH 03  Music Videos"
    assert rows["volume"] == "70"
    assert rows["audio"] == "PCH,d0"          # shortened for the screen
    assert rows["about"] == "JV Projects"


def test_muted_volume_reads_as_muted():
    rows = {r.key: r.value for r in MenuModel(_ctx(muted=True)).rows()}
    assert rows["volume"] == "MUTED"


def test_highlight_wraps_both_ways():
    model = MenuModel(_ctx())
    assert model.index == 0
    model.move(-1)
    assert model.index == 4          # wrapped to the bottom
    model.move(1)
    assert model.index == 0


def test_channels_screen_preselects_what_you_are_watching():
    model = MenuModel(_ctx())
    model.activate()                 # "Channels"
    assert model.screen == SCREEN_CHANNELS
    assert model.rows()[model.index].key == "ch:3"


def test_selecting_a_channel_returns_a_tune_command():
    model = MenuModel(_ctx())
    model.activate()                 # into Channels
    model.move(1)                    # ch:3 -> ch:4
    command = model.activate()
    assert command.kind == "tune" and command.value == 4


def test_channel_rows_mark_the_current_one():
    model = MenuModel(_ctx())
    model.activate()
    current = [r for r in model.rows() if r.key == "ch:3"][0]
    assert current.value.endswith("<")


def test_audio_screen_lists_devices_and_marks_the_active_one():
    model = MenuModel(_ctx())
    model.index = 2
    model.activate()
    assert model.screen == SCREEN_AUDIO
    rows = model.rows()
    assert rows[0].value == "<"      # the one currently in use
    assert rows[1].value == ""
    assert rows[-1].key == "back"


def test_audio_screen_copes_with_no_devices():
    model = MenuModel(_ctx(audio_devices=[]))
    model.index = 2
    model.activate()
    rows = model.rows()
    assert rows[0].label == "No HDMI outputs detected"
    assert rows[0].selectable is False
    # ...and the highlight lands on Back rather than the dead row.
    assert rows[model.index].key == "back"


def test_selecting_a_device_returns_an_audio_command():
    model = MenuModel(_ctx())
    model.index = 2
    model.activate()
    model.index = 1
    command = model.activate()
    assert command.kind == "audio"
    assert command.value == "alsa/hdmi:CARD=PCH,DEV=1"


def test_shutdown_defaults_to_no_and_can_confirm():
    model = MenuModel(_ctx())
    model.index = 3
    model.activate()
    assert model.screen == SCREEN_SHUTDOWN
    assert model.rows()[model.index].key == "no"   # safe default

    assert model.activate().kind == "none"         # "No" returns to the menu
    assert model.screen == SCREEN_MAIN

    model.index = 3
    model.activate()
    model.move(1)
    assert model.activate().kind == "shutdown"


def test_about_screen_skips_its_display_only_rows():
    model = MenuModel(_ctx())
    model.index = 4
    model.activate()
    assert model.screen == SCREEN_ABOUT
    assert [r.label for r in model.rows()][:2] == ["JV Projects", "Retro Box"]
    assert model.rows()[model.index].key == "back"   # only selectable row


def test_back_returns_to_main_then_reports_exhausted():
    model = MenuModel(_ctx())
    model.activate()
    assert model.back() is True and model.screen == SCREEN_MAIN
    assert model.back() is False     # the app closes the menu at this point


def test_volume_row_adjusts_in_place():
    model = MenuModel(_ctx())
    model.index = 1
    assert model.adjust(1).kind == "volume"
    model.index = 0
    assert model.adjust(1).kind == "none"


# ==========================================================================
# Rendering + hit-testing
# ==========================================================================
def _overlay(tmp_path):
    from retrobox.config import config_from_dict

    make_show(tmp_path, "a", 1)
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "A", "path": str(tmp_path / "a")}]}
    )
    player = MockPlayer()
    return OverlayManager(player, config, clock=FakeClock()), player


def test_menu_draws_and_is_persistent(tmp_path):
    om, player = _overlay(tmp_path)
    model = MenuModel(_ctx())
    om.show_menu(model.title, model.rows(), model.index)

    ass = player.overlays[_MENU_ID]
    assert "MENU" in ass and "Channels" in ass and "Shut down" in ass
    assert "> Channels" in ass          # highlight marker on row 0
    assert om.menu_visible

    om.tick()                           # menus never time out
    assert _MENU_ID in player.overlays
    om.clear_menu()
    assert _MENU_ID not in player.overlays and not om.menu_visible


def test_clear_all_takes_the_menu_too(tmp_path):
    om, player = _overlay(tmp_path)
    model = MenuModel(_ctx())
    om.show_menu(model.title, model.rows(), model.index)
    om.clear_all()
    assert _MENU_ID not in player.overlays and not om.menu_visible


def _row_centre(row: int):
    """Canvas coords for the middle of a menu row."""
    return _MENU_X + 50, _MENU_TOP + row * _MENU_ROW_H + _MENU_ROW_H / 2


def test_hit_test_resolves_each_row():
    rows = MenuModel(_ctx()).rows()
    for i in range(len(rows)):
        x, y = _row_centre(i)
        assert menu_row_at(rows, 0, x, y) == i


def test_hit_test_misses_outside_the_panel():
    rows = MenuModel(_ctx()).rows()
    _, y = _row_centre(0)
    assert menu_row_at(rows, 0, 10, y) is None            # left of the panel
    assert menu_row_at(rows, 0, CANVAS_W - 10, y) is None  # right of it
    assert menu_row_at(rows, 0, _MENU_X + 50, _MENU_TOP - 40) is None  # above
    assert menu_row_at(rows, 0, _MENU_X + 50, CANVAS_H) is None        # below


def test_hit_test_ignores_display_only_rows():
    model = MenuModel(_ctx())
    model.index = 4
    model.activate()                    # About: rows 0 and 1 are not selectable
    rows = model.rows()
    assert menu_row_at(rows, model.index, *_row_centre(0)) is None
    assert menu_row_at(rows, model.index, *_row_centre(1)) is None
    assert menu_row_at(rows, model.index, *_row_centre(2)) == 2   # Back


# ==========================================================================
# App integration - KEYBOARD
# ==========================================================================
def _app(tmp_path, monkeypatch, devices=("alsa/hdmi:CARD=PCH,DEV=0",)):
    monkeypatch.setattr(hwdetect, "detect_audio", lambda: list(devices))
    app, player, clock = build_app(tmp_path)
    app.start()
    return app, player, clock


def test_menu_opens_pauses_and_closes_resuming(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    playing = player.current

    send(app, Action.MENU)
    assert app._menu is not None
    assert player.paused is True
    assert player.mouse_enabled is True
    assert _MENU_ID in player.overlays

    send(app, Action.MENU)
    assert app._menu is None
    assert player.paused is False
    assert player.mouse_enabled is False
    assert _MENU_ID not in player.overlays
    assert player.current == playing, "closing must not restart playback"


def test_arrows_move_the_highlight_not_the_channel(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    send(app, Action.MENU)
    assert "> Channels" in player.overlays[_MENU_ID]

    send(app, Action.CHANNEL_DOWN)
    assert app.lineup.current.number == 2, "the tuner must not move"
    assert "> Volume" in player.overlays[_MENU_ID]

    send(app, Action.CHANNEL_UP)
    assert "> Channels" in player.overlays[_MENU_ID]


def test_keyboard_can_jump_straight_to_a_channel(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    send(app, Action.MENU)
    send(app, Action.ENTER)              # into Channels
    assert app._menu.screen == SCREEN_CHANNELS

    send(app, Action.CHANNEL_DOWN)       # ch 2 -> ch 3
    send(app, Action.ENTER)
    assert app.lineup.current.number == 3
    assert app._menu is None, "tuning closes the menu"
    assert player.paused is False


def test_volume_row_changes_the_volume(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    send(app, Action.MENU)
    send(app, Action.CHANNEL_DOWN)       # highlight Volume
    send(app, Action.VOLUME_UP)
    assert app.volume == 75 and player.volume == 75
    assert "75" in player.overlays[_MENU_ID]


def test_back_steps_out_then_closes(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    send(app, Action.MENU)
    send(app, Action.ENTER)              # into Channels
    send(app, Action.LAST_CHANNEL)
    assert app._menu.screen == SCREEN_MAIN

    send(app, Action.LAST_CHANNEL)       # already at the top -> close
    assert app._menu is None
    assert player.paused is False


def test_audio_choice_applies_live_for_the_session(tmp_path, monkeypatch):
    app, player, _ = _app(
        tmp_path, monkeypatch, devices=("alsa/hdmi:CARD=PCH,DEV=0", "alsa/hdmi:CARD=PCH,DEV=1")
    )
    send(app, Action.MENU)
    for _ in range(2):
        send(app, Action.CHANNEL_DOWN)   # highlight Audio output
    send(app, Action.ENTER)
    send(app, Action.CHANNEL_DOWN)       # second device
    send(app, Action.ENTER)

    assert player.audio_device == "alsa/hdmi:CARD=PCH,DEV=1"
    assert app._audio_device == "alsa/hdmi:CARD=PCH,DEV=1"
    # Session-only: the config on disk is untouched.
    assert app.config.audio_device is None


def test_shutdown_from_the_menu(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    send(app, Action.MENU)
    for _ in range(3):
        send(app, Action.CHANNEL_DOWN)   # highlight Shut down
    send(app, Action.ENTER)
    send(app, Action.CHANNEL_DOWN)       # No -> Yes
    send(app, Action.ENTER)
    assert app.powered_off is True
    assert app._menu is None


def test_guide_and_menu_do_not_fight(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    send(app, Action.GUIDE)
    assert app.overlay.guide_visible
    send(app, Action.MENU)
    assert app._menu is not None
    # Arrow keys now belong to the menu, not the guide.
    send(app, Action.CHANNEL_DOWN)
    assert "> Volume" in player.overlays[_MENU_ID]


# ==========================================================================
# App integration - MOUSE
# ==========================================================================
def _norm(x, y):
    """Canvas coords -> the normalised pair the player reports."""
    return (x / CANVAS_W, y / CANVAS_H)


def test_pointer_is_only_live_while_the_menu_is_open(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    player.mouse_position = _norm(*_row_centre(1))
    assert player.get_mouse_position() is None, "pointer off while watching"

    send(app, Action.MENU)
    assert player.get_mouse_position() is not None


def test_hovering_moves_the_highlight(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    send(app, Action.MENU)
    assert "> Channels" in player.overlays[_MENU_ID]

    player.mouse_position = _norm(*_row_centre(3))   # Shut down
    app.step()
    assert "> Shut down" in player.overlays[_MENU_ID]


def test_clicking_a_row_activates_it(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    send(app, Action.MENU)

    player.click_at(*_norm(*_row_centre(0)))          # "Channels"
    app.step()
    assert app._menu.screen == SCREEN_CHANNELS


def test_clicking_a_channel_tunes_and_closes(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    send(app, Action.MENU)
    player.click_at(*_norm(*_row_centre(0)))          # into Channels
    app.step()

    rows = app._menu.rows()
    target = [i for i, r in enumerate(rows) if r.key == "ch:4"][0]
    player.click_at(*_norm(*_row_centre(target)))
    app.step()

    assert app.lineup.current.number == 4
    assert app._menu is None
    assert player.paused is False


def test_clicking_outside_the_menu_does_nothing(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    send(app, Action.MENU)
    before = player.overlays[_MENU_ID]

    player.click_at(*_norm(10, 10))                   # off the panel entirely
    app.step()
    assert app._menu is not None
    assert player.overlays[_MENU_ID] == before


def test_clicks_are_ignored_when_the_menu_is_closed(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    player.mouse_enabled = True                        # pretend it leaked through
    player.click_at(*_norm(*_row_centre(0)))
    app.step()
    assert app._menu is None
    assert app.lineup.current.number == 2


def test_mouse_can_shut_down_via_the_confirm_screen(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    send(app, Action.MENU)

    player.click_at(*_norm(*_row_centre(3)))           # Shut down
    app.step()
    assert app._menu.screen == SCREEN_SHUTDOWN

    player.click_at(*_norm(*_row_centre(1)))           # "Yes, shut down"
    app.step()
    assert app.powered_off is True


def test_sleep_timer_firing_closes_the_menu_cleanly(tmp_path, monkeypatch):
    app, player, clock = _app(tmp_path, monkeypatch)
    send(app, Action.SLEEP)              # 30 minutes
    send(app, Action.MENU)
    assert app._menu is not None

    clock.advance(31 * 60)
    app.step()                            # timer fires -> standby
    assert app.standby is True
    assert app._menu is None, "menu must not stay 'open' but invisible"
    assert player.mouse_enabled is False


def test_daypart_boundary_waits_for_the_menu_to_close(tmp_path, monkeypatch):
    from tests.helpers import FakeWallClock

    monkeypatch.setattr(hwdetect, "detect_audio", lambda: [])
    make_show(tmp_path, "afterdark", 3)
    wall = FakeWallClock(21, 59)
    app, player, _ = build_app(
        tmp_path,
        wall_clock=wall,
        channels=[{
            "number": 2, "name": "Talk", "path": str(tmp_path / "adultswim"),
            "dayparts": [{"from": "22:00", "to": "02:00", "name": "AFTER DARK",
                          "path": str(tmp_path / "afterdark")}],
        }],
    )
    app.start()
    send(app, Action.MENU)

    wall.set(22, 0)
    app.step()
    assert player.current.parent.name == "adultswim", "must not retune behind the menu"
    assert player.paused is True

    send(app, Action.MENU)                # close it
    app.step()
    assert player.current.parent.name == "afterdark"


# ==========================================================================
# Show / hide contract: boot hidden, Escape in and out, cursor only in menu
# ==========================================================================
def test_boot_goes_straight_to_channel_mode_with_no_menu(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    assert app._menu is None
    assert _MENU_ID not in player.overlays
    assert player.current is not None, "playing a channel, not sitting on a menu"


def test_cursor_is_inert_at_boot_even_with_a_mouse_attached(tmp_path, monkeypatch):
    app, player, _ = _app(tmp_path, monkeypatch)
    assert player.mouse_enabled is False
    # A mouse that is physically present still reports nothing in channel mode.
    player.mouse_position = _norm(*_row_centre(0))
    assert player.get_mouse_position() is None
    player.click_at(*_norm(*_row_centre(0)))
    app.step()
    assert app._menu is None, "a stray click must not open anything"


def test_escape_opens_and_closes_the_menu(tmp_path, monkeypatch):
    from retrobox.input.keymap import evdev_key_to_event

    app, player, _ = _app(tmp_path, monkeypatch)
    escape = evdev_key_to_event("KEY_ESC")
    assert escape.action is Action.MENU, "Escape is the menu key now"

    app.handle_event(escape)
    assert app._menu is not None
    assert player.mouse_enabled is True, "cursor appears with the menu"

    app.handle_event(escape)
    assert app._menu is None
    assert player.mouse_enabled is False, "cursor disappears again"
    assert _MENU_ID not in player.overlays, "nothing left on screen"


def test_escape_no_longer_quits(tmp_path, monkeypatch):
    from retrobox.input.keymap import evdev_key_to_event

    app, player, _ = _app(tmp_path, monkeypatch)
    app._running = True
    app.handle_event(evdev_key_to_event("KEY_ESC"))
    assert app._running is True, "Escape must not shut the box down"
    assert evdev_key_to_event("KEY_Q").action is Action.QUIT
