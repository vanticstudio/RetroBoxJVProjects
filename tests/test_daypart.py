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


# ==========================================================================
# A clock that cannot be trusted
# ==========================================================================
# Dayparting is the one feature that fails silently and invisibly when the
# clock is wrong: nothing raises, nothing logs, the box simply plays the 3am
# block at teatime and the owner concludes the feature is broken. These tests
# pin down what happens instead.

#: A timestamp from long before any RetroBox existed - what a mini PC with a
#: flat CMOS battery actually comes up with after it is switched off at the
#: wall. Mid-2010 rather than New Year, so that no timezone offset can drag it
#: back into the previous year and make these tests depend on where they run.
FLAT_BATTERY_EPOCH = 1_277_942_400          # 2010-07-01T00:00:00Z

#: A whole day in one window, so it matches whatever local hour the test
#: machine's timezone turns an epoch into. Nothing about these tests should
#: depend on where the box is.
ALL_DAY = Daypart(start=0, end=0, name="ALWAYS ON")


@pytest.fixture(autouse=True)
def no_clock_trust_source_leaks_between_tests():
    """The trust source is module state; a test that installs one must not
    change the answer another test gets."""
    from retrobox import daypart

    before = daypart.clock_trust_source()
    yield
    daypart.set_clock_trust(before)


# -- the fallback ----------------------------------------------------------
def test_an_untrusted_clock_falls_back_to_the_channel_rather_than_raising():
    """A box that cannot vouch for its own clock plays the channel as itself.

    Falling back to "no daypart matched" is the only honest answer: it is
    exactly what the channel is outside every window anyway, so the box is
    guaranteed to have something to play, and it is the one outcome that
    cannot be a *confidently wrong* one.
    """
    from retrobox.daypart import active_daypart

    late = Daypart(start=parse_clock("22:00"), end=parse_clock("04:00"), name="LATE")
    assert active_daypart([late], at_local(23), clock_trust=lambda: False) is None
    # ...and the same call with a clock it can vouch for is untouched.
    assert active_daypart([late], at_local(23), clock_trust=lambda: True).name == "LATE"


def test_an_untrusted_clock_never_takes_a_channel_off_the_air():
    """The worst outcome of a wrong clock is a black screen nobody can explain.

    A sign-off window is the one daypart that stops television happening, so
    it is the one that must never fire on a guess.
    """
    from retrobox.daypart import active_daypart

    closedown = Daypart(start=0, end=0, off_air=True)
    assert active_daypart([closedown], at_local(3), clock_trust=lambda: False) is None


def test_a_date_from_before_the_box_existed_is_distrusted_without_being_told():
    """A flat CMOS battery is one of the commonest faults on a ten-year-old
    mini PC, and the box can spot it on its own - no network, no helper
    module, nothing to configure."""
    from retrobox.daypart import active_daypart

    assert active_daypart([ALL_DAY], FLAT_BATTERY_EPOCH) is None
    # A believable timestamp still dayparts exactly as it always has.
    assert active_daypart([ALL_DAY], at_local(12)).name == "ALWAYS ON"


@pytest.mark.parametrize("epoch", [-10**12, 10**18, float("nan"), float("inf")])
def test_a_clock_reading_pure_nonsense_still_plays_television(epoch):
    """Whatever the hardware hands us, the answer is a channel, not a traceback."""
    from retrobox.daypart import active_daypart

    assert active_daypart([ALL_DAY], epoch) is None


# -- the fallback is reported, not silent ----------------------------------
def test_the_fallback_says_out_loud_that_it_is_not_dayparting_and_why():
    """Silence is the original bug. Something has to be able to answer the
    question "why is this channel not changing at 10pm?"."""
    from retrobox.daypart import resolve

    decision = resolve([ALL_DAY], FLAT_BATTERY_EPOCH)
    assert decision.part is None
    assert decision.index is None
    assert decision.clock_trusted is False
    assert decision.reason                      # a sentence, not a blank
    assert "2010" in decision.reason            # says what it thinks the date is


