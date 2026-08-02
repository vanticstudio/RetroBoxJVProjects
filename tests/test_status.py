"""Where the TV and the dashboard meet, when the box is not being kind.

The two processes share exactly two files - status.json and control.sock - and
they find them by agreeing on a directory. They are separate processes started
at different moments by systemd, so "agreeing" has to hold no matter which one
starts first and no matter what has happened to /run/user in between.

Every test here is about that agreement surviving a runtime directory that is
missing, unwritable, or arrives late.
"""

import json
import os
import stat
import tempfile

import pytest

from retrobox import status as status_mod


@pytest.fixture(autouse=True)
def a_box_with_no_overrides(monkeypatch):
    """Start every test from the state a real box boots in.

    The overrides are what the rest of the suite uses to redirect these files
    into its own tmp_path, so they have to be cleared here or the thing under
    test never runs.
    """
    monkeypatch.delenv("RETROBOX_STATUS_PATH", raising=False)
    monkeypatch.delenv("RETROBOX_CONTROL_SOCKET", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)


@pytest.fixture
def fallback_base(tmp_path, monkeypatch):
    """Point the last-resort temp directory somewhere this test owns."""
    base = tmp_path / "tmp"
    base.mkdir()
    # tempfile.gettempdir() caches its answer in this module global, so this is
    # the only reliable way to redirect it mid-process.
    monkeypatch.setattr(tempfile, "tempdir", str(base))
    return base


def test_the_status_file_and_the_control_socket_always_sit_in_one_directory(
    fallback_base,
):
    assert status_mod.status_path().parent == status_mod.control_socket_path().parent


def test_the_runtime_directory_falls_back_to_a_temp_directory_when_xdg_runtime_dir_is_unset(
    fallback_base,
):
    resolved = status_mod.runtime_dir()

    assert fallback_base in resolved.parents


def test_the_runtime_directory_ignores_xdg_runtime_dir_when_that_directory_does_not_exist(
    tmp_path, fallback_base, monkeypatch
):
    """The exact case the systemd units guarantee.

    Both units set XDG_RUNTIME_DIR=/run/user/<uid> unconditionally, and without
    linger logind never creates it. A box in this state used to put the status
    file and the socket inside a directory that could not be made, so neither
    ever appeared and the dashboard sat there reporting a dead TV.
    """
    missing = tmp_path / "run" / "user" / "1000"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(missing))

    resolved = status_mod.runtime_dir()

    assert missing not in resolved.parents
    assert fallback_base in resolved.parents


def test_the_runtime_directory_ignores_xdg_runtime_dir_when_that_directory_cannot_be_written_to(
    tmp_path, fallback_base, monkeypatch
):
    unwritable = tmp_path / "readonly"
    unwritable.mkdir()
    unwritable.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(unwritable))
    try:
        resolved = status_mod.runtime_dir()
    finally:
        unwritable.chmod(stat.S_IRWXU)

    assert unwritable not in resolved.parents
    assert fallback_base in resolved.parents


def test_the_runtime_directory_ignores_a_relative_xdg_runtime_dir(
    fallback_base, monkeypatch
):
    """A relative path would resolve against whatever the process's cwd is,
    and the two units do not have to share one."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "run/user/1000")

    assert fallback_base in status_mod.runtime_dir().parents


def test_the_runtime_directory_uses_xdg_runtime_dir_when_it_exists_and_is_writable(
    tmp_path, fallback_base, monkeypatch
):
    """The provisioned box - linger enabled, /run/user/<uid> really there -
    must keep behaving exactly as it always has."""
    good = tmp_path / "run" / "user" / "1000"
    good.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(good))

    assert status_mod.runtime_dir() == good / "retrobox"


def test_the_dashboard_follows_the_tv_into_the_fallback_when_the_runtime_directory_turns_up_later(
    tmp_path, fallback_base, monkeypatch
):
    """The hazard this whole change exists for.

    The TV starts at boot with no /run/user and lands in the fallback. Moments
    later logind creates /run/user/<uid> and the dashboard starts. If the
    dashboard simply preferred the now-usable runtime directory, the two would
    be in different places and the box would look broken in exactly the way it
    did before - dashboard up, TV playing, status panel empty.
    """
    late = tmp_path / "run" / "user" / "1000"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(late))

    # The TV starts first, with nothing at /run/user/1000.
    assert status_mod.write_status({"channel": 3}) is True
    written_to = status_mod.status_path()

    # logind catches up.
    late.mkdir(parents=True)

    # The dashboard starts now, and must land on the TV's file.
    assert status_mod.status_path() == written_to
    assert status_mod.read_status()["channel"] == 3


def test_the_tv_keeps_writing_where_it_started_when_the_runtime_directory_turns_up_later(
    tmp_path, fallback_base, monkeypatch
):
    """The same sequence from the writer's side: the TV must not migrate
    mid-run and leave the dashboard reading a file nobody updates any more."""
    late = tmp_path / "run" / "user" / "1000"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(late))

    status_mod.write_status({"channel": 3})
    first = status_mod.status_path()

    late.mkdir(parents=True)

    status_mod.write_status({"channel": 4})
    assert status_mod.status_path() == first
    assert json.loads(first.read_text())["channel"] == 4


def test_a_control_socket_on_its_own_holds_both_processes_in_the_same_directory(
    tmp_path, fallback_base, monkeypatch
):
    """The TV binds the socket before it has written a single snapshot, so a
    lone socket has to be enough evidence of where the handshake lives."""
    late = tmp_path / "run" / "user" / "1000"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(late))

    bound = status_mod.control_socket_path()
    bound.parent.mkdir(parents=True, exist_ok=True)
    bound.touch()

    late.mkdir(parents=True)

    assert status_mod.control_socket_path() == bound
    assert status_mod.status_path().parent == bound.parent


def test_an_explicit_status_path_override_wins_even_with_no_runtime_directory(
    tmp_path, fallback_base, monkeypatch
):
    """Most of the suite, and the documented development recipe, depend on
    this override being the last word."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("RETROBOX_STATUS_PATH", str(tmp_path / "mine.json"))

    assert status_mod.status_path() == tmp_path / "mine.json"
    assert status_mod.write_status({"channel": 9}) is True
    assert status_mod.read_status()["channel"] == 9


