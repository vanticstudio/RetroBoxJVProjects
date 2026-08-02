"""Browsing, deleting and renaming the media library from an open dashboard.

Every path in here arrives from a network nobody logged in to. The three
things this file exists to hold down:

* a path from a request never escapes the media root, and never reaches the
  machinery - the trash, the upload spool, the welcome clip;
* deleting never unlinks, so a customer who deletes the wrong series at
  9pm on a Sunday can have it back;
* renaming a folder either moves the folder AND fixes config.yaml, or does
  neither, because a channel pointing at nothing is a fault nobody can trace
  back to the rename that caused it.
"""

import errno
import json
import os
import shutil
import threading
import time

import pytest

from retrobox import library, probe
from retrobox.autochannels import discover_new_channels
from retrobox.channel import scan_episodes
from retrobox.config import DEFAULT_VIDEO_EXTENSIONS, load_config
from retrobox.configstore import ConfigStore
from retrobox.safepath import UnsafePath
from retrobox.uploads import UploadLimits, UploadStore, UploadTarget

EXTS = DEFAULT_VIDEO_EXTENSIONS


# ==========================================================================
# fixtures
# ==========================================================================
@pytest.fixture
def media(tmp_path):
    """A media root shaped like a real one: two channels and the machinery."""
    root = tmp_path / "media"
    (root / "Cartoons" / "Season 1").mkdir(parents=True)
    (root / "Cartoons" / "ep1.mp4").write_bytes(b"a" * 100)
    (root / "Cartoons" / "ep2.mp4").write_bytes(b"b" * 300)
    (root / "Cartoons" / "Season 1" / "ep1.mp4").write_bytes(b"c" * 50)
    (root / "Sitcoms").mkdir()
    (root / "Sitcoms" / "ep1.mp4").write_bytes(b"d" * 700)
    (root / ".welcome").mkdir()
    (root / ".welcome" / "boot_splash.mp4").write_bytes(b"e" * 10)
    return root


@pytest.fixture
def store(tmp_path, media):
    """A ConfigStore over a hand-written config, comments and all."""
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""# hand written, and the customer's own comment
media_root: {media}

channels:
  - number: 2
    name: "Cartoons"
    path: {media / 'Cartoons'}
  - number: 3
    name: "Sitcoms"
    path: {media / 'Sitcoms'}
