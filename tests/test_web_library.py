"""The file manager, over HTTP, on a box with no login and no keyboard.

Everything here is written from the customer's side of the glass: browse the
library, pick some things, be told exactly what deleting them costs *before*
anything moves, and be able to change your mind afterwards. The unusual
requirement this file exists to protect is that last one - deleting frees no
space, on purpose, and the page has to say so rather than let somebody bin
forty gigabytes and conclude the disk gauge is broken.

The other half is the attacker on the LAN. There is no authentication, so
every path in every request is a hostile string until :mod:`retrobox.safepath`
has had its say, and these tests call the routes with the strings an attacker
would.
"""

import pytest

from retrobox import library
from retrobox.autochannels import discover_new_channels
from retrobox.channel import scan_episodes
from retrobox.config import DEFAULT_VIDEO_EXTENSIONS, load_config
from tests.helpers import make_show

flask = pytest.importorskip("flask")
from retrobox import webui  # noqa: E402
from retrobox.webui import create_app  # noqa: E402


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("RETROBOX_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", str(tmp_path / "c.sock"))


@pytest.fixture
def sent(monkeypatch):
    """Every command the dashboard sends the television, and it always lands."""
    out = []
    monkeypatch.setattr(
        "retrobox.webui.send_command", lambda c, **k: out.append(c) or True
    )
    return out