def test_an_explicit_control_socket_override_wins_even_with_no_runtime_directory(
    tmp_path, fallback_base, monkeypatch
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", str(tmp_path / "mine.sock"))

    assert status_mod.control_socket_path() == tmp_path / "mine.sock"


def test_the_fallback_directory_is_readable_only_by_the_box_user(fallback_base):
    """The fallback is inside a world-writable /tmp, and the control socket
    takes commands with no authentication of any kind. Nobody else on the
    machine gets to see it or reach into it."""
    assert status_mod.write_status({"channel": 1}) is True

    mode = stat.S_IMODE(status_mod.runtime_dir().stat().st_mode)
    assert mode == 0o700


def test_the_fallback_directory_is_specific_to_the_user_running_the_box(
    fallback_base,
):
    """/tmp is shared. A directory named after the uid cannot be blocked by a
    leftover one belonging to somebody else."""
    assert str(os.getuid()) in status_mod.runtime_dir().name


def test_a_status_snapshot_round_trips_with_no_runtime_directory_at_all(
    tmp_path, fallback_base, monkeypatch
):
    """The end the customer sees: TV writes, dashboard reads, on a box that
    was installed by the documented manual path and has no linger."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run" / "user" / "1000"))

    assert status_mod.write_status({"channel": 7, "volume": 40}) is True
    snapshot = status_mod.read_status()

    assert snapshot["channel"] == 7
    assert snapshot["schema"] == status_mod.SCHEMA_VERSION


# --- saying whether the picture is running, in words ------------------------
# The dashboard renders these strings as it finds them. They live here so that
# there is one wording rather than one per page, and so that the one thing
# that must never happen to them - a box that has gone quiet being dressed up
# as a box that has gone wrong - can be tested.
def test_a_playing_box_says_so_in_one_plain_word():
    assert status_mod.display_summary(sleeping=False, enabled=True) == "Playing."


def test_a_box_asleep_with_no_display_says_why_without_sounding_broken():
    summary = status_mod.display_summary(
        sleeping=True, enabled=True, state="absent",
    )
    assert summary == (
        "Asleep - nothing is connected to the video output, so playback is paused."
    )
    assert not any(
        word in summary.lower()
        for word in ("error", "fail", "problem", "no signal", "not responding")
    )


def test_a_box_asleep_because_the_television_said_so_says_that_instead():
    assert status_mod.display_summary(
        sleeping=True, enabled=True, state="present", cec="standby",
    ) == "Asleep - the television says it is in standby, so playback is paused."


def test_a_box_held_awake_by_something_else_says_so_rather_than_just_playing():
    assert status_mod.display_summary(
        sleeping=False, enabled=True, holds=["live-viewer"],
    ) == "Playing - something else is watching, so the box is staying awake."


def test_a_box_that_never_goes_quiet_says_that_it_is_switched_off_not_that_it_failed():
    assert status_mod.display_summary(sleeping=False, enabled=False) == (
        "Playing. Going quiet when the television is off is switched off."
    )


def test_asleep_is_still_asleep_when_nobody_can_say_which_way_round_it_is():
    """Never reachable through the app - it only sleeps on a confirmed
    absence - but the sentence must not come out empty if it ever is."""
    assert status_mod.display_summary(sleeping=True, enabled=True) == (
        "Asleep - nothing is watching, so playback is paused."
    )
