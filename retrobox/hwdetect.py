"""
Hardware detection for Retro Box on generic x86 mini PCs.

Detects the GPU vendor to pick the right VA-API hardware decode driver,
and finds an HDMI audio output automatically. Written standalone with no
dependency on other retrobox internals, so it drops into the project
regardless of how config.py or app.py are structured.

Usage:
    python3 -m retrobox.hwdetect              # print a report only
    python3 -m retrobox.hwdetect --install    # also apt install the right driver packages
"""

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class HardwareReport:
    gpu_vendor: str
    gpu_description: str
    decode_packages: List[str]
    audio_devices: List[str] = field(default_factory=list)
    recommended_audio_device: Optional[str] = None


def _run(cmd) -> str:
    """Run a command, return stdout as text, empty string on any failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.stdout or ""
    except Exception:
        return ""


def _parse_gpu(lspci_output: str) -> Tuple[str, str, List[str]]:
    """Pure parsing logic, kept separate from the subprocess call so it can
    be tested with sample lspci text instead of needing real hardware."""
    gpu_lines = [
        line for line in lspci_output.splitlines()
        if re.search(r"VGA compatible controller|3D controller|Display controller", line, re.I)
    ]
    if not gpu_lines:
        return "unknown", "no display controller found in lspci output", []

    description = gpu_lines[0].strip()

    if re.search(r"\bintel\b", description, re.I):
        # i965 covers older generations before Broadwell, the non-free iHD
        # driver covers Broadwell onward. Installing both is the standard
        # way to cover an unknown mix of old Intel chips without needing to
        # identify the exact generation first.
        return "intel", description, ["i965-va-driver", "intel-media-va-driver-non-free"]

    if re.search(r"\b(amd|ati)\b", description, re.I):
        # Word boundaries matter here, a loose "ati" substring match will
        # false positive on "Corporation" appearing in an Nvidia or Intel
        # vendor string.
        return "amd", description, ["mesa-va-drivers"]

    if re.search(r"\bnvidia\b", description, re.I):
        # Deliberately not installing proprietary Nvidia drivers, that's a
        # much bigger footprint and can break a headless box if it goes
        # wrong. Software decode is the safe default here.
        return "nvidia", description, []

    return "unknown", description, []


def _parse_audio(aplay_L_output: str) -> List[str]:
    """aplay -L already gives the exact usable device string, no need to
    reconstruct it from the human friendly aplay -l listing."""
    return [
        f"alsa/{line.strip()}"
        for line in aplay_L_output.splitlines()
        if line.strip().lower().startswith("hdmi:")
    ]


def detect_gpu() -> Tuple[str, str, List[str]]:
    if not shutil.which("lspci"):
        return "unknown", "lspci not available, install pciutils", []
    return _parse_gpu(_run(["lspci", "-nn"]))


def detect_audio() -> List[str]:
    if not shutil.which("aplay"):
        return []
    return _parse_audio(_run(["aplay", "-L"]))


def check_vaapi() -> bool:
    """After installing a decode driver, confirm it actually works."""
    if not shutil.which("vainfo"):
        return False
    return "VAProfile" in _run(["vainfo"])


def install_packages(packages: List[str]) -> bool:
    if not packages:
        return True
    result = subprocess.run(["sudo", "apt", "install", "-y", *packages])
    return result.returncode == 0


def build_report(run_install: bool = False) -> HardwareReport:
    vendor, description, packages = detect_gpu()
    audio_devices = detect_audio()

    if run_install and packages:
        install_packages(packages)

    return HardwareReport(
        gpu_vendor=vendor,
        gpu_description=description,
        decode_packages=packages,
        audio_devices=audio_devices,
        recommended_audio_device=audio_devices[0] if audio_devices else None,
    )


def main():
    run_install = "--install" in sys.argv
    report = build_report(run_install=run_install)

    print(f"GPU: {report.gpu_vendor} ({report.gpu_description})")
    if report.decode_packages:
        print(f"Decode packages: {', '.join(report.decode_packages)}")
        if run_install:
            ok = check_vaapi()
            status = "passed" if ok else "no VAProfile lines found, software decode will still work fine"
            print(f"vainfo check: {status}")
    else:
        print("No hardware decode driver for this GPU, software decode will be used")

    if report.audio_devices:
        print(f"HDMI audio device(s) found: {', '.join(report.audio_devices)}")
        print(f"Recommended: {report.recommended_audio_device}")
    else:
        print("No HDMI audio device found by aplay -L, check the cable or set audio_device manually")


if __name__ == "__main__":
    main()
