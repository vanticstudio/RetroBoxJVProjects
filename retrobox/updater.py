"""Applying an update to a box nobody can reach, and getting back if it fails.

The box runs an editable install from a git clone, so an update is a git
checkout and a pip install rather than an artefact pipeline. That is the whole
mechanism, and it is deliberately boring.

What is not boring is what happens when it goes wrong. A box that cannot roll
itself back has to be physically collected from somebody's living room, so
every step that can fail has a way back, the way back is taken automatically,
and the recorded rollback target is captured before anything at all is
touched.

Three things this module refuses to do:

* **Update a filesystem that will forget.** This product's own setup guide
  offers ``overlayroot``, which makes the root read-only with a tmpfs overlay.
  On such a box every step of an update appears to work and then vanishes at
  the next reboot - silent, repeating, and indistinguishable from a bug. That
  is checked first and aborts before anything changes.
* **Take a ref from anywhere but a version number.** ``apply()`` takes a
  version and validates its shape. There is no branch, no URL, no remote.
* **Report success for something it has not confirmed.** The player has to
  actually come back up, and if it does not the box goes back on its own.
  Coming back up once, on a box that is already running, is not confirmation
  either: this appliance is switched off at the wall, and the version that
  strands a box is the one that fails on the next cold start. So an update
  that passes its health check goes on *probation*, and the next few start-ups
  decide - see :meth:`Updater.on_boot`, which something has to call.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

#: A version, and nothing that could be a ref, a flag or a path.
_VERSION = re.compile(r"\A\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.-]+)?\Z")

#: Filesystems that accept a write and then forget it at the next boot.
_FORGETFUL = {"overlay", "overlayfs", "tmpfs", "ramfs", "squashfs", "aufs"}

#: How long to wait for the television after a restart. Long enough for a cold
#: mpv on a small box, short enough that nobody thinks it has hung.
HEALTH_TIMEOUT = 90.0


class UpdateError(Exception):
    """An update that was refused, or one that failed and was undone."""


@dataclass(frozen=True)
class Persistence:
    """Whether a change written here would still be here after a reboot."""

    writable: bool
    persists: bool
    reason: str = ""


def check_persistence(path: Path) -> Persistence:
    """Would a change to ``path`` survive a reboot?

    Not "can I write here" - an overlay is perfectly writable, it just forgets.
    So this asks what filesystem is actually underneath and whether that
    filesystem is one that keeps things.
    """
    target = Path(path).resolve()
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        # No /proc (macOS, a container). Cannot tell; do not block on a guess.
        return Persistence(writable=os.access(str(target), os.W_OK), persists=True)

    best: Tuple[int, str, str] = (-1, "", "")
    for line in mounts:
        parts = line.split()
        if len(parts) < 4:
            continue
        _device, mountpoint, fstype, options = parts[0], parts[1], parts[2], parts[3]
        try:
            resolved = Path(mountpoint).resolve()
        except OSError:
            continue
        if target == resolved or resolved in target.parents:
            if len(str(resolved)) > best[0]:
                best = (len(str(resolved)), fstype, options)

    _, fstype, options = best
    flags = set(options.split(","))
    if "ro" in flags:
        return Persistence(
            writable=False, persists=False,
            reason=f"{target} is on a read-only filesystem ({fstype})",
        )
    if fstype.lower() in _FORGETFUL:
        return Persistence(
            writable=True, persists=False,
            reason=(
                f"{target} is on a {fstype} filesystem, which is thrown away at "
                f"the next reboot - this box is running with a read-only root "
                f"(overlayroot). An update would appear to work and then vanish."
            ),
        )
    return Persistence(writable=os.access(str(target), os.W_OK), persists=True)


def _run(cmd: Sequence[str], *, cwd: Optional[Path] = None, timeout: float = 900.0):
    try:
        result = subprocess.run(
            list(cmd), cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()


def player_is_healthy(timeout: float = HEALTH_TIMEOUT) -> bool:
    """Wait for the television to actually come back and start playing."""
    from .status import read_status

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = read_status()
        if status and (status.get("now_playing") or status.get("channel")):
            return True
        time.sleep(2.0)
    return False


class Updater:
    """One update at a time, with the way back recorded before it starts."""

    #: The stages the dashboard names as it goes. A bare spinner during a
    #: firmware update on someone's television is not acceptable.
    STAGES = (
        "checking", "preparing", "downloading", "installing", "restarting",
        "health", "done",
    )

    #: How many boots on a new version may fail before the box gives up on it
    #: and puts the old one back without being asked.
    MAX_BOOT_ATTEMPTS = 3

    #: After this long with nothing written to it, an update that still says it
    #: is running is not running - it is what a power cut left behind. Nothing
    #: on the box clears that by itself and the owner cannot get a shell, so a
    #: stale one has to time out or it refuses every update for ever. Generous
    #: on purpose: it has to outlast a pip install on a slow box over slow
    #: broadband, because taking over from an update that really is running is
    #: two pip installs in one clone, which is how a box ends up unbootable.
    STALE_AFTER_SECONDS = 45 * 60

    def __init__(
        self,
        *,
        repo_dir: Path,
        state_path: Path,
        runner: Optional[Callable[..., Tuple[int, str]]] = None,
        health_check: Optional[Callable[[float], bool]] = None,
        persistence: Optional[Callable[[Path], Persistence]] = None,
        extras: str = "hardware,web",
        clock: Callable[[], float] = time.time,
        health_timeout: float = HEALTH_TIMEOUT,
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.state_path = Path(state_path)
        # Resolved here rather than as default arguments. A default is bound
        # when the function is defined, so `_run` would keep pointing at the
        # original even after the module attribute is replaced - which means a
        # test that thinks it has stubbed git out has not, and runs git for
        # real against whatever repository it is pointed at.
        self._run = runner if runner is not None else _run
        self._health = health_check if health_check is not None else player_is_healthy
        self._persistence = persistence if persistence is not None else check_persistence
        self.extras = extras
        self._clock = clock
        self.health_timeout = health_timeout

    # -- state, which is on disk so a reloaded page can find it -------------
    def state(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"phase": "idle", "stage": "", "message": "", "boots": 0}
        return data if isinstance(data, dict) else {"phase": "idle", "stage": ""}

    def _write_state(self, **changes: Any) -> None:
        from .configwrite import atomic_write_text

        data = self.state()
        data.update(changes)
        data["updated_at"] = self._clock()
        try:
            atomic_write_text(self.state_path, json.dumps(data, indent=1))
        except OSError:
            log.warning("could not record update progress", exc_info=True)

    def _age(self, state: Dict[str, Any]) -> float:
        """How long since anything last wrote to the state file, in seconds.

        Taken as a distance rather than a difference. A box with no
        battery-backed clock comes up in 1970 and jumps forward when the
        network arrives, so a stamp "in the future" is a clock that moved, not
        an update that is still going.
        """
        stamp = state.get("updated_at")
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            return float("inf")     # written by something that never stamped it
        return abs(self._clock() - float(stamp))

    # -- the update --------------------------------------------------------
    def apply(self, version: Any) -> Dict[str, Any]:
        """Move this box to ``version``, or put it back the way it was."""
        if not isinstance(version, str) or not _VERSION.match(version):
            # Not a ref, not a branch, not a flag. Only a version number.
            raise UpdateError(f"{version!r} is not a version this box can move to")

        running = self.state()
        if running.get("phase") == "running":
            age = self._age(running)
            if age <= self.STALE_AFTER_SECONDS:
                raise UpdateError("an update is already running on this box")
            # Nothing has touched this for the best part of an hour, so nothing
            # is running: the box was switched off mid-update, or the dashboard
            # was restarted under it. Refusing for ever would leave a box that
            # can never be updated again, including out of the bug that would
            # fix it, with no shell and no button to clear it.
            log.warning(
                "the update to %s was left part-finished %.0f minutes ago and "
                "nothing is running now; starting a fresh one",
                running.get("to_version"), age / 60.0,
            )

        self._write_state(
            phase="running", stage="checking", message="Checking this box can be updated.",
            to_version=version, error=None, started_at=self._clock(), boots=0,
        )

        # 1. Will a change here still be here tomorrow? Asked FIRST, because
        #    on an overlayroot box everything below succeeds and then vanishes.
        persistence = self._persistence(self.repo_dir)
        if not persistence.persists or not persistence.writable:
            reason = persistence.reason or "this box's filesystem will not keep the change"
            self._write_state(
                phase="failed", stage="checking", error=reason,
                message=(
                    "Nothing was changed. " + reason + " Make the root filesystem "
                    "writable (undo overlayroot) and try again."
                ),
            )
            raise UpdateError(reason)

        # 2. Where we came from. Recorded before anything moves, because it is
        #    the only way back.
        previous = self._current_ref()
        self._write_state(
            stage="preparing", previous_ref=previous, from_version=_strip(previous),
            message=f"Preparing to update from {_strip(previous)} to {version}.",
        )

        tag = f"v{version}"

        # Fetching touches nothing but .git, so a failure here is not a failed
        # update - it is an update that never started. Rolling back from it
        # would reinstall and restart the television because GitHub was
        # briefly unreachable, which is a far worse outcome than doing nothing.
        try:
            self._step("downloading", f"Downloading {version}.",
                       ["git", "fetch", "--tags", "--force"])
        except UpdateError as exc:
            self._write_state(
                phase="failed", stage="downloading", error=str(exc),
                finished_at=self._clock(),
                message=(
                    "Could not download the update, so nothing was changed. "
                    "This box is still running "
                    f"{_strip(previous)} and is working normally."
                ),
            )
            raise

        try:
            log.warning(
                "update: hard-resetting %s to %s - any local modifications to the "
                "installed copy are being discarded", self.repo_dir, tag,
            )
            self._step("downloading", f"Switching to {version}.",
                       ["git", "checkout", "--force", tag])
            self._step("installing", "Installing. This is the slow part.",
                       self._pip_command())
        except UpdateError as exc:
            return self._roll_back(previous, version, str(exc))

        # 3. Restart, then insist on seeing the picture come back.
        self._write_state(stage="restarting", message="Restarting the television.")
        self._run(["sudo", "-n", "systemctl", "restart", "retrobox.service"])

        self._write_state(
            stage="health", message="Waiting for the television to come back.",
        )
        if not self._health(self.health_timeout):
            return self._roll_back(
                previous, version, "the television did not come back up",
            )

        # 4. Installed, and on probation - not finished. All the health check
        #    above proves is that the television came back on a box that was
        #    already switched on. What strands a box in the field is the next
        #    cold start: a dependency that resolved badly, a migration that
        #    never ran. Nothing runs a timer while a box is off at the wall, so
        #    the next few start-ups are the only chance to notice, and on_boot()
        #    is what takes it.
        back_to = _strip(previous) or "the previous version"
        self._write_state(
            phase="probation", stage="done", to_version=version,
            error=None, finished_at=self._clock(),
            message=(
                f"Updated to {version}. The television is back on. This box "
                f"will check the next few times it starts up, and put "
                f"{back_to} back by itself if the new version stops coming up."
            ),
            boots=0,
        )
        log.info("updated to %s; on probation for the next %d start-ups",
                 version, self.MAX_BOOT_ATTEMPTS)
        return self.state()

    def _pip_command(self) -> list:
        # The same extras the box already has. Installing without them would
        # quietly remove mpv or Flask from a working television.
        target = f".[{self.extras}]" if self.extras else "."
        return [str(self.repo_dir / ".venv" / "bin" / "python"), "-m", "pip",
                "install", "-e", target]

    def _step(self, stage: str, message: str, cmd: Sequence[str]) -> None:
        self._write_state(stage=stage, message=message)
        code, output = self._run(cmd, cwd=self.repo_dir)
        if code != 0:
            raise UpdateError(f"{' '.join(cmd[:2])} failed: {output[:400]}")

    def _current_ref(self) -> str:
        code, output = self._run(
            ["git", "describe", "--tags", "--exact-match"], cwd=self.repo_dir
        )
        if code == 0 and output.strip():
            return output.strip()
        code, output = self._run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir)
        return output.strip() if code == 0 else ""

    # -- the way back ------------------------------------------------------
    def _roll_back(self, previous: str, attempted: str, why: str) -> Dict[str, Any]:
        """Put the old version back and say so plainly."""
        log.error("update to %s failed (%s); rolling back to %s", attempted, why, previous)
        self._write_state(
            phase="rolling_back", stage="installing",
            message=f"That did not work. Putting {_strip(previous)} back.",
            error=why,
        )
        if previous:
            self._run(["git", "reset", "--hard", previous], cwd=self.repo_dir)
            # Reinstall as well: the old code with the new version's
            # dependencies in the venv is its own broken state.
            self._run(self._pip_command(), cwd=self.repo_dir)
        self._run(["sudo", "-n", "systemctl", "restart", "retrobox.service"])

        back_to = _strip(previous) or "the previous version"
        self._write_state(
            phase="rolled_back", stage="done", error=why, finished_at=self._clock(),
            boots=0,
            message=(
                f"The update to {attempted} did not work ({why}), so this box put "
                f"{back_to} back by itself. It is working normally and nothing was "
                f"lost - your channels, settings and videos are untouched."
            ),
        )
        raise UpdateError(
            f"the update to {attempted} failed and was rolled back to {back_to}"
        )

    def rollback_now(self) -> Dict[str, Any]:
        """The manual rollback button: go back to the recorded previous ref."""
        state = self.state()
        previous = state.get("previous_ref")
        if not previous:
            raise UpdateError("this box has no previous version recorded to go back to")
        persistence = self._persistence(self.repo_dir)
        if not persistence.persists:
            raise UpdateError(persistence.reason or "this filesystem will not keep the change")
        try:
            self._roll_back(previous, state.get("to_version") or "the current version",
                            "you asked to go back")
        except UpdateError:
            pass
        return self.state()

    # -- boots on a new version --------------------------------------------
    def on_boot(self) -> Dict[str, Any]:
        """Called when the dashboard starts. Confirms or abandons an update.

        Nothing runs a timer while a box is switched off, so start-up is the
        only chance to notice that the new version does not come up. Called
        from :func:`check_at_boot`, which is what ``retrobox-web`` runs before
        it starts serving - see :mod:`retrobox.webservice`.
        """
        state = self.state()
        phase = state.get("phase")

        # Nothing survives the wall switch. An update or a rollback that the
        # state file still calls in-progress is not in progress: it is whatever
        # the power cut left behind, and it has to be dealt with before
        # anything else looks at this file.
        if phase in ("running", "rolling_back"):
            return self._finish_what_was_interrupted(state)

        if phase != "probation":
            return state

        # Written down before the wait, not after. Waiting for the picture
        # takes up to a minute and a half, and somebody looking at a blank
        # television is quite likely to switch the box off inside that. If the
        # attempt were only recorded at the end, none of those starts would
        # count and the box would never reach the point of giving up.
        boots = int(state.get("boots") or 0) + 1
        self._write_state(boots=boots)
        if self._health(self.health_timeout):
            log.info("the new version came up cleanly; confirming it")
            self._write_state(
                phase="success", stage="done", boots=0,
                message=f"Updated to {state.get('to_version')}. The television is back on.",
            )
            return self.state()

        if boots < self.MAX_BOOT_ATTEMPTS:
            log.warning(
                "the television did not come up (attempt %d of %d) - trying again "
                "on the next boot before giving up", boots, self.MAX_BOOT_ATTEMPTS,
            )
            back_to = _strip(state.get("previous_ref")) or "the previous version"
            self._write_state(message=(
                f"The television has not come up since the update to "
                f"{state.get('to_version')} ({boots} of {self.MAX_BOOT_ATTEMPTS} "
                f"start-ups). If it does not come up next time, this box will "
                f"put {back_to} back by itself."
            ))
            return self.state()

        log.error(
            "the television has failed to come up %d times on %s; rolling back",
            boots, state.get("to_version"),
        )
        try:
            self._roll_back(
                state.get("previous_ref") or "",
                state.get("to_version") or "the new version",
                f"the television did not come up after {boots} restarts",
            )
        except UpdateError:
            pass
        return self.state()

    def _finish_what_was_interrupted(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Clear up an update - or a rollback - that lost power part way.

        The clone may be on the new tag with the old venv, or the old tag with
        the new venv, and there is no way to tell which from here. The recorded
        previous ref is the one state this box is known to have worked in, so
        that is where it goes, reinstalled to match.
        """
        attempted = state.get("to_version") or "the new version"
        previous = state.get("previous_ref")

        if not previous:
            # It stopped before it had written down a way back, which is also
            # before it had touched the clone: nothing was changed, so there is
            # nothing to undo. All that is needed is for the file to stop
            # saying an update is running, or it refuses every update after it.
            log.warning(
                "the update to %s was interrupted before it changed anything", attempted
            )
            self._write_state(
                phase="failed", stage="checking", boots=0,
                error="the update was interrupted", finished_at=self._clock(),
                message=(
                    f"The update to {attempted} was interrupted - the box was "
                    f"switched off part way through. Nothing was changed and "
                    f"nothing was lost. You can try again whenever you like."
                ),
            )
            return self.state()

        log.error(
            "this box came up in the middle of %s %s; putting %s back",
            "an update to" if state.get("phase") == "running" else "a rollback from",
            attempted, previous,
        )
        try:
            self._roll_back(
                previous, attempted, "the box was switched off part way through",
            )
        except UpdateError:
            pass
        return self.state()


