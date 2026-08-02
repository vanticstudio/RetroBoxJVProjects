"""Fans multiple input backends into one queue of actions.

The application creates an :class:`InputManager`, starts it, and then simply
calls :meth:`get` in its main loop. Which backends are active is decided by
:func:`create_backends` based on the ``input:`` section of the config and on
what is actually available on the machine.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from queue import Empty, Queue
from typing import Any, Callable, Deque, Dict, List, Optional

from ..actions import InputEvent
from .base import InputBackend

log = logging.getLogger(__name__)


class InputManager:
    """Owns the shared event queue and the lifecycle of all input backends."""

    #: How many recent presses the dashboard's input test can show. Small on
    #: purpose - it rides along in the status snapshot, and this is a "did the
    #: remote work" display, not a history.
    RECENT_LIMIT = 40

    def __init__(
        self,
        backends: List[InputBackend],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._backends = backends
        self._queue: "Queue[InputEvent]" = Queue()
        self._started = False
        self._clock = clock
        # Events arrive on backend threads, so the log is guarded. A deque
        # with a maxlen is already atomic for append, but recent() copies it.
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=self.RECENT_LIMIT)
        self._recent_lock = threading.Lock()

    @property
    def backends(self) -> List[InputBackend]:
        return list(self._backends)

    def backend_names(self) -> List[str]:
        """Which input sources are actually live on this box."""
        return [b.name for b in self._backends]

    def start(self) -> None:
        if self._started:
            return
        for backend in self._backends:
            backend._observer = self._record
            backend.start(self._queue)
        self._started = True

    # -- the input test ------------------------------------------------------
    def _record(self, backend: str, event: InputEvent) -> None:
        """Note a press for the dashboard. Never allowed to affect delivery."""
        with self._recent_lock:
            self._recent.append({
                "at": self._clock(),
                "backend": backend,
                "action": event.action.name,
                "value": event.value,
            })

    def recent(self) -> List[Dict[str, Any]]:
        """The last few presses, oldest first."""
        with self._recent_lock:
            return list(self._recent)

    def get(self, timeout: Optional[float] = None) -> Optional[InputEvent]:
        """Return the next input event, or None if none arrives within timeout."""
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def put(self, event: InputEvent) -> None:
        """Inject an event directly (the dashboard's remote, dev mode, tests)."""
        self._record("dashboard", event)
        self._queue.put(event)

    def stop(self) -> None:
        for backend in self._backends:
            try:
                backend.stop()
            except Exception:  # noqa: BLE001
                log.debug("error stopping backend %s", backend.name, exc_info=True)
        self._started = False


def create_backends(options: Optional[Dict] = None) -> List[InputBackend]:
    """Build the list of input backends from the config ``input:`` options.

    Recognised options (all optional)::

        keyboard: true            # evdev USB/IR remote & keyboard input
        cec: true                 # HDMI-CEC (TV remote)
        stdin: false              # developer terminal input
        web: true                 # local socket for the web dashboard
        keyboard_devices: [/dev/input/event0, ...]
        keyboard_name_filter: "remote"
        keyboard_grab: false
        cec_binary: cec-client
        cec_osd_name: Retro Box

    Backends that are requested but unavailable on this machine are quietly
    skipped, so the same config works on the box and on a dev laptop.

    ``cec_binary`` becomes argv[0] of a real ``subprocess.Popen`` and
    ``cec_osd_name`` becomes an element of the same command line, so both are
    checked by ``config.parse_input_options`` before they ever get here - the
    same treatment ``power_off_command`` gets, and for the same reason. This
    function trusts what it is handed; the loader is where that trust is
    earned.
    """
    options = dict(options or {})
    backends: List[InputBackend] = []

    if options.get("keyboard", True):
        from .keyboard import KeyboardBackend
        from .keymap import parse_key_overrides

        try:
            overrides = parse_key_overrides(options.get("key_overrides"))
        except ValueError as exc:
            log.error("ignoring invalid key_overrides: %s", exc)
            overrides = {}
        if KeyboardBackend.is_available():
            backends.append(
                KeyboardBackend(
                    device_paths=options.get("keyboard_devices"),
                    name_filter=options.get("keyboard_name_filter"),
                    grab=bool(options.get("keyboard_grab", False)),
                    overrides=overrides,
                )
            )
        else:
            log.info("evdev not available; skipping keyboard backend")

    if options.get("web", True):
        from .web import WebBackend

        if WebBackend.is_available():
            backends.append(WebBackend(options.get("web_socket")))
        else:
            log.info("no AF_UNIX support; skipping web control backend")

    if options.get("cec", True):
        from .cec import CecBackend

        binary = options.get("cec_binary", "cec-client")
        if CecBackend.is_available(binary):
            backends.append(
                CecBackend(
                    binary=binary,
                    osd_name=options.get("cec_osd_name", "Retro Box"),
                )
            )
        else:
            log.info("cec-client not available; skipping HDMI-CEC backend")

    if options.get("stdin", False):
        from .stdin_backend import StdinBackend

        if StdinBackend.is_available():
            backends.append(StdinBackend())

    return backends


__all__ = ["InputManager", "create_backends"]
