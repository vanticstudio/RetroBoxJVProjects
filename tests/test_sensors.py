""""Is my box coping?" - answered from the sensors the box actually has.

Every test here builds a synthetic /sys tree in tmp_path, because the machine
this suite runs on may have no sensors at all, and because the box we ship to
is a secondhand mini PC whose chipset is unknown until it is in somebody's
hands. Nothing may be hardcoded to one machine's layout, so nothing here
pretends to know one: each test says what the kernel exposed and what the
owner should be told about it.
"""

import os

import pytest

from retrobox import sensors


# ==========================================================================
# Fixture builders - a fake /sys/class/hwmon and /sys/class/thermal
# ==========================================================================
def hwmon(root, index, driver):
    """One hwmon node, as the kernel lays it out: a name file and readings."""
    node = root / f"hwmon{index}"
    node.mkdir(parents=True, exist_ok=True)
    (node / "name").write_text(driver + "\n")
    return node


def temp(node, index, millidegrees, label=None, max_=None, crit=None, hyst=None):
    """A temp*_input with whatever thresholds this particular chip publishes."""
    if millidegrees is not None:
        (node / f"temp{index}_input").write_text(f"{millidegrees}\n")
    if label is not None:
        (node / f"temp{index}_label").write_text(label + "\n")
    if max_ is not None:
        (node / f"temp{index}_max").write_text(f"{max_}\n")
    if crit is not None:
        (node / f"temp{index}_crit").write_text(f"{crit}\n")
    if hyst is not None:
        (node / f"temp{index}_max_hyst").write_text(f"{hyst}\n")
    return node


def fan(node, index, rpm, label=None, fault=None):
    if rpm is not None:
        (node / f"fan{index}_input").write_text(f"{rpm}\n")
    if label is not None:
        (node / f"fan{index}_label").write_text(label + "\n")
    if fault is not None:
        (node / f"fan{index}_fault").write_text(f"{fault}\n")
    return node


def thermal_zone(root, index, kind, millidegrees, trips=()):
    """One thermal_zone with its trip points, which are the kernel's limits."""
    zone = root / f"thermal_zone{index}"
    zone.mkdir(parents=True, exist_ok=True)
    (zone / "type").write_text(kind + "\n")
    if millidegrees is not None:
        (zone / "temp").write_text(f"{millidegrees}\n")
    for n, (trip_type, trip_temp) in enumerate(trips):
        (zone / f"trip_point_{n}_type").write_text(trip_type + "\n")
        (zone / f"trip_point_{n}_temp").write_text(f"{trip_temp}\n")
    return zone


def named(readings, name):
    """The one reading with this plain name, so tests read like sentences."""
    matches = [r for r in readings if r["name"] == name]
    assert matches, f"no reading called {name!r} in {[r['name'] for r in readings]}"
    return matches[0]


#: Root can read a file with mode 000, so the "unreadable" cases cannot be
#: staged as root. Skipping beats a test that silently proves nothing.
unprivileged_only = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can read a file whose permissions forbid it",
)


def empty(tmp_path, *names):
    """Paths that exist as directories but contain nothing."""
    made = []
    for name in names:
        path = tmp_path / name
        path.mkdir(parents=True, exist_ok=True)
        made.append(path)
    return made if len(made) > 1 else made[0]


# ==========================================================================
# Enumeration - whatever this box happens to have, not what we guessed
# ==========================================================================
def test_every_temperature_sensor_on_every_hwmon_node_is_found(tmp_path):
    # Different chipsets put different sensors under different drivers, and
    # anything hardcoded to one machine is wrong on the next one.
    hw = empty(tmp_path, "hwmon")
    cpu = hwmon(hw, 0, "coretemp")
    temp(cpu, 1, 48000, label="Package id 0", max_=100000, crit=100000)
    temp(cpu, 2, 46000, label="Core 0", max_=100000, crit=100000)
    temp(cpu, 3, 47000, label="Core 1", max_=100000, crit=100000)
    pch = hwmon(hw, 1, "pch_cannonlake")
    temp(pch, 1, 61000)
    ssd = hwmon(hw, 2, "nvme")
    temp(ssd, 1, 40850, label="Composite", crit=84850)

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "none")
    assert len(readings) == 5
    assert {r["driver"] for r in readings} == {"coretemp", "pch_cannonlake", "nvme"}


def test_a_sensor_with_a_label_and_one_without_are_both_reported(tmp_path):
    # A chip that publishes no label is not a broken chip, and dropping it
    # loses a reading the owner may need.
    hw = empty(tmp_path, "hwmon")
    node = hwmon(hw, 0, "coretemp")
    temp(node, 1, 55000, label="Package id 0", max_=100000)
    temp(node, 2, 39000)  # no label at all

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "none")
    assert len(readings) == 2
    labelled = [r for r in readings if r["label"] == "Package id 0"][0]
    unlabelled = [r for r in readings if r["label"] is None][0]
    assert labelled["celsius"] == pytest.approx(55.0)
    assert unlabelled["celsius"] == pytest.approx(39.0)


def test_thermal_zones_are_read_as_well_as_hwmon_nodes(tmp_path):
    # Some platforms expose the ACPI zone only through /sys/class/thermal.
    hw, th = empty(tmp_path, "hwmon", "thermal")
    thermal_zone(th, 0, "acpitz", 42000, trips=[("critical", 105000)])

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=th)
    assert len(readings) == 1
    assert readings[0]["celsius"] == pytest.approx(42.0)
    assert readings[0]["name"] == "System"


