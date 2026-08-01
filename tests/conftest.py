"""Safety nets for the whole suite.

The update tests drive git, pip and systemctl. If a stub is ever wired up
wrongly - a default argument bound at import time, a patch applied to the
wrong module - those commands run for real, in this checkout, as this user.
``git reset --hard`` against the working tree destroys everything uncommitted,
and it very nearly did.

So rather than trusting every test to stub correctly, the dangerous commands
are blocked here for the whole suite. A test that reaches this guard has a bug
in it, and gets told so loudly instead of eating somebody's afternoon.
"""

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Commands that change something outside the test's own temp directory.
_DESTRUCTIVE = (
    ("git", "reset"),
    ("git", "checkout"),
    ("git", "clean"),
    ("git", "fetch"),
    ("git", "pull"),
    ("git", "push"),
    ("systemctl",),
    ("reboot",),
    ("poweroff",),
    ("shutdown",),
)


def _is_destructive(argv):
    parts = [str(a) for a in argv if not str(a).startswith("-")]
    if not parts:
        return False
    name = Path(parts[0]).name
    if name == "sudo":
        parts = parts[1:]
        name = Path(parts[0]).name if parts else ""
    for pattern in _DESTRUCTIVE:
        if name != pattern[0]:
            continue
        if len(pattern) == 1:
            return True
        if len(parts) > 1 and parts[1] == pattern[1]:
            return True
    return False


@pytest.fixture(autouse=True)
def never_touch_the_real_repository(monkeypatch, request):
    """Refuse to run a destructive command against this checkout.

    Anything writing inside its own tmp_path is fine; this only fires for the
    real repository or an unqualified command that would inherit the current
    working directory.
    """
    real_run = subprocess.run

    def guarded(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and _is_destructive(argv):
            where = Path(kwargs.get("cwd") or Path.cwd()).resolve()
            if where == REPO or REPO in where.parents:
                raise AssertionError(
                    f"test {request.node.name!r} tried to run {' '.join(map(str, argv))!r} "
                    f"in {where} - the real checkout. Stub it out; this would "
                    f"have changed the working tree."
                )
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
    yield
