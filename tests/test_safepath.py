"""The checks that stand between an unauthenticated upload and the box.

There is no login on the dashboard - that is a settled product decision - so
these are the only thing between someone on the LAN and an arbitrary file
write. An arbitrary file write on this box is a systemd unit or an
authorized_keys, and then it is not your box any more.
"""

import os

import pytest

from retrobox.safepath import UnsafePath, resolve_inside, safe_media_name

VIDEO = (".mp4", ".mkv", ".avi")


# -- filenames -------------------------------------------------------------
def test_an_ordinary_filename_comes_through():
    assert safe_media_name("episode_01.mp4", allowed=VIDEO) == "episode_01.mp4"


def test_the_extension_check_ignores_case():
    assert safe_media_name("Episode.MP4", allowed=VIDEO) == "Episode.MP4"


@pytest.mark.parametrize(
    "name",
    [
        "../../../etc/systemd/system/evil.service",
        "..%2f..%2fevil.mp4",
        "../evil.mp4",
        "..",
        ".",
        "sub/dir/evil.mp4",
        "sub\\dir\\evil.mp4",
        "/etc/cron.d/evil.mp4",
        "/absolute.mp4",
        "C:\\windows\\evil.mp4",
        "evil.mp4\x00.txt",
        "\x00",
        "",
        "   ",
        "\n",
        "evil\r\n.mp4",
        ".hidden.mp4",
        "~/evil.mp4",
        # No separator, real extension, does not start with a dot - none of the
        # other rules catch this one. It is refused anyway, because "reject
        # every name containing .." is a rule you can check by reading, and
        # "reject .. only in the combinations we thought of" is not.
        "ep..1.mp4",
    ],
)
def test_anything_that_could_escape_the_folder_is_refused(name):
    with pytest.raises(UnsafePath):
        safe_media_name(name, allowed=VIDEO)


@pytest.mark.parametrize(
    "name",
    [
        "notavideo.txt",
        "shell.sh",
        "unit.service",
        "authorized_keys",
        "episode.mp4.sh",
        "episode",
        "episode.",
        "episode.mp4.part",
    ],
)
def test_only_video_containers_are_accepted(name):
    with pytest.raises(UnsafePath):
        safe_media_name(name, allowed=VIDEO)


def test_a_silly_long_name_is_refused():
    with pytest.raises(UnsafePath):
        safe_media_name("x" * 300 + ".mp4", allowed=VIDEO)


# -- where the file actually lands -----------------------------------------
def test_a_path_inside_the_folder_is_returned_resolved(tmp_path):
    folder = tmp_path / "sitcoms"
    folder.mkdir()
    assert resolve_inside(folder, folder / "ep.mp4") == (folder / "ep.mp4").resolve()


def test_the_folder_itself_is_not_a_valid_destination(tmp_path):
    folder = tmp_path / "sitcoms"
    folder.mkdir()
    with pytest.raises(UnsafePath):
        resolve_inside(folder, folder)


@pytest.mark.parametrize("spelling", [".", "S01/..", "./.", "S01/S02/../.."])
def test_the_folder_itself_is_refused_as_itself_and_not_as_an_escape(tmp_path, spelling):
    """Naming the folder is its own refusal, with its own answer.

    Every one of these resolves back to the folder, and the containment check
    on its own already rejects them - a folder is never inside itself - so
    dropping the check above it would still raise. What it would lose is the
    sentence. "/media/sitcoms is outside /media/sitcoms" is what a customer
    would be shown for dropping a folder rather than a file into the uploader,
    and there is nothing in that sentence anybody can act on. Worse, it reads
    like a containment bug, which is the one refusal on this box that must
    never be dismissed as a glitch: it is the same message a real escape
    attempt produces, and someone who has learnt to ignore it will ignore that
    one too. The two are different refusals and they say different things.
    """
    folder = tmp_path / "sitcoms"
    folder.mkdir()

    with pytest.raises(UnsafePath) as caught:
        resolve_inside(folder, folder / spelling)

    said = str(caught.value)
    assert "folder itself" in said, (
        f"a folder named as its own destination was refused with {said!r}, "
        f"which describes a different problem"
    )
    assert "outside" not in said, (
        f"refused by the containment check rather than as what it is: {said!r}"
    )


