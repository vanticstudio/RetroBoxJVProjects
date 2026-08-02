"""Choosing an output, so a television is never silent by default.

The bench box shipped with no ``audio_device`` in config.yaml at all. mpv was
therefore left on ``--audio-device=auto``, which resolves to ALSA ``default``,
which on an Intel box is the ALC233 **analog** codec - the 3.5 mm headphone
jack. The box had been playing perfectly into a socket with nothing plugged
into it, and every layer above reported success.

So the television picks an output itself, every time it starts, and says
which one and why.
"""

from retrobox import audioout, eld
from tests.test_eld import (NOTHING_ATTACHED, SURROUND_TELEVISION,
                            TELEVISION_ATTACHED)


def _socket(text, *, index=0, device=3):
    return eld.HdmiOutput(card_index=0, card_id="PCH", hdmi_index=index,
                          alsa_device=device, eld=eld.parse_eld(text))


HDMI_0_EMPTY = _socket(NOTHING_ATTACHED, index=0, device=3)
HDMI_1_TV = _socket(TELEVISION_ATTACHED, index=1, device=7)
HDMI_2_SURROUND = _socket(SURROUND_TELEVISION, index=2, device=8)

# What mpv itself can see on the bench box, in mpv's own words.
MPV_DEVICES = [
    {"name": "auto", "description": "Autoselect device"},
    {"name": "alsa/default:CARD=PCH", "description": "ALC233 Analog/Default Audio Device"},
    {"name": "alsa/hdmi:CARD=PCH,DEV=0", "description": "HDMI 0/HDMI Audio Output"},
    {"name": "alsa/hdmi:CARD=PCH,DEV=1", "description": "HDMI 1/HDMI Audio Output"},
]


# ==========================================================================
# What the customer asked for always wins
# ==========================================================================
def test_a_configured_device_is_never_second_guessed():
    """Detection exists to save people configuring it, not to overrule them.
    The rare box that needs a hand-written name must keep working."""
    decision = audioout.decide("alsa/plughw:CARD=PCH,DEV=7",
                               sockets=[HDMI_0_EMPTY], player_devices=[])
    assert decision.device == "alsa/plughw:CARD=PCH,DEV=7"
    assert decision.source == "configured"
    assert decision.detected is False


def test_a_configured_device_still_gets_a_sensible_layout():
    decision = audioout.decide("alsa/hdmi:CARD=PCH,DEV=1",
                               sockets=[HDMI_1_TV], player_devices=[])
    assert decision.layout == "stereo", "the set on that port only takes stereo"


# ==========================================================================
# Nobody configured anything: read the answer off the hardware
# ==========================================================================
def test_the_socket_with_the_television_is_chosen():
    decision = audioout.decide(None, sockets=[HDMI_0_EMPTY, HDMI_1_TV],
                               player_devices=MPV_DEVICES)
    assert decision.device == "alsa/hdmi:CARD=PCH,DEV=1"
    assert decision.source == "eld"
    assert decision.detected is True
    assert "SAMSUNG" in decision.summary


def test_a_surround_set_is_allowed_its_surround():
    decision = audioout.decide(None, sockets=[HDMI_2_SURROUND],
                               player_devices=MPV_DEVICES)
    assert decision.layout == "5.1"


def test_a_stereo_set_gets_a_downmix_rather_than_silence():
    """The file that surfaced this was 5.1 AAC. Sent whole to a stereo sink
    it plays silently, with no error raised at any layer."""
    decision = audioout.decide(None, sockets=[HDMI_1_TV],
                               player_devices=MPV_DEVICES)
    assert decision.layout == "stereo"


# ==========================================================================
# Nothing attached: say so, do not guess, and keep the picture
# ==========================================================================
def test_no_display_advertising_audio_is_reported_not_invented():
    """The bench box today. Picking HDMI 0 and hoping is how a box ships
    silent; the honest answer is that no display is advertising audio."""
    decision = audioout.decide(None, sockets=[HDMI_0_EMPTY],
                               player_devices=MPV_DEVICES)
    assert decision.device is None
    assert decision.source == "none"
    assert decision.detected is False
    assert "no display" in decision.summary.lower()
    assert decision.fatal is False, "a box with no sound still plays pictures"


def test_a_box_with_no_audio_hardware_at_all_still_starts():
    decision = audioout.decide(None, sockets=[], player_devices=[])
    assert decision.device is None
    assert decision.fatal is False
    assert decision.summary


def test_the_analog_jack_is_never_chosen_on_its_own():
    """`default:CARD=PCH` is the analog jack and is what auto resolves to.
    Choosing it deliberately would just re-create the original fault."""
    decision = audioout.decide(None, sockets=[], player_devices=MPV_DEVICES)
    assert decision.device != "alsa/default:CARD=PCH"


# ==========================================================================
# The box built on a bench and plugged in later
# ==========================================================================
def test_a_socket_that_filled_up_since_install_is_found_on_the_next_start():
    """This is the whole scenario: built headless because ethernet is
    faster, moved to the living room afterwards. Nobody should have to
    intervene for it to find its sound on that boot."""
    at_install = audioout.decide(None, sockets=[HDMI_0_EMPTY, _socket(
        NOTHING_ATTACHED, index=1, device=7)], player_devices=MPV_DEVICES)
    assert at_install.device is None

    later = audioout.decide(None, sockets=[HDMI_0_EMPTY, HDMI_1_TV],
                            player_devices=MPV_DEVICES)
    assert later.device == "alsa/hdmi:CARD=PCH,DEV=1"


# ==========================================================================
# When ELD is unavailable, the player's own list is better than nothing
# ==========================================================================
def test_the_players_own_list_is_used_when_there_are_no_elds():
    """Some cards expose no ELD at all. mpv still knows what it can open,
    and it is asked as the service that actually holds the audio group."""
    decision = audioout.decide(None, sockets=[], player_devices=MPV_DEVICES)
    assert decision.device == "alsa/hdmi:CARD=PCH,DEV=0"
    assert decision.source == "player"


def test_the_players_list_is_only_a_fallback_not_a_veto():
    """ELD said HDMI 1. mpv lists HDMI 0 first. ELD wins - it is the one
    that knows which socket has a television in it."""
    decision = audioout.decide(None, sockets=[HDMI_0_EMPTY, HDMI_1_TV],
                               player_devices=MPV_DEVICES)
    assert decision.device == "alsa/hdmi:CARD=PCH,DEV=1"


def test_a_device_the_player_cannot_see_is_still_chosen_from_eld():
    """The ELD is the kernel's own answer. mpv's list can lag it, and the
    kernel is not overruled by a stale enumeration."""
    decision = audioout.decide(None, sockets=[HDMI_2_SURROUND],
                               player_devices=MPV_DEVICES)
    assert decision.device == "alsa/hdmi:CARD=PCH,DEV=2"