def test_the_explanation_names_the_two_dollar_part_the_owner_has_to_replace():
    """The owner cannot possibly guess it is the CMOS battery unless the box
    says so, and that is the whole difference between a fixable box and a box
    that goes in a cupboard."""
    from retrobox.daypart import resolve

    detail = resolve([ALL_DAY], FLAT_BATTERY_EPOCH).detail.lower()
    assert "battery" in detail


def test_a_trusted_clock_reports_no_reason_to_explain():
    from retrobox.daypart import resolve

    decision = resolve([ALL_DAY], at_local(12))
    assert decision.clock_trusted is True
    assert decision.reason == ""
    assert decision.part is ALL_DAY
    assert decision.index == 0


def test_the_dashboard_can_ask_whether_dayparting_is_running_at_all():
    """webui.py needs one call it can put on a page, without a channel in hand."""
    from retrobox.daypart import clock_report

    report = clock_report(epoch=FLAT_BATTERY_EPOCH)
    assert report["trusted"] is False
    assert report["dayparting"] is False
    assert report["reason"] and report["detail"]
    assert report["local_time"]                 # what the box believes it is

    healthy = clock_report(epoch=at_local(12))
    assert healthy["trusted"] is True and healthy["dayparting"] is True
    assert healthy["reason"] == ""


def test_crossing_into_an_untrusted_clock_is_logged_once_rather_than_every_tick(caplog):
    """The player asks this several times a second. A warning per tick is the
    same as no warning at all - it buries the one line that mattered."""
    import logging

    from retrobox import daypart

    # Installing a trust source means a fresh opinion about the clock, so it
    # also clears the memory of what has already been announced. Called here to
    # start from a known state whatever ran before this test.
    daypart.set_clock_trust(None)

    with caplog.at_level(logging.WARNING, logger="retrobox.daypart"):
        for _ in range(5):
            daypart.resolve([ALL_DAY], FLAT_BATTERY_EPOCH)
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


# -- the seam ---------------------------------------------------------------
# daypart.py must not import the module that decides whether the clock is
# trustworthy: that module talks to the network, and this one has to stay
# headless, instant and testable. So the answer is handed in.
def test_a_trust_source_can_be_installed_for_the_whole_box():
    from retrobox import daypart

    daypart.set_clock_trust(lambda: False)
    assert daypart.active_daypart([ALL_DAY], at_local(12)) is None
    daypart.set_clock_trust(None)
    assert daypart.active_daypart([ALL_DAY], at_local(12)) is ALL_DAY


def test_a_trust_source_that_says_nothing_yet_leaves_dayparting_alone():
    """Most boxes are offline and will never have an opinion. "I don't know"
    has to mean "carry on" - degrading on ignorance would break dayparting for
    the majority of boxes to protect the few with a dead battery."""
    from retrobox.daypart import active_daypart

    assert active_daypart([ALL_DAY], at_local(12), clock_trust=lambda: None) is ALL_DAY


def test_a_trust_source_that_breaks_cannot_take_the_television_with_it():
    """Nothing here may ever be the reason a box stops playing."""
    from retrobox.daypart import active_daypart, resolve

    def broken():
        raise RuntimeError("the clock checker fell over")

    assert active_daypart([ALL_DAY], at_local(12), clock_trust=broken) is ALL_DAY
    assert resolve([ALL_DAY], at_local(12), clock_trust=broken).clock_trusted is True


def test_a_trust_source_may_hand_back_its_own_wording():
    """The module that actually knows *why* the clock is wrong should be the
    one that says so, rather than having its reason flattened to a boolean."""
    from retrobox.daypart import resolve

    class Verdict:
        trusted = False
        reason = "This box has never managed to check the time with a time server."
        detail = "Plug in a network cable and it will fix itself."

    decision = resolve([ALL_DAY], at_local(12), clock_trust=lambda: Verdict())
    assert decision.clock_trusted is False
    assert decision.reason == Verdict.reason
    assert decision.detail == Verdict.detail


