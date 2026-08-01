import json
import pathlib
import time

import pytest

from retrobox import status as status_mod
from retrobox.actions import Action, InputEvent
from retrobox.config import config_from_dict
from retrobox.input.manager import InputManager
from retrobox.input.web import WebBackend, parse_command
from tests.helpers import make_show
from tests.test_app import build_app, send

flask = pytest.importorskip("flask")
from retrobox.webui import channel_rows, create_app  # noqa: E402


@pytest.fixture
def runtime(monkeypatch):
    """Point the status file and control socket at a throwaway directory.

    Deliberately NOT pytest's tmp_path: a Unix socket path is capped near 104
    bytes (much shorter on macOS than Linux), and pytest's nested temp paths
    blow straight past it. The real box uses /run/user/<uid>/retrobox, which is
    short, so this is a test-harness constraint rather than a product one.
    """
    import shutil
    import tempfile

    short = tempfile.mkdtemp(prefix="rb")
    monkeypatch.setenv("RETROBOX_STATUS_PATH", f"{short}/status.json")
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", f"{short}/c.sock")
    yield pathlib.Path(short)
    shutil.rmtree(short, ignore_errors=True)


# ==========================================================================
# Command parsing — every command maps onto an EXISTING action
# ==========================================================================
@pytest.mark.parametrize(
    "line,expected",
    [
        ("volume_up", [Action.VOLUME_UP]),
        ("volume_down", [Action.VOLUME_DOWN]),
        ("mute", [Action.MUTE]),
        ("channel_up", [Action.CHANNEL_UP]),
        ("power", [Action.POWER]),
        ("shutdown", [Action.SHUTDOWN]),
        ("guide", [Action.GUIDE]),
        ("menu", [Action.MENU]),
    ],
)
def test_simple_commands_map_to_actions(line, expected):
    assert [e.action for e in parse_command(line)] == expected


def test_channel_becomes_the_same_keypresses_a_remote_would_send():
    events = parse_command("channel 12")
    assert [(e.action, e.value) for e in events] == [
        (Action.DIGIT, 1), (Action.DIGIT, 2), (Action.ENTER, None),
    ]


def test_channel_command_is_case_and_space_tolerant():
    assert [e.action for e in parse_command("  VOLUME_UP  ")] == [Action.VOLUME_UP]


@pytest.mark.parametrize("line", ["", "   ", "nonsense", "channel", "channel abc",
                                  "channel 99999", "rm -rf /"])
def test_junk_is_ignored(line):
    assert parse_command(line) == []


# ==========================================================================
# The backend really listens, and events land on the shared queue
# ==========================================================================
def test_backend_delivers_events_over_the_socket(runtime):
    from queue import Queue

    backend = WebBackend()
    queue: "Queue" = Queue()
    backend.start(queue)
    try:
        for _ in range(50):                       # wait for bind()
            if backend.socket_path.exists():
                break
            time.sleep(0.02)
        assert backend.socket_path.exists(), "backend never bound its socket"

        assert status_mod.send_command("channel 3") is True
        events = [queue.get(timeout=2) for _ in range(2)]
        assert [(e.action, e.value) for e in events] == [
            (Action.DIGIT, 3), (Action.ENTER, None),
        ]
    finally:
        backend.stop()


def test_backend_cleans_up_its_socket(runtime):
    from queue import Queue

    backend = WebBackend()
    backend.start(Queue())
    for _ in range(50):
        if backend.socket_path.exists():
            break
        time.sleep(0.02)
    backend.stop()
    assert not backend.socket_path.exists()


def test_send_command_reports_failure_when_nothing_is_listening(runtime):
    assert status_mod.send_command("volume_up") is False


def test_backend_survives_a_stale_socket_file(runtime):
    from queue import Queue

    stale = status_mod.control_socket_path()
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("not really a socket")

    backend = WebBackend()
    backend.start(Queue())
    try:
        for _ in range(50):
            if backend.socket_path.is_socket():
                break
            time.sleep(0.02)
        assert backend.socket_path.is_socket()
    finally:
        backend.stop()


# ==========================================================================
# Status file
# ==========================================================================
def test_status_round_trips(runtime):
    assert status_mod.write_status({"volume": 70}) is True
    data = status_mod.read_status()
    assert data["volume"] == 70
    assert data["schema"] == status_mod.SCHEMA_VERSION


def test_missing_status_reads_as_empty(runtime):
    assert status_mod.read_status() == {}


def test_corrupt_status_reads_as_empty(runtime):
    status_mod.status_path().parent.mkdir(parents=True, exist_ok=True)
    status_mod.status_path().write_text("{ truncated")
    assert status_mod.read_status() == {}


