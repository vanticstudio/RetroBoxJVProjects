"""Crash-safety of the config writer.

These are the tests that stand between a power cut and a box that will not
boot, so they are deliberately paranoid about what is left on disk when a
write goes wrong half way through.
"""

import os
import stat

import pytest

from retrobox.configwrite import atomic_write_text, backup_once, write_config_text


def _config(tmp_path, body="original: yes\n"):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _temp_litter(directory):
    """Anything in ``directory`` that isn't a file we deliberately put there."""
    return sorted(
        p.name for p in directory.iterdir()
        if p.name not in ("config.yaml", "config.yaml.bak")
    )


# -- the write itself ------------------------------------------------------
def test_replaces_the_contents_of_an_existing_file(tmp_path):
    path = _config(tmp_path)
    atomic_write_text(path, "replaced: yes\n")
    assert path.read_text(encoding="utf-8") == "replaced: yes\n"


def test_creates_the_file_when_it_is_not_there_yet(tmp_path):
    path = tmp_path / "config.yaml"
    atomic_write_text(path, "fresh: yes\n")
    assert path.read_text(encoding="utf-8") == "fresh: yes\n"


def test_leaves_no_temp_file_behind_when_it_succeeds(tmp_path):
    path = _config(tmp_path)
    atomic_write_text(path, "replaced: yes\n")
    assert _temp_litter(tmp_path) == []


def test_the_temp_file_lives_next_to_the_target(tmp_path):
    # If the staging file is written to the system temp dir instead, os.replace
    # is a cross-device rename and fails outright on a box where /tmp is tmpfs.
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst)

    path = _config(tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", spy)
        atomic_write_text(path, "replaced: yes\n")

    assert seen, "nothing was renamed into place - this is not an atomic write"
    src, dst = seen[-1]
    assert os.path.dirname(src) == str(tmp_path)
    assert dst == str(path)


def test_the_data_and_the_directory_are_both_fsynced(tmp_path):
    # Without the data fsync the file can be empty after a power cut; without
    # the directory fsync the rename itself can be lost.
    kinds = []
    real_fsync = os.fsync

    def spy(fd):
        kinds.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real_fsync(fd)

    path = _config(tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "fsync", spy)
        atomic_write_text(path, "replaced: yes\n")

    assert "file" in kinds, "the staged data was never flushed to the platter"
    assert "dir" in kinds, "the rename was never flushed to the platter"


def test_existing_permissions_survive_a_rewrite(tmp_path):
    # The staging file is created 0600. Letting that mode ride along would
    # quietly lock the web service out of a config it used to be able to read.
    path = _config(tmp_path)
    path.chmod(0o644)
    atomic_write_text(path, "replaced: yes\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


# -- when it goes wrong ----------------------------------------------------
def test_a_failed_rename_leaves_the_original_intact(tmp_path):
    # This is the whole point of the module: the old file must survive a
    # write that dies part way through, rather than being truncated to zero.
    path = _config(tmp_path, "channels:\n  - number: 2\n")

    def boom(src, dst):
        raise OSError("power cut")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write_text(path, "replaced: yes\n")

    assert path.read_text(encoding="utf-8") == "channels:\n  - number: 2\n"


def test_a_failed_write_leaves_no_temp_file_behind(tmp_path):
    path = _config(tmp_path)

    def boom(src, dst):
        raise OSError("power cut")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write_text(path, "replaced: yes\n")

    assert _temp_litter(tmp_path) == []


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0, reason="root ignores directory modes"
)
def test_an_unwritable_directory_raises_without_touching_the_original(tmp_path):
    # The same failure with no monkeypatching at all: a read-only SD card.
    path = _config(tmp_path, "channels:\n  - number: 2\n")
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(OSError):
            atomic_write_text(path, "replaced: yes\n")
        assert path.read_text(encoding="utf-8") == "channels:\n  - number: 2\n"
    finally:
        tmp_path.chmod(0o700)


# -- the one and only backup -----------------------------------------------
ORIGINAL = "# hand written\nchannels:\n  - number: 2\n    name: \"Sitcoms\"\n"


def test_the_original_is_kept_the_first_time_a_config_is_modified(tmp_path):
    path = _config(tmp_path, ORIGINAL)
    write_config_text(path, "rewritten: yes\n")
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == ORIGINAL


def test_the_backup_is_never_overwritten_by_a_later_write(tmp_path):
    # The value of the file is entirely that it predates the automation. A
    # second run replacing it with our own output would destroy the only copy
    # of what the box looked like before any of this touched it.
    path = _config(tmp_path, ORIGINAL)
    write_config_text(path, "first: pass\n")
    write_config_text(path, "second: pass\n")

    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == ORIGINAL
    assert path.read_text(encoding="utf-8") == "second: pass\n"


def test_a_backup_made_by_hand_is_left_alone(tmp_path):
    path = _config(tmp_path, ORIGINAL)
    (tmp_path / "config.yaml.bak").write_text("my own copy\n", encoding="utf-8")
    write_config_text(path, "rewritten: yes\n")
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == "my own copy\n"


def test_backup_once_says_whether_it_was_the_one_that_made_it(tmp_path):
    path = _config(tmp_path, ORIGINAL)
    assert backup_once(path) == tmp_path / "config.yaml.bak"
    assert backup_once(path) is None


def test_there_is_nothing_to_back_up_for_a_config_that_does_not_exist_yet(tmp_path):
    path = tmp_path / "config.yaml"
    assert backup_once(path) is None
    write_config_text(path, "fresh: yes\n")
    assert not (tmp_path / "config.yaml.bak").exists()
    assert path.read_text(encoding="utf-8") == "fresh: yes\n"


def test_the_backup_is_readable_by_whoever_could_read_the_config(tmp_path):
    # It is restored by hand, by a person, months later. A 0600 copy of a 0644
    # file is a copy they cannot get at.
    path = _config(tmp_path, ORIGINAL)
    path.chmod(0o644)
    backup_once(path)
    assert stat.S_IMODE((tmp_path / "config.yaml.bak").stat().st_mode) == 0o644


def test_a_backup_that_fails_part_way_leaves_no_half_written_backup(tmp_path):
    # A truncated .bak would be worse than none at all: it is written once and
    # never revisited, so the corruption would be permanent. Better to leave
    # nothing and let the next run try again.
    path = _config(tmp_path, ORIGINAL)

    def dying_disk(fd):
        raise OSError("EIO")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "fsync", dying_disk)
        with pytest.raises(OSError):
            write_config_text(path, "rewritten: yes\n")

    assert not (tmp_path / "config.yaml.bak").exists()
    assert path.read_text(encoding="utf-8") == ORIGINAL
    assert _temp_litter(tmp_path) == []


def test_write_config_text_leaves_nothing_lying_around(tmp_path):
    path = _config(tmp_path, ORIGINAL)
    write_config_text(path, "rewritten: yes\n")
    assert _temp_litter(tmp_path) == []
