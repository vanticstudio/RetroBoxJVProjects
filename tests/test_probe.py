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
        probe, "_run_ffprobe", lambda p, t: (calls.append(p), 1234.0)[1]
    )

    assert probe.probe_duration(media) == 1234.0
    assert probe.probe_duration(media) == 1234.0
    assert len(calls) == 1, "the second call should not shell out again"


def test_cache_survives_a_restart(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    monkeypatch.setattr(probe, "_run_ffprobe", lambda p, t: 99.0)
    probe.probe_duration(media)
    probe.flush_cache()
    assert cache_file.is_file()

    # Fresh process: in-memory state gone, but the file is read back.
    probe.reset_cache()
    calls = []
    monkeypatch.setattr(probe, "_run_ffprobe", lambda p, t: (calls.append(p), 1.0)[1])
    assert probe.probe_duration(media) == 99.0
    assert calls == []


def test_cache_is_invalidated_when_the_file_changes(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    monkeypatch.setattr(probe, "_run_ffprobe", lambda p, t: 10.0)
    assert probe.probe_duration(media) == 10.0

    # Re-encode the file: different size, so the fingerprint no longer matches.
    media.write_bytes(b"\x00" * 4096)
    monkeypatch.setattr(probe, "_run_ffprobe", lambda p, t: 20.0)
    assert probe.probe_duration(media) == 20.0


def test_failures_are_never_cached(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    monkeypatch.setattr(probe, "_run_ffprobe", lambda p, t: None)
    assert probe.probe_duration(media) is None

    # A transient failure must not poison the entry forever.
    monkeypatch.setattr(probe, "_run_ffprobe", lambda p, t: 55.0)
    assert probe.probe_duration(media) == 55.0


def test_use_cache_false_always_probes(tmp_path, monkeypatch, cache_file):
    media = _media(tmp_path)
    calls = []
    monkeypatch.setattr(probe, "_run_ffprobe", lambda p, t: (calls.append(p), 7.0)[1])
    probe.probe_duration(media, use_cache=False)
    probe.probe_duration(media, use_cache=False)
    assert len(calls) == 2


def test_missing_ffprobe_short_circuits(tmp_path, monkeypatch, cache_file):
    monkeypatch.setattr(probe, "ffprobe_available", lambda: False)
    called = []
    monkeypatch.setattr(probe, "_run_ffprobe", lambda p, t: called.append(p))
    assert probe.probe_duration(_media(tmp_path)) is None
    assert called == []


def test_flush_survives_an_unwritable_location(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(probe, "CACHE_PATH", blocker / "durations.json")
    monkeypatch.setattr(probe, "ffprobe_available", lambda: True)
    probe.reset_cache()
    monkeypatch.setattr(probe, "_run_ffprobe", lambda p, t: 5.0)

    media = _media(tmp_path)
    assert probe.probe_duration(media) == 5.0
    probe.flush_cache()          # read-only / bad path must not raise
    assert probe.probe_duration(media) == 5.0
    probe.reset_cache()


def test_corrupt_cache_file_is_ignored(tmp_path, monkeypatch, cache_file):
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("{ this is not json")
    probe.reset_cache()
    monkeypatch.setattr(probe, "_run_ffprobe", lambda p, t: 42.0)
    assert probe.probe_duration(_media(tmp_path)) == 42.0
