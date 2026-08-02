"""Making a config change reach the TV that is already running.

A channel added from a phone is invisible to the player until something tells
it to look again. That something is one more command on the same Unix socket
every other button already uses - not a second control path.
"""

import pytest

from retrobox.actions import Action, InputEvent
from retrobox.app import TVApp
from retrobox.config import load_config
from retrobox.input.manager import InputManager
from retrobox.input.web import parse_command
from retrobox.player import MockPlayer
from tests.helpers import FakeClock, FakeWallClock, make_show


def build_from_file(tmp_path, body):
    """A TVApp that knows where its config file is, so it can re-read it."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(body)
    app = TVApp(
        load_config(cfg),
        MockPlayer(),
        InputManager([]),
        clock=FakeClock(),
        wall_clock=FakeWallClock(12),
        config_path=cfg,
    )
    return app, app.player, cfg


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    for name in ("sitcoms", "movies"):
        make_show(root, name, 3)
    body = (
        f"media_root: {root}\nstart_offset: 0\nshuffle_seed: 7\n"
        f"boot_splash: false\npower_off_command: []\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
        f'  - number: 5\n    name: "Movies"\n    path: {root / "movies"}\n'
    )
    app, player, cfg = build_from_file(tmp_path, body)
    return app, player, cfg, root


def rewrite(cfg, root, channels, **extra):
    lines = [f"media_root: {root}", "start_offset: 0", "shuffle_seed: 7",
             "boot_splash: false", "power_off_command: []"]
    for key, value in extra.items():
        lines.append(f"{key}: {value}")
    lines.append("channels:")
    for number, name, folder in channels:
        lines += [f"  - number: {number}", f'    name: "{name}"', f"    path: {folder}"]
    cfg.write_text("\n".join(lines) + "\n")


# ==========================================================================
# The command itself
# ==========================================================================
def test_reload_is_a_command_the_socket_understands():
    assert parse_command("reload") == [InputEvent(Action.RELOAD)]


def test_reload_is_case_and_space_tolerant():
    assert [e.action for e in parse_command("  RELOAD  ")] == [Action.RELOAD]


# ==========================================================================
# What it does
# ==========================================================================
def test_a_channel_added_on_disk_shows_up(box):
    app, player, cfg, root = box
    app.start()
    assert app.lineup.numbers == [2, 5]

    make_show(root, "cartoons", 2)
    rewrite(cfg, root, [
        (2, "Sitcoms", root / "sitcoms"),
        (5, "Movies", root / "movies"),
        (7, "Cartoons", root / "cartoons"),
    ])
    app.handle_event(InputEvent(Action.RELOAD))

    assert app.lineup.numbers == [2, 5, 7]
    assert app.select_channel_number(7) is True


def test_a_rename_reaches_the_running_tv(box):
    app, player, cfg, root = box
    app.start()
    rewrite(cfg, root, [
        (2, "Classic Sitcoms", root / "sitcoms"),
        (5, "Movies", root / "movies"),
    ])
    app.handle_event(InputEvent(Action.RELOAD))

    assert app.lineup.current.name == "Classic Sitcoms"
    assert app.build_status()["channel"]["name"] == "Classic Sitcoms"


def test_the_show_that_is_on_keeps_playing(box):
    # Reloading because someone renamed channel 9 must not restart the film
    # the person on the sofa is halfway through.
    app, player, cfg, root = box
    app.start()
    playing = player.current
    plays_before = len(player.played)

    rewrite(cfg, root, [
        (2, "Sitcoms", root / "sitcoms"),
        (5, "Renamed", root / "movies"),
    ])
    app.handle_event(InputEvent(Action.RELOAD))

    assert player.current == playing, "playback was restarted"
    assert len(player.played) == plays_before, "a new episode was started"


def test_deleting_the_channel_that_is_playing_falls_back(box):
    app, player, cfg, root = box
    app.start()
    app.select_channel_number(5)
    # A channel change is bridged: the old show keeps playing until the switch
    # commits, so let it, or "what is on screen" is still the previous channel.
    app._clock.advance(2)
    app.step()
    assert app.lineup.current.number == 5
    assert player.current.parent == root / "movies", "the switch never landed"

    rewrite(cfg, root, [(2, "Sitcoms", root / "sitcoms")])
    app.handle_event(InputEvent(Action.RELOAD))

    assert app.lineup.numbers == [2]
    assert app.lineup.current.number == 2, "left pointing at a channel that is gone"
    # Not just "something is on screen" - the old channel's episode would still
    # be playing if nothing retuned. It has to be playing the channel it is on.
    assert player.current.parent == root / "sitcoms", (
        "still playing the deleted channel's folder"
    )


def test_deleting_the_playing_channel_prefers_the_configured_start_channel(box):
    app, player, cfg, root = box
    make_show(root, "cartoons", 2)
    app.start()
    app.select_channel_number(5)

    rewrite(
        cfg, root,
        [(2, "Sitcoms", root / "sitcoms"), (7, "Cartoons", root / "cartoons")],
        start_channel=7,
    )
    app.handle_event(InputEvent(Action.RELOAD))
    assert app.lineup.current.number == 7


def test_repointing_the_playing_channel_retunes_it(box):
    # The channel survived but it is a different folder now, so what is on
    # screen is no longer what that channel is.
    app, player, cfg, root = box
    make_show(root, "cartoons", 2)
    app.start()
    plays_before = len(player.played)

    rewrite(cfg, root, [
        (2, "Sitcoms", root / "cartoons"),
        (5, "Movies", root / "movies"),
    ])
    app.handle_event(InputEvent(Action.RELOAD))

    assert len(player.played) > plays_before, "it kept playing the old folder"
    assert player.current.parent == root / "cartoons"


def test_a_new_audio_device_is_applied_without_a_restart(box):
    app, player, cfg, root = box
    app.start()
    rewrite(
        cfg, root,
        [(2, "Sitcoms", root / "sitcoms"), (5, "Movies", root / "movies")],
        audio_device='"alsa/hdmi:CARD=PCH,DEV=0"',
    )
    app.handle_event(InputEvent(Action.RELOAD))
    assert player.audio_device == "alsa/hdmi:CARD=PCH,DEV=0"


def test_a_new_curvature_reaches_the_picture_without_a_restart(box):
    """Curvature has no correct value - it is judged by eye, on the television.

    Anyone setting it drags the slider and looks. If the change only lands on
    the next restart, the slider is a control that appears to do nothing.
    """
    app, player, cfg, root = box
    app.start()
    rewrite(
        cfg, root,
        [(2, "Sitcoms", root / "sitcoms"), (5, "Movies", root / "movies")],
        crt="{enabled: true, curvature: 0.34}",
    )
    app.handle_event(InputEvent(Action.RELOAD))

    assert player.crt is not None
    assert player.crt.curvature == pytest.approx(0.34)


def test_turning_the_crt_effect_off_reaches_the_picture_without_a_restart(box):
    app, player, cfg, root = box
    app.start()
    rewrite(
        cfg, root,
        [(2, "Sitcoms", root / "sitcoms"), (5, "Movies", root / "movies")],
        crt="{enabled: false}",
    )
    app.handle_event(InputEvent(Action.RELOAD))

    assert player.crt is None
    assert player.crt_applied[-1] is None


def test_a_reload_that_changes_nothing_about_the_picture_leaves_the_shader_alone(box):
    """Every dashboard save comes through here.

    Re-applying the same effect makes mpv recompile the shader, which is a
    visible hitch on this hardware - so renaming a channel must not do it.
    """
    app, player, cfg, root = box
    app.start()
    rewrite(cfg, root, [(2, "Comedy", root / "sitcoms"), (5, "Movies", root / "movies")])
    app.handle_event(InputEvent(Action.RELOAD))

    assert player.crt_applied == []


def test_a_new_sleep_ladder_is_live(box):
    app, player, cfg, root = box
    app.start()
    rewrite(
        cfg, root,
        [(2, "Sitcoms", root / "sitcoms"), (5, "Movies", root / "movies")],
        sleep_timer="[5]",
    )
    app.handle_event(InputEvent(Action.RELOAD))
    assert app.config.sleep_steps == (5,)


# ==========================================================================
# It must never take the box down
# ==========================================================================
def test_a_config_that_will_not_parse_leaves_the_box_running(box):
    app, player, cfg, root = box
    app.start()
    playing = player.current

    cfg.write_text("channels: [[[ this is not yaml\n")
    app.handle_event(InputEvent(Action.RELOAD))

    assert app.lineup.numbers == [2, 5], "the running lineup was thrown away"
    assert player.current == playing, "playback stopped"


def test_a_config_that_parses_but_is_invalid_leaves_the_box_running(box):
    app, player, cfg, root = box
    app.start()
    rewrite(cfg, root, [
        (2, "One", root / "sitcoms"),
        (2, "Duplicate", root / "movies"),
    ])
    app.handle_event(InputEvent(Action.RELOAD))
    assert [c.name for c in app.config.channels] == ["Sitcoms", "Movies"]


def test_a_config_file_that_has_vanished_leaves_the_box_running(box):
    app, player, cfg, root = box
    app.start()
    cfg.unlink()
    app.handle_event(InputEvent(Action.RELOAD))
    assert app.lineup.numbers == [2, 5]


def test_reload_without_a_config_path_is_a_no_op_not_a_crash(tmp_path):
    # TVApp can be built straight from a Config object (the tests do it, and
    # so could an embedder). Reload has nothing to re-read, and says so.
    from tests.test_app import build_app

    app, player, _ = build_app(tmp_path)
    app.start()
    app.handle_event(InputEvent(Action.RELOAD))
    assert app.lineup.numbers == [2, 3, 4]


def test_the_menu_survives_a_reload_underneath_it(box, monkeypatch):
    from retrobox import hwdetect

    monkeypatch.setattr(hwdetect, "detect_audio", lambda: [])
    app, player, cfg, root = box
    app.start()
    app.handle_event(InputEvent(Action.MENU))
    assert app._menu is not None

    rewrite(cfg, root, [(2, "Sitcoms", root / "sitcoms")])
    app.handle_event(InputEvent(Action.RELOAD))

    assert app._menu is not None, "the menu was torn out from under the viewer"
    app.handle_event(InputEvent(Action.MENU))
    assert app._menu is None and player.paused is False


# ==========================================================================
# End to end: a phone edits the config, the TV in the corner notices
# ==========================================================================
def test_a_dashboard_edit_reaches_a_running_tv(tmp_path, monkeypatch):
    import shutil
    import tempfile
    import time

    from retrobox.input.web import WebBackend

    # A short socket path: the real box uses /run/user/<uid>, and macOS caps
    # AF_UNIX paths well below what pytest's tmp_path produces.
    short = tempfile.mkdtemp(prefix="rb")
    monkeypatch.setenv("RETROBOX_STATUS_PATH", f"{short}/status.json")
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", f"{short}/c.sock")
    pytest.importorskip("flask")
    from retrobox.webui import create_app

    root = tmp_path / "media"
    root.mkdir()
    for name in ("sitcoms", "movies"):
        make_show(root, name, 3)
    body = (
        f"media_root: {root}\nstart_offset: 0\nshuffle_seed: 7\n"
        f"boot_splash: false\npower_off_command: []\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
        f'  - number: 5\n    name: "Movies"\n    path: {root / "movies"}\n'
    )
    app, player, cfg = build_from_file(tmp_path, body)

    backend = WebBackend()
    manager = InputManager([backend])
    app.input = manager
    app.start()
    try:
        for _ in range(50):
            if backend.socket_path.exists():
                break
            time.sleep(0.02)
        assert backend.socket_path.exists(), "the TV never opened its socket"

        client = create_app(str(cfg)).test_client()
        res = client.patch("/api/channels/5", json={"name": "Late Movies"})
        assert res.status_code == 200, res.get_json()
        assert res.get_json()["applied"] is True, "the TV was not told"

        for _ in range(50):
            app.step()
            if app.lineup.current.name or "Late Movies" in [c.name for c in app.lineup]:
                break
            time.sleep(0.02)
        assert [c.name for c in app.lineup] == ["Sitcoms", "Late Movies"]
    finally:
        manager.stop()
        shutil.rmtree(short, ignore_errors=True)