""",
        encoding="utf-8",
    )
    return ConfigStore(path)


@pytest.fixture
def uploads(media):
    return UploadStore(
        library.upload_spool(media),
        UploadLimits(
            chunk_bytes=64,
            max_file_bytes=10 ** 9,
            max_files=50,
            max_sessions=4,
            min_free_bytes=0,
            expiry_seconds=3600,
        ),
        allowed=EXTS,
    )


@pytest.fixture
def cached_durations(tmp_path, monkeypatch):
    """A duration cache of our own, and an ffprobe that must never be run."""
    monkeypatch.setattr(probe, "CACHE_PATH", tmp_path / "durations.json")
    monkeypatch.setattr(probe, "ffprobe_available", lambda: True)

    def explode(path, timeout):  # pragma: no cover - only runs on failure
        raise AssertionError(f"a listing forked ffprobe for {path}")

    monkeypatch.setattr(probe, "_run_probe", explode)
    probe.reset_cache()
    yield
    probe.reset_cache()


def remember_duration(path, seconds):
    """Put a duration in the probe cache without running anything."""
    stat = path.stat()
    probe._load_cache()[str(path)] = [
        int(stat.st_mtime), stat.st_size, float(seconds), True
    ]


def names(listing):
    return [e["name"] for e in listing["entries"]]


# ==========================================================================
# 1. browsing
# ==========================================================================
def test_browsing_a_folder_reports_each_files_name_size_and_modified_date(media):
    page = library.browse(media, "Cartoons", allowed=EXTS)
    rows = {e["name"]: e for e in page["entries"]}

    assert rows["ep1.mp4"]["bytes"] == 100
    assert rows["ep2.mp4"]["bytes"] == 300
    assert rows["ep1.mp4"]["modified"] == pytest.approx(
        (media / "Cartoons" / "ep1.mp4").stat().st_mtime
    )
    assert rows["Season 1"]["kind"] == "folder"


def test_a_listing_shows_durations_it_already_knows_and_never_probes_for_one(
    media, cached_durations
):
    remember_duration(media / "Cartoons" / "ep1.mp4", 1320.0)

    page = library.browse(media, "Cartoons", allowed=EXTS)
    rows = {e["name"]: e for e in page["entries"]}

    assert rows["ep1.mp4"]["duration"] == 1320.0
    # The other file has never been probed, and rendering a page must not be
    # what probes it - that is one ffprobe per file on a two-core Celeron.
    assert rows["ep2.mp4"]["duration"] is None


def test_a_listing_asks_the_duration_cache_once_per_row_not_once_per_file(
    media, monkeypatch
):
    folder = media / "Big"
    folder.mkdir()
    for i in range(120):
        (folder / f"ep{i:03d}.mp4").write_bytes(b"x")

    asked = []
    monkeypatch.setattr(
        library.probe, "cached_media", lambda p: asked.append(p) or None
    )
    page = library.browse(media, "Big", allowed=EXTS, per_page=20)

    assert len(page["entries"]) == 20
    assert page["total"] == 120
    assert len(asked) == 20, "the whole folder was measured to render one page"


def test_browsing_walks_down_into_a_sub_folder_and_back_up_to_the_root(media):
    down = library.browse(media, "Cartoons/Season 1", allowed=EXTS)
    assert names(down) == ["ep1.mp4"]
    assert down["parent"] == "Cartoons"

    up = library.browse(media, down["parent"], allowed=EXTS)
    assert up["parent"] == ""

    root = library.browse(media, "", allowed=EXTS)
    assert root["parent"] is None, "there is nothing above the media root"


@pytest.mark.parametrize(
    "attempt",
    ["..", "../..", "Cartoons/../..", "/etc", "../etc", "Cartoons/../../etc",
     "~", "Cartoons/../.retrobox-trash"],
)
def test_browsing_refuses_every_way_of_asking_for_somewhere_above_the_media_root(
    media, attempt
):
    with pytest.raises(UnsafePath):
        library.browse(media, attempt, allowed=EXTS)


def test_browsing_refuses_a_symlink_that_points_out_of_the_media_root(media, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (media / "Escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafePath):
        library.browse(media, "Escape", allowed=EXTS)


def test_a_listing_can_be_sorted_by_name_by_size_and_by_date(media):
    folder = media / "Sorting"
    folder.mkdir()
    for name, size, age in [("b.mp4", 30, 300), ("a.mp4", 20, 100), ("c.mp4", 10, 200)]:
        path = folder / name
        path.write_bytes(b"x" * size)
        os.utime(path, (time.time() - age, time.time() - age))

    by_name = library.browse(media, "Sorting", allowed=EXTS, sort="name")
    assert names(by_name) == ["a.mp4", "b.mp4", "c.mp4"]

    by_size = library.browse(media, "Sorting", allowed=EXTS, sort="size")
    assert names(by_size) == ["c.mp4", "a.mp4", "b.mp4"]

    by_date = library.browse(media, "Sorting", allowed=EXTS, sort="date")
    assert names(by_date) == ["b.mp4", "c.mp4", "a.mp4"]

    newest_first = library.browse(media, "Sorting", allowed=EXTS, sort="date",
                                  order="desc")
    assert names(newest_first) == ["a.mp4", "c.mp4", "b.mp4"]


def test_folders_come_before_files_however_the_page_is_sorted(media):
    page = library.browse(media, "Cartoons", allowed=EXTS, sort="size")
    assert names(page)[0] == "Season 1"


def test_paging_through_six_hundred_episodes_gives_each_one_exactly_once(media):
    folder = media / "Huge"
    folder.mkdir()
    for i in range(600):
        (folder / f"ep{i:03d}.mp4").write_bytes(b"x")

    seen = []
    page_number = 1
    while True:
        page = library.browse(media, "Huge", allowed=EXTS, page=page_number,
                              per_page=100)
        seen.extend(names(page))
        if page_number >= page["pages"]:
            break
        page_number += 1

    assert len(seen) == 600
    assert len(set(seen)) == 600
    assert page["pages"] == 6


def test_the_machinery_is_listed_as_system_and_cannot_be_selected(media, uploads):
    library.trash_dir(media).mkdir(parents=True, exist_ok=True)
    library.upload_spool(media).mkdir(parents=True, exist_ok=True)

    page = library.browse(media, "", allowed=EXTS)
    rows = {e["name"]: e for e in page["entries"]}

    for machinery in (library.TRASH_NAME, ".retrobox-uploads", ".welcome"):
        assert rows[machinery]["kind"] == "system", machinery
        assert rows[machinery]["selectable"] is False, machinery
        assert rows[machinery]["note"], f"{machinery} needs to say what it is"

    assert rows["Cartoons"]["selectable"] is True


def test_the_machinery_cannot_be_browsed_into_deleted_or_renamed(media, store, uploads):
    library.trash_dir(media).mkdir(parents=True, exist_ok=True)

    for machinery in (library.TRASH_NAME, ".retrobox-uploads", ".welcome"):
        with pytest.raises(UnsafePath):
            library.browse(media, machinery, allowed=EXTS)
        with pytest.raises(UnsafePath):
            library.move_to_trash(media, machinery, allowed=EXTS)
        with pytest.raises(UnsafePath):
            library.rename_folder(store, media, machinery, "Mine")


def test_browsing_marks_a_file_that_is_not_a_video_as_something_it_will_not_touch(media):
    (media / "Cartoons" / "notes.txt").write_text("hello", encoding="utf-8")

    page = library.browse(media, "Cartoons", allowed=EXTS)
    row = {e["name"]: e for e in page["entries"]}["notes.txt"]

    assert row["kind"] == "other"
    assert row["selectable"] is False


# ==========================================================================
# 2. the trash
# ==========================================================================
def test_deleting_a_file_moves_it_into_the_trash_instead_of_unlinking_it(media):
    original = media / "Cartoons" / "ep1.mp4"
    inode = original.stat().st_ino

    item = library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)

    assert not original.exists()
    landed = library.trashed_payload(media, item["token"])
    assert landed.read_bytes() == b"a" * 100
    # Same inode: the file was renamed inside one filesystem, not copied. On a
    # box where one episode is a gigabyte that is the difference between
    # instant and a minute of disk grinding.
    assert landed.stat().st_ino == inode


def test_two_files_with_the_same_name_from_different_channels_do_not_collide(media):
    first = library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)
    second = library.move_to_trash(media, "Sitcoms/ep1.mp4", allowed=EXTS)

    assert first["token"] != second["token"]
    assert library.trashed_payload(media, first["token"]).read_bytes() == b"a" * 100
    assert library.trashed_payload(media, second["token"]).read_bytes() == b"d" * 700
    assert len(library.list_trash(media)) == 2


def test_the_trash_remembers_which_folder_a_file_came_out_of(media):
    item = library.move_to_trash(media, "Cartoons/Season 1/ep1.mp4", allowed=EXTS)
    assert item["from"] == "Cartoons/Season 1"
    assert item["relative"] == "Cartoons/Season 1/ep1.mp4"

    listed = library.list_trash(media)
    assert listed[0]["from"] == "Cartoons/Season 1"


def test_restoring_puts_a_file_back_exactly_where_it_came_from(media):
    item = library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)
    back = library.restore(media, item["token"])

    assert (media / "Cartoons" / "ep1.mp4").read_bytes() == b"a" * 100
    assert back["relative"] == "Cartoons/ep1.mp4"
    assert library.list_trash(media) == []


def test_restoring_rebuilds_the_folder_when_the_whole_channel_was_deleted(media):
    library.move_to_trash(media, "Cartoons/Season 1/ep1.mp4", allowed=EXTS)
    shutil.rmtree(media / "Cartoons" / "Season 1")

    token = library.list_trash(media)[0]["token"]
    library.restore(media, token)

    assert (media / "Cartoons" / "Season 1" / "ep1.mp4").is_file()


def test_restoring_onto_a_file_that_is_already_there_refuses_rather_than_overwrite(media):
    item = library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)
    (media / "Cartoons" / "ep1.mp4").write_bytes(b"NEW")

    with pytest.raises(library.LibraryConflict):
        library.restore(media, item["token"])

    assert (media / "Cartoons" / "ep1.mp4").read_bytes() == b"NEW"
    assert len(library.list_trash(media)) == 1, "the restore left the trash alone"


def test_restoring_over_something_puts_that_something_in_the_trash_first(media):
    item = library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)
    (media / "Cartoons" / "ep1.mp4").write_bytes(b"NEW")

    result = library.restore(media, item["token"], replace=True)

    assert (media / "Cartoons" / "ep1.mp4").read_bytes() == b"a" * 100
    assert result["replaced"] is not None
    displaced = library.trashed_payload(media, result["replaced"])
    assert displaced.read_bytes() == b"NEW", "nothing is ever destroyed on a restore"


def test_deleting_a_whole_folder_takes_its_episodes_with_it(media):
    item = library.move_to_trash(media, "Cartoons", allowed=EXTS)

    assert not (media / "Cartoons").exists()
    assert item["kind"] == "folder"
    assert item["files"] == 3
    payload = library.trashed_payload(media, item["token"])
    assert (payload / "Season 1" / "ep1.mp4").is_file()

    library.restore(media, item["token"])
    assert (media / "Cartoons" / "Season 1" / "ep1.mp4").is_file()


def test_the_trash_reports_how_much_space_it_is_holding(media):
    assert library.trash_usage(media)["bytes"] == 0

    library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)
    library.move_to_trash(media, "Sitcoms/ep1.mp4", allowed=EXTS)

    usage = library.trash_usage(media)
    assert usage["items"] == 2
    assert usage["bytes"] == 800


def test_emptying_the_trash_on_demand_reclaims_everything_in_it(media):
    library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)
    library.move_to_trash(media, "Sitcoms/ep1.mp4", allowed=EXTS)

    result = library.purge_trash(media)

    assert result["items"] == 2
    assert result["bytes"] == 800
    assert library.list_trash(media) == []


def test_one_item_can_be_purged_without_touching_the_rest(media):
    keep = library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)
    go = library.move_to_trash(media, "Sitcoms/ep1.mp4", allowed=EXTS)

    library.purge_trash(media, token=go["token"])

    assert [i["token"] for i in library.list_trash(media)] == [keep["token"]]


def test_the_trash_purges_itself_after_a_fortnight_and_leaves_this_weeks_alone(media):
    now = 1_700_000_000.0
    old = library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS,
                                now=now - 15 * 86400)
    recent = library.move_to_trash(media, "Sitcoms/ep1.mp4", allowed=EXTS,
                                   now=now - 2 * 86400)

    swept = library.sweep_trash(media, now=now)

    assert swept["items"] == 1
    assert [i["token"] for i in library.list_trash(media)] == [recent["token"]]
    assert old["token"] not in [i["token"] for i in library.list_trash(media)]


def test_how_long_the_trash_is_kept_can_be_changed(media):
    now = 1_700_000_000.0
    library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS,
                          now=now - 3 * 86400)

    assert library.sweep_trash(media, days=7, now=now)["items"] == 0
    assert library.sweep_trash(media, days=1, now=now)["items"] == 1


def test_a_trash_token_from_the_network_is_matched_not_cleaned_up(media):
    for attempt in ["../Cartoons", "..", "/etc", "", "a" * 300, "tok/en"]:
        with pytest.raises(UnsafePath):
            library.restore(media, attempt)
        with pytest.raises(UnsafePath):
            library.purge_trash(media, token=attempt)


def test_a_trashed_episode_is_invisible_to_the_channel_it_was_deleted_from(media):
    library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)

    found = scan_episodes(media / "Cartoons", EXTS)

    # ep2 in the channel folder, then the one in Season 1 - scan order is by
    # full path. The deleted ep1.mp4 is simply not among them.
    assert [p.name for p in found] == ["ep2.mp4", "ep1.mp4"]
    assert [p.parent.name for p in found] == ["Cartoons", "Season 1"]
    assert all(library.TRASH_NAME not in str(p) for p in found)


def test_a_channel_pointed_at_the_media_root_never_airs_the_trash(media):
    """The case that used to be open, now closed at the scanner itself.

    This test was originally written the other way round: it asserted that a
    channel pointed at the media root DID pick trashed episodes back up, because
    ``scan_episodes`` skipped dot-FILES and not dot-FOLDERS, and it published
    ``MACHINERY_GLOBS`` as the way to close that. Its own failure message said
    that if the assertion ever stopped holding, channel.py had been fixed.

    It has. ``channel._is_hidden`` now tests every path part relative to the
    folder being scanned, so a deleted episode is invisible to scanning with no
    exclusions passed at all - which is what a trash has to be, because a
    customer watching something they binned last week has no way to explain it.

    ``MACHINERY_GLOBS`` stays, and is now genuinely belt to that braces: it
    keeps working, and it also covers a caller who scans with a different
    hidden-file rule of their own.
    """
    library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)

    unguarded = scan_episodes(media, EXTS)
    assert all(library.TRASH_NAME not in str(p) for p in unguarded), (
        "a trashed episode is back on the air: " + str(unguarded)
    )
    assert any(p.name == "ep2.mp4" for p in unguarded), "real episodes survive"

    guarded = scan_episodes(media, EXTS, exclude=library.MACHINERY_GLOBS)
    assert all(library.TRASH_NAME not in str(p) for p in guarded)
    assert all(".welcome" not in str(p) for p in guarded)
    assert any(p.name == "ep2.mp4" for p in guarded), "real episodes survive"


def test_the_trash_never_becomes_a_channel_of_its_own(media, store):
    library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)
    config = store.load()

    discovered = discover_new_channels(config)

    assert all(library.TRASH_NAME not in str(c.path) for c in discovered)


def test_the_trash_does_not_appear_as_a_folder_anyone_can_browse_into(media):
    library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)

    page = library.browse(media, "", allowed=EXTS)
    row = {e["name"]: e for e in page["entries"]}[library.TRASH_NAME]

    assert row["kind"] == "system"
    assert row["selectable"] is False
    with pytest.raises(UnsafePath):
        library.browse(media, library.TRASH_NAME, allowed=EXTS)


# ==========================================================================
# 3. renaming
# ==========================================================================
def test_renaming_a_folder_nothing_references_leaves_config_yaml_untouched(media, store):
    (media / "Spare").mkdir()
    before = store.path.read_text(encoding="utf-8")

    result = library.rename_folder(store, media, "Spare", "Extras")

    assert (media / "Extras").is_dir()
    assert not (media / "Spare").exists()
    assert result["channels"] == []
    assert store.path.read_text(encoding="utf-8") == before, (
        "a rename nothing depends on must not rewrite the file and strip "
        "the customer's comments"
    )


def test_renaming_a_channels_folder_moves_the_folder_and_repoints_the_channel(
    media, store
):
    result = library.rename_folder(store, media, "Cartoons", "Saturday Morning")

    assert (media / "Saturday Morning" / "ep1.mp4").is_file()
    assert result["channels"] == [2]

    config = load_config(store.path)
    channel = [c for c in config.channels if c.number == 2][0]
    assert channel.path == media / "Saturday Morning"


def test_a_channel_pointing_inside_the_renamed_folder_is_repointed_too(media, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""media_root: {media}
channels:
  - number: 2
    name: "Season One"
    path: {media / 'Cartoons' / 'Season 1'}
""",
        encoding="utf-8",
    )
    store = ConfigStore(path)

    library.rename_folder(store, media, "Cartoons", "Toons")

    config = load_config(path)
    assert config.channels[0].path == media / "Toons" / "Season 1"


