"""Applying an update to a box you cannot physically reach.

A box that cannot roll itself back has to be collected from a customer's
house. That is the bar everything here is written against: every step that can
fail has a way back, the way back is taken automatically, and nothing reports
success for a change that will not survive a reboot.
"""

import json

import pytest

from retrobox.updater import Persistence, UpdateError, Updater


class Runner:
    """Stands in for git, pip and systemctl, and can be told to fail."""

    def __init__(self):
        self.calls = []
        self.where = []            # the cwd each call was made in
        self.fail_on = {}          # first word of argv -> (code, output)
        self.answers = {}

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        self.where.append(kw.get("cwd"))
        key = self._key(cmd)
        if key in self.fail_on:
            return self.fail_on[key]
        return (0, self.answers.get(key, ""))

    @staticmethod
    def _key(cmd):
        parts = [p for p in cmd if not p.startswith("-")]
        if parts and parts[0].endswith("python"):
            # The updater runs the venv's python for two quite different
            # things: `-m pip` to install the new code, and `-m
            # retrobox.updater --privileges` to ask that new code whether this
            # box may still run its own privileged commands. A test has to be
            # able to fail one of those without failing the other.
            return "pip" if "pip" in cmd else "privileges"
        return " ".join(parts[:2]) if len(parts) > 1 else (parts[0] if parts else "")

    def ran(self, needle):
        return [c for c in self.calls if needle in " ".join(c)]

    def at(self, needle):
        """Where in the sequence of calls something happened, first and last."""
        found = [i for i, c in enumerate(self.calls) if needle in " ".join(c)]
        return found


def privileges_answer(state="ok", **extra):
    """What ``python -m retrobox.updater --privileges`` prints.

    A healthy box by default, which is what almost every test here wants: the
    permission the installer wrote still covers everything the new code runs.
    """
    answer = {
        "state": state,
        "applied": False,
        "headline": "This box can look after itself",
        "message": "",
        "affected": [],
        "refused": [],
        "detail": "",
        "command": "cd /home/retrobox/RetroBox && ./scripts/install-service.sh",
    }
    answer.update(extra)
    return json.dumps(answer)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def repo(tmp_path):
    directory = tmp_path / "RetroBox"
    directory.mkdir()
    return directory


@pytest.fixture
def runner():
    r = Runner()
    r.answers["git describe"] = "v1.0.3"
    # A box whose permission still covers what the new code runs, which is the
    # ordinary case. Set here for the same reason "git describe" is: it is what
    # the real command answers on a healthy box, and a test about something
    # else should not have to know that the updater asks.
    r.answers["privileges"] = privileges_answer()
    return r


@pytest.fixture
def healthy():
    return lambda timeout: True


def make(repo, runner, healthy, **kw):
    options = dict(
        repo_dir=repo,
        state_path=repo / ".retrobox-update.json",
        runner=runner,
        health_check=healthy,
        persistence=lambda p: Persistence(True, True, ""),
        extras="hardware,web",
        clock=Clock(),
    )
    options.update(kw)
    return Updater(**options)


# ==========================================================================
# A read-only root is the silent failure, so it is checked first
# ==========================================================================
def test_an_overlay_root_aborts_before_anything_is_touched(repo, runner, healthy):
    # overlayroot is offered in this product's own setup guide. On such a box
    # every step succeeds and then vanishes at the next reboot, and nothing in
    # the logs looks wrong. Reporting success for that is the worst outcome.
    updater = make(repo, runner, healthy, persistence=lambda p: Persistence(
        writable=True, persists=False, reason="the root filesystem is an overlay",
    ))
    with pytest.raises(UpdateError) as caught:
        updater.apply("1.1.0")

    assert "overlay" in str(caught.value).lower()
    assert runner.calls == [], "it started changing things anyway"
    assert updater.state()["phase"] == "failed"


def test_a_read_only_root_aborts_too(repo, runner, healthy):
    updater = make(repo, runner, healthy, persistence=lambda p: Persistence(
        writable=False, persists=False, reason="mounted read-only",
    ))
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")
    assert runner.calls == []


def test_the_message_says_what_to_do_about_it(repo, runner, healthy):
    updater = make(repo, runner, healthy, persistence=lambda p: Persistence(
        False, False, "the root filesystem is read-only (overlayroot)",
    ))
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")
    assert "overlayroot" in updater.state()["error"]


# ==========================================================================
# The happy path
# ==========================================================================
def test_an_update_fetches_checks_out_installs_and_restarts(repo, runner, healthy):
    updater = make(repo, runner, healthy)
    updater.apply("1.1.0")

    assert runner.ran("fetch"), "it never fetched the tags"
    assert runner.ran("v1.1.0"), "it never checked the tag out"
    assert runner.ran("pip"), "it never reinstalled"
    assert runner.ran("systemctl"), "it never restarted anything"
    # "probation", not "success": this used to say success, and that was the
    # bug. An update that has come back once on a box that never went off is
    # installed, not proven - see the probation tests below.
    assert updater.state()["phase"] == "probation"
    assert updater.state()["to_version"] == "1.1.0"


def test_the_current_tag_is_recorded_before_anything_changes(repo, runner, healthy):
    # That recorded ref is the only way back. It has to be taken first.
    updater = make(repo, runner, healthy)
    updater.apply("1.1.0")
    assert updater.state()["previous_ref"] == "v1.0.3"

    order = [" ".join(c) for c in runner.calls]
    assert "describe" in order[0], f"first command was {order[0]}"


