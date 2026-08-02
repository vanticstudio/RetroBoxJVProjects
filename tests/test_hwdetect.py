"""Picking the right driver, and being honest about whether it worked.

Two failures on the bench box drove all of this:

* The System page said "hardware decode: not active" while the television was
  demonstrably decoding with VA-API. The probe was run from the dashboard,
  which had no ``render`` group, so ``vainfo`` could not open the GPU at all.
  Its refusal was collapsed into a plain False and stated as fact.
* "no HDMI audio found", from the same process, for the same reason.

So a probe that could not look now says *that*, rather than answering the
question it never got to ask.

Nothing here runs apt, opens a device, or touches real hardware.
"""

import pytest

from retrobox import hwdetect


# Real lspci lines. The bench box first.
GEMINILAKE = ("00:02.0 VGA compatible controller [0300]: Intel Corporation "
              "GeminiLake [UHD Graphics 600] [8086:3185] (rev 03)")
IVYBRIDGE = ("00:02.0 VGA compatible controller [0300]: Intel Corporation "
             "3rd Gen Core processor Graphics Controller [8086:0166] (rev 09)")
SANDYBRIDGE = ("00:02.0 VGA compatible controller [0300]: Intel Corporation "
               "2nd Generation Core Processor Family Integrated Graphics "
               "Controller [8086:0126] (rev 09)")
ALDERLAKE = ("00:02.0 VGA compatible controller [0300]: Intel Corporation "
             "Alder Lake-N [UHD Graphics] [8086:46d0]")
AMD = ("07:00.0 VGA compatible controller [0300]: Advanced Micro Devices, "
       "Inc. [AMD/ATI] Raphael [1002:164e] (rev c1)")
NVIDIA = ("01:00.0 VGA compatible controller [0300]: NVIDIA Corporation "
          "GK208B [GeForce GT 710] [10de:128b] (rev a1)")


# ==========================================================================
# The driver is chosen by GENERATION, not by the word "Intel"
# ==========================================================================
def test_gen9_intel_gets_the_modern_media_driver():
    """UHD Graphics 600 is Gen9 LP. i965 does not drive it: it installs
    cleanly, reports success, and leaves the box on software decode."""
    vendor, _, packages = hwdetect._parse_gpu(GEMINILAKE)
    assert vendor == "intel"
    assert any("intel-media" in p for p in packages)
    assert not any(p.startswith("i965") for p in packages), \
        "the wrong-generation driver must not be installed alongside"


def test_a_modern_intel_gets_the_modern_driver_too():
    _, _, packages = hwdetect._parse_gpu(ALDERLAKE)
    assert any("intel-media" in p for p in packages)


@pytest.mark.parametrize("line", [IVYBRIDGE, SANDYBRIDGE])
def test_pre_broadwell_intel_gets_i965(line):
    """The old driver is right for old silicon, and iHD will not drive it."""
    vendor, _, packages = hwdetect._parse_gpu(line)
    assert vendor == "intel"
    assert packages == ["i965-va-driver"]


def test_an_unknown_intel_id_gets_the_modern_driver():
    """Anything a secondhand mini PC ships today is Broadwell or newer, so
    that is the better guess for an id this table has never seen."""
    unknown = ("00:02.0 VGA compatible controller [0300]: Intel Corporation "
               "Something New [8086:ffff]")
    _, _, packages = hwdetect._parse_gpu(unknown)
    assert any("intel-media" in p for p in packages)


def test_amd_gets_mesa():
    vendor, _, packages = hwdetect._parse_gpu(AMD)
    assert vendor == "amd"
    assert packages == ["mesa-va-drivers"]


def test_nvidia_is_left_on_software_decode_deliberately():
    vendor, _, packages = hwdetect._parse_gpu(NVIDIA)
    assert vendor == "nvidia"
    assert packages == [], "the proprietary stack can break a headless box"


def test_no_gpu_line_at_all_is_not_a_crash():
    vendor, description, packages = hwdetect._parse_gpu("")
    assert vendor == "unknown"
    assert packages == []
    assert description


