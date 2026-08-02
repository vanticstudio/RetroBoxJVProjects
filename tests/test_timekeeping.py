"""The clock, on a box nobody can reach.

Two things make the clock matter more here than on most products.

``daypart.py`` changes what a channel *is* by the time of day, and it fails
silently: nothing errors, the box simply plays cartoons at ten at night and the
owner concludes the feature is broken. And a decade-old office mini PC very
often has a flat CMOS battery, so it comes up with an absurd date every time it
loses power - a two-dollar part the owner cannot possibly guess at.

So the tests here are about honesty and restraint, in that order. Honesty:
"synchronised four minutes ago", "never synchronised" and "cannot tell" are
three different answers and none of them may be dressed up as another.
Restraint: detection is for a box that has never been told where it is, never
for one that knows better than its owner, and it may never delay the picture.
"""

import json
import threading
import time

import pytest

from retrobox import timekeeping


# ==========================================================================
# Helpers - nothing in this file may ever reach the network
# ==========================================================================
def opener_for(body, *, seen=None):
    """An opener that answers every provider with ``body``.

    ``body`` may be bytes, str, or a dict (dumped as JSON). Every URL asked
    for is appended to ``seen``, which is how the "made no request at all"
    tests prove a negative.
    """
    if isinstance(body, dict):
        body = json.dumps(body)
    if isinstance(body, str):
        body = body.encode("utf-8")

    def opener(url, timeout):
        if seen is not None:
            seen.append(url)
        return body

    return opener


def refusing_opener(seen=None):
    """What no internet looks like: the call raises."""

    def opener(url, timeout):
        if seen is not None:
            seen.append(url)
        raise OSError("Network is unreachable")

    return opener


def recording_setter(calls):
    def setter(zone, *, allowed):
        calls.append((zone, tuple(allowed)))
        return zone

    return setter


def exploding_setter(zone, *, allowed):
    raise AssertionError(
        f"the timezone was set to {zone!r} when nothing should have been set"
    )


MACHINE_ZONES = (
    "Australia/Melbourne",
    "Australia/Sydney",
    "Europe/London",
    "America/New_York",
    "America/Argentina/Buenos_Aires",
    "Etc/GMT-10",
    "Etc/UTC",
    "UTC",
    "EST5EDT",
)


def detect(tmp_path, **kwargs):
    """Run one detection with every outside edge stubbed by default."""
    kwargs.setdefault("state_path", tmp_path / timekeeping.STATE_NAME)
    kwargs.setdefault("opener", opener_for({"timezone": "Australia/Melbourne",
                                            "ip": "203.0.113.7"}))
    kwargs.setdefault("setter", exploding_setter)
    kwargs.setdefault("zones", lambda: MACHINE_ZONES)
    kwargs.setdefault("current_zone", lambda: "Etc/UTC")
    kwargs.setdefault("fingerprint", lambda: "192.0.2.1 eth0")
    kwargs.setdefault("clock", lambda: 1_800_000_000.0)
    return timekeeping.detect_once(**kwargs)


# ==========================================================================
# A named zone, and never a fixed offset
# ==========================================================================
@pytest.mark.parametrize("zone", [
    "Australia/Melbourne",
    "Europe/London",
    "America/New_York",
    "America/Argentina/Buenos_Aires",
    "Pacific/Auckland",
])
def test_a_real_geographic_zone_is_accepted(zone):
    assert timekeeping.is_named_zone(zone) is True


@pytest.mark.parametrize("zone", [
    "UTC+10",            # an offset wearing a name
    "+10:00",
    "GMT+10",
    "Etc/GMT-10",        # an offset that got itself into the tz database
    "Etc/UTC",
    "UTC",
    "GMT",
    "EST5EDT",           # legacy POSIX spelling
    "EST",
    "AEST",
    "Australia",         # an area with no place in it
    "",
    None,
    123,
    "Australia/Melbourne; rm -rf /",
    "../../etc/passwd",
    "Australia/../Etc/GMT-10",
    "A" * 500,
])
def test_anything_that_is_not_a_named_geographic_zone_is_refused(zone):
    # An offset is wrong for half of every year, so the shape that could carry
    # one is not merely discouraged here - it is unrepresentable.
    assert timekeeping.is_named_zone(zone) is False


