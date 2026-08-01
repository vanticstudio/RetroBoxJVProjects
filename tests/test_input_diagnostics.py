"""Watching the remote work.

Programming the Flirc is the one manual step that never goes away, and until
now the only way to check it took was to point it at the telly and guess. This
turns that into something with feedback: press a button, watch it appear.

It is an observer, not a mode. The events still go where they were going.
"""

from queue import Queue

import pytest

from retrobox.actions import Action, InputEvent
from retrobox.input.base import InputBackend
from retrobox.input.manager import InputManager


class Fake(InputBackend):
    """A backend that emits only what a test tells it to."""

    def __init__(self, name):
        super().__init__()
        self.name = name

    def _run(self):
        pass


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def manager():
    clock = Clock()
    remote = Fake("keyboard")
    cec = Fake("cec")
    manager = InputManager([remote, cec], clock=clock)
    manager.start()
    return manager, remote, cec, clock


# ==========================================================================
# It observes
# ==========================================================================
def test_a_button_press_is_recorded_with_its_backend_and_action(manager):
    manager, remote, _, _ = manager
    remote.emit(InputEvent(Action.CHANNEL_UP))

    seen = manager.recent()
    assert len(seen) == 1
    assert seen[0]["backend"] == "keyboard"
    assert seen[0]["action"] == "CHANNEL_UP"


def test_the_digit_that_was_pressed_is_recorded_too(manager):
    manager, remote, _, _ = manager
    remote.emit(InputEvent.digit(7))
    assert manager.recent()[0]["value"] == 7


def test_events_from_different_backends_are_told_apart(manager):
    manager, remote, cec, _ = manager
    remote.emit(InputEvent(Action.VOLUME_UP))
    cec.emit(InputEvent(Action.POWER))

    assert [(e["backend"], e["action"]) for e in manager.recent()] == [
        ("keyboard", "VOLUME_UP"), ("cec", "POWER"),
    ]


def test_each_press_is_stamped_so_the_page_can_show_when(manager):
    manager, remote, _, clock = manager
    remote.emit(InputEvent(Action.MUTE))
    clock.now += 5
    remote.emit(InputEvent(Action.MUTE))

    stamps = [e["at"] for e in manager.recent()]
    assert stamps == [1000.0, 1005.0]


def test_an_injected_event_is_recorded_as_coming_from_the_dashboard(manager):
    manager, _, _, _ = manager
    manager.put(InputEvent(Action.GUIDE))
    assert manager.recent()[0]["backend"] == "dashboard"


# ==========================================================================
# ...without swallowing anything
# ==========================================================================
def test_the_event_still_reaches_the_television(manager):
    manager, remote, _, _ = manager
    remote.emit(InputEvent(Action.CHANNEL_UP))

    delivered = manager.get(timeout=1.0)
    assert delivered == InputEvent(Action.CHANNEL_UP), "the observer ate it"


def test_every_event_arrives_exactly_once(manager):
    manager, remote, _, _ = manager
    for _ in range(5):
        remote.emit(InputEvent(Action.CHANNEL_UP))

    got = [manager.get(timeout=1.0) for _ in range(5)]
    assert all(e is not None for e in got)
    assert manager.get(timeout=0.05) is None, "the observer duplicated one"


def test_an_observer_that_explodes_does_not_lose_the_button_press(manager):
    # The diagnostic is a convenience. The remote is the product.
    manager, remote, _, _ = manager

    def boom(*a, **k):
        raise RuntimeError("the diagnostic is broken")

    remote._observer = boom
    remote.emit(InputEvent(Action.POWER))
    assert manager.get(timeout=1.0) == InputEvent(Action.POWER)


def test_a_backend_used_without_a_manager_still_works():
    # Backends are startable on their own; the observer is wired by the
    # manager and must be optional.
    backend = Fake("solo")
    queue = Queue()
    backend.start(queue)
    backend.emit(InputEvent(Action.INFO))
    assert queue.get(timeout=1.0) == InputEvent(Action.INFO)
    backend.stop()


# ==========================================================================
# ...and without growing forever
# ==========================================================================
def test_only_the_last_few_presses_are_kept(manager):
    manager, remote, _, _ = manager
    for _ in range(InputManager.RECENT_LIMIT + 40):
        remote.emit(InputEvent(Action.CHANNEL_UP))

    assert len(manager.recent()) == InputManager.RECENT_LIMIT


def test_the_newest_press_is_last(manager):
    manager, remote, _, _ = manager
    remote.emit(InputEvent(Action.CHANNEL_UP))
    remote.emit(InputEvent(Action.CHANNEL_DOWN))
    assert manager.recent()[-1]["action"] == "CHANNEL_DOWN"


# ==========================================================================
# Which backends are actually live
# ==========================================================================
def test_the_live_backends_are_reported(manager):
    manager, _, _, _ = manager
    assert manager.backend_names() == ["keyboard", "cec"]


def test_a_box_with_no_input_backends_reports_none():
    assert InputManager([]).backend_names() == []
