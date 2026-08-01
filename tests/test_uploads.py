"""Chunked, resumable uploads: the part that has to survive real life.

Home wifi drops, laptops sleep, people close tabs, and the box gets switched
off at the wall. A 40 GB folder upload that restarts from zero on any of those
is not an upload feature, so every test here is about what happens when
something goes wrong halfway through.
"""

import errno
import io
import os
import pathlib
import threading
import time

import pytest

from retrobox.channel import scan_episodes
from retrobox.config import DEFAULT_VIDEO_EXTENSIONS
from retrobox.safepath import UnsafePath
from retrobox.uploads import (
    UploadError,
    UploadLimits,
    UploadStore,
    UploadTarget,
)

CHUNK = 64


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def spool(tmp_path):
    return tmp_path / "media" / ".retrobox-uploads"


@pytest.fixture
def channel_dir(tmp_path):
    folder = tmp_path / "media" / "disney"
    folder.mkdir(parents=True)
    return folder


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def other_drive(tmp_path):
    """A channel folder that is not under the media root - a plugged-in disk.

    Nothing stops somebody adding a channel by hand that points at
    /mnt/usb/Cartoons, and then the spool and the destination are two different
    filesystems.
    """
    folder = tmp_path / "usb" / "Cartoons"
    folder.mkdir(parents=True)
    return folder


def pretend_another_filesystem(monkeypatch, elsewhere):
    """Make a rename into ``elsewhere`` fail the way a second disk does."""
    real = pathlib.Path.replace

    def replace(self, dest):
        # Only a rename that actually crosses the boundary fails; one from the
        # staging name inside that folder to its neighbour is an ordinary
        # same-filesystem rename and must still work.
        crossing = str(dest).startswith(str(elsewhere)) and not str(self).startswith(
            str(elsewhere)
        )
        if crossing:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(self, dest)

    monkeypatch.setattr(pathlib.Path, "replace", replace)


def limits(**kw):
    base = dict(
        chunk_bytes=CHUNK,
        max_file_bytes=10 * 1024 * 1024,
        max_files=10,
        max_sessions=3,
        min_free_bytes=0,
        expiry_seconds=3600.0,
    )
    base.update(kw)
    return UploadLimits(**base)


@pytest.fixture
def store(spool, clock):
    return UploadStore(
        spool, limits(), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock
    )


def target(channel_dir, number=2):
    return UploadTarget(kind="channel", folder=channel_dir, channel_number=number)


def send(store, session, index, chunks_of, *, order=None):
    """Push every chunk of file ``index``, optionally in a given order."""
    total = len(chunks_of)
    for i in (order if order is not None else range(total)):
        store.put_chunk(session.id, index, i, io.BytesIO(chunks_of[i]))


def split(data, size=CHUNK):
    return [data[i:i + size] for i in range(0, len(data), size)]


# ==========================================================================
# The happy path, and the shape of a session
# ==========================================================================
def test_a_session_knows_what_it_is_expecting(store, channel_dir):
    session = store.create(target(channel_dir), [("ep1.mp4", 150)])
    assert session.files[0].relative == "ep1.mp4"
    assert session.files[0].chunks == 3          # 64 + 64 + 22
    assert store.missing(session.id) == {0: [0, 1, 2]}


def test_assembly_produces_a_byte_identical_file(store, channel_dir):
    payload = bytes(range(256)) * 5
    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])
    send(store, session, 0, split(payload))

    results = store.commit(session.id)
    assert (channel_dir / "ep1.mp4").read_bytes() == payload
    assert [r.state for r in results] == ["done"]


def test_a_folder_structure_is_preserved(store, channel_dir):
    payload = b"\xab" * 100
    session = store.create(
        target(channel_dir), [("Disney/S01/ep1.mp4", len(payload))]
    )
    send(store, session, 0, split(payload))
    store.commit(session.id)
    assert (channel_dir / "S01" / "ep1.mp4").read_bytes() == payload


