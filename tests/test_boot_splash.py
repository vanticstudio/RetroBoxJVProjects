"""The JV Projects clip that plays when a box is switched on.

Two separate things are guarded here.

**That it plays at all.** The clip ships in the repo and every box shows it
unless it is deliberately turned off - that is what makes a Retro Box look
like a product rather than a script that starts.

**That it can never strand a box.** A splash is the first thing a customer
sees, on hardware nobody can reach. The hard timeout in app.py is what
guarantees a clip which never reports end-of-file still hands over to channel
one, and it is tested here against the real shipped file rather than a stub.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from retrobox.actions import Action, InputEvent
from retrobox.config import config_from_dict
from tests.helpers import make_show

ASSET = Path(__file__).resolve().parent.parent / "retrobox" / "assets" / "boot_splash.mp4"


# ==========================================================================
# The asset itself
# ==========================================================================
def test_the_clip_ships_in_the_repo():
    assert ASSET.is_file(), "the boot splash is missing from retrobox/assets"


def test_the_clip_is_small_enough_for_an_appliance():
    # Four seconds of 720p-ish video. A ceiling here catches somebody dropping
    # in a 200 MB ProRes export by accident.
    size = ASSET.stat().st_size
    assert 0 < size <= 1024 * 1024, f"{size} bytes is not a four-second splash"


def _ffprobe(*fields):
    """One ffprobe call for a stream property, or None if there is no ffprobe."""
    if shutil.which("ffprobe") is None:
        return None
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=" + ",".join(fields),
         "-of", "default=noprint_wrappers=1:nokey=1", str(ASSET)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip().splitlines()


def test_the_clip_is_exactly_the_format_the_box_expects():
    """The spec, asserted against the committed file.

    This is the point of the exercise: a splash that is subtly the wrong
    format fails on the box's hardware decoder and plays perfectly on a
    laptop, so the laptop is not the thing to trust.
    """
    values = _ffprobe("codec_name", "width", "height", "pix_fmt", "r_frame_rate")
    if values is None:
        pytest.skip("ffprobe is not installed here - the format cannot be checked")

    codec, width, height, pix_fmt, rate = values
    assert codec == "h264", codec
    assert (int(width), int(height)) == (1920, 1080)
    # yuv420p is not a preference. It is what plays on every hardware decoder
    # this box might have, and what the shipped clip uses.
    assert pix_fmt == "yuv420p", pix_fmt
    assert rate == "30/1", rate


def test_the_clip_is_four_seconds_long():
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe is not installed here - the duration cannot be checked")
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(ASSET)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0
    assert float(result.stdout.strip()) == pytest.approx(4.0, abs=0.05)


# ==========================================================================
# Every box plays it, unless it is told not to
# ==========================================================================
def _config(tmp_path, **extra):
    make_show(tmp_path, "sitcoms", 2)
    data = {
        "channels": [{"number": 2, "name": "S", "path": str(tmp_path / "sitcoms")}],
    }
    data.update(extra)
    return config_from_dict(data)


def test_a_config_that_says_nothing_about_it_still_plays_it(tmp_path):
    # The whole point of this change: a box shows its own branding on power-up
    # without anybody having to ask for it.
    assert _config(tmp_path).boot_splash is not None
    assert _config(tmp_path).boot_splash.name == "boot_splash.mp4"


@pytest.mark.parametrize("value", [False, None, ""])
def test_it_can_be_turned_off_outright(tmp_path, value):
    assert _config(tmp_path, boot_splash=value).boot_splash is None


def test_a_clip_of_your_own_still_wins(tmp_path):
    assert _config(tmp_path, boot_splash="my_own.mp4").boot_splash.name == "my_own.mp4"


def test_a_box_out_of_the_box_actually_shows_it(tmp_path):
    """End to end: a fresh config, and the real clip is on screen."""
    from retrobox.app import TVApp
    from retrobox.input.manager import InputManager
    from retrobox.player import MockPlayer
    from tests.helpers import FakeClock, FakeWallClock

    app = TVApp(
        _config(tmp_path), MockPlayer(), InputManager([]),
        clock=FakeClock(), wall_clock=FakeWallClock(12),
    )
    app.start()

    assert app._splash_active is True, "a new box did not show its splash"
    assert app.player.current == ASSET, "it played something other than the clip"


# ==========================================================================
# ...and it can never strand the box
# ==========================================================================
def _booting(tmp_path):
    from retrobox.app import TVApp
    from retrobox.input.manager import InputManager
    from retrobox.player import MockPlayer
    from tests.helpers import FakeClock, FakeWallClock

    clock = FakeClock()
    app = TVApp(
        _config(tmp_path), MockPlayer(), InputManager([]),
        clock=clock, wall_clock=FakeWallClock(12),
    )
    app.start()
    return app, app.player, clock


def test_a_clip_that_never_ends_still_hands_over_to_channel_one(tmp_path):
    """The safety net, against the real shipped file.

    MockPlayer never reports end-of-file, which is exactly the failure this
    guards: a truncated clip, or a codec mpv cannot finish. Without the
    timeout the box sits on the splash for ever, on hardware nobody can reach.
    """
    app, player, clock = _booting(tmp_path)
    assert app._splash_active is True

    from retrobox.app import _SPLASH_TIMEOUT_SECONDS

    clock.advance(_SPLASH_TIMEOUT_SECONDS + 1)
    app.step()

    assert app._splash_active is False, "the box was stranded on the splash"
    assert player.current != ASSET, "it never reached a channel"
    assert app.lineup.current.number == 2


def test_any_keypress_skips_it(tmp_path):
    app, player, clock = _booting(tmp_path)
    app.handle_event(InputEvent(Action.CHANNEL_UP))

    assert app._splash_active is False
    assert player.current != ASSET


def test_the_deadline_is_set_from_the_clock_not_from_the_file(tmp_path):
    # Nothing read out of the clip goes anywhere near the timeout, so no file
    # - however malformed - can extend or defeat it.
    from retrobox.app import _SPLASH_TIMEOUT_SECONDS

    app, player, clock = _booting(tmp_path)
    assert app._splash_deadline == pytest.approx(
        clock.now + _SPLASH_TIMEOUT_SECONDS, abs=0.01
    )


def test_the_first_press_is_consumed_by_the_skip(tmp_path):
    # Nobody wants their first channel-up to silently land on channel 3.
    app, player, clock = _booting(tmp_path)
    app.handle_event(InputEvent(Action.CHANNEL_UP))
    assert app.lineup.current.number == 2
