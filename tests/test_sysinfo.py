""""Is my box alright?", answered without a terminal.

The box this runs on is a machine nobody has seen: no temperature sensor, a
media drive that may or may not be its own disk, an hwdetect that may never
have run. Every one of those is normal, and none of them may take the page
down - a health page that breaks when something is missing is worse than no
health page, because it breaks exactly when you are trying to find out what
is wrong.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from retrobox import sysinfo


def disk(total_gb, free_gb):
    return SimpleNamespace(
        total=total_gb * 1024**3,
        used=(total_gb - free_gb) * 1024**3,
        free=free_gb * 1024**3,
    )


# ==========================================================================
# Storage - the field that actually matters
# ==========================================================================
def test_the_media_and_root_volumes_are_reported_separately(tmp_path, monkeypatch):
    # They are frequently different disks. Conflating them hides the one that
    # is about to bite: a full root filesystem is a box that will not boot.
    sizes = {"/": disk(16, 1), str(tmp_path): disk(2000, 900)}
    monkeypatch.setattr(sysinfo.shutil, "disk_usage", lambda p: sizes[str(p)])

    report = sysinfo.storage(media_root=tmp_path)
    assert report["root"]["free_bytes"] == 1 * 1024**3
    assert report["media"]["free_bytes"] == 900 * 1024**3
    assert report["media"]["path"] == str(tmp_path)


def test_a_full_root_volume_raises_the_alarm(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sysinfo.shutil, "disk_usage",
        lambda p: disk(16, 0) if str(p) == "/" else disk(2000, 900),
    )
    report = sysinfo.storage(media_root=tmp_path)
    assert report["root"]["state"] == "critical"
    assert report["warning"] is True


def test_a_nearly_full_media_volume_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sysinfo.shutil, "disk_usage",
        lambda p: disk(16, 8) if str(p) == "/" else disk(2000, 20),
    )
    report = sysinfo.storage(media_root=tmp_path)
    assert report["media"]["state"] == "low"
    assert report["warning"] is True


def test_a_healthy_box_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(sysinfo.shutil, "disk_usage", lambda p: disk(500, 300))
    report = sysinfo.storage(media_root=tmp_path)
    assert report["warning"] is False
    assert report["root"]["state"] == "ok" and report["media"]["state"] == "ok"


def test_media_on_the_same_disk_is_not_reported_twice(tmp_path, monkeypatch):
    # One disk is the common case on a small box. Showing it as two volumes
    # reads as "you have twice as much space as you do".
    monkeypatch.setattr(sysinfo.shutil, "disk_usage", lambda p: disk(500, 300))
    monkeypatch.setattr(sysinfo.os, "stat", _same_device)
    report = sysinfo.storage(media_root=tmp_path)
    assert report["media"]["same_disk_as_root"] is True


def _same_device(path, *a, **k):
    return SimpleNamespace(st_dev=1, st_ino=hash(str(path)) & 0xFFFF)


def test_no_media_root_configured_is_not_an_error(monkeypatch):
    monkeypatch.setattr(sysinfo.shutil, "disk_usage", lambda p: disk(50, 25))
    report = sysinfo.storage(media_root=None)
    assert report["media"] is None
    assert report["root"]["free_bytes"] > 0


def test_an_unreadable_volume_does_not_break_the_page(tmp_path, monkeypatch):
    def refuse(path):
        raise OSError("gone")

    monkeypatch.setattr(sysinfo.shutil, "disk_usage", refuse)
    report = sysinfo.storage(media_root=tmp_path)
    assert report["root"] is None and report["media"] is None
    assert report["warning"] is False, "unknown is not the same as bad"


# ==========================================================================
# Temperature and load, which plenty of machines simply do not have
# ==========================================================================
def test_temperature_is_read_when_the_platform_has_one(tmp_path):
    # A fixture tree the test builds itself, passed straight to the function,
    # rather than a monkeypatch of the module global - so this test's result
    # cannot depend on whatever /sys/class/thermal the host machine happens
    # to have.
    zone = tmp_path / "thermal_zone0"
    zone.mkdir()
    (zone / "temp").write_text("54321\n")
    (zone / "type").write_text("x86_pkg_temp\n")
    assert sysinfo.temperature(thermal_root=tmp_path)["celsius"] == pytest.approx(
        54.3, abs=0.1
    )


def test_no_thermal_sensor_is_reported_as_unknown_not_zero(tmp_path):
    # Reporting 0 degrees would look like a reading. It is the absence of one.
    assert sysinfo.temperature(thermal_root=tmp_path / "nothing-here") is None


def test_a_nonsense_thermal_reading_is_ignored(tmp_path):
    zone = tmp_path / "thermal_zone0"
    zone.mkdir()
    (zone / "temp").write_text("banana")
    assert sysinfo.temperature(thermal_root=tmp_path) is None


def test_temperature_never_touches_the_real_sysfs_when_a_root_is_supplied(
    tmp_path, monkeypatch
):
    # Proof the fix works: even if the real (module-default) THERMAL_ROOT is
    # unreadable or points at machine state this test does not control,
    # passing an explicit root must be the only thing that is read.
    real_root = Path("/sys/class/thermal")
    real_glob = type(real_root).glob

    def poisoned_glob(self, pattern):
        if self == real_root:
            raise AssertionError(
                "temperature() read the real sysfs root instead of the one it was given"
            )
        return real_glob(self, pattern)

    monkeypatch.setattr(sysinfo, "THERMAL_ROOT", real_root)
    monkeypatch.setattr(type(real_root), "glob", poisoned_glob, raising=False)

    zone = tmp_path / "thermal_zone0"
    zone.mkdir()
    (zone / "temp").write_text("40000\n")
    (zone / "type").write_text("cpu-thermal\n")
    assert sysinfo.temperature(thermal_root=tmp_path)["celsius"] == pytest.approx(40.0)


def test_temperature_still_honours_a_monkeypatched_thermal_root_by_default(
    tmp_path, monkeypatch
):
    # Existing callers (report(), and other tests outside this file) rely on
    # overriding sysinfo.THERMAL_ROOT and calling temperature() with no
    # argument at all. That has to keep working: the injectable parameter is
    # additive, not a replacement for the module-global override.
    zone = tmp_path / "thermal_zone0"
    zone.mkdir()
    (zone / "temp").write_text("30000\n")
    (zone / "type").write_text("cpu-thermal\n")
    monkeypatch.setattr(sysinfo, "THERMAL_ROOT", tmp_path)
    assert sysinfo.temperature()["celsius"] == pytest.approx(30.0)


def test_load_average_is_reported_with_the_core_count(monkeypatch):
    monkeypatch.setattr(sysinfo.os, "getloadavg", lambda: (0.5, 0.7, 0.9))
    monkeypatch.setattr(sysinfo.os, "cpu_count", lambda: 4)
    load = sysinfo.load()
    assert load["one_minute"] == 0.5 and load["cores"] == 4


def test_a_platform_without_load_average_is_not_an_error(monkeypatch):
    def refuse():
        raise OSError("not here")

    monkeypatch.setattr(sysinfo.os, "getloadavg", refuse)
    assert sysinfo.load() is None


# ==========================================================================
# Where the box is on the network
# ==========================================================================
def test_addresses_list_each_interface(monkeypatch):
    monkeypatch.setattr(
        sysinfo, "_run",
        lambda cmd, **k: "192.168.1.42 10.0.0.5 " if cmd[0] == "hostname" else "",
    )
    monkeypatch.setattr(sysinfo.socket, "gethostname", lambda: "retrobox")
    where = sysinfo.addresses()
    assert where["hostname"] == "retrobox"
    assert where["addresses"] == ["192.168.1.42", "10.0.0.5"]
    assert where["urls"][0] == "http://retrobox.local/"


def test_a_box_with_no_network_still_reports_its_name(monkeypatch):
    monkeypatch.setattr(sysinfo, "_run", lambda cmd, **k: "")
    monkeypatch.setattr(sysinfo.socket, "gethostname", lambda: "retrobox")
    where = sysinfo.addresses()
    assert where["addresses"] == []
    assert where["hostname"] == "retrobox"


# ==========================================================================
# Hardware, in words rather than a vainfo dump
# ==========================================================================
def test_hardware_is_summarised_in_a_sentence(monkeypatch):
    from retrobox.hwdetect import HardwareReport

    monkeypatch.setattr(
        sysinfo.hwdetect, "build_report",
        lambda **k: HardwareReport(
            gpu_vendor="intel", gpu_description="Intel UHD 630",
            decode_packages=["intel-media-va-driver"],
            audio_devices=["alsa/hdmi:CARD=PCH"],
            recommended_audio_device="alsa/hdmi:CARD=PCH",
            # The verdict is reached in build_report, which is the only place
            # that can tell "the GPU is not decoding" apart from "this process
            # was not allowed to open the GPU and ask". sysinfo carries it; it
            # no longer re-derives it from a two-state bool.
            decode_working=True,
            decode_summary="Hardware decode: working, Intel UHD 630",
        ),
    )

    hw = sysinfo.hardware()
    assert hw["gpu_vendor"] == "intel"
    assert hw["decode"]["working"] is True
    assert "Intel" in hw["decode"]["summary"]
    assert hw["audio_devices"] == ["alsa/hdmi:CARD=PCH"]


def test_hardware_detection_failing_is_reported_not_raised(monkeypatch):
    def refuse(**k):
        raise OSError("no lspci on this thing")

    monkeypatch.setattr(sysinfo.hwdetect, "build_report", refuse)
    hw = sysinfo.hardware()
    assert hw["gpu_vendor"] == "unknown"
    assert hw["decode"]["working"] is None, "could not tell is not the same as no"


def test_software_decode_is_described_as_fine_because_it_is(monkeypatch):
    from retrobox.hwdetect import HardwareReport

    monkeypatch.setattr(
        sysinfo.hwdetect, "build_report",
        lambda **k: HardwareReport(
            gpu_vendor="nvidia", gpu_description="NVIDIA GK107",
            decode_packages=[], audio_devices=[], recommended_audio_device=None,
            decode_working=False,
            decode_summary=("Hardware decode: not available for NVIDIA GK107, "
                            "using software decode - which is fine"),
        ),
    )
    hw = sysinfo.hardware()
    assert hw["decode"]["working"] is False
    assert "software" in hw["decode"]["summary"].lower()


# ==========================================================================
# The whole report, which is what the page actually asks for
# ==========================================================================
def test_the_report_survives_every_source_being_absent(monkeypatch, tmp_path):
    def refuse(*a, **k):
        raise OSError("nothing works on this box")

    monkeypatch.setattr(sysinfo.shutil, "disk_usage", refuse)
    monkeypatch.setattr(sysinfo.os, "getloadavg", refuse)
    monkeypatch.setattr(sysinfo, "THERMAL_ROOT", tmp_path / "nope")
    monkeypatch.setattr(sysinfo, "_run", lambda cmd, **k: "")
    monkeypatch.setattr(sysinfo.hwdetect, "build_report", refuse)

    report = sysinfo.report(media_root=None)
    for key in ("version", "storage", "addresses", "hardware", "uptime_seconds",
                "temperature", "load", "share", "timezone"):
        assert key in report, key


def test_the_report_includes_the_version_it_is_running(monkeypatch, tmp_path):
    monkeypatch.setattr(sysinfo.shutil, "disk_usage", lambda p: disk(50, 25))
    from retrobox import __version__

    assert sysinfo.report(media_root=None)["version"] == __version__


def test_an_undetectable_gpu_is_not_described_as_a_sentence(monkeypatch):
    # detect_gpu returns its reason as the description ("lspci not available,
    # install pciutils"). Dropping that into "not available for {description},
    # using software decode" produces a line nobody can read.
    from retrobox.hwdetect import HardwareReport

    monkeypatch.setattr(
        sysinfo.hwdetect, "build_report",
        lambda **k: HardwareReport(
            gpu_vendor="unknown",
            gpu_description="lspci not available, install pciutils",
            decode_packages=[], audio_devices=[], recommended_audio_device=None,
        ),
    )
    hw = sysinfo.hardware()
    assert hw["decode"]["working"] is None
    assert "lspci" not in hw["decode"]["summary"]
    assert "could not tell" in hw["decode"]["summary"]


# ==========================================================================
# The clock - the reading that changes what the television shows
# ==========================================================================
def test_the_clock_reading_says_when_it_last_actually_synchronised(monkeypatch, tmp_path):
    # "Synchronised" on its own is not an answer anybody can act on. "Four
    # minutes ago" and "never" are completely different states of the same
    # box, and dayparting is wrong in the second one.
    from retrobox import timekeeping

    monkeypatch.setattr(
        sysinfo, "_run",
        lambda cmd, **kw: "Timezone=Australia/Melbourne\nNTP=yes\nNTPSynchronized=yes\n",
    )
    clock_file = tmp_path / "clock"
    clock_file.touch()
    monkeypatch.setattr(timekeeping, "TIMESYNC_CLOCK", clock_file)

    info = sysinfo.timezone()
    assert info["synchronised"] is True
    assert info["last_sync_state"] == "known"
    assert isinstance(info["last_sync"], float)
    assert info["sync_summary"]


def test_a_clock_that_cannot_be_asked_says_so_rather_than_saying_never(monkeypatch):
    # No timedatectl at all. "Never synchronised" would send somebody off to
    # buy a battery for a box that is perfectly healthy.
    monkeypatch.setattr(sysinfo, "_run", lambda cmd, **kw: "")

    info = sysinfo.timezone()
    assert info["synchronised"] is None
    assert info["last_sync_state"] == "unknown"
    assert info["warning"] is False


def test_a_clock_from_before_this_software_existed_is_flagged(monkeypatch):
    # A flat CMOS battery, which is the single most common fault on a
    # ten-year-old office mini PC.
    monkeypatch.setattr(sysinfo, "_run", lambda cmd, **kw: "")
    monkeypatch.setattr(sysinfo.time, "time", lambda: 1_300_000_000.0)

    info = sysinfo.timezone()
    assert info["plausible"] is False
    assert info["warning"] is True


def test_the_clock_reading_never_raises_when_timekeeping_cannot_answer(monkeypatch):
    from retrobox import timekeeping

    def broken(**kwargs):
        raise OSError("nothing works on this box")

    monkeypatch.setattr(sysinfo, "_run", lambda cmd, **kw: "Timezone=Etc/UTC\n")
    monkeypatch.setattr(timekeeping, "sync_status", broken)

    info = sysinfo.timezone()
    assert info["timezone"] == "Etc/UTC"
    assert info["last_sync_state"] == "unknown"