# ==========================================================================
# Out of order, duplicated, resumed
# ==========================================================================
def test_chunks_arriving_out_of_order_still_assemble(store, channel_dir):
    payload = bytes(range(256)) * 3
    parts = split(payload)
    # Deliberately scrambled, and every chunk covered exactly once: browsers
    # retry and reorder, and the assembled file is only correct if the store
    # puts them back by index rather than by arrival.
    scrambled = sorted(range(len(parts)), key=lambda i: (i * 7) % len(parts))
    assert sorted(scrambled) == list(range(len(parts)))
    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])
    send(store, session, 0, parts, order=scrambled)

    store.commit(session.id)
    assert (channel_dir / "ep1.mp4").read_bytes() == payload


def test_a_chunk_sent_twice_is_not_written_twice(store, channel_dir):
    payload = b"\x01" * 150
    parts = split(payload)
    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])
    for i in (0, 0, 1, 1, 1, 2, 0):
        store.put_chunk(session.id, 0, i, io.BytesIO(parts[i]))

    store.commit(session.id)
    assert (channel_dir / "ep1.mp4").read_bytes() == payload


def test_an_interrupted_upload_resumes_where_it_stopped(store, channel_dir):
    payload = b"\x02" * 500
    parts = split(payload)
    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])

    for i in range(3):                          # wifi drops after three chunks
        store.put_chunk(session.id, 0, i, io.BytesIO(parts[i]))

    still_needed = store.missing(session.id)[0]
    assert still_needed == list(range(3, len(parts))), "it would start again"

    for i in still_needed:
        store.put_chunk(session.id, 0, i, io.BytesIO(parts[i]))
    store.commit(session.id)
    assert (channel_dir / "ep1.mp4").read_bytes() == payload


def test_a_session_survives_the_box_being_rebooted(spool, channel_dir, clock):
    # Nothing is held in memory: a brand new store, as if the process had been
    # restarted, must find the session and know exactly which chunks it has.
    payload = b"\x03" * 300
    parts = split(payload)
    first = UploadStore(spool, limits(), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock)
    session = first.create(target(channel_dir), [("ep1.mp4", len(payload))])
    for i in range(2):
        first.put_chunk(session.id, 0, i, io.BytesIO(parts[i]))

    reborn = UploadStore(spool, limits(), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock)
    assert [s.id for s in reborn.sessions()] == [session.id]
    assert reborn.missing(session.id)[0] == list(range(2, len(parts)))

    for i in reborn.missing(session.id)[0]:
        reborn.put_chunk(session.id, 0, i, io.BytesIO(parts[i]))
    reborn.commit(session.id)
    assert (channel_dir / "ep1.mp4").read_bytes() == payload


def test_a_half_written_chunk_does_not_count_as_received(store, channel_dir):
    # Power cut mid-chunk. The staging name is what stops a truncated chunk
    # being treated as delivered and silently corrupting the assembled file.
    payload = b"\x04" * 200
    parts = split(payload)
    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])
    store.put_chunk(session.id, 0, 0, io.BytesIO(parts[0]))

    class Dies(io.BytesIO):
        def read(self, size=-1):
            raise OSError("the wifi went")

    with pytest.raises(OSError):
        store.put_chunk(session.id, 0, 1, Dies())

    assert 1 in store.missing(session.id)[0], "a torn chunk counted as delivered"
    assert store.stray_files(session.id) == [], "a half-written chunk was left"


# ==========================================================================
# Nothing incomplete is ever visible to the television
# ==========================================================================
def test_chunks_live_outside_the_channel_folder(store, channel_dir, spool):
    session = store.create(target(channel_dir), [("ep1.mp4", 200)])
    store.put_chunk(session.id, 0, 0, io.BytesIO(b"\x05" * CHUNK))

    assert list(channel_dir.iterdir()) == [], "chunks were written into the channel"
    assert spool.exists() and any(spool.rglob("*"))


def test_a_partly_uploaded_file_is_not_an_episode(store, channel_dir):
    payload = b"\x06" * 300
    parts = split(payload)
    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])
    for i in range(len(parts) - 1):             # all but the last
        store.put_chunk(session.id, 0, i, io.BytesIO(parts[i]))

    assert scan_episodes(channel_dir, DEFAULT_VIDEO_EXTENSIONS) == []


