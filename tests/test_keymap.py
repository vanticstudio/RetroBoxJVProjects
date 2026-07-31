import pytest

from retrobox.actions import Action
from retrobox.input.keymap import (
    cec_key_to_event,
    evdev_key_to_event,
    parse_key_overrides,
    stdin_char_to_event,
    stdin_escape_to_event,
)


def test_evdev_channel_and_volume():
    assert evdev_key_to_event("KEY_CHANNELUP").action == Action.CHANNEL_UP
    assert evdev_key_to_event("KEY_PAGEUP").action == Action.CHANNEL_UP
    assert evdev_key_to_event("KEY_UP").action == Action.CHANNEL_UP
    assert evdev_key_to_event("KEY_VOLUMEDOWN").action == Action.VOLUME_DOWN
    assert evdev_key_to_event("KEY_MUTE").action == Action.MUTE


def test_evdev_digits():
    ev = evdev_key_to_event("KEY_5")
    assert ev.action == Action.DIGIT and ev.value == 5
    ev = evdev_key_to_event("KEY_KP0")
    assert ev.action == Action.DIGIT and ev.value == 0


def test_evdev_power_and_last_letters():
    assert evdev_key_to_event("KEY_P").action == Action.POWER
    assert evdev_key_to_event("KEY_L").action == Action.LAST_CHANNEL
    assert evdev_key_to_event("KEY_POWER").action == Action.POWER


def test_evdev_unknown_key():
    assert evdev_key_to_event("KEY_FLIBBERTIGIBBET") is None


def test_stdin_chars():
    assert stdin_char_to_event("+").action == Action.VOLUME_UP
    assert stdin_char_to_event("-").action == Action.VOLUME_DOWN
    assert stdin_char_to_event("m").action == Action.MUTE
    assert stdin_char_to_event("q").action == Action.QUIT
    assert stdin_char_to_event("3").value == 3
    assert stdin_char_to_event("\r").action == Action.ENTER


def test_stdin_arrows():
    assert stdin_escape_to_event("[A").action == Action.CHANNEL_UP
    assert stdin_escape_to_event("[B").action == Action.CHANNEL_DOWN
    assert stdin_escape_to_event("[C").action == Action.VOLUME_UP
    assert stdin_escape_to_event("[D").action == Action.VOLUME_DOWN
    assert stdin_escape_to_event("[Z") is None


def test_parse_key_overrides_basic():
    ov = parse_key_overrides({"KEY_F5": "volume_up", "KEY_DOT": "volume_down"})
    assert ov["KEY_F5"].action == Action.VOLUME_UP
    assert ov["KEY_DOT"].action == Action.VOLUME_DOWN


def test_parse_key_overrides_normalises_names():
    # bare names get the KEY_ prefix and are upper-cased
    ov = parse_key_overrides({"f5": "channel_up", "pageup": "channel_down"})
    assert ov["KEY_F5"].action == Action.CHANNEL_UP
    assert ov["KEY_PAGEUP"].action == Action.CHANNEL_DOWN


def test_parse_key_overrides_digit_and_none():
    ov = parse_key_overrides({"KEY_KP5": "digit_5", "KEY_ESC": "none"})
    assert ov["KEY_KP5"].value == 5
    assert ov["KEY_ESC"] is None  # explicitly unbound


def test_parse_key_overrides_rejects_bad_action():
    with pytest.raises(ValueError, match="unknown action"):
        parse_key_overrides({"KEY_F5": "explode"})


def test_parse_key_overrides_empty():
    assert parse_key_overrides(None) == {}
    assert parse_key_overrides({}) == {}


def test_keyboard_backend_override_precedence():
    from retrobox.input.keyboard import KeyboardBackend

    ov = parse_key_overrides({"KEY_F5": "volume_up", "KEY_ESC": "none"})
    kb = KeyboardBackend(overrides=ov)
    assert kb._lookup("KEY_F5").action == Action.VOLUME_UP     # override applied
    assert kb._lookup("KEY_ESC") is None                       # explicitly unbound
    assert kb._lookup("KEY_VOLUMEUP").action == Action.VOLUME_UP  # default intact
    assert kb._lookup("KEY_PAGEUP").action == Action.CHANNEL_UP


def test_cec_keys():
    assert cec_key_to_event("channel up").action == Action.CHANNEL_UP
    assert cec_key_to_event("Volume Down").action == Action.VOLUME_DOWN
    assert cec_key_to_event("select").action == Action.ENTER
    assert cec_key_to_event("number 4").value == 4
    assert cec_key_to_event("power").action == Action.POWER
    assert cec_key_to_event("nonsense") is None


# --------------------------------------------------------------------------
# Guide and sleep buttons
# --------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["KEY_EPG", "KEY_GUIDE", "KEY_PROGRAM", "KEY_PVR", "KEY_G"])
def test_guide_keys(key):
    assert evdev_key_to_event(key).action is Action.GUIDE


@pytest.mark.parametrize("key", ["KEY_SLEEP", "KEY_S"])
def test_sleep_keys(key):
    assert evdev_key_to_event(key).action is Action.SLEEP


def test_sleep_key_is_no_longer_power():
    # A remote's SLEEP button now runs the sleep timer, not standby.
    assert evdev_key_to_event("KEY_SLEEP").action is Action.SLEEP
    assert evdev_key_to_event("KEY_POWER").action is Action.POWER


def test_stdin_guide_and_sleep():
    assert stdin_char_to_event("g").action is Action.GUIDE
    assert stdin_char_to_event("G").action is Action.GUIDE
    assert stdin_char_to_event("s").action is Action.SLEEP
    assert stdin_char_to_event("S").action is Action.SLEEP


def test_cec_guide_button():
    assert cec_key_to_event("electronic program guide").action is Action.GUIDE
    assert cec_key_to_event("guide").action is Action.GUIDE
    # "display information" stays on the plain info banner.
    assert cec_key_to_event("display information").action is Action.INFO


def test_key_overrides_can_bind_the_new_actions():
    mapping = parse_key_overrides({"f1": "guide", "f2": "sleep", "f3": "sleep_timer"})
    assert mapping["KEY_F1"].action is Action.GUIDE
    assert mapping["KEY_F2"].action is Action.SLEEP
    assert mapping["KEY_F3"].action is Action.SLEEP


def test_escape_opens_the_menu_and_no_longer_quits():
    assert evdev_key_to_event("KEY_ESC").action is Action.MENU
    assert stdin_char_to_event("\x1b").action is Action.MENU
    # Quit survives on 'q' only.
    assert evdev_key_to_event("KEY_Q").action is Action.QUIT
    assert stdin_char_to_event("q").action is Action.QUIT
