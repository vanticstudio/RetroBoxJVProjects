"""On-screen display: the green digital channel banner, volume bar, and messages.

These are drawn to look like a late-90s/early-2000s TV's on-screen display: a
chunky phosphor-green readout in a retro terminal font, with a soft CRT glow.
Two signature elements:

* the **channel banner** ("CH 03" + the show name) that flashes top-right when
  you change channels, and
* the **volume bar** - a row of solid green bars for the current level followed
  by green dots for the rest, with a "Volume" label - matching a classic TV OSD.

Everything is rendered as ASS overlays on a fixed 1280x720 virtual canvas (mpv
scales it to the TV) and cleared automatically after a few seconds by
:meth:`OverlayManager.tick`, which the main loop calls every iteration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .config import Config, UiConfig
from .menu import MenuItem
from .player import Player

# Virtual canvas the overlays are laid out on. This maps to the WHOLE display
# (a 16:9 TV), so mpv scales it to whatever the screen is.
CANVAS_W = 1280
CANVAS_H = 720

# The video is forced into a 4:3 frame centred on the 16:9 canvas (see
# MpvPlayer.force_4_3). We lay the OSD out *inside* that 4:3 frame - with a small
# safe-area inset so nothing sits under the CRT's rounded corners - so the green
# readouts always sit over the picture, never out in the black pillarbox bars.
_FRAME_W = int(round(CANVAS_H * 4 / 3))        # 960
_FRAME_X0 = (CANVAS_W - _FRAME_W) // 2          # 160
_FRAME_X1 = _FRAME_X0 + _FRAME_W                # 1120
_FRAME_CX = (_FRAME_X0 + _FRAME_X1) // 2        # 640
_SAFE = 0.06
_IX0 = _FRAME_X0 + int(_FRAME_W * _SAFE)        # ~217  (left safe edge)
_IX1 = _FRAME_X1 - int(_FRAME_W * _SAFE)        # ~1062 (right safe edge)
_IY0 = int(CANVAS_H * _SAFE)                     # ~43   (top safe edge)
_IY1 = CANVAS_H - int(CANVAS_H * _SAFE)          # ~677  (bottom safe edge)

# Overlay slots (ids). Each kind of overlay owns one id so it can be replaced
# or cleared independently.
_ID_CHANNEL = 1
_ID_VOLUME = 2
_ID_STANDBY = 3
_ID_MESSAGE = 4
_ID_GUIDE = 5
_ID_SLEEP = 6
_ID_MENU = 7

# Menu layout. Rows are laid out on the same fixed 1280x720 virtual canvas as
# everything else, which is what makes mouse hit-testing exact: a pointer
# position is scaled into this space and compared against these rectangles.
_MENU_ROW_H = 56
_MENU_TOP = _IY0 + 96                       # first row, below the title
_MENU_X = _IX0 + 40
_MENU_W = (_IX1 - _IX0) - 80
_MENU_VALUE_X = _IX0 + _MENU_W - 40
_MENU_MAX_ROWS = (_IY1 - _MENU_TOP) // _MENU_ROW_H
_MENU_DIM_ALPHA = 0x78

_BLACK = "&H00000000"

# Channel-guide layout, all inside the 4:3 safe area.
_GUIDE_ROW_H = 44
_GUIDE_TOP = _IY0 + 76                 # first row sits below the header
_GUIDE_MAX_ROWS = (_IY1 - _GUIDE_TOP) // _GUIDE_ROW_H
_GUIDE_X_NUM = _IX0
_GUIDE_X_NAME = _IX0 + 112
_GUIDE_X_NOW = _IX0 + 432
# Rows other than the one you're watching are drawn semi-transparent so the
# current channel reads instantly, the way a cable box highlighted its row.
_GUIDE_DIM_ALPHA = 0x78


@dataclass(frozen=True)
class GuideEntry:
    """One row of the on-screen channel guide."""

    number: int
    name: str
    now_playing: str = ""
    off_air: bool = False


class OverlayManager:
    """Draws and expires the TV's on-screen overlays."""

    def __init__(
        self,
        player: Player,
        config: Config,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._player = player
        self._config = config
        self._ui = config.ui
        self._clock = clock
        # overlay id -> wall time (monotonic) at which it should disappear.
        self._expiry: Dict[int, float] = {}
        # The guide is the only overlay you can toggle, so its visibility is
        # tracked here rather than in the app (which would go stale on expiry).
        self._guide_visible = False
        self._menu_visible = False

    # -- public API ---------------------------------------------------------
    def use_config(self, config: Config) -> None:
        """Adopt a reloaded config, so overlay timings and colours follow it."""
        self._config = config
        self._ui = config.ui

    def show_channel_bug(
        self, number: int, name: str, *, duration: Optional[float] = None
    ) -> None:
        """Flash the channel number + name, like changing channels on a cable box."""
        dur = self._config.channel_bug_seconds if duration is None else duration
        ass = _channel_bug_ass(number, name, self._ui)
        self._player.set_overlay(_ID_CHANNEL, ass, CANVAS_W, CANVAS_H)
        self._arm(_ID_CHANNEL, dur)

    def show_volume(
        self, level: int, muted: bool, *, duration: Optional[float] = None
    ) -> None:
        dur = self._config.osd_duration if duration is None else duration
        ass = _volume_ass(level, muted, self._ui)
        self._player.set_overlay(_ID_VOLUME, ass, CANVAS_W, CANVAS_H)
        self._arm(_ID_VOLUME, dur)

    def show_message(self, text: str, *, duration: Optional[float] = None) -> None:
        dur = self._config.osd_duration if duration is None else duration
        ass = _message_ass(text, self._ui)
        self._player.set_overlay(_ID_MESSAGE, ass, CANVAS_W, CANVAS_H)
        self._arm(_ID_MESSAGE, dur)

    def show_standby(self) -> None:
        """Persistent 'standby' notice for when the box is 'off'."""
        ass = _standby_ass(self._ui)
        self._player.set_overlay(_ID_STANDBY, ass, CANVAS_W, CANVAS_H)
        self._expiry.pop(_ID_STANDBY, None)

    def clear_standby(self) -> None:
        self._player.clear_overlay(_ID_STANDBY)
        self._expiry.pop(_ID_STANDBY, None)

    # -- channel guide ------------------------------------------------------
    @property
    def guide_visible(self) -> bool:
        return self._guide_visible

    def show_guide(
        self,
        entries: Sequence[GuideEntry],
        *,
        current_number: Optional[int] = None,
        selected_number: Optional[int] = None,
        header: str = "",
        duration: Optional[float] = None,
    ) -> None:
        """Draw the channel grid, scrolled so the highlighted row is on screen.

        ``current_number`` is the channel actually playing; ``selected_number``
        is the row the viewer has arrowed onto (they are the same when the guide
        first opens).
        """
        dur = self._config.guide_seconds if duration is None else duration
        selected = current_number if selected_number is None else selected_number
        ass = _guide_ass(entries, current_number, selected, self._ui, header)
        self._player.set_overlay(_ID_GUIDE, ass, CANVAS_W, CANVAS_H)
        self._guide_visible = True
        self._arm(_ID_GUIDE, dur)

    def clear_guide(self) -> None:
        self._player.clear_overlay(_ID_GUIDE)
        self._expiry.pop(_ID_GUIDE, None)
        self._guide_visible = False

    # -- menu ---------------------------------------------------------------
    @property
    def menu_visible(self) -> bool:
        return self._menu_visible

    def show_menu(self, title: str, rows: Sequence["MenuItem"], index: int) -> None:
        """Draw the menu. Persistent - it stays until explicitly closed."""
        ass = _menu_ass(title, rows, index, self._ui)
        self._player.set_overlay(_ID_MENU, ass, CANVAS_W, CANVAS_H)
        self._expiry.pop(_ID_MENU, None)
        self._menu_visible = True

    def clear_menu(self) -> None:
        self._player.clear_overlay(_ID_MENU)
        self._expiry.pop(_ID_MENU, None)
        self._menu_visible = False

    # -- sleep timer --------------------------------------------------------
    def show_sleep(self, minutes: int) -> None:
        """Persistent corner readout while the sleep timer counts down."""
        ass = _sleep_ass(minutes, self._ui)
        self._player.set_overlay(_ID_SLEEP, ass, CANVAS_W, CANVAS_H)
        self._expiry.pop(_ID_SLEEP, None)

    def clear_sleep(self) -> None:
        self._player.clear_overlay(_ID_SLEEP)
        self._expiry.pop(_ID_SLEEP, None)

    def tick(self) -> None:
        """Clear any overlays whose time is up. Call this every loop iteration."""
        now = self._clock()
        for overlay_id, when in list(self._expiry.items()):
            if now >= when:
                self._player.clear_overlay(overlay_id)
                self._expiry.pop(overlay_id, None)
                if overlay_id == _ID_GUIDE:
                    self._guide_visible = False

    def clear_all(self) -> None:
        for overlay_id in (
            _ID_CHANNEL, _ID_VOLUME, _ID_STANDBY, _ID_MESSAGE, _ID_GUIDE,
            _ID_SLEEP, _ID_MENU,
        ):
            self._player.clear_overlay(overlay_id)
        self._expiry.clear()
        self._guide_visible = False
        self._menu_visible = False

    # -- internals ----------------------------------------------------------
    def _arm(self, overlay_id: int, duration: float) -> None:
        if duration <= 0:
            # duration 0 means "leave it until explicitly cleared"
            self._expiry.pop(overlay_id, None)
        else:
            self._expiry[overlay_id] = self._clock() + duration


# --------------------------------------------------------------------------
# Colour + style helpers
# --------------------------------------------------------------------------
def _hex_to_ass(hex_color: str, alpha: int = 0) -> str:
    """Convert ``#RRGGBB`` to an ASS ``&HAABBGGRR`` colour string."""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _style(ui: UiConfig, *, size: int, alpha: int = 0) -> str:
    """Common ASS override tags: retro font, green fill, and a soft CRT glow."""
    color = _hex_to_ass(ui.color, alpha)
    tags = rf"\fn{ui.font}\b1\fs{size}\c{color}\1a&H{alpha:02X}&"
    if ui.glow:
        # A blurred green border reads as phosphor bloom; a faint dark edge keeps
        # it legible over bright video.
        tags += rf"\bord2\blur4\3c{color}\4c{_BLACK}\shad0"
    else:
        tags += rf"\bord2\3c{_BLACK}\shad0"
    return tags


# --------------------------------------------------------------------------
# ASS builders (free functions so they are easy to unit test)
# --------------------------------------------------------------------------
def _channel_bug_ass(number: int, name: str, ui: UiConfig) -> str:
    """Green digital 'CH 03' + show name, flashed inside the top-right of the frame."""
    num = f"{number:02d}"
    number_line = (
        rf"{{\an9\pos({_IX1},{_IY0}){_style(ui, size=88)}}}CH {num}"
    )
    name_line = (
        rf"{{\an9\pos({_IX1},{_IY0 + 104}){_style(ui, size=40)}}}{_escape(name)}"
    )
    return "\n".join([number_line, name_line])


def _volume_ass(level: int, muted: bool, ui: UiConfig) -> str:
    """A 'Volume' label with solid green bars (level) then green dots (remainder)."""
    level = max(0, min(100, int(level)))
    segments = 20
    filled = 0 if muted else round(level / 100 * segments)

    bar_w = 16
    pitch = 38
    bar_h = 48
    total_w = (segments - 1) * pitch + bar_w
    x0 = _FRAME_CX - total_w // 2          # centre the bar within the 4:3 frame
    row_top = _IY1 - bar_h                  # sit just above the bottom safe edge
    dot_r = 6
    green = _hex_to_ass(ui.color)
    dim = _hex_to_ass(ui.dim_color)   # unlit segments, like a real TV's bar

    label = "Mute" if muted else "Volume"
    parts = [
        rf"{{\an7\pos({x0},{row_top - 62}){_style(ui, size=48)}}}{label}"
    ]

    for i in range(segments):
        cx = x0 + i * pitch + bar_w / 2
        if i < filled:
            parts.append(
                _filled_rect(x=x0 + i * pitch, y=row_top, w=bar_w, h=bar_h, fill=green)
            )
        else:
            parts.append(_dot(cx=cx, cy=row_top + bar_h / 2, r=dot_r, fill=dim))
    return "\n".join(parts)


def _message_ass(text: str, ui: UiConfig) -> str:
    """A centred green digital message (channel entry, 'NO SIGNAL', etc.)."""
    return rf"{{\an8\pos({_FRAME_CX},{_IY0}){_style(ui, size=60)}}}{_escape(text)}"


def _standby_ass(ui: UiConfig) -> str:
    return rf"{{\an5\pos({_FRAME_CX},{CANVAS_H // 2}){_style(ui, size=72)}}}STANDBY"


def _guide_window(
    entries: Sequence[GuideEntry], current_number: Optional[int], max_rows: int
) -> List[GuideEntry]:
    """Slice the lineup so the highlighted row is visible and roughly centred."""
    rows = list(entries)
    if max_rows <= 0 or len(rows) <= max_rows:
        return rows
    index = next((i for i, e in enumerate(rows) if e.number == current_number), 0)
    start = max(0, min(index - max_rows // 2, len(rows) - max_rows))
    return rows[start : start + max_rows]


def _guide_ass(
    entries: Sequence[GuideEntry],
    current_number: Optional[int],
    selected_number: Optional[int],
    ui: UiConfig,
    header: str = "",
) -> str:
    """The cable-box channel grid: number, station name, and what's on now.

    The highlighted row (``>``) is what the viewer has arrowed onto; the channel
    actually playing is flagged with ``*`` when it isn't the highlighted one.
    """
    parts = []
    if header:
        parts.append(
            rf"{{\an7\pos({_IX0},{_IY0}){_style(ui, size=44)}}}{_escape(header)}"
        )

    window = _guide_window(entries, selected_number, _GUIDE_MAX_ROWS)
    for row, entry in enumerate(window):
        y = _GUIDE_TOP + row * _GUIDE_ROW_H
        selected = entry.number == selected_number
        alpha = 0 if selected else _GUIDE_DIM_ALPHA
        style = _style(ui, size=34, alpha=alpha)
        if alpha:
            # Fade the glow with the text, or dimmed rows keep a bright halo.
            style += rf"\3a&H{alpha:02X}&"

        if selected:
            marker = ">"
        elif entry.number == current_number:
            marker = "*"
        else:
            marker = " "
        now = "OFF AIR" if entry.off_air else entry.now_playing
        cells = (
            (_GUIDE_X_NUM, f"{marker}CH {entry.number:02d}"),
            (_GUIDE_X_NAME, _truncate(entry.name, 20)),
            (_GUIDE_X_NOW, _truncate(now, 26)),
        )
        for x, text in cells:
            if text:
                parts.append(rf"{{\an7\pos({x},{y}){style}}}{_escape(text)}")
    return "\n".join(parts)


def _menu_window(rows: Sequence["MenuItem"], index: int, max_rows: int) -> Tuple[int, int]:
    """(start, end) slice of rows to draw, scrolled to keep ``index`` visible."""
    if max_rows <= 0 or len(rows) <= max_rows:
        return 0, len(rows)
    start = max(0, min(index - max_rows // 2, len(rows) - max_rows))
    return start, start + max_rows


def menu_row_at(rows: Sequence["MenuItem"], index: int, x: float, y: float) -> Optional[int]:
    """Which row is at canvas point (x, y)? ``None`` if the click missed.

    This is the mouse half of the menu, and it is the exact inverse of the
    layout in :func:`_menu_ass` - both work in the same 1280x720 canvas space,
    so a click can be resolved without asking the display anything.
    """
    if x < _MENU_X - 20 or x > _MENU_X + _MENU_W:
        return None
    start, end = _menu_window(rows, index, _MENU_MAX_ROWS)
    offset = int((y - _MENU_TOP) // _MENU_ROW_H)
    if offset < 0:
        return None
    row = start + offset
    if row >= end or row >= len(rows):
        return None
    return row if rows[row].selectable else None


def _menu_ass(title: str, rows: Sequence["MenuItem"], index: int, ui: UiConfig) -> str:
    """The menu: a dimmed backdrop, a title, and one line per row."""
    green = _hex_to_ass(ui.color)
    parts = [
        # A translucent black panel so the menu stays legible over any picture.
        _filled_rect(
            x=_MENU_X - 20, y=_IY0 - 10,
            w=_MENU_W + 40, h=_IY1 - _IY0 + 20,
            fill="&H00000000", alpha=0xB4,
        ),
        rf"{{\an7\pos({_MENU_X},{_IY0 + 16}){_style(ui, size=52)}}}{_escape(title)}",
    ]

    start, end = _menu_window(rows, index, _MENU_MAX_ROWS)
    for offset, row in enumerate(rows[start:end]):
        y = _MENU_TOP + offset * _MENU_ROW_H
        selected = (start + offset) == index
        alpha = 0 if selected else _MENU_DIM_ALPHA
        style = _style(ui, size=38, alpha=alpha)
        if alpha:
            style += rf"\3a&H{alpha:02X}&"

        if selected:
            # A filled bar behind the highlighted row, the way a set-top box did.
            parts.append(
                _filled_rect(
                    x=_MENU_X - 16, y=y - 6,
                    w=_MENU_W + 16, h=_MENU_ROW_H - 6,
                    fill=green, alpha=0xC8,
                )
            )
        marker = ">" if selected else " "
        parts.append(
            rf"{{\an7\pos({_MENU_X},{y}){style}}}{_escape(marker + ' ' + row.label)}"
        )
        if row.value:
            parts.append(
                rf"{{\an9\pos({_MENU_VALUE_X},{y}){style}}}{_escape(row.value)}"
            )
    return "\n".join(parts)


def _sleep_ass(minutes: int, ui: UiConfig) -> str:
    """A small persistent 'SLEEP 29m' readout in the top-left of the frame."""
    return (
        rf"{{\an7\pos({_IX0},{_IY0}){_style(ui, size=34)}}}"
        rf"SLEEP {max(0, int(minutes))}m"
    )


def _truncate(text: str, limit: int) -> str:
    """Cut over-long guide cells to fit their column (plain ASCII, font-safe)."""
    text = str(text).strip()
    if len(text) <= limit or limit < 4:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _filled_rect(
    *, x: float, y: float, w: float, h: float, fill: str, alpha: int = 0
) -> str:
    """An ASS drawing (\\p1) filled rectangle at absolute canvas coordinates.

    ``alpha`` is ASS transparency: 0 is opaque, 0xFF invisible. The menu uses it
    for its translucent backdrop and highlight bar.
    """
    x, y = round(x), round(y)
    w, h = round(w), round(h)
    draw = f"m 0 0 l {w} 0 l {w} {h} l 0 {h}"
    return (
        rf"{{\an7\pos({x},{y})\p1\c{fill}\1a&H{alpha:02X}&\bord0\shad0}}"
        rf"{draw}{{\p0}}"
    )


def _dot(*, cx: float, cy: float, r: float, fill: str) -> str:
    """A small filled circle centred at (cx, cy) using 4 bezier arcs."""
    c = 0.5523 * r  # magic constant to approximate a circle with cubic beziers
    x, y = round(cx), round(cy)
    r = round(r, 2)
    c = round(c, 2)
    path = (
        f"m 0 {-r} "
        f"b {c} {-r} {r} {-c} {r} 0 "
        f"b {r} {c} {c} {r} 0 {r} "
        f"b {-c} {r} {-r} {c} {-r} 0 "
        f"b {-r} {-c} {-c} {-r} 0 {-r}"
    )
    return rf"{{\an5\pos({x},{y})\p1\c{fill}\1a&H00&\bord0\shad0}}{path}{{\p0}}"


def _escape(text: str) -> str:
    """Escape characters that are meaningful inside an ASS override block."""
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


__all__ = [
    "OverlayManager",
    "GuideEntry",
    "menu_row_at",
    "CANVAS_W",
    "CANVAS_H",
]
