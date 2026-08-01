"""The update flow as the person owning the box experiences it.

Idle, available, applying, done, rolled back, refused. Six states, and the
only one that may ever start on its own is checking.
"""

import json

import pytest

from retrobox import updates
from retrobox.updater import Persistence
from tests.helpers import make_show

flask = pytest.importorskip("flask")
from retrobox.webui import create_app  # noqa: E402


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("RETROBOX_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", str(tmp_path / "c.sock"))


@pytest.fixture
def github(monkeypatch):
    def fetch(url, *, timeout):
        fetch.calls.append(url)
        if isinstance(fetch.answer, Exception):
            raise fetch.answer
        return fetch.answer

    fetch.calls = []
    fetch.answer = "[]"
    monkeypatch.setattr(updates, "_fetch", fetch)
    return fetch


def release(tag, notes="### Added\n- something good", published="2026-05-01T00:00:00Z"):
    return {"tag_name": tag, "name": tag, "body": notes,
            "published_at": published, "draft": False, "prerelease": False}


@pytest.fixture
def box(tmp_path, runtime, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    # Belt: point the updater at a throwaway clone-shaped directory, so even
    # a stub wired up wrongly cannot reach the real checkout.
    fake_repo = tmp_path / "RetroBox"
    fake_repo.mkdir()
    monkeypatch.setattr("retrobox.webui.REPO_DIR", fake_repo)

    # Braces: and stub the commands out anyway.
    ran = []
    monkeypatch.setattr(
        "retrobox.updater._run", lambda cmd, **kw: (ran.append(list(cmd)), (0, "v1.0.3"))[1]
    )
    monkeypatch.setattr(
        "retrobox.updater.check_persistence", lambda p: Persistence(True, True, "")
    )
    monkeypatch.setattr("retrobox.updater.player_is_healthy", lambda timeout: True)
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app.test_client(), cfg, ran


# ==========================================================================
# Idle and available
# ==========================================================================
def test_an_up_to_date_box_says_so_without_nagging(box, github):
    client, _, _ = box
    github.answer = "[]"
    data = client.post("/api/updates/check").get_json()
    assert data["available"] is False
    assert data["current"]


def test_an_available_update_is_reported_with_its_notes(box, github):
    client, _, _ = box
    github.answer = json.dumps([release("v9.9.9", "### Added\n- a *new* thing")])
    data = client.post("/api/updates/check").get_json()

    assert data["available"] is True and data["latest"] == "9.9.9"
    assert data["releases"][0]["published"] == "2026-05-01"
    assert "<li>a <em>new</em> thing</li>" in data["releases"][0]["notes_html"]


def test_every_skipped_version_is_offered_not_just_the_newest(box, github):
    client, _, _ = box
    github.answer = json.dumps([
        release("v9.9.9"), release("v9.9.8"), release("v9.9.7"),
    ])
    data = client.post("/api/updates/check").get_json()
    assert [r["version"] for r in data["releases"]] == ["9.9.9", "9.9.8", "9.9.7"]


def test_the_status_route_never_starts_an_update(box, github):
    client, _, ran = box
    github.answer = json.dumps([release("v9.9.9")])
    client.get("/api/updates")
    client.get("/api/updates")
    assert ran == [], "landing on the page installed something"


def test_looking_at_the_page_does_not_even_hit_the_network(box, github):
    client, _, _ = box
    client.get("/api/updates")
    assert github.calls == [], "the page checked GitHub just by being opened"


# ==========================================================================
# Turned off
# ==========================================================================
def test_checking_disabled_makes_no_network_call_at_all(tmp_path, runtime, github, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nupdates:\n  check: false\nchannels:\n"
        f'  - number: 2\n    name: "S"\n    path: {root / "sitcoms"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    client = app.test_client()

    data = client.post("/api/updates/check").get_json()
    assert github.calls == [], "it checked despite being turned off"
    assert data["available"] is False


def test_auto_apply_is_off_so_an_available_update_is_never_installed(box, github):
    client, _, ran = box
    github.answer = json.dumps([release("v9.9.9")])
    for _ in range(3):
        client.post("/api/updates/check")
        client.get("/api/updates")
    assert ran == [], "it applied an update nobody asked for"


# ==========================================================================
# Applying
# ==========================================================================
def test_applying_needs_confirmation(box, github):
    client, _, ran = box
    github.answer = json.dumps([release("v9.9.9")])
    client.post("/api/updates/check")

    assert client.post("/api/updates/apply", json={"version": "9.9.9"}).status_code == 400
    assert ran == []


def test_applying_runs_the_update(box, github):
    client, _, ran = box
    github.answer = json.dumps([release("v9.9.9")])
    client.post("/api/updates/check")

    res = client.post("/api/updates/apply?confirm=yes", json={"version": "9.9.9"})
    assert res.status_code == 200, res.get_json()
    assert any("fetch" in " ".join(c) for c in ran)
    assert any("v9.9.9" in " ".join(c) for c in ran)


@pytest.mark.parametrize(
    "version",
    ["main", "HEAD", "origin/main", "", "; rm -rf /", "../../etc",
     "https://evil.example/x.git", "v", None, "1.0.0 --upload-pack=evil"],
)
def test_a_version_that_is_not_a_version_is_refused(box, github, version):
    client, _, ran = box
    res = client.post("/api/updates/apply?confirm=yes", json={"version": version})
    assert res.status_code == 400, version
    assert ran == [], f"{version!r} reached git"


@pytest.mark.parametrize("field", ["url", "repo", "repository", "ref", "source", "remote"])
def test_no_request_field_can_redirect_where_the_update_comes_from(box, github, field):
    # The dashboard has no authentication. If this accepted an address, anyone
    # on the LAN could make the box fetch and run their code.
    client, _, ran = box
    github.answer = json.dumps([release("v9.9.9")])
    client.post("/api/updates/check")

    client.post("/api/updates/apply?confirm=yes",
                json={"version": "9.9.9", field: "https://evil.example/x.git"})
    joined = " ".join(" ".join(c) for c in ran)
    assert "evil.example" not in joined
    assert updates.REPOSITORY not in joined or "git fetch" in joined


def test_an_unknown_version_is_not_installed(box, github):
    # Only something the releases API actually offered.
    client, _, ran = box
    github.answer = json.dumps([release("v9.9.9")])
    client.post("/api/updates/check")

    res = client.post("/api/updates/apply?confirm=yes", json={"version": "4.5.6"})
    assert res.status_code == 400
    assert ran == []


# ==========================================================================
# Refused before starting
# ==========================================================================
def test_a_read_only_root_refuses_and_says_so(box, github, monkeypatch):
    client, _, ran = box
    monkeypatch.setattr(
        "retrobox.updater.check_persistence",
        lambda p: Persistence(True, False, "this box runs with a read-only root (overlayroot)"),
    )
    github.answer = json.dumps([release("v9.9.9")])
    client.post("/api/updates/check")

    res = client.post("/api/updates/apply?confirm=yes", json={"version": "9.9.9"})
    assert res.status_code == 409
    assert "overlayroot" in res.get_json()["error"]
    assert ran == [], "it started changing things on a filesystem that forgets"


# ==========================================================================
# Rolling back
# ==========================================================================
def test_a_failed_health_check_rolls_back_and_says_what_happened(box, github, monkeypatch):
    client, _, ran = box
    monkeypatch.setattr("retrobox.updater.player_is_healthy", lambda timeout: False)
    github.answer = json.dumps([release("v9.9.9")])
    client.post("/api/updates/check")

    res = client.post("/api/updates/apply?confirm=yes", json={"version": "9.9.9"})
    assert res.status_code == 500
    state = client.get("/api/updates").get_json()["progress"]
    assert state["phase"] == "rolled_back"
    assert "nothing was lost" in state["message"].lower()


def test_the_manual_rollback_button_needs_confirming(box, github):
    client, _, ran = box
    github.answer = json.dumps([release("v9.9.9")])
    client.post("/api/updates/check")
    client.post("/api/updates/apply?confirm=yes", json={"version": "9.9.9"})
    ran.clear()

    assert client.post("/api/updates/rollback").status_code == 400
    assert ran == []
    assert client.post("/api/updates/rollback?confirm=yes").status_code == 200
    assert any("reset" in " ".join(c) for c in ran)


def test_there_is_nothing_to_roll_back_to_on_a_fresh_box(box):
    client, _, _ = box
    assert client.post("/api/updates/rollback?confirm=yes").status_code == 400


# ==========================================================================
# Progress survives a reload
# ==========================================================================
def test_progress_is_readable_after_the_page_is_reloaded(box, github):
    client, cfg, _ = box
    github.answer = json.dumps([release("v9.9.9")])
    client.post("/api/updates/check")
    client.post("/api/updates/apply?confirm=yes", json={"version": "9.9.9"})

    # A brand new app object, as a reloaded page (or a restarted dashboard)
    # would produce. The progress has to come off the disk.
    fresh = create_app(str(cfg))
    fresh.config.update(TESTING=True)
    progress = fresh.test_client().get("/api/updates").get_json()["progress"]
    # "probation", not "success". This line used to say success and that was
    # the bug it took a fleet to notice: the box declared an update finished
    # while it was still switched on, so the machinery that puts the old
    # version back after a few bad start-ups could never run. What this test is
    # actually about - the progress coming off the disk rather than out of the
    # app object - is unchanged.
    assert progress["phase"] == "probation"
    assert progress["to_version"] == "9.9.9"


def test_the_page_is_told_the_named_stage(box, github):
    client, _, _ = box
    github.answer = json.dumps([release("v9.9.9")])
    client.post("/api/updates/check")
    client.post("/api/updates/apply?confirm=yes", json={"version": "9.9.9"})
    progress = client.get("/api/updates").get_json()["progress"]
    assert progress["stage"] == "done"
    assert progress["message"]


def test_the_panel_says_something_after_an_update_that_is_still_on_trial(box):
    """An update now finishes in "probation", and the panel has to know that.

    The page walks a chain of outcomes - rolled back, failed, success - and
    anything not in it falls through and renders nothing at all. So the update
    a customer just installed would leave the panel silent about it, which
    reads exactly like an update that did not happen. There is no JavaScript
    runner in this suite, so this is the page's own text.
    """
    client, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    assert "progress.phase === 'probation'" in page, (
        "the update panel has no branch for an update that is still on trial"
    )


# ==========================================================================
# The start-up check has exactly one caller
# ==========================================================================
def test_creating_the_app_does_not_run_the_start_up_check_itself(tmp_path, runtime, monkeypatch):
    """One caller, and it is retrobox/webservice.py.

    The boot check counts how many times this box has started since an update
    and gives up on the new version after three bad ones. A second caller
    counts every start twice, so a good version gets thrown away after two
    bad start-ups instead of three - and throwing away a good version means
    reinstalling on a box nobody can reach. If the call is ever wanted here
    instead, it has to be removed from webservice.py in the same change.
    """
    from retrobox import updater as updater_module

    def refuse(*args, **kwargs):
        raise AssertionError("create_app ran the boot check as well as webservice")

    monkeypatch.setattr(updater_module, "check_at_boot", refuse)
    monkeypatch.setattr(updater_module, "start_boot_check", refuse)

    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nchannels:\n"
        f'  - number: 2\n    name: "S"\n    path: {root / "sitcoms"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    assert app.test_client().get("/api/updates").status_code == 200


def test_a_ruined_update_record_still_gives_the_customer_a_dashboard(tmp_path, runtime):
    """Half a JSON file is what a power cut during an update can leave.

    A box in that state is precisely the box somebody needs the dashboard for.
    It comes up, the update panel says nothing is happening, and the customer
    can update again from there.
    """
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nchannels:\n"
        f'  - number: 2\n    name: "S"\n    path: {root / "sitcoms"}\n'
    )
    from retrobox.webui import UPDATE_STATE_NAME

    (tmp_path / UPDATE_STATE_NAME).write_text('{"phase": "runn')

    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    client = app.test_client()
    assert client.get("/dash").status_code == 200
    assert client.get("/api/updates").get_json()["progress"]["phase"] == "idle"