def test_committing_an_incomplete_file_refuses_rather_than_truncating(store, channel_dir):
    session = store.create(target(channel_dir), [("ep1.mp4", 300)])
    store.put_chunk(session.id, 0, 0, io.BytesIO(b"\x07" * CHUNK))

    with pytest.raises(UploadError):
        store.commit(session.id)
    assert scan_episodes(channel_dir, DEFAULT_VIDEO_EXTENSIONS) == []


def test_the_assembled_file_appears_all_at_once(store, channel_dir, monkeypatch):
    # Assembly happens in the spool and lands by rename, so the scanner never
    # sees a file growing.
    import pathlib

    payload = b"\x08" * 300
    seen = []
    original = pathlib.Path.replace

    def spy(self, dest):
        seen.append(scan_episodes(channel_dir, DEFAULT_VIDEO_EXTENSIONS))
        return original(self, dest)

    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])
    send(store, session, 0, split(payload))
    monkeypatch.setattr(pathlib.Path, "replace", spy)
    store.commit(session.id)

    assert seen, "nothing was renamed into place"
    assert seen[-1] == [], "the file was scannable before it was finished"


# ==========================================================================
# Everything from the network is hostile
# ==========================================================================
@pytest.mark.parametrize(
    "name",
    [
        "../../../etc/systemd/system/evil.service",
        "Disney/../../evil.mp4",
        "/etc/evil.mp4",
        "evil.mp4\x00.sh",
        "..",
        "Disney/.ssh/authorized_keys",
    ],
)
def test_a_traversing_file_path_is_refused_at_session_creation(store, channel_dir, name):
    with pytest.raises(UnsafePath):
        store.create(target(channel_dir), [(name, 100)])


@pytest.mark.parametrize("name", ["evil.sh", "unit.service", "notes.txt", "ep.mp4.sh"])
def test_a_disallowed_extension_never_gets_a_session(store, channel_dir, name):
    with pytest.raises(UnsafePath):
        store.create(target(channel_dir), [(name, 100)])


@pytest.mark.parametrize(
    "sid",
    [
        "../../../etc/passwd",
        "..",
        "a/b",
        "a\\b",
        "sess\x00ion",
        "",
        "   ",
        "x" * 200,
        "%2e%2e%2f",
        ".hidden",
    ],
)
def test_a_hostile_session_id_never_reaches_the_filesystem(store, channel_dir, sid):
    # The session id is a path component, and it arrives in the URL.
    with pytest.raises((UnsafePath, UploadError)):
        store.missing(sid)
    with pytest.raises((UnsafePath, UploadError)):
        store.put_chunk(sid, 0, 0, io.BytesIO(b"x"))
    with pytest.raises((UnsafePath, UploadError)):
        store.commit(sid)


def test_session_ids_are_not_guessable(store, channel_dir):
    ids = {store.create(target(channel_dir), [("ep1.mp4", 10)]).id for _ in range(3)}
    assert len(ids) == 3
    assert all(len(i) >= 16 for i in ids), "a counter would be trivially guessable"


@pytest.mark.parametrize("index", [-1, 99, 10**9])
def test_a_chunk_index_outside_the_file_is_refused(store, channel_dir, index):
    session = store.create(target(channel_dir), [("ep1.mp4", 100)])
    with pytest.raises(UploadError):
        store.put_chunk(session.id, 0, index, io.BytesIO(b"x"))


@pytest.mark.parametrize("index", [-1, 5, 10**9])
def test_a_file_index_outside_the_session_is_refused(store, channel_dir, index):
    session = store.create(target(channel_dir), [("ep1.mp4", 100)])
    with pytest.raises(UploadError):
        store.put_chunk(session.id, index, 0, io.BytesIO(b"x"))


def test_an_oversized_chunk_is_cut_off(store, channel_dir):
    session = store.create(target(channel_dir), [("ep1.mp4", 200)])
    with pytest.raises(UploadError):
        store.put_chunk(session.id, 0, 0, io.BytesIO(b"x" * (CHUNK * 4)))
    assert 0 in store.missing(session.id)[0]
    assert store.stray_files(session.id) == []


