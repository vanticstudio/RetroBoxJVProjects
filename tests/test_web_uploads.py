"""The two flows Jake actually asked for, over HTTP.

1. Drop a folder, get a channel filled with it.
2. Drop files on a channel that already exists, top it up.

Both without SSH, without Samba and without a file manager - and both without
a login, which is why every value in every request is bounded before it is
allowed near the disk.
"""

import json
from types import SimpleNamespace

import pytest

from retrobox.channel import scan_episodes
from retrobox.config import DEFAULT_VIDEO_EXTENSIONS, load_config
from tests.helpers import make_show

flask = pytest.importorskip("flask")
from retrobox.webui import create_app  # noqa: E402

CHUNK = 1024 * 1024      # the default chunk_mb, in bytes


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("RETROBOX_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", str(tmp_path / "c.sock"))


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(
        "retrobox.webui.send_command", lambda c, **k: out.append(c) or True
    )
    return out


@pytest.fixture
def box(tmp_path, runtime):
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\n"
        f"web:\n  chunk_mb: 1\n"
        f"channels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app.test_client(), cfg, root


def start(client, payload):
    return client.post("/api/uploads", json=payload)


def put_chunk(client, sid, index, chunk, data):
    return client.put(
        f"/api/uploads/{sid}/{index}/{chunk}",
        data=data,
        content_type="application/octet-stream",
    )


