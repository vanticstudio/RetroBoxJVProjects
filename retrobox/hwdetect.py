"""Hardware detection for Retro Box on generic x86 mini PCs.

Picks the VA-API decode driver that matches the GPU's *generation*, finds the
HDMI audio outputs, and - this is the part that was missing - **confirms the
result instead of assuming it**.

Two rules are load-bearing here, both bought with a real fault on a real box:

**"I could not look" is not "no".**  The dashboard used to run ``vainfo`` from
a process with no ``render`` group. libva could not open /dev/dri/renderD128,
printed its refusal to *stderr*, and exited non-zero. The old ``_run``
returned stdout only and discarded both, so ``"VAProfile" in ""`` came out
False, and the System page told the customer their box was decoding in
software while the television beside it was decoding with VA-API. Every probe
here is therefore three-valued: yes, no, or **could not tell**.

**An installer that claims success it did not verify is worse than one that
admits failure**, because the second one gets fixed and the first one ships.
So ``--install`` installs and then checks, and reports honestly either way.

Nothing here is ever fatal. A box on software decode with no sound is still a
box, and some hardware genuinely cannot do better.

Usage:
    python3 -m retrobox.hwdetect              # print a report only
    python3 -m retrobox.hwdetect --install    # also install the right drivers
"""

import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

log = logging.getLogger(__name__)

#: The Intel media driver (iHD) covers Broadwell (Gen8) onward. The old i965
#: driver covers what came before. Installing the wrong one is not a loud
#: failure - it installs cleanly, reports success, and silently leaves the box
#: on software decode, which is exactly what happened on the bench box's
#: GeminiLake. So the choice is made by PCI device id, not by the word "Intel".
INTEL_MODERN = "intel-media-va-driver-non-free"
INTEL_LEGACY = "i965-va-driver"

#: Known pre-Broadwell Intel graphics device-id ranges. Anything outside these
#: gets the modern driver: a secondhand mini PC bought today is Broadwell or
#: newer, so that is the better guess for an id this table has never seen.
_INTEL_LEGACY_IDS = (
    (0x2500, 0x2FFF),   # i915/945/G33/G45 and friends
    (0x0040, 0x004F),   # Ironlake
    (0x0100, 0x0130),   # Sandy Bridge      (Gen6)
    (0x0150, 0x0170),   # Ivy Bridge        (Gen7)
    (0x0400, 0x0430),   # Haswell           (Gen7.5)
    (0x0A00, 0x0A40),   # Haswell ULT
    (0x0D00, 0x0D40),   # Haswell GT3
    (0x0F30, 0x0F40),   # Bay Trail
    (0x2200, 0x22C0),   # Cherry Trail / Braswell
)

#: Intel HDA/SOF audio needs this on modern Ubuntu. A box with no sound card
#: at all is usually this package missing, and a message that does not name it
#: leaves the owner with a problem and nowhere to go.
SOF_FIRMWARE = "firmware-sof-signed"