def test_a_box_with_no_hwmon_directory_reports_nothing_rather_than_failing(tmp_path):
    readings = sensors.temperatures(
        hwmon_root=tmp_path / "no-hwmon-here", thermal_root=tmp_path / "no-thermal"
    )
    assert readings == []


def test_a_box_with_no_thermal_directory_still_reports_its_hwmon_sensors(tmp_path):
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "coretemp"), 1, 50000, label="Package id 0", max_=100000)

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "gone")
    assert [r["name"] for r in readings] == ["CPU"]


@unprivileged_only
def test_a_sensor_that_cannot_be_read_is_skipped_rather_than_shown_as_zero(tmp_path):
    # Absent and zero are different states everywhere in this product.
    hw = empty(tmp_path, "hwmon")
    node = hwmon(hw, 0, "coretemp")
    temp(node, 1, 50000, label="Package id 0", max_=100000)
    unreadable = node / "temp2_input"
    unreadable.write_text("46000\n")
    unreadable.chmod(0o000)
    try:
        readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "none")
    finally:
        unreadable.chmod(0o644)

    assert [r["celsius"] for r in readings] == [pytest.approx(50.0)]


def test_a_sensor_file_full_of_rubbish_is_ignored_not_guessed_at(tmp_path):
    hw = empty(tmp_path, "hwmon")
    node = hwmon(hw, 0, "coretemp")
    (node / "temp1_input").write_text("banana\n")
    temp(node, 2, 51000, label="Core 0", max_=100000)

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "none")
    assert [r["celsius"] for r in readings] == [pytest.approx(51.0)]


def test_an_impossible_reading_is_not_shown_as_a_temperature(tmp_path):
    # Sensors that are not wired up report values no room has ever been at.
    hw = empty(tmp_path, "hwmon")
    node = hwmon(hw, 0, "it87")
    temp(node, 1, -128000, label="AUXTIN")
    temp(node, 2, 36000, label="SYSTIN")

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "none")
    assert [r["celsius"] for r in readings] == [pytest.approx(36.0)]


def test_the_same_sensor_seen_twice_is_only_shown_once(tmp_path):
    # The CPU package usually appears under coretemp and again as the
    # x86_pkg_temp thermal zone. Two identical lines reads as two problems.
    hw, th = empty(tmp_path, "hwmon", "thermal")
    temp(hwmon(hw, 0, "coretemp"), 1, 57000, label="Package id 0", max_=100000)
    thermal_zone(th, 0, "x86_pkg_temp", 57000, trips=[("critical", 100000)])

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=th)
    assert [r["name"] for r in readings] == ["CPU"]


def test_the_older_sysfs_layout_is_read_too(tmp_path):
    # Kernels of the era these boxes shipped in put the readings one level
    # down, in hwmonN/device/. Secondhand hardware turns up with both.
    hw = empty(tmp_path, "hwmon")
    node = hw / "hwmon0"
    node.mkdir()
    (node / "name").write_text("coretemp\n")
    legacy = node / "device"
    legacy.mkdir()
    temp(legacy, 1, 53000, label="Package id 0", max_=100000)

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")
    assert [r["name"] for r in readings] == ["CPU"]
    assert readings[0]["celsius"] == pytest.approx(53.0)


# ==========================================================================
# Plain names - "temp1_input on pch_cannonlake" is not an answer
# ==========================================================================
def test_sensors_are_given_names_a_person_recognises(tmp_path):
    hw = empty(tmp_path, "hwmon")
    cpu = hwmon(hw, 0, "coretemp")
    temp(cpu, 1, 48000, label="Package id 0", max_=100000)
    temp(cpu, 2, 46000, label="Core 0", max_=100000)
    pch = hwmon(hw, 1, "pch_cannonlake")
    temp(pch, 1, 61000)
    ssd = hwmon(hw, 2, "nvme")
    temp(ssd, 1, 40000, label="Composite", crit=84000)
    acpi = hwmon(hw, 3, "acpitz")
    temp(acpi, 1, 44000)

    names = [r["name"] for r in sensors.temperatures(hwmon_root=hw,
                                                     thermal_root=tmp_path / "n")]
    assert "CPU" in names
    assert "CPU core 1" in names, "kernel counts cores from nought, people from one"
    assert "Chipset" in names
    assert "SSD" in names
    assert "System" in names


def test_a_sensor_we_cannot_identify_shows_its_raw_name_rather_than_hiding(tmp_path):
    # Hiding it loses a real reading; renaming it invents a meaning it may
    # not have. So it keeps the name the kernel gave it.
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "mystery_chip"), 3, 52000)

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")[0]
    assert reading["identified"] is False
    assert "mystery_chip" in reading["name"]
    assert "temp3" in reading["name"]


def test_an_unidentified_sensor_keeps_its_label_when_it_has_one(tmp_path):
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "mystery_chip"), 1, 52000, label="TSKN")

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")[0]
    assert reading["identified"] is False
    assert "TSKN" in reading["name"] and "mystery_chip" in reading["name"]


