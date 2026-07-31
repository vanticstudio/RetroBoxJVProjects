"""The on-screen menu: what it contains and what selecting things does.

This module is deliberately pure. It knows the shape of the menu - which rows
exist on which screen, where the highlight is, what activating a row means -
but it never touches the player, the config or the overlay. The application
feeds it a :class:`MenuContext` snapshot and gets back a :class:`MenuCommand`
to carry out, which keeps every navigation rule unit-testable without a display.

Layout and hit-testing live in ``overlay.py`` alongside the other ASS builders,
because the row rectangles and the drawing have to agree with each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

# Screens. The menu is one focused overlay, not a desktop: MAIN plus three
# drill-downs that each return straight back to it.
SCREEN_MAIN = "main"
SCREEN_CHANNELS = "channels"
SCREEN_AUDIO = "audio"
SCREEN_SHUTDOWN = "shutdown"
SCREEN_ABOUT = "about"

BRAND = "JV Projects"


@dataclass(frozen=True)
class MenuItem:
    """One selectable row: a label, an optional right-hand value, and an id."""

    key: str
    label: str
    value: str = ""
    #: Rows that only display (the About screen's body) are not selectable.
    selectable: bool = True


@dataclass(frozen=True)
class MenuCommand:
    """What the application should do as a result of activating a row."""

    kind: str          # none | close | tune | volume | audio | shutdown
    value: object = None


NO_COMMAND = MenuCommand("none")


@dataclass
class MenuContext:
    """A snapshot of everything the menu needs to render itself."""

    channels: Sequence[Tuple[int, str]] = ()      # (number, name)
    current_channel: Optional[int] = None
    volume: int = 0
    muted: bool = False
    audio_devices: Sequence[str] = ()
    current_audio: Optional[str] = None
    version: str = ""


class MenuModel:
    """Screen + highlight position, and the rules for moving between them."""

    def __init__(self, context: MenuContext) -> None:
        self.context = context
        self.screen = SCREEN_MAIN
        self.index = 0

    # -- rows ---------------------------------------------------------------
    def rows(self) -> List[MenuItem]:
        if self.screen == SCREEN_CHANNELS:
            return self._channel_rows()
        if self.screen == SCREEN_AUDIO:
            return self._audio_rows()
        if self.screen == SCREEN_SHUTDOWN:
            return self._shutdown_rows()
        if self.screen == SCREEN_ABOUT:
            return self._about_rows()
        return self._main_rows()

    @property
    def title(self) -> str:
        return {
            SCREEN_CHANNELS: "CHANNELS",
            SCREEN_AUDIO: "AUDIO OUTPUT",
            SCREEN_SHUTDOWN: "SHUT DOWN",
            SCREEN_ABOUT: "ABOUT",
        }.get(self.screen, "MENU")

    def _main_rows(self) -> List[MenuItem]:
        ctx = self.context
        current = next(
            (name for number, name in ctx.channels if number == ctx.current_channel),
            "",
        )
        channel_value = (
            f"CH {ctx.current_channel:02d}  {current}" if ctx.current_channel else ""
        )
        volume_value = "MUTED" if ctx.muted else f"{ctx.volume}"
        audio_value = _short_device(ctx.current_audio) if ctx.current_audio else "system default"
        return [
            MenuItem("channels", "Channels", channel_value),
            MenuItem("volume", "Volume", volume_value),
            MenuItem("audio", "Audio output", audio_value),
            MenuItem("shutdown", "Shut down"),
            MenuItem("about", "About", BRAND),
        ]

    def _channel_rows(self) -> List[MenuItem]:
        ctx = self.context
        rows = [
            MenuItem(
                f"ch:{number}",
                f"CH {number:02d}",
                name + ("   <" if number == ctx.current_channel else ""),
            )
            for number, name in ctx.channels
        ]
        rows.append(MenuItem("back", "Back"))
        return rows

    def _audio_rows(self) -> List[MenuItem]:
        ctx = self.context
        rows = [
            MenuItem(
                f"audio:{device}",
                _short_device(device),
                "<" if device == ctx.current_audio else "",
            )
            for device in ctx.audio_devices
        ]
        if not rows:
            rows.append(MenuItem("none", "No HDMI outputs detected", "", selectable=False))
        rows.append(MenuItem("back", "Back"))
        return rows

    def _shutdown_rows(self) -> List[MenuItem]:
        return [
            MenuItem("no", "No, keep watching"),
            MenuItem("yes", "Yes, shut down"),
        ]

    def _about_rows(self) -> List[MenuItem]:
        return [
            MenuItem("brand", BRAND, "", selectable=False),
            MenuItem("product", "Retro Box", self.context.version, selectable=False),
            MenuItem("back", "Back"),
        ]

    # -- navigation ---------------------------------------------------------
    def move(self, delta: int) -> None:
        """Move the highlight, wrapping, skipping any display-only rows."""
        rows = self.rows()
        selectable = [i for i, row in enumerate(rows) if row.selectable]
        if not selectable:
            return
        if self.index in selectable:
            position = selectable.index(self.index)
            self.index = selectable[(position + delta) % len(selectable)]
        else:
            self.index = selectable[0]

    def set_index(self, index: int) -> bool:
        """Point the highlight at ``index``. False if that row can't be picked."""
        rows = self.rows()
        if not 0 <= index < len(rows) or not rows[index].selectable:
            return False
        self.index = index
        return True

    def _goto(self, screen: str) -> None:
        self.screen = screen
        self.index = 0
        # Land on something usable rather than a display-only first row.
        rows = self.rows()
        if rows and not rows[self.index].selectable:
            self.move(1)

    # -- activation ---------------------------------------------------------
    def activate(self) -> MenuCommand:
        """Act on the highlighted row and report what the app should do."""
        rows = self.rows()
        if not 0 <= self.index < len(rows):
            return NO_COMMAND
        item = rows[self.index]
        if not item.selectable:
            return NO_COMMAND

        if item.key == "back":
            self._goto(SCREEN_MAIN)
            return NO_COMMAND

        if self.screen == SCREEN_MAIN:
            if item.key == "channels":
                self._goto(SCREEN_CHANNELS)
                self._preselect_current_channel()
            elif item.key == "audio":
                self._goto(SCREEN_AUDIO)
            elif item.key == "shutdown":
                self._goto(SCREEN_SHUTDOWN)
            elif item.key == "about":
                self._goto(SCREEN_ABOUT)
            # Volume is adjusted in place with left/right, so activating it
            # does nothing rather than opening a screen with one slider on it.
            return NO_COMMAND

        if item.key.startswith("ch:"):
            return MenuCommand("tune", int(item.key[3:]))
        if item.key.startswith("audio:"):
            return MenuCommand("audio", item.key[6:])
        if item.key == "yes":
            return MenuCommand("shutdown")
        if item.key == "no":
            self._goto(SCREEN_MAIN)
            return NO_COMMAND
        return NO_COMMAND

    def adjust(self, delta: int) -> MenuCommand:
        """Left/right on a row that holds a value. Only Volume has one."""
        rows = self.rows()
        if self.screen == SCREEN_MAIN and 0 <= self.index < len(rows):
            if rows[self.index].key == "volume":
                return MenuCommand("volume", delta)
        return NO_COMMAND

    def back(self) -> bool:
        """Step back one screen. False when already at the top (menu closes)."""
        if self.screen == SCREEN_MAIN:
            return False
        self._goto(SCREEN_MAIN)
        return True

    def _preselect_current_channel(self) -> None:
        rows = self.rows()
        target = f"ch:{self.context.current_channel}"
        for i, row in enumerate(rows):
            if row.key == target:
                self.index = i
                return


def _short_device(device: Optional[str]) -> str:
    """ALSA device strings are long; show the informative tail on screen."""
    if not device:
        return ""
    text = str(device)
    for prefix in ("alsa/", "hdmi:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.replace("CARD=", "").replace("DEV=", "d")


__all__ = [
    "BRAND",
    "MenuCommand",
    "MenuContext",
    "MenuItem",
    "MenuModel",
    "NO_COMMAND",
    "SCREEN_ABOUT",
    "SCREEN_AUDIO",
    "SCREEN_CHANNELS",
    "SCREEN_MAIN",
    "SCREEN_SHUTDOWN",
]
