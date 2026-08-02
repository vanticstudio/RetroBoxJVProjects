""""Is my box coping?" - answered from the sensors the box actually has.

The person reading the System page is not technical, is watching television,
and wants one question answered. "LOAD 1.53 over 2 cores" is not an answer to
it. Heat, a stopped fan, a processor slowing itself down and a worn-out drive
are, because each of them is a thing they could act on.

Three rules shape everything in here.

**Nothing is hardcoded to one machine.** These are cheap secondhand mini PCs
and the chipset is unknown until the box is in somebody's hands. So this
module enumerates whatever the kernel exposes - every hwmon node, every
thermal zone - rather than looking for the sensors one particular machine
happened to have.

**The limits come from the manufacturer, never from us.** Seventy degrees is a
happy NVMe drive, a normal chipset and a comfortable CPU, and each has a
completely different point where it stops being fine. hwmon publishes those
points next to the reading (``temp1_max``, ``temp1_crit``) and thermal zones
publish them as trip points, so those are what decide the state. Where a
sensor publishes no limits, the state is *unknown* - inventing one would mean
guessing on hardware we have never seen.

**Absent and bad are different states.** No fan node means a passively cooled
box, which is healthy; a fan node reading zero means a seized fan, which is
not. Conflating them either hides a real fault or puts a frightening false
alarm on a perfectly good box.

Every reader takes its root directory as a parameter with the real path as the
default, because the sensors this must cope with do not exist on the machine
the tests run on and have to be built as files instead.

Nothing here may affect playback: temperatures, fans and throttling are read
from files only, never by running a command. The single exception is the
optional SMART block, which shells out only if smartmontools already happens
to be installed, gives up quickly if it is not, runs behind the page rather
than in front of it, and remembers the answer for an hour.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

log = logging.getLogger(__name__)

#: Where Linux publishes sensors. Overridable so tests need no hardware.
HWMON_ROOT = Path("/sys/class/hwmon")
THERMAL_ROOT = Path("/sys/class/thermal")
CPU_ROOT = Path("/sys/devices/system/cpu")
BLOCK_ROOT = Path("/sys/block")

#: How long smartctl gets. It is optional detail on a page about the box, and
#: a page that hangs waiting for a sick disk is worse than one that omits it.
SMART_TIMEOUT = 4.0

#: How long a SMART answer is kept before it is worth asking again. Power-on
#: hours, reallocated sectors and life remaining move over months, so an hour
#: is already generous - and the alternative is forking a subprocess per drive
#: every time somebody opens the System page, on a box whose whole job is to
#: keep decoding video while they look at it.
SMART_CACHE_SECONDS = 3600.0

# These two are NOT health thresholds - no judgement about hot or cold is made
# anywhere in this file except against the kernel's own numbers. They only ask
# "is this a temperature at all", because unconnected sensor pins report
# things like -128 degrees, and showing that as a reading is a lie of a
# different kind.
COLDEST_PLAUSIBLE_C = -40.0
HOTTEST_PLAUSIBLE_C = 200.0

PathLike = Union[str, Path]


# ==========================================================================
# Reading files that may not exist, may not be readable, and may be rubbish
# ==========================================================================
def _text(path: Path) -> Optional[str]:
    """The contents of a sysfs file, or None for every way that can fail.

    A sensor node can vanish between the glob and the read (USB drives,
    hot-added devices), be unreadable, or be a directory. All of those mean
    the same thing to the page: we do not know.
    """
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, ValueError):
        return None


def _integer(path: Path) -> Optional[int]:
    """The number in a sysfs file. Rubbish reads as absent, not as zero."""
    raw = _text(path)
    if not raw:
        return None
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


def _tenths(millidegrees: int) -> float:
    """Millidegrees to one decimal place, rounding half away from zero.

    ``round(81.85, 1)`` gives 81.8, because 81.85 is not exactly 81.85 once it
    is a binary float. That is a threshold number printed to a customer right
    beside the limit it is being compared against, so it rounds the way the
    person reading it was taught to at school, and it does it on the integer
    the kernel actually published rather than on a float made out of it.
    """
    whole, remainder = divmod(abs(millidegrees), 100)
    tenths = whole + (1 if remainder >= 50 else 0)
    return (-tenths if millidegrees < 0 else tenths) / 10.0


def _celsius(path: Path) -> Optional[float]:
    """A hwmon or thermal-zone temperature, which are both in millidegrees."""
    value = _integer(path)
    if value is None:
        return None
    # Exactly zero millidegrees is a header with nothing plugged into it.
    # Motherboard monitoring chips publish a temperature line for every header
    # on the board whether it is wired or not, and the unused ones sit at a
    # dead 0 - which then appears on the page as a "0 °C" row with no limit
    # beside it, which is a wiring detail dressed up as news about the box.
    # Only *exactly* zero is treated this way: a box in a cold garage reading
    # 0.1 or -4 is a genuine reading and stays, because hiding a real cold box
    # would be the same mistake in the other direction.
    if value == 0:
        return None
    degrees = _tenths(value)
    if not COLDEST_PLAUSIBLE_C < degrees < HOTTEST_PLAUSIBLE_C:
        return None
    return degrees


_DIGITS = re.compile(r"(\d+)")


def _order(path: Path) -> Tuple[str, int]:
    """Sort hwmon10 after hwmon2 rather than after hwmon1."""
    digits = _DIGITS.search(path.name)
    return (_DIGITS.sub("", path.name), int(digits.group(1)) if digits else 0)


def _children(root: PathLike, pattern: str) -> List[Path]:
    """Directories matching a pattern, or nothing at all if the root is absent."""
    try:
        return sorted((p for p in Path(root).glob(pattern) if p.is_dir()), key=_order)
    except OSError:
        return []


def _files(directory: Path, pattern: str) -> List[Path]:
    try:
        return sorted(directory.glob(pattern), key=_order)
    except OSError:
        return []


def _readings_dir(node: Path) -> Path:
    """Where this hwmon node keeps its numbers.

    Modern kernels put them straight in the node; older ones put them one
    level down in ``device/``. Both turn up on secondhand hardware, and
    checking costs one stat.
    """
    if _files(node, "temp*_input") or _files(node, "fan*_input"):
        return node
    device = node / "device"
    try:
        if device.is_dir():
            return device
    except OSError:
        pass
    return node


# ==========================================================================
# Saying which sensor is which, in words rather than in kernel identifiers
# ==========================================================================
# "temp1_input on pch_cannonlake" is not an answer to any question the owner
# has. These map the driver (and the label the driver publishes, where it
# publishes one) onto the name of the part somebody would point at.
#
# The list is deliberately open-ended: anything not on it keeps the kernel's
# own name rather than being renamed to something we are only guessing at.
_CPU_DRIVERS = {
    "coretemp", "k10temp", "k8temp", "zenpower", "via_cputemp",
    "cpu_thermal", "soc_dts0", "soc_dts1", "x86_pkg_temp",
}
_GRAPHICS_DRIVERS = {"amdgpu", "radeon", "nouveau", "i915", "xe"}
_WIFI_DRIVERS = {"iwlwifi", "ath10k_hwmon", "ath11k_hwmon", "mt7921_phy0"}
_SYSTEM_DRIVERS = {"acpitz", "acpitz-acpi-0", "thermal", "int3400 thermal"}

_CORE_LABEL = re.compile(r"^core\s+(\d+)$", re.IGNORECASE)
_CPU_LABELS = {"package id 0", "tctl", "tdie", "cputin", "cpu temp", "cpu"}
_SYSTEM_LABELS = {"systin", "system", "sys temp", "ambient", "motherboard"}


def _identify(driver: str, label: Optional[str]) -> Optional[str]:
    """A name a person recognises, or None when we genuinely do not know."""
    key = (driver or "").strip().lower()
    text = (label or "").strip()
    low = text.lower()

    # The chipset driver carries Intel's codename for the silicon
    # (pch_cannonlake, pch_skylake). Nobody who owns one of these knows that.
    if key.startswith("pch") or key == "chipset":
        return "Chipset"

    if key in _CPU_DRIVERS:
        core = _CORE_LABEL.match(text)
        if core:
            # The kernel counts cores from nought. People count from one, and
            # the box has a sticker on it that says four cores, not 0-3.
            return "CPU core {}".format(int(core.group(1)) + 1)
        return "CPU"

    if key == "nvme":
        return "SSD"
    if key in {"drivetemp", "sd", "ata"}:
        return "Drive"
    if key in _GRAPHICS_DRIVERS:
        return "Graphics"
    if key in _WIFI_DRIVERS:
        return "Wi-Fi adapter"
    if key in _SYSTEM_DRIVERS:
        return "System"

    # Motherboard monitoring chips (nct6775, it87, dell_smm and friends) put
    # everything under one driver, so here it is the label that says what the
    # sensor is attached to - and only some of the labels mean anything.
    if low in _CPU_LABELS:
        return "CPU"
    if low in _SYSTEM_LABELS:
        return "System"
    return None


def _raw_name(driver: str, label: Optional[str], sensor: str) -> str:
    """What to call a sensor we cannot identify.

    Showing it is right: it is a real reading and hiding it would lose it.
    Renaming it is wrong: we would be telling the owner it means something we
    do not know it means. So it keeps the name the kernel gave it.
    """
    if label:
        return "{} ({})".format(label, driver)
    return "{} on {}".format(sensor, driver)


# ==========================================================================
# Temperature, judged against the numbers the chip's maker published
# ==========================================================================
def _state(
    celsius: float,
    max_c: Optional[float],
    critical_c: Optional[float],
    hysteresis_c: Optional[float],
) -> str:
    """fine / warm / hot / critical, or unknown when nobody told us the limit.

    Only the kernel's own numbers appear here. ``max`` is where the
    manufacturer says the part is out of spec, ``crit`` is where it will take
    action to save itself, and the gap between them is that manufacturer's own
    idea of how much headroom this part has - which is what makes it the
    honest place to start saying "getting warm" rather than a number we picked.
    """
    hot_at = max_c if max_c is not None else critical_c
    if hot_at is None:
        # No max, no crit. There is no truthful way to say whether 61 degrees
        # is fine on a part we know nothing about, so we do not say.
        return "unknown"

    if critical_c is not None and celsius >= critical_c:
        return "critical"
    if celsius >= hot_at:
        return "hot"

    # Two candidate "getting warm" points, both the kernel's: the hysteresis
    # the driver publishes, and one headroom-span below the limit. The higher
    # one wins, because a driver that reports a nonsense hysteresis of zero
    # would otherwise paint a stone-cold box amber.
    span = hot_at - (critical_c - hot_at) if critical_c is not None and critical_c > hot_at else None
    candidates = [c for c in (hysteresis_c, span) if c is not None and 0 < c < hot_at]
    if candidates and celsius >= max(candidates):
        return "warm"
    return "fine"


def _reading(
    driver: str,
    label: Optional[str],
    sensor: str,
    celsius: float,
    max_c: Optional[float],
    critical_c: Optional[float],
    hysteresis_c: Optional[float],
    source: Path,
) -> Dict[str, Any]:
    plain = _identify(driver, label)
    name = plain or _raw_name(driver, label, sensor)
    state = _state(celsius, max_c, critical_c, hysteresis_c)
    return {
        "name": name,
        "identified": plain is not None,
        "driver": driver,
        "label": label,
        "sensor": sensor,
        "celsius": celsius,
        "state": state,
        "max_celsius": max_c,
        "critical_celsius": critical_c,
        "source": str(source),
        "summary": _sensor_sentence(name, celsius, state, max_c, critical_c),
    }


def _sensor_sentence(name: str, celsius: float, state: str,
                     max_c: Optional[float], critical_c: Optional[float]) -> str:
    """The number in the sentence has to be the limit the state is about.

    A drive publishing a max of 81.85 and a crit of 84.85 and reading 86 is
    critical because of the *crit*. Quoting the max there tells the owner a
    number that is not the one being breached, and 86 next to "the limit is
    81.85" reads as a page that has got its own arithmetic wrong.
    """
    reading = "{} is at {:.0f} °C".format(name, celsius)
    if state == "unknown":
        return ("{}. This part does not publish a safe limit, so there is "
                "nothing to compare it against.".format(reading))
    # Everything short of critical is measured against max where there is one;
    # critical is the crit point being passed, so that is what it quotes.
    limit = max_c if max_c is not None else critical_c
    if state == "critical" and critical_c is not None:
        limit = critical_c
    if state == "critical":
        return ("{}, past the {:.0f} °C its manufacturer treats as the limit. "
                "Expect it to slow down or switch itself off."
                .format(reading, limit))
    if state == "hot":
        return ("{}, above the {:.0f} °C its manufacturer allows."
                .format(reading, limit))
    if state == "warm":
        return "{}, warm but still under the {:.0f} °C limit.".format(reading, limit)
    return "{}, comfortably under the {:.0f} °C limit.".format(reading, limit)


def _hwmon_temperatures(hwmon_root: PathLike) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for node in _children(hwmon_root, "hwmon*"):
        driver = _text(node / "name") or node.name
        where = _readings_dir(node)
        for input_file in _files(where, "temp*_input"):
            sensor = input_file.name[: -len("_input")]
            celsius = _celsius(input_file)
            if celsius is None:
                continue          # unreadable or nonsense: absent, not zero
            found.append(_reading(
                driver=driver,
                label=_text(where / (sensor + "_label")) or None,
                sensor=sensor,
                celsius=celsius,
                max_c=_celsius(where / (sensor + "_max")),
                critical_c=_celsius(where / (sensor + "_crit")),
                hysteresis_c=(_celsius(where / (sensor + "_max_hyst"))
                              or _celsius(where / (sensor + "_crit_hyst"))),
                source=input_file,
            ))
    return found


def _zone_trips(zone: Path) -> Dict[str, float]:
    """A thermal zone's own trip points, which are its published limits."""
    trips: Dict[str, float] = {}
    for trip in _files(zone, "trip_point_*_type"):
        kind = (_text(trip) or "").strip().lower()
        celsius = _celsius(Path(str(trip)[: -len("_type")] + "_temp"))
        if not kind or celsius is None:
            continue
        # Several trips of one kind can exist; the lowest is the one that
        # bites first, so it is the one worth reporting.
        if kind not in trips or celsius < trips[kind]:
            trips[kind] = celsius
    return trips