def test_a_daypart_pointing_at_the_renamed_folder_is_repointed_too(media, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""media_root: {media}
channels:
  - number: 2
    name: "Sitcoms"
    path: {media / 'Sitcoms'}
    dayparts:
      - from: "22:00"
        to: "02:00"
        name: "Late Night"
        path: {media / 'Cartoons'}
""",
        encoding="utf-8",
    )
    store = ConfigStore(path)

    result = library.rename_folder(store, media, "Cartoons", "After Dark")

    assert result["dayparts"] == [2]
    config = load_config(path)
    assert config.channels[0].dayparts[0].path == media / "After Dark"


def _config_write_fails(monkeypatch, message="read-only file system"):
    """Break the very last step of ConfigStore.update, after the folder moved."""
    def refuse(path, text):
        raise OSError(message)

    monkeypatch.setattr(library.configstore, "write_config_text", refuse)


def test_the_folder_is_renamed_back_when_the_config_write_fails(media, store, monkeypatch):
    _config_write_fails(monkeypatch)

    with pytest.raises(library.LibraryError):
        library.rename_folder(store, media, "Cartoons", "Saturday Morning")

    assert (media / "Cartoons" / "ep1.mp4").is_file(), "the folder came back"
    assert not (media / "Saturday Morning").exists()
    assert load_config(store.path).channels[0].path == media / "Cartoons"


def test_a_failed_rename_back_says_exactly_what_state_the_box_is_in(
    media, store, monkeypatch
):
    _config_write_fails(monkeypatch, "nope")

    def no_undo(src, dst):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(library, "_rename_back", no_undo)

    with pytest.raises(library.HalfRenamed) as caught:
        library.rename_folder(store, media, "Cartoons", "Saturday Morning")

    message = str(caught.value)
    assert "Cartoons" in message and "Saturday Morning" in message
    assert "2" in message, "it has to name the channel that is now pointing at nothing"


@pytest.mark.parametrize(
    "bad", ["../etc", ".hidden", "a/b", "a\\b", "", "   ", "..", "with\x00null",
            "line\nbreak", "~root", "x" * 300]
)
def test_a_new_name_is_judged_as_strictly_as_an_uploaded_filename(media, store, bad):
    with pytest.raises(UnsafePath):
        library.rename_folder(store, media, "Cartoons", bad)
    assert (media / "Cartoons").is_dir()


def test_renaming_onto_a_folder_that_already_exists_refuses(media, store):
    with pytest.raises(library.LibraryConflict):
        library.rename_folder(store, media, "Cartoons", "Sitcoms")

    assert (media / "Cartoons" / "ep1.mp4").is_file()
    assert (media / "Sitcoms" / "ep1.mp4").is_file()


def test_the_media_root_itself_cannot_be_renamed(media, store):
    with pytest.raises(library.LibraryError):
        library.rename_folder(store, media, "", "Elsewhere")


def test_renaming_a_folder_to_the_name_it_already_has_changes_nothing(media, store):
    before = store.path.read_text(encoding="utf-8")
    result = library.rename_folder(store, media, "Cartoons", "Cartoons")

    assert result["unchanged"] is True
    assert store.path.read_text(encoding="utf-8") == before


def test_which_channels_and_dayparts_reference_a_folder_can_be_asked_before_anything_moves(
    media, store
):
    refs = library.folder_references(store.load(), media / "Cartoons")

    assert refs["channels"] == [2]
    assert refs["dayparts"] == []
    assert refs["used"] is True

    assert library.folder_references(store.load(), media / "Nothing")["used"] is False


# ==========================================================================
# 4. the awkward cases
# ==========================================================================
def test_a_folder_being_uploaded_into_cannot_be_renamed_out_from_under_the_upload(
    media, store, uploads
):
    uploads.create(
        UploadTarget(kind="channel", folder=media / "Cartoons"),
        [("Cartoons/new.mp4", 64)],
    )

    with pytest.raises(library.LibraryBusy):
        library.rename_folder(store, media, "Cartoons", "Toons", uploads=uploads)

    assert (media / "Cartoons").is_dir()


def test_a_folder_being_uploaded_into_cannot_be_deleted_out_from_under_the_upload(
    media, uploads
):
    uploads.create(
        UploadTarget(kind="channel", folder=media / "Cartoons"),
        [("Cartoons/new.mp4", 64)],
    )

    with pytest.raises(library.LibraryBusy):
        library.move_to_trash(media, "Cartoons", allowed=EXTS, uploads=uploads)


def test_cancelling_the_upload_leaves_no_orphaned_chunks_behind(media, store, uploads):
    session = uploads.create(
        UploadTarget(kind="channel", folder=media / "Cartoons"),
        [("Cartoons/new.mp4", 64)],
    )
    uploads.put_chunk(session.id, 0, 0, _stream(b"z" * 64))

    library.rename_folder(store, media, "Cartoons", "Toons", uploads=uploads,
                          cancel_uploads=True)

    assert uploads.sessions() == []
    spool = library.upload_spool(media)
    assert list(spool.glob("**/*.chunk")) == [], "chunks were left behind"
    assert (media / "Toons").is_dir()


def test_an_upload_aimed_below_the_folder_still_counts_as_in_progress(media, uploads):
    uploads.create(
        UploadTarget(kind="new", folder=media / "Cartoons" / "Season 2"),
        [("Season 2/new.mp4", 64)],
    )

    busy = library.uploads_into(uploads, media / "Cartoons")

    assert len(busy) == 1


def test_deleting_a_non_empty_folder_offers_an_exact_count_and_size_first(media):
    plan = library.deletion_plan(media, "Cartoons", allowed=EXTS)

    assert plan["files"] == 3
    assert plan["folders"] == 1
    assert plan["bytes"] == 450
    assert plan["kind"] == "folder"


def test_the_confirmation_says_which_channels_lose_their_folder(media, store, uploads):
    plan = library.deletion_plan(media, "Cartoons", allowed=EXTS,
                                 config=store.load(), uploads=uploads)

    assert plan["references"]["channels"] == [2]
    assert plan["uploads"] == 0
    assert plan["frees_space"] is False


def test_moving_forty_gigabytes_to_the_trash_frees_nothing_and_says_so(media):
    before = library.free_space(media)
    library.move_to_trash(media, "Sitcoms/ep1.mp4", allowed=EXTS)
    after = library.free_space(media)

    assert after["trash_bytes"] == 700
    assert after["reclaimable_bytes"] >= 700
    assert before["free_bytes"] == pytest.approx(after["free_bytes"], abs=10 ** 7)
    assert "trash" in after["note"].lower()


def test_free_space_counts_the_upload_spool_as_reclaimable_too(media, uploads):
    session = uploads.create(
        UploadTarget(kind="channel", folder=media / "Cartoons"),
        [("Cartoons/new.mp4", 64)],
    )
    uploads.put_chunk(session.id, 0, 0, _stream(b"z" * 64))

    space = library.free_space(media, uploads=uploads)

    assert space["spool_bytes"] >= 64
    assert space["reclaimable_bytes"] >= 64


def test_deleting_something_that_is_not_there_says_so_rather_than_failing_oddly(media):
    with pytest.raises(library.LibraryNotFound):
        library.move_to_trash(media, "Cartoons/nope.mp4", allowed=EXTS)
    with pytest.raises(library.LibraryNotFound):
        library.browse(media, "Cartoons/nope", allowed=EXTS)
    with pytest.raises(library.LibraryNotFound):
        library.restore(media, "20200101-000000-abcdef01")


def test_a_file_that_is_not_a_video_cannot_be_deleted_through_this_route(media):
    (media / "Cartoons" / "notes.txt").write_text("x", encoding="utf-8")

    with pytest.raises(UnsafePath):
        library.move_to_trash(media, "Cartoons/notes.txt", allowed=EXTS)

    assert (media / "Cartoons" / "notes.txt").is_file()


def test_a_hand_made_trash_note_cannot_restore_a_file_outside_the_library(media):
    """The trash sits where the LAN file share can write. Treat it that way.

    Nothing stops somebody dropping their own ``item.json`` in there over SMB,
    so the "put it back where it came from" path is a path arriving from the
    network like any other and is checked exactly as hard.
    """
    item = library.trash_dir(media) / "20200101-000000-abcdef01"
    (item / "payload").mkdir(parents=True)
    (item / "payload" / "evil.mp4").write_bytes(b"x")

    for escape in ["../../../etc/cron.d", "..", "/etc", "Cartoons/../..",
                   library.TRASH_NAME]:
        (item / "item.json").write_text(
            json.dumps({"name": "evil.mp4", "from": escape,
                        "relative": f"{escape}/evil.mp4", "kind": "file",
                        "deleted": 0, "bytes": 1}),
            encoding="utf-8",
        )
        with pytest.raises(UnsafePath):
            library.restore(media, "20200101-000000-abcdef01")


def test_a_nonsense_note_in_the_trash_cannot_raise_out_of_a_listing(media):
    """A listing is the one place nothing may ever fail.

    The trash is a folder on a share, and the box is switched off at the wall,
    so a note holding a word where a timestamp belongs is a matter of when.
    """
    real = library.move_to_trash(media, "Cartoons/ep1.mp4", allowed=EXTS)
    junk = library.trash_dir(media) / "20200101-000000-abcdef01"
    (junk / "payload").mkdir(parents=True)
    (junk / "payload" / "x.mp4").write_bytes(b"x" * 9)
    (junk / "item.json").write_text(
        json.dumps({"name": "x.mp4", "from": "", "deleted": "last Tuesday"}),
        encoding="utf-8",
    )

    listed = library.list_trash(media)

    assert {i["token"] for i in listed} == {real["token"], junk.name}
    assert library.trash_usage(media)["bytes"] == 109
    # Undatable, so it counts as ancient and the next sweep reclaims it rather
    # than it sitting in the trash for ever taking up room.
    assert library.sweep_trash(media, now=real["deleted"])["items"] == 1
    assert [i["token"] for i in library.list_trash(media)] == [real["token"]]


def test_a_trash_item_with_no_note_left_by_a_power_cut_is_simply_not_an_item(media):
    orphan = library.trash_dir(media) / "20200101-000000-abcdef01" / "payload"
    orphan.mkdir(parents=True)
    (orphan / "ep1.mp4").write_bytes(b"x" * 5)

    assert library.list_trash(media) == []
    assert library.trash_usage(media)["items"] == 0
    assert library.trash_usage(media)["bytes"] == 0


def test_a_config_that_will_not_load_leaves_the_folder_exactly_where_it_was(
    media, tmp_path
):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"media_root: {media}\nchannels:\n  - name: 'no path at all'\n",
        encoding="utf-8",
    )

    with pytest.raises(library.LibraryError):
        library.rename_folder(ConfigStore(path), media, "Cartoons", "Toons")

    assert (media / "Cartoons" / "ep1.mp4").is_file()
    assert not (media / "Toons").exists()


def test_the_half_renamed_state_is_marked_so_a_route_cannot_call_it_a_bad_request(
    media, store, monkeypatch
):
    _config_write_fails(monkeypatch)
    monkeypatch.setattr(
        library, "_rename_back",
        lambda src, dst: (_ for _ in ()).throw(OSError("permission denied")),
    )

    with pytest.raises(library.LibraryError) as caught:
        library.rename_folder(store, media, "Cartoons", "Toons")

    # Deliberately caught as the ordinary error a route would catch: even a
    # handler that never heard of HalfRenamed must be able to tell that this
    # one is not the customer's fault and not a 400.
    assert caught.value.catastrophic is True
    assert library.LibraryConflict("x").catastrophic is False


def test_asking_for_a_page_past_the_end_gives_the_last_one_rather_than_nothing(media):
    page = library.browse(media, "Cartoons", allowed=EXTS, page=99, per_page=2)

    assert page["page"] == page["pages"] == 2
    assert page["entries"], "an out-of-range page must still show something"


def test_a_deletion_plan_for_a_single_file_reports_just_that_file(media):
    plan = library.deletion_plan(media, "Cartoons/ep2.mp4", allowed=EXTS)

    assert plan["kind"] == "file"
    assert (plan["files"], plan["folders"], plan["bytes"]) == (1, 0, 300)


def test_purging_something_that_is_not_in_the_trash_says_so(media):
    with pytest.raises(library.LibraryNotFound):
        library.purge_trash(media, token="20200101-000000-abcdef01")


def test_a_hidden_file_inside_a_channel_is_shown_as_machinery_not_as_an_episode(media):
    (media / "Cartoons" / ".ep1.mp4.part").write_bytes(b"half an upload")

    page = library.browse(media, "Cartoons", allowed=EXTS)
    row = {e["name"]: e for e in page["entries"]}[".ep1.mp4.part"]

    assert row["kind"] == "system"
    assert row["selectable"] is False
    assert names(page)[-1] == ".ep1.mp4.part", "machinery sorts to the bottom"


def test_no_second_request_can_act_on_the_box_mid_rename(media, store, monkeypatch):
    """Flask serves on threads and there is no password, so this is ordinary.

    Between the folder moving and config.yaml being rewritten the box is
    momentarily inconsistent - the files are at the new name and the config
    still says the old one. Anything else that touches the library in that
    window is working from a picture that is wrong, so it has to wait. This
    catches the second thread in exactly that window and checks that it does.
    """
    real_update = store.update
    got_in = []

    def update(mutate):
        assert (media / "Toons").is_dir() and not (media / "Cartoons").exists(), (
            "this should be running while the box is half-changed"
        )

        def intrude():
            lock = library._lock_for(media)
            if lock.acquire(blocking=False):
                lock.release()
                got_in.append(True)

        thread = threading.Thread(target=intrude)
        thread.start()
        thread.join()
        return real_update(mutate)

    monkeypatch.setattr(store, "update", update)
    library.rename_folder(store, media, "Cartoons", "Toons")

    assert got_in == [], "another request got in while the box was half-renamed"

    # And when it is over, the config agrees with the disk.
    channel = [c for c in load_config(store.path).channels if c.number == 2][0]
    assert channel.path == media / "Toons"
    assert channel.path.is_dir()


class _stream:
    """The smallest thing put_chunk will read from."""

    def __init__(self, data):
        self._data = data

    def read(self, size):
        piece, self._data = self._data[:size], self._data[size:]
        return piece