def test_a_fixed_offset_from_the_lookup_is_never_written_to_the_system(tmp_path):
    # Etc/GMT-10 is a real entry in the machine's own timezone list, so the
    # whitelist in servicectl would happily accept it. It is still an offset
    # and it would be an hour wrong for half of every year.
    result = detect(
        tmp_path,
        opener=opener_for({"timezone": "Etc/GMT-10", "ip": "203.0.113.7"}),
        setter=exploding_setter,
    )
    assert result["action"] == "refused"
    assert result["zone"] is None


def test_a_named_zone_is_what_reaches_the_system(tmp_path):
    calls = []
    result = detect(tmp_path, setter=recording_setter(calls))
    assert result["action"] == "set"
    assert calls == [("Australia/Melbourne", MACHINE_ZONES)]


def test_the_timezone_is_only_ever_set_through_the_one_privileged_path(tmp_path):
    # servicectl.set_timezone is a whitelist against the machine's own
    # timedatectl list-timezones. Detection must go through it, with that list,
    # rather than growing a second way to reach a privileged command.
    from retrobox import servicectl

    calls = []
    detect(tmp_path, setter=recording_setter(calls))
    assert calls[0][1] == MACHINE_ZONES
    # And with nothing injected, that path is servicectl's own whitelist.
    assert timekeeping.DEFAULT_SETTER is servicectl.set_timezone


def test_a_zone_this_machine_has_never_heard_of_is_refused(tmp_path):
    result = detect(
        tmp_path,
        opener=opener_for({"timezone": "Mars/Olympus_Mons", "ip": "203.0.113.7"}),
        setter=exploding_setter,
    )
    assert result["action"] == "refused"


# ==========================================================================
# A choice the owner made outranks anything we detect
# ==========================================================================
def test_a_timezone_the_owner_set_by_hand_survives_detection(tmp_path):
    state = tmp_path / timekeeping.STATE_NAME
    timekeeping.record_manual_timezone(state, "Australia/Sydney")

    result = detect(
        tmp_path,
        current_zone=lambda: "Australia/Sydney",
        setter=exploding_setter,
        opener=opener_for({"timezone": "Europe/London", "ip": "198.51.100.4"}),
    )
    assert result["action"] == "manual"
    assert timekeeping.read_state(state)["zone"] == "Australia/Sydney"


def test_detection_still_reports_what_it_thinks_when_the_owner_chose_the_zone(tmp_path):
    # "Leaves it alone" is not "says nothing": an owner who moved house wants
    # to be told their box thinks it is somewhere else now.
    state = tmp_path / timekeeping.STATE_NAME
    timekeeping.record_manual_timezone(state, "Australia/Sydney")

    result = detect(
        tmp_path,
        current_zone=lambda: "Australia/Sydney",
        opener=opener_for({"timezone": "Europe/London", "ip": "198.51.100.4"}),
    )
    assert result["suggested"] == "Europe/London"
    assert "Australia/Sydney" in result["note"]


def test_a_zone_that_was_already_set_before_detection_ever_ran_is_left_alone(tmp_path):
    # No record at all, but the box is already on a real geographic zone. Some
    # human put it there. Detection is for a box that has never been told.
    result = detect(
        tmp_path,
        current_zone=lambda: "Europe/London",
        setter=exploding_setter,
        opener=opener_for({"timezone": "Australia/Melbourne", "ip": "203.0.113.7"}),
    )
    assert result["action"] == "manual"
    assert result["source"] == timekeeping.SOURCE_PREEXISTING