# ==========================================================================
# State comes from the manufacturer's numbers, never from ours
# ==========================================================================
def test_the_same_temperature_is_fine_on_one_part_and_hot_on_another(tmp_path):
    # Seventy degrees is a happy NVMe and an unhappy chipset, which is exactly
    # why the limits have to come out of the kernel rather than out of us.
    hw = empty(tmp_path, "hwmon")
    ssd = hwmon(hw, 0, "nvme")
    temp(ssd, 1, 70000, label="Composite", crit=84850, max_=81850)
    pch = hwmon(hw, 1, "pch_cannonlake")
    temp(pch, 1, 70000, max_=68000, crit=78000)

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")
    assert named(readings, "SSD")["state"] == "fine"
    assert named(readings, "Chipset")["state"] == "hot"


def test_a_reading_past_the_kernels_critical_point_is_critical(tmp_path):
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "coretemp"), 1, 101000, label="Package id 0",
         max_=100000, crit=100000)

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")[0]
    assert reading["state"] == "critical"


def test_a_reading_closing_on_the_limit_is_warm_before_it_is_hot(tmp_path):
    # The gap the manufacturer leaves between "max" and "critical" is its own
    # idea of headroom, so it is what decides when to say "getting warm".
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "coretemp"), 1, 88000, label="Package id 0",
         max_=90000, crit=100000)

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")[0]
    assert reading["state"] == "warm"


def test_a_sensor_with_no_limits_at_all_is_unknown_rather_than_invented(tmp_path):
    # Without the manufacturer's numbers there is no honest way to say whether
    # 61 degrees is fine, so the page must not pretend there is.
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "pch_cannonlake"), 1, 61000)

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")[0]
    assert reading["state"] == "unknown"
    assert reading["critical_celsius"] is None and reading["max_celsius"] is None


def test_a_sensor_with_only_a_critical_point_still_gets_a_state(tmp_path):
    hw = empty(tmp_path, "hwmon")
    ssd = hwmon(hw, 0, "nvme")
    temp(ssd, 1, 40000, label="Composite", crit=84850)
    ssd2 = hwmon(hw, 1, "nvme")
    temp(ssd2, 1, 90000, label="Composite", crit=84850)

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")
    assert readings[0]["state"] == "fine"
    assert readings[1]["state"] == "critical"


def test_thermal_zone_trip_points_are_used_as_that_zones_limits(tmp_path):
    hw, th = empty(tmp_path, "hwmon", "thermal")
    thermal_zone(th, 0, "acpitz", 99000,
                 trips=[("passive", 90000), ("critical", 98000)])

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=th)[0]
    assert reading["critical_celsius"] == pytest.approx(98.0)
    assert reading["state"] == "critical"


def test_the_hottest_sensor_is_the_one_the_page_leads_with(tmp_path):
    hw = empty(tmp_path, "hwmon")
    cpu = hwmon(hw, 0, "coretemp")
    temp(cpu, 1, 48000, label="Package id 0", max_=100000)
    pch = hwmon(hw, 1, "pch_cannonlake")
    temp(pch, 1, 71000, max_=68000, crit=78000)

    assert sensors.hottest(hwmon_root=hw, thermal_root=tmp_path / "n")["name"] == "Chipset"


def test_a_box_with_no_sensors_has_no_hottest_reading(tmp_path):
    assert sensors.hottest(hwmon_root=tmp_path / "a", thermal_root=tmp_path / "b") is None


# ==========================================================================
# Fans - and the difference between "there isn't one" and "it has stopped"
# ==========================================================================
def test_a_fan_that_is_spinning_is_reported_as_working(tmp_path):
    hw = empty(tmp_path, "hwmon")
    fan(hwmon(hw, 0, "dell_smm"), 1, 2400, label="Processor Fan")

    found = sensors.fans(hwmon_root=hw)
    assert len(found) == 1
    assert found[0]["rpm"] == 2400
    assert found[0]["stopped"] is False


def test_a_fan_reading_zero_is_called_out_as_a_fan_that_has_stopped(tmp_path):
    # A seized fan is mundane on ten-year-old office hardware and presents as
    # a stuttering picture. Nobody watching TV can work that out unaided.
    hw = empty(tmp_path, "hwmon")
    fan(hwmon(hw, 0, "dell_smm"), 1, 0, label="Processor Fan")

    found = sensors.fans(hwmon_root=hw)
    assert found[0]["stopped"] is True
    assert "not spinning" in found[0]["summary"]


def test_a_fanless_box_is_not_accused_of_having_a_broken_fan(tmp_path):
    # Plenty of these boxes are passively cooled. A scary false alarm on a
    # healthy box is worse than saying nothing.
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "coretemp"), 1, 52000, label="Package id 0", max_=100000)

    assert sensors.fans(hwmon_root=hw) == []
    combined = sensors.report(hwmon_root=hw, thermal_root=tmp_path / "n",
                              cpu_root=tmp_path / "n", block_root=tmp_path / "n")
    assert combined["fan_warning"] is None
    assert "fan" not in combined["summary"].lower()


def test_a_box_with_no_hwmon_at_all_reports_no_fans(tmp_path):
    assert sensors.fans(hwmon_root=tmp_path / "nothing") == []


def test_a_fan_the_driver_flags_as_faulty_is_reported_as_stopped(tmp_path):
    hw = empty(tmp_path, "hwmon")
    fan(hwmon(hw, 0, "nct6775"), 2, 0, fault=1)

    found = sensors.fans(hwmon_root=hw)
    assert found[0]["stopped"] is True
    assert found[0]["fault"] is True


def test_a_fan_reading_rubbish_is_not_reported_as_a_stopped_fan(tmp_path):
    hw = empty(tmp_path, "hwmon")
    node = hwmon(hw, 0, "nct6775")
    (node / "fan1_input").write_text("nonsense\n")

    assert sensors.fans(hwmon_root=hw) == []


