"""The dashboard's only way of reaching the television, and how it heals.

Found on the bench box: ``send_command`` returned False for every command -
including ``reload``, which predates all of this - with ConnectionRefused
against a control socket file that was sitting right there. Both processes
agreed on the path. Nothing was listening on it.

A unix socket bound by a process that has gone, or whose directory was swept
out from under it, leaves exactly that: a file everybody can see and nobody
answers. ``/run/user/<uid>`` is managed by logind and can be torn down and
recreated underneath a long-running service, so the socket has to be able to
notice it has been orphaned and bind itself again. It is bound once at start
and then never checked, which is a listener that only works until the first
time anything disturbs it.

Nothing here needs a television.
"""

import queue
import socket
import time
from pathlib import Path

import pytest

from retrobox.actions import Action
from retrobox.input.web import WebBackend


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _can_connect(path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(path))
        return True
    except OSError:
        return False


def _send(path, line) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(path))
            sock.sendall((line + "\n").encode())
        return True
    except OSError:
        return False


@pytest.fixture
def short_dir():
    """A unix socket path may be ~108 bytes; pytest's tmp_path blows that on
    macOS before a single test runs."""
    import shutil
    import tempfile

    made = Path(tempfile.mkdtemp(prefix="rb", dir="/tmp"))
    try:
        yield made
    finally:
        shutil.rmtree(made, ignore_errors=True)


@pytest.fixture
def backend(short_dir):
    made = WebBackend(short_dir / "control.sock")
    made.REBIND_CHECK_SECONDS = 0.1     # the test is not waiting ten seconds
    events: "queue.Queue" = queue.Queue()
    made.start(events)
    assert _wait_until(lambda: _can_connect(short_dir / "control.sock")), \
        "the backend never started listening at all"
    yield made, events, short_dir / "control.sock"
    made.stop()


def test_it_listens_to_start_with(backend):
    _, events, path = backend
    assert _send(path, "reload")
    assert events.get(timeout=2).action is Action.RELOAD


def test_a_socket_somebody_deleted_is_bound_again(backend):
    """logind tears /run/user/<uid> down and builds it again. A listener that
    binds once and never looks is deaf from that moment on."""
    _, events, path = backend
    path.unlink()
    assert _wait_until(lambda: path.exists()), "the socket was never recreated"
    assert _wait_until(lambda: _can_connect(path)), "recreated but nobody home"
    assert _send(path, "reload")
    assert events.get(timeout=2).action is Action.RELOAD


def test_a_socket_replaced_by_another_file_is_taken_back(backend):
    """A stale file at the path is worse than none: everything can see it and
    nothing answers, which is precisely the reported symptom."""
    _, events, path = backend
    path.unlink()
    path.write_text("not a socket")
    assert _wait_until(lambda: _can_connect(path), timeout=6)
    assert _send(path, "audio_setup")
    assert events.get(timeout=2).action is Action.AUDIO_SETUP


def test_the_whole_directory_disappearing_is_survived(backend):
    """The real failure: logind removes /run/user/<uid> entirely."""
    _, events, path = backend
    path.unlink()
    path.parent.rmdir()
    assert _wait_until(lambda: _can_connect(path), timeout=6), \
        "the backend did not rebuild its directory"
    assert _send(path, "test_tone")
    assert events.get(timeout=2).action is Action.TEST_TONE


def test_stopping_still_stops(backend):
    """The watchdog must not keep a stopped backend alive, or the television
    would refuse to shut down."""
    made, _, path = backend
    made.stop()
    assert _wait_until(lambda: not _can_connect(path), timeout=3)


def test_it_does_not_fight_another_listener_for_the_path(backend):
    """The regression the bench box caught and these tests did not.

    The first version compared inodes: "the socket here is not the one I
    bound, so I will bind again". If anything else legitimately owns the path
    - a second backend after a reload, a newer process mid-restart - both
    sides unlink each other and rebind for ever, ten seconds apart, with the
    box deaf throughout. On the real box that showed up as an endless pair of
    'binding a new one' / 'stopped accepting' warnings.

    Whoever holds a working socket keeps it. A command reaching the other
    listener still reaches the television; a command reaching neither does not.
    """
    _, _, path = backend
    path.unlink()
    rival = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    rival.bind(str(path))
    rival.listen(4)
    try:
        settled = path.stat().st_ino
        time.sleep(0.8)                  # many watchdog passes at 0.1s
        assert path.stat().st_ino == settled, (
            "it rebound over a healthy socket somebody else owns - that is "
            "the rebind war"
        )
    finally:
        rival.close()


def test_a_socket_whose_listener_died_is_taken_over(backend):
    """The reported fault, exactly: a socket file everybody can see and
    nobody answers. It stats perfectly, so only knocking on it tells the
    truth - and the difference between this test and the one above is the
    whole design: take over a corpse, never fight the living."""
    _, events, path = backend
    path.unlink()
    corpse = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    corpse.bind(str(path))
    corpse.listen(4)
    corpse.close()                       # the file stays; the listener is gone
    assert path.exists(), "the corpse should still be on the filesystem"
    assert not _can_connect(path), "this test is not reproducing the fault"

    assert _wait_until(lambda: _can_connect(path), timeout=6), \
        "a dead socket was left in place and the box stayed deaf"
    assert _send(path, "reload")
    assert events.get(timeout=2).action is Action.RELOAD


def test_a_healthy_socket_is_left_alone(backend):
    """Rebinding a working socket would drop whatever was mid-command."""
    made, events, path = backend
    first = path.stat().st_ino
    time.sleep(0.5)                      # several watchdog passes
    assert path.stat().st_ino == first, "it rebound a socket that was fine"
    assert _send(path, "reload")
    assert events.get(timeout=2).action is Action.RELOAD