def test_the_install_uses_the_extras_the_box_already_has(repo, runner, healthy):
    updater = make(repo, runner, healthy, extras="hardware,web")
    updater.apply("1.1.0")
    assert any(".[hardware,web]" in " ".join(c) for c in runner.ran("pip"))


def test_a_hard_reset_is_logged_loudly_because_it_destroys_local_changes(repo, runner, healthy, caplog):
    import logging

    updater = make(repo, runner, healthy)
    with caplog.at_level(logging.WARNING, logger="retrobox.updater"):
        updater.apply("1.1.0")
    assert any("discard" in r.message.lower() or "local" in r.message.lower()
               for r in caplog.records), "a destructive reset went by quietly"


# ==========================================================================
# When the install fails
# ==========================================================================
def test_a_failed_install_rolls_back_and_never_restarts_into_it(repo, runner, healthy):
    runner.fail_on["pip"] = (1, "could not build wheel")
    updater = make(repo, runner, healthy)

    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    # It must have gone back to the recorded ref...
    resets = [" ".join(c) for c in runner.ran("reset")]
    assert any("v1.0.3" in r for r in resets), "it did not go back"
    assert updater.state()["phase"] == "rolled_back"


def test_a_failed_checkout_rolls_back(repo, runner, healthy):
    runner.fail_on["git checkout"] = (1, "unknown revision")
    runner.fail_on["git reset"] = (0, "")
    updater = make(repo, runner, healthy)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")
    assert updater.state()["phase"] in ("rolled_back", "failed")


def test_a_failed_fetch_stops_before_touching_the_working_tree(repo, runner, healthy):
    runner.fail_on["git fetch"] = (1, "could not resolve host github.com")
    updater = make(repo, runner, healthy)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")
    assert runner.ran("reset") == [], "it reset the tree despite having nothing to reset to"


# ==========================================================================
# When the box comes back unhealthy
# ==========================================================================
def test_a_failed_health_check_rolls_back(repo, runner):
    updater = make(repo, runner, healthy=lambda timeout: False)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    assert updater.state()["phase"] == "rolled_back"
    assert any("v1.0.3" in " ".join(c) for c in runner.ran("reset"))


def test_the_rollback_reinstalls_and_restarts_too(repo, runner):
    # Going back to the old code without reinstalling it leaves a venv full of
    # the new version's dependencies, which is its own broken state.
    updater = make(repo, runner, healthy=lambda timeout: False)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    after_reset = False
    reinstalled = restarted = False
    for call in runner.calls:
        joined = " ".join(call)
        if "reset" in joined and "v1.0.3" in joined:
            after_reset = True
        elif after_reset and "pip" in Runner._key(call):
            reinstalled = True
        elif after_reset and "systemctl" in joined:
            restarted = True
    assert reinstalled and restarted


def test_the_message_after_a_rollback_says_the_box_is_working(repo, runner):
    updater = make(repo, runner, healthy=lambda timeout: False)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")
    message = updater.state()["message"].lower()
    assert "1.0.3" in message
    assert "working" in message or "back" in message


# ==========================================================================
# The permission the new code needs, which nothing used to put back
# ==========================================================================
# This is the failure that has not happened yet and arrives at every unit at
# once. servicectl.COMMANDS is the closed list of things this box may do as
# root, and the sudoers fragment is generated from it - by the installer, on
# the day the box was set up, and by nothing else since. The first release that
# adds a privileged action therefore reaches a field full of boxes running new
# code against a grant written for the old table: the update reports success,
# the television keeps playing, and the new button silently does nothing on
# every unit simultaneously, with nobody able to SSH in and find out why.
def test_an_update_puts_the_permission_back_in_step_before_it_restarts_anything(
    repo, runner, healthy
):
    updater = make(repo, runner, healthy)
    updater.apply("1.1.0")

    asked = runner.at("--privileges")
    assert asked, (
        "the update never regenerated or checked this box's sudo permission. "
        "servicectl.COMMANDS grows, and the fragment on disk was generated "
        "from whatever that table said on the day the installer last ran - so "
        "without this step a release that adds a privileged action ships a "
        "dead button to every box in the field at once."
    )

    installed = runner.at("install -e") or [
        i for i, c in enumerate(runner.calls) if Runner._key(c) == "pip"
    ]
    restarted = runner.at("systemctl")
    assert max(installed) < asked[0], (
        "the permission was worked out before the new code was installed, so "
        "it was generated from the OLD command table - which is the bug"
    )
    assert asked[0] < min(restarted), (
        "the television was restarted into a version this box has not been "
        "given permission to run"
    )


def test_the_permission_is_asked_of_the_new_code_not_of_the_running_process(
    repo, runner, healthy
):
    # The dashboard process doing the update imported retrobox.servicectl when
    # it started, months ago, and that module object is the OLD command table -
    # re-importing it under a live dashboard is not something this can do. So
    # the question has to be put to a fresh interpreter, running the code that
    # was just checked out, out of the box's own venv.
    updater = make(repo, runner, healthy)
    updater.apply("1.1.0")

    asked = runner.at("--privileges")
    argv = runner.calls[asked[0]]
    assert argv[0] == str(repo / ".venv" / "bin" / "python"), (
        f"it asked {argv[0]} rather than the box's own venv"
    )
    assert argv[1:] == ["-m", "retrobox.updater", "--privileges"]
    assert runner.where[asked[0]] == repo


