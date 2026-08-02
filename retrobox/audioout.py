"""Deciding which output the television should play through.

This exists because of one line that was never there. The bench box shipped
with no ``audio_device`` in config.yaml, so mpv ran on ``--audio-device=auto``,
which resolves to ALSA ``default``, which on an Intel box is the ALC233
**analog** codec - the 3.5 mm headphone jack. The box played perfectly into a
socket with nothing in it for its entire life, and every layer above reported
success: mpv opened an output, the level moved, the picture ran.

So the choice is made deliberately, out loud, every time the television
starts, in this order:

1. **What the customer configured.** Always wins. Detection is here to save
   people configuring it, never to overrule them.
2. **What the kernel says.** The socket whose ELD reports a display that is
   present, valid, and advertising audio (see :mod:`retrobox.eld`).
3. **What the player can see.** Some cards publish no ELD. mpv still knows
   what it can open, and it is asked rather than ``aplay`` because the
   television service is the one holding the ``audio`` group.
4. **Nothing.** A real, reportable answer. Not an invitation to pick port 0.

Never fatal. A box with no working sound still plays pictures, and says why.
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import eld

log = logging.getLogger(__name__)

#: mpv names its analog default this way, and ``auto`` resolves to it. It is
#: never chosen deliberately - doing so just re-creates the original fault.
_ANALOG = re.compile(r"(default|sysdefault|front|surround\d+|dmix):", re.I)

#: How an HDMI output describes itself in mpv's device list.
_HDMI = re.compile(r"\bhdmi\b", re.I)


@dataclass(frozen=True)
class Decision:
    """The chosen output, and enough to explain it to a customer."""

    device: Optional[str]
    layout: str
    source: str                 # configured | eld | player | none
    summary: str
    monitor_name: Optional[str] = None

    @property
    def detected(self) -> bool:
        """True when the box worked it out rather than being told."""
        return self.source in ("eld", "player")

    @property
    def fatal(self) -> bool:
        """Never. A television with no sound is still a television.

        Kept as a property rather than left implicit so that the rule is
        stated somewhere a future change has to walk past.
        """
        return False


def _hdmi_from_player(devices: Sequence[Dict[str, str]]) -> Optional[str]:
    """The first HDMI output the player itself can see."""
    for entry in devices or []:
        name = str(entry.get("name") or "")
        described = f"{name} {entry.get('description') or ''}"
        if not name or name == "auto":
            continue
        if _ANALOG.search(name):
            continue
        if _HDMI.search(described):
            return name
    return None


def decide(configured: Optional[str], *,
           sockets: Optional[Sequence[eld.HdmiOutput]] = None,
           player_devices: Optional[Sequence[Dict[str, str]]] = None) -> Decision:
    """Pick the output, and say where the answer came from.

    ``sockets`` is what :func:`retrobox.eld.hdmi_outputs` found and
    ``player_devices`` what :meth:`Player.list_audio_devices` reported. Both
    are passed in rather than read here so this stays a pure decision that a
    test can drive without a sound card.
    """
    sockets = list(sockets or [])
    live = [s for s in sockets if s.usable]
    chosen_socket = live[0] if live else None

    if configured:
        # Still work out the layout: a hand-configured device on a stereo set
        # is just as silent with a 5.1 track as an auto-detected one.
        layout = eld.channel_layout_for(chosen_socket.eld if chosen_socket else None)
        return Decision(
            device=configured, layout=layout, source="configured",
            summary=f"Sound: using the output set in config.yaml ({configured})",
            monitor_name=chosen_socket.monitor_name if chosen_socket else None,
        )

    if chosen_socket is not None:
        layout = eld.channel_layout_for(chosen_socket.eld)
        name = chosen_socket.monitor_name or "a display"
        spoken = "stereo" if layout == "stereo" else layout
        return Decision(
            device=chosen_socket.mpv_name, layout=layout, source="eld",
            summary=(f"Sound: HDMI {chosen_socket.hdmi_index} - {name} is "
                     f"plugged in and accepts {spoken}"),
            monitor_name=chosen_socket.monitor_name,
        )

    if sockets:
        # The card published ELDs and every one of them says the socket is
        # empty. That is the kernel positively answering the question, not an
        # absence of information, so the player's list does not get to
        # overrule it with a port that has nothing plugged into it.
        where = "socket" if len(sockets) == 1 else "sockets"
        return Decision(
            device=None, layout=eld.DEFAULT_LAYOUT, source="none",
            summary=(f"Sound: no display advertising audio is attached - "
                     f"{len(sockets)} HDMI {where} on this box, all empty. "
                     "Plug the television in and switch the box off and on, "
                     "and it will find the sound by itself."),
        )

    # No ELD information at all: some cards publish none. mpv still knows
    # what it can open, and it is a better answer than giving up.
    fallback = _hdmi_from_player(player_devices or [])
    if fallback:
        return Decision(
            device=fallback, layout=eld.DEFAULT_LAYOUT, source="player",
            summary=("Sound: this card does not report what is plugged into "
                     f"it, so the first HDMI output was used ({fallback})"),
        )

    return Decision(
        device=None, layout=eld.DEFAULT_LAYOUT, source="none",
        summary=("Sound: this box has no HDMI audio outputs at all. The "
                 "picture still plays."),
    )


# ==========================================================================
# Unmuting what was chosen
# ==========================================================================
def unmute(card: Optional[str] = None, *, runner=None) -> List[str]:
    """Unmute and raise the HDMI/IEC958 controls on ``card``.

    HDMI outputs are frequently muted at zero by default, and everything
    above looks perfectly healthy while producing silence. Returns the
    controls it changed, so the dashboard can say what it did rather than
    claiming to have repaired something it did not touch.

    Never raises: no amixer, no card, no permission - all just mean nothing
    was changed, which is reported as an empty list.
    """
    run = runner or _run
    card = card or "0"
    listed = run(["amixer", "-c", str(card), "scontrols"])
    changed: List[str] = []
    for line in (listed or "").splitlines():
        match = re.search(r"'([^']+)'", line)
        if not match:
            continue
        control = match.group(1)
        if not re.search(r"hdmi|iec958|master|pcm", control, re.I):
            continue
        if run(["amixer", "-c", str(card), "sset", control, "unmute"]) is None:
            continue
        run(["amixer", "-c", str(card), "sset", control, "100%"])
        # A card lists the same control once per index ("IEC958,1",
        # "IEC958,2"). All of them get unmuted; the customer is told the name
        # once, because "IEC958, IEC958, IEC958" reads like a stutter.
        if control not in changed:
            changed.append(control)
    if changed:
        log.info("unmuted %s on card %s", ", ".join(changed), card)
    return changed


def _run(cmd) -> Optional[str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 - no amixer, no alsa-utils, no card
        log.debug("could not run %s", cmd, exc_info=True)
        return None
    if done.returncode != 0:
        return None
    return done.stdout or ""