def test_a_path_that_climbs_out_is_refused(tmp_path):
    folder = tmp_path / "sitcoms"
    folder.mkdir()
    with pytest.raises(UnsafePath):
        resolve_inside(folder, folder / ".." / "escaped.mp4")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_writing_through_a_symlink_out_of_the_folder_is_refused(tmp_path):
    # The filename is harmless; the file already sitting there is the attack.
    folder = tmp_path / "sitcoms"
    folder.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (folder / "ep.mp4").symlink_to(outside / "planted.mp4")

    with pytest.raises(UnsafePath):
        resolve_inside(folder, folder / "ep.mp4")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_sibling_folder_with_a_shared_prefix_is_not_inside(tmp_path):
    # /media/show must not be treated as containing /media/show-evil.
    folder = tmp_path / "show"
    folder.mkdir()
    (tmp_path / "show-evil").mkdir()
    with pytest.raises(UnsafePath):
        resolve_inside(folder, tmp_path / "show-evil" / "ep.mp4")


# ==========================================================================
# Relative paths inside an uploaded folder
# ==========================================================================
# A dropped folder hands the browser's webkitRelativePath straight to us. It is
# a string from the network like any other, and it is the one people forget to
# check because it "comes from the file picker".
from retrobox.safepath import safe_relative_path  # noqa: E402


def test_a_plain_relative_path_survives():
    assert safe_relative_path("Disney/S01/ep1.mp4", allowed=VIDEO) == "S01/ep1.mp4"


def test_the_dropped_folders_own_name_is_stripped():
    # The channel folder IS the dropped folder, so keeping it would nest a
    # Disney folder inside the Disney channel.
    assert safe_relative_path("Disney/ep1.mp4", allowed=VIDEO) == "ep1.mp4"


def test_a_bare_filename_is_fine():
    assert safe_relative_path("ep1.mp4", allowed=VIDEO) == "ep1.mp4"


def test_windows_separators_are_understood_not_smuggled():
    assert safe_relative_path("Disney\\S01\\ep1.mp4", allowed=VIDEO) == "S01/ep1.mp4"


def test_a_doubled_separator_is_refused_rather_than_collapsed():
    with pytest.raises(UnsafePath):
        safe_relative_path("Disney\\\\S01\\ep1.mp4", allowed=VIDEO)


@pytest.mark.parametrize(
    "raw",
    [
        "../../../etc/systemd/system/evil.service",
        "Disney/../../../etc/passwd.mp4",
        "Disney/../../evil.mp4",
        "/etc/cron.d/evil.mp4",
        "//etc/evil.mp4",
        "C:\\windows\\evil.mp4",
        "Disney/./../../evil.mp4",
        "Disney/\x00/ep.mp4",
        "Disney/ep.mp4\x00.sh",
        "Disney/.ssh/authorized_keys",
        "Disney/~/evil.mp4",
        "..",
        "",
        "   ",
        "Disney/",
        "Disney//ep.mp4",
    ],
)
def test_a_relative_path_that_could_escape_is_refused(raw):
    with pytest.raises(UnsafePath):
        safe_relative_path(raw, allowed=VIDEO)


def test_a_non_video_inside_a_folder_is_refused():
    with pytest.raises(UnsafePath):
        safe_relative_path("Disney/notes.txt", allowed=VIDEO)


def test_a_silly_deep_path_is_refused():
    deep = "Disney/" + "/".join(f"level{i}" for i in range(12)) + "/ep.mp4"
    with pytest.raises(UnsafePath):
        safe_relative_path(deep, allowed=VIDEO)


def test_the_result_still_has_to_land_inside_the_channel(tmp_path):
    # The belt to safe_relative_path's braces: whatever it returns is joined
    # and resolved against the real folder before anything is written.
    folder = tmp_path / "disney"
    folder.mkdir()
    relative = safe_relative_path("Disney/S01/ep1.mp4", allowed=VIDEO)
    assert resolve_inside(folder, folder / relative) == (folder / "S01/ep1.mp4").resolve()
