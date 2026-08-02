"""Which HDMI socket is the television actually plugged into.

An Intel HDMI audio card exposes one playback device per physical port -
commonly 3, 7 and 8 - and only the one carrying the television makes a
sound. Opening the wrong one succeeds, plays silently and reports no error
whatsoever, which is the worst failure a television can have: everything
looks right and nothing comes out.

The kernel already knows the answer. Each port publishes an ELD - the block
of information a display sends back down the HDMI cable describing itself -
and a port with nothing plugged into it says so. So the device is read, not
guessed, and ``config.example.yaml`` no longer asks a customer to try port
numbers until one works.

The fixtures below are the real files from the bench box (Intel GeminiLake,
HDA Intel PCH), captured with nothing attached and with a set attached.
"""

import pytest

from retrobox import eld


# The bench box, headless: nine ELDs, three ports by three MST device ids,
# every one of them reporting no monitor. This is what a box built on a
# bench with no screen looks like, and it must not be mistaken for a fault.
NOTHING_ATTACHED = """\
monitor_present\t\t0
eld_valid\t\t0
codec_pin_nid\t\t0x5
codec_dev_id\t\t0x0
codec_cvt_nid\t\t0x0
"""

# The same file once a television is plugged in and switched on.
TELEVISION_ATTACHED = """\
monitor_present\t\t1
eld_valid\t\t1
codec_pin_nid\t\t0x6
codec_dev_id\t\t0x0
codec_cvt_nid\t\t0x2
monitor_name\t\tSAMSUNG
connection_type\t\tHDMI
eld_version\t\t[0x2] CEA-861D or below
edid_version\t\t[0x3] CEA-861-B, C or D
manufacture_id\t\t0x4d2e
product_id\t\t0x100
port_id\t\t\t0x0
support_hdcp\t\t0
support_ai\t\t0
audio_sync_delay\t0
speakers\t\t[0x1] FL/FR
sad_count\t\t1
sad0_coding_type\t[0x1] LPCM
sad0_channels\t\t2
sad0_rates\t\t[0xe0] 32000 44100 48000
sad0_bits\t\t[0xe0] 16 20 24
"""

# A set that genuinely accepts 5.1, so the downmix decision has something
# to say yes to rather than only ever saying no.
SURROUND_TELEVISION = TELEVISION_ATTACHED.replace(
    "speakers\t\t[0x1] FL/FR",
    "speakers\t\t[0x5f] FL/FR LFE FC RL/RR RLC/RRC",
).replace("sad0_channels\t\t2", "sad0_channels\t\t6")

# /proc/asound/pcm from the same box. This is how an HDMI ordinal becomes
# an ALSA device number, and the numbers are not consecutive.
PCM_LIST = """\
00-00: ALC233 Analog : ALC233 Analog : playback 1 : capture 1
00-01: ALC233 Digital : ALC233 Digital : playback 1
00-03: HDMI 0 : HDMI 0 : playback 1
00-07: HDMI 1 : HDMI 1 : playback 1
00-08: HDMI 2 : HDMI 2 : playback 1
"""


# ==========================================================================
# Reading one ELD
# ==========================================================================
def test_a_port_with_nothing_in_it_says_so():
    reading = eld.parse_eld(NOTHING_ATTACHED)
    assert reading.monitor_present is False
    assert reading.eld_valid is False
    assert reading.usable is False, "no display, so nothing to play into"
    assert reading.monitor_name is None
    assert reading.pin == 0x5
    assert reading.device_id == 0


def test_a_port_with_a_television_in_it_describes_the_television():
    reading = eld.parse_eld(TELEVISION_ATTACHED)
    assert reading.monitor_present is True
    assert reading.eld_valid is True
    assert reading.usable is True
    assert reading.monitor_name == "SAMSUNG"
    assert reading.pin == 0x6
    assert reading.max_channels == 2, "this set only accepts stereo"


def test_a_surround_set_reports_its_channels():
    reading = eld.parse_eld(SURROUND_TELEVISION)
    assert reading.max_channels == 6
    assert reading.usable is True


def test_a_monitor_that_is_present_but_sent_rubbish_is_not_usable():
    """monitor_present without eld_valid is a real state: the cable is in,
    the set answered, and what it said could not be understood. Playing into
    it is a guess, so it is treated as not knowing rather than as yes."""
    text = TELEVISION_ATTACHED.replace("eld_valid\t\t1", "eld_valid\t\t0")
    reading = eld.parse_eld(text)
    assert reading.monitor_present is True
    assert reading.eld_valid is False
    assert reading.usable is False


def test_a_malformed_file_does_not_raise():
    reading = eld.parse_eld("this is not an ELD at all\n\x00\x01garbage")
    assert reading.usable is False
    assert reading.monitor_name is None


def test_an_empty_file_does_not_raise():
    assert eld.parse_eld("").usable is False


# ==========================================================================
# Turning a card full of ELDs into one answer
# ==========================================================================
def _card(tmp_path, files, pcm=PCM_LIST):
    """Build a fake /proc/asound tree. Nothing here opens a real device."""
    root = tmp_path / "asound"
    card = root / "card0"
    card.mkdir(parents=True)
    for name, text in files.items():
        (card / name).write_text(text)
    (root / "pcm").write_text(pcm)
    (root / "cards").write_text(
        " 0 [PCH            ]: HDA-Intel - HDA Intel PCH\n"
        "                      HDA Intel PCH at 0xa1310000 irq 134\n"
    )
    return root