def test_an_update_the_box_has_not_been_given_permission_for_is_rolled_back(
    repo, runner, healthy
):
    # "stale" is the shape this arrives in: the older, smaller grant covers
    # most of what the box does, so almost everything works and one page does
    # not. Left alone that is weeks of a customer pressing a button that does
    # nothing.
    runner.answers["privileges"] = privileges_answer(
        "stale",
        headline="This box's permission is out of date",
        affected=["saving network settings"],
    )
    updater = make(repo, runner, healthy)

    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    assert any("v1.0.3" in " ".join(c) for c in runner.ran("reset")), (
        "it left the box on a version half its dashboard cannot drive"
    )
    assert updater.state()["phase"] == "rolled_back"


def test_a_version_that_was_never_granted_anything_is_rolled_back_too(
    repo, runner, healthy
):
    runner.answers["privileges"] = privileges_answer("missing")
    updater = make(repo, runner, healthy)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")
    assert updater.state()["phase"] == "rolled_back"


def test_the_television_is_never_restarted_into_a_version_that_cannot_be_granted(
    repo, runner, healthy
):
    # The picture going off is the most alarming thing this box does. There is
    # no reason to put a customer through it for a version that is about to be
    # taken away again.
    runner.answers["privileges"] = privileges_answer("stale")
    updater = make(repo, runner, healthy)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    asked = runner.at("--privileges")[0]
    reset = runner.at("reset")[0]
    restarts = runner.at("systemctl")
    assert all(index > reset for index in restarts), (
        "it restarted the television into the new version and only then found "
        "out the box may not run it"
    )
    assert asked < reset


def test_a_rolled_back_update_tells_the_owner_the_one_command_that_fixes_it(
    repo, runner, healthy
):
    # An owner who is told only "the update did not work" tries it again, and
    # again, for ever. The fix is one line typed on the box, and it is the only
    # thing the dashboard is allowed to do about this: a rule that let an
    # unauthenticated page write sudo's own configuration would hand the box to
    # anyone on the home network.
    runner.answers["privileges"] = privileges_answer(
        "stale", command="cd /home/retrobox/RetroBox && ./scripts/install-service.sh",
    )
    updater = make(repo, runner, healthy)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    message = updater.state()["message"]
    assert "install-service.sh" in message, (
        f"nothing in this tells the owner what to do:\n{message}"
    )
    assert "1.0.3" in message, "it never says which version the box is on now"
    assert "sudo" not in message.lower(), (
        "the word sudo means nothing to somebody looking at a television"
    )


def test_a_permission_answer_that_never_arrives_is_treated_as_a_failed_update(
    repo, runner, healthy
):
    # A box that cannot answer the question has not answered it "yes". Going
    # on regardless is exactly the bug: an update that reports success and
    # leaves a dashboard that half works. Stopping and putting the old version
    # back leaves a box that works and says why.
    runner.fail_on["privileges"] = (1, "ModuleNotFoundError: No module named 'retrobox'")
    updater = make(repo, runner, healthy)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")
    assert updater.state()["phase"] == "rolled_back"


def test_a_reply_that_is_not_an_answer_is_not_taken_for_a_yes(repo, runner, healthy):
    runner.answers["privileges"] = "Traceback (most recent call last):"
    updater = make(repo, runner, healthy)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")
    assert updater.state()["phase"] == "rolled_back"


def test_a_state_this_version_has_never_heard_of_is_not_taken_for_a_yes(
    repo, runner, healthy
):
    # The answer comes from the NEW code, so it can say something this version
    # does not recognise. Anything that is not plainly "yes" has to fail shut.
    runner.answers["privileges"] = privileges_answer("something-invented-later")
    updater = make(repo, runner, healthy)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")
    assert updater.state()["phase"] == "rolled_back"


def test_a_fault_that_undoing_the_update_cannot_fix_does_not_undo_the_update(
    repo, runner, healthy
):
    """"blocked" is sudo unable to become root at all, whatever the rules say.

    That is the NoNewPrivileges= shape, it is a property of the unit file the
    dashboard is *already* running under - the update has not restarted
    anything yet when this is asked - and the previous version is every bit as
    blocked as the new one. Rolling back would cost the customer their update
    and fix nothing. So the update goes through and the fault is recorded and
    logged, because it is real and it needs a different fix.
    """
    runner.answers["privileges"] = privileges_answer("blocked")
    updater = make(repo, runner, healthy)
    updater.apply("1.1.0")

    assert updater.state()["phase"] == "probation", (
        "it undid a working update for a fault undoing it cannot touch"
    )
    assert updater.state()["privileges"] == "blocked"