def test_a_stopped_fan_on_a_hot_box_is_named_as_the_reason_it_is_hot(tmp_path):
    hw = empty(tmp_path, "hwmon")
    cpu = hwmon(hw, 0, "coretemp")
    temp(cpu, 1, 99000, label="Package id 0", max_=90000, crit=100000)
    fan(cpu, 1, 0, label="Processor Fan")

    combined = sensors.report(hwmon_root=hw, thermal_root=tmp_path / "n",
                              cpu_root=tmp_path / "n", block_root=tmp_path / "n")
    assert combined["fan_warning"] is not None
    assert "not spinning" in combined["summary"]
    assert "hot" in combined["summary"] or "running hot" in combined["summary"]
    assert combined["warning"] is True


def test_a_stopped_fan_on_a_cool_box_is_not_blamed_for_heat_that_is_not_there(tmp_path):
    hw = empty(tmp_path, "hwmon")
    cpu = hwmon(hw, 0, "coretemp")
    temp(cpu, 1, 41000, label="Package id 0", max_=90000, crit=100000)
    fan(cpu, 1, 0, label="Processor Fan")

    combined = sensors.report(hwmon_root=hw, thermal_root=tmp_path / "n",
                              cpu_root=tmp_path / "n", block_root=tmp_path / "n")
    assert "not spinning" in combined["summary"]
    assert "running hot" not in combined["summary"]


# ==========================================================================
# Throttling - the thing that makes video stutter and looks like bad software
# ==========================================================================
def throttle_dir(root, cpu, core=0, package=0):
    node = root / f"cpu{cpu}" / "thermal_throttle"
    node.mkdir(parents=True, exist_ok=True)
    (node / "core_throttle_count").write_text(f"{core}\n")
    (node / "package_throttle_count").write_text(f"{package}\n")
    return node


def test_a_cpu_that_has_never_throttled_says_so_plainly(tmp_path):
    cpus = empty(tmp_path, "cpu")
    throttle_dir(cpus, 0)
    throttle_dir(cpus, 1)

    state = sensors.throttling(cpu_root=cpus)
    assert state["throttled"] is False
    assert state["events"] == 0


def test_a_cpu_that_has_been_throttling_is_reported_prominently(tmp_path):
    # Throttling is a direct cause of stuttering video and is otherwise
    # completely undiagnosable by the person watching.
    cpus = empty(tmp_path, "cpu")
    throttle_dir(cpus, 0, core=812, package=95)
    throttle_dir(cpus, 1, core=804, package=95)

    state = sensors.throttling(cpu_root=cpus)
    assert state["throttled"] is True
    assert state["core_events"] == 1616
    assert "slow" in state["summary"].lower()

    combined = sensors.report(hwmon_root=tmp_path / "n", thermal_root=tmp_path / "n",
                              cpu_root=cpus, block_root=tmp_path / "n")
    assert combined["warning"] is True
    assert "slow" in combined["summary"].lower()


def test_a_platform_that_does_not_report_throttling_says_unknown_not_no(tmp_path):
    assert sensors.throttling(cpu_root=tmp_path / "no-cpus-here") is None


def test_an_unreadable_throttle_counter_does_not_invent_a_count(tmp_path):
    cpus = empty(tmp_path, "cpu")
    node = cpus / "cpu0" / "thermal_throttle"
    node.mkdir(parents=True)
    (node / "core_throttle_count").write_text("rubbish\n")

    assert sensors.throttling(cpu_root=cpus) is None


# ==========================================================================
# The drive, which on secondhand hardware is the oldest part in the box
# ==========================================================================
def block_device(root, name, model="Some SSD", rotational=0, sectors=500118192):
    node = root / name
    (node / "device").mkdir(parents=True, exist_ok=True)
    (node / "queue").mkdir(parents=True, exist_ok=True)
    (node / "device" / "model").write_text(model + "\n")
    (node / "queue" / "rotational").write_text(f"{rotational}\n")
    (node / "size").write_text(f"{sectors}\n")
    return node


HEALTHY_NVME = """\
smartctl 7.2 2020-12-30 r5155 [x86_64-linux] (local build)

=== START OF SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED

SMART/Health Information (NVMe Log 0x02)
Critical Warning:                   0x00
Temperature:                        41 Celsius
Available Spare:                    100%
Available Spare Threshold:          10%
Percentage Used:                    3%
Power On Hours:                     4,102
Unsafe Shutdowns:                   17
Media and Data Integrity Errors:    0
"""

WORN_OUT_NVME = """\
=== START OF SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED

SMART/Health Information (NVMe Log 0x02)
Available Spare:                    4%
Available Spare Threshold:          10%
Percentage Used:                    103%
Power On Hours:                     41,231
Media and Data Integrity Errors:    118
"""

FAILING_SATA = """\
=== START OF READ SMART DATA SECTION ===
SMART overall-health self-assessment test result: FAILED!

ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  5 Reallocated_Sector_Ct   0x0033   012   012   036    Pre-fail  Always   FAILING_NOW 2048
  9 Power_On_Hours          0x0032   021   021   000    Old_age   Always       -       68231
177 Wear_Leveling_Count     0x0013   006   006   000    Old_age   Always       -       2841
"""