def _thermal_temperatures(thermal_root: PathLike) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for zone in _children(thermal_root, "thermal_zone*"):
        celsius = _celsius(zone / "temp")
        if celsius is None:
            continue
        kind = _text(zone / "type") or zone.name
        trips = _zone_trips(zone)
        found.append(_reading(
            driver=kind,
            label=None,
            sensor=zone.name,
            celsius=celsius,
            max_c=trips.get("hot"),
            critical_c=trips.get("critical"),
            hysteresis_c=trips.get("passive") or trips.get("active"),
            source=zone / "temp",
        ))
    return found


def temperatures(
    hwmon_root: PathLike = HWMON_ROOT,
    thermal_root: PathLike = THERMAL_ROOT,
) -> List[Dict[str, Any]]:
    """Every temperature this box publishes, named and judged.

    hwmon first because it carries the manufacturer's limits alongside the
    reading; thermal zones after, minus any that are plainly the same sensor
    seen twice - the CPU package usually appears in both, and two identical
    lines on the page reads as two separate problems.
    """
    found = _hwmon_temperatures(hwmon_root)
    already = {(r["name"], r["celsius"]) for r in found}
    for zone in _thermal_temperatures(thermal_root):
        if (zone["name"], zone["celsius"]) in already:
            continue
        found.append(zone)
    return found