def test_a_zone_changed_behind_our_back_is_treated_as_a_choice_somebody_made(tmp_path):
    # Belt and braces for the one way this could go badly wrong in the field.
    # The record is written by the dashboard when somebody uses the picker; if
    # that call is ever missed, or the zone is changed by some other route,
    # the record would still say "detected" and detection would feel entitled
    # to change it back. So a live zone that is not the one we set is read as
    # somebody having made a choice, whatever the record says.
    calls = []
    detect(tmp_path, setter=recording_setter(calls))          # we set Melbourne

    result = detect(
        tmp_path,
        fingerprint=lambda: "10.0.0.1 wlan0",
        current_zone=lambda: "Europe/Berlin",                  # somebody moved it
        setter=exploding_setter,
        opener=opener_for({"timezone": "Europe/London", "ip": "198.51.100.4"}),
    )
    assert result["action"] == "manual"
    assert result["zone"] == "Europe/Berlin"


def test_a_box_left_on_the_installer_default_has_never_been_told_where_it_is(tmp_path):
    calls = []
    result = detect(tmp_path, current_zone=lambda: "Etc/UTC",
                    setter=recording_setter(calls))
    assert result["action"] == "set"
    assert calls


# ==========================================================================
# Only when the box looks like it moved
# ==========================================================================
def test_a_later_boot_on_the_same_network_makes_no_request_at_all(tmp_path):
    # A customer on a VPN would otherwise find their box hopping timezones,
    # and every boot would be one more thing told to a third party.
    calls, seen = [], []
    detect(tmp_path, setter=recording_setter(calls),
           opener=opener_for({"timezone": "Australia/Melbourne",
                              "ip": "203.0.113.7"}, seen=seen))
    assert seen  # the first boot did ask

    seen.clear()
    result = detect(tmp_path, setter=exploding_setter,
                    opener=opener_for({"timezone": "Europe/London",
                                       "ip": "198.51.100.4"}, seen=seen))
    assert seen == []
    assert result["action"] == "unchanged"


def test_the_same_public_ip_on_a_later_boot_does_not_re_detect(tmp_path):
    calls, seen = [], []
    detect(tmp_path, setter=recording_setter(calls))

    # A new router on the same connection: the local network looks different,
    # so we do ask - but the answer is the same public address, so the box did
    # not move and nothing is changed.
    result = detect(
        tmp_path,
        fingerprint=lambda: "10.0.0.1 wlan0",
        setter=exploding_setter,
        opener=opener_for({"timezone": "Europe/London", "ip": "203.0.113.7"},
                          seen=seen),
    )
    assert seen  # it did ask
    assert result["action"] == "unchanged"
    assert timekeeping.read_state(tmp_path / timekeeping.STATE_NAME)["zone"] == \
        "Australia/Melbourne"


def test_a_box_that_moved_to_a_new_network_and_a_new_address_is_re_detected(tmp_path):
    calls = []
    detect(tmp_path, setter=recording_setter(calls))
    detect(
        tmp_path,
        fingerprint=lambda: "10.0.0.1 wlan0",
        setter=recording_setter(calls),
        opener=opener_for({"timezone": "Europe/London", "ip": "198.51.100.4"}),
    )
    assert [zone for zone, _ in calls] == ["Australia/Melbourne", "Europe/London"]


def test_a_factory_reset_lets_detection_run_again(tmp_path):
    calls = []
    detect(tmp_path, setter=recording_setter(calls))
    (tmp_path / timekeeping.STATE_NAME).unlink()

    detect(tmp_path, setter=recording_setter(calls),
           opener=opener_for({"timezone": "Europe/London", "ip": "198.51.100.4"}))
    assert len(calls) == 2


def test_the_public_address_is_remembered_only_as_a_fingerprint(tmp_path):
    # We only ever compare it for equality, so there is no reason to keep a
    # durable record of where this customer lives.
    detect(tmp_path, setter=recording_setter([]))
    written = (tmp_path / timekeeping.STATE_NAME).read_text(encoding="utf-8")
    assert "203.0.113.7" not in written


# ==========================================================================
# The off switch, and what leaves the box
# ==========================================================================
def test_detection_turned_off_makes_no_outbound_request_at_all(tmp_path):
    seen = []
    result = detect(tmp_path, enabled=False, opener=opener_for({}, seen=seen),
                    setter=exploding_setter)
    assert seen == []
    assert result["action"] == "disabled"


def minimal_config(tmp_path, **extra):
    from retrobox.config import config_from_dict

    (tmp_path / "shows").mkdir(exist_ok=True)
    return config_from_dict({
        "channels": [{"number": 2, "name": "Shows", "path": str(tmp_path / "shows")}],
        **extra,
    })