# ==========================================================================
# Limits, because there is no login
# ==========================================================================
def test_a_file_bigger_than_the_cap_is_refused(spool, channel_dir, clock):
    store = UploadStore(
        spool, limits(max_file_bytes=1000), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock
    )
    with pytest.raises(UploadError):
        store.create(target(channel_dir), [("huge.mp4", 5000)])


def test_too_many_files_in_one_session_is_refused(spool, channel_dir, clock):
    store = UploadStore(
        spool, limits(max_files=2), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock
    )
    with pytest.raises(UploadError):
        store.create(target(channel_dir), [(f"ep{i}.mp4", 10) for i in range(3)])


def test_too_many_sessions_at_once_is_refused(spool, channel_dir, clock):
    store = UploadStore(
        spool, limits(max_sessions=2), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock
    )
    for _ in range(2):
        store.create(target(channel_dir), [("ep.mp4", 10)])
    with pytest.raises(UploadError):
        store.create(target(channel_dir), [("ep.mp4", 10)])


def test_the_whole_batch_is_weighed_before_a_byte_is_accepted(
    spool, channel_dir, clock, monkeypatch
):
    # Per-file checks pass happily while the batch as a whole fills the disk.
    from types import SimpleNamespace

    import retrobox.uploads as uploads_mod

    monkeypatch.setattr(
        uploads_mod.shutil, "disk_usage",
        lambda p: SimpleNamespace(total=10**9, used=0, free=1000),
    )
    store = UploadStore(
        spool, limits(min_free_bytes=500), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock
    )
    with pytest.raises(UploadError) as caught:
        store.create(target(channel_dir), [(f"ep{i}.mp4", 200) for i in range(5)])
    assert "space" in str(caught.value).lower()
    assert not spool.exists() or list(spool.iterdir()) == [], "it staged anyway"


def test_space_is_rechecked_while_the_upload_runs(spool, channel_dir, clock, monkeypatch):
    # Something else on the box may be eating the disk at the same time.
    from types import SimpleNamespace

    import retrobox.uploads as uploads_mod

    free = {"bytes": 10**9}
    monkeypatch.setattr(
        uploads_mod.shutil, "disk_usage",
        lambda p: SimpleNamespace(total=10**9, used=0, free=free["bytes"]),
    )
    store = UploadStore(
        spool, limits(min_free_bytes=500), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock
    )
    session = store.create(target(channel_dir), [("ep1.mp4", 200)])
    store.put_chunk(session.id, 0, 0, io.BytesIO(b"x" * CHUNK))

    free["bytes"] = 100                          # the disk filled up under us
    with pytest.raises(UploadError):
        store.put_chunk(session.id, 0, 1, io.BytesIO(b"x" * CHUNK))


# ==========================================================================
# Abandoned uploads must not own the disk forever
# ==========================================================================
def test_an_abandoned_session_is_swept_after_its_time(store, channel_dir, clock):
    session = store.create(target(channel_dir), [("ep1.mp4", 200)])
    store.put_chunk(session.id, 0, 0, io.BytesIO(b"x" * CHUNK))
    assert store.reclaimable() > 0

    clock.advance(3600.0 + 1)
    assert store.sweep() > 0
    assert store.sessions() == []
    assert store.reclaimable() == 0


def test_a_session_still_being_uploaded_is_not_swept(store, channel_dir, clock):
    session = store.create(target(channel_dir), [("ep1.mp4", 400)])
    for i in range(3):
        clock.advance(3000.0)                    # slow, but still going
        store.put_chunk(session.id, 0, i, io.BytesIO(b"x" * CHUNK))
        store.sweep()

    assert [s.id for s in store.sessions()] == [session.id], "an active upload was binned"


def test_the_sweep_happens_after_a_restart_too(spool, channel_dir, clock):
    # The box was powered off mid-upload. Nothing runs a timer while it is off,
    # so start-up is the only chance to notice.
    first = UploadStore(spool, limits(), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock)
    session = first.create(target(channel_dir), [("ep1.mp4", 200)])
    first.put_chunk(session.id, 0, 0, io.BytesIO(b"x" * CHUNK))

    clock.advance(3600.0 + 1)
    reborn = UploadStore(
        spool, limits(), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock, sweep_on_start=True
    )
    assert reborn.sessions() == []
    assert not (spool / session.id).exists()


