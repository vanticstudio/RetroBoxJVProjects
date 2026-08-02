"""The sudo rules the installer puts on a box, which cannot be installed here.

A unit that had been sold played video perfectly and had half its dashboard
dead. Only ``/etc/sudoers.d/retrobox-poweroff`` was on it, so **Shut down**
worked while **Restart**, **Reboot** and every setting on the **Network** page
came back with ``sudo: interactive authentication is required`` - words that
mean nothing to somebody who owns a television and cannot be talked through a
log file. The installer had printed a warning about it weeks earlier, on a
screen nobody was watching, and then reported success.

So the rule is not optional any more, and "visudo parsed it" is not accepted as
"sudo will act on it" - those are different questions and the difference is
exactly how this shipped.

None of it can be reproduced on a laptop: there is no ``visudo`` here, no
``/etc/sudoers.d`` anybody may write to and no systemd. What can be done is to
run the installer's own shell functions with a stand-in ``sudo`` first on PATH.
That stand-in records every call, keeps its own throwaway sudoers directory,
and answers ``sudo -l`` by reading the fragments that have actually been
installed into it - so "does this rule grant that command" is asked of the file
on disk rather than of a flag in the test. It never executes anything it is
handed and refuses any path under ``/etc`` it does not recognise, so nothing
here can reach the real machine's sudoers.
"""

import getpass
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SERVICE = ROOT / "scripts" / "install-service.sh"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"

#: A stand-in for sudo. Everything it is asked to do is recorded and answered
#: from a throwaway directory; nothing is ever executed with privilege, and a
#: path under /etc that is not the throwaway sudoers directory is refused
#: outright rather than passed through.
STUB_SUDO = r"""#!/usr/bin/env bash
# Stand-in for sudo, used by tests/test_install_sudoers.py. Never execs.
set -uo pipefail

printf '%s\n' "$*" >> "${STUB_LOG}"

args=("$@")
listing=0
about=""
i=0
while [[ ${i} -lt ${#args[@]} ]]; do
  case "${args[${i}]}" in
    -l) listing=1 ;;
    -U) i=$((i + 1)); about="${args[${i}]}" ;;
    --) i=$((i + 1)); break ;;
    -*) ;;
    *) break ;;
  esac
  i=$((i + 1))
done
rest=("${args[@]:${i}}")

# /etc/sudoers.d/x becomes the throwaway copy of x. Anything else under /etc
# is a bug in the script under test and is refused loudly.
remap() {
  case "$1" in
    /etc/sudoers.d/*) printf '%s/%s\n' "${STUB_SUDOERS_DIR}" "${1##*/}" ;;
    /etc/*) printf 'REFUSED\n' ;;
    *) printf '%s\n' "$1" ;;
  esac
}

if [[ ${listing} -eq 1 ]]; then
  # "would you run this without a password?" answered from the fragments that
  # are actually installed. Every entry in a generated rule is a full path, so
  # the command as the code spells it appears after a slash. A fragment named
  # in STUB_IGNORED is one real sudo would skip for having the wrong
  # permissions - silently, which is the point of it.
  want="${rest[*]}"
  # dotglob because real sudo reads the directory itself and sees the names
  # beginning with a dot as well - it skips them for the dot, not for being
  # hidden, and the difference is the whole reason a half-written rule is
  # parked under one.
  shopt -s dotglob nullglob
  for fragment in "${STUB_SUDOERS_DIR}"/*; do
    [[ -f "${fragment}" ]] || continue
    # Real sudo reads NOTHING in sudoers.d whose name contains a dot. The
    # installer parks a half-written file under such a name on purpose, so the
    # stand-in has to skip them too or it would answer yes on the strength of
    # a file the box itself would never read.
    [[ "${fragment##*/}" == *.* ]] && continue
    if [[ -f "${STUB_IGNORED:-/nonexistent}" ]] &&
       grep -qxF -- "${fragment##*/}" "${STUB_IGNORED}"; then
      continue
    fi
    if grep -qF -- "/${want}" "${fragment}"; then
      exit 0
    fi
  done
  printf 'sudo: a password is required\n' >&2
  exit 1
fi

last=$((${#rest[@]} - 1))
case "${rest[0]:-}" in
  visudo)
    if [[ "${STUB_VISUDO_EXIT:-0}" != "0" ]]; then
      printf 'visudo: syntax error near line 4\n' >&2
      exit "${STUB_VISUDO_EXIT}"
    fi
    # STUB_VISUDO_REJECT names one fragment rather than all of them: a box
    # where the SECOND rule is the bad one is the case that shows whether
    # "nothing has been changed" was true when it was printed.
    if [[ -n "${STUB_VISUDO_REJECT:-}" ]] &&
       grep -qF -- "${STUB_VISUDO_REJECT}" "${rest[${last}]}"; then
      printf 'visudo: syntax error near line 4\n' >&2
      exit 1
    fi
    exit 0
    ;;
  install)
    src="${rest[$((last - 1))]}"
    dst="$(remap "${rest[${last}]}")"
    if [[ "${dst}" == "REFUSED" ]]; then
      printf 'stub sudo: refusing to write %s\n' "${rest[${last}]}" >&2
      exit 111
    fi
    cp "${src}" "${dst}"
    # `install -m 440 -o root -g root` is what makes sudo willing to read a
    # fragment again, so writing one clears the pretend permission problem.
    if [[ -f "${STUB_IGNORED:-/nonexistent}" ]]; then
      rm -f "${STUB_IGNORED}"
    fi
    exit 0
    ;;
  mv)
    src="$(remap "${rest[$((last - 1))]}")"
    dst="$(remap "${rest[${last}]}")"
    if [[ "${src}" == "REFUSED" || "${dst}" == "REFUSED" ]]; then
      printf 'stub sudo: refusing to move onto %s\n' "${rest[${last}]}" >&2
      exit 111
    fi
    mv -f "${src}" "${dst}"
    exit 0
    ;;
  cmp)
    a="$(remap "${rest[$((last - 1))]}")"
    b="$(remap "${rest[${last}]}")"
    if [[ "${a}" == "REFUSED" || "${b}" == "REFUSED" ]]; then
      exit 111
    fi
    if cmp -s "${a}" "${b}"; then
      exit 0
    fi
    exit 1
    ;;
esac
exit 0
"""


