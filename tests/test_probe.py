import json

import pytest

from retrobox import probe


@pytest.fixture
def cache_file(tmp_path, monkeypatch):
    """Point the duration cache at a throwaway file and pretend ffprobe exists."""
    path = tmp_path / "cache" / "durations.json"
    monkeypatch.setattr(probe, "CACHE_PATH", path)
    monkeypatch.setattr(probe, "ffprobe_available", lambda: True)
    probe.reset_cache()
    yield path
    probe.reset_cache()


def _media(tmp_path, name="ep.mp4", data=b"\x00"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_second_probe_comes_from_the_cache(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    calls = []
    monkeypatch.setattr(
        probe, "_run_probe", lambda p, t: (calls.append(p), probe.MediaInfo(1234.0, True))[1]
    )

    assert probe.probe_duration(media) == 1234.0
    assert probe.probe_duration(media) == 1234.0
    assert len(calls) == 1, "the second call should not shell out again"


def test_cache_survives_a_restart(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: probe.MediaInfo(99.0, True))
    probe.probe_duration(media)
    probe.flush_cache()
    assert cache_file.is_file()

    # Fresh process: in-memory state gone, but the file is read back.
    probe.reset_cache()
    calls = []
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: (calls.append(p), probe.MediaInfo(1.0, True))[1])
    assert probe.probe_duration(media) == 99.0
    assert calls == []


def test_cache_is_invalidated_when_the_file_changes(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: probe.MediaInfo(10.0, True))
    assert probe.probe_duration(media) == 10.0

    # Re-encode the file: different size, so the fingerprint no longer matches.
    media.write_bytes(b"\x00" * 4096)
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: probe.MediaInfo(20.0, True))
    assert probe.probe_duration(media) == 20.0


def test_failures_are_never_cached(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: probe.MediaInfo())
    assert probe.probe_duration(media) is None

    # A transient failure must not poison the entry forever.
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: probe.MediaInfo(55.0, True))
    assert probe.probe_duration(media) == 55.0


def test_use_cache_false_always_probes(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    calls = []
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: (calls.append(p), probe.MediaInfo(7.0, True))[1])
    probe.probe_duration(media, use_cache=False)
    probe.probe_duration(media, use_cache=False)
    assert len(calls) == 2


def test_missing_ffprobe_short_circuits(tmp_path, monkeypatch, cache_file):
    monkeypatch.setattr(probe, "ffprobe_available", lambda: False)
    called = []
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: (called.append(p), probe.MediaInfo())[1])
    assert probe.probe_duration(_media(tmp_path)) is None
    assert called == []


def test_flush_survives_an_unwritable_location(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(probe, "CACHE_PATH", blocker / "durations.json")
    monkeypatch.setattr(probe, "ffprobe_available", lambda: True)
    probe.reset_cache()
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: probe.MediaInfo(5.0, True))

    media = _media(tmp_path)
    assert probe.probe_duration(media) == 5.0
    probe.flush_cache()          # read-only / bad path must not raise
    assert probe.probe_duration(media) == 5.0
    probe.reset_cache()


def test_corrupt_cache_file_is_ignored(tmp_path, monkeypatch, cache_file):
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("{ this is not json")
    probe.reset_cache()
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: probe.MediaInfo(42.0, True))
    assert probe.probe_duration(_media(tmp_path)) == 42.0


# ==========================================================================
# Checking an uploaded file will actually play
# ==========================================================================
# Someone uploads an audio-only .mp4, or something renamed to .mp4 that isn't
# one. Finding that out as a black screen on the television at 9pm is the bad
# outcome; the box should say so at upload time.

_WITH_VIDEO = """
{"format": {"duration": "1320.5"},
 "streams": [{"codec_type": "audio"}, {"codec_type": "video"}]}
"""
_AUDIO_ONLY = """
{"format": {"duration": "180.0"}, "streams": [{"codec_type": "audio"}]}
"""
_NO_STREAMS = '{"format": {}, "streams": []}'