def _strip(ref: Any) -> str:
    text = str(ref or "")
    return text[1:] if text[:1].lower() == "v" else text[:12]


# ==========================================================================
# What a service calls when it starts
# ==========================================================================
def installed_extras() -> str:
    """Which optional extras this box already has, so a reinstall keeps them.

    Reinstalling without them would quietly remove mpv or Flask from a working
    television, so it is worked out from what actually imports rather than
    assumed.
    """
    import importlib.util

    found = []
    if importlib.util.find_spec("mpv") or importlib.util.find_spec("evdev"):
        found.append("hardware")
    if importlib.util.find_spec("flask"):
        found.append("web")
    return ",".join(found)


def check_at_boot(
    *,
    repo_dir: Any,
    state_path: Any,
    extras: Optional[str] = None,
    health_timeout: float = HEALTH_TIMEOUT,
) -> Dict[str, Any]:
    """Run this box's start-up update check. Never raises.

    Builds the :class:`Updater` here rather than making every caller know how,
    and swallows everything: whatever calls this is a service coming up, and a
    box with no dashboard because the update check threw is a box nobody can
    reach.

    Note there is no ``runner`` argument and no default that captures one. The
    Updater resolves the module's own ``_run`` when it is constructed - which
    is when this is called - so a test that has replaced it gets the
    replacement. See ``tests/conftest.py`` for why that sentence exists.
    """
    try:
        updater = Updater(
            repo_dir=Path(repo_dir),
            state_path=Path(state_path),
            extras=installed_extras() if extras is None else extras,
            health_timeout=health_timeout,
        )
        return updater.on_boot()
    except AssertionError:
        # Never swallowed. tests/conftest.py raises one of these when a
        # destructive command gets loose towards the real checkout, and a
        # safety net that ends up as a WARNING nobody reads is not one. On a
        # box this runs on its own thread, so nothing here can stop the
        # dashboard coming up either way.
        raise
    except Exception:  # noqa: BLE001 - a service is starting; it starts anyway
        log.warning("the start-up update check did not finish", exc_info=True)
        return {}


def start_boot_check(
    *,
    repo_dir: Any,
    state_path: Any,
    extras: Optional[str] = None,
    health_timeout: float = HEALTH_TIMEOUT,
) -> threading.Thread:
    """:func:`check_at_boot` on its own thread, handed back so a test can wait.

    On its own thread because it waits up to a minute and a half for the
    television to come up, and the dashboard is the one thing somebody with a
    sick box can still reach. It may not be held up by this.
    """
    thread = threading.Thread(
        target=check_at_boot,
        kwargs={
            "repo_dir": repo_dir, "state_path": state_path,
            "extras": extras, "health_timeout": health_timeout,
        },
        name="update-boot-check",
        daemon=True,
    )
    thread.start()
    return thread


__all__ = [
    "HEALTH_TIMEOUT",
    "Persistence",
    "UpdateError",
    "Updater",
    "check_at_boot",
    "check_persistence",
    "installed_extras",
    "player_is_healthy",
    "start_boot_check",
]