def test_detection_is_on_by_default(tmp_path):
    from retrobox.config import TimeConfig

    # A box with the wrong timezone is broken in a way its owner cannot
    # diagnose, so this has to work on a box nobody configured.
    assert TimeConfig().detect_timezone is True
    assert minimal_config(tmp_path).time.detect_timezone is True


def test_the_owner_can_turn_detection_off_in_the_config_file(tmp_path):
    config = minimal_config(tmp_path, time={"detect_timezone": False})
    assert config.time.detect_timezone is False


def test_a_broken_time_section_never_costs_the_picture(tmp_path):
    # Same rule as everything else in config.py: this box has no SSH and sits
    # in a living room, so a bad setting falls back rather than refusing to
    # load. A config that will not load takes the television away for good.
    config = minimal_config(tmp_path, time={"detect_timezone": "yes please"})
    assert config.time.detect_timezone is True


def test_start_makes_no_request_when_detection_is_turned_off(tmp_path):
    from retrobox.config import Config, TimeConfig

    seen = []
    config = Config(channels=[], time=TimeConfig(detect_timezone=False))
    thread = timekeeping.start(
        state_path=tmp_path / timekeeping.STATE_NAME,
        config=config,
        opener=opener_for({}, seen=seen),
        setter=exploding_setter,
        zones=lambda: MACHINE_ZONES,
        current_zone=lambda: "Etc/UTC",
    )
    if thread is not None:
        thread.join(timeout=5)
    assert seen == []


def test_the_lookup_sends_nothing_but_the_request():
    # No identifier, no serial, no version, no query parameters. This box does
    # not phone home and this must not become the thing that starts it.
    for provider in timekeeping.PROVIDERS:
        assert "?" not in provider.url, provider.url
        assert provider.url.startswith("https://"), provider.url

    sent = {}

    class FakeResponse:
        def read(self, size=-1):
            return b'{"timezone": "Australia/Melbourne"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import urllib.request

    def fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["headers"] = dict(request.header_items())
        sent["data"] = request.data
        return FakeResponse()

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        timekeeping._urlopen(timekeeping.PROVIDERS[0].url, 1.0)
    finally:
        urllib.request.urlopen = original

    assert sent["data"] is None
    from retrobox import __version__

    blob = " ".join(f"{k}: {v}" for k, v in sent["headers"].items())
    assert __version__ not in blob
    assert "retrobox" not in blob.lower()


# ==========================================================================
# No internet, and responses that are not answers
# ==========================================================================
def test_no_internet_fails_quietly_and_changes_nothing(tmp_path):
    result = detect(tmp_path, opener=refusing_opener(), setter=exploding_setter)
    assert result["action"] == "no-answer"
    assert result["zone"] is None
    # Nothing recorded, so the box tries again next time rather than deciding
    # it has already looked.
    assert not (tmp_path / timekeeping.STATE_NAME).exists()


def test_a_lookup_that_never_answers_does_not_delay_start_up(tmp_path):
    from retrobox.config import Config

    started = threading.Event()
    release = threading.Event()

    def slow_opener(url, timeout):
        started.set()
        release.wait(10)
        raise OSError("gave up")

    began = time.monotonic()
    thread = timekeeping.start(
        state_path=tmp_path / timekeeping.STATE_NAME,
        config=Config(channels=[]),
        opener=slow_opener,
        setter=exploding_setter,
        zones=lambda: MACHINE_ZONES,
        current_zone=lambda: "Etc/UTC",
        fingerprint=lambda: "192.0.2.1 eth0",
    )
    elapsed = time.monotonic() - began
    try:
        assert elapsed < 1.0, f"start-up waited {elapsed:.2f}s for the lookup"
        assert thread is not None and thread.daemon, "must never hold up a shutdown"
        assert started.wait(5), "the lookup should be happening on the thread"
    finally:
        release.set()
        thread.join(timeout=10)