class Box:
    """A throwaway machine: a stand-in sudo on PATH and its own sudoers.d."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.sudoers_dir = tmp_path / "sudoers.d"
        self.sudoers_dir.mkdir()
        self.staging = tmp_path / "staging"
        self.staging.mkdir()
        self.log = tmp_path / "sudo.log"
        self.log.write_text("", encoding="utf-8")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "sudo"
        stub.write_text(STUB_SUDO, encoding="utf-8")
        stub.chmod(0o755)
        self.bin_dir = bin_dir
        self._staged = 0

    def run(self, snippet, **env_extra):
        """Source the installer for its functions, then run one line of shell."""
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "RETROBOX_SUDOERS_LIB_ONLY": "1",
            "STUB_LOG": str(self.log),
            "STUB_SUDOERS_DIR": str(self.sudoers_dir),
        })
        env.update({k: str(v) for k, v in env_extra.items()})
        script = f"source {shlex.quote(str(INSTALL_SERVICE))}\n{snippet}\n"
        return subprocess.run(
            ["bash", "-c", script], cwd=str(self.root), env=env,
            capture_output=True, text=True, timeout=120,
        )

    def stage(self, text):
        """A file holding the content a fragment is supposed to have."""
        self._staged += 1
        path = self.staging / f"fragment-{self._staged}"
        path.write_text(text, encoding="utf-8")
        return path

    @property
    def calls(self):
        return [
            line for line in self.log.read_text(encoding="utf-8").splitlines() if line
        ]

    def installs(self):
        return [line for line in self.calls if line.startswith("install ")]

    def fragment(self, name):
        path = self.sudoers_dir / name
        return path.read_text(encoding="utf-8") if path.exists() else None


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def rule_for(box, function, user="retro"):
    """The text one of the installer's rule generators produces."""
    result = box.run(f"{function} {user}")
    assert result.returncode == 0, result.stderr
    return result.stdout


def system_rule(box, user="retro"):
    out = box.staging / "generated"
    result = box.run(
        f"generate_system_sudoers {shlex.quote(str(VENV_PYTHON))} {user} "
        f"{shlex.quote(str(out))}"
    )
    assert result.returncode == 0, result.stderr
    return out.read_text(encoding="utf-8")