class Ran(NamedTuple):
    """A finished command, including the parts that used to be thrown away."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def said(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


@dataclass(frozen=True)
class VaapiReport:
    """Whether the GPU is actually decoding, and how we know."""

    working: Optional[bool]        # True / False / None = could not tell
    profiles: List[str]
    reason: str


@dataclass(frozen=True)
class AudioReport:
    """What can be played out of, and whether we were allowed to look."""

    devices: List[str]
    working: Optional[bool]        # True / False / None = could not tell
    reason: str


@dataclass
class HardwareReport:
    gpu_vendor: str
    gpu_description: str
    decode_packages: List[str]
    audio_devices: List[str] = field(default_factory=list)
    recommended_audio_device: Optional[str] = None
    # Everything below is new. The old report could only say what it had
    # found, never whether any of it worked.
    decode_working: Optional[bool] = None
    decode_profiles: List[str] = field(default_factory=list)
    decode_summary: str = ""
    audio_working: Optional[bool] = None
    audio_summary: str = ""
    audio_advice: str = ""
    install_ok: Optional[bool] = None

    @property
    def degraded(self) -> bool:
        """True when this box will not do everything it could.

        Deliberately not an error: it is the thing install.sh has to be able
        to SAY, not the thing that makes it fail.
        """
        return self.decode_working is not True or self.audio_working is not True


def _run(cmd, *, timeout: int = 15) -> Ran:
    """Run a command and keep everything it said.

    The old version returned ``result.stdout or ""`` and discarded the exit
    code and stderr, which is how a permission refusal became a confident
    statement about hardware.
    """
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return Ran("", f"{cmd[0]}: not installed", 127)
    except Exception as exc:  # noqa: BLE001
        return Ran("", str(exc), 1)
    return Ran(done.stdout or "", done.stderr or "", done.returncode)


# ==========================================================================
# Graphics
# ==========================================================================
def _pci_id(description: str) -> Optional[Tuple[int, int]]:
    """``[8086:3185]`` -> ``(0x8086, 0x3185)``."""
    match = re.search(r"\[([0-9a-f]{4}):([0-9a-f]{4})\]", description, re.I)
    if not match:
        return None
    try:
        return int(match.group(1), 16), int(match.group(2), 16)
    except ValueError:
        return None


def _intel_packages(description: str) -> List[str]:
    """Which Intel VA-API driver actually drives this chip."""
    ids = _pci_id(description)
    if ids is None:
        return [INTEL_MODERN]
    _, device = ids
    for low, high in _INTEL_LEGACY_IDS:
        if low <= device <= high:
            return [INTEL_LEGACY]
    return [INTEL_MODERN]


def _parse_gpu(lspci_output: str) -> Tuple[str, str, List[str]]:
    """Pure parsing, kept away from the subprocess so it can be tested with
    sample lspci text instead of needing real hardware."""
    gpu_lines = [
        line for line in (lspci_output or "").splitlines()
        if re.search(r"VGA compatible controller|3D controller|Display controller",
                     line, re.I)
    ]
    if not gpu_lines:
        return "unknown", "no display controller found in lspci output", []

    description = gpu_lines[0].strip()

    if re.search(r"\bintel\b", description, re.I):
        return "intel", description, _intel_packages(description)

    if re.search(r"\b(amd|ati)\b", description, re.I):
        # Word boundaries matter: a loose "ati" match false-positives on
        # "Corporation" in an Nvidia or Intel vendor string.
        return "amd", description, ["mesa-va-drivers"]

    if re.search(r"\bnvidia\b", description, re.I):
        # Deliberately not installing the proprietary stack: a much bigger
        # footprint that can break a headless box if it goes wrong.
        return "nvidia", description, []

    return "unknown", description, []


def detect_gpu() -> Tuple[str, str, List[str]]:
    if not shutil.which("lspci"):
        return "unknown", "lspci not available, install pciutils", []
    return _parse_gpu(_run(["lspci", "-nn"]).stdout)


def _parse_vainfo(text: str) -> List[str]:
    """The profiles this GPU will actually DECODE.

    Encode-only entrypoints are excluded on purpose: a chip that can encode
    H.264 and not decode it is not doing hardware decode, and counting it
    would put the old bug back with extra steps.
    """
    profiles = []
    for line in (text or "").splitlines():
        if "VAEntrypointVLD" not in line:
            continue
        match = re.search(r"(VAProfile\w+)", line)
        if match and match.group(1) != "VAProfileNone":
            profiles.append(match.group(1))
    return profiles


def vaapi_report() -> VaapiReport:
    """Is the GPU decoding - yes, no, or could-not-tell.

    ``None`` is the important one. It is what a caller that could not open
    the render node gets, and it is emphatically not "software decode".
    """
    if not shutil.which("vainfo"):
        return VaapiReport(None, [], "vainfo is not installed, so nothing asked the GPU")

    ran = _run(["vainfo"])
    profiles = _parse_vainfo(ran.stdout)
    if profiles:
        return VaapiReport(True, profiles,
                           f"{len(profiles)} decode profiles reported")

    said = ran.said.lower()
    refused = (
        "permission denied" in said
        or "failed to initialize display" in said
        or "can't connect to x server" in said
        or "no such file or directory" in said
        or "operation not permitted" in said
    )
    if refused or (not ran.ok and not ran.stdout.strip()):
        # This is the bench box's exact failure. The answer is "could not
        # look", and the caller must not turn it into a verdict.
        return VaapiReport(
            None, [],
            "could not open the graphics device to ask - usually a missing "
            "'render' or 'video' group on the process doing the asking")
    return VaapiReport(False, [], "the driver loaded but offers no decode profiles")


def check_vaapi() -> Optional[bool]:
    """Back-compatible shorthand. Prefer :func:`vaapi_report`."""
    return vaapi_report().working


# ==========================================================================
# Sound
# ==========================================================================
def _parse_audio(aplay_L_output: str) -> List[str]:
    """Every HDMI output ``aplay -L`` describes, however the card names it.

    The old version kept only lines starting ``hdmi:``. That is an alsa-lib
    alias emitted for ``HDA-Intel`` cards; a SOF-driven box - which is most of
    this chipset generation on a current kernel - calls the same sockets
    ``hw:CARD=sofhdadsp,DEV=3`` and describes them as ``HDMI0``. Those boxes
    have perfectly good HDMI audio and were reported as having none.
    """
    canonical: List[str] = []      # named "hdmi:" - one entry per socket
    hardware: List[str] = []       # "hw:" on a card that never says "hdmi:"
    other: List[str] = []
    name: Optional[str] = None

    for raw in (aplay_L_output or "").splitlines():
        if not raw.strip():
            continue
        if not raw[0].isspace():
            name = raw.strip()
            if name.lower().startswith("hdmi:"):
                canonical.append(f"alsa/{name}")
            continue
        # An indented line describes the name above it. SOF puts the only
        # occurrence of "HDMI" there rather than in the PCM name.
        if not name or not re.search(r"\bhdmi\d*\b", raw, re.I):
            continue
        lowered = name.lower()
        if lowered.startswith(("default:", "sysdefault:", "null", "hdmi:")):
            continue
        if lowered.startswith("hw:"):
            hardware.append(f"alsa/{name}")
        elif not lowered.startswith(("plughw:", "dmix:", "dsnoop:")):
            other.append(f"alsa/{name}")

    # One entry per socket, not one per alsa-lib alias of the same socket.
    # hw:/plughw:/dmix: are three names for the device "hdmi:" already names,
    # and listing all of them told a customer with three sockets that their
    # box had twelve HDMI outputs.
    for group in (canonical, hardware, other):
        if group:
            return list(dict.fromkeys(group))
    return []


def _sound_cards() -> List[str]:
    """The cards the kernel registered, straight from /proc."""
    try:
        text = Path("/proc/asound/cards").read_text()
    except OSError:
        return []
    return [line.strip() for line in text.splitlines()
            if re.match(r"\s*\d+\s*\[", line)]


def audio_advice(*, vendor: str, cards: List[str]) -> str:
    """What to actually do about having no sound, naming the package."""
    if cards:
        return ""
    if vendor == "intel":
        return (f"No sound card is registered at all. On Intel boxes this is "
                f"usually the missing '{SOF_FIRMWARE}' package.")
    return ("No sound card is registered at all - this box may have no audio "
            "hardware, or its driver did not load.")


def audio_report() -> AudioReport:
    """What can be played out of, and whether we were allowed to look."""
    if not shutil.which("aplay"):
        return AudioReport([], None,
                           "aplay is not installed, so nothing enumerated the outputs")

    ran = _run(["aplay", "-L"])
    devices = _parse_audio(ran.stdout)
    if devices:
        return AudioReport(devices, True, f"{len(devices)} HDMI output(s) found")

    said = ran.said.lower()
    if "no soundcards found" in said or "permission denied" in said or not ran.ok:
        # Exactly what the dashboard got: aplay cannot open /dev/snd/controlC0
        # (root:audio 0660) without the audio group, and says "no soundcards
        # found" - which is indistinguishable from a box that has none.
        return AudioReport(
            [], None,
            "could not enumerate the sound devices - usually a missing "
            "'audio' group on the process doing the asking")
    return AudioReport([], False, "no HDMI outputs on this box")


def detect_audio() -> List[str]:
    """Back-compatible shorthand. Prefer :func:`audio_report`."""
    return audio_report().devices


# ==========================================================================
# Installing
# ==========================================================================
def install_packages(packages: List[str]) -> bool:
    if not packages:
        return True
    try:
        result = subprocess.run(["sudo", "-n", "apt-get", "install", "-y", *packages])
    except Exception:  # noqa: BLE001
        log.warning("could not run apt to install %s", ", ".join(packages),
                    exc_info=True)
        return False
    return result.returncode == 0


def build_report(run_install: bool = False) -> HardwareReport:
    """Detect, optionally install, and then CONFIRM.

    The old version called ``install_packages`` and discarded the answer, so
    an apt failure was indistinguishable from a success, and then never
    re-checked whether the driver it installed actually drove anything.
    """
    vendor, description, packages = detect_gpu()

    install_ok: Optional[bool] = None
    if run_install and packages:
        install_ok = install_packages(packages)
        if not install_ok:
            log.warning("could not install %s", ", ".join(packages))

    decode = vaapi_report()
    sound = audio_report()
    cards = _sound_cards()

    if not packages and vendor != "unknown":
        decode_summary = (f"Hardware decode: not available for {description}, "
                          f"using software decode - which is fine")
        decode_working: Optional[bool] = False
    elif decode.working is True:
        decode_summary = f"Hardware decode: working - {decode.reason}"
        decode_working = True
    elif decode.working is False:
        decode_summary = (f"Hardware decode: not active on {description} - "
                          f"software decode is being used, which is fine on "
                          f"most files ({decode.reason})")
        decode_working = False
    else:
        decode_summary = f"Hardware decode: could not tell - {decode.reason}"
        decode_working = None

    if sound.working is True:
        audio_summary = f"Sound: {len(sound.devices)} HDMI output(s) found"
    elif sound.working is False:
        audio_summary = "Sound: no HDMI audio output on this box"
    else:
        audio_summary = f"Sound: could not tell - {sound.reason}"

    return HardwareReport(
        gpu_vendor=vendor,
        gpu_description=description,
        decode_packages=packages,
        audio_devices=list(sound.devices),
        recommended_audio_device=sound.devices[0] if sound.devices else None,
        decode_working=decode_working,
        decode_profiles=list(decode.profiles),
        decode_summary=decode_summary,
        audio_working=sound.working,
        audio_summary=audio_summary,
        audio_advice=audio_advice(vendor=vendor, cards=cards),
        install_ok=install_ok,
    )


def main():
    run_install = "--install" in sys.argv
    report = build_report(run_install=run_install)

    print(f"GPU: {report.gpu_vendor} ({report.gpu_description})")
    if report.decode_packages:
        print(f"Decode driver: {', '.join(report.decode_packages)}")
        if run_install:
            print(f"  install: {'ok' if report.install_ok else 'FAILED'}")
    print(f"  {report.decode_summary}")
    if report.decode_profiles:
        print(f"  can decode: {', '.join(p[9:] for p in report.decode_profiles)}")

    print(report.audio_summary)
    for device in report.audio_devices:
        print(f"  {device}")
    if report.audio_advice:
        print(f"  {report.audio_advice}")

    # Deliberately always 0. Degraded hardware is not an installation
    # failure - it is something the installer has to be able to SAY.
    return 0


if __name__ == "__main__":
    sys.exit(main())
