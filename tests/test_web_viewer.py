"""The address a customer actually types, and what they get.

    http://retrobox.local        what is on TV right now
    http://retrobox.local/dash   the management console

The viewer page is read-only on purpose: it is what you leave open on a phone
on the arm of the sofa, and nothing on it should be able to change the
channel by accident.
"""

import json

import pytest

from retrobox import status as status_mod
from tests.helpers import make_show

flask = pytest.importorskip("flask")
from retrobox.webui import create_app, main  # noqa: E402


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("RETROBOX_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", str(tmp_path / "c.sock"))


@pytest.fixture
def client(tmp_path, runtime):
    make_show(tmp_path, "sitcoms", 2)
    make_show(tmp_path, "movies", 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'media_root: {tmp_path}\n'
        f'channels:\n'
        f'  - number: 2\n    name: "Sitcoms"\n    path: {tmp_path / "sitcoms"}\n'
        f'  - number: 5\n    name: "Movies"\n    path: {tmp_path / "movies"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app.test_client()


def on_air(**extra):
    snapshot = {
        "version": "1.0.0",
        "channel": {"number": 2, "name": "Sitcoms"},
        "now_playing": "Sitcoms Ep01",
        "position": 300.0,
        "duration": 1320.0,
        "off_air": False,
        "standby": False,
        "lineup": [
            {"number": 2, "name": "Sitcoms", "off_air": False,
             "now_playing": "Sitcoms Ep01", "current": True},
            {"number": 5, "name": "Movies", "off_air": False,
             "now_playing": "", "current": False},
        ],
    }
    snapshot.update(extra)
    status_mod.write_status(snapshot)


# ==========================================================================
# The split
# ==========================================================================
def test_the_front_page_is_the_viewer(client):
    body = client.get("/").get_data(as_text=True)
    assert "JV PROJECTS" in body
    assert "NOW PLAYING" in body.upper()
    assert "/dash" in body, "there must be a way through to the console"


def test_the_console_moved_to_dash(client):
    body = client.get("/dash").get_data(as_text=True)
    assert "JV PROJECTS" in body
    assert 'data-tab="channels"' in body, "this should be the management console"


def test_the_two_pages_are_not_the_same_page(client):
    assert client.get("/").get_data() != client.get("/dash").get_data()


def test_the_viewer_has_no_way_to_change_anything(client):
    # Read-only by design: this is the page left open on the sofa arm.
    body = client.get("/").get_data(as_text=True)
    for danger in ("/api/tune", "/api/shutdown", "/api/power", "/api/volume",
                   "/api/mute", "/api/channels", "/api/settings", "/api/uploads"):
        assert danger not in body, f"the viewer can reach {danger}"
    assert "method:'POST'" not in body.replace(" ", "")


def test_both_pages_are_self_contained(client):
    for path in ("/", "/dash"):
        body = client.get(path).get_data(as_text=True)
        assert "http://" not in body.replace("http://www.w3.org", ""), path
        assert "https://" not in body, path
        assert "<script src=" not in body and "@import" not in body, path


# ==========================================================================
# Every existing API route is exactly where it was
# ==========================================================================
def test_every_api_route_still_answers(client, runtime, monkeypatch):
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    on_air()
    for path in ("/api/status", "/api/channels", "/api/settings", "/api/uploads",
                 "/api/media/2"):
        assert client.get(path).status_code == 200, path
    for path in ("/api/tune/5", "/api/volume/up", "/api/mute", "/api/power",
                 "/api/reload"):
        assert client.post(path).status_code == 200, path
    assert client.post("/api/shutdown?confirm=yes").status_code == 200


# ==========================================================================
# What the viewer reads
# ==========================================================================
def test_now_reports_what_is_on(client, runtime):
    on_air()
    data = client.get("/api/now").get_json()
    assert data["online"] is True
    assert data["channel"]["number"] == 2
    assert data["now_playing"] == "Sitcoms Ep01"
    assert data["position"] == 300.0 and data["duration"] == 1320.0
    assert [c["number"] for c in data["lineup"]] == [2, 5]


def test_now_says_so_when_the_tv_is_not_running(client, runtime):
    data = client.get("/api/now").get_json()
    assert data["online"] is False
    assert data["lineup"] == []


def test_now_falls_back_to_the_config_lineup_when_the_tv_is_off(client, runtime):
    # Nothing is playing, but the box still knows what channels exist.
    data = client.get("/api/now").get_json()
    assert data["channels_configured"] == [
        {"number": 2, "name": "Sitcoms"}, {"number": 5, "name": "Movies"},
    ]


def test_a_stale_snapshot_is_marked_stale(client, runtime, monkeypatch):
    import os
    import time

    on_air()
    old = time.time() - 600
    os.utime(status_mod.status_path(), (old, old))

    data = client.get("/api/now").get_json()
    assert data["stale"] is True
    assert data["online"] is False, "a snapshot from ten minutes ago is not 'now'"


def test_a_fresh_snapshot_is_not_stale(client, runtime):
    on_air()
    data = client.get("/api/now").get_json()
    assert data["stale"] is False and data["online"] is True


@pytest.mark.parametrize(
    "raw",
    [
        "{ truncated",
        "null",
        "[]",
        '"a string"',
        "123",
        '{"channel": "not an object", "lineup": "not a list"}',
        '{"channel": {"number": "two"}, "position": "soon", "duration": []}',
        '{"lineup": [1, 2, 3]}',
        '{"lineup": [{"number": null}]}',
        "",
    ],
)
def test_a_malformed_snapshot_never_breaks_the_page(client, runtime, raw):
    path = status_mod.status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw)

    assert client.get("/").status_code == 200
    api = client.get("/api/now")
    assert api.status_code == 200
    body = api.get_json()
    assert isinstance(body["online"], bool)

    # Not just "it is a list" - the page renders these rows straight onto the
    # screen, so a row that is a bare number or has no channel number would
    # come out as a line of "undefined" in front of the customer.
    assert isinstance(body["lineup"], list)
    for row in body["lineup"]:
        assert isinstance(row, dict), row
        assert isinstance(row["number"], int) and not isinstance(row["number"], bool)
        assert isinstance(row["name"], str)
        assert isinstance(row["off_air"], bool)
        assert isinstance(row["now_playing"], str)

    assert body["position"] is None or isinstance(body["position"], (int, float))
    assert body["duration"] is None or isinstance(body["duration"], (int, float))
    assert isinstance(body["channel"]["name"], str)
    assert body["channel"]["number"] is None or isinstance(body["channel"]["number"], int)