def dashboard_commands():
    """Every command the dashboard runs as root, as sudo is asked for it."""
    sys.path.insert(0, str(ROOT))
    from retrobox.servicectl import COMMANDS
    return [" ".join(argv[2:]) for argv in COMMANDS.values()]


# ==========================================================================
# A rule that is not valid must never be installed, and must not be shrugged off
# ==========================================================================
def test_a_sudo_rule_that_visudo_will_not_parse_stops_the_install(box):
    # The check itself is right and stays: a malformed file in /etc/sudoers.d
    # breaks sudo for everything, on a box whose only way back to root is
    # sudo. What changes is what happens next - it used to print a warning and
    # carry on to report success.
    staged = box.stage("retro ALL=(root) NOPASSWD (((\n")
    result = box.run(
        f"sudoers_ensure retrobox-system {shlex.quote(str(staged))} "
        f'"the dashboard cannot restart the box" retro "systemctl reboot"',
        STUB_VISUDO_EXIT="1",
    )
    assert result.returncode != 0, "an unusable sudo rule must fail the install"
    assert box.fragment("retrobox-system") is None, (
        "the rule failed to parse and must not have been installed anyway"
    )
    assert "NOT installed" in result.stderr


def test_the_words_a_person_sees_say_what_broke_and_that_nothing_changed(box):
    staged = box.stage("retro ALL=(root) NOPASSWD (((\n")
    result = box.run(
        f"sudoers_ensure retrobox-system {shlex.quote(str(staged))} "
        f'"the dashboard cannot restart the box" retro "systemctl reboot"',
        STUB_VISUDO_EXIT="1",
    )
    said = result.stderr
    assert "error:" in said
    assert "syntax error near line 4" in said, "quote what visudo actually said"
    assert "Nothing has been changed" in said
    assert "the dashboard cannot restart the box" in said, (
        "say what stops working, in words somebody who owns one would use"
    )


# ==========================================================================
# Parsing is not granting
# ==========================================================================
def test_a_sudo_rule_that_parses_but_grants_nothing_is_caught_after_installing(box):
    # visudo is happy with a file of comments. sudo will not restart anything
    # because of it. Telling those two apart is the whole of this task.
    staged = box.stage("# a comment, and nothing else\n")
    result = box.run(
        f"sudoers_ensure retrobox-system {shlex.quote(str(staged))} "
        f'"the dashboard cannot restart the box" retro "systemctl reboot"'
    )
    assert result.returncode != 0, (
        "a rule that parses but does not grant must fail the install"
    )
    assert "systemctl reboot" in result.stderr, "name the command that is refused"
    assert "a password is required" in result.stderr, "quote what sudo said"


def test_the_check_covers_every_command_the_dashboard_runs_not_just_one(box):
    # The box this was found on had only the old poweroff rule, so Shut down
    # worked and everything else did not. A check that asked about one command
    # would have called that box healthy and shipped it.
    poweroff_only = rule_for(box, "poweroff_sudoers_rule")
    staged = box.stage(poweroff_only)
    commands = " ".join(shlex.quote(c) for c in dashboard_commands())
    result = box.run(
        f"sudoers_ensure retrobox-system {shlex.quote(str(staged))} "
        f'"the dashboard cannot restart the box" retro {commands}'
    )
    assert result.returncode != 0
    assert "systemctl restart retrobox.service" in result.stderr, (
        "the one command the broken box could run must not be the only one asked about"
    )


def test_nothing_is_ever_run_to_prove_the_rule_works(box):
    # There is no side-effect-free entry in servicectl.COMMANDS: all four
    # restart, reboot or power off the box. So the check asks sudo whether it
    # would allow them - `sudo -l` - and runs none of them. An installer that
    # proved the reboot button worked by rebooting the box would be its own
    # bug report.
    rule = system_rule(box)
    staged = box.stage(rule)
    commands = " ".join(shlex.quote(c) for c in dashboard_commands())
    result = box.run(
        f"sudoers_ensure retrobox-system {shlex.quote(str(staged))} "
        f'"the dashboard cannot restart the box" retro {commands}'
    )
    assert result.returncode == 0, result.stderr
    for call in box.calls:
        if call.startswith("-"):
            assert " -l " in f" {call} ", (
                f"sudo was asked to actually run something: {call!r}"
            )


