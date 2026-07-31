import re

from retrobox.config import config_from_dict
from retrobox.overlay import (
    GuideEntry,
    OverlayManager,
    _guide_window,
    _truncate,
)
from retrobox.player import MockPlayer
from tests.helpers import FakeClock, make_show

# The 4:3 frame within the 1280x720 canvas spans x in [160, 1120].
_FRAME_X0, _FRAME_X1 = 160, 1120


def _all_x_positions(ass: str):
    return [int(m) for m in re.findall(r"\\pos\((\d+),", ass)]


def _config(tmp_path):
    make_show(tmp_path, "a", 1)
    return config_from_dict(
        {
            "channel_bug_seconds": 4,
            "osd_duration": 2,
            "channels": [{"number": 3, "name": "MTV Classic", "path": str(tmp_path / "a")}],
        }
    )


def test_channel_bug_drawn_and_expires(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)

    om.show_channel_bug(3, "MTV Classic")
    assert 1 in player.overlays  # channel overlay id
    ass = player.overlays[1]
    assert "CH 03" in ass and "MTV Classic" in ass

    clock.advance(3.9)
    om.tick()
    assert 1 in player.overlays  # not yet expired

    clock.advance(0.2)
    om.tick()
    assert 1 not in player.overlays  # expired after 4s


def test_volume_overlay_has_label_and_bars(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(45, muted=False)
    ass = player.overlays[2]
    assert "Volume" in ass
    # 20 segments: some drawn as bars (rectangles start "m 0 0 l"), rest as dots.
    assert ass.count("\\p1") == 20


def test_volume_bars_scale_with_level(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(100, muted=False)
    full = player.overlays[2].count("m 0 0 l")  # rectangle (filled bar) count
    om.show_volume(0, muted=False)
    empty = player.overlays[2].count("m 0 0 l")
    assert full == 20 and empty == 0


def test_muted_volume_overlay(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(45, muted=True)
    assert "Mute" in player.overlays[2]


def test_standby_overlay_does_not_expire(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)
    om.show_standby()
    clock.advance(1000)
    om.tick()
    assert 3 in player.overlays  # standby id persists
    om.clear_standby()
    assert 3 not in player.overlays


def test_channel_name_with_braces_is_escaped(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(5, "Weird{name}")
    # Braces in the name must be neutralised (they delimit ASS override blocks).
    ass = player.overlays[1]
    assert "Weird(name)" in ass
    assert "Weird{name}" not in ass


def test_message_overlay(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_message("CH 12  -  NO CHANNEL")
    assert "NO CHANNEL" in player.overlays[4]


def test_channel_bug_sits_inside_4x3_frame(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(3, "MTV Classic")
    xs = _all_x_positions(player.overlays[1])
    assert xs and all(_FRAME_X0 <= x <= _FRAME_X1 for x in xs)


def test_volume_bar_sits_inside_4x3_frame(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(100, muted=False)  # widest case: all 20 bars drawn
    xs = _all_x_positions(player.overlays[2])
    assert xs and all(_FRAME_X0 <= x <= _FRAME_X1 for x in xs)


def test_overlay_uses_configured_font_and_color(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(3, "MTV Classic")
    ass = player.overlays[1]
    assert "\\fnVT323" in ass          # bundled retro font
    assert "&H005AFF4D" in ass         # #4DFF5A -> ASS BBGGRR


# --------------------------------------------------------------------------
# Channel guide
# --------------------------------------------------------------------------
def _guide_entries(n, start=2):
    return [
        GuideEntry(number=start + i, name=f"Channel {start + i}", now_playing="Some Show")
        for i in range(n)
    ]


def test_guide_draws_rows_inside_the_frame(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())

    om.show_guide(_guide_entries(3), current_number=3, header="GUIDE    23:40")
    ass = player.overlays[5]

    assert "GUIDE    23:40" in ass
    assert ">CH 03" in ass          # current channel is marked
    assert " CH 02" in ass
    assert om.guide_visible
    for x in _all_x_positions(ass):
        assert _FRAME_X0 <= x <= _FRAME_X1


def test_guide_expires_and_clears_its_visibility(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)

    om.show_guide(_guide_entries(2), current_number=2, duration=5)
    assert om.guide_visible
    clock.advance(5.1)
    om.tick()
    assert 5 not in player.overlays
    assert not om.guide_visible


def test_clear_all_takes_the_guide_with_it(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_guide(_guide_entries(2), current_number=2)
    om.clear_all()
    assert 5 not in player.overlays
    assert not om.guide_visible


def test_guide_marks_off_air_channels(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    entries = [GuideEntry(number=2, name="Sign Off", now_playing="ignored", off_air=True)]
    om.show_guide(entries, current_number=2)
    ass = player.overlays[5]
    assert "OFF AIR" in ass
    assert "ignored" not in ass


def test_guide_dims_rows_you_are_not_watching(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_guide(_guide_entries(3), current_number=3)
    lines = player.overlays[5].splitlines()

    current = [ln for ln in lines if ">CH 03" in ln][0]
    other = [ln for ln in lines if " CH 02" in ln][0]
    assert r"\1a&H00&" in current          # fully opaque
    assert r"\1a&H00&" not in other        # dimmed
    assert r"\3a&H" in other               # ...glow faded to match


def test_guide_scrolls_to_keep_the_current_channel_visible():
    entries = _guide_entries(40)
    window = _guide_window(entries, current_number=30, max_rows=10)
    assert len(window) == 10
    assert 30 in [e.number for e in window]


def test_guide_window_pins_to_the_ends():
    entries = _guide_entries(40, start=1)
    first = _guide_window(entries, current_number=1, max_rows=10)
    assert [e.number for e in first] == list(range(1, 11))

    last = _guide_window(entries, current_number=40, max_rows=10)
    assert [e.number for e in last] == list(range(31, 41))


def test_guide_window_passes_short_lineups_through():
    entries = _guide_entries(3)
    assert _guide_window(entries, current_number=2, max_rows=10) == entries


def test_guide_truncates_over_long_cells():
    assert _truncate("short", 20) == "short"
    assert _truncate("x" * 30, 10) == "xxxxxxx..."
    assert len(_truncate("x" * 30, 10)) == 10
    assert _truncate("", 10) == ""


def test_unlit_volume_dots_use_dim_color(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(50, muted=False)
    ass = player.overlays[2]
    # #4DFF5A -> &H005AFF4D (lit bars), #123B18 -> &H00183B12 (unlit dots).
    assert "&H005AFF4D" in ass
    assert "&H00183B12" in ass


def test_sleep_indicator_is_persistent(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)
    om.show_sleep(45)
    assert "SLEEP 45m" in player.overlays[6]
    clock.advance(600)
    om.tick()
    assert 6 in player.overlays, "the sleep readout must not time out"
    om.clear_sleep()
    assert 6 not in player.overlays
