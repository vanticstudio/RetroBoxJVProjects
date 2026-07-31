import pytest

from retrobox.autochannels import (
    MARKER,
    apply_auto_channels,
    discover_new_channels,
    write_channels,
)
from retrobox.config import config_from_dict, load_config
from tests.helpers import make_show


def _media(tmp_path, *shows, empty=()):
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    for name in shows:
        make_show(root, name, 3)
    for name in empty:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


# -- discovery -------------------------------------------------------------
def test_finds_folders_that_have_no_channel(tmp_path):
    root = _media(tmp_path, "sitcoms", "movies")
    config = config_from_dict({"media_root": str(root), "auto_channels": True})
    # media_root discovery already claimed both, so nothing is "new".
    assert discover_new_channels(config) == []

    make_show(root, "talk shows", 2)
    found = discover_new_channels(config)
    assert [(c.number, c.name) for c in found] == [(4, "Talk Shows")]


def test_names_are_tidied_up(tmp_path):
    root = _media(tmp_path, "late_night_tv", "music-videos")
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "Keep", "path": str(tmp_path / "other")}],
         "media_root": str(root), "auto_channels": True}
    )
    (tmp_path / "other").mkdir(exist_ok=True)
    names = [c.name for c in discover_new_channels(config)]
    assert names == ["Late Night Tv", "Music Videos"]


def test_numbering_continues_past_existing_channels(tmp_path):
    root = _media(tmp_path, "sitcoms")
    config = config_from_dict(
        {
            "channels": [
                {"number": 2, "name": "A", "path": str(root / "sitcoms")},
                {"number": 9, "name": "B", "path": str(root / "sitcoms")},
            ],
            "media_root": str(root),
            "auto_channels": True,
        }
    )
    make_show(root, "movies", 2)
    assert discover_new_channels(config)[0].number == 10


def test_empty_folders_are_skipped(tmp_path):
    root = _media(tmp_path, "sitcoms", empty=("nothing_here", "also_empty"))
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "A", "path": str(root / "sitcoms")}],
         "media_root": str(root), "auto_channels": True}
    )
    assert discover_new_channels(config) == []


def test_folders_with_only_non_video_files_are_skipped(tmp_path):
    root = _media(tmp_path, "sitcoms")
    junk = root / "subtitles_only"
    junk.mkdir()
    (junk / "notes.txt").write_text("nope")
    (junk / "cover.jpg").write_bytes(b"\x00")
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "A", "path": str(root / "sitcoms")}],
         "media_root": str(root), "auto_channels": True}
    )
    assert discover_new_channels(config) == []


def test_existing_channels_are_never_touched(tmp_path):
    root = _media(tmp_path, "sitcoms", "movies")
    config = config_from_dict(
        {
            # A deliberate name and number on a folder auto-discovery would
            # otherwise have called "Sitcoms" on channel 2.
            "channels": [{"number": 47, "name": "MY CHANNEL",
                          "path": str(root / "sitcoms")}],
            "media_root": str(root),
            "auto_channels": True,
        }
    )
    found = discover_new_channels(config)
    assert [(c.number, c.name) for c in found] == [(48, "Movies")]
    # The hand-set one is untouched.
    assert (config.channels[0].number, config.channels[0].name) == (47, "MY CHANNEL")


def test_daypart_folders_do_not_become_their_own_channel(tmp_path):
    root = _media(tmp_path, "sitcoms", "after_dark")
    config = config_from_dict(
        {
            "channels": [{
                "number": 2, "name": "A", "path": str(root / "sitcoms"),
                "dayparts": [{"from": "22:00", "to": "02:00",
                              "path": str(root / "after_dark")}],
            }],
            "media_root": str(root),
            "auto_channels": True,
        }
    )
    assert discover_new_channels(config) == []


def test_discovery_is_stable_across_runs(tmp_path):
    root = _media(tmp_path, "b_show", "a_show", "c_show")
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "X", "path": str(tmp_path / "x")}],
         "media_root": str(root), "auto_channels": True}
    )
    (tmp_path / "x").mkdir(exist_ok=True)
    first = [(c.number, c.name) for c in discover_new_channels(config)]
    second = [(c.number, c.name) for c in discover_new_channels(config)]
    assert first == second == [(3, "A Show"), (4, "B Show"), (5, "C Show")]


def test_off_by_default(tmp_path):
    root = _media(tmp_path, "sitcoms")
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "A", "path": str(root / "sitcoms")}],
         "media_root": str(root)}
    )
    assert config.auto_channels is False
    merged, added = apply_auto_channels(config, None)
    assert added == [] and merged is config