@pytest.mark.parametrize("body", [
    b"",                                        # empty
    b"   ",
    b"<html><body>404 not found</body></html>", # the day it disappears
    b"{",                                       # truncated
    b"null",
    b"[]",
    b'{"timezone": null}',
    b'{"timezone": {"nested": "Australia/Melbourne"}}',
    b'{"timezone": ["Australia/Melbourne"]}',
    b'{"timezone": "\\u0000Australia/Melbourne"}',
    b'{"timezone": "Australia/Melbourne\\n\\nEtc/GMT-10"}',
    b'{"error": true, "reason": "quota exceeded"}',
])
def test_a_response_that_is_not_an_answer_changes_nothing(tmp_path, body):
    result = detect(tmp_path, opener=opener_for(body), setter=exploding_setter)
    assert result["action"] in ("no-answer", "refused")
    assert result["zone"] is None


def test_an_oversized_response_is_refused(tmp_path):
    huge = b'{"timezone": "Australia/Melbourne", "junk": "' + b"x" * 200_000 + b'"}'
    result = detect(tmp_path, opener=opener_for(huge), setter=exploding_setter)
    assert result["action"] in ("no-answer", "refused")


def test_a_provider_that_has_gone_falls_through_to_the_next_one(tmp_path):
    calls, seen = [], []

    def opener(url, timeout):
        seen.append(url)
        if url == timekeeping.PROVIDERS[0].url:
            raise OSError("host not found")
        return json.dumps({"ip": "203.0.113.7",
                           "timezone": {"id": "Australia/Melbourne"}}).encode()

    result = detect(tmp_path, opener=opener, setter=recording_setter(calls))
    assert result["action"] == "set"
    assert len(seen) == 2


def test_detection_never_raises_whatever_comes_back(tmp_path):
    def hostile(url, timeout):
        raise RuntimeError("something nobody thought of")

    result = detect(tmp_path, opener=hostile, setter=exploding_setter)
    assert result["action"] in ("no-answer", "failed")


# ==========================================================================
# Time sync, reported honestly
# ==========================================================================
def test_a_recent_sync_is_reported_with_when_it_happened(tmp_path):
    clock_file = tmp_path / "clock"
    clock_file.touch()
    import os

    os.utime(clock_file, (1_800_000_000.0 - 240, 1_800_000_000.0 - 240))

    status = timekeeping.sync_status(
        reader=lambda: "NTP=yes\nNTPSynchronized=yes\n",
        clock_file=clock_file,
        clock=lambda: 1_800_000_000.0,
    )
    assert status["synchronised"] is True
    assert status["last_sync_state"] == "known"
    assert status["last_sync"] == pytest.approx(1_800_000_000.0 - 240)
    assert "4 minutes ago" in status["summary"]


def test_never_synchronised_is_a_different_answer_from_synchronised(tmp_path):
    status = timekeeping.sync_status(
        reader=lambda: "NTP=yes\nNTPSynchronized=no\n",
        clock_file=tmp_path / "not-here",
        clock=lambda: 1_800_000_000.0,
    )
    assert status["synchronised"] is False
    assert status["last_sync_state"] == "never"
    assert status["last_sync"] is None
    assert "never" in status["summary"].lower()


def test_cannot_tell_is_not_dressed_up_as_either_of_the_other_two(tmp_path):
    # No timedatectl at all - a container, a non-systemd box, a machine using
    # chrony. We do not know, and saying "never synchronised" would be a lie
    # in exactly the direction that sends somebody to buy a battery.
    status = timekeeping.sync_status(
        reader=lambda: "",
        clock_file=tmp_path / "not-here",
        clock=lambda: 1_800_000_000.0,
    )
    assert status["synchronised"] is None
    assert status["last_sync_state"] == "unknown"
    assert status["last_sync"] is None
    assert "never" not in status["summary"].lower()
    assert status["summary"]


def test_a_sync_service_that_is_switched_off_is_said_so_plainly(tmp_path):
    status = timekeeping.sync_status(
        reader=lambda: "NTP=no\nNTPSynchronized=no\n",
        clock_file=tmp_path / "not-here",
        clock=lambda: 1_800_000_000.0,
    )
    assert status["enabled"] is False
    # It has to say what it costs, not just that a flag is off, and it has to
    # say what to do - this box cannot switch it on for itself.
    assert "timedatectl set-ntp true" in status["fix"]
    assert "day" in status["fix"]


