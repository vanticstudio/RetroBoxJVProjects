"""Retro Box - a retro TV media player for a small Linux box.

Retro Box turns an Intel NUC (or any similar machine) into a late-90s style
television. It presents a fixed set of "channels" (each backed by a folder of
video), plays them on a continuous randomized shuffle, changes them with the
clock, and drives the whole thing from a remote control with authentic touches
like a channel banner, an on-screen volume bar, a channel guide, station
bumpers and a sleep timer.

The package is split so that the "brains" (channel scanning, shuffle logic,
the application state machine) are pure Python with no hardware dependencies,
while the "hands" (the mpv video player, the remote-control input backends)
are isolated and imported lazily. This makes the interesting logic fully
testable on any machine, not just on the box in front of a TV.
"""

#: The single source of truth for the version of this box.
#:
#: pyproject.toml derives its own version from this attribute, and the
#: release process requires the git tag to match it - there is a test that
#: fails on a tagged commit if they disagree. Nothing else may hold a copy.
__version__ = "1.0.3"

__all__ = ["__version__"]