def hottest(
    hwmon_root: PathLike = HWMON_ROOT,
    thermal_root: PathLike = THERMAL_ROOT,
) -> Optional[Dict[str, Any]]:
    """The one reading to lead with, or None on a box with no sensors."""
    found = temperatures(hwmon_root=hwmon_root, thermal_root=thermal_root)
    if not found:
        return None
    # Worst state first, then hottest: a chipset 2 degrees over its own limit
    # matters more than a CPU at 70 with 30 to spare.
    rank = {"critical": 4, "hot": 3, "warm": 2, "fine": 1, "unknown": 0}
    return max(found, key=lambda r: (rank.get(r["state"], 0), r["celsius"]))


# ==========================================================================
# Fans - and the difference between "there isn't one" and "it has stopped"
# ==========================================================================
def fans(hwmon_root: PathLike = HWMON_ROOT) -> List[Dict[str, Any]]:
    """Every fan the box publishes a reading for.

    A box with no fan nodes is a fanless box and gets an empty list, not a
    warning: passive cooling is normal on this hardware and a false alarm
    about a fan that was never fitted is worse than no fan section at all.

    A fan node that reads zero is a different matter entirely. That is a
    seized fan - mundane on ten-year-old office hardware, and it presents to
    the owner as nothing more than a stuttering picture.
    """
    found: List[Dict[str, Any]] = []
    for node in _children(hwmon_root, "hwmon*"):
        driver = _text(node / "name") or node.name
        where = _readings_dir(node)
        for input_file in _files(where, "fan*_input"):
            sensor = input_file.name[: -len("_input")]
            rpm = _integer(input_file)
            if rpm is None or rpm < 0:
                continue          # no reading at all - not a stopped fan
            fault_flag = _integer(where / (sensor + "_fault"))
            fault = None if fault_flag is None else bool(fault_flag)
            label = _text(where / (sensor + "_label")) or None
            found.append({
                "name": label or "Fan {}".format(_order(input_file)[1] or 1),
                "driver": driver,
                "node": node.name,
                "sensor": sensor,
                "rpm": rpm,
                "stopped": rpm == 0 or fault is True,
                "fault": fault,
            })

    # The name has to be settled before the sentence is written, because the
    # sentence names the fan the owner is being asked to go and look at.
    _name_fans_apart(found)
    for spinner in found:
        if spinner["stopped"]:
            spinner["summary"] = (
                "{} is not spinning. On a box that has a fan fitted, that "
                "normally means it has seized.".format(spinner["name"]))
        else:
            spinner["summary"] = "{} is spinning at {:,} rpm.".format(
                spinner["name"], spinner["rpm"])
    return found