@pytest.fixture
def box(tmp_path, runtime):
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 3)
    make_show(root, "movies", 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\n"
        f"web:\n  chunk_mb: 1\n"
        f"channels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
        f'  - number: 5\n    name: "Movies"\n    path: {root / "movies"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app.test_client(), cfg, root


def names(payload):
    return [e["name"] for e in payload["entries"]]


def delete(client, paths, **args):
    query = "&".join(f"{k}={v}" for k, v in args.items())
    return client.post(
        "/api/library/delete" + ("?" + query if query else ""),
        json={"paths": paths},
    )


# ==========================================================================
# Browsing
# ==========================================================================
def test_the_library_route_lists_the_media_root(box):
    client, _, _ = box
    data = client.get("/api/library").get_json()
    assert data["path"] == ""
    assert data["parent"] is None
    assert names(data) == ["movies", "sitcoms"]
    assert data["counts"]["folders"] == 2


def test_the_library_route_lists_inside_a_folder_and_offers_the_way_back(box):
    client, _, _ = box
    data = client.get("/api/library?path=sitcoms").get_json()
    assert data["path"] == "sitcoms"
    assert data["parent"] == ""
    assert [c["name"] for c in data["crumbs"]] == ["Library", "sitcoms"]
    assert len(data["entries"]) == 3
    assert all(e["kind"] == "video" and e["selectable"] for e in data["entries"])


def test_the_library_route_pages_and_sorts_the_way_it_was_asked_to(box):
    client, _, _ = box
    data = client.get("/api/library?path=sitcoms&per_page=2&page=2").get_json()
    assert data["per_page"] == 2 and data["page"] == 2 and data["pages"] == 2
    assert len(data["entries"]) == 1

    down = client.get("/api/library?path=sitcoms&sort=name&order=desc").get_json()
    assert names(down) == sorted(names(down), reverse=True)


@pytest.mark.parametrize(
    "path", ["../../etc", "/etc", "sitcoms/../../etc", "sitcoms\\..\\.."]
)
def test_the_library_route_refuses_a_path_that_climbs_out_of_the_library(box, path):
    client, _, _ = box
    res = client.get("/api/library", query_string={"path": path})
    assert res.status_code == 400
    assert res.get_json()["ok"] is False


def test_browsing_a_folder_that_is_not_there_is_a_plain_404(box):
    client, _, _ = box
    res = client.get("/api/library?path=westerns")
    assert res.status_code == 404
    assert "westerns" in res.get_json()["error"]


def test_the_library_routes_say_what_to_do_when_there_is_no_media_root(tmp_path, runtime):
    cfg = tmp_path / "config.yaml"
    make_show(tmp_path, "sitcoms", 1)
    cfg.write_text(
        f'channels:\n  - number: 2\n    name: "S"\n    path: {tmp_path / "sitcoms"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    res = app.test_client().get("/api/library")
    assert res.status_code == 400
    assert "media_root" in res.get_json()["error"]


# ==========================================================================
# The confirmation: what this delete actually costs
# ==========================================================================
def test_the_plan_says_how_many_files_and_how_much_space(box):
    client, _, root = box
    (root / "sitcoms" / "sitcoms_ep01.mp4").write_bytes(b"x" * 2048)
    plan = client.post("/api/library/plan", json={"paths": ["sitcoms"]}).get_json()
    assert plan["totals"]["files"] == 3
    assert plan["totals"]["bytes"] >= 2048
    assert plan["items"][0]["name"] == "sitcoms"


def test_the_plan_names_the_channels_that_would_lose_their_folder(box):
    client, _, _ = box
    plan = client.post("/api/library/plan", json={"paths": ["sitcoms"]}).get_json()
    assert plan["channels"] == [2]
    assert any("Sitcoms" in w for w in plan["warnings"])


def test_the_plan_says_the_channel_empties_and_what_the_box_plays_instead(box):
    client, _, _ = box
    plan = client.post("/api/library/plan", json={"paths": ["sitcoms"]}).get_json()
    warning = " ".join(plan["warnings"])
    assert "NO SIGNAL" in warning, (
        "somebody deleting a channel's whole folder has to be told what the "
        "television will show afterwards, not just that something will change"
    )


def test_the_plan_names_a_scheduled_block_that_would_lose_its_folder(tmp_path, runtime):
    """A daypart can point at its own folder, and it orphans just as easily as
    a channel does - but only that part of the day goes quiet, so it is said
    differently."""
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 2)
    make_show(root, "latenight", 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\n"
        f"channels:\n"
        f'  - number: 3\n    name: "Mixed"\n    path: {root / "sitcoms"}\n'
        f"    dayparts:\n"
        f'      - from: "22:00"\n        to: "02:00"\n'
        f'        name: "AFTER HOURS"\n        path: {root / "latenight"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    plan = app.test_client().post(
        "/api/library/plan", json={"paths": ["latenight"]}
    ).get_json()
    assert plan["dayparts"] == [3]
    warning = " ".join(plan["warnings"])
    assert "scheduled block" in warning
    assert "NO SIGNAL" in warning


def test_the_plan_says_plainly_that_deleting_frees_no_space(box):
    client, _, _ = box
    plan = client.post("/api/library/plan", json={"paths": ["movies"]}).get_json()
    assert plan["frees_space"] is False
    assert "trash" in plan["note"].lower()


def test_the_plan_carries_the_disk_so_a_nearly_full_box_can_say_so(box):
    client, _, _ = box
    plan = client.post("/api/library/plan", json={"paths": ["movies"]}).get_json()
    assert "free_bytes" in plan["space"]
    assert plan["space"]["reclaimable_bytes"] >= 0


def test_the_plan_adds_up_a_bulk_selection(box):
    client, _, _ = box
    plan = client.post(
        "/api/library/plan", json={"paths": ["sitcoms", "movies"]}
    ).get_json()
    assert plan["totals"]["files"] == 5
    assert plan["channels"] == [2, 5]


def test_the_plan_refuses_a_path_that_climbs_out(box):
    client, _, _ = box
    res = client.post("/api/library/plan", json={"paths": ["../../etc"]})
    assert res.status_code == 400


# ==========================================================================
# Deleting
# ==========================================================================
def test_deleting_without_the_confirmation_changes_nothing(box):
    client, _, root = box
    res = client.post("/api/library/delete", json={"paths": ["movies"]})
    assert res.status_code == 400
    assert "confirm" in res.get_json()["error"]
    assert (root / "movies").is_dir()


def test_deleting_moves_the_folder_to_the_trash_rather_than_unlinking_it(box, sent):
    client, _, root = box
    res = delete(client, ["movies"], confirm="yes")
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert not (root / "movies").exists()
    assert body["deleted"][0]["name"] == "movies"
    token = body["deleted"][0]["token"]
    assert (root / library.TRASH_NAME / token / "payload" / "movies").is_dir()


def test_deleting_reports_that_it_freed_nothing_and_offers_the_trash(box, sent):
    client, _, _ = box
    body = delete(client, ["movies"], confirm="yes").get_json()
    assert body["freed_bytes"] == 0
    assert body["trash_bytes"] > 0
    assert "empty the trash" in body["note"].lower()


def test_deleting_tells_the_television_to_reload(box, sent):
    client, _, _ = box
    delete(client, ["movies"], confirm="yes")
    assert "reload" in sent, (
        "the running television is still holding a playlist of files that are "
        "no longer there"
    )


def test_a_bulk_delete_takes_everything_that_was_selected(box, sent):
    client, _, root = box
    body = delete(
        client, ["sitcoms/sitcoms_ep01.mp4", "sitcoms/sitcoms_ep02.mp4"],
        confirm="yes",
    ).get_json()
    assert len(body["deleted"]) == 2
    assert not (root / "sitcoms" / "sitcoms_ep01.mp4").exists()
    assert (root / "sitcoms" / "sitcoms_ep03.mp4").exists()


def test_a_bulk_delete_reports_the_one_that_failed_and_keeps_the_rest(box, sent):
    client, _, root = box
    body = delete(client, ["movies", "westerns"], confirm="yes").get_json()
    assert [d["name"] for d in body["deleted"]] == ["movies"]
    assert body["failed"][0]["path"] == "westerns"
    assert "westerns" in body["failed"][0]["error"]


def test_deleting_nothing_at_all_is_refused_rather_than_answered_cheerfully(box):
    client, _, _ = box
    res = client.post("/api/library/delete?confirm=yes", json={"paths": []})
    assert res.status_code == 400


def test_an_absurd_number_of_paths_is_refused_before_the_disk_is_walked(box):
    """Select-all on six hundred episodes is ordinary. A hundred thousand
    paths in one body is not a customer, and each one costs a walk."""
    client, _, _ = box
    too_many = [f"sitcoms/ep{n}.mp4" for n in range(webui.MAX_LIBRARY_SELECTION + 1)]
    res = client.post("/api/library/delete?confirm=yes", json={"paths": too_many})
    assert res.status_code == 400
    assert str(webui.MAX_LIBRARY_SELECTION) in res.get_json()["error"]


def test_a_path_that_is_not_even_a_string_is_refused(box):
    client, _, _ = box
    res = client.post("/api/library/delete?confirm=yes", json={"paths": [{"x": 1}]})
    assert res.status_code == 400


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", ".welcome"])
def test_deleting_refuses_a_hostile_or_machinery_path(box, path):
    client, _, _ = box
    res = delete(client, [path], confirm="yes")
    assert res.status_code == 400


def test_deleting_refuses_a_file_that_is_not_a_video(box):
    client, _, root = box
    (root / "sitcoms" / "notes.txt").write_text("hello")
    res = delete(client, ["sitcoms/notes.txt"], confirm="yes")
    assert res.status_code == 400
    assert (root / "sitcoms" / "notes.txt").exists()


def test_deleting_a_folder_an_upload_is_landing_in_is_refused_until_it_is_cancelled(box):
    client, _, root = box
    start = client.post(
        "/api/uploads",
        json={"channel": 5, "files": [{"path": "new.mp4", "size": 10}]},
    )
    assert start.status_code in (200, 201), start.get_json()

    res = delete(client, ["movies"], confirm="yes")
    assert res.status_code == 409
    assert "upload" in res.get_json()["error"].lower()
    assert (root / "movies").is_dir()

    forced = client.post(
        "/api/library/delete?confirm=yes",
        json={"paths": ["movies"], "cancel_uploads": True},
    )
    assert forced.status_code == 200, forced.get_json()
    assert not (root / "movies").exists()


# ==========================================================================
# The trash is invisible to the television - proved, not assumed
# ==========================================================================
def test_a_deleted_episode_leaves_the_channel_the_television_scans(box, sent):
    client, cfg, root = box
    delete(client, ["sitcoms/sitcoms_ep01.mp4"], confirm="yes")
    config = load_config(str(cfg))
    channel = next(c for c in config.channels if c.number == 2)
    found = [p.name for p in scan_episodes(channel.path, config.video_extensions)]
    assert "sitcoms_ep01.mp4" not in found
    assert len(found) == 2


def test_auto_discovery_never_offers_the_trash_as_a_channel(box, sent):
    client, cfg, root = box
    delete(client, ["movies"], confirm="yes")
    assert (root / library.TRASH_NAME).is_dir(), (
        "nothing reached the trash, so this proves nothing"
    )
    found = [c.path.name for c in discover_new_channels(load_config(str(cfg)))]
    assert library.TRASH_NAME not in found


def test_a_channel_pointed_at_the_media_root_is_taught_to_ignore_the_trash(
    tmp_path, runtime, sent
):
    """The one gap the library module could not close from inside itself.

    ``scan_episodes`` skips hidden *files*, not hidden *folders*, so a channel
    whose path is the media root - nothing creates one, but a person can write
    one by hand - would find the trash and put deleted episodes back on the
    air. The route that does the deleting is the one place that knows both
    facts, so it is the place that fixes it.
    """
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\n"
        f'channels:\n  - number: 1\n    name: "Everything"\n    path: {root}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    client = app.test_client()

    res = delete(client, ["sitcoms/sitcoms_ep01.mp4"], confirm="yes")
    assert res.status_code == 200, res.get_json()

    config = load_config(str(cfg))
    channel = config.channels[0]
    for glob in library.MACHINERY_GLOBS:
        assert glob in channel.exclude, (
            f"{glob} is not excluded, so the trash is on the air"
        )
    found = scan_episodes(
        channel.path, config.video_extensions, exclude=channel.exclude
    )
    assert [p.name for p in found] == ["sitcoms_ep02.mp4"]


def test_an_ordinary_channel_is_left_exactly_as_the_customer_wrote_it(box, sent):
    """A config rewrite costs every comment in the file, so it happens only
    when there is something to fix."""
    client, cfg, _ = box
    before = cfg.read_text()
    delete(client, ["sitcoms/sitcoms_ep01.mp4"], confirm="yes")
    assert cfg.read_text() == before


# ==========================================================================
# The trash view
# ==========================================================================
def test_the_trash_lists_what_was_deleted_newest_first_with_its_size(box, sent):
    client, _, root = box
    delete(client, ["sitcoms/sitcoms_ep01.mp4"], confirm="yes")
    delete(client, ["movies"], confirm="yes")
    data = client.get("/api/library/trash").get_json()
    assert [i["name"] for i in data["items"]] == ["movies", "sitcoms_ep01.mp4"]
    assert data["usage"]["items"] == 2
    assert data["usage"]["bytes"] >= 0
    assert data["items"][0]["from"] == ""
    assert data["items"][1]["from"] == "sitcoms"


def test_the_trash_is_listed_in_the_library_but_cannot_be_picked(box, sent):
    """Somebody hunting forty missing gigabytes has to be able to see where
    they went. Seeing the box's own folders is the point; picking them is how
    a television gets broken."""
    client, _, _ = box
    delete(client, ["movies"], confirm="yes")
    data = client.get("/api/library").get_json()
    row = next(e for e in data["entries"] if e["name"] == library.TRASH_NAME)
    assert row["kind"] == "system"
    assert row["selectable"] is False
    assert row["note"]


def test_the_trash_is_empty_and_says_so_on_a_box_that_has_deleted_nothing(box):
    client, _, _ = box
    data = client.get("/api/library/trash").get_json()
    assert data["items"] == []
    assert data["usage"]["items"] == 0


def test_restoring_puts_it_back_and_tells_the_television(box, sent):
    client, _, root = box
    token = delete(client, ["movies"], confirm="yes").get_json()["deleted"][0]["token"]
    sent.clear()
    res = client.post("/api/library/trash/restore", json={"token": token})
    assert res.status_code == 200, res.get_json()
    assert (root / "movies" / "movies_ep01.mp4").exists()
    assert "reload" in sent


def test_restoring_onto_a_name_that_came_back_refuses_and_offers_to_replace(box, sent):
    client, _, root = box
    token = delete(
        client, ["sitcoms/sitcoms_ep01.mp4"], confirm="yes"
    ).get_json()["deleted"][0]["token"]
    (root / "sitcoms" / "sitcoms_ep01.mp4").write_bytes(b"a newer one")

    res = client.post("/api/library/trash/restore", json={"token": token})
    assert res.status_code == 409
    assert "trash, not away" in res.get_json()["error"]

    guarded = client.post(
        "/api/library/trash/restore", json={"token": token, "replace": True}
    )
    assert guarded.status_code == 400, "replacing a file needs the confirmation"

    done = client.post(
        "/api/library/trash/restore?confirm=yes",
        json={"token": token, "replace": True},
    )
    assert done.status_code == 200, done.get_json()
    assert done.get_json()["replaced"], "the file that was there went to the trash"


def test_restoring_something_that_is_not_in_the_trash_is_a_404(box):
    client, _, _ = box
    res = client.post(
        "/api/library/trash/restore", json={"token": "20240101-000000-deadbeef"}
    )
    assert res.status_code == 404


def test_a_malformed_trash_token_is_refused_before_it_reaches_the_disk(box):
    client, _, _ = box
    res = client.post(
        "/api/library/trash/restore", json={"token": "../../../etc"}
    )
    assert res.status_code == 400


def test_emptying_the_trash_needs_the_confirmation(box, sent):
    client, _, root = box
    delete(client, ["movies"], confirm="yes")
    res = client.delete("/api/library/trash")
    assert res.status_code == 400
    assert client.get("/api/library/trash").get_json()["usage"]["items"] == 1


def test_emptying_the_trash_is_the_thing_that_actually_frees_the_space(box, sent):
    client, _, root = box
    delete(client, ["movies"], confirm="yes")
    res = client.delete("/api/library/trash?confirm=yes")
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["items"] == 1
    assert body["bytes"] >= 0
    assert not (root / library.TRASH_NAME).exists() or not list(
        (root / library.TRASH_NAME).iterdir()
    )


def test_one_item_can_be_purged_without_emptying_the_whole_trash(box, sent):
    client, _, _ = box
    delete(client, ["movies"], confirm="yes")
    token = delete(
        client, ["sitcoms/sitcoms_ep01.mp4"], confirm="yes"
    ).get_json()["deleted"][0]["token"]
    res = client.delete(f"/api/library/trash?confirm=yes&token={token}")
    assert res.status_code == 200, res.get_json()
    left = client.get("/api/library/trash").get_json()
    assert [i["name"] for i in left["items"]] == ["movies"]


# ==========================================================================
# Space, which is the whole reason the trash needs explaining
# ==========================================================================
def test_the_space_route_says_what_is_reclaimable_and_why_deleting_did_not_help(box, sent):
    client, _, _ = box
    delete(client, ["movies"], confirm="yes")
    data = client.get("/api/library/space").get_json()
    assert data["trash_items"] == 1
    assert data["reclaimable_bytes"] >= data["trash_bytes"]
    assert "empty the trash" in data["note"].lower()


def test_the_system_page_carries_the_trash_so_it_never_becomes_mystery_usage(box, sent):
    client, _, _ = box
    delete(client, ["movies"], confirm="yes")
    report = client.get("/api/system").get_json()
    assert report["trash"]["items"] == 1
    assert "bytes" in report["trash"]


def test_the_support_bundle_mentions_the_trash(box, sent):
    client, _, _ = box
    delete(client, ["movies"], confirm="yes")
    text = client.get("/api/system/support").get_data(as_text=True)
    assert "trash" in text.lower()


# ==========================================================================
# Renaming
# ==========================================================================
def test_renaming_a_folder_repoints_the_channel_and_tells_the_television(box, sent):
    client, cfg, root = box
    res = client.post("/api/library/rename", json={"path": "sitcoms", "name": "Comedy"})
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["channels"] == [2]
    assert (root / "Comedy").is_dir()
    assert not (root / "sitcoms").exists()
    assert "reload" in sent
    config = load_config(str(cfg))
    assert next(c for c in config.channels if c.number == 2).path == root / "Comedy"


def test_renaming_a_folder_to_the_name_it_already_has_changes_nothing(box, sent):
    client, _, _ = box
    body = client.post(
        "/api/library/rename", json={"path": "sitcoms", "name": "sitcoms"}
    ).get_json()
    assert body["unchanged"] is True


def test_renaming_onto_a_folder_that_already_exists_is_a_409(box, sent):
    client, _, root = box
    res = client.post("/api/library/rename", json={"path": "sitcoms", "name": "movies"})
    assert res.status_code == 409
    assert (root / "sitcoms").is_dir()


def test_renaming_the_library_itself_is_refused(box, sent):
    client, _, _ = box
    res = client.post("/api/library/rename", json={"path": "", "name": "anything"})
    assert res.status_code == 400


@pytest.mark.parametrize("name", ["../evil", "/evil", ".welcome", ""])
def test_renaming_refuses_a_hostile_new_name(box, name):
    client, _, _ = box
    res = client.post("/api/library/rename", json={"path": "sitcoms", "name": name})
    assert res.status_code == 400


def test_a_half_finished_rename_is_reported_verbatim_and_as_a_server_fault(
    box, sent, monkeypatch, caplog
):
    """The one failure that leaves the box in a state nobody can see.

    ``HalfRenamed`` subclasses ``LibraryError``, so a handler that branched on
    except-clause order would answer the worst thing this module can produce
    as a 400 "bad request" and print a tidied-up version of the one message
    that connects a dead channel to a rename.
    """
    client, _, _ = box

    def explode(*a, **k):
        raise library.HalfRenamed("the folder is now called X but config.yaml says Y")

    monkeypatch.setattr("retrobox.webui.library.rename_folder", explode)
    res = client.post("/api/library/rename", json={"path": "sitcoms", "name": "X"})
    assert res.status_code == 500
    assert res.get_json()["error"] == (
        "the folder is now called X but config.yaml says Y"
    )
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_an_ordinary_refusal_is_never_answered_as_a_server_fault(box, monkeypatch):
    """The other half of the same branch: ordinary refusals stay refusals."""
    client, _, _ = box

    def refuse(*a, **k):
        raise library.LibraryError("nothing was changed")

    monkeypatch.setattr("retrobox.webui.library.rename_folder", refuse)
    res = client.post("/api/library/rename", json={"path": "sitcoms", "name": "X"})
    assert res.status_code == 400


def test_renaming_a_folder_an_upload_is_landing_in_is_refused(box, sent):
    client, _, root = box
    started = client.post(
        "/api/uploads",
        json={"channel": 2, "files": [{"path": "new.mp4", "size": 10}]},
    )
    assert started.status_code in (200, 201), started.get_json()
    res = client.post("/api/library/rename", json={"path": "sitcoms", "name": "Comedy"})
    assert res.status_code == 409
    assert (root / "sitcoms").is_dir()


# ==========================================================================
# Housekeeping the box has to do for itself
# ==========================================================================
def test_the_trash_is_swept_when_the_dashboard_starts(tmp_path, runtime, monkeypatch):
    """This box is switched off at the wall, so a timer alone never fires."""
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\n"
        f'channels:\n  - number: 2\n    name: "S"\n    path: {root / "sitcoms"}\n'
    )
    library.move_to_trash(
        root, "sitcoms/sitcoms_ep01.mp4",
        allowed=DEFAULT_VIDEO_EXTENSIONS,
        now=0.0,                       # 1970: older than any sweep window
    )
    assert library.trash_usage(root)["items"] == 1

    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    assert library.trash_usage(root)["items"] == 0