# ==========================================================================
# "I was refused" is not the same answer as "it does not work"
# ==========================================================================
def _vainfo(monkeypatch, *, stdout="", stderr="", code=0, present=True):
    monkeypatch.setattr(hwdetect.shutil, "which",
                        lambda name: "/usr/bin/vainfo" if present else None)
    monkeypatch.setattr(hwdetect, "_run",
                        lambda cmd, **k: hwdetect.Ran(stdout, stderr, code))


REAL_VAINFO = """\
vainfo: VA-API version: 1.23 (libva 2.22.0)
vainfo: Driver version: Intel iHD driver for Intel(R) Gen Graphics - 26.1.2 ()
vainfo: Supported profile and entrypoints
      VAProfileH264Main               : VAEntrypointVLD
      VAProfileHEVCMain10             : VAEntrypointVLD
      VAProfileVP9Profile0            : VAEntrypointVLD
"""

# What the dashboard actually got, and stated as "software decode is used".
REFUSED = """\
error: can't connect to X server!
error: failed to initialize display
Trying display: wayland
Trying display: x11
Trying display: drm
"""


def test_a_working_gpu_reports_its_profiles(monkeypatch):
    _vainfo(monkeypatch, stdout=REAL_VAINFO)
    result = hwdetect.vaapi_report()
    assert result.working is True
    assert "VAProfileHEVCMain10" in result.profiles
    assert len(result.profiles) == 3


def test_being_refused_the_gpu_is_could_not_tell_not_no(monkeypatch):
    """The bug, in one assertion. The dashboard could not open the render
    node; that is not evidence that the television is decoding in software."""
    _vainfo(monkeypatch, stdout="", stderr=REFUSED, code=1)
    result = hwdetect.vaapi_report()
    assert result.working is None, "could not look is not the same as no"
    assert "permission" in result.reason.lower() or "open" in result.reason.lower()


def test_a_missing_vainfo_is_could_not_tell_too(monkeypatch):
    _vainfo(monkeypatch, present=False)
    result = hwdetect.vaapi_report()
    assert result.working is None
    assert "vainfo" in result.reason


def test_a_driver_that_loads_but_offers_nothing_is_a_real_no(monkeypatch):
    """This is the verification bar: the driver installed, vainfo ran, and
    the GPU offers no decode entrypoint. That IS a failure, and the installer
    claiming success over it is exactly what must stop."""
    empty = ("vainfo: VA-API version: 1.23\n"
             "vainfo: Supported profile and entrypoints\n")
    _vainfo(monkeypatch, stdout=empty, code=0)
    result = hwdetect.vaapi_report()
    assert result.working is False
    assert result.profiles == []


def test_only_decode_entrypoints_count(monkeypatch):
    """A GPU that can only ENCODE is not doing hardware decode."""
    encode_only = ("vainfo: Supported profile and entrypoints\n"
                   "      VAProfileH264Main    : VAEntrypointEncSlice\n")
    _vainfo(monkeypatch, stdout=encode_only)
    result = hwdetect.vaapi_report()
    assert result.working is False


# ==========================================================================
# Sound
# ==========================================================================
APLAY_L = """\
default:CARD=PCH
    HDA Intel PCH, ALC233 Analog
sysdefault:CARD=PCH
    HDA Intel PCH, ALC233 Analog
hdmi:CARD=PCH,DEV=0
    HDA Intel PCH, HDMI 0
hdmi:CARD=PCH,DEV=1
    HDA Intel PCH, HDMI 1
plughw:CARD=PCH,DEV=3
    HDA Intel PCH, HDMI 0
"""

# A SOF-driven box, where nothing is called "hdmi:" at all. The old filter
# found nothing here and reported a box with working HDMI as having none.
APLAY_L_SOF = """\
default:CARD=sofhdadsp
    sof-hda-dsp,
hw:CARD=sofhdadsp,DEV=3
    sof-hda-dsp, HDMI0 (*)
hw:CARD=sofhdadsp,DEV=4
    sof-hda-dsp, HDMI1 (*)
"""


def test_hdmi_outputs_are_found_by_name():
    found = hwdetect._parse_audio(APLAY_L)
    assert "alsa/hdmi:CARD=PCH,DEV=0" in found
    assert "alsa/hdmi:CARD=PCH,DEV=1" in found