def test_the_drive_is_described_from_sysfs_without_any_extra_tools(tmp_path):
    blocks = empty(tmp_path, "block")
    block_device(blocks, "nvme0n1", model="KINGSTON SA400S37240G")

    found = sensors.disks(block_root=blocks, smart_runner=lambda dev: None)
    assert len(found) == 1
    assert found[0]["device"] == "nvme0n1"
    assert found[0]["model"] == "KINGSTON SA400S37240G"
    assert found[0]["rotational"] is False


def test_partitions_and_virtual_devices_are_not_listed_as_drives(tmp_path):
    blocks = empty(tmp_path, "block")
    block_device(blocks, "sda")
    (blocks / "sda" / "sda1").mkdir()
    (blocks / "loop0").mkdir()
    (blocks / "zram0").mkdir()

    found = sensors.disks(block_root=blocks, smart_runner=lambda dev: None)
    assert [d["device"] for d in found] == ["sda"]


def test_no_smartmontools_means_no_smart_block_and_no_complaint_about_it(tmp_path):
    # If the tool is not installed that is not a fault and not the owner's
    # problem: say nothing at all rather than shouting about a missing tool.
    blocks = empty(tmp_path, "block")
    block_device(blocks, "sda")

    found = sensors.disks(block_root=blocks, smart_runner=lambda dev: None)
    assert "power_on_hours" not in found[0]
    assert found[0]["healthy"] is None
    assert found[0]["concerns"] == []
    assert "smart" not in found[0]["summary"].lower()


def test_smartmontools_absent_from_the_path_is_not_even_attempted(tmp_path, monkeypatch):
    blocks = empty(tmp_path, "block")
    block_device(blocks, "sda")
    monkeypatch.setattr(sensors.shutil, "which", lambda name: None)

    def explode(*a, **k):
        raise AssertionError("smartctl must not be run when it is not installed")

    monkeypatch.setattr(sensors.subprocess, "run", explode)
    found = sensors.disks(block_root=blocks)
    assert found[0]["healthy"] is None


def test_a_healthy_drive_is_reported_as_healthy_in_plain_words(tmp_path):
    blocks = empty(tmp_path, "block")
    block_device(blocks, "nvme0n1")

    found = sensors.disks(block_root=blocks, smart_runner=lambda dev: HEALTHY_NVME)
    disk = found[0]
    assert disk["healthy"] is True
    assert disk["power_on_hours"] == 4102
    assert disk["life_remaining_percent"] == 97
    assert disk["concerns"] == []


def test_a_worn_out_drive_says_how_much_life_it_has_left(tmp_path):
    # "This drive reports 4% life remaining" is worth knowing before a
    # library disappears.
    blocks = empty(tmp_path, "block")
    block_device(blocks, "nvme0n1")

    disk = sensors.disks(block_root=blocks,
                         smart_runner=lambda dev: WORN_OUT_NVME)[0]
    assert disk["healthy"] is False
    assert disk["life_remaining_percent"] == 0
    assert any("life" in c.lower() for c in disk["concerns"])
    assert disk["power_on_hours"] == 41231


def test_a_worn_out_drive_is_described_in_a_sentence_an_owner_can_act_on(tmp_path):
    blocks = empty(tmp_path, "block")
    block_device(blocks, "nvme0n1")

    disk = sensors.disks(block_root=blocks,
                         smart_runner=lambda dev: WORN_OUT_NVME)[0]
    assert "not healthy" in disk["summary"]
    assert "life remaining" in disk["summary"]
    assert "41,231 hours" in disk["summary"]


def test_a_failing_drive_with_reallocated_sectors_is_called_out(tmp_path):
    blocks = empty(tmp_path, "block")
    block_device(blocks, "sda", rotational=1)

    disk = sensors.disks(block_root=blocks,
                         smart_runner=lambda dev: FAILING_SATA)[0]
    assert disk["healthy"] is False
    assert disk["reallocated_sectors"] == 2048
    assert disk["power_on_hours"] == 68231
    assert any("bad" in c.lower() or "fail" in c.lower() for c in disk["concerns"])


def test_smartctl_output_we_cannot_parse_is_not_treated_as_a_failing_drive(tmp_path):
    blocks = empty(tmp_path, "block")
    block_device(blocks, "sda")

    disk = sensors.disks(block_root=blocks,
                         smart_runner=lambda dev: "Permission denied\n")[0]
    assert disk["healthy"] is None
    assert disk["concerns"] == []


def test_a_box_with_no_block_directory_reports_no_drives(tmp_path):
    assert sensors.disks(block_root=tmp_path / "nothing") == []


# ==========================================================================
# The whole answer to "is my box coping?"
# ==========================================================================
def test_a_cool_healthy_box_is_told_it_is_fine(tmp_path):
    hw = empty(tmp_path, "hwmon")
    cpu = hwmon(hw, 0, "coretemp")
    temp(cpu, 1, 44000, label="Package id 0", max_=90000, crit=100000)
    fan(cpu, 1, 1800, label="Processor Fan")

    combined = sensors.report(hwmon_root=hw, thermal_root=tmp_path / "n",
                              cpu_root=tmp_path / "n", block_root=tmp_path / "n")
    assert combined["warning"] is False
    assert combined["state"] == "fine"
    assert combined["hottest"]["name"] == "CPU"