def test_the_check_asks_about_the_account_the_box_runs_as_not_the_one_installing(box):
    # installer/provision.sh builds the image every unit ships with as root,
    # with SUDO_USER pointing at the box's own account. Asked plainly there,
    # sudo would be listing root's privileges - which are everything - and
    # would have said yes to a rule that grants the box nothing at all. The one
    # build where nobody is standing next to the box is the one that most needs
    # the question asked about the right account.
    result = box.run(
        f"install_sudo_rules {shlex.quote(str(VENV_PYTHON))} retro "
        f"{shlex.quote(str(box.staging))}"
    )
    assert result.returncode == 0, result.stderr
    listings = [call for call in box.calls if " -l " in f" {call} "]
    assert listings, "nothing was checked at all"
    for call in listings:
        assert "-U retro" in call, (
            f"sudo was asked about whoever ran the installer, not the box: {call!r}"
        )


def test_installing_for_yourself_does_not_ask_sudo_for_anybody_elses_rules(box):
    # `sudo -l -U <user>` is only allowed to root, so asking that way on the
    # ordinary path - somebody typing ./scripts/install-service.sh on their own
    # box - would fail every install with a permissions error.
    me = getpass.getuser()
    if not re.match(r"\A[a-z_][a-z0-9_-]{0,31}\Z", me):
        pytest.skip(f"{me!r} is not a name servicectl.sudoers_rule will vouch for")
    result = box.run(
        f"install_sudo_rules {shlex.quote(str(VENV_PYTHON))} {shlex.quote(me)} "
        f"{shlex.quote(str(box.staging))}"
    )
    assert result.returncode == 0, result.stderr
    listings = [call for call in box.calls if " -l " in f" {call} "]
    assert listings, "nothing was checked at all"
    for call in listings:
        assert " -U " not in f" {call} ", (
            f"an ordinary account cannot ask sudo about anybody, even itself: {call!r}"
        )


# ==========================================================================
# The box that is already broken, and the box that is already right
# ==========================================================================
def test_re_running_the_installer_on_a_healthy_box_writes_nothing(box):
    (box.sudoers_dir / "retrobox-system").write_text(
        system_rule(box), encoding="utf-8"
    )
    (box.sudoers_dir / "retrobox-poweroff").write_text(
        rule_for(box, "poweroff_sudoers_rule"), encoding="utf-8"
    )
    result = box.run(
        f"install_sudo_rules {shlex.quote(str(VENV_PYTHON))} retro "
        f"{shlex.quote(str(box.staging))}"
    )
    assert result.returncode == 0, result.stderr
    assert box.installs() == [], (
        "a box that is already right must not have its sudo rules rewritten"
    )
    assert "already up to date" in result.stdout


def test_a_box_with_only_the_old_power_off_rule_is_repaired_by_re_running(box):
    # The real unit in the field. It installed cleanly, booted cleanly and had
    # no /etc/sudoers.d/retrobox-system at all.
    (box.sudoers_dir / "retrobox-poweroff").write_text(
        rule_for(box, "poweroff_sudoers_rule"), encoding="utf-8"
    )
    result = box.run(
        f"install_sudo_rules {shlex.quote(str(VENV_PYTHON))} retro "
        f"{shlex.quote(str(box.staging))}"
    )
    assert result.returncode == 0, result.stderr
    assert box.fragment("retrobox-system") == system_rule(box), (
        "re-running the installer is how a box in the field gets fixed"
    )


def test_a_rule_that_is_there_but_being_ignored_is_written_again_not_skipped(box):
    # The contents are not the whole story. sudo skips a fragment whose
    # permissions or owner are wrong and says nothing at all about it, so
    # "the file already matches" is not the same as "the box works". If the
    # check fails after nothing was written, write it properly and ask again
    # before giving up - otherwise re-running the installer, which is the whole
    # of the advice anybody gets, could never fix that box.
    rule = system_rule(box)
    (box.sudoers_dir / "retrobox-system").write_text(rule, encoding="utf-8")
    ignored = box.root / "ignored-by-sudo"
    ignored.write_text("retrobox-system\n", encoding="utf-8")
    staged = box.stage(rule)
    commands = " ".join(shlex.quote(c) for c in dashboard_commands())
    result = box.run(
        f"sudoers_ensure retrobox-system {shlex.quote(str(staged))} "
        f'"the dashboard cannot restart the box" retro {commands}',
        STUB_IGNORED=str(ignored),
    )
    assert result.returncode == 0, result.stderr
    assert "sudo was ignoring it" in result.stdout