def _name_fans_apart(found: List[Dict[str, Any]]) -> None:
    """Give any two fans that came out with the same name different ones.

    A fan's index is only unique within its own hwmon node, so a box with a
    super-I/O chip and a vendor driver ends up with two fans both called
    "Fan 1". One row saying a fan has stopped and another saying it is fine,
    both with the same name on them, does not tell somebody holding a
    screwdriver which fan to go and look at. So the ones that clash take the
    driver's name with them, exactly as an unidentified temperature does - and
    only the ones that clash, because "Fan 2 (nct6775)" on a box where there
    is only one Fan 2 is just harder to read.
    """
    base = [f["name"] for f in found]

    def clashing(names: List[str]) -> set:
        return {name for name, count in Counter(names).items() if count > 1}

    clash = clashing(base)
    if not clash:
        return
    named = ["{} ({})".format(name, f["driver"]) if name in clash else name
             for name, f in zip(base, found)]

    # Two of the same chip (two nvme drives, two identical monitoring chips)
    # share a driver name too, and then the kernel's own node name is the only
    # thing left that separates them.
    clash = clashing(named)
    if clash:
        named = ["{} ({} {})".format(original, f["driver"], f["node"])
                 if name in clash else name
                 for original, name, f in zip(base, named, found)]

    for fan_found, name in zip(found, named):
        fan_found["name"] = name