def test_cancelling_takes_the_chunks_with_it(store, channel_dir, spool):
    session = store.create(target(channel_dir), [("ep1.mp4", 200)])
    store.put_chunk(session.id, 0, 0, io.BytesIO(b"x" * CHUNK))
    store.cancel(session.id)

    assert store.sessions() == []
    assert not (spool / session.id).exists()
    assert list(channel_dir.iterdir()) == []


def test_junk_in_the_spool_does_not_break_the_sweep(store, channel_dir, spool, clock):
    spool.mkdir(parents=True, exist_ok=True)
    (spool / "not-a-session").mkdir()
    (spool / "stray.txt").write_text("hello")

    store.create(target(channel_dir), [("ep1.mp4", 10)])
    assert len(store.sessions()) == 1, "junk was mistaken for a session"
    clock.advance(3600.0 + 1)
    store.sweep()


# ==========================================================================
# Duplicates are the user's call, never ours
# ==========================================================================
def test_an_existing_episode_is_reported_not_overwritten(store, channel_dir):
    (channel_dir / "ep1.mp4").write_bytes(b"the original")
    session = store.create(target(channel_dir), [("ep1.mp4", 100)])
    assert session.files[0].duplicate is True


def test_skipping_a_duplicate_leaves_the_original_alone(store, channel_dir):
    (channel_dir / "ep1.mp4").write_bytes(b"the original")
    payload = b"\x09" * 100
    session = store.create(
        target(channel_dir), [("ep1.mp4", len(payload))], actions={0: "skip"}
    )
    results = store.commit(session.id)

    assert (channel_dir / "ep1.mp4").read_bytes() == b"the original"
    assert [r.state for r in results] == ["skipped"]


def test_replacing_a_duplicate_is_allowed_when_asked_for(store, channel_dir):
    (channel_dir / "ep1.mp4").write_bytes(b"the original")
    payload = b"\x0a" * 100
    session = store.create(
        target(channel_dir), [("ep1.mp4", len(payload))], actions={0: "replace"}
    )
    send(store, session, 0, split(payload))
    store.commit(session.id)
    assert (channel_dir / "ep1.mp4").read_bytes() == payload


def test_an_unasked_for_duplicate_does_not_clobber_the_original(store, channel_dir):
    # No decision recorded: the safe reading is "leave what is there".
    (channel_dir / "ep1.mp4").write_bytes(b"the original")
    payload = b"\x0b" * 100
    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])
    send(store, session, 0, split(payload))
    results = store.commit(session.id)

    assert (channel_dir / "ep1.mp4").read_bytes() == b"the original"
    assert results[0].state == "skipped"


# ==========================================================================
# Something that will not play
# ==========================================================================
def test_a_file_with_no_video_is_flagged_and_kept(store, channel_dir, monkeypatch):
    import retrobox.uploads as uploads_mod
    from retrobox.probe import MediaInfo

    monkeypatch.setattr(
        uploads_mod.probe, "probe_media", lambda p, **k: MediaInfo(120.0, False)
    )
    payload = b"\x0c" * 100
    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])
    send(store, session, 0, split(payload))
    results = store.commit(session.id)

    assert results[0].state == "no video"
    assert (channel_dir / "ep1.mp4").read_bytes() == payload, (
        "the box deleted the user's file instead of telling them"
    )


def test_a_file_we_cannot_probe_is_not_condemned(store, channel_dir, monkeypatch):
    import retrobox.uploads as uploads_mod
    from retrobox.probe import MediaInfo

    monkeypatch.setattr(uploads_mod.probe, "probe_media", lambda p, **k: MediaInfo())
    payload = b"\x0d" * 100
    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])
    send(store, session, 0, split(payload))
    assert store.commit(session.id)[0].state == "done"


