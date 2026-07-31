import pytest

from retrobox.channel import build_lineup
from retrobox.config import ConfigError, config_from_dict
from retrobox.daypart import (
    Daypart,
    DaypartError,
    active_daypart,
    format_clock,
    parse_clock,
)
from tests.helpers import FakeWallClock, at_local, make_show


# -- clock parsing ---------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("22:00", 22 * 60),
        ("9:30", 9 * 60 + 30),
        ("2200", 22 * 60),
        ("06", 6 * 60),
        (6, 6 * 60),
        ("24:00", 24 * 60),
        ("00:00", 0),
        ("22:00:00", 22 * 60),
    ],
)
def test_parse_clock_accepts_common_forms(raw, expected):
    assert parse_clock(raw) == expected


@pytest.mark.parametrize("raw", ["", "nope", "25:00", "-1:00", True, "12:xx"])
def test_parse_clock_rejects_junk(raw):
    with pytest.raises(DaypartError):
        parse_clock(raw)


def test_format_clock_round_trips():
    assert format_clock(parse_clock("22:05")) == "22:05"
    assert format_clock(0) == "00:00"
    assert format_clock(24 * 60) == "24:00"


# -- window matching -------------------------------------------------------
def test_window_contains_within_the_day():
    part = Daypart(start=parse_clock("09:00"), end=parse_clock("17:00"))
    assert not part.wraps
    assert part.contains(parse_clock("09:00"))    # start is inclusive
    assert part.contains(parse_clock("12:30"))
    assert not part.contains(parse_clock("17:00"))  # end is exclusive
    assert not part.contains(parse_clock("03:00"))


def test_window_wraps_past_midnight():
    part = Daypart(start=parse_clock("22:00"), end=parse_clock("04:00"))
    assert part.wraps
    assert part.contains(parse_clock("23:59"))
    assert part.contains(parse_clock("00:30"))
    assert part.contains(parse_clock("03:59"))
    assert not part.contains(parse_clock("04:00"))
    assert not part.contains(parse_clock("12:00"))


def test_equal_start_and_end_covers_the_whole_day():
    part = Daypart(start=600, end=600)
    assert all(part.contains(m) for m in (0, 599, 600, 601, 1439))


def test_minutes_until_end_handles_the_wrap():
    part = Daypart(start=parse_clock("22:00"), end=parse_clock("04:00"))
    assert part.minutes_until_end(parse_clock("23:00")) == 5 * 60
    assert part.minutes_until_end(parse_clock("01:00")) == 3 * 60
    assert part.minutes_until_end(parse_clock("12:00")) == 0  # not inside it


def test_active_daypart_picks_the_first_match():
    early = Daypart(start=parse_clock("20:00"), end=parse_clock("23:00"), name="EARLY")
    late = Daypart(start=parse_clock("22:00"), end=parse_clock("02:00"), name="LATE")
    assert active_daypart([early, late], at_local(22, 30)).name == "EARLY"
    assert active_daypart([late, early], at_local(22, 30)).name == "LATE"
    assert active_daypart([early, late], at_local(12)) is None
    assert active_daypart([], at_local(22)) is None


# -- config parsing --------------------------------------------------------
def _config_with_dayparts(tmp_path, dayparts):
    make_show(tmp_path, "day", 3)
    make_show(tmp_path, "night", 2)
    return config_from_dict(
        {
            "shuffle_seed": 3,
            "channels": [
                {
                    "number": 2,
                    "name": "Talk",
                    "path": str(tmp_path / "day"),
                    "dayparts": dayparts,
                }
            ],
        }
    )


def test_config_parses_dayparts(tmp_path):
    cfg = _config_with_dayparts(
        tmp_path,
        [{"from": "22:00", "to": "04:00", "name": "LATE NIGHT",
          "path": str(tmp_path / "night")}],
    )
    (part,) = cfg.channels[0].dayparts
    assert part.name == "LATE NIGHT"
    assert part.path == tmp_path / "night"
    assert part.label == "22:00-04:00"
    assert not part.off_air


def test_config_rejects_daypart_without_bounds(tmp_path):
    with pytest.raises(ConfigError, match="needs both 'from' and 'to'"):
        _config_with_dayparts(tmp_path, [{"from": "22:00"}])


def test_config_rejects_off_air_with_a_path(tmp_path):
    with pytest.raises(ConfigError, match="cannot set both"):
        _config_with_dayparts(
            tmp_path,
            [{"from": "02:00", "to": "06:00", "off_air": True, "path": str(tmp_path)}],
        )