def test_a_box_that_reports_nothing_at_all_says_so_instead_of_saying_fine(tmp_path):
    combined = sensors.report(hwmon_root=tmp_path / "a", thermal_root=tmp_path / "b",
                              cpu_root=tmp_path / "c", block_root=tmp_path / "d")
    assert combined["state"] == "unknown"
    assert combined["warning"] is False, "unknown is not the same as bad"
    assert combined["temperatures"] == [] and combined["fans"] == []


def test_a_hot_box_leads_with_the_part_that_is_hot(tmp_path):
    hw = empty(tmp_path, "hwmon")
    cpu = hwmon(hw, 0, "coretemp")
    temp(cpu, 1, 97000, label="Package id 0", max_=90000, crit=100000)
    fan(cpu, 1, 4100, label="Processor Fan")

    combined = sensors.report(hwmon_root=hw, thermal_root=tmp_path / "n",
                              cpu_root=tmp_path / "n", block_root=tmp_path / "n")
    assert combined["state"] == "hot"
    assert combined["warning"] is True
    assert "CPU" in combined["summary"]


def test_the_report_never_raises_however_broken_the_tree_is(tmp_path):
    # This runs on a box that is switched off at the wall by someone who
    # cannot see a stack trace. It has to come back with something.
    hw = empty(tmp_path, "hwmon")
    node = hwmon(hw, 0, "coretemp")
    (node / "temp1_input").write_text("banana")
    (node / "fan1_input").mkdir()          # a directory where a file should be
    (node / "name").unlink()

    combined = sensors.report(hwmon_root=hw, thermal_root=hw,
                              cpu_root=hw, block_root=hw)
    for key in ("temperatures", "fans", "throttling", "disks", "hottest",
                "state", "summary", "warning", "fan_warning"):
        assert key in combined, key


def test_the_report_is_json_safe_because_the_dashboard_serialises_it(tmp_path):
    import json

    hw = empty(tmp_path, "hwmon")
    cpu = hwmon(hw, 0, "coretemp")
    temp(cpu, 1, 44000, label="Package id 0", max_=90000, crit=100000)
    fan(cpu, 1, 1800)

    combined = sensors.report(hwmon_root=hw, thermal_root=tmp_path / "n",
                              cpu_root=tmp_path / "n", block_root=tmp_path / "n")
    json.dumps(combined)  # would raise on a Path or a set


def test_nothing_in_this_module_shells_out_for_temperatures(tmp_path, monkeypatch):
    # Video comes first. Sensors are read from files, every time, so that a
    # page refresh can never fork a process behind the player.
    def explode(*a, **k):
        raise AssertionError("temperatures must be read from files, not commands")

    monkeypatch.setattr(sensors.subprocess, "run", explode)
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "coretemp"), 1, 44000, label="Package id 0", max_=90000)

    assert sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")
    assert sensors.fans(hwmon_root=hw) == []
    assert sensors.throttling(cpu_root=tmp_path / "n") is None


def test_the_real_paths_are_the_defaults_so_nothing_has_to_be_wired_up(tmp_path):
    # Every reader takes its root as a parameter for testing, but the box
    # itself must get the real ones without being told.
    assert str(sensors.HWMON_ROOT) == "/sys/class/hwmon"
    assert str(sensors.THERMAL_ROOT) == "/sys/class/thermal"
    assert str(sensors.CPU_ROOT) == "/sys/devices/system/cpu"
    assert str(sensors.BLOCK_ROOT) == "/sys/block"


@unprivileged_only
@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_a_whole_hwmon_node_that_cannot_be_entered_does_not_stop_the_others(tmp_path):
    hw = empty(tmp_path, "hwmon")
    good = hwmon(hw, 0, "coretemp")
    temp(good, 1, 50000, label="Package id 0", max_=100000)
    locked = hwmon(hw, 1, "nvme")
    temp(locked, 1, 40000, label="Composite", crit=84000)
    locked.chmod(0o000)
    try:
        readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")
    finally:
        locked.chmod(0o755)

    assert [r["name"] for r in readings] == ["CPU"]


# ==========================================================================
# The number printed next to the manufacturer's limit has to be right
# ==========================================================================
def test_a_temperature_ending_in_a_half_degree_rounds_up_and_not_down(tmp_path):
    # 81850 millidegrees is 81.85 degrees, which is 81.9 to one place. Binary
    # floating point makes the obvious rounding give 81.8, and this number is
    # printed to a customer beside the limit it is being compared against.
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "nvme"), 1, 81850, label="Composite", crit=84850)

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")[0]
    assert reading["celsius"] == pytest.approx(81.9)


def test_a_published_limit_is_rounded_the_same_way_the_reading_is(tmp_path):
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "nvme"), 1, 40000, label="Composite", max_=81850, crit=84850)

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")[0]
    assert reading["max_celsius"] == pytest.approx(81.9)
    assert reading["critical_celsius"] == pytest.approx(84.9)


# ==========================================================================
# The sentence has to quote the limit that is actually being breached
# ==========================================================================
def test_a_critical_sensor_quotes_the_critical_point_it_has_passed(tmp_path):
    # An NVMe at 86 is critical because it is past temp1_crit (84.85), not
    # because of temp1_max (81.85). Quoting the max tells the owner a number
    # that is not the limit the state is about.
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "nvme"), 1, 86000, label="Composite",
         max_=81850, crit=84850)

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")[0]
    assert reading["state"] == "critical"
    assert "past the 85 °C" in reading["summary"], reading["summary"]
    assert "82 °C" not in reading["summary"], reading["summary"]