def test_the_owner_is_never_shown_sudos_own_words_or_a_path_out_of_etc(
    repo, runner, healthy
):
    # The state file is served to the browser as-is by the dashboard's update
    # panel, so anything written into it is on a page. "sudo: interactive
    # authentication is required" and /etc/sudoers.d are for the journal.
    runner.answers["privileges"] = privileges_answer(
        "stale",
        refused=["/usr/bin/systemctl --no-block restart retrobox-web.service"],
        detail="1 of 21 commands refused; /etc/sudoers.d/retrobox-system is unknown",
    )
    updater = make(repo, runner, healthy)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    written = (repo / ".retrobox-update.json").read_text()
    for leak in ("sudoers.d", "/usr/bin/systemctl", "commands refused"):
        assert leak not in written, f"{leak!r} reached a page a customer reads"


def test_a_rollback_puts_the_permission_back_in_step_with_the_code_it_leaves_behind(
    repo, runner, healthy
):
    """A box left on old code with a new, wider grant is a security regression.

    Only root can rewrite the fragment, so on a shipped box nothing is ever
    written and there is nothing to undo. But the updater is also run by hand
    as root, and a box switched off at the wall mid-update finishes the job at
    the next start-up - and in both of those the fragment may have been
    regenerated from the new command table before the update failed. Whatever
    version the box actually ends up running, the grant has to match it, which
    means asking again after the old code is back.
    """
    runner.fail_on["pip"] = (1, "could not build wheel")
    updater = make(repo, runner, healthy)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    reset = runner.at("reset")[0]
    asked = [index for index in runner.at("--privileges") if index > reset]
    assert asked, (
        "the rollback left this box's sudo permission generated from the "
        "version it just took away - a grant wider than the code it is now "
        "running needs"
    )


def test_the_rule_the_update_puts_back_is_generated_from_todays_command_table(
    monkeypatch, tmp_path
):
    """The tripwire for the release that adds a privileged action.

    Every other test here drives a stand-in for the subprocess. This one runs
    the real thing against a stand-in /etc/sudoers.d, with a fragment on it
    generated from a command table one entry short - which is precisely what a
    box set up by an earlier version has, and precisely what the box this bug
    was found on had.

    If it fails, what has been forgotten is that ``servicectl.COMMANDS`` is
    the source of the sudoers fragment as well as of the commands, and that
    something in the update path has to regenerate it. A box that updates its
    code and keeps the grant the installer wrote months ago gets a button that
    silently does nothing, on every unit in the field on the same day.
    """
    from retrobox import servicectl
    from retrobox import updater as updater_module

    user = "retrobox"
    assert len(servicectl.COMMANDS) > 1

    # What a box set up by an earlier version has on disk: the same generator,
    # run against a command table with one entry missing.
    older = dict(servicectl.COMMANDS)
    older.pop(next(iter(older)))
    monkeypatch.setattr(servicectl, "COMMANDS", older)
    stale_rule = servicectl.sudoers_rule(user)
    monkeypatch.undo()

    wanted = servicectl.sudoers_rule(user)
    assert stale_rule != wanted, (
        "dropping a command from COMMANDS no longer changes the generated "
        "rule, so this test can no longer tell a stale grant from a current one"
    )

    sudoers = tmp_path / "sudoers.d"
    sudoers.mkdir()
    target = sudoers / "retrobox-system"
    target.write_text(stale_rule, encoding="utf-8")

    # A box where this process is root, visudo is happy, and no privileged
    # command exists to be probed - so nothing runs sudo and nothing runs.
    monkeypatch.setattr(servicectl, "SUDOERS_PATH", str(target))
    monkeypatch.setattr(servicectl, "_am_root", lambda: True)
    monkeypatch.setattr(servicectl, "current_user", lambda: user)
    monkeypatch.setattr(
        servicectl, "_first_existing",
        lambda paths: "/usr/bin/visudo" if paths is servicectl._VISUDO_PATHS else None,
    )
    monkeypatch.setattr(servicectl, "_run", lambda cmd, **kw: (0, ""))

    answer = updater_module.refresh_privileges()

    assert target.read_text(encoding="utf-8") == wanted, (
        "the update path did not regenerate /etc/sudoers.d/retrobox-system "
        "from today's servicectl.COMMANDS. Whoever added a privileged command "
        "also has to make sure the update regenerates the rule - the "
        "installer writes it once, on the day the box is set up, and nothing "
        "else ever has."
    )
    assert answer["applied"] is True


def test_asking_about_the_permission_never_runs_any_of_the_commands_it_asks_about(
    monkeypatch, tmp_path
):
    # An installer that proved the reboot button worked by rebooting the box
    # would be its own bug report. The same goes for an update.
    from retrobox import servicectl
    from retrobox import updater as updater_module

    ran = []

    def watch(cmd, **kw):
        ran.append(list(cmd))
        return (0, "")

    monkeypatch.setattr(servicectl, "_run", watch)
    monkeypatch.setattr(servicectl, "_am_root", lambda: False)
    updater_module.refresh_privileges()

    for call in ran:
        assert "-l" in call, f"{' '.join(call)} was run, not asked about"


def test_the_privileges_answer_is_one_line_of_json_on_its_own(capsys, monkeypatch):
    # It is read back by a process that has only the exit status and the
    # output to go on. Anything else on stdout makes it unreadable.
    from retrobox import servicectl
    from retrobox import updater as updater_module

    # No real sudo. This runs on a laptop, and asking the developer's own
    # machine about its sudo rules is not this test's business.
    monkeypatch.setattr(servicectl, "_run", lambda cmd, **kw: (0, ""))
    monkeypatch.setattr(servicectl, "_am_root", lambda: False)

    assert updater_module.main(["--privileges"]) == 0
    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) == 1, printed
    answer = json.loads(printed[0])
    assert set(answer) >= {"state", "applied", "message", "command"}