# ==========================================================================
# Throttling - stuttering video that looks exactly like bad software
# ==========================================================================
def throttling(cpu_root: PathLike = CPU_ROOT) -> Optional[Dict[str, Any]]:
    """Has this processor been slowing itself down to cool off?

    This is the single most useful thing on the page, because a box that
    throttles and drops frames is indistinguishable, from the sofa, from a box
    running bad software. The counters are cumulative since boot, so a caller
    that wants "is it happening right now" reads ``events`` twice and compares.

    None means the platform does not publish this - which is unknown, not no.
    """
    core = package = 0
    published = False
    for cpu in _children(cpu_root, "cpu[0-9]*"):
        node = cpu / "thermal_throttle"
        core_count = _integer(node / "core_throttle_count")
        package_count = _integer(node / "package_throttle_count")
        if core_count is None and package_count is None:
            continue
        published = True
        core += core_count or 0
        package += package_count or 0

    if not published:
        return None

    events = core + package
    if events:
        summary = ("This box's processor has slowed itself down to cool off "
                   "{:,} times since it was switched on. That is the usual "
                   "cause of a stuttering picture.".format(events))
    else:
        summary = "The processor has not had to slow itself down to cool off."
    return {
        "core_events": core,
        "package_events": package,
        "events": events,
        "throttled": events > 0,
        "summary": summary,
    }


# ==========================================================================
# The drive, which on secondhand hardware is the oldest part in the box
# ==========================================================================
_NOT_A_DRIVE = ("loop", "ram", "zram", "dm-", "md", "sr", "fd", "nbd")

_HEALTH_LINE = re.compile(
    r"SMART overall-health self-assessment test result:\s*(\S+)", re.IGNORECASE)
_NVME_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9 /_-]+?):\s+(.+?)\s*$")
_SATA_ATTRIBUTE = re.compile(
    r"^\s*(\d+)\s+(\S+)\s+0x[0-9a-fA-F]+\s+(\d+)\s+(\d+)\s+(\d+)\s+"
    r"(\S+)\s+(\S+)\s+(\S+)\s+(.+?)\s*$")

#: Attributes that mean "how much of this drive's life is left", by whichever
#: name its manufacturer chose. The normalised value is the percentage.
_WEAR_ATTRIBUTES = {
    "wear_leveling_count", "media_wearout_indicator", "ssd_life_left",
    "percent_lifetime_remain", "remaining_lifetime_perc", "ssd_life_leftover",
}


def _number(text: str) -> Optional[int]:
    """The first plain number in a smartctl value ("41,231" or "3%")."""
    cleaned = text.replace(",", "").strip()
    match = re.match(r"-?\d+", cleaned)
    return int(match.group(0)) if match else None


def _smartctl(device: str) -> Optional[str]:
    """smartctl's output, if smartmontools happens to be installed.

    It is not a dependency and must never become one. If it is not there, or
    refuses (it usually wants root), the answer is None and the disk section
    simply says less - no error, and nothing shouting about a missing tool at
    somebody who never asked for one.
    """
    binary = shutil.which("smartctl")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "-H", "-A", "/dev/" + device],
            capture_output=True, text=True, timeout=SMART_TIMEOUT, check=False,
        )
    except Exception:  # noqa: BLE001 - an absent or unhappy tool is ordinary
        log.debug("smartctl unavailable for %s", device, exc_info=True)
        return None
    return result.stdout or None