def test_a_trust_source_may_be_a_plain_value_rather_than_a_callable():
    """A caller that already has the answer should not have to wrap it."""
    from retrobox.daypart import active_daypart

    assert active_daypart([ALL_DAY], at_local(12), clock_trust=False) is None
    assert active_daypart([ALL_DAY], at_local(12), clock_trust=True) is ALL_DAY


def test_saying_trust_this_timestamp_overrides_even_an_impossible_date():
    """The schedule editor asks hypotheticals - "what would be on at 3am" -
    and must still answer them on a box whose own clock is broken."""
    from retrobox.daypart import active_daypart

    assert active_daypart([ALL_DAY], FLAT_BATTERY_EPOCH, clock_trust=True) is ALL_DAY


# -- nothing changes when the clock is fine --------------------------------
def test_a_trustworthy_clock_gives_the_same_answer_every_minute_of_the_day():
    """The whole feature is worthless if it moved anything on a healthy box."""
    from retrobox import daypart

    parts = [
        Daypart(start=parse_clock("22:00"), end=parse_clock("04:00"), name="LATE"),
        Daypart(start=parse_clock("06:00"), end=parse_clock("12:00"), name="AM"),
        Daypart(start=parse_clock("02:00"), end=parse_clock("03:00"), off_air=True),
    ]
    for hour in range(24):
        for minute in (0, 29, 59):
            epoch = at_local(hour, minute)
            plain = daypart.active_daypart(parts, epoch)
            told = daypart.active_daypart(parts, epoch, clock_trust=lambda: True)
            expected = next(
                (p for p in parts if p.contains(hour * 60 + minute)), None
            )
            assert plain is expected
            assert told is expected


# -- and the television itself ---------------------------------------------
def test_a_box_with_a_flat_battery_still_plays_television(tmp_path):
    """The end of the whole exercise: switched off at the wall, dead battery,
    absurd clock, and it still comes up playing the channel."""
    class DeadBatteryClock:
        def __call__(self):
            return FLAT_BATTERY_EPOCH

    lineup = _lineup(
        tmp_path,
        [{"from": "00:00", "to": "24:00", "name": "SHOULD NOT WIN", "off_air": True}],
        DeadBatteryClock(),
    )
    channel = lineup.current

    assert channel.name == "Talk"                 # its own name, not the block's
    assert not channel.is_off_air()               # never a black screen on a guess
    assert channel.tune_in() is not None
    assert channel.tune_in().path.parent.name == "day"


def test_a_trust_source_may_hand_back_a_plain_dictionary():
    """Whatever works out the clock's health reports in dictionaries, because
    that is what a dashboard renders. It should not have to build an object on
    the way past just to be understood here."""
    from retrobox.daypart import resolve

    decision = resolve(
        [ALL_DAY], at_local(12),
        clock_trust=lambda: {
            "trusted": False,
            "reason": "This box has never checked the time with a time server.",
            "detail": "Give it a network connection and it will fix itself.",
        },
    )
    assert decision.clock_trusted is False
    assert decision.part is None
    assert "time server" in decision.reason
    assert "network" in decision.detail


def test_a_report_that_does_not_mention_trust_at_all_leaves_the_television_alone():
    """Anything handed in that this module cannot make sense of means "carry
    on". Dayparting stopping is the expensive outcome, so it needs to be
    something's deliberate decision, never a misread field."""
    from retrobox.daypart import active_daypart

    assert active_daypart([ALL_DAY], at_local(12), clock_trust=lambda: {}) is ALL_DAY
    assert active_daypart(
        [ALL_DAY], at_local(12), clock_trust=lambda: {"summary": "all fine"}
    ) is ALL_DAY


def test_a_report_that_says_it_cannot_tell_is_not_read_as_a_verdict():
    """"Cannot tell" and "no" are different answers, and the difference is the
    whole thing. A box with no timedatectl - a container, a machine running
    chrony - cannot say whether its clock was ever checked, and treating that
    silence as "the clock is wrong" would switch dayparting off on boxes whose
    clocks are perfectly fine.
    """
    from retrobox.daypart import active_daypart

    assert active_daypart(
        [ALL_DAY], at_local(12), clock_trust=lambda: {"trusted": None}
    ) is ALL_DAY