# ==========================================================================
# A rollback that says it worked has to have worked
# ==========================================================================
def test_a_rollback_whose_reset_failed_does_not_say_the_box_is_working_normally(
    repo, runner
):
    # This is read off a television by somebody deciding whether the box can be
    # left alone until the morning. If the old files never went back, it can
    # not, and telling them it can is worse than telling them nothing.
    runner.fail_on["git reset"] = (1, "fatal: could not write .git/index")
    updater = make(repo, runner, healthy=lambda timeout: False)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    message = updater.state()["message"]
    assert "working normally" not in message.lower(), message
    assert updater.state()["rollback_complete"] is False


def test_a_rollback_whose_reinstall_failed_says_so(repo, runner):
    # Old files with the new version's dependencies in the venv is its own
    # broken state, and it is not one the owner can see from the outside.
    runner.fail_on["pip"] = (1, "no matching distribution")
    updater = make(repo, runner, healthy=lambda timeout: False)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    assert updater.state()["rollback_complete"] is False
    assert "working normally" not in updater.state()["message"].lower()


def test_a_rollback_whose_restart_failed_says_so(repo, runner):
    runner.fail_on["sudo systemctl"] = (1, "Failed to restart retrobox.service")
    updater = make(repo, runner, healthy=lambda timeout: False)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    assert updater.state()["rollback_complete"] is False
    assert "working normally" not in updater.state()["message"].lower()


def test_a_rollback_that_did_work_still_says_the_box_is_fine(repo, runner):
    # The other half of the same promise: this wording is the reassuring one
    # and it has to keep being available for the case where it is true.
    updater = make(repo, runner, healthy=lambda timeout: False)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    assert updater.state()["rollback_complete"] is True
    assert "nothing was lost" in updater.state()["message"]


def test_a_rollback_that_could_not_finish_tells_the_owner_what_to_try(repo, runner):
    # The one thing an owner of this box can actually do is switch it off and
    # on again at the wall, and that genuinely does finish an interrupted
    # rollback - the next start-up picks the job back up.
    runner.fail_on["git reset"] = (1, "fatal: could not write .git/index")
    updater = make(repo, runner, healthy=lambda timeout: False)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")

    message = updater.state()["message"].lower()
    assert "off" in message and "on" in message, updater.state()["message"]


def test_a_rollback_that_could_not_finish_is_picked_up_again_at_the_next_start(
    repo, runner, healthy
):
    # And the message above only gets to say "switch it off and on again"
    # because of this. A box that could not put the old version back and then
    # sat there for ever is the box that has to be collected from somebody's
    # living room, which is the thing this whole module exists to avoid.
    runner.fail_on["git reset"] = (1, "fatal: could not write .git/index")
    updater = make(repo, runner, healthy=lambda timeout: False)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")
    assert updater.state()["rollback_complete"] is False

    runner.fail_on.pop("git reset")             # switched off and on; disk behaves
    runner.calls.clear()
    make(repo, runner, healthy).on_boot()

    assert any("v1.0.3" in " ".join(c) for c in runner.ran("reset")), (
        "it never tried again to put the old version back"
    )
    assert updater.state()["rollback_complete"] is True


def test_a_finished_rollback_is_not_done_all_over_again_at_every_start(
    repo, runner, healthy
):
    updater = make(repo, runner, healthy=lambda timeout: False)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")
    assert updater.state()["rollback_complete"] is True

    runner.calls.clear()
    make(repo, runner, healthy).on_boot()
    assert runner.calls == [], "it rolled a settled box back on every start-up"


def test_a_state_file_written_by_an_older_version_is_not_rolled_back_at_boot(
    repo, runner, healthy
):
    # Boxes in the field have a state file with no rollback_complete in it at
    # all, written before this version existed. "I do not know" is not "it
    # failed", and treating it as one would reset and reinstall every one of
    # them at the next start-up.
    updater = make(repo, runner, healthy)
    updater._write_state(
        phase="rolled_back", stage="done", previous_ref="v1.0.3", to_version="1.1.0",
    )
    runner.calls.clear()
    make(repo, runner, healthy).on_boot()
    assert runner.calls == []


# ==========================================================================
# An update is not finished until the box has been switched on again
# ==========================================================================
def test_an_update_that_passed_its_health_check_is_on_probation_not_finished(
    repo, runner, healthy
):
    # The health check inside apply() only proves the television came back
    # while the box was still switched on. This appliance is switched off at
    # the wall every night, and the version that kills a box is the one that
    # fails on the next cold start - a dependency that resolved badly, a
    # migration that never ran. So a version that has come back once is
    # installed, not confirmed, and the next few start-ups decide.
    updater = make(repo, runner, healthy)
    updater.apply("1.1.0")

    state = updater.state()
    assert state["phase"] == "probation", "it called the update finished on the spot"
    assert state["to_version"] == "1.1.0"
    assert state["previous_ref"] == "v1.0.3"
    assert state["boots"] == 0


def test_the_message_names_both_versions_so_the_owner_knows_what_would_come_back(
    repo, runner, healthy
):
    updater = make(repo, runner, healthy)
    updater.apply("1.1.0")
    message = updater.state()["message"]
    assert "1.1.0" in message and "1.0.3" in message