def test_a_chunk_that_is_the_wrong_length_is_refused(store, channel_dir):
    # Every chunk of a file of known size has a known exact length. A short
    # chunk stored as complete is the one failure that corrupts the middle of
    # an assembled film silently, so the length is checked rather than trusted.
    session = store.create(target(channel_dir), [("ep1.mp4", CHUNK * 3)])
    with pytest.raises(UploadError):
        store.put_chunk(session.id, 0, 0, io.BytesIO(b"x" * (CHUNK - 1)))
    assert 0 in store.missing(session.id)[0]
    assert store.stray_files(session.id) == []


def test_the_last_chunk_may_be_short_because_it_is_the_remainder(store, channel_dir):
    payload = b"y" * (CHUNK * 2 + 5)
    parts = split(payload)
    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])
    send(store, session, 0, parts)
    store.commit(session.id)
    assert (channel_dir / "ep1.mp4").read_bytes() == payload


def test_a_chunk_never_exists_under_its_real_name_until_it_is_whole(store, channel_dir):
    # The staging rename is what makes a power cut survivable: if the process
    # is killed outright there is no cleanup, so a partial write sitting at the
    # final name would be counted as delivered and corrupt the assembled file.
    session = store.create(target(channel_dir), [("ep1.mp4", CHUNK * 2)])
    chunk_dir = store._file_dir(session.id, 0)
    seen = []

    class Watching(io.BytesIO):
        def read(self, size=-1):
            # Mid-write: what does the chunk directory look like right now?
            seen.append(sorted(p.name for p in chunk_dir.iterdir()))
            return super().read(size)

    store.put_chunk(session.id, 0, 0, Watching(b"z" * CHUNK))

    assert seen, "the stream was never read"
    assert all("0.chunk" not in names for names in seen), (
        "the chunk was written straight to its final name"
    )
    assert store.missing(session.id)[0] == [1], "and it did land once complete"


def test_a_session_where_everything_is_skipped_lands_nothing(store, channel_dir):
    session = store.create(
        target(channel_dir), [("ep1.mp4", 100)], actions={0: "skip"}
    )
    results = store.commit(session.id)
    assert [r.state for r in results] == ["skipped"]
    assert list(channel_dir.iterdir()) == []


# ==========================================================================
# A batch either lands or it does not, and the box says which
# ==========================================================================
def test_a_commit_that_is_short_a_chunk_lands_none_of_the_batch(store, channel_dir):
    # Twenty episodes, one of them a chunk short. Writing the first nineteen
    # into the channel and *then* refusing the request is the worst of both:
    # the user is told the upload failed while most of it is already there.
    payload = b"\x11" * 100
    session = store.create(
        target(channel_dir),
        [("ep1.mp4", len(payload)), ("ep2.mp4", len(payload))],
    )
    send(store, session, 0, split(payload))
    store.put_chunk(session.id, 1, 0, io.BytesIO(payload[:CHUNK]))

    with pytest.raises(UploadError) as caught:
        store.commit(session.id)
    assert "missing" in str(caught.value)
    assert list(channel_dir.iterdir()) == [], "part of the batch landed anyway"


def test_the_retry_of_a_short_commit_does_not_call_our_own_files_duplicates(
    store, channel_dir
):
    # The user sends the chunk that was missing and finishes the upload. Every
    # file is theirs and every file is new, so every file must say "done".
    payload = b"\x12" * 100
    parts = split(payload)
    session = store.create(
        target(channel_dir),
        [("ep1.mp4", len(payload)), ("ep2.mp4", len(payload))],
    )
    send(store, session, 0, parts)
    store.put_chunk(session.id, 1, 0, io.BytesIO(parts[0]))
    with pytest.raises(UploadError):
        store.commit(session.id)

    store.put_chunk(session.id, 1, 1, io.BytesIO(parts[1]))
    results = store.commit(session.id)

    assert [r.state for r in results] == ["done", "done"], (
        "the box called the episodes it had just written duplicates"
    )
    assert (channel_dir / "ep1.mp4").read_bytes() == payload
    assert (channel_dir / "ep2.mp4").read_bytes() == payload