def test_a_sync_service_that_is_on_is_not_nagged_about(tmp_path):
    status = timekeeping.sync_status(
        reader=lambda: "NTP=yes\nNTPSynchronized=yes\n",
        clock_file=tmp_path / "not-here",
        clock=lambda: 1_800_000_000.0,
    )
    assert status["enabled"] is True
    assert status["fix"] is None


def test_the_status_reader_never_raises_when_the_command_is_missing():
    def missing():
        raise FileNotFoundError("timedatectl")

    status = timekeeping.sync_status(reader=missing)
    assert status["synchronised"] is None


# ==========================================================================
# The flat CMOS battery
# ==========================================================================
def test_a_clock_from_before_this_software_existed_is_implausible():
    assert timekeeping.clock_is_plausible(now=1_800_000_000.0) is True
    assert timekeeping.clock_is_plausible(now=1_300_000_000.0) is False   # 2011
    assert timekeeping.clock_is_plausible(now=0.0) is False               # 1970


def test_an_implausible_clock_with_no_internet_names_the_two_dollar_part(tmp_path):
    report = timekeeping.clock_report(
        now=1_300_000_000.0,
        sync={"synchronised": False, "last_sync": None,
              "last_sync_state": "never", "enabled": True, "active": True,
              "summary": "Never synchronised", "fix": None,
              "service": "systemd-timesyncd"},
    )
    assert report["alarm"] is True
    assert report["plausible"] is False
    assert "CR2032" in report["detail"]
    # And it must say what it MEANS, not just that a number is wrong.
    assert "day" in report["detail"].lower()


def test_a_hardware_clock_the_network_corrected_is_still_reported(tmp_path):
    # The clock is right now, but it will be wrong again at the next power cut
    # and nobody would ever find out otherwise.
    report = timekeeping.clock_report(
        now=1_800_000_000.0,
        sync={"synchronised": True, "last_sync": 1_800_000_000.0 - 60,
              "last_sync_state": "known", "enabled": True, "active": True,
              "summary": "Synchronised 1 minute ago", "fix": None,
              "service": "systemd-timesyncd"},
        boot_clock_wrong=True,
    )
    assert report["plausible"] is True
    assert "CR2032" in report["detail"]


def test_a_healthy_clock_raises_no_alarm():
    report = timekeeping.clock_report(
        now=1_800_000_000.0,
        sync={"synchronised": True, "last_sync": 1_800_000_000.0 - 60,
              "last_sync_state": "known", "enabled": True, "active": True,
              "summary": "Synchronised 1 minute ago", "fix": None,
              "service": "systemd-timesyncd"},
    )
    assert report["alarm"] is False
    assert report["headline"] is None


def test_a_wrong_clock_is_recorded_at_start_up_so_it_can_be_reported_later(tmp_path):
    from retrobox.config import Config, TimeConfig

    state = tmp_path / timekeeping.STATE_NAME
    thread = timekeeping.start(
        state_path=state,
        config=Config(channels=[], time=TimeConfig(detect_timezone=False)),
        clock=lambda: 1_300_000_000.0,
        opener=refusing_opener(),
        setter=exploding_setter,
        zones=lambda: MACHINE_ZONES,
        current_zone=lambda: "Etc/UTC",
    )
    if thread is not None:
        thread.join(timeout=5)
    assert timekeeping.read_state(state)["boot_clock_wrong"] is True


def test_a_flat_battery_is_not_forgotten_the_moment_the_clock_is_corrected(tmp_path):
    # The whole reason to write it down is that the fix erases the evidence.
    # A box with a flat cell and a working connection is wrong for about forty
    # seconds and then perfectly normal, and the dashboard restarting must not
    # be what makes the most common fault on this hardware disappear.
    state = tmp_path / timekeeping.STATE_NAME
    timekeeping.note_boot_clock(state, clock=lambda: 1_300_000_000.0)  # power-on
    timekeeping.note_boot_clock(state, clock=lambda: 1_800_000_000.0)  # restart

    assert timekeeping.read_state(state)["boot_clock_wrong"] is False
    report = timekeeping.report(state_path=state, now=1_800_000_000.0,
                                sync={"synchronised": True, "last_sync": None,
                                      "last_sync_state": "known",
                                      "summary": "", "enabled": True,
                                      "fix": None, "service": None},
                                current_zone=lambda: "Australia/Melbourne")
    assert "CR2032" in report["detail"]


