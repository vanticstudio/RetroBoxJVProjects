"""The dayparting editor: a day laid out, and what is actually on at 3am.

daypart.py already does all of this. Nothing here changes what it does - this
is the layer that makes it editable by somebody who has never seen the config
file, and that refuses the shapes which produce silently wrong behaviour.

The test that matters most is the last section: the preview and the engine
have to agree, at every minute of the day, or the editor is lying.
"""

import time

import pytest

from retrobox.daypart import Daypart, active_daypart
from retrobox.schedule import ScheduleError, blocks_from_config, day_view, preview_at, validate


def block(start, end, name=None, path=None, off_air=False):
    return {"from": start, "to": end, "name": name, "path": path, "off_air": off_air}


# ==========================================================================
# The shapes that produce silently wrong behaviour
# ==========================================================================
def test_a_plain_schedule_is_accepted():
    parts = validate([block("06:00", "12:00", "Mornings"),
                      block("12:00", "22:00", "Daytime")])
    assert [p.label for p in parts] == ["06:00-12:00", "12:00-22:00"]


def test_a_block_that_crosses_midnight_is_fine():
    # The whole reason dayparting exists: 22:00-04:00 is one late-night block,
    # not a mistake.
    parts = validate([block("22:00", "04:00", "After Dark")])
    assert parts[0].wraps is True
    assert parts[0].contains(23 * 60) and parts[0].contains(2 * 60)


def test_two_blocks_that_overlap_are_refused():
    # The engine resolves an overlap by order, which is right for a
    # hand-written file and wrong for a visual editor - somebody dragging two
    # blocks over each other has made a mistake, not a decision.
    with pytest.raises(ScheduleError) as caught:
        validate([block("06:00", "12:00", "A"), block("11:00", "14:00", "B")])
    assert "overlap" in str(caught.value).lower()
    assert "11:00" in str(caught.value), "it should say which ones"


def test_an_overlap_that_only_shows_up_across_midnight_is_still_found():
    with pytest.raises(ScheduleError):
        validate([block("22:00", "04:00", "Late"), block("03:00", "06:00", "Early")])


def test_a_block_that_covers_the_whole_day_is_refused():
    # start == end means "all day" to the engine. In an editor it is a block
    # that swallows every other one, which is never what somebody meant.
    with pytest.raises(ScheduleError):
        validate([block("06:00", "06:00", "Everything")])


def test_a_zero_length_block_is_refused():
    with pytest.raises(ScheduleError):
        validate([block("06:00", "06:00")])


@pytest.mark.parametrize("when", ["", "banana", "25:00", "6:70", None, "-1"])
def test_a_time_that_is_not_a_time_is_refused(when):
    with pytest.raises(ScheduleError):
        validate([block(when, "12:00", "A")])


def test_a_block_that_is_both_off_air_and_a_folder_is_refused():
    # The engine treats these as mutually exclusive; the editor says so.
    with pytest.raises(ScheduleError):
        validate([block("06:00", "12:00", "A", path="/media/x", off_air=True)])


def test_too_many_blocks_is_refused():
    with pytest.raises(ScheduleError):
        validate([block(f"{h:02d}:00", f"{h:02d}:30") for h in range(24)] * 3)


# ==========================================================================
# The gaps, and what happens in them
# ==========================================================================
def test_a_gap_is_reported_rather_than_hidden():
    view = day_view([block("06:00", "12:00", "Mornings")])
    gaps = [b for b in view if b["kind"] == "gap"]
    assert gaps, "a day with one block is mostly gap"
    assert sum(b["minutes"] for b in view) == 24 * 60


def test_a_gap_says_the_channel_runs_normally():
    view = day_view([block("06:00", "12:00", "Mornings")])
    gap = next(b for b in view if b["kind"] == "gap")
    assert "normal" in gap["label"].lower() or "usual" in gap["label"].lower()


def test_a_full_day_of_blocks_has_no_gaps():
    view = day_view([block("00:00", "12:00", "AM"), block("12:00", "24:00", "PM")])
    assert [b["kind"] for b in view] == ["block", "block"]


def test_the_day_view_is_in_clock_order_even_when_a_block_wraps():
    view = day_view([block("22:00", "04:00", "Late"), block("04:00", "22:00", "Day")])
    starts = [b["start"] for b in view]
    assert starts == sorted(starts), "the day should read midnight to midnight"


def test_a_wrapping_block_appears_at_both_ends_of_the_day():
    view = day_view([block("22:00", "04:00", "Late")])
    late = [b for b in view if b.get("name") == "Late"]
    assert len(late) == 2, "22:00-24:00 and 00:00-04:00"
    assert late[0]["start"] == 0 and late[-1]["end"] == 24 * 60


