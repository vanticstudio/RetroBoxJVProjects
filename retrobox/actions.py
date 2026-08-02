"""The abstract "remote control" vocabulary.

Every input backend (a USB/IR remote seen as a keyboard, the TV's own remote
over HDMI-CEC, or the developer's keyboard over stdin) is translated into one
of these high-level :class:`Action` values. The rest of the application only
ever deals with actions, never with raw key codes, which keeps the input
handling decoupled from the application logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class Action(Enum):
    """A single high-level intent produced by a remote control."""

    CHANNEL_UP = auto()
    CHANNEL_DOWN = auto()
    VOLUME_UP = auto()
    VOLUME_DOWN = auto()
    MUTE = auto()
    DIGIT = auto()          # carries which digit in InputEvent.value (0-9)
    ENTER = auto()          # confirm a direct channel entry ("OK" / select)
    INFO = auto()           # re-show the channel banner
    GUIDE = auto()          # toggle the on-screen channel guide
    MENU = auto()           # toggle the on-screen settings menu
    LAST_CHANNEL = auto()   # jump back to the previously watched channel
    SLEEP = auto()          # cycle the sleep timer (30 -> 60 -> 90 -> off)
    SHUTDOWN = auto()       # shut the machine down cleanly (menu / dashboard)
    POWER = auto()          # toggle standby (blank screen)
    RELOAD = auto()         # re-read config.yaml (the dashboard changed it)
    CRT_PREVIEW = auto()    # try picture settings on the live screen, unsaved
    CRT_CANCEL = auto()     # throw the preview away, back to what was saved
    WAKE = auto()           # the box went quiet with nothing watching: bring it back
    QUIT = auto()           # shut the application down entirely


@dataclass(frozen=True)
class CrtSettings:
    """A partial adjustment to the CRT picture effect, as a slider makes one.

    Every field is optional and None means "leave this one alone". A dashboard
    dragging one control sends only the control that moved, and the running
    television merges it onto what it is already showing - so a curvature drag
    does not quietly reset the scanlines somebody set a moment earlier.

    This is deliberately NOT a :class:`~retrobox.config.CrtConfig`. A CrtConfig
    is a complete, saved picture; this is an unsaved nudge to part of one, and
    the type difference is what stops a preview being mistaken for something
    somebody committed to.
    """

    enabled: Optional[bool] = None
    curvature: Optional[float] = None
    corner_radius: Optional[float] = None
    vignette: Optional[float] = None
    scanlines: Optional[bool] = None
    scanline_intensity: Optional[float] = None

    def changes(self) -> dict:
        """The fields that were actually given, ready to merge onto a config."""
        return {
            name: value
            for name, value in vars(self).items()
            if value is not None
        }


@dataclass(frozen=True)
class InputEvent:
    """An action plus optional payload, as emitted by an input backend.

    ``value`` carries the pressed digit for :attr:`Action.DIGIT` events.

    ``crt`` carries the settings for :attr:`Action.CRT_PREVIEW`. It is a second
    slot rather than a reuse of ``value`` because a curvature is not a digit
    and pretending otherwise would cost the next reader half an hour. Both are
    immutable, so an event stays safe to hand between the input thread and the
    main loop.
    """

    action: Action
    value: Optional[int] = None
    crt: Optional[CrtSettings] = None

    @classmethod
    def digit(cls, number: int) -> "InputEvent":
        if not 0 <= number <= 9:
            raise ValueError(f"digit must be 0-9, got {number}")
        return cls(Action.DIGIT, number)


__all__ = ["Action", "CrtSettings", "InputEvent"]
