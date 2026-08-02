""""Is my box coping?", answered with measurements instead of a queue length.

Every test here builds its own /proc out of files in tmp_path. That is not
only so the suite runs on a Mac with no /proc - it is so the awkward cases can
actually be tested. A real box will not produce a truncated /proc/stat on
demand, and those are exactly the readings that must not take the System page
down.

The numbers below are chosen to be obviously wrong if the arithmetic slips:
a core that goes from 100 ticks idle to 200 ticks idle while 300 ticks pass in
total is two thirds busy, and nothing else.
"""

import ast
import os
import re
import sys
from collections import deque
from pathlib import Path

import pytest

from retrobox import metrics


# ==========================================================================
# Fixtures: a synthetic /proc, because this machine has none
# ==========================================================================
STAT_IDLE = """\
cpu  1000 0 500 8000 100 0 0 0 0 0
cpu0 500 0 250 4000 50 0 0 0 0 0
cpu1 500 0 250 4000 50 0 0 0 0 0
intr 12345
ctxt 67890
"""

MEMINFO = """\
MemTotal:        4030008 kB
MemFree:          210000 kB
MemAvailable:    2015004 kB
Buffers:           50000 kB
Cached:          1600000 kB
SwapTotal:       2097148 kB
SwapFree:        2097148 kB
"""

LOADAVG = "1.53 0.98 0.74 2/331 4242\n"


def make_proc(tmp_path, *, stat=STAT_IDLE, meminfo=MEMINFO, loadavg=LOADAVG):
    """A /proc with only the three files this module reads."""
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for name, text in (("stat", stat), ("meminfo", meminfo), ("loadavg", loadavg)):
        if text is None:
            continue
        (root / name).write_text(text)
    return root


def cpu_stat(*, total_busy, total_idle, cores=None):
    """A /proc/stat with the busy and idle tick counts we want to test."""
    lines = [f"cpu  {total_busy} 0 0 {total_idle} 0 0 0 0 0 0"]
    for index, (busy, idle) in enumerate(cores or []):
        lines.append(f"cpu{index} {busy} 0 0 {idle} 0 0 0 0 0 0")
    lines.append("intr 1")
    return "\n".join(lines) + "\n"


