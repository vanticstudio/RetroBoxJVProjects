"""Dragging the curvature slider changes the television while you watch it.

Curvature is a taste setting with no correct value. The only way anybody sets
it sensibly is by dragging it and looking at the picture, which means the
dashboard has to be able to put a value on the LIVE television before anyone
has committed to it - and, just as importantly, has to be able to take it back
off again.

A preview is never written to config.yaml. Somebody who drags the slider and
walks away must not find their television permanently changed, and "walks
away" has three concrete meanings, all of them tested here: the browser tab
closes, the socket drops mid-drag, and the box is switched off at the wall.
Each one has to end with the last SAVED settings on the screen.
"""

from __future__ import annotations

import pytest

from retrobox.actions import Action, CrtSettings, InputEvent
from retrobox.app import TVApp
from retrobox.config import load_config
from retrobox.input.manager import InputManager
from retrobox.input.web import parse_command
from retrobox.player import MockPlayer
from tests.helpers import FakeClock, FakeWallClock, make_show


def write_config(cfg, root, **crt):
    """Write a config file whose crt block holds ``crt``."""
    lines = [
        f"media_root: {root}",
        "start_offset: 0",
        "shuffle_seed: 7",
        "boot_splash: false",
        "power_off_command: []",
        "crt:",
    ]
    for key, value in crt.items():
        lines.append(f"  {key}: {value}")
    lines += ["channels:", "  - number: 2", '    name: "Sitcoms"',
              f"    path: {root / 'sitcoms'}"]
    cfg.write_text("\n".join(lines) + "\n")


@pytest.fixture
def box(tmp_path):
    """A TV that knows its own config file, showing a gentle 0.12 curve."""
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 3)
    cfg = tmp_path / "config.yaml"
    write_config(cfg, root, enabled="true", curvature=0.12, scanlines="true",
                 scanline_intensity=0.1)
    clock = FakeClock()
    app = TVApp(
        load_config(cfg),
        MockPlayer(),
        InputManager([]),
        clock=clock,
        wall_clock=FakeWallClock(12),
        config_path=cfg,
    )
    return app, app.player, cfg, clock, root


def preview(app, **fields):
    """Send one preview line's worth of settings, as the socket would."""
    app.handle_event(InputEvent(Action.CRT_PREVIEW, crt=CrtSettings(**fields)))


def settle(app, clock, seconds=1.0):
    """Let the main loop run past the throttle so a pending value lands."""
    clock.advance(seconds)
    app.step()


# ==========================================================================
# The socket protocol - untrusted text, so everything is validated
# ==========================================================================
def test_a_curvature_preview_is_a_command_the_socket_understands():
    events = parse_command("crt_preview curvature=0.23")
    assert [e.action for e in events] == [Action.CRT_PREVIEW]
    assert events[0].crt == CrtSettings(curvature=0.23)


def test_a_preview_line_can_carry_the_whole_picture_at_once():
    events = parse_command(
        "crt_preview enabled=on curvature=0.3 corner_radius=0.1 "
        "vignette=0.4 scanlines=off scanline_intensity=0.2"
    )
    assert events[0].crt == CrtSettings(
        enabled=True, curvature=0.3, corner_radius=0.1,
        vignette=0.4, scanlines=False, scanline_intensity=0.2,
    )


def test_cancelling_a_preview_is_a_command_the_socket_understands():
    assert parse_command("crt_cancel") == [InputEvent(Action.CRT_CANCEL)]


def test_a_preview_line_is_case_and_space_tolerant():
    events = parse_command("  CRT_PREVIEW   Curvature=0.2  ")
    assert events[0].crt == CrtSettings(curvature=0.2)


@pytest.mark.parametrize("line", [
    "crt_preview",                          # nothing to preview
    "crt_preview curvature",                # no value at all
    "crt_preview curvature=",               # empty value
    "crt_preview curvature=abc",            # not a number
    "crt_preview curvature=0.9",            # above the allowed range
    "crt_preview curvature=-0.1",           # below it
    "crt_preview curvature=nan",            # not orderable, so never in range
    "crt_preview curvature=inf",
    "crt_preview vignette=2",
    "crt_preview corner_radius=0.9",
    "crt_preview scanline_intensity=1.5",
    "crt_preview scanlines=maybe",          # not a yes or a no
    "crt_preview enabled=1.5",
    "crt_preview brightness=0.5",           # not a setting we have
    "crt_preview curvature=0.1 curvature=0.2",   # which one did they mean?
    "crt_preview curvature=0.1 rm -rf /",
    "crt_preview=0.2",
])
def test_a_malformed_preview_line_is_refused_rather_than_guessed_at(line):
    assert parse_command(line) == []


@pytest.mark.parametrize("word,expected", [
    ("on", True), ("off", False), ("true", True), ("false", False),
    ("yes", True), ("no", False), ("1", True), ("0", False),
])
def test_the_on_off_words_a_dashboard_might_send_are_all_understood(word, expected):
    events = parse_command(f"crt_preview scanlines={word}")
    assert events[0].crt == CrtSettings(scanlines=expected)