def test_config_rejects_bad_clock_time(tmp_path):
    with pytest.raises(ConfigError, match="clock time"):
        _config_with_dayparts(tmp_path, [{"from": "banana", "to": "04:00"}])


# -- channel behaviour -----------------------------------------------------
def _lineup(tmp_path, dayparts, clock):
    cfg = _config_with_dayparts(tmp_path, dayparts)
    return build_lineup(cfg, wall_clock=clock)


def test_daypart_swaps_the_channel_name_and_pool(tmp_path):
    clock = FakeWallClock(12)
    lineup = _lineup(
        tmp_path,
        [{"from": "22:00", "to": "04:00", "name": "LATE NIGHT",
          "path": str(tmp_path / "night")}],
        clock,
    )
    channel = lineup.current

    assert channel.name == "Talk"
    assert len(channel.episodes_at()) == 3
    assert channel.tune_in().path.parent.name == "day"

    clock.set(23)
    assert channel.name == "LATE NIGHT"
    assert len(channel.episodes_at()) == 2
    assert channel.tune_in().path.parent.name == "night"

    clock.set(3)  # still inside the wrapped window
    assert channel.name == "LATE NIGHT"

    clock.set(5)  # back to the base channel
    assert channel.name == "Talk"
    assert channel.tune_in().path.parent.name == "day"


def test_daypart_can_just_rename_without_a_second_folder(tmp_path):
    clock = FakeWallClock(23)
    lineup = _lineup(
        tmp_path, [{"from": "22:00", "to": "04:00", "name": "AFTER DARK"}], clock
    )
    channel = lineup.current
    assert channel.name == "AFTER DARK"
    # No `path`, so it keeps drawing from the channel's own folder.
    assert len(channel.episodes_at()) == 3
    assert channel.tune_in().path.parent.name == "day"


def test_off_air_daypart_stops_the_channel(tmp_path):
    clock = FakeWallClock(3)
    lineup = _lineup(
        tmp_path, [{"from": "02:00", "to": "06:00", "off_air": True}], clock
    )
    channel = lineup.current

    assert channel.is_off_air()
    assert channel.episodes_at() == []
    assert channel.tune_in() is None
    assert channel.advance() is None
    # ...but the channel itself is not "empty" - it has programming by day.
    assert not channel.is_empty

    clock.set(7)
    assert not channel.is_off_air()
    assert channel.tune_in() is not None


def test_resume_does_not_leak_across_dayparts(tmp_path):
    make_show(tmp_path, "day", 3)
    make_show(tmp_path, "night", 2)
    cfg = config_from_dict(
        {
            "shuffle_seed": 3,
            "tune_in": "resume",
            "start_offset": 0,
            "channels": [
                {
                    "number": 2,
                    "name": "Talk",
                    "path": str(tmp_path / "day"),
                    "dayparts": [
                        {"from": "22:00", "to": "04:00", "name": "LATE",
                         "path": str(tmp_path / "night")}
                    ],
                }
            ],
        }
    )
    clock = FakeWallClock(12)
    channel = build_lineup(cfg, wall_clock=clock).current

    daytime = channel.tune_in().path
    channel.remember(daytime, 300.0)

    resumed = channel.tune_in()
    assert resumed.path == daytime and resumed.start == 300.0

    clock.set(23)
    # The remembered daytime episode is not in the late-night pool, so the
    # channel starts something from that pool instead of replaying it.
    request = channel.tune_in()
    assert request is not None
    assert request.path.parent.name == "night"


def test_peek_now_never_builds_a_schedule(tmp_path):
    clock = FakeWallClock(12)
    lineup = _lineup(tmp_path, [], clock)
    channel = lineup.current
    # Nothing has been tuned yet, so the guide gets no answer rather than
    # paying to ffprobe the whole channel.
    assert channel.peek_now() is None


def test_daypart_pool_reports_counts_for_check(tmp_path):
    clock = FakeWallClock(12)
    lineup = _lineup(
        tmp_path,
        [
            {"from": "22:00", "to": "04:00", "path": str(tmp_path / "night")},
            {"from": "04:00", "to": "06:00", "off_air": True},
        ],
        clock,
    )
    channel = lineup.current
    assert len(channel.daypart_pool(0)) == 2
    assert channel.daypart_pool(1) == []      # off air carries nothing
    assert channel.daypart_pool(99) == []     # out of range is harmless