def test_app_writes_a_status_snapshot(tmp_path, runtime):
    app, player, clock = build_app(tmp_path)
    player.hwdec = "vaapi"
    app.start()
    app.step()

    data = json.loads(status_mod.status_path().read_text())
    assert data["channel"]["number"] == 2
    assert data["channel"]["name"] == "Adult Swim"
    assert data["volume"] == 70
    assert data["hwdec"] == "vaapi"
    assert data["channel_count"] == 3
    assert data["standby"] is False


def test_status_tracks_the_box(tmp_path, runtime):
    app, player, clock = build_app(tmp_path)
    app.start()
    send(app, Action.CHANNEL_UP)
    send(app, Action.MUTE)
    clock.advance(5)
    app.step()

    data = status_mod.read_status()
    assert data["channel"]["number"] == 3
    assert data["muted"] is True
    assert data["uptime_seconds"] >= 5


# ==========================================================================
# Flask routes
# ==========================================================================
@pytest.fixture
def client(tmp_path, runtime):
    make_show(tmp_path, "sitcoms", 3)
    make_show(tmp_path, "movies", 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'channels:\n'
        f'  - number: 2\n    name: "Sitcoms"\n    path: {tmp_path / "sitcoms"}\n'
        f'  - number: 5\n    name: "Movies"\n    path: {tmp_path / "movies"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app.test_client()


def test_both_pages_serve(client):
    for path in ("/", "/dash"):
        res = client.get(path)
        assert res.status_code == 200, path
        body = res.get_data(as_text=True)
        assert "RETRO BOX" in body, path
        assert "#4DFF5A" in body, f"{path} is styled with the on-screen green"


def test_status_route_reports_offline_when_the_tv_is_not_running(client):
    data = client.get("/api/status").get_json()
    assert data["online"] is False


def test_status_route_passes_the_snapshot_through(client, runtime):
    status_mod.write_status({"volume": 42, "channel": {"number": 5, "name": "Movies"}})
    data = client.get("/api/status").get_json()
    assert data["online"] is True
    assert data["volume"] == 42
    assert data["channel"]["name"] == "Movies"


def test_channel_list_comes_from_the_config(client):
    rows = client.get("/api/channels").get_json()["channels"]
    assert [(r["number"], r["name"]) for r in rows] == [(2, "Sitcoms"), (5, "Movies")]
    assert [r["label"] for r in rows] == ["CH 02", "CH 05"]


def test_channel_list_marks_what_is_playing(client, runtime):
    status_mod.write_status({"channel": {"number": 5, "name": "Movies"}})
    rows = client.get("/api/channels").get_json()["channels"]
    assert [r["current"] for r in rows] == [False, True]


def test_tune_sends_a_command(client, runtime, monkeypatch):
    sent = []
    monkeypatch.setattr(status_mod, "send_command", lambda c, **k: sent.append(c) or True)
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: sent.append(c) or True)
    res = client.post("/api/tune/5")
    assert res.status_code == 200 and res.get_json()["ok"] is True
    assert sent == ["channel 5"]


def test_tune_rejects_a_silly_channel(client):
    # Flask's <int:> converter happily matches 99999, so the guard in the view
    # is what has to reject it.
    res = client.post("/api/tune/99999")
    assert res.status_code == 400
    assert "out of range" in res.get_json()["error"]


def test_volume_and_mute_dispatch(client, monkeypatch):
    sent = []
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: sent.append(c) or True)
    client.post("/api/volume/up")
    client.post("/api/volume/down")
    client.post("/api/mute")
    client.post("/api/power")
    assert sent == ["volume_up", "volume_down", "mute", "power"]


def test_bad_volume_direction_is_rejected(client):
    res = client.post("/api/volume/sideways")
    assert res.status_code == 400


def test_shutdown_needs_confirmation(client, monkeypatch):
    sent = []
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: sent.append(c) or True)
    assert client.post("/api/shutdown").status_code == 400
    assert sent == []
    assert client.post("/api/shutdown?confirm=yes").status_code == 200
    assert sent == ["shutdown"]


def test_routes_report_503_when_the_tv_is_not_listening(client, runtime):
    # No backend bound, so send_command genuinely fails.
    res = client.post("/api/volume/up")
    assert res.status_code == 503
    assert res.get_json()["ok"] is False


def test_dashboard_copes_with_an_unreadable_config(tmp_path, runtime):
    app = create_app(str(tmp_path / "does-not-exist.yaml"))
    app.config.update(TESTING=True)
    res = app.test_client().get("/api/channels")
    assert res.status_code == 200
    assert res.get_json()["channels"] == []


