"""The boot splash, and the guarantee that it can never strand the box.

A splash is the first thing a customer sees on power-up and the last thing you
want to go wrong. Two rules: it has to be short, and whatever happens the
television starts anyway.

That second one is not a preference. ``_SPLASH_TIMEOUT_SECONDS`` is what stops
a truncated or unplayable clip leaving somebody looking at a black screen
forever, and it has to hold for a file the customer uploaded, not just for the
one that ships in the box.
"""

import pytest

from retrobox.actions import Action, InputEvent
from retrobox.branding import BrandingError, check_splash
from retrobox.probe import MediaInfo


def info(duration=4.0, has_video=True):
    return MediaInfo(duration=duration, has_video=has_video)


# ==========================================================================
# What may become a splash
# ==========================================================================
def test_a_short_playable_clip_is_accepted(tmp_path, monkeypatch):
    clip = tmp_path / "splash.mp4"
    clip.write_bytes(b"\x00" * 1024)
    monkeypatch.setattr("retrobox.branding.probe_media", lambda p, **k: info(4.0))
    assert check_splash(clip).duration == 4.0


def test_a_splash_that_runs_too_long_is_refused(tmp_path, monkeypatch):
    # A two-minute splash turns a television into a fault.
    clip = tmp_path / "splash.mp4"
    clip.write_bytes(b"\x00" * 1024)
    monkeypatch.setattr("retrobox.branding.probe_media", lambda p, **k: info(120.0))

    with pytest.raises(BrandingError) as caught:
        check_splash(clip)
    assert "seconds" in str(caught.value)


def test_a_file_with_no_picture_is_refused(tmp_path, monkeypatch):
    clip = tmp_path / "splash.mp4"
    clip.write_bytes(b"\x00" * 1024)
    monkeypatch.setattr(
        "retrobox.branding.probe_media", lambda p, **k: info(3.0, has_video=False)
    )
    with pytest.raises(BrandingError) as caught:
        check_splash(clip)
    assert "picture" in str(caught.value).lower()


def test_a_file_that_cannot_be_probed_at_all_is_refused(tmp_path, monkeypatch):
    # Unlike an uploaded episode - which is the user's file and their decision -
    # a splash that will not play is a black screen on every power-up.
    clip = tmp_path / "splash.mp4"
    clip.write_bytes(b"not a video")
    monkeypatch.setattr(
        "retrobox.branding.probe_media", lambda p, **k: MediaInfo(None, None)
    )
    with pytest.raises(BrandingError):
        check_splash(clip)


def test_an_empty_file_is_refused(tmp_path):
    clip = tmp_path / "splash.mp4"
    clip.write_bytes(b"")
    with pytest.raises(BrandingError):
        check_splash(clip)


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(BrandingError):
        check_splash(tmp_path / "not-there.mp4")


def test_a_splash_the_box_could_only_half_read_is_not_refused_for_ever(tmp_path, monkeypatch):
    # Not stubbed at the probe_media seam like the tests above: this one goes
    # through the real duration cache, because that is where it went wrong.
    # ffprobe reported how long the clip is but nothing we recognised about its
    # streams, so the box does not know whether there is a picture - which is
    # not a refusal. The answer is cached against the file's size and
    # modification time, so a wrong answer here refuses that exact file every
    # time it is uploaded, for ever, with nothing the customer can do about it.
    from retrobox import probe

    monkeypatch.setattr(probe, "CACHE_PATH", tmp_path / "cache" / "durations.json")
    monkeypatch.setattr(probe, "ffprobe_available", lambda: True)
    monkeypatch.setattr(probe, "_run_probe", lambda p, t: MediaInfo(4.0, None))
    probe.reset_cache()

    clip = tmp_path / "splash.mp4"
    clip.write_bytes(b"\x00" * 1024)
    try:
        assert check_splash(clip).duration == 4.0
        assert check_splash(clip).duration == 4.0, "the cached answer refused it"
    finally:
        probe.reset_cache()


def test_without_ffprobe_a_splash_is_refused_rather_than_hoped_for(tmp_path, monkeypatch):
    # Everywhere else "cannot tell" means "do not condemn it". Here it means
    # refuse: the cost of being wrong is a box that looks broken on every boot,
    # and the default splash is always available instead.
    clip = tmp_path / "splash.mp4"
    clip.write_bytes(b"\x00" * 1024)
    monkeypatch.setattr("retrobox.branding.probe_media", lambda p, **k: MediaInfo())
    with pytest.raises(BrandingError):
        check_splash(clip)


# ==========================================================================
# The timeout, with a customer's own file installed
# ==========================================================================
def build(tmp_path, splash_name):
    """A TV whose boot splash is whatever file we point it at."""
    from tests.test_app import build_app

    app, player, clock = build_app(
        tmp_path, assets_dir=tmp_path, boot_splash=splash_name,
    )
    return app, player, clock


def test_the_timeout_still_fires_for_an_uploaded_splash(tmp_path):
    # The clip never reports end-of-file - a truncated upload, a codec mpv
    # cannot finish. Without the timeout the box sits on it forever.
    custom = tmp_path / "customer-splash.mp4"
    custom.write_bytes(b"\x00" * 2048)
    app, player, clock = build(tmp_path, "customer-splash.mp4")

    app.start()
    assert app._splash_active is True
    assert player.current == custom, "it did not play the uploaded file"

    clock.advance(31)
    app.step()

    assert app._splash_active is False, "the box was stranded on an uploaded splash"
    assert app.lineup.current is not None
    assert player.current != custom, "it never got to a channel"


def test_a_keypress_still_skips_an_uploaded_splash(tmp_path):
    custom = tmp_path / "customer-splash.mp4"
    custom.write_bytes(b"\x00" * 2048)
    app, player, clock = build(tmp_path, "customer-splash.mp4")

    app.start()
    app.handle_event(InputEvent(Action.CHANNEL_UP))

    assert app._splash_active is False
    assert player.current != custom


def test_the_timeout_is_not_something_a_file_can_influence(tmp_path):
    # Whatever is in the clip, the deadline is set from the clock when it
    # starts - nothing read out of the file goes anywhere near it.
    custom = tmp_path / "customer-splash.mp4"
    custom.write_bytes(b"\xff" * 4096)
    app, player, clock = build(tmp_path, "customer-splash.mp4")
    app.start()

    from retrobox.app import _SPLASH_TIMEOUT_SECONDS

    assert app._splash_deadline == pytest.approx(
        clock.now + _SPLASH_TIMEOUT_SECONDS, abs=0.01
    )


def test_a_splash_that_is_not_there_at_boot_simply_does_not_play(tmp_path):
    app, player, clock = build(tmp_path, "deleted-by-someone.mp4")
    app.start()
    assert app._splash_active is False, "a missing splash must not stall start-up"
    assert player.current is not None, "the television should just start"


# ==========================================================================
# Getting back to the shipped one
# ==========================================================================
def test_the_default_splash_ships_in_the_repo():
    from retrobox.branding import DEFAULT_SPLASH_NAME, default_splash_path

    assert DEFAULT_SPLASH_NAME == "boot_splash.mp4"
    # This feature makes the branding replaceable, not absent.
    assert default_splash_path().name == DEFAULT_SPLASH_NAME