class SmartCache:
    """smartctl's answer, remembered for an hour and fetched behind the page.

    Reading files is cheap; forking a process is not. Everything else in this
    module is a handful of reads out of sysfs, but SMART needs a subprocess -
    and the System page is refreshed while the box is playing video on two
    Celeron cores. Doing that per drive per page load is the box spending its
    afternoon measuring itself, and it shows up as the very stutter the page
    exists to explain.

    So two things happen here, and neither of them changes what is reported.

    **The answer is kept.** Power-on hours, reallocated sectors and life
    remaining are numbers that move over months. Asking again within the hour
    cannot tell anybody anything they were not already told. "smartmontools is
    not installed" is kept the same way - it is an answer, and the box that
    does not have the tool is exactly the box that would otherwise pay for the
    lookup on every single poll.

    **The first answer is fetched behind the page.** A sick disk can sit on
    the whole timeout, so nothing that somebody is looking at waits for it:
    the first call returns what it has (nothing, the first time) and the read
    happens on a daemon thread. The page comes back straight away without the
    SMART block, and has it on the next refresh. Set ``background=False`` for
    a caller that has nothing better to do - the tests, and a one-shot from
    the command line.
    """

    def __init__(
        self,
        *,
        runner: Optional[Callable[[str], Optional[str]]] = None,
        clock: Callable[[], float] = time.monotonic,
        lifetime: float = SMART_CACHE_SECONDS,
        background: bool = True,
    ) -> None:
        self._runner = _smartctl if runner is None else runner
        self._clock = clock
        self._lifetime = max(0.0, float(lifetime))
        self._background = background
        self._lock = threading.Lock()
        self._answers: Dict[str, Tuple[float, Optional[str]]] = {}
        self._running: set = set()

    def __call__(self, device: str) -> Optional[str]:
        """What we know about this drive right now, without going and asking."""
        now = self._clock()
        with self._lock:
            known = self._answers.get(device)
            if known is not None and now - known[0] < self._lifetime:
                return known[1]
            if device in self._running:
                # Already being read. Piling a second fork in behind the first
                # is the thing this class exists to prevent.
                return known[1] if known else None
            self._running.add(device)

        if not self._background:
            self._refresh(device)
            with self._lock:
                fresh = self._answers.get(device)
            return fresh[1] if fresh else None

        threading.Thread(target=self._refresh, args=(device,),
                         name="retrobox-smart", daemon=True).start()
        return known[1] if known else None

    def _refresh(self, device: str) -> None:
        text: Optional[str] = None
        try:
            text = self._runner(device)
        except Exception:  # noqa: BLE001 - an optional extra may never break this
            log.debug("smart read failed for %s", device, exc_info=True)
        finally:
            # A read that failed is remembered too, with the time it failed at.
            # Otherwise the drive that cannot be read is retried on every poll,
            # which is the cost this whole class exists to avoid.
            with self._lock:
                self._answers[device] = (self._clock(), text)
                self._running.discard(device)


#: The one the box uses. Shared, so the hour is an hour for the whole process
#: rather than an hour per page load.
_DEFAULT_SMART_CACHE = SmartCache()


def _parse_smart(text: str) -> Dict[str, Any]:
    """Whatever of interest is in smartctl output, for both NVMe and SATA."""
    facts: Dict[str, Any] = {"concerns": [], "understood": False}

    health = _HEALTH_LINE.search(text)
    if health:
        passed = health.group(1).upper().startswith("PASS")
        facts["understood"] = True
        facts["self_assessment_passed"] = passed
        if not passed:
            facts["concerns"].append(
                "This drive reports that it is failing its own health check.")

    spare = spare_threshold = None
    for line in text.splitlines():
        attribute = _SATA_ATTRIBUTE.match(line)
        if attribute:
            _, name, value, _worst, threshold, kind, _upd, when, raw = attribute.groups()
            name = name.lower()
            facts["understood"] = True
            raw_value = _number(raw)
            if name == "power_on_hours" and raw_value is not None:
                facts["power_on_hours"] = raw_value
            if name == "reallocated_sector_ct" and raw_value is not None:
                facts["reallocated_sectors"] = raw_value
                if raw_value > 0:
                    facts["concerns"].append(
                        "This drive has {:,} reallocated sectors, which means "
                        "parts of it have already gone bad.".format(raw_value))
            if name in _WEAR_ATTRIBUTES:
                facts.setdefault("life_remaining_percent", int(value))
            # The manufacturer's own failure point for this attribute, where
            # it publishes one. We add nothing of our own to it.
            if when not in ("-", "") or (int(threshold) > 0 and int(value) <= int(threshold)):
                if kind.lower().startswith("pre-fail"):
                    facts["concerns"].append(
                        "The drive's own {} check has failed."
                        .format(name.replace("_", " ")))
            continue

        field = _NVME_LINE.match(line)
        if not field:
            continue
        key, value_text = field.group(1).strip().lower(), field.group(2)
        if key == "power on hours":
            facts["power_on_hours"] = _number(value_text)
            facts["understood"] = True
        elif key == "percentage used":
            used = _number(value_text)
            if used is not None:
                facts["understood"] = True
                # Over 100% used is a drive past the endurance its maker rated
                # it for. It is not dead, but nothing left is guaranteed.
                facts["life_remaining_percent"] = max(0, 100 - used)
                if used >= 100:
                    facts["concerns"].append(
                        "This drive has used up all of the write life its "
                        "manufacturer rated it for.")
        elif key == "available spare":
            spare = _number(value_text)
        elif key == "available spare threshold":
            spare_threshold = _number(value_text)
        elif key == "media and data integrity errors":
            errors = _number(value_text)
            if errors:
                facts["understood"] = True
                facts["media_errors"] = errors
                facts["concerns"].append(
                    "This drive has failed to read back {:,} pieces of data "
                    "it stored.".format(errors))

    if spare is not None and spare_threshold is not None:
        facts["understood"] = True
        facts["spare_percent"] = spare
        if spare < spare_threshold:
            facts["concerns"].append(
                "The drive's spare capacity is down to {}%, below the {}% its "
                "manufacturer treats as the limit.".format(spare, spare_threshold))
    return facts