def test_an_unreadable_config_does_not_break_the_viewer(tmp_path, runtime):
    app = create_app(str(tmp_path / "nope.yaml"))
    app.config.update(TESTING=True)
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/now").status_code == 200
    assert client.get("/api/now").get_json()["channels_configured"] == []


# ==========================================================================
# The port
# ==========================================================================
def test_the_default_port_is_80(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "retrobox.webui.create_app",
        lambda cfg: type("A", (), {"run": lambda self, **kw: seen.update(kw)})(),
    )
    main([])
    assert seen["port"] == 80


def test_the_port_is_still_overridable(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "retrobox.webui.create_app",
        lambda cfg: type("A", (), {"run": lambda self, **kw: seen.update(kw)})(),
    )
    main(["--port", "8080"])
    assert seen["port"] == 8080


def test_a_port_it_cannot_bind_falls_back_rather_than_dying(monkeypatch, capsys):
    # If the CAP_NET_BIND_SERVICE capability is not in place on some distro,
    # a box with no dashboard at all is a far worse outcome than one on 8080.
    attempts = []

    class Refuses:
        def run(self, **kw):
            attempts.append(kw["port"])
            if kw["port"] == 80:
                raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("retrobox.webui.create_app", lambda cfg: Refuses())
    assert main([]) == 0
    assert attempts == [80, 8080]
    assert "8080" in capsys.readouterr().out


def test_an_explicit_port_is_not_second_guessed(monkeypatch):
    # Only the default falls back. If someone asked for 9000, silently serving
    # somewhere else would be worse than failing.
    class Refuses:
        def run(self, **kw):
            raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("retrobox.webui.create_app", lambda cfg: Refuses())
    assert main(["--port", "9000"]) == 1