def test_a_box_on_probation_can_still_be_updated_again(repo, runner, healthy):
    # Probation is not a lock. A box left watching a version it never gets to
    # confirm - because nobody switches it off - must still accept the next
    # release, or one update jams the box out of every update after it.
    updater = make(repo, runner, healthy)
    updater.apply("1.1.0")
    updater.apply("1.2.0")
    assert updater.state()["to_version"] == "1.2.0"


# ==========================================================================
# Repeated bad boots
# ==========================================================================
def test_a_version_that_never_comes_up_again_is_rolled_back_by_the_box_itself(
    repo, runner, healthy
):
    # The whole reason this module exists. The update works, the television
    # comes back, the owner switches the box off at the wall that evening -
    # and from that night on the new version does not start. There is no SSH,
    # no dashboard the owner can reach, nobody to call. The box has to notice
    # on its own and put the old version back.
    updater = make(repo, runner, healthy)
    updater.apply("1.1.0")

    dead = lambda timeout: False
    for attempt in range(1, Updater.MAX_BOOT_ATTEMPTS):
        runner.calls.clear()
        make(repo, runner, dead).on_boot()              # switched on, no picture
        assert runner.ran("reset") == [], f"gave up after only {attempt} start(s)"
        assert updater.state()["phase"] == "probation"
        assert updater.state()["boots"] == attempt

    runner.calls.clear()
    make(repo, runner, dead).on_boot()
    assert any("v1.0.3" in " ".join(c) for c in runner.ran("reset")), (
        "the box sat on a version that will not start, for ever"
    )
    assert updater.state()["phase"] == "rolled_back"


def test_a_start_is_written_down_before_the_television_is_waited_for(repo, runner):
    # Waiting for the picture takes up to ninety seconds, and the owner of a
    # box showing nothing is quite likely to switch it off inside that. If the
    # attempt were only recorded afterwards, every one of those starts would
    # count for nothing and the box would never reach the point of giving up.
    seen = []

    def health(timeout):
        seen.append(updater.state()["boots"])
        return False

    updater = make(repo, runner, health)
    updater._write_state(
        phase="probation", boots=0, previous_ref="v1.0.3", to_version="1.1.0",
    )
    updater.on_boot()
    assert seen == [1], "the start was only written down after the wait"


def test_a_box_on_probation_that_comes_up_healthy_is_confirmed(repo, runner, healthy):
    updater = make(repo, runner, healthy)
    updater.apply("1.1.0")
    updater._write_state(phase="probation", boots=0)      # as a reboot would leave it

    fresh = make(repo, runner, healthy)
    fresh.on_boot()
    assert fresh.state()["phase"] == "success"


def test_repeated_unhealthy_boots_roll_back_without_being_asked(repo, runner):
    sick = lambda timeout: False
    updater = make(repo, runner, sick)
    updater._write_state(
        phase="probation", boots=0, previous_ref="v1.0.3",
        from_version="1.0.3", to_version="1.1.0",
    )

    for expected in range(1, Updater.MAX_BOOT_ATTEMPTS):
        runner.calls.clear()
        make(repo, runner, sick).on_boot()
        assert runner.ran("reset") == [], f"rolled back after only {expected} boots"

    runner.calls.clear()
    make(repo, runner, sick).on_boot()
    assert any("v1.0.3" in " ".join(c) for c in runner.ran("reset")), (
        "it never gave up on a version that will not come up"
    )


def test_the_third_start_with_no_picture_is_the_one_that_rolls_the_box_back(
    repo, runner
):
    """The number itself, spelled out, rather than taken from the constant.

    Every other test of this behaviour takes its loop bound from
    MAX_BOOT_ATTEMPTS, so all of them go on passing whatever it is set to -
    including a number so large the box never reaches it. That is a real way to
    ship: the behaviour is there, the rollback is written, and a box that
    cannot start its new version simply keeps trying, in somebody's living
    room, with no dashboard to ask and nobody who can get a shell on it.

    Three is the shipped answer and it is a judgement, not an accident. An
    owner faced with a black screen switches the box off and on again, usually
    more than once, so one or two is far too eager - it would roll back boxes
    that were only being restarted impatiently. Three means the box has genuinely
    failed to come up three separate times before it undoes the update, and it
    still gets there the same evening rather than sitting broken for a week.
    """
    sick = lambda timeout: False
    updater = make(repo, runner, sick)
    updater._write_state(
        phase="probation", boots=0, previous_ref="v1.0.3",
        from_version="1.0.3", to_version="1.1.0",
    )

    make(repo, runner, sick).on_boot()          # switched on, no picture
    assert runner.ran("reset") == [], "it gave up on the very first start"
    make(repo, runner, sick).on_boot()
    assert runner.ran("reset") == [], "it gave up after only two starts"

    make(repo, runner, sick).on_boot()
    assert any("v1.0.3" in " ".join(c) for c in runner.ran("reset")), (
        "three starts with no picture and the box is still trying the new "
        "version - shipped like this it never rolls itself back at all"
    )
    assert updater.state()["phase"] == "rolled_back"
    assert Updater.MAX_BOOT_ATTEMPTS == 3, (
        "the shipped number changed; if that is deliberate, this test and the "
        "reasoning above are what has to change with it"
    )