def test_an_unreadable_config_does_not_stop_the_dashboard_starting(tmp_path, runtime):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("channels: [oh dear\n")
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    assert app.test_client().get("/dash").status_code == 200


# ==========================================================================
# The page itself
# ==========================================================================
@pytest.fixture
def page(box):
    client, _, _ = box
    return client.get("/dash").get_data(as_text=True)


def test_the_dashboard_has_a_library_tab(page):
    assert 'data-tab="library"' in page
    assert 'id="tab-library"' in page


def test_the_library_offers_select_all_and_a_count_of_what_is_selected(page):
    assert 'id="lib-all"' in page
    assert 'id="lib-count"' in page


def test_the_delete_button_is_the_same_red_as_reboot_and_factory_reset(page):
    marker = page.index('id="lib-delete"')
    opening = page.rindex("<button", 0, marker)
    assert 'class="danger"' in page[opening:marker], (
        "the button that bins somebody's films has to look like the other "
        "buttons that cannot be undone"
    )


def test_the_library_has_a_trash_view_with_restore_and_empty(page):
    assert 'id="lib-trash"' in page
    assert 'id="lib-empty"' in page


def test_the_library_tab_is_wired_up_and_not_just_drawn(page):
    assert "'library'" in page, "the tab is in the markup but not in the TABS list"
    assert "loadLibrary()" in page


def test_the_stylesheet_dresses_the_file_manager(box):
    from retrobox import webstyle

    css = webstyle.CONSOLE_CSS
    for selector in (".lib ", ".libbar", ".crumbs", ".peril", ".pager"):
        assert selector in css, f"{selector} is unstyled"


def test_the_confirmation_is_styled_in_the_same_red_as_the_danger_buttons(box):
    from retrobox import webstyle

    css = webstyle.CONSOLE_CSS
    peril = css[css.index(".peril {"):css.index(".peril {") + 400]
    assert "var(--red)" in peril, (
        "the panel that confirms a delete has to read as a warning, in the "
        "same colour as Reboot and Factory Reset"
    )
