"""One version, one place.

Before this, four things disagreed about what version the box was: pyproject
said 1.0.0, the code said 1.0.0, and the repo had moved three releases past
both. That was untidy right up until an update system existed, at which point
it becomes a box that believes it is permanently out of date and reinstalls
the same release forever.
"""

import re
import subprocess
from pathlib import Path

import pytest

import retrobox

REPO = Path(__file__).resolve().parent.parent


def test_the_version_is_a_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.-]+)?", retrobox.__version__), (
        retrobox.__version__
    )


def test_pyproject_does_not_carry_a_second_copy_of_the_version():
    # The two cannot disagree if there is only one of them. A literal
    # `version = "..."` in [project] is a second copy waiting to go stale.
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    assert not re.search(r'^\s*version\s*=\s*"', project, re.M), (
        "pyproject.toml declares its own version; it must derive it instead"
    )
    assert 'dynamic = ["version"]' in project


def test_the_packaging_metadata_is_the_same_number_the_code_reports():
    # The one that actually matters: what `pip show retrobox` says has to be
    # what the box tells the dashboard and the update checker.
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("retrobox")
    except PackageNotFoundError:  # pragma: no cover - not installed in this env
        pytest.skip("retrobox is not installed in this environment")
    assert installed == retrobox.__version__


def test_setuptools_reads_the_version_from_the_package():
    # Proves the derivation actually works rather than just being declared.
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = {attr = "retrobox.__version__"}' in text


def _head_tag():
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--exact-match"],
            cwd=REPO, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def test_a_tagged_commit_reports_the_version_of_its_tag():
    """The link the update system depends on.

    Every box decides whether to update by comparing its own ``__version__``
    against the newest release tag. If a release is tagged v1.1.0 while the
    code still says 1.0.3, every box in the field installs that release over
    and over, for ever.

    Skipped on ordinary commits - the check only means anything on a tag.
    """
    tag = _head_tag()
    if tag is None:
        pytest.skip("HEAD is not on a tag")
    assert tag.lstrip("v") == retrobox.__version__, (
        f"tag {tag} does not match __version__ {retrobox.__version__}"
    )