def test_a_normal_video_reads_as_playable():
    info = probe._parse_probe(_WITH_VIDEO)
    assert info.duration == 1320.5
    assert info.has_video is True
    assert info.unplayable is False


def test_an_audio_only_file_is_flagged():
    info = probe._parse_probe(_AUDIO_ONLY)
    assert info.has_video is False
    assert info.unplayable is True, "this would be a black screen on the TV"


def test_a_file_with_no_streams_at_all_is_flagged():
    assert probe._parse_probe(_NO_STREAMS).unplayable is True


@pytest.mark.parametrize("junk", ["", "not json", "{}", '{"streams": "nope"}'])
def test_unreadable_probe_output_is_unknown_not_a_verdict(junk):
    # "We could not tell" must never be reported as "this is broken", or a box
    # without ffmpeg would condemn every file uploaded to it.
    info = probe._parse_probe(junk)
    assert info.has_video is None
    assert info.unplayable is False


def test_without_ffprobe_nothing_is_condemned(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ffprobe_available", lambda: False)
    info = probe.probe_media(_media(tmp_path))
    assert info.has_video is None and info.unplayable is False


def test_probe_media_shares_the_duration_cache(tmp_path, monkeypatch, cache_file):
    # One ffprobe run per file, not one for the duration and another for the
    # streams: on a 200-episode folder that is 200 extra processes.
    media = _media(tmp_path)
    calls = []
    monkeypatch.setattr(
        probe, "_run_probe",
        lambda p, t: (calls.append(p), probe._parse_probe(_WITH_VIDEO))[1],
    )

    first = probe.probe_media(media)
    assert first.duration == 1320.5 and first.has_video is True
    assert probe.probe_duration(media) == 1320.5, "the duration came from elsewhere"
    assert probe.probe_media(media).has_video is True
    assert len(calls) == 1, f"ffprobe ran {len(calls)} times for one file"


def test_a_replaced_file_is_probed_again(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    calls = []
    monkeypatch.setattr(
        probe, "_run_probe",
        lambda p, t: (calls.append(p), probe._parse_probe(_WITH_VIDEO))[1],
    )
    probe.probe_media(media)
    media.write_bytes(b"\x00" * 500)          # different size -> new fingerprint
    probe.probe_media(media)
    assert len(calls) == 2


# ==========================================================================
# Looking up what we already know, without ever starting a process
# ==========================================================================
def test_a_cached_lookup_answers_from_the_cache(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    monkeypatch.setattr(
        probe, "_run_probe", lambda p, t: probe._parse_probe(_WITH_VIDEO)
    )
    probe.probe_media(media)                      # warm it

    def explode(path, timeout):
        raise AssertionError("cached_media started an ffprobe")

    monkeypatch.setattr(probe, "_run_probe", explode)
    assert probe.cached_media(media).duration == 1320.5


def test_a_cached_lookup_returns_nothing_when_it_does_not_know(tmp_path, cache_file):
    # The status snapshot is written every couple of seconds. If this probed
    # on a miss, every channel change would fork ffprobe on the box.
    assert probe.cached_media(_media(tmp_path)) is None


def test_a_cached_lookup_ignores_a_stale_entry(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    monkeypatch.setattr(
        probe, "_run_probe", lambda p, t: probe._parse_probe(_WITH_VIDEO)
    )
    probe.probe_media(media)
    media.write_bytes(b"\x00" * 900)              # replaced: the entry is stale
    assert probe.cached_media(media) is None


# ==========================================================================
# The cache has to remember all three answers, not two
# ==========================================================================
# has_video is three-valued on purpose: yes, no, and "we could not tell". If
# the cache flattens that third one into "no", a good file the box merely could
# not judge gets condemned - and because the entry is keyed on the file's size
# and modification time, it stays condemned. Uploading the same file again
# fails in exactly the same way and the customer has no way to clear it.

def _never_probe(path, timeout):
    raise AssertionError("ffprobe ran for a file the cache already knew about")


def test_a_verdict_we_never_reached_is_not_cached_as_no_picture(
    tmp_path, monkeypatch, cache_file
):
    media = _media(tmp_path)
    # ffprobe told us how long the file is, but nothing we understood about
    # its streams - so we do not know whether there is a picture in it.
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: probe.MediaInfo(300.0, None))
    assert probe.probe_media(media).has_video is None

    second = probe.probe_media(media)             # this one comes from the cache
    assert second.duration == 300.0
    assert second.has_video is None, "the cache turned 'we could not tell' into a verdict"
    assert second.unplayable is False


def test_a_verdict_we_never_reached_is_still_unknown_after_a_restart(
    tmp_path, monkeypatch, cache_file
):
    media = _media(tmp_path)
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: probe.MediaInfo(300.0, None))
    probe.probe_media(media)
    probe.flush_cache()

    probe.reset_cache()                           # fresh process, file read back
    monkeypatch.setattr(probe, "_run_probe", _never_probe)
    info = probe.probe_media(media)
    assert info.duration == 300.0
    assert info.has_video is None and info.unplayable is False


def test_a_file_with_no_picture_is_still_remembered_as_having_none(
    tmp_path, monkeypatch, cache_file
):
    # The other side of the same coin. A real "there is no video stream in
    # this" has to survive the round trip too, or the upload page would stop
    # warning about audio-only files the moment the box was switched off.
    media = _media(tmp_path)
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: probe._parse_probe(_AUDIO_ONLY))
    assert probe.probe_media(media).has_video is False
    probe.flush_cache()

    probe.reset_cache()
    monkeypatch.setattr(probe, "_run_probe", _never_probe)
    info = probe.probe_media(media)
    assert info.has_video is False and info.unplayable is True


def test_a_cached_lookup_does_not_turn_do_not_know_into_no_picture(
    tmp_path, monkeypatch, cache_file
):
    media = _media(tmp_path)
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: probe.MediaInfo(300.0, None))
    probe.probe_media(media)

    info = probe.cached_media(media)
    assert info.duration == 300.0
    assert info.has_video is None and info.unplayable is False