def test_a_box_not_on_probation_does_nothing_at_boot(repo, runner, healthy):
    updater = make(repo, runner, healthy)
    updater.on_boot()
    assert runner.calls == []


def test_a_missing_or_corrupt_state_file_is_not_a_crash(repo, runner, healthy):
    (repo / ".retrobox-update.json").write_text("{ truncated")
    updater = make(repo, runner, healthy)
    assert updater.state()["phase"] == "idle"
    updater.on_boot()


# ==========================================================================
# Switched off at the wall in the middle of an update
# ==========================================================================
def test_an_update_cut_off_part_way_through_does_not_jam_every_update_after_it(
    repo, runner, healthy
):
    # An update running when the box loses power leaves phase="running" on
    # disk with nothing running. Nothing clears it, there is no button for it
    # and the owner cannot get a shell, so refusing for ever would mean a box
    # that can never be updated again - including out of the very bug that
    # would fix it.
    clock = Clock()
    updater = make(repo, runner, healthy, clock=clock)
    updater._write_state(phase="running", stage="installing", to_version="1.1.0")

    clock.advance(Updater.STALE_AFTER_SECONDS + 1)
    updater.apply("1.1.0")
    assert updater.state()["phase"] == "probation"


def test_an_update_that_really_is_running_is_still_refused(repo, runner, healthy):
    # The other side of the same coin: recent means running, and two pip
    # installs in the same clone at once is how a box ends up unbootable.
    clock = Clock()
    updater = make(repo, runner, healthy, clock=clock)
    updater._write_state(phase="running", stage="installing", to_version="1.1.0")

    clock.advance(60.0)
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")


def test_a_box_that_starts_up_half_way_through_an_update_puts_the_old_one_back(
    repo, runner, healthy
):
    # Nothing survives the wall switch, so a "running" update found at start-up
    # is not running: it is a half-installed clone with a venv that may match
    # neither version. The recorded way back is the only known-good state.
    updater = make(repo, runner, healthy)
    updater._write_state(
        phase="running", stage="installing", previous_ref="v1.0.3", to_version="1.1.0",
    )

    runner.calls.clear()
    make(repo, runner, healthy).on_boot()
    assert any("v1.0.3" in " ".join(c) for c in runner.ran("reset")), (
        "it left the box on a half-installed version"
    )
    assert updater.state()["phase"] == "rolled_back"


def test_a_rollback_cut_off_part_way_through_is_finished_at_the_next_start(
    repo, runner, healthy
):
    # Worse than an interrupted update: the tree may be back on the old
    # version with the new version's dependencies in the venv. Finishing the
    # job is the only thing that makes those two agree again.
    updater = make(repo, runner, healthy)
    updater._write_state(
        phase="rolling_back", previous_ref="v1.0.3", to_version="1.1.0",
    )

    runner.calls.clear()
    make(repo, runner, healthy).on_boot()
    assert any("v1.0.3" in " ".join(c) for c in runner.ran("reset"))
    assert any("pip" in Runner._key(c) for c in runner.calls), "the venv was left mixed"
    assert updater.state()["phase"] == "rolled_back"


def test_an_interrupted_update_with_nowhere_to_go_back_to_is_cleared_not_left_stuck(
    repo, runner, healthy
):
    # Interrupted before the previous ref was recorded, so nothing was touched
    # either. There is nothing to undo and nothing to restart - but the state
    # still has to stop saying "running", or it jams the next attempt.
    updater = make(repo, runner, healthy)
    updater._write_state(phase="running", stage="checking", to_version="1.1.0")

    runner.calls.clear()
    make(repo, runner, healthy).on_boot()
    assert runner.calls == [], "it started undoing an update that never began"
    assert updater.state()["phase"] != "running"
    updater.apply("1.1.0")                      # and the box can be updated again


# ==========================================================================
# Something has to actually call it
# ==========================================================================
def test_the_dashboard_command_the_box_runs_goes_through_the_boot_check():
    # The bug this section exists for was never that on_boot() was wrong. It
    # was that nothing on the box ever called it, so the entire safety net was
    # dead code. The systemd unit runs `.venv/bin/retrobox-web`, and what that
    # points at is decided here.
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert 'retrobox-web = "retrobox.webservice:main"' in text


def test_starting_the_dashboard_checks_how_the_last_update_went(tmp_path, monkeypatch):
    from retrobox import updater as updater_module
    from retrobox import webservice, webui

    served = []
    monkeypatch.setattr(webui, "main", lambda argv=None: served.append(argv) or 0)

    asked = {}
    monkeypatch.setattr(updater_module, "start_boot_check", lambda **kw: asked.update(kw))

    cfg = tmp_path / "config.yaml"
    cfg.write_text("channels: []\n")
    assert webservice.main(["--config", str(cfg), "--port", "8080"]) == 0

    # The same file the dashboard's own update panel reads, or the check looks
    # at one state file while the box writes another.
    assert asked["state_path"] == tmp_path / webui.UPDATE_STATE_NAME
    assert asked["repo_dir"] == webui.REPO_DIR
    assert served, "the dashboard never started"