def test_a_hot_sensor_still_quotes_the_max_it_has_gone_above(tmp_path):
    # "hot" is the max being passed, so that is the number that belongs in
    # the sentence - the crit is still ahead of it.
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "nvme"), 1, 83000, label="Composite",
         max_=81850, crit=84850)

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")[0]
    assert reading["state"] == "hot"
    assert "above the 82 °C" in reading["summary"], reading["summary"]


def test_a_sensor_with_only_a_critical_point_quotes_that_when_it_is_critical(tmp_path):
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "nvme"), 1, 90000, label="Composite", crit=84850)

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")[0]
    assert reading["state"] == "critical"
    assert "past the 85 °C" in reading["summary"], reading["summary"]


# ==========================================================================
# Zero degrees: an unwired header, not a reading
# ==========================================================================
def test_a_sensor_on_an_unpopulated_header_is_not_shown_as_a_zero_degree_reading(tmp_path):
    # Motherboard monitoring chips publish a temperature line for every header
    # on the board whether anything is plugged into it or not, and an unused
    # one reads exactly zero. A "0 °C" row with no limit beside it is a wiring
    # detail dressed up as a reading about the box.
    hw = empty(tmp_path, "hwmon")
    node = hwmon(hw, 0, "nct6775")
    temp(node, 1, 0, label="AUXTIN0")
    temp(node, 2, 36000, label="SYSTIN")

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")
    assert [r["label"] for r in readings] == ["SYSTIN"]


def test_a_genuinely_cold_box_is_still_reported_and_not_hidden(tmp_path):
    # A box in a garage in winter is cold, not faulty. Only exactly zero is
    # treated as an unwired header, so a real reading either side of freezing
    # still reaches the page.
    hw = empty(tmp_path, "hwmon")
    node = hwmon(hw, 0, "coretemp")
    temp(node, 1, 100, label="Package id 0", max_=100000)     # 0.1 °C
    temp(node, 2, -4000, label="Core 0", max_=100000)         # -4 °C

    readings = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")
    assert sorted(r["celsius"] for r in readings) == [
        pytest.approx(-4.0), pytest.approx(0.1)]


def test_a_limit_published_as_zero_is_treated_as_no_limit_at_all(tmp_path):
    # A chip that publishes temp1_max of 0 has not told us its limit, and
    # comparing anything against zero would paint a healthy box critical.
    hw = empty(tmp_path, "hwmon")
    temp(hwmon(hw, 0, "it87"), 1, 44000, label="SYSTIN", max_=0)

    reading = sensors.temperatures(hwmon_root=hw, thermal_root=tmp_path / "n")[0]
    assert reading["max_celsius"] is None
    assert reading["state"] == "unknown"


# ==========================================================================
# Two fans, two names - or nobody knows which one to go and look at
# ==========================================================================
def test_two_hwmon_nodes_each_with_a_fan_1_are_not_both_called_fan_1(tmp_path):
    # One of these has stopped. Two rows with the same name on them do not
    # tell the person holding a screwdriver which fan to go and look at.
    hw = empty(tmp_path, "hwmon")
    fan(hwmon(hw, 0, "nct6775"), 1, 0)
    fan(hwmon(hw, 1, "dell_smm"), 1, 2400)

    found = sensors.fans(hwmon_root=hw)
    names = [f["name"] for f in found]
    assert len(set(names)) == 2, names
    assert any("nct6775" in n for n in names), names
    assert any("dell_smm" in n for n in names), names
    stopped = [f for f in found if f["stopped"]][0]
    assert "nct6775" in stopped["summary"], stopped["summary"]


def test_the_only_fan_in_the_box_is_still_just_called_fan_1(tmp_path):
    # Disambiguating a name that was never ambiguous only makes it harder to
    # read, and the driver's name means nothing to the owner.
    hw = empty(tmp_path, "hwmon")
    fan(hwmon(hw, 0, "nct6775"), 1, 2400)

    assert [f["name"] for f in sensors.fans(hwmon_root=hw)] == ["Fan 1"]


def test_a_fan_whose_name_is_already_unique_keeps_it_when_another_pair_clashes(tmp_path):
    hw = empty(tmp_path, "hwmon")
    node = hwmon(hw, 0, "nct6775")
    fan(node, 1, 1200)
    fan(node, 2, 900)
    fan(hwmon(hw, 1, "dell_smm"), 1, 2400)

    names = [f["name"] for f in sensors.fans(hwmon_root=hw)]
    assert "Fan 2" in names, names
    assert len(set(names)) == 3, names


def test_two_identical_chips_both_reporting_fan_1_are_still_told_apart(tmp_path):
    # Same driver twice, so the driver's name alone does not separate them.
    hw = empty(tmp_path, "hwmon")
    fan(hwmon(hw, 0, "nct6775"), 1, 1200)
    fan(hwmon(hw, 1, "nct6775"), 1, 0)

    names = [f["name"] for f in sensors.fans(hwmon_root=hw)]
    assert len(set(names)) == 2, names


def test_two_fans_sharing_one_label_are_told_apart_as_well(tmp_path):
    hw = empty(tmp_path, "hwmon")
    fan(hwmon(hw, 0, "nct6775"), 1, 1200, label="Chassis Fan")
    fan(hwmon(hw, 1, "dell_smm"), 1, 0, label="Chassis Fan")

    names = [f["name"] for f in sensors.fans(hwmon_root=hw)]
    assert len(set(names)) == 2, names