# ==========================================================================
# What is on at 3am
# ==========================================================================
def at(hour, minute=0):
    """A POSIX timestamp whose LOCAL clock reads hour:minute."""
    parts = list(time.localtime(1_700_000_000))
    parts[3], parts[4], parts[5] = hour, minute, 0
    parts[8] = -1
    return time.mktime(tuple(parts))


def test_the_preview_says_which_block_is_on():
    parts = validate([block("06:00", "12:00", "Mornings"),
                      block("22:00", "04:00", "After Dark")])
    assert preview_at(parts, 8 * 60)["name"] == "Mornings"
    assert preview_at(parts, 23 * 60)["name"] == "After Dark"
    assert preview_at(parts, 2 * 60)["name"] == "After Dark"


def test_the_preview_says_when_nothing_is_scheduled():
    parts = validate([block("06:00", "12:00", "Mornings")])
    result = preview_at(parts, 15 * 60)
    assert result["active"] is False
    assert result["name"] is None


def test_the_preview_reports_an_off_air_block_as_off_air():
    parts = validate([block("02:00", "06:00", "Closedown", off_air=True)])
    assert preview_at(parts, 3 * 60)["off_air"] is True


def test_the_preview_says_how_long_is_left():
    parts = validate([block("06:00", "12:00", "Mornings")])
    assert preview_at(parts, 11 * 60 + 30)["minutes_left"] == 30


# ==========================================================================
# The test that actually matters
# ==========================================================================
@pytest.mark.parametrize(
    "schedule",
    [
        [block("06:00", "12:00", "A")],
        [block("22:00", "04:00", "Late"), block("04:00", "22:00", "Day")],
        [block("00:00", "08:00", "A"), block("08:00", "16:00", "B"),
         block("16:00", "24:00", "C")],
        [block("23:30", "00:30", "Midnight")],
        [block("02:00", "06:00", "Off", off_air=True), block("06:00", "09:00", "AM")],
        [],
    ],
)
def test_the_preview_agrees_with_the_engine_at_every_minute_of_the_day(schedule):
    """The editor and the television must never disagree about 3am.

    Checked minute by minute against ``active_daypart`` itself, because a
    preview that is right about the easy hours and wrong about the wrap is
    worse than no preview - it is a confident wrong answer.
    """
    parts = validate(schedule)
    for minute in range(0, 24 * 60):
        engine = active_daypart(parts, at(minute // 60, minute % 60))
        shown = preview_at(parts, minute)

        if engine is None:
            assert shown["active"] is False, f"minute {minute}"
        else:
            assert shown["active"] is True, f"minute {minute}"
            assert shown["name"] == engine.name, f"minute {minute}"
            assert shown["off_air"] == engine.off_air, f"minute {minute}"
            assert shown["label"] == engine.label, f"minute {minute}"


def test_the_preview_matches_the_engine_on_an_overlap_the_engine_still_allows():
    # A hand-written config may legitimately still have overlaps - the engine
    # resolves them first-wins and keeps working. The preview must agree with
    # that too, not with what the editor would have insisted on.
    parts = [Daypart(start=6 * 60, end=12 * 60, name="First"),
             Daypart(start=11 * 60, end=14 * 60, name="Second")]
    for minute in range(0, 24 * 60):
        engine = active_daypart(parts, at(minute // 60, minute % 60))
        shown = preview_at(parts, minute)
        assert shown["name"] == (engine.name if engine else None), f"minute {minute}"


# ==========================================================================
# Round-tripping through the config
# ==========================================================================
def test_blocks_survive_a_trip_through_the_config_shape():
    original = [block("22:00", "04:00", "After Dark", path="/media/late"),
                block("06:00", "09:00", "Breakfast")]
    parts = validate(original)
    back = blocks_from_config([
        {"from": "22:00", "to": "04:00", "name": "After Dark", "path": "/media/late"},
        {"from": "06:00", "to": "09:00", "name": "Breakfast"},
    ])
    assert [p.label for p in back] == [p.label for p in parts]
    assert [p.name for p in back] == ["After Dark", "Breakfast"]


def test_an_empty_schedule_is_valid_and_means_no_dayparting():
    assert validate([]) == []
    assert day_view([]) == [{
        "kind": "gap", "start": 0, "end": 1440, "minutes": 1440,
        "label": "the channel runs as usual all day", "name": None,
        "off_air": False, "path": None,
    }]