def test_a_boot_check_that_goes_wrong_never_stops_the_dashboard_starting(
    tmp_path, monkeypatch
):
    # The dashboard is the one thing an owner can still reach on a sick box.
    # Nothing in here may be the reason it does not come up.
    from retrobox import updater as updater_module
    from retrobox import webservice, webui

    monkeypatch.setattr(webui, "main", lambda argv=None: 0)

    def explode(**kw):
        raise OSError("the state file is on a disk that just died")

    monkeypatch.setattr(updater_module, "start_boot_check", explode)
    assert webservice.main(["--config", str(tmp_path / "not-there.yaml")]) == 0


def test_the_boot_check_finds_git_when_it_runs_not_when_it_was_imported(
    repo, runner, monkeypatch
):
    # tests/conftest.py exists because this module once ran `git reset --hard`
    # over the real checkout. That happened because a stub was bound at import
    # time and the real command ran anyway. The boot check builds its own
    # Updater with no runner passed in, so it is the one place that could
    # bring the mistake back.
    from retrobox import updater as updater_module

    ran = []
    monkeypatch.setattr(
        updater_module, "_run", lambda cmd, **kw: (ran.append(list(cmd)), (0, ""))[1]
    )
    monkeypatch.setattr(updater_module, "player_is_healthy", lambda timeout: False)

    state = repo / ".retrobox-update.json"
    make(repo, runner, healthy=lambda t: True)._write_state(
        phase="probation", boots=Updater.MAX_BOOT_ATTEMPTS - 1,
        previous_ref="v1.0.3", to_version="1.1.0",
    )

    updater_module.check_at_boot(repo_dir=repo, state_path=state)
    assert any("v1.0.3" in " ".join(c) for c in ran), "nothing rolled back"
    assert runner.calls == [], "it ran something other than the stub"


def test_the_boot_check_does_not_swallow_the_guard_in_conftest(repo, runner, monkeypatch):
    # check_at_boot() eats every exception, because a service coming up must
    # come up. That is right on a box and wrong in a test: the guard in
    # tests/conftest.py shouts by raising, and an eaten AssertionError would
    # turn "this just ran git reset --hard over your working tree" into a
    # WARNING nobody reads.
    from retrobox import updater as updater_module

    def guard(cmd, **kw):
        raise AssertionError("that would have hit the real checkout")

    monkeypatch.setattr(updater_module, "_run", guard)
    monkeypatch.setattr(updater_module, "player_is_healthy", lambda timeout: False)

    state = repo / ".retrobox-update.json"
    make(repo, runner, healthy=lambda t: True)._write_state(
        phase="probation", boots=Updater.MAX_BOOT_ATTEMPTS - 1,
        previous_ref="v1.0.3", to_version="1.1.0",
    )

    with pytest.raises(AssertionError):
        updater_module.check_at_boot(repo_dir=repo, state_path=state)


def test_the_boot_check_does_not_hold_the_dashboard_up_while_it_waits(
    repo, runner, monkeypatch
):
    # It waits up to ninety seconds for the television. Serving the page has
    # to start now, not after that.
    from retrobox import updater as updater_module

    monkeypatch.setattr(updater_module, "_run", lambda cmd, **kw: (0, ""))
    monkeypatch.setattr(updater_module, "player_is_healthy", lambda timeout: True)

    state = repo / ".retrobox-update.json"
    make(repo, runner, healthy=lambda t: True)._write_state(
        phase="probation", boots=0, previous_ref="v1.0.3", to_version="1.1.0",
    )

    thread = updater_module.start_boot_check(repo_dir=repo, state_path=state)
    assert thread.daemon, "a stuck check would keep the dashboard alive for ever"
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert json.loads(state.read_text())["phase"] == "success"


# ==========================================================================
# The state file, which is what a reloaded page reads
# ==========================================================================
def test_progress_is_written_where_a_reloaded_page_can_find_it(repo, runner, healthy):
    seen = []
    updater = make(repo, runner, healthy)
    original = updater._write_state

    def spy(**kw):
        original(**kw)
        seen.append(json.loads((repo / ".retrobox-update.json").read_text())["stage"])

    updater._write_state = spy
    updater.apply("1.1.0")

    assert "installing" in seen and "restarting" in seen
    assert seen.index("installing") < seen.index("restarting")


def test_the_named_stages_are_the_ones_the_page_shows(repo, runner, healthy):
    assert Updater.STAGES == (
        "checking", "preparing", "downloading", "installing", "restarting",
        "health", "done",
    )


def test_two_updates_at_once_are_refused(repo, runner, healthy):
    updater = make(repo, runner, healthy)
    updater._write_state(phase="running", stage="installing")
    with pytest.raises(UpdateError):
        updater.apply("1.1.0")


# ==========================================================================
# Nothing here takes an address from anywhere
# ==========================================================================
def test_apply_takes_a_version_and_nothing_else():
    import inspect

    parameters = list(inspect.signature(Updater.apply).parameters)
    assert parameters == ["self", "version"]


def test_the_version_it_is_given_has_to_look_like_a_version(repo, runner, healthy):
    updater = make(repo, runner, healthy)
    for bad in ("", "main", "; rm -rf /", "../../etc", "v", None, "HEAD",
                "1.1.0 --upload-pack=evil", "origin/main"):
        with pytest.raises(UpdateError):
            updater.apply(bad)
    assert runner.calls == [], "a bad ref reached git"
