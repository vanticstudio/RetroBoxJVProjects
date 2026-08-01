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
        self.fail_on = {}          # first word of argv -> (code, output)
        self.answers = {}

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        key = self._key(cmd)
        if key in self.fail_on:
            return self.fail_on[key]
        return (0, self.answers.get(key, ""))

    @staticmethod
    def _key(cmd):
        parts = [p for p in cmd if not p.startswith("-")]
        if parts and parts[0].endswith("python"):
            return "pip"
        return " ".join(parts[:2]) if len(parts) > 1 else (parts[0] if parts else "")

    def ran(self, needle):
        return [c for c in self.calls if needle in " ".join(c)]


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