class FakeClock:
    """A clock the test moves by hand, so no test ever sleeps."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds
        return self.now


def collector(tmp_path, clock=None, **kwargs):
    kwargs.setdefault("proc_root", make_proc(tmp_path))
    return metrics.Collector(clock=clock or FakeClock(), **kwargs)


def sample_now(box, clock, seconds=None, stat=None):
    """Move past the sample interval and take a reading.

    ``stat`` replaces /proc/stat first, because on a real box the tick
    counters always move on between two samples.
    """
    if stat is not None:
        (box.proc_root / "stat").write_text(stat)
    clock.tick(seconds if seconds is not None else metrics.SAMPLE_INTERVAL_SECONDS)
    box.someone_is_watching()
    return box.poll()


# ==========================================================================
# CPU: a percentage, from a delta, and never from the load average
# ==========================================================================
def test_the_cpu_percentage_comes_from_the_difference_between_two_reads(tmp_path):
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=1000, total_idle=1000))
    before = metrics.read_cpu_times(proc)
    (proc / "stat").write_text(cpu_stat(total_busy=1200, total_idle=1100))
    after = metrics.read_cpu_times(proc)

    # 200 busy ticks and 100 idle ticks passed: two thirds of the time was
    # spent doing something.
    assert metrics.busy_percent(before["cpu"], after["cpu"]) == pytest.approx(66.7, abs=0.1)


def test_a_cpu_percentage_is_always_between_zero_and_one_hundred(tmp_path):
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=0, total_idle=0))
    before = metrics.read_cpu_times(proc)

    # Every tick busy.
    (proc / "stat").write_text(cpu_stat(total_busy=500, total_idle=0))
    assert metrics.busy_percent(before["cpu"], metrics.read_cpu_times(proc)["cpu"]) == 100.0

    # Every tick idle.
    (proc / "stat").write_text(cpu_stat(total_busy=0, total_idle=500))
    assert metrics.busy_percent(before["cpu"], metrics.read_cpu_times(proc)["cpu"]) == 0.0

    # And it stays a percentage however unpleasant the counters get. Nothing
    # on a customer's page is allowed to read 140%.
    ticks = metrics.CpuTicks
    pairs = [
        (ticks(0, 0), ticks(1, 0)),
        (ticks(0, 0), ticks(0, 1)),
        (ticks(1, 1), ticks(10 ** 18, 1)),
        (ticks(1, 1), ticks(1, 10 ** 18)),
        (ticks(2 ** 31, 2 ** 31), ticks(2 ** 32, 2 ** 32)),
    ]
    for before_ticks, after_ticks in pairs:
        assert 0.0 <= metrics.busy_percent(before_ticks, after_ticks) <= 100.0


def test_counters_that_went_backwards_produce_no_percentage_at_all(tmp_path):
    # /proc/stat only ever counts up, so a smaller number means we are not
    # comparing what we think we are. Guessing here would print a number that
    # looks like a measurement.
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=5000, total_idle=5000))
    before = metrics.read_cpu_times(proc)
    (proc / "stat").write_text(cpu_stat(total_busy=10, total_idle=10))
    assert metrics.busy_percent(before["cpu"], metrics.read_cpu_times(proc)["cpu"]) is None


def test_two_identical_reads_produce_no_percentage_rather_than_zero(tmp_path):
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=1000, total_idle=1000))
    before = metrics.read_cpu_times(proc)
    after = metrics.read_cpu_times(proc)
    # No time passed between them, so nothing has been measured. 0% would be a
    # claim that the box was idle.
    assert metrics.busy_percent(before["cpu"], after["cpu"]) is None


def test_the_very_first_sample_reports_no_percentage_rather_than_inventing_one(tmp_path):
    clock = FakeClock()
    box = collector(tmp_path, clock)
    box.someone_is_watching()
    first = box.poll()

    assert first["cpu"]["percent"] is None
    assert first["cpu"]["state"] == "measuring"
    # And the sentence at the top must not claim health it has not measured.
    assert first["verdict"]["state"] == "measuring"
    assert "measur" in first["verdict"]["sentence"].lower()


def test_the_second_sample_is_the_first_real_measurement(tmp_path):
    clock = FakeClock()
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=0, total_idle=1000))
    box = metrics.Collector(proc_root=proc, clock=clock)
    box.someone_is_watching()
    box.poll()

    (proc / "stat").write_text(cpu_stat(total_busy=500, total_idle=1500))
    snapshot = sample_now(box, clock)
    assert snapshot["cpu"]["percent"] == pytest.approx(50.0, abs=0.1)
    assert snapshot["cpu"]["state"] == "measured"


def test_one_core_pinned_and_one_idle_is_not_reported_as_both_at_half(tmp_path):
    # This is the whole reason per-core numbers are here. On a two core box
    # these two situations feel completely different and average identically.
    clock = FakeClock()
    proc = make_proc(
        tmp_path,
        stat=cpu_stat(total_busy=0, total_idle=2000, cores=[(0, 1000), (0, 1000)]),
    )
    box = metrics.Collector(proc_root=proc, clock=clock)
    box.someone_is_watching()
    box.poll()

    (proc / "stat").write_text(
        cpu_stat(total_busy=1000, total_idle=3000, cores=[(1000, 1000), (0, 2000)])
    )
    snapshot = sample_now(box, clock)

    assert snapshot["cpu"]["percent"] == pytest.approx(50.0, abs=0.1)
    assert snapshot["cpu"]["per_core"]["cpu0"] == pytest.approx(100.0, abs=0.1)
    assert snapshot["cpu"]["per_core"]["cpu1"] == pytest.approx(0.0, abs=0.1)
    assert snapshot["cpu"]["busiest_core_percent"] == pytest.approx(100.0, abs=0.1)


def test_the_number_of_cores_comes_from_the_file_not_from_this_machine(tmp_path):
    proc = make_proc(
        tmp_path, stat=cpu_stat(total_busy=1, total_idle=1, cores=[(1, 1), (1, 1)])
    )
    assert metrics.core_count(proc) == 2


# ==========================================================================
# Everything missing, unreadable or malformed - the normal case in the field
# ==========================================================================
def test_a_missing_proc_stat_is_reported_as_unknown_rather_than_crashing(tmp_path):
    proc = make_proc(tmp_path)
    (proc / "stat").unlink()
    assert metrics.read_cpu_times(proc) is None

    clock = FakeClock()
    box = metrics.Collector(proc_root=proc, clock=clock)
    box.someone_is_watching()
    snapshot = box.poll()
    sample_now(box, clock)
    assert box.snapshot()["cpu"]["percent"] is None
    assert box.snapshot()["cpu"]["state"] == "unknown"
    assert snapshot["verdict"]["sentence"]          # still says something


def test_a_missing_proc_meminfo_is_reported_as_unknown_rather_than_crashing(tmp_path):
    proc = make_proc(tmp_path)
    (proc / "meminfo").unlink()
    assert metrics.read_memory(proc) is None

    box = metrics.Collector(proc_root=proc, clock=FakeClock())
    box.someone_is_watching()
    assert box.poll()["memory"] is None


def test_a_missing_load_average_is_reported_as_unknown_rather_than_crashing(tmp_path):
    proc = make_proc(tmp_path)
    (proc / "loadavg").unlink()
    assert metrics.read_load_average(proc) is None

    box = metrics.Collector(proc_root=proc, clock=FakeClock())
    box.someone_is_watching()
    assert box.poll()["load"] is None


def test_an_unreadable_proc_file_is_reported_as_unknown_rather_than_crashing(tmp_path):
    proc = make_proc(tmp_path)
    os.chmod(proc / "stat", 0o000)
    os.chmod(proc / "meminfo", 0o000)
    try:
        assert metrics.read_cpu_times(proc) is None
        assert metrics.read_memory(proc) is None
    finally:
        os.chmod(proc / "stat", 0o644)
        os.chmod(proc / "meminfo", 0o644)


def test_a_truncated_proc_stat_line_is_ignored_rather_than_believed(tmp_path):
    # A short read can cut a line in half. Half a line has half the columns,
    # and treating the last one as idle time would report a wild percentage.
    proc = make_proc(tmp_path, stat="cpu  1000 0 500\ncpu0 500 0\n")
    assert metrics.read_cpu_times(proc) is None


def test_a_file_that_stops_mid_number_still_yields_the_lines_before_it(tmp_path):
    proc = make_proc(
        tmp_path,
        stat="cpu  1000 0 500 8000 100 0 0 0\ncpu0 500 0 25",
    )
    times = metrics.read_cpu_times(proc)
    assert times is not None and "cpu" in times and "cpu0" not in times


def test_unexpected_extra_columns_in_proc_stat_do_not_change_the_answer(tmp_path):
    # The kernel has added columns to this line before and may again. Guest
    # time is already counted inside user time, so a reader that blindly sums
    # everything reports more than 100% busy on a box running a VM.
    proc = make_proc(tmp_path, stat="cpu  100 0 0 100 0 0 0 0 50 25 999 999\n")
    times = metrics.read_cpu_times(proc)
    assert times["cpu"].busy == 100 and times["cpu"].idle == 100


def test_a_proc_stat_full_of_nonsense_is_reported_as_unknown(tmp_path):
    proc = make_proc(tmp_path, stat="cpu  banana pear plum apple\n")
    assert metrics.read_cpu_times(proc) is None


def test_a_truncated_meminfo_still_reports_what_it_can(tmp_path):
    proc = make_proc(tmp_path, meminfo="MemTotal:        4030008 kB\nMemFree:     2000")
    memory = metrics.read_memory(proc)
    # MemAvailable never arrived, so it falls back to free memory rather than
    # refusing to say anything.
    assert memory is not None
    assert memory["total_bytes"] == 4030008 * 1024
    assert memory["swap"] is None


def test_a_meminfo_with_no_total_says_nothing_rather_than_guessing(tmp_path):
    proc = make_proc(tmp_path, meminfo="Buffers:  50000 kB\n")
    assert metrics.read_memory(proc) is None


# ==========================================================================
# Memory and swap
# ==========================================================================
def test_memory_is_reported_as_both_a_percentage_and_real_numbers(tmp_path):
    memory = metrics.read_memory(make_proc(tmp_path))
    assert memory["total_bytes"] == 4030008 * 1024
    assert memory["available_bytes"] == 2015004 * 1024
    assert memory["used_bytes"] == (4030008 - 2015004) * 1024
    assert memory["percent_used"] == pytest.approx(50.0, abs=0.1)


def test_swap_is_reported_only_when_some_of_it_is_actually_in_use(tmp_path):
    # A box with swap configured and untouched is a healthy box. Showing it a
    # zero invites worry about a number that means nothing.
    assert metrics.read_memory(make_proc(tmp_path))["swap"] is None

    busy = MEMINFO.replace("SwapFree:        2097148 kB", "SwapFree:         097148 kB")
    swap = metrics.read_memory(make_proc(tmp_path, meminfo=busy))["swap"]
    assert swap is not None
    assert swap["used_bytes"] == (2097148 - 97148) * 1024
    assert swap["percent_used"] == pytest.approx(95.4, abs=0.1)


def test_a_box_with_no_swap_at_all_is_not_a_box_in_trouble(tmp_path):
    none = MEMINFO.replace("SwapTotal:       2097148 kB", "SwapTotal:             0 kB")
    none = none.replace("SwapFree:        2097148 kB", "SwapFree:              0 kB")
    assert metrics.read_memory(make_proc(tmp_path, meminfo=none))["swap"] is None


# ==========================================================================
# The load average - kept, but explained
# ==========================================================================
def test_the_load_average_is_explained_in_words_rather_than_left_as_a_number(tmp_path):
    load = metrics.read_load_average(make_proc(tmp_path))
    assert load["one_minute"] == 1.53
    assert load["five_minutes"] == 0.98
    assert load["fifteen_minutes"] == 0.74
    # The number on its own is a queue length, which answers nothing.
    assert load["summary"] and not load["summary"][0].isdigit()


def test_a_load_average_below_the_core_count_is_described_as_keeping_up(tmp_path):
    proc = make_proc(
        tmp_path,
        stat=cpu_stat(total_busy=1, total_idle=1, cores=[(1, 1), (1, 1)]),
        loadavg="0.40 0.30 0.20 1/100 5\n",
    )
    load = metrics.read_load_average(proc)
    assert load["cores"] == 2
    assert "wait" in load["summary"].lower() or "keep" in load["summary"].lower()


def test_a_load_average_well_over_the_core_count_says_work_is_queuing(tmp_path):
    proc = make_proc(
        tmp_path,
        stat=cpu_stat(total_busy=1, total_idle=1, cores=[(1, 1), (1, 1)]),
        loadavg="6.00 5.00 4.00 9/100 5\n",
    )
    assert "queu" in metrics.read_load_average(proc)["summary"].lower()


def test_a_garbled_load_average_is_reported_as_unknown(tmp_path):
    assert metrics.read_load_average(make_proc(tmp_path, loadavg="not a number\n")) is None


# ==========================================================================
# Trend and peak - the cabinet behind the television
# ==========================================================================
def climb(box, clock, proc, steps, busy_per_step, idle_per_step):
    busy = idle = 0
    for _ in range(steps):
        busy += busy_per_step
        idle += idle_per_step
        (proc / "stat").write_text(cpu_stat(total_busy=busy, total_idle=idle))
        sample_now(box, clock)


def test_the_peak_is_remembered_after_the_busy_moment_has_passed(tmp_path):
    clock = FakeClock()
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=0, total_idle=0))
    box = metrics.Collector(proc_root=proc, clock=clock)
    box.someone_is_watching()
    box.poll()

    (proc / "stat").write_text(cpu_stat(total_busy=1000, total_idle=0))   # 100%
    sample_now(box, clock)
    (proc / "stat").write_text(cpu_stat(total_busy=1000, total_idle=1000))  # 0%
    snapshot = sample_now(box, clock)

    assert snapshot["cpu"]["percent"] == pytest.approx(0.0, abs=0.1)
    assert snapshot["cpu"]["peak_percent"] == pytest.approx(100.0, abs=0.1)


def test_the_peak_resets_when_the_television_restarts(tmp_path):
    clock = FakeClock()
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=0, total_idle=0))
    uptime = {"seconds": 500.0}
    box = metrics.Collector(
        proc_root=proc, clock=clock, tv_uptime_source=lambda: uptime["seconds"]
    )
    box.someone_is_watching()
    box.poll()
    (proc / "stat").write_text(cpu_stat(total_busy=1000, total_idle=0))
    sample_now(box, clock)
    assert box.snapshot()["cpu"]["peak_percent"] == pytest.approx(100.0, abs=0.1)

    # The television process restarted: its uptime went backwards. A peak from
    # the previous run says nothing about this one.
    uptime["seconds"] = 3.0
    (proc / "stat").write_text(cpu_stat(total_busy=1000, total_idle=1000))
    snapshot = sample_now(box, clock)
    assert snapshot["cpu"]["peak_percent"] is None
    assert snapshot["samples"] == 1


def test_a_value_climbing_over_the_hour_is_reported_as_climbing(tmp_path):
    clock = FakeClock()
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=0, total_idle=0))
    box = metrics.Collector(proc_root=proc, clock=clock)
    box.someone_is_watching()
    box.poll()

    busy = idle = 0
    for step in range(10):
        busy += step * 100        # a little busier every time
        idle += (10 - step) * 100
        (proc / "stat").write_text(cpu_stat(total_busy=busy, total_idle=idle))
        sample_now(box, clock)

    assert box.snapshot()["cpu"]["trend"] == "climbing"


def test_a_steady_value_is_not_reported_as_a_trend(tmp_path):
    clock = FakeClock()
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=0, total_idle=0))
    box = metrics.Collector(proc_root=proc, clock=clock)
    box.someone_is_watching()
    box.poll()
    climb(box, clock, proc, steps=10, busy_per_step=500, idle_per_step=500)
    assert box.snapshot()["cpu"]["trend"] == "steady"


def test_a_falling_value_is_reported_as_falling(tmp_path):
    clock = FakeClock()
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=0, total_idle=0))
    box = metrics.Collector(proc_root=proc, clock=clock)
    box.someone_is_watching()
    box.poll()

    busy = idle = 0
    for step in range(10):
        busy += (10 - step) * 100
        idle += step * 100
        (proc / "stat").write_text(cpu_stat(total_busy=busy, total_idle=idle))
        sample_now(box, clock)

    assert box.snapshot()["cpu"]["trend"] == "falling"


def test_a_trend_is_not_claimed_from_one_or_two_readings(tmp_path):
    clock = FakeClock()
    box = collector(tmp_path, clock)
    box.someone_is_watching()
    box.poll()
    assert box.snapshot()["cpu"]["trend"] == "unknown"


def test_the_temperature_peak_and_trend_come_from_whatever_is_plugged_in(tmp_path):
    # sensors.py owns reading the thermal zones. This module only remembers
    # what it is handed, so it stays testable without that file existing.
    clock = FakeClock()
    reading = {"celsius": 50.0}
    box = collector(tmp_path, clock, temperature_source=lambda: reading["celsius"])
    box.someone_is_watching()
    box.poll()
    for celsius in (60.0, 70.0, 80.0, 75.0, 70.0, 65.0, 60.0, 55.0):
        reading["celsius"] = celsius
        sample_now(box, clock)

    temperature = box.snapshot()["temperature"]
    assert temperature["celsius"] == 55.0
    assert temperature["peak_celsius"] == 80.0
    assert temperature["trend"] in ("climbing", "steady", "falling")


def test_a_temperature_reading_is_judged_by_the_sensor_not_by_a_number_in_here(tmp_path):
    # The same reading, twice, with opposite verdicts - because 82 C is a
    # comfortable SSD and 62 C is a chipset past its own limit, and only the
    # part's manufacturer knows which. sensors.py reads those limits out of
    # temp*_max and temp*_crit; this module takes its answer and adds nothing.
    clock = FakeClock()
    cool = idling(tmp_path / "cool", clock, temperature_source=lambda: {
        "name": "SSD", "celsius": 82.0, "state": "fine",
    }).snapshot()
    assert cool["verdict"]["state"] == "coping"
    assert not any("hot" in reason.lower() for reason in cool["verdict"]["reasons"])

    cooking = idling(tmp_path / "cooking", FakeClock(), temperature_source=lambda: {
        "name": "Chipset", "celsius": 62.0, "state": "critical",
    }).snapshot()
    assert cooking["verdict"]["state"] == "struggling"
    assert any("hot" in reason.lower() for reason in cooking["verdict"]["reasons"])


def test_a_sensor_that_publishes_no_limit_gets_no_verdict_invented_for_it(tmp_path):
    # sensors.py says "unknown" when a part publishes neither max nor crit.
    # There is no truthful way to say whether 95 C is fine on hardware we have
    # never seen, so nothing is said - and the reading is still shown.
    snapshot = idling(tmp_path, FakeClock(), temperature_source=lambda: {
        "name": "temp1 on some_driver", "celsius": 95.0, "state": "unknown",
    }).snapshot()
    assert snapshot["temperature"]["celsius"] == 95.0
    assert snapshot["temperature"]["state"] == "unknown"
    assert snapshot["verdict"]["state"] == "coping"
    assert not any("hot" in reason.lower() for reason in snapshot["verdict"]["reasons"])


def test_a_bare_temperature_number_is_still_shown_but_nobody_pretends_to_judge_it(tmp_path):
    # A caller with only a number is still worth listening to - the reading
    # goes on the page - but a number with no published limit beside it is a
    # reading, not a diagnosis, so the verdict says nothing about it.
    snapshot = idling(tmp_path, FakeClock(), temperature_source=lambda: 99.0).snapshot()
    assert snapshot["temperature"]["celsius"] == 99.0
    assert snapshot["temperature"]["state"] == "unknown"
    assert snapshot["verdict"]["state"] == "coping"


def test_the_temperature_the_page_shows_carries_the_sensors_own_state_and_name(tmp_path):
    snapshot = idling(tmp_path, FakeClock(), temperature_source=lambda: {
        "name": "CPU", "celsius": 71.0, "state": "warm",
    }).snapshot()
    assert snapshot["temperature"]["name"] == "CPU"
    assert snapshot["temperature"]["state"] == "warm"
    assert snapshot["verdict"]["state"] == "working"
    assert any("CPU" in reason for reason in snapshot["verdict"]["reasons"])


def test_no_temperature_source_is_an_absence_not_a_zero(tmp_path):
    box = collector(tmp_path)
    box.someone_is_watching()
    assert box.poll()["temperature"] is None


def test_a_temperature_source_that_fails_does_not_take_the_page_down(tmp_path):
    def broken():
        raise RuntimeError("no sensor today")

    box = collector(tmp_path, temperature_source=broken)
    box.someone_is_watching()
    assert box.poll()["temperature"] is None


# ==========================================================================
# A stale pair of readings is not a measurement of now
# ==========================================================================
def test_a_reading_from_before_the_page_was_closed_is_too_old_to_measure_against(tmp_path):
    clock = FakeClock()
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=0, total_idle=0))
    box = metrics.Collector(proc_root=proc, clock=clock)
    box.someone_is_watching()
    box.poll()
    open_page = sample_now(box, clock, stat=cpu_stat(total_busy=200, total_idle=0))
    assert open_page["cpu"]["percent"] == pytest.approx(100.0, abs=0.1)

    # The page is closed. Twenty minutes later somebody opens it again. The
    # box was flat out for two seconds of those twenty minutes and idle for
    # the rest, and dividing by the whole gap answers a question nobody asked
    # - then puts the answer on the page as though it were now.
    clock.tick(20 * 60)
    (proc / "stat").write_text(cpu_stat(total_busy=400, total_idle=119800))
    box.someone_is_watching()
    reopened = box.poll()

    assert reopened["cpu"]["percent"] is None
    assert reopened["cpu"]["state"] == "measuring"
    assert reopened["verdict"]["state"] == "measuring"


def test_the_measurement_comes_straight_back_on_the_sample_after_the_gap(tmp_path):
    # Refusing the stale pair must not leave the page saying "measuring" for
    # ever: the reading taken when the page reopened is a perfectly good first
    # half of the next pair, exactly like the genuine first sample of a run.
    clock = FakeClock()
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=0, total_idle=0))
    box = metrics.Collector(proc_root=proc, clock=clock)
    box.someone_is_watching()
    box.poll()
    clock.tick(20 * 60)
    (proc / "stat").write_text(cpu_stat(total_busy=100, total_idle=119900))
    box.someone_is_watching()
    box.poll()

    after = sample_now(box, clock, stat=cpu_stat(total_busy=300, total_idle=119900))
    assert after["cpu"]["percent"] == pytest.approx(100.0, abs=0.1)
    assert after["cpu"]["state"] == "measured"


def test_a_frame_drop_count_from_before_the_page_was_closed_is_not_turned_into_a_rate(tmp_path):
    # The same door: a cumulative counter and a timestamp from twenty minutes
    # ago divide into a confident-looking drops-per-minute for a stretch
    # nobody was watching.
    clock = FakeClock()
    dropped = {"count": 0.0}
    box = idling(tmp_path, clock, frame_drop_source=lambda: dropped["count"])

    clock.tick(20 * 60)
    dropped["count"] = 4000.0
    (box.proc_root / "stat").write_text(idle_stat(IDLING_STEPS + 1))
    box.someone_is_watching()
    reopened = box.poll()

    assert reopened["frames"]["dropped_per_minute"] is None
    assert reopened["verdict"]["state"] != "struggling"


# ==========================================================================
# Not making it worse: the watching gate and the ring buffer
# ==========================================================================
def test_nothing_is_sampled_when_nobody_is_watching(tmp_path):
    clock = FakeClock()
    box = collector(tmp_path, clock)
    for _ in range(20):
        clock.tick(60)
        box.poll()
    assert box.snapshot()["samples"] == 0
    assert box.snapshot()["watching"] is False


def test_sampling_stops_as_soon_as_the_viewer_leaves_the_page(tmp_path):
    clock = FakeClock()
    box = collector(tmp_path, clock)
    box.someone_is_watching()
    box.poll()
    clock.tick(metrics.SAMPLE_INTERVAL_SECONDS)
    box.poll()
    taken = box.snapshot()["samples"]

    box.nobody_is_watching()
    for _ in range(20):
        clock.tick(metrics.SAMPLE_INTERVAL_SECONDS)
        box.poll()
    assert box.snapshot()["samples"] == taken


def test_a_watcher_that_never_says_goodbye_stops_counting_after_the_timeout(tmp_path):
    # A closed laptop lid, a phone in a pocket, a browser tab killed by the
    # phone's own memory manager. None of them get to leave the box sampling.
    clock = FakeClock()
    box = collector(tmp_path, clock)
    box.someone_is_watching()
    box.poll()
    assert box.is_watching() is True

    clock.tick(metrics.WATCHER_TIMEOUT_SECONDS + 1)
    assert box.is_watching() is False
    before = box.snapshot()["samples"]
    for _ in range(20):
        clock.tick(metrics.SAMPLE_INTERVAL_SECONDS)
        box.poll()
    assert box.snapshot()["samples"] == before


def test_no_caller_can_ask_this_box_to_keep_sampling_for_longer_than_the_cap(tmp_path):
    # A leaked watcher is a bug. A leaked watcher that samples for ever is a
    # bug that costs a customer frames, so however politely a caller asks -
    # and a browser that asks for a day is asking politely - the deadline is
    # capped at MAX_WATCH_SECONDS and the sampling stops on its own.
    clock = FakeClock()
    box = collector(tmp_path, clock)
    box.someone_is_watching(timeout=24 * 60 * 60)
    assert box.is_watching() is True

    clock.tick(metrics.MAX_WATCH_SECONDS + 1)
    assert box.is_watching() is False

    before = box.snapshot()["samples"]
    for _ in range(20):
        clock.tick(metrics.SAMPLE_INTERVAL_SECONDS)
        box.poll()
    assert box.snapshot()["samples"] == before


def test_samples_are_not_taken_more_often_than_the_interval(tmp_path):
    # The page may poll as fast as it likes; the box does not have to keep up.
    clock = FakeClock()
    box = collector(tmp_path, clock)
    for _ in range(50):
        clock.tick(0.05)
        box.someone_is_watching()
        box.poll()
    assert box.snapshot()["samples"] <= 3


def test_the_history_cannot_grow_without_bound(tmp_path):
    clock = FakeClock()
    box = collector(tmp_path, clock, window_seconds=60.0)
    limit = box.history_limit
    assert limit <= 64
    for _ in range(limit * 20):
        sample_now(box, clock)
    assert box.history_length == limit


def test_the_window_is_a_ring_buffer_not_a_list_that_grows_for_months(tmp_path):
    # A box left on for six months at one sample every two seconds is nearly
    # eight million samples. The buffer is sized by the window, not by uptime.
    box = collector(tmp_path, window_seconds=3600.0)
    assert box.history_limit == pytest.approx(3600 / metrics.SAMPLE_INTERVAL_SECONDS, abs=2)


def test_looking_at_the_last_reading_opens_no_files_at_all(tmp_path, monkeypatch):
    # Every /proc read has to happen inside a sample, where the interval gates
    # it and the cost measurement can see it. A read hiding in the formatting
    # is a cost paid on every poll, unmeasured, even when nobody is watching.
    clock = FakeClock()
    box = collector(tmp_path, clock)
    sample_now(box, clock)

    reads = []
    real = metrics._read_text
    monkeypatch.setattr(metrics, "_read_text", lambda p: reads.append(p) or real(p))
    for _ in range(10):
        box.snapshot()
    assert reads == []

    # And a poll with nobody watching is the same: it reports, it does not read.
    box.nobody_is_watching()
    clock.tick(600)
    box.poll()
    assert reads == []


def test_the_load_average_is_part_of_what_a_poll_reports(tmp_path):
    clock = FakeClock()
    box = collector(tmp_path, clock)
    snapshot = sample_now(box, clock)
    assert snapshot["load"]["one_minute"] == 1.53
    assert snapshot["load"]["summary"]
    assert snapshot["cpu"]["cores"] == 2


def test_the_cost_of_collecting_is_measured_and_reported(tmp_path):
    clock = FakeClock()
    box = collector(tmp_path, clock)
    for _ in range(5):
        sample_now(box, clock)
    cost = box.snapshot()["collection"]
    assert cost["samples"] == 5
    assert cost["average_ms"] is not None and cost["average_ms"] >= 0.0
    assert cost["percent_of_one_core"] is not None
    assert cost["note"]


# ==========================================================================
# The verdict - the sentence somebody actually reads
# ==========================================================================
def pinned(tmp_path, clock, **kwargs):
    """A collector whose box is flat out on both cores."""
    proc = make_proc(
        tmp_path,
        stat=cpu_stat(total_busy=0, total_idle=0, cores=[(0, 0), (0, 0)]),
        loadavg="4.50 4.20 4.00 5/100 5\n",
    )
    box = metrics.Collector(proc_root=proc, clock=clock, **kwargs)
    box.someone_is_watching()
    box.poll()
    for step in range(1, 6):
        (proc / "stat").write_text(
            cpu_stat(
                total_busy=step * 2000, total_idle=0,
                cores=[(step * 1000, 0), (step * 1000, 0)],
            )
        )
        sample_now(box, clock)
    return box


def idle_stat(step):
    """/proc/stat for a box that has been ticking over at 5% for `step` samples."""
    return cpu_stat(
        total_busy=step * 100, total_idle=step * 1900,
        cores=[(step * 50, step * 950), (step * 50, step * 950)],
    )


#: How many samples :func:`idling` has already taken, so a test that carries on
#: from there keeps the counters moving forward the way a real box does.
IDLING_STEPS = 5


def idling(tmp_path, clock, **kwargs):
    """A collector whose box is barely doing anything."""
    proc = make_proc(
        tmp_path,
        stat=cpu_stat(total_busy=0, total_idle=0, cores=[(0, 0), (0, 0)]),
        loadavg="0.30 0.25 0.20 1/100 5\n",
    )
    box = metrics.Collector(proc_root=proc, clock=clock, **kwargs)
    box.someone_is_watching()
    box.poll()
    for step in range(1, IDLING_STEPS + 1):
        sample_now(box, clock, stat=idle_stat(step))
    return box


def test_a_box_under_no_strain_says_plainly_that_it_is_coping(tmp_path):
    snapshot = idling(tmp_path, FakeClock()).snapshot()
    assert snapshot["verdict"]["state"] == "coping"
    assert snapshot["verdict"]["sentence"] == "This box is coping fine."


def test_a_box_that_is_flat_out_says_plainly_that_it_is_struggling(tmp_path):
    snapshot = pinned(tmp_path, FakeClock()).snapshot()
    assert snapshot["verdict"]["state"] == "struggling"
    assert "struggling" in snapshot["verdict"]["sentence"]
    assert snapshot["verdict"]["reasons"]


def test_the_verdict_leads_with_a_sentence_a_customer_can_read(tmp_path):
    for snapshot in (
        idling(tmp_path / "a", FakeClock()).snapshot(),
        pinned(tmp_path / "b", FakeClock()).snapshot(),
    ):
        sentence = snapshot["verdict"]["sentence"]
        assert sentence.startswith("This box")
        assert sentence.endswith(".")
        assert "%" not in sentence and "CPU" not in sentence


def test_frame_drops_from_the_player_outrank_a_comfortable_looking_processor(tmp_path):
    # Frame drops are what the customer can actually see. A box at 40% that is
    # dropping frames is not coping, whatever the percentage says.
    clock = FakeClock()
    dropped = {"count": 0.0}
    box = idling(tmp_path, clock, frame_drop_source=lambda: dropped["count"])
    assert box.snapshot()["verdict"]["state"] == "coping"

    for step in range(1, 4):
        dropped["count"] += 60          # a dropped frame every second
        sample_now(box, clock, stat=idle_stat(IDLING_STEPS + step))

    snapshot = box.snapshot()
    assert snapshot["frames"]["dropped_per_minute"] > 6
    assert snapshot["verdict"]["state"] == "struggling"
    assert any("frame" in reason.lower() for reason in snapshot["verdict"]["reasons"])


def test_the_verdict_is_sensible_when_the_player_reports_nothing(tmp_path):
    snapshot = idling(tmp_path, FakeClock()).snapshot()
    assert snapshot["frames"]["known"] is False
    assert snapshot["frames"]["dropped_per_minute"] is None
    assert snapshot["verdict"]["state"] == "coping"


def test_a_frame_drop_counter_that_resets_is_not_read_as_a_flood_of_drops(tmp_path):
    # mpv restarts its counter with every file. Without this, the end of every
    # episode would look like a catastrophe.
    clock = FakeClock()
    dropped = {"count": 500.0}
    box = idling(tmp_path, clock, frame_drop_source=lambda: dropped["count"])
    sample_now(box, clock, stat=idle_stat(IDLING_STEPS + 1))
    dropped["count"] = 0.0
    snapshot = sample_now(box, clock, stat=idle_stat(IDLING_STEPS + 2))
    assert snapshot["frames"]["dropped_per_minute"] in (None, 0.0)
    assert snapshot["verdict"]["state"] == "coping"


def test_a_frame_drop_source_that_fails_does_not_take_the_page_down(tmp_path):
    def broken():
        raise RuntimeError("the player went away")

    snapshot = idling(tmp_path, FakeClock(), frame_drop_source=broken).snapshot()
    assert snapshot["frames"]["known"] is False
    assert snapshot["verdict"]["state"] == "coping"


def test_swap_in_use_is_treated_as_trouble_and_said_out_loud(tmp_path):
    swapping = MEMINFO.replace("SwapFree:        2097148 kB", "SwapFree:        1000000 kB")
    clock = FakeClock()
    proc = make_proc(tmp_path, meminfo=swapping, stat=cpu_stat(total_busy=0, total_idle=0))
    box = metrics.Collector(proc_root=proc, clock=clock)
    box.someone_is_watching()
    box.poll()
    (proc / "stat").write_text(cpu_stat(total_busy=100, total_idle=1900))
    snapshot = sample_now(box, clock)

    assert snapshot["verdict"]["state"] == "struggling"
    assert any("memory" in reason.lower() for reason in snapshot["verdict"]["reasons"])


def test_a_hot_box_is_called_out_before_it_starts_throttling(tmp_path):
    # "hot" is the kernel's own word for past temp*_max - the point the part's
    # manufacturer says it is out of spec, which comes before temp*_crit where
    # it starts slowing itself down. That gap is the whole warning.
    clock = FakeClock()
    box = idling(tmp_path, clock, temperature_source=lambda: {
        "name": "CPU", "celsius": 88.0, "state": "hot",
    })
    snapshot = box.snapshot()
    assert snapshot["verdict"]["state"] == "struggling"
    assert any("hot" in reason.lower() for reason in snapshot["verdict"]["reasons"])


def test_a_verdict_with_nothing_measured_does_not_claim_the_box_is_fine(tmp_path):
    proc = make_proc(tmp_path)
    (proc / "stat").unlink()
    (proc / "meminfo").unlink()
    box = metrics.Collector(proc_root=proc, clock=FakeClock())
    box.someone_is_watching()
    verdict = box.poll()["verdict"]
    assert verdict["state"] == "unknown"
    assert "could not" in verdict["sentence"].lower()


# ==========================================================================
# No number in this file may decide what a temperature means
# ==========================================================================
#: Anything to do with heat. A statement that mentions one of these words and
#: also contains a number is a limit somebody wrote down here instead of
#: reading it off the part.
HEAT_WORDS = re.compile(
    r"temperature|celsius|degrees|thermal|\bhot\b|\bwarm\b|\bheat\b", re.IGNORECASE)

#: A number stated as a temperature in prose or a comment: "80 C", "90°C".
CELSIUS_IN_WORDS = re.compile(r"\d+(?:\.\d+)?\s*°?\s*C\b")

#: A constant named after heat and set to a number - the exact thing that came
#: back last time, under whichever new name it comes back under next time.
HEAT_CONSTANT = re.compile(
    r"^\s*_?[A-Za-z_]*(?:TEMPERATURE|CELSIUS|DEGREES|THERMAL|HOT|WARM|HEAT)"
    r"[A-Za-z_]*\s*(?::[^=]+)?=\s*-?\d", re.IGNORECASE | re.MULTILINE)


def metrics_source():
    return Path(metrics.__file__).read_text(encoding="utf-8")


def test_no_temperature_limit_is_written_down_anywhere_in_this_module():
    """An SSD at 80 C is fine, a chipset at 80 C is not, and this file has no
    way of knowing which one it is looking at. The limits are published by the
    part's own manufacturer in temp*_max and temp*_crit and read by sensors.py,
    so a number standing in for one of them here is wrong on the next chipset -
    including one that only lives in a comment, because that is where the next
    one will be copied from."""
    source = metrics_source()
    assert not HEAT_CONSTANT.findall(source), (
        "a temperature limit has been written into metrics.py: "
        + repr(HEAT_CONSTANT.findall(source)))
    assert not CELSIUS_IN_WORDS.findall(source), (
        "a temperature in degrees is stated in metrics.py: "
        + repr(CELSIUS_IN_WORDS.findall(source)))


def test_no_number_anywhere_in_this_module_takes_part_in_a_decision_about_heat():
    """The regexes above catch the constant coming back. This catches it being
    smuggled in unnamed - a bare 80.0 inside the verdict, a comparison against
    a literal - by finding every number in the file and asking what the code
    around it is talking about."""
    source = metrics_source()
    tree = ast.parse(source)
    lines = source.splitlines()

    # How many decimal places a reading is shown to is not a limit, so the
    # second argument of round() is the one number allowed near a temperature.
    rounding = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "round" and len(node.args) > 1):
            rounding.add(id(node.args[1]))

    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            continue
        if id(node) in rounding:
            continue
        statement = node
        while id(statement) in parents and not isinstance(statement, ast.stmt):
            statement = parents[id(statement)]
        if not isinstance(statement, ast.stmt):
            continue
        text = "\n".join(lines[statement.lineno - 1:statement.end_lineno])
        if HEAT_WORDS.search(text):
            offenders.append((node.lineno, node.value, text.strip().splitlines()[0]))

    assert not offenders, "numbers deciding a temperature question: {}".format(offenders)


# ==========================================================================
# What an hour of history actually costs, measured rather than claimed
# ==========================================================================
def held_bytes(obj, seen=None):
    """What this object really holds, counting each distinct object once.

    ``sys.getsizeof`` on a deque reports the deque and not one byte of what is
    in it, and every sample in the ring is a tuple of separately allocated
    floats. Anything that does not walk into them under-reports the ring by
    more than half, which is how the published figure came to be wrong.
    """
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    total = sys.getsizeof(obj)
    if isinstance(obj, (tuple, list, set, frozenset, deque)):
        for item in obj:
            total += held_bytes(item, seen)
    return total


def test_an_hour_of_history_costs_what_the_manual_says_it_costs(tmp_path):
    # The README tells the owner what this page costs their box. That claim is
    # measured here, on a full ring with a real temperature reading in every
    # sample, so the day somebody adds a fifth field to a sample this fails
    # rather than quietly doubling the number in the manual.
    clock = FakeClock()
    proc = make_proc(tmp_path, stat=cpu_stat(total_busy=0, total_idle=0))
    heat = {"celsius": 40.0}
    box = metrics.Collector(
        proc_root=proc, clock=clock,
        temperature_source=lambda: {"name": "CPU", "celsius": heat["celsius"],
                                    "state": "fine"},
    )
    busy = idle = 0
    for step in range(box.history_limit + 10):
        busy += 100 + step % 37           # no two samples share a float
        idle += 400 + step % 11
        heat["celsius"] = 40.0 + (step % 400) * 0.1
        sample_now(box, clock, stat=cpu_stat(total_busy=busy, total_idle=idle))

    assert box.history_length == box.history_limit
    measured = held_bytes(box._history)
    # Measured at 318 KB on CPython 3.9 (64-bit) with the shipped defaults.
    # The bound is the promise: an hour of history stays under half a megabyte
    # on a box that has a thermal sensor.
    assert measured < 512 * 1024, "an hour of history now holds {:,} bytes".format(measured)


# ==========================================================================
# The seam webui.py uses
# ==========================================================================
def test_the_shared_collector_is_one_collector(tmp_path):
    metrics.reset_collector()
    assert metrics.collector() is metrics.collector()
    metrics.reset_collector()


def test_the_module_level_helpers_drive_the_shared_collector(tmp_path):
    metrics.reset_collector()
    try:
        metrics.configure(proc_root=make_proc(tmp_path), clock=FakeClock())
        metrics.someone_is_watching()
        snapshot = metrics.snapshot()
        assert snapshot["watching"] is True
        assert "verdict" in snapshot
        metrics.nobody_is_watching()
        assert metrics.snapshot()["watching"] is False
    finally:
        metrics.reset_collector()


def test_nothing_in_here_raises_whatever_the_box_does(tmp_path):
    # The System page is read when something is already wrong. It does not get
    # to be the thing that is wrong.
    empty = tmp_path / "nothing"
    empty.mkdir()
    box = metrics.Collector(
        proc_root=empty,
        clock=FakeClock(),
        temperature_source=lambda: 1 / 0,
        frame_drop_source=lambda: 1 / 0,
        tv_uptime_source=lambda: 1 / 0,
    )
    box.someone_is_watching()
    for _ in range(3):
        box.poll()
    assert box.snapshot()["verdict"]["sentence"]