def test_the_analog_jack_is_not_offered_as_an_hdmi_output():
    found = hwdetect._parse_audio(APLAY_L)
    assert not any("default:CARD=PCH" in f for f in found)


def test_one_entry_per_socket_not_one_per_alsa_alias():
    """hw:, plughw: and dmix: are three more names for the device `hdmi:`
    already names. Listing them all told a customer with three HDMI sockets
    that their box had twelve HDMI outputs."""
    found = hwdetect._parse_audio(APLAY_L)
    assert len(found) == 2, found
    assert not any("plughw:" in f or "dmix:" in f for f in found)


def test_a_sof_card_that_never_says_hdmi_is_still_found():
    """GeminiLake-class hardware is routinely claimed by SOF, which names
    its outputs HDMI0/HDMI1 in the DESCRIPTION and never as an `hdmi:` PCM."""
    found = hwdetect._parse_audio(APLAY_L_SOF)
    assert found, "a box with working HDMI must not be reported as having none"
    assert any("sofhdadsp" in f for f in found)


def test_no_sound_card_at_all_is_reported_not_swallowed():
    """`aplay -l` prints this when the card is missing AND when the caller
    was not allowed to look. Either way it is not 'no HDMI audio'."""
    assert hwdetect._parse_audio("") == []


def test_a_refusal_to_enumerate_is_distinguished_from_having_no_card(monkeypatch):
    monkeypatch.setattr(hwdetect.shutil, "which", lambda name: "/usr/bin/aplay")
    monkeypatch.setattr(hwdetect, "_run", lambda cmd, **k: hwdetect.Ran(
        "", "aplay: device_list:279: no soundcards found...", 1))
    report = hwdetect.audio_report()
    assert report.devices == []
    assert report.working is None, "could not look is not the same as none"


# ==========================================================================
# Install, then CONFIRM
# ==========================================================================
def test_install_failing_is_carried_not_discarded(monkeypatch):
    """build_report used to call install_packages and throw the answer away,
    so an apt failure was indistinguishable from a success."""
    monkeypatch.setattr(hwdetect, "detect_gpu",
                        lambda: ("intel", "Intel", ["intel-media-va-driver-non-free"]))
    monkeypatch.setattr(hwdetect, "audio_report",
                        lambda: hwdetect.AudioReport([], None, "none"))
    monkeypatch.setattr(hwdetect, "install_packages", lambda pkgs: False)
    monkeypatch.setattr(hwdetect, "vaapi_report",
                        lambda: hwdetect.VaapiReport(None, [], "no vainfo"))

    report = hwdetect.build_report(run_install=True)
    assert report.install_ok is False
    assert report.decode_working is not True


def test_a_driver_that_installs_but_does_not_work_is_reported_as_such(monkeypatch):
    """An installer that says it set up hardware acceleration and did not is
    worse than one that says it could not: the second one gets fixed."""
    monkeypatch.setattr(hwdetect, "detect_gpu",
                        lambda: ("intel", "Intel", ["intel-media-va-driver-non-free"]))
    monkeypatch.setattr(hwdetect, "audio_report",
                        lambda: hwdetect.AudioReport([], None, "none"))
    monkeypatch.setattr(hwdetect, "install_packages", lambda pkgs: True)
    monkeypatch.setattr(hwdetect, "vaapi_report",
                        lambda: hwdetect.VaapiReport(False, [], "no decode profiles"))

    report = hwdetect.build_report(run_install=True)
    assert report.install_ok is True
    assert report.decode_working is False
    assert "software decode" in report.decode_summary.lower()


def test_the_report_never_raises_on_a_machine_with_none_of_the_tools(monkeypatch):
    monkeypatch.setattr(hwdetect.shutil, "which", lambda name: None)
    report = hwdetect.build_report(run_install=False)
    assert report.gpu_vendor == "unknown"
    assert report.decode_working is None
    assert report.audio_devices == []


def test_missing_firmware_is_named_so_it_can_be_fixed(monkeypatch):
    """"No sound card" on Intel is often a missing firmware package, and a
    message that does not name it leaves the owner with nowhere to go."""
    monkeypatch.setattr(hwdetect, "_sound_cards", lambda: [])
    advice = hwdetect.audio_advice(vendor="intel", cards=[])
    assert "firmware-sof-signed" in advice