def test_missing_media_root_is_not_fatal(tmp_path):
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "A", "path": str(tmp_path)}],
         "media_root": str(tmp_path / "gone"), "auto_channels": True}
    )
    assert discover_new_channels(config) == []


def test_no_media_root_configured_is_not_fatal(tmp_path):
    make_show(tmp_path, "a", 1)
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "A", "path": str(tmp_path / "a")}],
         "auto_channels": True}
    )
    assert discover_new_channels(config) == []


# -- writing back ----------------------------------------------------------
def _config_file(tmp_path, body):
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


def test_new_channels_are_spliced_into_the_channels_block(tmp_path):
    root = _media(tmp_path, "sitcoms", "movies")
    path = _config_file(
        tmp_path,
        f"""# a comment at the top
media_root: {root}
auto_channels: true

channels:
  - number: 2
    name: "Sitcoms"
    path: {root / 'sitcoms'}

tune_in: random     # trailing comment
""",
    )
    config = load_config(path)
    merged, added = apply_auto_channels(config, path)

    assert [c.name for c in added] == ["Movies"]
    text = path.read_text()
    # Comments survive - the whole point of splicing rather than re-dumping.
    assert "# a comment at the top" in text
    assert "tune_in: random     # trailing comment" in text
    assert MARKER in text
    # ...and it still parses, with both channels present.
    reloaded = load_config(path)
    assert [(c.number, c.name) for c in reloaded.channels] == [
        (2, "Sitcoms"), (3, "Movies"),
    ]


def test_writing_is_idempotent_across_restarts(tmp_path):
    root = _media(tmp_path, "sitcoms", "movies")
    path = _config_file(
        tmp_path,
        f'media_root: {root}\nauto_channels: true\nchannels:\n'
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n',
    )
    apply_auto_channels(load_config(path), path)
    first = path.read_text()

    # Second start: Movies is now a real channel, so nothing more to add.
    merged, added = apply_auto_channels(load_config(path), path)
    assert added == []
    assert path.read_text() == first, "a second run must not duplicate anything"


def test_creates_a_channels_block_when_there_isnt_one(tmp_path):
    from retrobox.config import ChannelConfig

    root = _media(tmp_path, "sitcoms")
    path = _config_file(tmp_path, f"media_root: {root}\nauto_channels: true\n")
    write_channels(path, [ChannelConfig(number=5, name="Movies",
                                        path=root / "movies")])
    text = path.read_text()
    assert "channels:" in text and "Movies" in text
    assert load_config(path).auto_channels is True, "the rest of the file survives"


def test_media_root_only_config_has_nothing_left_to_discover(tmp_path):
    # media_root discovery already claims every folder at parse time, so
    # auto_channels is a no-op there - it exists for explicit channel lists.
    root = _media(tmp_path, "sitcoms", "movies")
    path = _config_file(tmp_path, f"media_root: {root}\nauto_channels: true\n")
    _merged, added = apply_auto_channels(load_config(path), path)
    assert added == []


def test_write_stops_at_the_next_top_level_key(tmp_path):
    root = _media(tmp_path, "sitcoms")
    path = _config_file(
        tmp_path,
        f"""media_root: {root}
auto_channels: true
channels:
  - number: 2
    name: "Sitcoms"
    path: {root / 'sitcoms'}
tune_in: broadcast
start_channel: 2
""",
    )
    make_show(root, "movies", 2)
    apply_auto_channels(load_config(path), path)
    lines = path.read_text().splitlines()
    # The new entry must land inside the list, above tune_in - not after it.
    assert lines.index("tune_in: broadcast") > lines.index("  - number: 3")
    assert load_config(path).tune_in == "broadcast"


def test_unwritable_config_still_yields_working_channels(tmp_path):
    root = _media(tmp_path, "sitcoms", "movies")
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "A", "path": str(root / "sitcoms")}],
         "media_root": str(root), "auto_channels": True}
    )
    # A directory where a file should be: writing raises OSError.
    bad = tmp_path / "nope"
    bad.mkdir()
    merged, added = apply_auto_channels(config, bad)
    assert [c.name for c in added] == ["Movies"]
    assert len(merged.channels) == 2, "the session still gets the channel"


def test_write_channels_with_nothing_to_add_is_a_no_op(tmp_path):
    path = _config_file(tmp_path, "channels:\n  - number: 2\n    name: A\n    path: /x\n")
    before = path.read_text()
    write_channels(path, [])
    assert path.read_text() == before