# ==========================================================================
# It reaches the picture, and it reaches it now
# ==========================================================================
def test_a_previewed_curvature_reaches_the_picture_without_a_restart(box):
    app, player, _cfg, _clock, _root = box
    preview(app, curvature=0.45)
    assert player.crt is not None
    assert player.crt.curvature == pytest.approx(0.45)


def test_previewing_the_effect_off_clears_the_shader_rather_than_flattening_it(box):
    app, player, _cfg, _clock, _root = box
    preview(app, enabled=False)
    # None is "no shader at all". An identity pass would look the same and
    # cost this box's two-core Celeron GPU work on every single frame.
    assert player.crt_applied[-1] is None


def test_the_scanline_switch_and_its_strength_preview_too(box):
    app, player, _cfg, clock, _root = box
    preview(app, scanlines=False)
    assert player.crt.scanlines is False
    settle(app, clock)
    preview(app, scanline_intensity=0.4)
    assert player.crt.scanline_intensity == pytest.approx(0.4)
    # The earlier nudge is still there: the dashboard sends what moved, not
    # the whole panel, so the two have to accumulate.
    assert player.crt.scanlines is False


def test_a_preview_builds_on_the_saved_settings_it_did_not_mention(box):
    app, player, _cfg, _clock, _root = box
    preview(app, curvature=0.45)
    assert player.crt.scanline_intensity == pytest.approx(0.1)


# ==========================================================================
# A preview is a preview: nothing is kept
# ==========================================================================
def test_previewing_never_writes_to_the_config_file(box):
    app, _player, cfg, clock, _root = box
    before = cfg.read_text()
    for value in (0.2, 0.3, 0.4):
        preview(app, curvature=value)
        settle(app, clock)
    assert cfg.read_text() == before
    assert app.config.crt.curvature == pytest.approx(0.12)


def test_switching_the_box_off_at_the_wall_mid_preview_comes_back_saved(box):
    app, _player, cfg, _clock, _root = box
    preview(app, curvature=0.45, enabled=True)
    # No shutdown: the mains going is exactly "the process stops here". The
    # only thing that survives is the file, so that is what is checked.
    restarted = load_config(cfg)
    assert restarted.crt.curvature == pytest.approx(0.12)


def test_cancel_puts_the_last_saved_settings_back_on_the_picture(box):
    app, player, _cfg, clock, _root = box
    preview(app, curvature=0.45)
    settle(app, clock)
    app.handle_event(InputEvent(Action.CRT_CANCEL))
    settle(app, clock)
    assert player.crt.curvature == pytest.approx(0.12)


def test_cancel_puts_the_effect_back_on_after_previewing_it_off(box):
    app, player, _cfg, clock, _root = box
    preview(app, enabled=False)
    settle(app, clock)
    assert player.crt is None
    app.handle_event(InputEvent(Action.CRT_CANCEL))
    settle(app, clock)
    assert player.crt is not None
    assert player.crt.curvature == pytest.approx(0.12)


def test_cancelling_when_nothing_was_previewed_leaves_the_picture_alone(box):
    app, player, _cfg, clock, _root = box
    app.handle_event(InputEvent(Action.CRT_CANCEL))
    settle(app, clock)
    app.handle_event(InputEvent(Action.CRT_CANCEL))
    settle(app, clock)
    assert player.crt_applied == []


def test_a_dashboard_that_went_away_mid_drag_returns_to_the_saved_settings(box):
    app, player, _cfg, clock, _root = box
    preview(app, curvature=0.45)
    settle(app, clock)
    assert player.crt.curvature == pytest.approx(0.45)
    # The browser tab closed, or the wifi dropped: nothing tells the box, so
    # the box has to notice for itself that nobody is watching any more.
    clock.advance(TVApp.CRT_PREVIEW_HOLD_SECONDS + 1.0)
    app.step()
    assert player.crt.curvature == pytest.approx(0.12)


def test_a_dashboard_that_keeps_saying_it_is_there_keeps_its_preview(box):
    app, player, _cfg, clock, _root = box
    preview(app, curvature=0.45)
    for _ in range(6):
        clock.advance(TVApp.CRT_PREVIEW_HOLD_SECONDS / 2.0)
        app.step()
        preview(app, curvature=0.45)          # the dashboard's heartbeat
    assert player.crt.curvature == pytest.approx(0.45)


def test_a_heartbeat_that_changes_nothing_costs_the_picture_nothing(box):
    app, player, _cfg, clock, _root = box
    preview(app, curvature=0.45)
    settle(app, clock)
    applied = len(player.crt_applied)
    for _ in range(10):
        preview(app, curvature=0.45)
        settle(app, clock)
    assert len(player.crt_applied) == applied