def test_a_box_with_a_stale_rule_gets_the_current_one(box):
    # Nothing ever checked that the fragment on disk matched the code. A box
    # updated from an older release has a rule that is valid, parses, and is
    # missing the commands the newer code runs.
    stale = rule_for(box, "poweroff_sudoers_rule")
    (box.sudoers_dir / "retrobox-system").write_text(stale, encoding="utf-8")
    (box.sudoers_dir / "retrobox-poweroff").write_text(stale, encoding="utf-8")
    result = box.run(
        f"install_sudo_rules {shlex.quote(str(VENV_PYTHON))} retro "
        f"{shlex.quote(str(box.staging))}"
    )
    assert result.returncode == 0, result.stderr
    assert box.fragment("retrobox-system") == system_rule(box)


# ==========================================================================
# The older, hand-written fragment
# ==========================================================================
def test_the_old_power_off_rule_is_checked_before_it_is_installed_too(box):
    # It used to be a bare `sudo tee` with nothing looking at it, one typo away
    # from locking root out of a box whose only recovery story is sudo.
    result = box.run(
        f"install_sudo_rules {shlex.quote(str(VENV_PYTHON))} retro "
        f"{shlex.quote(str(box.staging))}",
        STUB_VISUDO_EXIT="1",
    )
    assert result.returncode != 0
    assert list(box.sudoers_dir.iterdir()) == [], "nothing may be installed unchecked"
    assert "retrobox-poweroff" in result.stderr


def test_the_old_power_off_rule_will_not_be_written_for_an_implausible_name(box):
    # The generated rule refuses a name like this (servicectl.sudoers_rule),
    # and the hand-written one beside it used to take whatever it was handed.
    # It is a file that grants root: a name with a comma, a space or a newline
    # in it does not name a user, it changes what the line means.
    result = box.run("poweroff_sudoers_rule 'ro,ot'")
    assert result.returncode != 0
    assert list(box.sudoers_dir.iterdir()) == []
    assert "ro,ot" in result.stderr, "say which name was refused"


def test_the_old_power_off_rule_still_grants_what_the_television_switches_off_with(box):
    # Why it has not been deleted now that servicectl.COMMANDS generates the
    # real one: the television turns the box off with power_off_command from
    # config.yaml, which defaults to a plain `sudo poweroff` - not
    # `systemctl poweroff`, which is the only shape the generated rule grants.
    # Volume-down-past-zero and the sleep timer both go through it.
    sys.path.insert(0, str(ROOT))
    from retrobox.config import DEFAULT_POWER_OFF_COMMAND

    rule = rule_for(box, "poweroff_sudoers_rule")
    program = DEFAULT_POWER_OFF_COMMAND[-1]
    assert any(f"{where}/{program}" in rule for where in ("/sbin", "/usr/sbin")), (
        f"the television runs {' '.join(DEFAULT_POWER_OFF_COMMAND)} and nothing "
        f"else grants it"
    )


# ==========================================================================
# "Nothing has been changed" has to be true when it is printed
# ==========================================================================
def test_a_second_rule_that_will_not_parse_leaves_the_first_one_uninstalled(box):
    # Both fragments go on together or neither does. The failure message says
    # /etc/sudoers.d is untouched, and somebody reads that off a television and
    # decides they can walk away from a box mid-install. It is either true or
    # it should not be printed - and the fragment that fails is not always the
    # first one looked at.
    result = box.run(
        f"install_sudo_rules {shlex.quote(str(VENV_PYTHON))} retro "
        f"{shlex.quote(str(box.staging))}",
        STUB_VISUDO_REJECT="set-timezone",   # only the generated rule has it
    )
    assert result.returncode != 0
    assert list(box.sudoers_dir.iterdir()) == [], (
        "the install stopped, so it must not have left one of the two rules behind"
    )
    assert "Nothing has been changed" in result.stderr