def test_the_port_with_the_television_is_chosen_over_lower_numbered_ones(tmp_path):
    """The set is on the second port. Device 3 comes first and is empty, so
    a box that simply took the lowest number would play into nothing."""
    root = _card(tmp_path, {
        "eld#2.0": NOTHING_ATTACHED,
        "eld#2.1": TELEVISION_ATTACHED,     # pin 0x6 -> HDMI 1 -> device 7
        "eld#2.2": NOTHING_ATTACHED.replace("0x5", "0x7"),
    })
    chosen = eld.choose_output(asound=root)
    assert chosen is not None
    assert chosen.hdmi_index == 1
    assert chosen.alsa_device == 7, "HDMI 1 is device 7, not device 1"
    assert chosen.mpv_name == "alsa/hdmi:CARD=PCH,DEV=1"
    assert chosen.monitor_name == "SAMSUNG"


def test_nothing_attached_is_reported_honestly_not_guessed_at(tmp_path):
    """The bench box, exactly as it is today. The right answer is 'no
    display advertising audio is attached', not a plausible-looking port."""
    root = _card(tmp_path, {
        "eld#2.0": NOTHING_ATTACHED,
        "eld#2.1": NOTHING_ATTACHED.replace("0x5", "0x6"),
        "eld#2.2": NOTHING_ATTACHED.replace("0x5", "0x7"),
    })
    assert eld.choose_output(asound=root) is None

    outputs = eld.hdmi_outputs(asound=root)
    assert len(outputs) == 3, "the ports still exist, they are just empty"
    assert all(not o.usable for o in outputs)


def test_a_card_with_no_eld_files_at_all_is_not_a_crash(tmp_path):
    root = _card(tmp_path, {})
    assert eld.choose_output(asound=root) is None
    assert eld.hdmi_outputs(asound=root) == []


def test_a_missing_asound_tree_is_not_a_crash(tmp_path):
    assert eld.choose_output(asound=tmp_path / "not-here") is None
    assert eld.hdmi_outputs(asound=tmp_path / "not-here") == []


def test_ports_map_to_alsa_devices_through_the_pcm_list(tmp_path):
    """HDMI 0/1/2 are devices 3/7/8 on this chipset. Assuming the ordinal
    IS the device number is the single easiest way to get this wrong."""
    root = _card(tmp_path, {
        "eld#2.0": NOTHING_ATTACHED,
        "eld#2.1": NOTHING_ATTACHED.replace("0x5", "0x6"),
        "eld#2.2": NOTHING_ATTACHED.replace("0x5", "0x7"),
    })
    devices = [o.alsa_device for o in eld.hdmi_outputs(asound=root)]
    assert devices == [3, 7, 8]


def test_mst_device_ids_do_not_invent_extra_ports(tmp_path):
    """The bench box publishes nine ELDs for three sockets - three MST
    stream ids per port. Counting files would report nine televisions."""
    files = {}
    for slot, pin in enumerate((0x5, 0x6, 0x7)):
        for dev_id in range(3):
            text = NOTHING_ATTACHED.replace("0x5", hex(pin)).replace(
                "codec_dev_id\t\t0x0", f"codec_dev_id\t\t{hex(dev_id)}")
            files[f"eld#2.{slot * 3 + dev_id}"] = text
    root = _card(tmp_path, files)
    assert len(eld.hdmi_outputs(asound=root)) == 3


def test_the_first_stream_on_a_port_is_the_one_that_counts(tmp_path):
    """With MST ids present, the port is usable when its primary stream is."""
    files = {
        "eld#2.0": NOTHING_ATTACHED,
        "eld#2.1": NOTHING_ATTACHED.replace("codec_dev_id\t\t0x0",
                                            "codec_dev_id\t\t0x1"),
        "eld#2.2": TELEVISION_ATTACHED,       # pin 0x6, dev 0
    }
    root = _card(tmp_path, files)
    chosen = eld.choose_output(asound=root)
    assert chosen is not None and chosen.hdmi_index == 1


def test_a_pcm_list_that_cannot_be_read_falls_back_to_the_ordinal(tmp_path):
    """Without /proc/asound/pcm there is no device mapping to be had. The
    port is still named for mpv, which addresses it by ordinal anyway."""
    root = _card(tmp_path, {"eld#2.0": TELEVISION_ATTACHED.replace("0x6", "0x5")},
                 pcm="")
    chosen = eld.choose_output(asound=root)
    assert chosen is not None
    assert chosen.alsa_device is None
    assert chosen.mpv_name == "alsa/hdmi:CARD=PCH,DEV=0"


# ==========================================================================
# What the sink will accept
# ==========================================================================
def test_a_stereo_set_asks_for_a_stereo_downmix():
    reading = eld.parse_eld(TELEVISION_ATTACHED)
    assert eld.channel_layout_for(reading) == "stereo"


def test_a_surround_set_is_allowed_its_surround():
    reading = eld.parse_eld(SURROUND_TELEVISION)
    assert eld.channel_layout_for(reading) == "5.1"


def test_an_unknown_sink_gets_stereo_because_silence_is_never_right():
    """When the set did not say, the safe answer is the one that produces
    sound on the widest range of hardware. A 5.1 track opened against a
    sink that cannot take it is silent, with no error anywhere."""
    assert eld.channel_layout_for(None) == "stereo"
    assert eld.channel_layout_for(eld.parse_eld(NOTHING_ATTACHED)) == "stereo"
