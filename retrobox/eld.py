"""Which HDMI socket the television is actually plugged into.

An Intel HDMI audio card exposes one playback device per physical port -
commonly ALSA devices 3, 7 and 8 for HDMI 0, 1 and 2. Only the device
carrying the television produces a sound. Opening the wrong one *succeeds*:
mpv reports an output, the picture plays, the level meter moves, and the
room stays silent. Nothing anywhere reports an error.

That is why ``config.example.yaml`` used to say "try other DEV= for other
ports". Asking a customer to guess port numbers is not a product, and it is
not necessary, because the kernel already knows the answer.

Every HDMI port publishes an **ELD** - the block of data a display sends back
down the cable describing itself: its name, and which audio formats and how
many channels it will accept. A port with nothing plugged into it says
``monitor_present 0``. So the right device is *read*, never guessed.

Two details here are easy to get wrong and both are load-bearing:

* **The HDMI ordinal is not the ALSA device number.** HDMI 0/1/2 are devices
  3/7/8 on this chipset. The ordinal is what ALSA's ``hdmi:`` alias and mpv
  want; the device number is what ``hw:``/``plughw:`` want. Both are carried.
* **One port publishes several ELDs.** The bench box exposes nine files for
  three sockets - three MST stream ids per port. Counting files reports nine
  televisions. Ports are identified by ``codec_pin_nid``, not by filename.

Everything reads out of ``/proc``; nothing here opens an audio device, so it
is safe to call on a box that is mid-programme.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

ASOUND = Path("/proc/asound")
DRM = Path("/sys/class/drm")

#: What to ask for when the sink did not say what it accepts. A 5.1 track
#: opened against a sink that only takes stereo is silent, with no error
#: raised anywhere, so the cautious answer is the one that makes a noise.
DEFAULT_LAYOUT = "stereo"


@dataclass(frozen=True)
class EldReading:
    """One ELD file, parsed. Never raises - a garbled file reads as 'no'."""

    monitor_present: bool = False
    eld_valid: bool = False
    monitor_name: Optional[str] = None
    pin: Optional[int] = None
    device_id: int = 0
    max_channels: Optional[int] = None
    speakers: Optional[str] = None

    @property
    def usable(self) -> bool:
        """A display is attached AND it told us what it can play.

        ``monitor_present`` without ``eld_valid`` is a real state: the cable
        is in, the set answered, and what it said could not be understood.
        Playing into that is a guess, so it counts as not knowing.
        """
        return bool(self.monitor_present and self.eld_valid)


@dataclass(frozen=True)
class HdmiOutput:
    """One physical HDMI socket, and how to address it."""

    card_index: int
    card_id: str
    hdmi_index: int                       # HDMI 0, 1, 2 - what mpv wants
    alsa_device: Optional[int]            # 3, 7, 8 - what hw:/plughw: want
    eld: EldReading

    @property
    def usable(self) -> bool:
        return self.eld.usable

    @property
    def monitor_name(self) -> Optional[str]:
        return self.eld.monitor_name

    @property
    def mpv_name(self) -> str:
        """The name to hand mpv. ALSA's ``hdmi:`` alias sets the IEC958
        status bits an HDMI sink expects, which ``hw:`` does not."""
        return f"alsa/hdmi:CARD={self.card_id},DEV={self.hdmi_index}"

    @property
    def fallback_mpv_name(self) -> Optional[str]:
        """``plughw:`` converts rate, format and channel count in software.
        Uglier, and it will make a noise on a sink that refuses the exact
        format - worth having when the correct name has been tried."""
        if self.alsa_device is None:
            return None
        return f"alsa/plughw:CARD={self.card_id},DEV={self.alsa_device}"

    def describe(self) -> str:
        """One line, for a customer rather than for a log."""
        where = f"HDMI {self.hdmi_index}"
        if not self.usable:
            return f"{where} - nothing plugged in"
        name = self.monitor_name or "a display"
        channels = self.eld.max_channels or 2
        sound = "stereo" if channels <= 2 else f"{channels} channels"
        return f"{where} - {name}, accepts {sound}"


# ==========================================================================
# Reading one ELD
# ==========================================================================
def _fields(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        # The kernel writes "key\t\tvalue"; the value itself may contain
        # spaces ("FL/FR LFE FC"), so only the first run of whitespace splits.
        parts = re.split(r"\s+", line.strip(), maxsplit=1)
        if len(parts) == 2 and parts[0]:
            out[parts[0]] = parts[1].strip()
    return out


def _as_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    token = raw.split()[0]
    try:
        return int(token, 16) if token.lower().startswith("0x") else int(token)
    except ValueError:
        return None


def parse_eld(text: str) -> EldReading:
    """Parse one ``/proc/asound/card*/eld#*`` file.

    Never raises. A file that is missing, empty, truncated mid-write or full
    of rubbish reads as "no display", which is the safe answer: the caller
    then reports honestly rather than playing into a port on a hunch.
    """
    try:
        found = _fields(text or "")
    except Exception:  # noqa: BLE001 - a parser must never take the sound out
        log.debug("could not parse an ELD", exc_info=True)
        return EldReading()

    channels = None
    for key, value in found.items():
        if re.fullmatch(r"sad\d+_channels", key):
            count = _as_int(value)
            if count:
                channels = max(channels or 0, count)

    return EldReading(
        monitor_present=_as_int(found.get("monitor_present")) == 1,
        eld_valid=_as_int(found.get("eld_valid")) == 1,
        monitor_name=found.get("monitor_name") or None,
        pin=_as_int(found.get("codec_pin_nid")),
        device_id=_as_int(found.get("codec_dev_id")) or 0,
        max_channels=channels,
        speakers=found.get("speakers"),
    )


# ==========================================================================
# Reading a whole card
# ==========================================================================
def _card_ids(asound: Path) -> Dict[int, str]:
    """``0 -> "PCH"`` from /proc/asound/cards."""
    try:
        text = (asound / "cards").read_text()
    except OSError:
        return {}
    found = {}
    for line in text.splitlines():
        match = re.match(r"\s*(\d+)\s*\[([^\]]+)\]", line)
        if match:
            found[int(match.group(1))] = match.group(2).strip()
    return found


def _pcm_devices(asound: Path) -> Dict[int, Dict[int, int]]:
    """``{card: {hdmi_ordinal: alsa_device}}`` from /proc/asound/pcm.

    ``00-03: HDMI 0 : HDMI 0 : playback 1`` means HDMI 0 is device 3. The
    numbers are not consecutive and assuming the ordinal *is* the device
    number is the single easiest way to get this wrong.
    """
    try:
        text = (asound / "pcm").read_text()
    except OSError:
        return {}
    found: Dict[int, Dict[int, int]] = {}
    for line in text.splitlines():
        match = re.match(r"\s*(\d+)-(\d+):\s*HDMI\s+(\d+)\b", line)
        if match:
            card, device, ordinal = (int(g) for g in match.groups())
            found.setdefault(card, {})[ordinal] = device
    return found


def hdmi_outputs(*, asound: Path = ASOUND) -> List[HdmiOutput]:
    """Every physical HDMI socket the sound card exposes, in port order.

    Ports come back whether or not anything is plugged into them - "there
    are three sockets and all three are empty" is a different answer from
    "this box has no HDMI audio", and the customer needs to be told which.
    """
    asound = Path(asound)
    ids = _card_ids(asound)
    pcm = _pcm_devices(asound)
    outputs: List[HdmiOutput] = []

    try:
        cards = sorted(p for p in asound.glob("card*") if p.is_dir())
    except OSError:
        return []

    for card_dir in cards:
        match = re.fullmatch(r"card(\d+)", card_dir.name)
        if not match:
            continue
        card_index = int(match.group(1))

        # Group by pin: one physical socket, several MST stream ids.
        by_pin: Dict[int, EldReading] = {}
        try:
            eld_files = sorted(card_dir.glob("eld#*"))
        except OSError:
            continue
        for path in eld_files:
            try:
                reading = parse_eld(path.read_text())
            except OSError:
                continue
            if reading.pin is None:
                continue
            seen = by_pin.get(reading.pin)
            # Stream 0 is the port itself; a later stream only gets to speak
            # if it found a display that stream 0 did not.
            if seen is None or (reading.usable and not seen.usable):
                by_pin[reading.pin] = reading

        for ordinal, pin in enumerate(sorted(by_pin)):
            outputs.append(HdmiOutput(
                card_index=card_index,
                card_id=ids.get(card_index, str(card_index)),
                hdmi_index=ordinal,
                alsa_device=pcm.get(card_index, {}).get(ordinal),
                eld=by_pin[pin],
            ))

    return outputs


def _connected_hdmi_connectors(drm: Path) -> int:
    """How many HDMI connectors the graphics driver calls connected."""
    try:
        found = sorted(drm.glob("card*-HDMI-*"))
    except OSError:
        return 0
    live = 0
    for connector in found:
        try:
            if connector.joinpath("status").read_text().strip() == "connected":
                live += 1
        except OSError:
            continue
    return live


def choose_output(*, asound: Path = ASOUND,
                  drm: Path = DRM) -> Optional[HdmiOutput]:
    """The socket the television is on, or ``None`` if none is.

    ``None`` is a real, reportable answer - "no display advertising audio is
    attached" - and it is what a box built on a bench with no screen must
    say. It is emphatically not an invitation to pick port 0 and hope.

    A valid ELD is already stronger evidence than a connected DRM connector:
    it means a display is attached *and* has told us what audio it accepts,
    whereas a connector can be connected to something that takes no sound at
    all. DRM is consulted only to break a tie between several live ports.
    """
    live = [o for o in hdmi_outputs(asound=asound) if o.usable]
    if not live:
        return None
    if len(live) == 1:
        return live[0]

    # Several sets attached. Prefer one the graphics driver also sees, then
    # the lowest port, so the choice is at least stable between boots.
    if _connected_hdmi_connectors(Path(drm)):
        log.debug("several HDMI sinks advertise audio; taking the first")
    return sorted(live, key=lambda o: o.hdmi_index)[0]


# ==========================================================================
# What the sink will accept
# ==========================================================================
def channel_layout_for(reading: Optional[EldReading]) -> str:
    """The mpv ``audio-channels`` value this sink can actually play.

    A 5.1 track sent to a sink that only accepts stereo does not fail - it
    is *silent*, and nothing logs a reason. So the source is downmixed to
    what the display said it takes, and when the display did not say, to
    stereo, because silence is never the right answer.
    """
    if reading is None or not reading.usable:
        return DEFAULT_LAYOUT
    channels = reading.max_channels or 2
    if channels >= 8:
        return "7.1"
    if channels >= 6:
        return "5.1"
    return DEFAULT_LAYOUT