def test_a_box_whose_clock_has_always_been_right_is_never_told_about_a_battery(tmp_path):
    state = tmp_path / timekeeping.STATE_NAME
    timekeeping.note_boot_clock(state, clock=lambda: 1_800_000_000.0)
    report = timekeeping.report(state_path=state, now=1_800_000_000.0,
                                sync={"synchronised": True, "last_sync": None,
                                      "last_sync_state": "known",
                                      "summary": "", "enabled": True,
                                      "fix": None, "service": None},
                                current_zone=lambda: "Australia/Melbourne")
    assert report["detail"] is None
    assert report["alarm"] is False


def test_a_wrong_clock_never_stops_the_box_starting(tmp_path):
    from retrobox.config import Config

    # Every outside edge broken at once: no state directory, no network, no
    # timezone list. start() still returns, and returns quickly.
    def broken_zones():
        raise OSError("timedatectl is not on this box")

    thread = timekeeping.start(
        state_path=tmp_path / "no-such-directory" / timekeeping.STATE_NAME,
        config=Config(channels=[]),
        clock=lambda: 1_300_000_000.0,
        opener=refusing_opener(),
        setter=exploding_setter,
        zones=broken_zones,
        current_zone=lambda: "Etc/UTC",
        fingerprint=lambda: None,
    )
    if thread is not None:
        thread.join(timeout=10)


# ==========================================================================
# What the dashboard reads
# ==========================================================================
def test_the_report_holds_everything_a_page_needs_and_never_raises(tmp_path):
    report = timekeeping.report(
        state_path=tmp_path / timekeeping.STATE_NAME,
        now=1_800_000_000.0,
        sync={"synchronised": None, "last_sync": None,
              "last_sync_state": "unknown", "enabled": None, "active": None,
              "summary": "Cannot tell", "fix": None, "service": None},
        current_zone=lambda: "Etc/UTC",
        detect_enabled=True,
    )
    for key in ("clock", "sync", "timezone", "detection", "alarm"):
        assert key in report, key
    assert report["timezone"]["source"] == timekeeping.SOURCE_UNKNOWN
    assert report["detection"]["enabled"] is True
    assert report["detection"]["what_is_sent"]


def test_the_report_says_who_chose_the_timezone(tmp_path):
    state = tmp_path / timekeeping.STATE_NAME
    timekeeping.record_manual_timezone(state, "Australia/Sydney")
    report = timekeeping.report(state_path=state, now=1_800_000_000.0,
                                sync=None, current_zone=lambda: "Australia/Sydney")
    assert report["timezone"]["source"] == timekeeping.SOURCE_MANUAL


def test_an_unreadable_state_file_is_treated_as_no_record(tmp_path):
    state = tmp_path / timekeeping.STATE_NAME
    state.write_text("this is not json", encoding="utf-8")
    assert timekeeping.read_state(state) == {}


def test_recording_a_manual_choice_refuses_anything_that_is_not_a_zone(tmp_path):
    state = tmp_path / timekeeping.STATE_NAME
    with pytest.raises(ValueError):
        timekeeping.record_manual_timezone(state, "not a zone; rm -rf /")


def test_an_owner_who_deliberately_chooses_utc_is_protected_like_any_other(tmp_path):
    # "UTC" is not a geographic zone, so detection would otherwise read it as
    # a box that has never been told. The record is what makes the difference
    # between "nobody has said" and "somebody said this".
    state = tmp_path / timekeeping.STATE_NAME
    timekeeping.record_manual_timezone(state, "UTC")
    result = detect(tmp_path, current_zone=lambda: "UTC", setter=exploding_setter)
    assert result["action"] == "manual"