# ==========================================================================
# Cache files written by earlier versions of the box
# ==========================================================================
# Boxes already in the field have a durations.json on disk in the old format.
# An update must neither fall over on one nor read the old fourth field as a
# verdict, because that field is where the bug lived: it was written through
# bool(), so "we could not tell" and "there is no picture" both came out false.

def _put_on_disk(cache_file, contents):
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(contents), encoding="utf-8")
    probe.reset_cache()


def _entry(media, *rest):
    """A cache entry for ``media`` that matches the file actually on disk."""
    stat = media.stat()
    return [int(stat.st_mtime), stat.st_size, *rest]


def test_a_cache_from_before_this_fix_keeps_its_durations(
    tmp_path, monkeypatch, cache_file
):
    # Durations are the expensive part and were never ambiguous, so an update
    # must not throw away the probing a customer's box has already done.
    media = _media(tmp_path)
    _put_on_disk(cache_file, {str(media): _entry(media, 300.0, True)})
    monkeypatch.setattr(probe, "_run_probe", _never_probe)

    info = probe.probe_media(media)
    assert info.duration == 300.0
    assert info.has_video is True, "a stored true could only ever have been true"


def test_the_oldest_cache_entries_still_give_up_their_duration(
    tmp_path, monkeypatch, cache_file
):
    # Written before the box looked at streams at all: three fields, no verdict.
    media = _media(tmp_path)
    _put_on_disk(cache_file, {str(media): _entry(media, 300.0)})
    monkeypatch.setattr(probe, "_run_probe", _never_probe)

    info = probe.probe_media(media)
    assert info.duration == 300.0
    assert info.has_video is None and info.unplayable is False