# ==========================================================================
# smartctl is a fork. It does not get to happen on every page load.
# ==========================================================================
def test_the_smart_cache_lifetime_is_measured_in_hours_not_seconds():
    # Power-on hours, reallocated sectors and life remaining move over months.
    # Re-reading them every few seconds costs a fork and tells nobody anything.
    assert sensors.SMART_CACHE_SECONDS >= 3600


def test_smartctl_is_not_forked_again_for_every_page_load(tmp_path):
    # This is a two-core Celeron playing video. A subprocess per drive per
    # poll is the box measuring itself instead of showing the picture.
    blocks = empty(tmp_path, "block")
    block_device(blocks, "nvme0n1")
    calls = []
    now = [1000.0]
    cache = sensors.SmartCache(
        runner=lambda device: calls.append(device) or HEALTHY_NVME,
        clock=lambda: now[0], background=False)

    for _ in range(5):
        disk = sensors.disks(block_root=blocks, smart_runner=cache)[0]

    assert calls == ["nvme0n1"]
    assert disk["power_on_hours"] == 4102


def test_smart_data_is_read_again_once_it_is_old_enough_to_have_changed(tmp_path):
    blocks = empty(tmp_path, "block")
    block_device(blocks, "nvme0n1")
    calls = []
    now = [1000.0]
    cache = sensors.SmartCache(
        runner=lambda device: calls.append(device) or HEALTHY_NVME,
        clock=lambda: now[0], background=False)

    sensors.disks(block_root=blocks, smart_runner=cache)
    now[0] += sensors.SMART_CACHE_SECONDS + 1
    sensors.disks(block_root=blocks, smart_runner=cache)

    assert calls == ["nvme0n1", "nvme0n1"]


def test_a_box_without_smartmontools_is_not_asked_again_on_every_load(tmp_path):
    # "Not installed" is an answer, and it is worth remembering. Otherwise the
    # gating is undone by the one box that will pay for it every single poll.
    blocks = empty(tmp_path, "block")
    block_device(blocks, "sda")
    calls = []
    cache = sensors.SmartCache(
        runner=lambda device: calls.append(device) or None,
        clock=lambda: 5.0, background=False)

    for _ in range(4):
        disk = sensors.disks(block_root=blocks, smart_runner=cache)[0]

    assert calls == ["sda"]
    assert disk["healthy"] is None


def test_a_smart_read_that_raises_is_remembered_rather_than_retried(tmp_path):
    blocks = empty(tmp_path, "block")
    block_device(blocks, "sda")
    calls = []

    def angry(device):
        calls.append(device)
        raise OSError("smartctl fell over")

    cache = sensors.SmartCache(runner=angry, clock=lambda: 5.0, background=False)
    for _ in range(3):
        disk = sensors.disks(block_root=blocks, smart_runner=cache)[0]

    assert calls == ["sda"]
    assert disk["healthy"] is None


def test_the_default_disk_read_goes_through_the_cache_rather_than_forking(tmp_path,
                                                                         monkeypatch):
    # report() defaults smart_runner to None, and that default is the one the
    # System page will actually use.
    blocks = empty(tmp_path, "block")
    block_device(blocks, "sda", rotational=1)
    calls = []
    monkeypatch.setattr(sensors, "_DEFAULT_SMART_CACHE", sensors.SmartCache(
        runner=lambda device: calls.append(device) or FAILING_SATA,
        clock=lambda: 0.0, background=False))

    sensors.disks(block_root=blocks)
    second = sensors.disks(block_root=blocks)[0]

    assert calls == ["sda"]
    assert second["reallocated_sectors"] == 2048


def test_a_slow_first_smart_read_does_not_hold_the_page_up(tmp_path):
    # The first read is the expensive one, and a sick disk can take the whole
    # timeout. It happens behind the page, never in front of it: nothing the
    # owner is looking at waits on a fork, and neither does the picture.
    import threading
    import time

    blocks = empty(tmp_path, "block")
    block_device(blocks, "nvme0n1")
    started = threading.Event()
    released = threading.Event()

    def slow(device):
        started.set()
        assert released.wait(10), "the slow read was never released"
        return HEALTHY_NVME

    cache = sensors.SmartCache(runner=slow)
    began = time.monotonic()
    disk = sensors.disks(block_root=blocks, smart_runner=cache)[0]
    waited = time.monotonic() - began
    assert waited < 1.0, "the page sat for {:.1f}s waiting on smartctl".format(waited)
    assert disk["healthy"] is None, "the page waited for smartctl"
    assert started.wait(10), "the read never happened behind the page"

    released.set()
    for _ in range(500):
        disk = sensors.disks(block_root=blocks, smart_runner=cache)[0]
        if disk["healthy"] is not None:
            break
        time.sleep(0.01)
    assert disk["power_on_hours"] == 4102


def test_one_slow_drive_is_only_read_once_however_often_the_page_is_opened(tmp_path):
    # Every poll while the first read is still running must not pile up
    # another fork behind it.
    import threading

    blocks = empty(tmp_path, "block")
    block_device(blocks, "nvme0n1")
    released = threading.Event()
    calls = []

    def slow(device):
        calls.append(device)
        assert released.wait(10), "the slow read was never released"
        return HEALTHY_NVME

    cache = sensors.SmartCache(runner=slow)
    try:
        for _ in range(10):
            sensors.disks(block_root=blocks, smart_runner=cache)
        assert calls == ["nvme0n1"]
    finally:
        released.set()