def test_a_commit_cut_off_half_way_finishes_on_the_retry(
    store, channel_dir, monkeypatch
):
    # Switched off at the wall part way through a commit: some files landed,
    # the session is still on disk. Finishing it again must complete the job
    # and report honestly, not tell the user their own episodes were skipped
    # as duplicates of themselves.
    import retrobox.uploads as uploads_mod

    payload = b"\x13" * 100
    session = store.create(
        target(channel_dir),
        [("ep1.mp4", len(payload)), ("ep2.mp4", len(payload))],
    )
    send(store, session, 0, split(payload))
    send(store, session, 1, split(payload))

    real_probe = uploads_mod.probe.probe_media
    seen = []

    def dies_on_the_second_file(path, **kw):
        seen.append(path)
        if len(seen) == 2:
            raise RuntimeError("the power went")
        return real_probe(path, **kw)

    monkeypatch.setattr(uploads_mod.probe, "probe_media", dies_on_the_second_file)
    with pytest.raises(RuntimeError):
        store.commit(session.id)
    assert (channel_dir / "ep1.mp4").read_bytes() == payload, "nothing landed at all"

    monkeypatch.setattr(uploads_mod.probe, "probe_media", real_probe)
    results = store.commit(session.id)

    assert [r.state for r in results] == ["done", "done"]
    assert (channel_dir / "ep2.mp4").read_bytes() == payload


def test_a_file_that_appeared_while_the_upload_ran_is_not_called_a_duplicate(
    store, channel_dir
):
    # It was not there when the session was created, so "a file with that name
    # was already there" is not a true account of what happened. Somebody
    # dropped one over the file share while this was uploading.
    payload = b"\x14" * 100
    session = store.create(target(channel_dir), [("ep1.mp4", len(payload))])
    send(store, session, 0, split(payload))
    (channel_dir / "ep1.mp4").write_bytes(b"somebody else's copy")

    result = store.commit(session.id)[0]

    assert result.state == "skipped"
    assert (channel_dir / "ep1.mp4").read_bytes() == b"somebody else's copy"
    assert "while" in result.detail, (
        f"the wording does not match what happened: {result.detail!r}"
    )


# ==========================================================================
# A channel folder on a second disk
# ==========================================================================
def test_a_channel_folder_on_another_drive_still_gets_its_episode(
    store, other_drive, spool, monkeypatch
):
    # media_root is on the internal card; this channel was pointed by hand at a
    # plugged-in drive. A rename cannot cross a filesystem, and an unguarded
    # one comes out of the route as a bare 500 with every uploaded gigabyte
    # stranded in the hidden spool - and it does it again on every retry.
    payload = bytes(range(256)) * 4
    session = store.create(target(other_drive), [("ep1.mp4", len(payload))])
    send(store, session, 0, split(payload))
    pretend_another_filesystem(monkeypatch, other_drive)

    results = store.commit(session.id)

    assert [r.state for r in results] == ["done"]
    assert (other_drive / "ep1.mp4").read_bytes() == payload
    assert [p.name for p in other_drive.iterdir()] == ["ep1.mp4"], "staging left behind"
    assert not any(spool.rglob("*")), "the chunks were stranded in the spool"


def test_a_copy_onto_another_drive_that_dies_leaves_nothing_playable(
    store, other_drive, monkeypatch
):
    # Half a film sitting under its real name is the one failure that looks
    # perfectly fine to the scanner and plays as a broken episode.
    import retrobox.uploads as uploads_mod

    payload = b"\x15" * 300
    session = store.create(target(other_drive), [("ep1.mp4", len(payload))])
    send(store, session, 0, split(payload))
    pretend_another_filesystem(monkeypatch, other_drive)

    real_copy = uploads_mod.shutil.copyfileobj

    def dies_on_the_other_drive(src, dst, *args, **kw):
        if str(getattr(dst, "name", "")).startswith(str(other_drive)):
            dst.write(src.read(50))
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy(src, dst, *args, **kw)

    monkeypatch.setattr(uploads_mod.shutil, "copyfileobj", dies_on_the_other_drive)
    results = store.commit(session.id)

    assert [r.state for r in results] == ["failed"]
    assert scan_episodes(other_drive, DEFAULT_VIDEO_EXTENSIONS) == []
    assert list(other_drive.iterdir()) == [], "a half-copied file was left behind"