# ==========================================================================
# End-to-end: browser click -> socket -> the same state machine
# ==========================================================================
def test_a_web_command_changes_the_channel_for_real(tmp_path, runtime):
    app, player, _ = build_app(tmp_path)
    backend = WebBackend()
    manager = InputManager([backend])
    app.input = manager
    app.start()
    try:
        for _ in range(50):
            if backend.socket_path.exists():
                break
            time.sleep(0.02)

        assert status_mod.send_command("channel 4") is True
        for _ in range(50):                      # let the events arrive
            app.step()
            if app.lineup.current.number == 4:
                break
            time.sleep(0.02)
        assert app.lineup.current.number == 4
    finally:
        manager.stop()


def test_web_shutdown_powers_the_box_off(tmp_path, runtime):
    app, player, _ = build_app(tmp_path)
    app.start()
    # Same event the socket would deliver.
    for event in parse_command("shutdown"):
        app.handle_event(event)
    assert app.powered_off is True


# ==========================================================================
# The on-screen menu must be completely unaffected by any of this
# ==========================================================================
def test_on_screen_menu_still_behaves(tmp_path, runtime, monkeypatch):
    from retrobox import hwdetect

    monkeypatch.setattr(hwdetect, "detect_audio", lambda: [])
    app, player, _ = build_app(tmp_path)
    app.start()

    send(app, Action.MENU)
    assert app._menu is not None and player.paused is True
    send(app, Action.CHANNEL_DOWN)
    assert "> Volume" in player.overlays[7]
    send(app, Action.MENU)
    assert app._menu is None and player.paused is False


def test_status_writing_does_not_disturb_the_menu(tmp_path, runtime, monkeypatch):
    from retrobox import hwdetect

    monkeypatch.setattr(hwdetect, "detect_audio", lambda: [])
    app, player, clock = build_app(tmp_path)
    app.start()
    send(app, Action.MENU)
    before = player.overlays[7]

    for _ in range(5):                            # several status writes
        clock.advance(3)
        app.step()

    assert app._menu is not None, "the menu stayed open"
    assert player.paused is True, "playback stayed paused"
    assert player.overlays[7] == before, "the menu was not redrawn under it"
    assert status_mod.read_status()["menu_open"] is True


# ==========================================================================
# What the now-playing page needs in the snapshot
# ==========================================================================
def test_status_carries_the_playback_position(tmp_path, runtime):
    app, player, clock = build_app(tmp_path)
    app.start()
    player.time_pos = 431.5
    app.step()

    data = status_mod.read_status()
    assert data["position"] == 431.5


def test_status_carries_the_whole_lineup(tmp_path, runtime):
    # The viewer page shows what is on the other channels. It reads the status
    # file like everything else - the web process never builds a lineup itself.
    app, player, clock = build_app(tmp_path)
    app.start()
    app.step()

    lineup = status_mod.read_status()["lineup"]
    assert [(c["number"], c["name"]) for c in lineup] == [
        (2, "Adult Swim"), (3, "MTV Classic"), (4, "Late Night"),
    ]
    assert all("off_air" in c for c in lineup)


def test_duration_is_only_reported_when_it_is_already_known(tmp_path, runtime, monkeypatch):
    # Nothing in the two-second status loop is allowed to fork ffprobe.
    from retrobox import probe

    def explode(*a, **k):
        raise AssertionError("the status snapshot ran ffprobe")

    monkeypatch.setattr(probe, "_run_probe", explode)
    app, player, clock = build_app(tmp_path)
    app.start()
    app.step()
    assert status_mod.read_status()["duration"] is None

    monkeypatch.setattr(probe, "cached_media", lambda p: probe.MediaInfo(1320.0, True))
    clock.advance(5)
    app.step()
    assert status_mod.read_status()["duration"] == 1320.0


# ==========================================================================
# The input test needs the presses to reach the dashboard
# ==========================================================================
def test_status_carries_recent_button_presses(tmp_path, runtime):
    app, player, clock = build_app(tmp_path)
    app.start()
    app.input.put(InputEvent(Action.CHANNEL_UP))
    app.step()

    presses = status_mod.read_status()["input"]["recent"]
    assert presses[-1]["action"] == "CHANNEL_UP"
    assert presses[-1]["backend"] == "dashboard"


def test_status_says_which_input_backends_are_live(tmp_path, runtime):
    app, player, clock = build_app(tmp_path)
    app.start()
    app.step()
    assert status_mod.read_status()["input"]["backends"] == []


def test_a_press_is_published_without_waiting_for_the_next_tick(tmp_path, runtime):
    # "Press a button, see it light up" is the whole point of the input test.
    # Waiting out the two-second status interval makes it feel broken.
    app, player, clock = build_app(tmp_path)
    app.start()
    app.step()
    before = len(status_mod.read_status()["input"]["recent"])

    app.input.put(InputEvent(Action.MUTE))
    app.step()                                    # no clock.advance
    after = status_mod.read_status()["input"]["recent"]
    assert len(after) == before + 1, "the press had to wait for the status timer"