def _capacity(size_bytes: Optional[int]) -> Optional[str]:
    if not size_bytes:
        return None
    # Drives are sold in decimal gigabytes, so that is what the owner will
    # recognise from the sticker.
    gigabytes = size_bytes / 1000 ** 3
    if gigabytes >= 1000:
        return "{:.1f} TB".format(gigabytes / 1000)
    return "{:.0f} GB".format(gigabytes)


def disks(
    block_root: PathLike = BLOCK_ROOT,
    smart_runner: Optional[Callable[[str], Optional[str]]] = None,
) -> List[Dict[str, Any]]:
    """One honest block per drive on whether it looks healthy.

    On secondhand hardware the drive is usually the oldest and most worn part
    in the box, and a failing one is a likelier cause of a bad experience than
    any software. sysfs gives the model and the type with no tools and no
    root; smartmontools, *if it is already installed*, gives hours, wear and
    reallocated sectors as well.

    This is not a monitoring product. It answers "does the drive look alright"
    once, and stops. The default runner is the shared :class:`SmartCache`, so
    calling this on every page load costs no subprocess at all - see that class
    for why that matters on a box that is playing video while you read it.
    """
    ask = _DEFAULT_SMART_CACHE if smart_runner is None else smart_runner
    found: List[Dict[str, Any]] = []

    for node in _children(block_root, "*"):
        name = node.name
        if name.startswith(_NOT_A_DRIVE):
            continue
        device = node / "device"
        try:
            if not device.is_dir():
                continue          # virtual block devices are not drives
        except OSError:
            continue

        sectors = _integer(node / "size")
        rotational = _integer(node / "queue" / "rotational")
        model = _text(device / "model")
        vendor = _text(device / "vendor")
        if model and vendor and vendor.lower() not in ("ata", "nvme"):
            model = "{} {}".format(vendor, model)

        size_bytes = sectors * 512 if sectors else None
        disk: Dict[str, Any] = {
            "device": name,
            "model": model,
            "rotational": None if rotational is None else bool(rotational),
            "size_bytes": size_bytes,
            "capacity": _capacity(size_bytes),
            "healthy": None,
            "concerns": [],
        }

        facts: Dict[str, Any] = {}
        try:
            output = ask(name)
        except Exception:  # noqa: BLE001 - an optional extra may never break this
            log.debug("smart read failed for %s", name, exc_info=True)
            output = None
        if output:
            facts = _parse_smart(output)

        if facts.get("understood"):
            for key in ("power_on_hours", "reallocated_sectors",
                        "life_remaining_percent", "spare_percent",
                        "media_errors", "self_assessment_passed"):
                if key in facts and facts[key] is not None:
                    disk[key] = facts[key]
            disk["concerns"] = list(facts["concerns"])
            disk["healthy"] = not disk["concerns"]

        disk["summary"] = _disk_sentence(disk)
        found.append(disk)
    return found


def _disk_sentence(disk: Dict[str, Any]) -> str:
    kind = "drive"
    if disk["rotational"] is True:
        kind = "hard drive"
    elif disk["rotational"] is False:
        kind = "SSD"
    described = "The {}{}".format(
        kind, " ({})".format(disk["capacity"]) if disk["capacity"] else "")

    if disk["healthy"] is None:
        # Nothing beyond what sysfs knows. Say what it is and stop, rather
        # than leaving an empty section complaining about a missing tool.
        return "{} in this box is a {}.".format(
            described, disk["model"] or "drive whose model it does not report")

    details = []
    if disk.get("life_remaining_percent") is not None:
        details.append("reports {}% of its life remaining".format(
            disk["life_remaining_percent"]))
    if disk.get("power_on_hours") is not None:
        details.append("has been powered on for {:,} hours".format(
            disk["power_on_hours"]))
    tail = " It {}.".format(" and ".join(details)) if details else ""

    if disk["healthy"]:
        return "{} looks healthy.{}".format(described, tail)
    return "{} is not healthy. {}{}".format(
        described, " ".join(disk["concerns"]), tail)


# ==========================================================================
# The whole answer, in one sentence with the detail underneath
# ==========================================================================
_RANK = {"unknown": 0, "fine": 1, "warm": 2, "hot": 3, "critical": 4}