# ==========================================================================
# Saving is the end of the preview, whatever the preview was showing
# ==========================================================================
def test_saving_the_previewed_value_does_not_recompile_the_same_shader(box):
    app, player, cfg, clock, root = box
    preview(app, curvature=0.45)
    settle(app, clock)
    applied = len(player.crt_applied)
    write_config(cfg, root, enabled="true", curvature=0.45, scanlines="true",
                 scanline_intensity=0.1)
    app.handle_event(InputEvent(Action.RELOAD))
    # The picture is already showing exactly this. Re-applying it would make
    # mpv compile the same shader again for no visible change at all.
    assert len(player.crt_applied) == applied


def test_saving_something_else_while_previewing_still_drops_the_preview(box):
    app, player, cfg, clock, root = box
    preview(app, curvature=0.45)
    settle(app, clock)
    # A channel rename, with the picture settings untouched. The preview is
    # not what anybody saved, so it must not survive the save.
    write_config(cfg, root, enabled="true", curvature=0.12, scanlines="true",
                 scanline_intensity=0.1)
    app.handle_event(InputEvent(Action.RELOAD))
    assert player.crt.curvature == pytest.approx(0.12)


def test_cancel_after_a_save_restores_what_was_just_saved(box):
    app, player, cfg, clock, root = box
    write_config(cfg, root, enabled="true", curvature=0.3, scanlines="true",
                 scanline_intensity=0.1)
    app.handle_event(InputEvent(Action.RELOAD))
    preview(app, curvature=0.5)
    settle(app, clock)
    app.handle_event(InputEvent(Action.CRT_CANCEL))
    settle(app, clock)
    assert player.crt.curvature == pytest.approx(0.3)


# ==========================================================================
# The throttle. A slider fires per pixel; a shader compile is real work.
# ==========================================================================
def test_a_burst_of_slider_movements_makes_a_bounded_number_of_changes(box):
    app, player, _cfg, clock, _root = box
    # A hundred events inside a tenth of a second, which is roughly what a
    # dragged range input produces on a fast mouse.
    for step in range(100):
        preview(app, curvature=0.2 + step * 0.002)
        clock.advance(0.001)
        app.step()
    assert len(player.crt_applied) <= 3


def test_the_value_the_slider_was_let_go_of_is_the_one_left_on_the_picture(box):
    app, player, _cfg, clock, _root = box
    for step in range(100):
        preview(app, curvature=0.2 + step * 0.002)
        clock.advance(0.001)
        app.step()
    settle(app, clock)
    # 0.2 + 99 * 0.002. A throttle that simply dropped events would leave the
    # television on whatever value happened to arrive on a tick boundary.
    assert player.crt.curvature == pytest.approx(0.398)


def test_a_client_ignoring_the_guidance_still_cannot_melt_the_television(box):
    app, player, _cfg, clock, _root = box
    # No step() at all between events: a caller hammering the socket flat out
    # with no main-loop iteration in between must not turn into one shader
    # compile per event.
    for step in range(200):
        preview(app, curvature=0.2 + step * 0.001)
    assert len(player.crt_applied) == 1


def test_a_player_that_refuses_the_change_does_not_take_the_television_down(box):
    app, player, _cfg, clock, _root = box

    refusals = []

    def explode(_crt):
        refusals.append(_crt)
        raise RuntimeError("no shader for you")

    player.set_crt = explode
    preview(app, curvature=0.45)
    settle(app, clock)
    assert refusals, "the player was never actually asked"
    # Still running, still on the channel it was on. A cosmetic effect is
    # outranked by the programme somebody is halfway through.
    assert app.lineup.current.number == 2
    # And the box has not decided the picture changed when it did not, so the
    # next attempt is a real attempt rather than a "no change needed".
    player.set_crt = MockPlayer.set_crt.__get__(player, MockPlayer)
    settle(app, clock)
    preview(app, curvature=0.45)
    assert player.crt.curvature == pytest.approx(0.45)


# ==========================================================================
# End to end: a line on the socket, and the picture changes
# ==========================================================================
def test_a_preview_line_off_the_socket_reaches_the_picture(box):
    app, player, _cfg, _clock, _root = box
    for event in parse_command("crt_preview curvature=0.4 scanlines=off"):
        app.handle_event(event)
    assert player.crt.curvature == pytest.approx(0.4)
    assert player.crt.scanlines is False


def test_a_cancel_line_off_the_socket_restores_the_saved_picture(box):
    app, player, _cfg, clock, _root = box
    for event in parse_command("crt_preview curvature=0.4"):
        app.handle_event(event)
    settle(app, clock)
    for event in parse_command("crt_cancel"):
        app.handle_event(event)
    settle(app, clock)
    assert player.crt.curvature == pytest.approx(0.12)


def test_flipping_between_preview_and_cancel_is_throttled_too(box):
    app, player, _cfg, clock, _root = box
    for _ in range(50):
        preview(app, curvature=0.45)
        app.handle_event(InputEvent(Action.CRT_CANCEL))
        clock.advance(0.002)
        app.step()
    # Alternating two different values is the one pattern that defeats a
    # "same value, do nothing" guard, so the time gate has to cover it.
    assert len(player.crt_applied) <= 3