def test_a_rule_that_cannot_be_generated_leaves_the_first_one_uninstalled_too(box):
    # Same promise, made by a different message: the one printed when the
    # Python side of the install is too broken to say what it needs.
    result = box.run(
        f"install_sudo_rules /nonexistent/python retro "
        f"{shlex.quote(str(box.staging))}"
    )
    assert result.returncode != 0
    assert list(box.sudoers_dir.iterdir()) == [], (
        "nothing may be installed before every rule has been built and checked"
    )
    assert "Nothing has been changed" in result.stderr


# ==========================================================================
# A box switched off at the wall in the middle of the install
# ==========================================================================
def test_a_rule_is_renamed_into_place_so_a_power_cut_cannot_truncate_it(box):
    # These boxes are switched off at the wall. Writing over the live file
    # empties it first, and a sudoers file cut off halfway through is not a
    # missing permission - it is a syntax error, and sudo refuses EVERYTHING
    # while there is one, including the sudo somebody would need to repair it.
    # So the new text is written beside the live file and renamed onto it,
    # which the filesystem does in one step or not at all.
    result = box.run(
        f"install_sudo_rules {shlex.quote(str(VENV_PYTHON))} retro "
        f"{shlex.quote(str(box.staging))}"
    )
    assert result.returncode == 0, result.stderr

    live = ["/etc/sudoers.d/retrobox-poweroff", "/etc/sudoers.d/retrobox-system"]
    for call in box.calls:
        if not call.startswith(("install ", "cp ", "tee ")):
            continue
        for path in live:
            assert not call.endswith(f" {path}"), (
                f"the rule sudo is reading was written over in place: {call!r}"
            )

    renames = [call for call in box.calls if call.startswith("mv -f ")]
    assert len(renames) == 2, f"both rules are renamed into place, got {renames!r}"
    for call in renames:
        parked = call.split()[2]
        assert "." in parked.rsplit("/", 1)[-1], (
            f"a name sudo would read while it is half-written: {parked!r}"
        )


def test_the_half_written_file_a_power_cut_leaves_behind_is_one_sudo_skips(box):
    # Why the parked name has a dot in it. Everything in /etc/sudoers.d is
    # read, in full, on every single sudo - except names containing a dot,
    # which are skipped. That is what makes it safe to leave one lying there
    # after the power goes off mid-install.
    (box.sudoers_dir / ".retrobox-system.new").write_text(
        system_rule(box), encoding="utf-8"
    )
    result = box.run("sudoers_grants retro 'systemctl reboot'")
    assert result.returncode != 0, (
        "a parked, possibly half-written file must never be what grants root"
    )


# ==========================================================================
# Generating the rule at all
# ==========================================================================
def test_the_install_stops_when_it_cannot_work_out_which_commands_to_allow(box):
    out = box.staging / "generated"
    result = box.run(
        f"generate_system_sudoers /nonexistent/python retro {shlex.quote(str(out))}"
    )
    assert result.returncode != 0, (
        "a box that cannot generate the rule must not go on to report success"
    )
    assert "error:" in result.stderr


# ==========================================================================
# The shape of the installer itself
# ==========================================================================
def test_the_installer_has_no_branch_left_that_carries_on_without_the_rule():
    # Both of these printed a warning and let the install go on to report
    # success. That is the bug: the box was fine for weeks and then was not.
    text = INSTALL_SERVICE.read_text(encoding="utf-8")
    assert "NOT installing it" not in text
    assert "skipping" not in text.lower()
    assert "will be unavailable in the dashboard" not in text


def test_the_sudo_rules_go_on_before_anything_in_etc_systemd_is_touched():
    # So that "Nothing has been changed" in the failure message is true. If the
    # rules are done second, stopping leaves a box with a half-written idea of
    # how to start itself and no way to say so.
    lines = INSTALL_SERVICE.read_text(encoding="utf-8").splitlines()
    rules = next(
        i for i, line in enumerate(lines) if line.startswith("install_sudo_rules ")
    )
    systemd = next(
        i for i, line in enumerate(lines)
        if re.search(r"sudo (cp|tee|systemctl)\b", line)
    )
    assert rules < systemd


def test_the_rule_that_is_installed_is_the_one_the_code_generates(box):
    # Not a copy kept in the shell script: the file on disk cannot come to
    # permit more, or less, than servicectl.COMMANDS runs.
    sys.path.insert(0, str(ROOT))
    from retrobox.servicectl import sudoers_rule

    assert system_rule(box, "retro") == sudoers_rule("retro")