def _safely(read: Callable[[], Any], fallback: Any) -> Any:
    """Never let one broken sensor take the page down with it.

    This runs on an appliance that gets switched off at the wall by somebody
    who cannot see a stack trace and has nobody to send it to.
    """
    try:
        return read()
    except Exception:  # noqa: BLE001 - deliberately everything
        log.debug("sensor read failed", exc_info=True)
        return fallback


def report(
    hwmon_root: PathLike = HWMON_ROOT,
    thermal_root: PathLike = THERMAL_ROOT,
    cpu_root: PathLike = CPU_ROOT,
    block_root: PathLike = BLOCK_ROOT,
    smart_runner: Optional[Callable[[str], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Everything the System page needs to answer "is my box coping?".

    Safe to call on every page load: the temperatures, fans and throttling are
    file reads, and the drive block goes through the shared
    :class:`SmartCache`, which forks at most once an hour per drive and never
    on the caller's thread.

    ``state`` is the whole box's worst temperature state - one of ``fine``,
    ``warm``, ``hot``, ``critical`` or ``unknown`` - and that vocabulary is the
    stable thing for other modules to consume, not the degrees.
    """
    readings = _safely(
        lambda: temperatures(hwmon_root=hwmon_root, thermal_root=thermal_root), [])
    spinning = _safely(lambda: fans(hwmon_root=hwmon_root), [])
    slowdown = _safely(lambda: throttling(cpu_root=cpu_root), None)
    drives = _safely(
        lambda: disks(block_root=block_root, smart_runner=smart_runner), [])

    state = "unknown"
    for reading in readings:
        if _RANK.get(reading["state"], 0) > _RANK[state]:
            state = reading["state"]

    warmest = max(readings, key=lambda r: (_RANK.get(r["state"], 0), r["celsius"]),
                  default=None) if readings else None
    stopped = [f for f in spinning if f["stopped"]]
    unhealthy = [d for d in drives if d["healthy"] is False]
    throttled = bool(slowdown and slowdown["throttled"])
    too_hot = state in ("hot", "critical")

    fan_warning = None
    if stopped:
        fan_warning = ("This box's fan is not spinning"
                       + (", which is why it is running hot."
                          if too_hot else
                          ". Nothing is overheating yet, but a stopped fan "
                          "does not usually start again on its own."))

    return {
        "temperatures": readings,
        "hottest": warmest,
        "fans": spinning,
        "fan_warning": fan_warning,
        "throttling": slowdown,
        "disks": drives,
        "state": state,
        "warning": bool(too_hot or stopped or throttled or unhealthy),
        "summary": _overall_sentence(
            state, warmest, fan_warning, throttled, slowdown, unhealthy, readings),
    }


def _overall_sentence(
    state: str,
    warmest: Optional[Dict[str, Any]],
    fan_warning: Optional[str],
    throttled: bool,
    slowdown: Optional[Dict[str, Any]],
    unhealthy: List[Dict[str, Any]],
    readings: List[Dict[str, Any]],
) -> str:
    """One plain answer first. The detail is on the page underneath it."""
    # Order matters: lead with the thing the owner could do something about.
    # A stopped fan is a physical fault they can see and act on, and it is the
    # cause of the heat and the stuttering rather than another symptom of it.
    if fan_warning:
        return fan_warning
    if state == "critical" and warmest:
        return ("This box is too hot: the {} is at {:.0f} °C, past the limit "
                "its manufacturer sets. It will slow down or switch itself "
                "off to protect itself.".format(warmest["name"], warmest["celsius"]))
    if throttled and slowdown:
        return slowdown["summary"]
    if state == "hot" and warmest:
        return ("This box is running hot: the {} is at {:.0f} °C, above the "
                "{:.0f} °C its manufacturer allows. Expect the picture to "
                "stutter until it cools down.".format(
                    warmest["name"], warmest["celsius"],
                    warmest["max_celsius"] if warmest["max_celsius"] is not None
                    else warmest["critical_celsius"]))
    if unhealthy:
        return unhealthy[0]["summary"]
    if state == "warm" and warmest:
        return ("This box is warm but still inside its limits - the {} is at "
                "{:.0f} °C.".format(warmest["name"], warmest["celsius"]))
    if state == "fine":
        return "This box is running at a normal temperature."
    if readings:
        return ("This box's sensors do not publish safe limits, so there is "
                "nothing to compare their readings against.")
    return "This box does not report any temperatures, so there is nothing to check."


__all__ = [
    "BLOCK_ROOT",
    "CPU_ROOT",
    "HWMON_ROOT",
    "SMART_CACHE_SECONDS",
    "SMART_TIMEOUT",
    "SmartCache",
    "THERMAL_ROOT",
    "disks",
    "fans",
    "hottest",
    "report",
    "temperatures",
    "throttling",
]