def test_a_no_picture_verdict_from_before_this_fix_is_not_trusted(
    tmp_path, monkeypatch, cache_file
):
    # The old cache wrote bool(has_video), so a stored false might mean "there
    # is no picture" or it might mean "we could not tell". Nothing can tell
    # them apart now, and the second one has to stop condemning good files - so
    # the entry goes, and the file is looked at once more. One ffprobe run is a
    # cheap price for not refusing somebody's boot splash forever.
    media = _media(tmp_path)
    _put_on_disk(cache_file, {str(media): _entry(media, 300.0, False)})
    calls = []
    monkeypatch.setattr(
        probe, "_run_probe",
        lambda p, t: (calls.append(p), probe._parse_probe(_WITH_VIDEO))[1],
    )

    info = probe.probe_media(media)
    assert calls, "the box kept an ambiguous verdict instead of looking again"
    assert info.has_video is True


def test_a_no_picture_verdict_from_before_this_fix_is_not_reported_as_one(
    tmp_path, cache_file
):
    # cached_media never starts a process, so all it can do with an entry it
    # cannot trust is say it knows nothing about the file.
    media = _media(tmp_path)
    _put_on_disk(cache_file, {str(media): _entry(media, 300.0, False)})
    assert probe.cached_media(media) is None


def test_a_cache_from_a_newer_box_is_thrown_away_rather_than_misread(
    tmp_path, monkeypatch, cache_file
):
    # If an update gets rolled back, this build can meet a cache file written
    # by a later one whose fields it has never seen. Guessing at those fields
    # is how good files get condemned; re-probing costs one ffprobe run.
    media = _media(tmp_path)
    _put_on_disk(cache_file, {
        "version": probe.CACHE_VERSION + 1,
        "entries": {str(media): _entry(media, 300.0, False)},
    })
    calls = []
    monkeypatch.setattr(
        probe, "_run_probe",
        lambda p, t: (calls.append(p), probe._parse_probe(_WITH_VIDEO))[1],
    )

    assert probe.probe_media(media).has_video is True
    assert len(calls) == 1


def test_a_cache_entry_holding_nonsense_cannot_take_the_television_down(
    tmp_path, monkeypatch, cache_file
):
    # This box gets switched off at the wall, so a half-written or hand-edited
    # cache file is a matter of when. An entry we cannot make sense of has to
    # read as "we know nothing about this file", not raise out of a channel
    # change and leave somebody with no television.
    media = _media(tmp_path)
    _put_on_disk(cache_file, {str(media): _entry(media, "twenty minutes", True)})
    monkeypatch.setattr(
        probe, "_run_probe", lambda p, t: probe._parse_probe(_WITH_VIDEO)
    )

    assert probe.probe_media(media).duration == 1320.5
    assert probe.cached_media(media).duration == 1320.5


def test_a_cache_entry_claiming_an_endless_episode_is_ignored(
    tmp_path, monkeypatch, cache_file
):
    # json.load hands Infinity back as a float quite happily. A broadcast
    # schedule built on an episode that never ends is a channel stuck on one
    # show, and the entry would outlive every restart.
    media = _media(tmp_path)
    _put_on_disk(cache_file, {str(media): _entry(media, float("inf"), True)})
    monkeypatch.setattr(
        probe, "_run_probe", lambda p, t: probe._parse_probe(_WITH_VIDEO)
    )

    assert probe.probe_media(media).duration == 1320.5


@pytest.mark.parametrize(
    "contents",
    [
        {"ep.mp4": "not an entry"},
        {"ep.mp4": []},
        {"ep.mp4": [1, 2]},
        {"ep.mp4": {"duration": 12}},
        {"version": 2, "entries": "not a dict"},
        {"version": 2},
        {"version": "two", "entries": {}},
        ["not a dict at all"],
    ],
)
def test_a_cache_file_full_of_nonsense_is_ignored_rather_than_believed(
    tmp_path, monkeypatch, cache_file, contents
):
    media = _media(tmp_path)
    _put_on_disk(cache_file, contents)
    monkeypatch.setattr(
        probe, "_run_probe", lambda p, t: probe._parse_probe(_WITH_VIDEO)
    )

    info = probe.probe_media(media)
    assert info.duration == 1320.5 and info.has_video is True