def test_the_space_check_looks_at_the_drive_the_files_are_going_to(
    spool, other_drive, clock, monkeypatch
):
    # The spool is on the internal card with room to spare; the channel is on a
    # nearly-full drive. Measuring only the spool accepts the upload and fails
    # at the very end, after the customer has waited for the whole transfer.
    from types import SimpleNamespace

    import retrobox.uploads as uploads_mod

    def usage(path):
        if str(path).startswith(str(other_drive.parent)):
            return SimpleNamespace(total=10**9, used=0, free=1000)
        return SimpleNamespace(total=10**9, used=0, free=10**9)

    monkeypatch.setattr(uploads_mod.shutil, "disk_usage", usage)
    store = UploadStore(
        spool, limits(min_free_bytes=500), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock
    )

    with pytest.raises(UploadError) as caught:
        store.create(target(other_drive), [("ep1.mp4", 5000)])
    assert "space" in str(caught.value).lower()


# ==========================================================================
# Two phones uploading at once
# ==========================================================================
def test_every_store_on_one_spool_shares_the_same_lock(spool, clock):
    # The dashboard builds a store per request on purpose - nothing is held in
    # memory, which is what makes a mid-upload reboot survivable. A lock made
    # in __init__ is therefore a different object on every request and
    # serialises nothing at all.
    first = UploadStore(spool, limits(), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock)
    second = UploadStore(spool, limits(), allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock)

    assert first._lock is second._lock


def test_two_phones_starting_at_once_cannot_get_past_the_session_cap(
    spool, channel_dir, clock
):
    # Flask is threaded, so this is two real requests. The cap is what keeps an
    # endpoint with no login from filling the disk past the reserve.
    kw = dict(allowed=DEFAULT_VIDEO_EXTENSIONS, clock=clock)
    phone_a = UploadStore(spool, limits(max_sessions=1), **kw)
    phone_b = UploadStore(spool, limits(max_sessions=1), **kw)

    outcome = {}
    finished = threading.Event()
    real_sessions = phone_a.sessions
    interrupted = []

    def run_b():
        try:
            outcome["b"] = phone_b.create(target(channel_dir), [("ep2.mp4", 10)])
        except UploadError as exc:
            outcome["b"] = exc
        finished.set()

    def sessions_then_let_b_in():
        found = real_sessions()
        if not interrupted:
            interrupted.append(True)
            threading.Thread(target=run_b, daemon=True).start()
            # If the two requests are properly serialised this wait times out,
            # which is the whole point: B is parked until A has finished.
            finished.wait(timeout=1.0)
        return found

    phone_a.sessions = sessions_then_let_b_in
    try:
        outcome["a"] = phone_a.create(target(channel_dir), [("ep1.mp4", 10)])
    except UploadError as exc:
        outcome["a"] = exc
    finished.wait(timeout=5.0)

    assert interrupted, "the race never happened; the test proves nothing"
    assert len(phone_a.sessions()) == 1, (
        "both requests got a session past a cap of one"
    )
    assert sum(isinstance(v, UploadError) for v in outcome.values()) == 1


def test_a_sweep_does_not_bin_a_session_that_is_being_created(store, spool):
    # A second request sweeps in the window between the session directory being
    # made and its manifest being written. A brand new directory with no
    # manifest yet is not rubbish - it is somebody's upload, one instruction
    # from existing, and deleting it hands them a 500 for nothing.
    spool.mkdir(parents=True, exist_ok=True)
    directory = spool / ("A" * 24)
    directory.mkdir()

    store.sweep()

    assert directory.is_dir(), "a session was deleted while it was being created"


def test_a_husk_with_no_manifest_is_still_reclaimed_once_it_is_old(store, spool, clock):
    # The other half of it: a power cut during a cancel can leave chunks behind
    # with the manifest already gone, and those are real gigabytes.
    spool.mkdir(parents=True, exist_ok=True)
    directory = spool / ("B" * 24)
    (directory / "f0").mkdir(parents=True)
    (directory / "f0" / "0.chunk").write_bytes(b"z" * 500)
    long_ago = time.time() - (5 * 3600.0)
    os.utime(directory, (long_ago, long_ago))

    freed = store.sweep()

    assert freed >= 500
    assert not directory.exists()
