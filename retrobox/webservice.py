"""How the dashboard starts on a box, as opposed to how it starts in a test.

``retrobox-web`` points here rather than straight at :func:`retrobox.webui.main`
because one thing has to happen before the dashboard settles into serving
pages: a box that was updated has to be told, on this start, whether the new
version actually came up. That is
:meth:`retrobox.updater.Updater.on_boot`, and until this module existed nothing
on a box ever called it - the machinery that puts the old version back after a
few bad starts was written, tested, documented, and completely dead.

Why the dashboard's process and not the television's: when an update goes
wrong, the television is the thing that does not start. Driving a rollback from
inside a process that is itself dying means a ``git reset`` and a ``pip
install`` that get killed part way through, which leaves a worse box than the
one we set out to rescue. The dashboard keeps running when the TV does not -
that is the whole reason it is a separate service - and the health check it
runs is exactly the question being asked here: is the television playing?

It is deliberately thin. Everything it does has to survive being wrong: if the
check cannot be started at all, the dashboard still comes up, because the
dashboard is the only way anybody reaches a box that has gone wrong.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


def config_path_from(argv: Optional[List[str]]) -> Optional[str]:
    """The ``--config`` the dashboard is about to use, read off the arguments.

    Not a second argument parser. A parser here that drifted from the one in
    ``webui.main`` would leave the boot check watching one state file while the
    page shows another, and the two would disagree in silence.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    for index, arg in enumerate(args):
        if arg in ("-c", "--config"):
            return args[index + 1] if index + 1 < len(args) else None
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
        if arg.startswith("-c") and len(arg) > 2 and not arg.startswith("--"):
            return arg[2:]
    return None


def update_state_path(config_path: Optional[str]) -> Path:
    """Where the update state lives, worked out the way the dashboard does it.

    Same ConfigStore, same file name, so this cannot end up looking at a
    different file to the one the update panel reads.
    """
    from .configstore import ConfigStore
    from .webui import UPDATE_STATE_NAME

    return ConfigStore(config_path or "config.yaml").path.with_name(UPDATE_STATE_NAME)


def main(argv: Optional[List[str]] = None) -> int:
    """``retrobox-web`` - the boot check, then the dashboard."""
    from . import updater
    from .webui import REPO_DIR, main as serve

    try:
        updater.start_boot_check(
            repo_dir=REPO_DIR,
            state_path=update_state_path(config_path_from(argv)),
        )
    except Exception:  # noqa: BLE001 - the dashboard comes up regardless
        log.warning("could not start this boot's update check", exc_info=True)

    return serve(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