def upload_whole(client, sid, index, payload, chunk_bytes=CHUNK):
    for i in range(0, max(len(payload), 1), chunk_bytes):
        res = put_chunk(client, sid, index, i // chunk_bytes, payload[i:i + chunk_bytes])
        assert res.status_code == 200, res.get_json()


# ==========================================================================
# Flow 2: top up a channel that already exists
# ==========================================================================
def test_a_file_dropped_on_a_channel_becomes_an_episode(box, sent):
    client, cfg, root = box
    payload = bytes(range(256)) * 20

    started = start(client, {
        "channel": 2, "files": [{"path": "new_ep.mp4", "size": len(payload)}],
    })
    assert started.status_code == 201, started.get_json()
    sid = started.get_json()["session"]

    upload_whole(client, sid, 0, payload)
    done = client.post(f"/api/uploads/{sid}/commit")
    assert done.status_code == 200, done.get_json()
    assert [r["state"] for r in done.get_json()["results"]] == ["done"]

    landed = root / "sitcoms" / "new_ep.mp4"
    assert landed.read_bytes() == payload
    assert landed in scan_episodes(root / "sitcoms", DEFAULT_VIDEO_EXTENSIONS)


def test_finishing_an_upload_tells_the_tv_to_rescan(box, sent):
    client, cfg, root = box
    payload = b"\x01" * 100
    sid = start(client, {
        "channel": 2, "files": [{"path": "ep.mp4", "size": len(payload)}],
    }).get_json()["session"]
    upload_whole(client, sid, 0, payload)
    client.post(f"/api/uploads/{sid}/commit")
    assert "reload" in sent


def test_uploading_to_a_channel_that_is_not_there(box, sent):
    client, _, _ = box
    res = start(client, {"channel": 77, "files": [{"path": "e.mp4", "size": 1}]})
    assert res.status_code == 404


# ==========================================================================
# Flow 1: a whole folder becomes a channel
# ==========================================================================
def test_a_dropped_folder_becomes_a_filled_channel(box, sent):
    client, cfg, root = box
    episodes = {f"Disney/ep{i}.mp4": bytes([i]) * 300 for i in range(1, 4)}

    started = start(client, {
        "new_channel": {"name": "Disney", "folder": "Disney"},
        "files": [{"path": p, "size": len(d)} for p, d in episodes.items()],
    })
    assert started.status_code == 201, started.get_json()
    body = started.get_json()
    sid = body["session"]

    # The channel must not exist yet - nothing has been uploaded.
    assert [c.name for c in load_config(cfg).channels] == ["Sitcoms"]

    for item in body["files"]:
        upload_whole(client, sid, item["index"], episodes["Disney/" + item["name"]])

    done = client.post(f"/api/uploads/{sid}/commit")
    assert done.status_code == 200, done.get_json()

    reloaded = load_config(cfg)
    assert [c.name for c in reloaded.channels] == ["Sitcoms", "Disney"]
    disney = reloaded.channels[1]
    assert disney.path == root / "Disney"
    assert len(scan_episodes(disney.path, DEFAULT_VIDEO_EXTENSIONS)) == 3


def test_a_new_channel_takes_the_next_free_number(box, sent):
    client, cfg, root = box
    payload = b"\x02" * 50
    started = start(client, {
        "new_channel": {"name": "Disney", "folder": "Disney"},
        "files": [{"path": "Disney/ep.mp4", "size": len(payload)}],
    })
    sid = started.get_json()["session"]
    upload_whole(client, sid, 0, payload)
    client.post(f"/api/uploads/{sid}/commit")
    assert load_config(cfg).channels[1].number == 3


def test_a_new_channel_can_be_given_a_number(box, sent):
    client, cfg, root = box
    payload = b"\x03" * 50
    sid = start(client, {
        "new_channel": {"name": "Disney", "folder": "Disney", "number": 12},
        "files": [{"path": "Disney/ep.mp4", "size": len(payload)}],
    }).get_json()["session"]
    upload_whole(client, sid, 0, payload)
    client.post(f"/api/uploads/{sid}/commit")
    assert load_config(cfg).channels[1].number == 12


def test_a_colliding_channel_number_is_refused_before_anything_uploads(box, sent):
    client, cfg, root = box
    res = start(client, {
        "new_channel": {"name": "Disney", "folder": "Disney", "number": 2},
        "files": [{"path": "Disney/ep.mp4", "size": 50}],
    })
    assert res.status_code == 400
    assert "already" in res.get_json()["error"].lower()
    assert not (root / "Disney").exists()


def test_no_channel_is_created_when_the_upload_fails(box, sent):
    # A channel pointing at a folder that failed to fill is worse than no
    # channel: it is an entry on the dial that plays nothing.
    client, cfg, root = box
    started = start(client, {
        "new_channel": {"name": "Disney", "folder": "Disney"},
        "files": [{"path": "Disney/ep.mp4", "size": CHUNK * 3}],
    })
    sid = started.get_json()["session"]
    # One chunk of three arrives, then the laptop shuts its lid.
    assert put_chunk(client, sid, 0, 0, b"\x04" * CHUNK).status_code == 200

    done = client.post(f"/api/uploads/{sid}/commit")
    assert done.status_code == 400
    assert "missing" in done.get_json()["error"]
    assert [c.name for c in load_config(cfg).channels] == ["Sitcoms"]
    assert not (root / "Disney").exists()


def test_an_abandoned_new_channel_folder_does_not_linger(box, sent):
    client, cfg, root = box
    sid = start(client, {
        "new_channel": {"name": "Disney", "folder": "Disney"},
        "files": [{"path": "Disney/ep.mp4", "size": 50}],
    }).get_json()["session"]
    client.delete(f"/api/uploads/{sid}")

    assert [c.name for c in load_config(cfg).channels] == ["Sitcoms"]
    assert not (root / "Disney").exists(), "an empty folder was left behind"


@pytest.mark.parametrize(
    "folder",
    ["../evil", "/etc", "..", ".hidden", "a/b", "a\\b", "", "   ", "x" * 300, "we:ird"],
)
def test_a_hostile_new_folder_name_is_refused(box, sent, folder):
    client, cfg, root = box
    res = start(client, {
        "new_channel": {"name": "Bad", "folder": folder},
        "files": [{"path": "ep.mp4", "size": 10}],
    })
    assert res.status_code == 400
    assert cfg.exists()


def test_a_new_channel_cannot_take_over_an_existing_folder(box, sent):
    client, cfg, root = box
    res = start(client, {
        "new_channel": {"name": "Not Sitcoms", "folder": "sitcoms"},
        "files": [{"path": "ep.mp4", "size": 10}],
    })
    assert res.status_code == 409
    assert len(scan_episodes(root / "sitcoms", DEFAULT_VIDEO_EXTENSIONS)) == 2


# ==========================================================================
# Resuming
# ==========================================================================
def test_a_reloaded_page_can_find_and_finish_its_upload(box, sent):
    client, cfg, root = box
    payload = bytes(range(256)) * 12
    sid = start(client, {
        "channel": 2, "files": [{"path": "ep.mp4", "size": len(payload)}],
    }).get_json()["session"]

    put_chunk(client, sid, 0, 0, payload[:CHUNK])       # then the tab closes

    listed = client.get("/api/uploads").get_json()
    assert [s["id"] for s in listed["sessions"]] == [sid]
    resumed = listed["sessions"][0]
    assert resumed["missing"]["0"] == [], "one chunk covered the whole file"

    done = client.post(f"/api/uploads/{sid}/commit")
    assert done.status_code == 200
    assert (root / "sitcoms" / "ep.mp4").read_bytes() == payload


def test_the_status_route_says_what_is_still_needed(box, sent):
    client, cfg, root = box
    size = CHUNK * 3
    sid = start(client, {
        "channel": 2, "files": [{"path": "big.mp4", "size": size}],
    }).get_json()["session"]

    put_chunk(client, sid, 0, 1, b"\x05" * CHUNK)      # the middle one only
    status = client.get(f"/api/uploads/{sid}").get_json()
    assert status["missing"]["0"] == [0, 2]
    assert status["received_bytes"] == CHUNK


def test_a_cancelled_upload_takes_its_chunks_with_it(box, sent):
    client, cfg, root = box
    sid = start(client, {
        "channel": 2, "files": [{"path": "ep.mp4", "size": CHUNK * 2}],
    }).get_json()["session"]
    assert put_chunk(client, sid, 0, 0, b"\x06" * CHUNK).status_code == 200

    assert client.delete(f"/api/uploads/{sid}").status_code == 200
    assert client.get("/api/uploads").get_json()["sessions"] == []
    assert list((root / "sitcoms").glob("*.chunk")) == []


# ==========================================================================
# Hostile input, at the route
# ==========================================================================
@pytest.mark.parametrize(
    "sid",
    ["..", "../../etc", "%2e%2e%2f", "a b", ".hidden", "x" * 200, "sess%00ion"],
)
def test_a_hostile_session_id_is_refused_by_every_route(box, sent, sid, tmp_path):
    # The property that matters is not which error code comes back - some of
    # these never even reach a view, because the router rejects or rewrites
    # them first - but that none of them succeeds and none of them writes.
    client, cfg, root = box
    before = {p for p in tmp_path.rglob("*") if p.is_file()}

    codes = [
        client.get(f"/api/uploads/{sid}", follow_redirects=True).status_code,
        client.put(
            f"/api/uploads/{sid}/0/0", data=b"x",
            content_type="application/octet-stream", follow_redirects=True,
        ).status_code,
        client.post(f"/api/uploads/{sid}/commit", follow_redirects=True).status_code,
        client.delete(f"/api/uploads/{sid}", follow_redirects=True).status_code,
    ]
    assert all(code >= 400 for code in codes), f"{sid} was accepted: {codes}"
    assert {p for p in tmp_path.rglob("*") if p.is_file()} == before
    assert cfg.exists()


@pytest.mark.parametrize(
    "path",
    [
        "../../../etc/systemd/system/evil.service",
        "Disney/../../evil.mp4",
        "/etc/cron.d/evil.mp4",
        "evil.mp4\x00.sh",
        "Disney/.ssh/authorized_keys",
        "..",
        "C:\\windows\\evil.mp4",
    ],
)
def test_a_traversing_file_path_never_gets_a_session(box, sent, path, tmp_path):
    client, cfg, root = box
    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    res = start(client, {"channel": 2, "files": [{"path": path, "size": 10}]})
    assert res.status_code == 400, path
    assert {p for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("path", ["evil.sh", "unit.service", "notes.txt", "ep.mp4.sh"])
def test_a_disallowed_extension_never_gets_a_session(box, sent, path):
    client, _, _ = box
    assert start(client, {
        "channel": 2, "files": [{"path": path, "size": 10}],
    }).status_code == 400


def test_a_tampered_manifest_cannot_talk_the_box_into_a_bad_write(box, sent):
    # Defence in depth: the manifest is a file on disk, and the layer that
    # actually writes does not take its word for where a file belongs.
    client, cfg, media = box
    sid = start(client, {
        "channel": 2, "files": [{"path": "ep.mp4", "size": 100}],
    }).get_json()["session"]

    manifest = media / ".retrobox-uploads" / sid / "session.json"
    data = json.loads(manifest.read_text())
    data["files"][0]["relative"] = "../../../evil.sh"
    manifest.write_text(json.dumps(data))

    assert put_chunk(client, sid, 0, 0, b"x" * 10).status_code == 400
    assert not (media.parent / "evil.sh").exists()


@pytest.mark.parametrize("index,chunk", [(-1, 0), (99, 0), (0, -1), (0, 9999)])
def test_indexes_outside_the_session_are_refused(box, sent, index, chunk):
    client, _, _ = box
    sid = start(client, {
        "channel": 2, "files": [{"path": "ep.mp4", "size": 100}],
    }).get_json()["session"]
    assert put_chunk(client, sid, index, chunk, b"x").status_code in (400, 404)


def test_an_oversized_chunk_is_refused(box, sent):
    client, _, root = box
    sid = start(client, {
        "channel": 2, "files": [{"path": "ep.mp4", "size": CHUNK * 2}],
    }).get_json()["session"]
    res = put_chunk(client, sid, 0, 0, b"x" * (CHUNK * 2 + 10))
    assert res.status_code in (400, 413)


# ==========================================================================
# Limits and space
# ==========================================================================
def test_a_batch_that_would_fill_the_disk_is_refused_before_it_starts(
    box, sent, monkeypatch, tmp_path
):
    client, cfg, root = box
    monkeypatch.setattr(
        "retrobox.uploads.shutil.disk_usage",
        lambda p: SimpleNamespace(total=10**12, used=0, free=1200 * 1024 * 1024),
    )
    res = start(client, {
        "channel": 2,
        "files": [{"path": f"ep{i}.mp4", "size": 100 * 1024 * 1024} for i in range(5)],
    })
    assert res.status_code in (400, 507)
    assert "space" in res.get_json()["error"].lower()
    assert not (root / ".retrobox-uploads").exists() or \
        list((root / ".retrobox-uploads").iterdir()) == []


def test_too_many_files_at_once_is_refused(tmp_path, runtime, sent):
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nweb:\n  max_files_per_upload: 3\nchannels:\n"
        f'  - number: 2\n    name: "S"\n    path: {root / "sitcoms"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    client = app.test_client()

    res = start(client, {
        "channel": 2, "files": [{"path": f"e{i}.mp4", "size": 10} for i in range(4)],
    })
    assert res.status_code == 400


def test_too_many_sessions_at_once_is_refused(tmp_path, runtime, sent):
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nweb:\n  max_upload_sessions: 1\nchannels:\n"
        f'  - number: 2\n    name: "S"\n    path: {root / "sitcoms"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    client = app.test_client()

    assert start(client, {"channel": 2, "files": [{"path": "a.mp4", "size": 10}]}).status_code == 201
    assert start(client, {"channel": 2, "files": [{"path": "b.mp4", "size": 10}]}).status_code == 400


# ==========================================================================
# Duplicates and unplayable files
# ==========================================================================
def test_an_existing_episode_is_reported_as_a_duplicate(box, sent):
    client, cfg, root = box
    res = start(client, {
        "channel": 2, "files": [{"path": "sitcoms_ep01.mp4", "size": 10}],
    })
    assert res.get_json()["files"][0]["duplicate"] is True


def test_a_duplicate_is_left_alone_unless_replace_was_asked_for(box, sent):
    client, cfg, root = box
    original = (root / "sitcoms" / "sitcoms_ep01.mp4").read_bytes()
    payload = b"\x07" * 200
    sid = start(client, {
        "channel": 2,
        "files": [{"path": "sitcoms_ep01.mp4", "size": len(payload)}],
    }).get_json()["session"]
    upload_whole(client, sid, 0, payload)
    done = client.post(f"/api/uploads/{sid}/commit")

    assert done.get_json()["results"][0]["state"] == "skipped"
    assert (root / "sitcoms" / "sitcoms_ep01.mp4").read_bytes() == original


def test_replace_is_honoured_when_the_user_picks_it(box, sent):
    client, cfg, root = box
    payload = b"\x08" * 200
    sid = start(client, {
        "channel": 2,
        "files": [{"path": "sitcoms_ep01.mp4", "size": len(payload),
                   "action": "replace"}],
    }).get_json()["session"]
    upload_whole(client, sid, 0, payload)
    client.post(f"/api/uploads/{sid}/commit")
    assert (root / "sitcoms" / "sitcoms_ep01.mp4").read_bytes() == payload


def test_a_file_with_no_picture_is_flagged_but_kept(box, sent, monkeypatch):
    from retrobox.probe import MediaInfo

    monkeypatch.setattr(
        "retrobox.uploads.probe.probe_media", lambda p, **k: MediaInfo(60.0, False)
    )
    client, cfg, root = box
    payload = b"\x09" * 200
    sid = start(client, {
        "channel": 2, "files": [{"path": "audio_only.mp4", "size": len(payload)}],
    }).get_json()["session"]
    upload_whole(client, sid, 0, payload)
    done = client.post(f"/api/uploads/{sid}/commit").get_json()

    assert done["results"][0]["state"] == "no video"
    assert (root / "sitcoms" / "audio_only.mp4").exists(), "the box deleted it"


# ==========================================================================
# One spool, one lock
# ==========================================================================
def test_the_store_each_request_builds_locks_against_all_the_others(box, sent):
    """A store per request is correct; a lock per request is not.

    The dashboard deliberately builds a fresh ``UploadStore`` for every
    request - nothing about an upload is kept in memory, which is what makes a
    mid-upload reboot survivable - and Flask serves those requests on threads.
    A lock made in ``__init__`` would therefore be a different object every
    time and would serialise nothing at all: two phones could walk past the
    session cap together, and a sweep could delete a session in the instant
    between its directory being made and its manifest being written.

    This reaches for a private attribute because that is the only place the
    answer is visible. What it is really asserting is that two requests meet
    on one lock.
    """
    import threading

    from retrobox import webui

    made = []

    class Recording(webui.UploadStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(webui, "UploadStore", Recording)
        client, _, _ = box
        assert client.get("/api/uploads").status_code == 200
        assert client.get("/api/settings").status_code == 200

    assert len(made) >= 2, "the routes did not build a store per request"
    assert hasattr(made[0], "_move_into_place"), (
        "the whole-file upload route borrows this to land a finished file on "
        "another filesystem - see webui._land_upload. Renaming it here without "
        "changing that leaves an upload to a plugged-in drive failing at the "
        "very end, on a box with no way to be told why."
    )
    first = made[0]._lock
    assert isinstance(first, type(threading.RLock())), "that is not a lock"
    assert all(store._lock is first for store in made), (
        "each request got its own lock, which serialises nothing"
    )


# ==========================================================================
# Reclaiming abandoned uploads
# ==========================================================================
def test_reclaimable_space_is_reported_in_settings(box, sent):
    client, cfg, root = box
    sid = start(client, {
        "channel": 2, "files": [{"path": "ep.mp4", "size": CHUNK * 2}],
    }).get_json()["session"]
    assert put_chunk(client, sid, 0, 0, b"\x0a" * CHUNK).status_code == 200

    settings = client.get("/api/settings").get_json()
    assert settings["upload_spool_bytes"] >= CHUNK
    assert settings["upload_sessions"] == 1


def test_the_spool_is_never_inside_a_channel_folder(box, sent):
    client, cfg, media = box
    sid = start(client, {
        "channel": 2, "files": [{"path": "ep.mp4", "size": CHUNK}],
    }).get_json()["session"]
    assert put_chunk(client, sid, 0, 0, b"\x0b" * CHUNK).status_code == 200

    assert (media / ".retrobox-uploads" / sid).is_dir()
    assert scan_episodes(media / "sitcoms", DEFAULT_VIDEO_EXTENSIONS) == \
        sorted((media / "sitcoms").glob("sitcoms_ep*.mp4"), key=lambda p: str(p).lower())


def test_the_spool_never_becomes_a_channel(box, sent):
    # media_root discovery turns every folder into a channel. A dotted spool
    # directory is skipped, which is exactly why it is dotted.
    client, cfg, media = box
    sid = start(client, {
        "channel": 2, "files": [{"path": "ep.mp4", "size": CHUNK}],
    }).get_json()["session"]
    assert put_chunk(client, sid, 0, 0, b"\x0c" * CHUNK).status_code == 200

    cfg.write_text(f"media_root: {media}\n")
    assert [c.name for c in load_config(cfg).channels] == ["Sitcoms"]


def test_a_new_channel_is_not_created_when_every_file_is_skipped(box, sent):
    # Nothing landed, so there is nothing to point a channel at. The folder
    # must not be left behind either.
    client, cfg, root = box
    started = start(client, {
        "new_channel": {"name": "Disney", "folder": "Disney"},
        "files": [{"path": "Disney/ep.mp4", "size": 50, "action": "skip"}],
    })
    assert started.status_code == 201, started.get_json()
    sid = started.get_json()["session"]

    done = client.post(f"/api/uploads/{sid}/commit")
    assert done.status_code == 400
    assert "no channel" in done.get_json()["error"].lower()
    assert [c.name for c in load_config(cfg).channels] == ["Sitcoms"]
    assert not (root / "Disney").exists()
