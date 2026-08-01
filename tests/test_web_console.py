"""The dashboard as a management console: channels, media and settings.

Everything here runs against a real config file on disk and asserts on what
came back out of it, because the whole point of these routes is that they
change a file the box boots from.
"""

import io
import os
import pathlib
import time
from types import SimpleNamespace

import pytest

from retrobox.channel import scan_episodes
from retrobox.config import DEFAULT_VIDEO_EXTENSIONS, load_config
from tests.helpers import make_show

flask = pytest.importorskip("flask")
from retrobox.webui import create_app  # noqa: E402


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("RETROBOX_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", str(tmp_path / "c.sock"))


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have gone down the control socket."""
    out = []
    monkeypatch.setattr(
        "retrobox.webui.send_command", lambda c, **k: out.append(c) or True
    )
    return out


@pytest.fixture
def box(tmp_path, runtime):
    """A config file with a media root and two channels, plus a test client."""
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 3)
    make_show(root, "movies", 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\n"
        f"channels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
        f'  - number: 5\n    name: "Movies"\n    path: {root / "movies"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app.test_client(), cfg, root


def _channels(cfg):
    return [(c.number, c.name) for c in load_config(cfg).channels]


# ==========================================================================
# Channels — create, rename, renumber, delete
# ==========================================================================
def test_creating_a_channel_lands_in_the_config(box, sent):
    client, cfg, root = box
    make_show(root, "cartoons", 2)

    res = client.post("/api/channels", json={
        "name": "Cartoons", "path": str(root / "cartoons"), "number": 7,
    })
    assert res.status_code == 201, res.get_json()
    assert (7, "Cartoons") in _channels(cfg)


def test_a_new_channel_gets_the_next_free_number_by_itself(box, sent):
    client, cfg, root = box
    make_show(root, "cartoons", 2)
    res = client.post("/api/channels", json={
        "name": "Cartoons", "path": str(root / "cartoons"),
    })
    assert res.status_code == 201
    assert res.get_json()["channel"]["number"] == 6


def test_renaming_a_channel(box, sent):
    client, cfg, _ = box
    res = client.patch("/api/channels/2", json={"name": "Classic Sitcoms"})
    assert res.status_code == 200, res.get_json()
    assert _channels(cfg) == [(2, "Classic Sitcoms"), (5, "Movies")]


def test_renumbering_a_channel(box, sent):
    client, cfg, _ = box
    assert client.patch("/api/channels/5", json={"number": 9}).status_code == 200
    assert _channels(cfg) == [(2, "Sitcoms"), (9, "Movies")]


def test_repointing_a_channel_at_another_folder(box, sent):
    client, cfg, root = box
    make_show(root, "cartoons", 2)
    res = client.patch("/api/channels/2", json={"path": str(root / "cartoons")})
    assert res.status_code == 200, res.get_json()
    assert load_config(cfg).channels[0].path == root / "cartoons"


def test_deleting_a_channel_needs_confirmation(box, sent):
    client, cfg, _ = box
    assert client.delete("/api/channels/5").status_code == 400
    assert _channels(cfg) == [(2, "Sitcoms"), (5, "Movies")]

    assert client.delete("/api/channels/5?confirm=yes").status_code == 200
    assert _channels(cfg) == [(2, "Sitcoms")]


def test_deleting_a_channel_leaves_every_video_file_alone(box, sent):
    client, cfg, root = box
    before = sorted(p.name for p in (root / "movies").iterdir())
    assert before, "the fixture should have made some episodes"

    client.delete("/api/channels/5?confirm=yes")

    assert (root / "movies").is_dir(), "the folder was deleted"
    assert sorted(p.name for p in (root / "movies").iterdir()) == before


def test_the_last_channel_cannot_be_deleted(box, sent):
    # A config with no channels does not load, so the box would not boot.
    client, cfg, _ = box
    client.delete("/api/channels/5?confirm=yes")
    res = client.delete("/api/channels/2?confirm=yes")
    assert res.status_code == 400
    assert _channels(cfg) == [(2, "Sitcoms")]


def test_editing_a_channel_that_does_not_exist_is_a_404(box, sent):
    client, _, _ = box
    assert client.patch("/api/channels/77", json={"name": "Nope"}).status_code == 404
    assert client.delete("/api/channels/77?confirm=yes").status_code == 404


# -- validation ------------------------------------------------------------
def test_a_duplicate_channel_number_is_refused(box, sent):
    client, cfg, _ = box
    res = client.patch("/api/channels/5", json={"number": 2})
    assert res.status_code == 400
    assert "already" in res.get_json()["error"].lower()
    assert _channels(cfg) == [(2, "Sitcoms"), (5, "Movies")]


def test_a_duplicate_number_on_create_is_refused(box, sent):
    client, cfg, root = box
    make_show(root, "cartoons", 1)
    res = client.post("/api/channels", json={
        "name": "Cartoons", "path": str(root / "cartoons"), "number": 2,
    })
    assert res.status_code == 400
    assert _channels(cfg) == [(2, "Sitcoms"), (5, "Movies")]


@pytest.mark.parametrize("number", [-1, 1000, 99999])
def test_channel_numbers_are_bounded(box, sent, number):
    client, cfg, root = box
    make_show(root, "cartoons", 1)
    res = client.post("/api/channels", json={
        "name": "Cartoons", "path": str(root / "cartoons"), "number": number,
    })
    assert res.status_code == 400


@pytest.mark.parametrize("name", ["", "   ", "x" * 200, "bad\x00name", "line\nbreak"])
def test_channel_names_are_bounded(box, sent, name):
    client, cfg, _ = box
    assert client.patch("/api/channels/2", json={"name": name}).status_code == 400
    assert _channels(cfg) == [(2, "Sitcoms"), (5, "Movies")]


def test_a_channel_path_outside_the_media_root_is_refused(box, sent, tmp_path):
    client, cfg, _ = box
    outside = tmp_path / "elsewhere"
    make_show(outside, "secret", 1)
    res = client.post("/api/channels", json={
        "name": "Secret", "path": str(outside / "secret"),
    })
    assert res.status_code == 400
    assert "media_root" in res.get_json()["error"]


def test_a_channel_path_that_climbs_out_is_refused(box, sent):
    client, cfg, root = box
    res = client.post("/api/channels", json={
        "name": "Escape", "path": str(root / "sitcoms" / ".." / ".." / ".."),
    })
    assert res.status_code == 400


def test_a_channel_path_that_does_not_exist_is_refused(box, sent):
    client, cfg, root = box
    res = client.post("/api/channels", json={
        "name": "Ghost", "path": str(root / "not_there"),
    })
    assert res.status_code == 400
    assert "exist" in res.get_json()["error"].lower()


# -- reorder ---------------------------------------------------------------
def test_reordering_renumbers_the_whole_lineup(box, sent):
    client, cfg, _ = box
    res = client.post("/api/channels/reorder", json={"order": [5, 2]})
    assert res.status_code == 200, res.get_json()
    assert _channels(cfg) == [(2, "Movies"), (3, "Sitcoms")]


def test_a_reorder_that_leaves_a_channel_out_is_refused(box, sent):
    client, cfg, _ = box
    res = client.post("/api/channels/reorder", json={"order": [2]})
    assert res.status_code == 400
    assert _channels(cfg) == [(2, "Sitcoms"), (5, "Movies")]


def test_a_reorder_naming_an_unknown_channel_is_refused(box, sent):
    client, cfg, _ = box
    assert client.post("/api/channels/reorder", json={"order": [2, 5, 8]}).status_code == 400
    assert _channels(cfg) == [(2, "Sitcoms"), (5, "Movies")]


# ==========================================================================
# Telling the TV
# ==========================================================================
def test_a_config_change_asks_the_running_tv_to_reload(box, sent):
    client, _, _ = box
    client.patch("/api/channels/2", json={"name": "Renamed"})
    assert sent == ["reload"]


def test_a_change_still_saves_when_the_tv_is_not_running(box, runtime):
    # No socket bound: send_command genuinely fails. The config is still
    # written - it will be picked up next start - and the user is told.
    client, cfg, _ = box
    res = client.patch("/api/channels/2", json={"name": "Renamed"})
    assert res.status_code == 200
    assert res.get_json()["applied"] is False
    assert _channels(cfg) == [(2, "Renamed"), (5, "Movies")]


# ==========================================================================
# Media — listing
# ==========================================================================
def test_listing_a_channels_files(box):
    client, _, root = box
    data = client.get("/api/media/2").get_json()
    assert sorted(f["name"] for f in data["files"]) == [
        "sitcoms_ep01.mp4", "sitcoms_ep02.mp4", "sitcoms_ep03.mp4",
    ]
    assert all("bytes" in f for f in data["files"])


def test_listing_an_unknown_channel_is_a_404(box):
    client, _, _ = box
    assert client.get("/api/media/77").status_code == 404


def test_the_listing_only_shows_video_files(box):
    client, _, root = box
    (root / "sitcoms" / "notes.txt").write_text("hello")
    (root / "sitcoms" / ".hidden.mp4").write_bytes(b"\x00")
    names = [f["name"] for f in client.get("/api/media/2").get_json()["files"]]
    assert "notes.txt" not in names
    assert ".hidden.mp4" not in names


# ==========================================================================
# Media — upload. The most dangerous surface on the box.
# ==========================================================================
def _upload(client, number, name, payload, **kw):
    return client.post(
        f"/api/media/{number}?name={name}",
        data=payload,
        content_type="application/octet-stream",
        **kw,
    )


def test_an_upload_lands_as_a_playable_episode(box, sent):
    client, _, root = box
    res = _upload(client, 2, "new_episode.mp4", b"\x00" * 2048)
    assert res.status_code == 201, res.get_json()

    landed = root / "sitcoms" / "new_episode.mp4"
    assert landed.read_bytes() == b"\x00" * 2048
    assert landed in scan_episodes(root / "sitcoms", DEFAULT_VIDEO_EXTENSIONS)


def test_an_upload_does_not_overwrite_an_existing_episode(box, sent):
    client, _, root = box
    original = (root / "sitcoms" / "sitcoms_ep01.mp4").read_bytes()
    res = _upload(client, 2, "sitcoms_ep01.mp4", b"\xff" * 100)
    assert res.status_code == 409
    assert (root / "sitcoms" / "sitcoms_ep01.mp4").read_bytes() == original


@pytest.mark.parametrize(
    "name",
    [
        "..%2f..%2f..%2fetc%2fevil.mp4",
        "..%2Fevil.mp4",
        "%2e%2e%2fevil.mp4",
        "%2fetc%2fsystemd%2fsystem%2fevil.mp4",
        "..",
        "%00.mp4",
        "evil.mp4%00.sh",
        ".bashrc.mp4",
        "~root.mp4",
    ],
)
def test_a_traversing_upload_name_is_refused(box, sent, tmp_path, name):
    client, _, root = box
    before = {p for p in tmp_path.rglob("*") if p.is_file()}

    res = _upload(client, 2, name, b"\x00" * 64)

    assert res.status_code in (400, 404), f"{name} was accepted"
    after = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before, f"{name} created a file somewhere"


@pytest.mark.parametrize(
    "name", ["evil.sh", "evil.service", "authorized_keys", "note.txt", "ep.mp4.sh"]
)
def test_an_upload_that_is_not_a_video_is_refused(box, sent, name):
    client, _, root = box
    res = _upload(client, 2, name, b"\x00" * 64)
    assert res.status_code == 400
    assert not (root / "sitcoms" / name).exists()


def test_an_upload_through_a_planted_symlink_is_refused(box, sent, tmp_path):
    # The filename is fine; what is already sitting at that name is the attack.
    client, _, root = box
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "planted.mp4"
    (root / "sitcoms" / "innocent.mp4").symlink_to(target)

    res = _upload(client, 2, "innocent.mp4", b"\x00" * 64)
    assert res.status_code in (400, 409)
    assert not target.exists(), "the upload was written through the symlink"


def _litter(folder):
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".part"))


def test_an_upload_bigger_than_the_cap_is_refused_before_it_is_written(box, sent, tmp_path):
    client, cfg, root = box
    cfg.write_text(cfg.read_text() + "web:\n  max_upload_mb: 1\n")

    res = _upload(client, 2, "huge.mp4", b"\x00" * 32,
                  environ_overrides={"CONTENT_LENGTH": str(4 * 1024 * 1024)})
    assert res.status_code == 413
    assert not (root / "sitcoms" / "huge.mp4").exists()
    assert _litter(root / "sitcoms") == [], "it started writing before checking"


def _disk(free_mb):
    return SimpleNamespace(total=10**12, used=0, free=free_mb * 1024 * 1024)


def test_an_upload_that_would_fill_the_disk_is_refused(box, sent, monkeypatch):
    # 2 GB free, 1 GB of that reserved, so a 1.5 GB upload does not fit even
    # though it is smaller than the free space.
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.shutil.disk_usage", lambda p: _disk(2048))

    res = _upload(client, 2, "big.mp4", b"\x00" * 64,
                  environ_overrides={"CONTENT_LENGTH": str(1536 * 1024 * 1024)})
    assert res.status_code == 507
    assert "space" in res.get_json()["error"].lower()
    assert not (root / "sitcoms" / "big.mp4").exists()
    assert _litter(root / "sitcoms") == []


def test_an_upload_that_does_fit_is_still_allowed(box, sent, monkeypatch):
    # The control for the test above: the reserve must not block everything.
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.shutil.disk_usage", lambda p: _disk(2048))
    assert _upload(client, 2, "small.mp4", b"\x00" * 64).status_code == 201


def test_an_upload_with_no_declared_size_is_refused(box, sent):
    client, _, root = box
    res = client.post(
        "/api/media/2?name=x.mp4",
        data=(b"\x00" * 32),
        content_type="application/octet-stream",
        headers={"Transfer-Encoding": "chunked"},
        environ_overrides={"CONTENT_LENGTH": ""},
    )
    assert res.status_code == 411
    assert list((root / "sitcoms").iterdir()) != []
    assert not (root / "sitcoms" / "x.mp4").exists()


def test_an_interrupted_upload_leaves_nothing_the_scanner_can_see(box, sent):
    # The browser goes away halfway: fewer bytes arrive than were promised.
    client, _, root = box
    folder = root / "sitcoms"
    before = sorted(p.name for p in folder.iterdir())

    res = client.post(
        "/api/media/2?name=half.mp4",
        input_stream=io.BytesIO(b"\x00" * 500),
        content_type="application/octet-stream",
        environ_overrides={"CONTENT_LENGTH": "100000"},
    )

    assert res.status_code == 400
    assert sorted(p.name for p in folder.iterdir()) == before, "a stray file was left"
    assert _litter(folder) == []
    assert not (folder / "half.mp4").exists()


def test_a_partial_upload_is_never_visible_to_the_scanner_while_it_runs(box, sent):
    # The staging name has to be invisible to the episode scanner, or a
    # half-uploaded film becomes an episode that plays as corruption.
    client, _, root = box
    folder = root / "sitcoms"
    seen = []

    original = pathlib.Path.replace

    def spy(self, target):
        seen.append(scan_episodes(folder, DEFAULT_VIDEO_EXTENSIONS))
        return original(self, target)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pathlib.Path, "replace", spy)
        _upload(client, 2, "new.mp4", b"\x00" * 4096)

    assert seen, "the upload never renamed anything into place"
    assert all(
        not str(p).endswith("new.mp4") for p in seen[0]
    ), "the file was scannable before it was complete"


def test_uploading_to_an_unknown_channel_is_a_404(box, sent):
    client, _, _ = box
    assert _upload(client, 77, "x.mp4", b"\x00").status_code == 404


# ==========================================================================
# Media — upload. What a power cut leaves behind.
#
# The wall switch is how this box is turned off, and nothing tidies up after
# it: the `except:` that deletes a half-arrived upload only runs if the
# process is still alive to run it. So the question these ask is not "is it
# cleaned up" - it is "when it is NOT cleaned up, can the box still see the
# bytes and get them back". In a channel folder it cannot: the scanner skips
# .part, the media list filters on extension, and the Settings page's spool
# total does not look there. Eight gigabytes gone, with nothing in the
# dashboard able to find it or free it.
# ==========================================================================
def _spool(root):
    from retrobox.uploads import spool_for

    return spool_for(root)


def _power_cut_mid_upload(client, mp, name="film.mp4", sent_bytes=4096):
    """An upload the box dies in the middle of.

    Fewer bytes arrive than were promised, which is what a browser going away
    looks like - and `unlink` is turned off, which is what the power going off
    looks like: none of the tidying up runs at all.
    """
    mp.setattr(pathlib.Path, "unlink", lambda self, **kw: None)
    return client.post(
        f"/api/media/2?name={name}",
        input_stream=io.BytesIO(b"\x00" * sent_bytes),
        content_type="application/octet-stream",
        environ_overrides={"CONTENT_LENGTH": str(sent_bytes * 100)},
    )


def _age_the_spool(root, hours):
    """Put every clock in the spool back, as if nobody came back for days."""
    when = time.time() - hours * 3600
    for path in _spool(root).rglob("*"):
        os.utime(path, (when, when))


def test_an_upload_in_flight_is_staged_where_the_box_can_find_it_again(box, sent):
    """Half an upload waits in the spool, not in the channel folder.

    The spool is the one place the box already knows how to account for and
    clear up. A staging file in the channel folder is invisible to every part
    of the dashboard the moment the process that made it is gone.
    """
    client, _, root = box
    folder = root / "sitcoms"
    before = sorted(p.name for p in folder.iterdir())
    seen = {}

    original = pathlib.Path.replace

    def spy(self, target):
        seen["staged_in"] = self.parent
        seen["channel_folder"] = sorted(p.name for p in folder.iterdir())
        return original(self, target)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pathlib.Path, "replace", spy)
        assert _upload(client, 2, "new.mp4", b"\x00" * 4096).status_code == 201

    assert seen, "the upload never renamed anything into place"
    assert seen["channel_folder"] == before, (
        "the bytes were staged in the channel folder, where a power cut strands them"
    )
    spool = _spool(root)
    assert spool == seen["staged_in"] or spool in seen["staged_in"].parents, (
        f"staged in {seen['staged_in']}, which the spool sweeper does not own"
    )


def test_what_a_power_cut_strands_is_counted_as_space_the_box_is_using(box, sent):
    client, _, root = box
    with pytest.MonkeyPatch.context() as mp:
        assert _power_cut_mid_upload(client, mp).status_code == 400

    assert _litter(root / "sitcoms") == [], "stranded in the channel folder"
    spool_bytes = client.get("/api/settings").get_json()["upload_spool_bytes"]
    assert spool_bytes >= 4096, (
        "the Settings page says the box is using no space, and 4 KB of a "
        "customer's disk - a whole film, on a real box - has quietly gone"
    )


def test_what_a_power_cut_strands_is_cleared_up_like_any_other_dead_upload(box, sent):
    client, _, root = box
    with pytest.MonkeyPatch.context() as mp:
        _power_cut_mid_upload(client, mp)
    assert client.get("/api/settings").get_json()["upload_spool_bytes"] >= 4096

    # A day later, nobody came back for it.
    _age_the_spool(root, hours=48)
    client.get("/api/uploads")          # the page that reclaims

    assert client.get("/api/settings").get_json()["upload_spool_bytes"] == 0


def test_an_upload_to_a_channel_outside_the_library_still_lands(tmp_path, runtime, sent):
    """A channel added by hand can point at a plugged-in drive.

    Staging in the spool means the finished file may have to cross from one
    filesystem to another to reach that folder, and a rename cannot. This is
    one filesystem, so it only proves the ordinary path still works - the
    crossing itself is the chunked uploader's, tested in tests/test_uploads.py.
    """
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 1)
    outside = tmp_path / "usb-drive" / "Cartoons"
    outside.mkdir(parents=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
        f'  - number: 3\n    name: "Cartoons"\n    path: {outside}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    client = app.test_client()

    assert _upload(client, 3, "toon.mp4", b"\x07" * 2048).status_code == 201
    assert (outside / "toon.mp4").read_bytes() == b"\x07" * 2048
    assert _litter(outside) == []


def test_a_box_with_no_media_library_can_still_be_uploaded_to(tmp_path, runtime, sent):
    """No media_root means no spool, and there is nowhere better to stage.

    That box keeps the old behaviour rather than losing the ability to upload
    at all: staging next to the destination is worse than the spool, and an
    upload button that answers "set media_root first" is worse than both.
    """
    outside = tmp_path / "shows" / "Cartoons"
    outside.mkdir(parents=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'channels:\n  - number: 3\n    name: "Cartoons"\n    path: {outside}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    client = app.test_client()

    assert _upload(client, 3, "toon.mp4", b"\x09" * 1024).status_code == 201
    assert (outside / "toon.mp4").read_bytes() == b"\x09" * 1024
    assert _litter(outside) == []


def test_an_upload_that_is_still_arriving_is_not_swept_out_from_under_it(box, sent):
    """The clock that matters is the last byte written, not the first."""
    client, _, root = box
    with pytest.MonkeyPatch.context() as mp:
        _power_cut_mid_upload(client, mp)

    client.get("/api/uploads")          # sweeps, and must take nothing
    assert client.get("/api/settings").get_json()["upload_spool_bytes"] >= 4096


# ==========================================================================
# Media — delete
# ==========================================================================
def test_deleting_a_file_needs_confirmation(box, sent):
    client, _, root = box
    target = root / "sitcoms" / "sitcoms_ep01.mp4"

    assert client.delete("/api/media/2/sitcoms_ep01.mp4").status_code == 400
    assert target.exists()

    assert client.delete("/api/media/2/sitcoms_ep01.mp4?confirm=yes").status_code == 200
    assert not target.exists()


@pytest.mark.parametrize("name", ["..", "..%2fconfig.yaml", "%2eb", ".hidden"])
def test_deleting_something_outside_the_folder_is_refused(box, sent, tmp_path, name):
    client, cfg, _ = box
    res = client.delete(f"/api/media/2/{name}?confirm=yes")
    assert res.status_code in (400, 404)
    assert cfg.exists(), "the config file was deleted"


def test_deleting_a_file_that_is_not_there_is_a_404(box, sent):
    client, _, _ = box
    assert client.delete("/api/media/2/nope.mp4?confirm=yes").status_code == 404


# ==========================================================================
# Settings
# ==========================================================================
def test_settings_are_reported(box, monkeypatch):
    monkeypatch.setattr("retrobox.hwdetect.detect_audio", lambda: ["alsa/hdmi:CARD=PCH"])
    client, _, _ = box
    data = client.get("/api/settings").get_json()
    assert data["initial_volume"] == 70
    assert data["auto_channels"] is False
    assert data["sleep_timer"] == [30, 60, 90]
    assert data["audio_devices"] == ["alsa/hdmi:CARD=PCH"]


def test_audio_detection_failing_does_not_break_the_settings_page(box, monkeypatch):
    def boom():
        raise OSError("no aplay here")

    monkeypatch.setattr("retrobox.hwdetect.detect_audio", boom)
    client, _, _ = box
    res = client.get("/api/settings")
    assert res.status_code == 200
    assert res.get_json()["audio_devices"] == []


def test_settings_round_trip_into_the_config(box, sent):
    client, cfg, _ = box
    res = client.post("/api/settings", json={
        "initial_volume": 35, "auto_channels": True, "sleep_timer": [15, 45],
        "audio_device": "alsa/hdmi:CARD=PCH,DEV=0",
    })
    assert res.status_code == 200, res.get_json()

    reloaded = load_config(cfg)
    assert reloaded.initial_volume == 35
    assert reloaded.auto_channels is True
    assert reloaded.sleep_steps == (15, 45)
    assert reloaded.audio_device == "alsa/hdmi:CARD=PCH,DEV=0"


def test_turning_the_sleep_timer_off(box, sent):
    client, cfg, _ = box
    assert client.post("/api/settings", json={"sleep_timer": []}).status_code == 200
    assert load_config(cfg).sleep_steps == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"initial_volume": 101},
        {"initial_volume": -1},
        {"initial_volume": "loud"},
        {"sleep_timer": [0]},
        {"sleep_timer": [99999]},
        {"sleep_timer": "thirty"},
        {"sleep_timer": [5, "x"]},
        {"auto_channels": "maybe"},
        {"audio_device": "x" * 500},
        {"audio_device": "bad\x00device"},
    ],
)
def test_out_of_range_settings_are_refused(box, sent, payload):
    client, cfg, _ = box
    before = cfg.read_text()
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 400, payload
    assert cfg.read_text() == before


def test_an_unknown_setting_is_refused_rather_than_ignored(box, sent):
    client, cfg, _ = box
    res = client.post("/api/settings", json={"power_off_command": ["rm", "-rf", "/"]})
    assert res.status_code == 400
    assert "power_off_command" in res.get_json()["error"]


def test_settings_say_when_a_restart_is_needed(box, sent):
    client, _, _ = box
    res = client.post("/api/settings", json={"auto_channels": True})
    assert res.get_json()["restart_required"] == ["auto_channels"]

    res = client.post("/api/settings", json={"initial_volume": 50})
    assert res.get_json()["restart_required"] == []


# ==========================================================================
# The page itself
# ==========================================================================
def test_the_page_is_self_contained(box):
    client, _, _ = box
    body = client.get("/dash").get_data(as_text=True)
    assert "JV PROJECTS" in body
    assert "http://" not in body.replace("http://www.w3.org", "")
    assert "https://" not in body
    assert "<script src=" not in body and "@import" not in body


def test_the_existing_remote_control_still_works(box, sent):
    client, _, _ = box
    client.post("/api/tune/5")
    client.post("/api/volume/up")
    client.post("/api/mute")
    assert sent == ["channel 5", "volume_up", "mute"]


# ==========================================================================
# The streaming copy itself, where the size checks actually live
# ==========================================================================
class _ShortStream:
    """A client that closes politely having sent less than it promised.

    Werkzeug raises ClientDisconnected instead, but the box is not required to
    be served by Werkzeug forever, and a truncated film that got saved anyway
    is a channel with a broken episode on it.
    """

    def __init__(self, data):
        self._data = data
        self._done = False

    def read(self, _size):
        if self._done:
            return b""
        self._done = True
        return self._data


def test_a_clean_but_short_upload_is_rejected(tmp_path):
    from retrobox.webui import ApiError, stream_to_file

    out = (tmp_path / "staged").open("wb")
    with pytest.raises(ApiError) as caught:
        stream_to_file(_ShortStream(b"\x00" * 10), out, declared=1000, limit=10**9)
    out.close()
    assert "before it finished" in str(caught.value)


def test_a_stream_that_runs_over_the_limit_is_cut_off(tmp_path):
    from retrobox.webui import ApiError, stream_to_file

    out = (tmp_path / "staged").open("wb")
    with pytest.raises(ApiError) as caught:
        stream_to_file(_ShortStream(b"\x00" * 5000), out, declared=5000, limit=100)
    out.close()
    assert caught.value.status == 413


def test_a_complete_stream_reports_what_it_wrote(tmp_path):
    from retrobox.webui import stream_to_file

    target = tmp_path / "staged"
    with target.open("wb") as out:
        assert stream_to_file(
            _ShortStream(b"\xff" * 4096), out, declared=4096, limit=10**9
        ) == 4096
    assert target.read_bytes() == b"\xff" * 4096
