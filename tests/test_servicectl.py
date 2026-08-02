"""Restarting things, on a box anyone on the LAN can reach.

The dashboard has no authentication, and restart/reboot being available to
anyone on the home network is an accepted trade for a television. What is NOT
accepted is a sudoers rule broad enough to be turned into something worse than
a reboot, so the set of commands this module can run is closed and every one
of them is spelled out in full.
"""

import pytest

from retrobox import servicectl


@pytest.fixture
def ran(monkeypatch):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(servicectl, "_run", runner)
    return calls


# ==========================================================================
# The closed set
# ==========================================================================
def test_the_actions_are_a_fixed_list():
    assert set(servicectl.ACTIONS) == {
        "restart-tv", "restart-dashboard", "reboot", "shutdown",
    }


@pytest.mark.parametrize(
    "action",
    ["", "rm", "restart", "systemctl", "restart-tv; rm -rf /", "RESTART-TV",
     "../reboot", "restart-sshd", None, 42],
)
def test_anything_not_on_the_list_is_refused(ran, action):
    with pytest.raises(servicectl.ServiceError):
        servicectl.run(action)
    assert ran == [], "it tried to run something anyway"


def test_every_action_maps_to_a_fully_spelled_out_command():
    # Nothing here may be assembled from user input at call time.
    for action, argv in servicectl.COMMANDS.items():
        assert argv[0] == "sudo", action
        assert all(isinstance(part, str) for part in argv), action
        assert not any("*" in part for part in argv), action


# ==========================================================================
# What each one does
# ==========================================================================
def test_restarting_the_tv_restarts_only_the_tv(ran):
    servicectl.run("restart-tv")
    assert ran == [["sudo", "-n", "systemctl", "restart", "retrobox.service"]]


def test_rebooting_is_a_reboot(ran):
    servicectl.run("reboot")
    assert ran == [["sudo", "-n", "systemctl", "reboot"]]


def test_shutting_down_still_works(ran):
    servicectl.run("shutdown")
    assert ran == [["sudo", "-n", "systemctl", "poweroff"]]


def test_restarting_the_dashboard_does_not_wait_for_itself(ran):
    # The process running this command is the one being restarted. Without
    # --no-block systemctl waits for a restart that kills it first, and the
    # browser gets a dead connection instead of an answer.
    servicectl.run("restart-dashboard")
    assert ran == [[
        "sudo", "-n", "systemctl", "--no-block", "restart", "retrobox-web.service",
    ]]


def test_a_command_that_fails_says_so_rather_than_pretending(monkeypatch):
    monkeypatch.setattr(
        servicectl, "_run", lambda cmd, **kw: (1, "sudo: a password is required")
    )
    with pytest.raises(servicectl.ServiceError) as caught:
        servicectl.run("reboot")
    assert "password" in str(caught.value)


def test_sudo_is_never_allowed_to_sit_waiting_for_a_password(ran):
    # -n on every one of them: a prompt on a headless box is a hung request.
    for action in servicectl.ACTIONS:
        ran.clear()
        servicectl.run(action)
        assert "-n" in ran[0], action


# ==========================================================================
# The rule that gets installed
# ==========================================================================
def test_the_sudoers_rule_names_every_command_it_allows():
    rule = servicectl.sudoers_rule("retro")
    for argv in servicectl.COMMANDS.values():
        wanted = " ".join(argv[2:])          # drop "sudo -n"
        assert wanted in rule, wanted


def test_the_sudoers_rule_never_grants_systemctl_in_general():
    rule = servicectl.sudoers_rule("retro")
    for line in rule.splitlines():
        if "systemctl" not in line:
            continue
        assert "systemctl *" not in line
        assert not line.rstrip().endswith("systemctl")


def test_no_systemctl_rule_carries_a_wildcard():
    # The service rules name their units exactly. A wildcard on systemctl is
    # the difference between a reboot button and control of the whole machine.
    for line in servicectl.sudoers_rule("retro").splitlines():
        if "systemctl" in line and not line.lstrip().startswith("#"):
            assert "*" not in line, line


# Every wildcard in the rule, deliberately enumerated. Each is a fixed
# subcommand taking one argument that is checked in Python against a real
# whitelist first - the machine's timezone list, its own interface list, a
# hostname pattern, or a number this box chose. Anything not on this list
# appearing in the rule is a new hole and the test says so.
PERMITTED_WILDCARDS = (
    "timedatectl set-timezone *",
    "netplan try --timeout=*",
    "iw dev * scan",
    "hostnamectl set-hostname *",
)


def test_every_wildcard_in_the_rule_is_one_we_chose():
    for line in servicectl.sudoers_rule("retro").splitlines():
        if "*" not in line or line.lstrip().startswith("#"):
            continue
        for fragment in line.split(","):
            fragment = fragment.strip().rstrip("\\").strip()
            if "*" not in fragment:
                continue
            assert any(fragment.endswith(w) for w in PERMITTED_WILDCARDS), fragment


def test_the_netplan_files_are_named_exactly_never_wildcarded():
    # The file contents carry the wifi password. A wildcard on the path would
    # let anyone on the LAN write any file in /etc/netplan.
    for line in servicectl.sudoers_rule("retro").splitlines():
        if "/etc/netplan" not in line or line.lstrip().startswith("#"):
            continue
        assert "/etc/netplan/*" not in line
        assert "*" not in line.split("/etc/netplan")[1].split(",")[0]


def test_writing_a_netplan_file_never_takes_the_content_as_an_argument():
    # tee and cat name the target and nothing else; the document goes on stdin.
    rule = servicectl.sudoers_rule("retro")
    for path in ("/etc/netplan/90-retrobox-wired.yaml",
                 "/etc/netplan/91-retrobox-wifi.yaml"):
        assert f"tee {path}" in rule
        assert f"chmod 600 {path}" in rule


def test_the_rename_that_puts_a_netplan_file_in_place_is_one_sudo_permits():
    """The netplan write stages beside the file and renames over it.

    sudo matches the literal argv it is given, so this rule and the command
    the code runs have to agree character for character. They are built from
    the same constant for that reason: if they ever drift, a customer's
    Network page stops being able to save anything at all, on a box nobody
    can log in to.
    """
    from retrobox import netconf

    rule = servicectl.sudoers_rule("retro")
    for target in (netconf.WIRED_FILE, netconf.WIFI_FILE):
        staged = netconf.staging_for(target)
        assert staged.startswith("/etc/netplan/"), staged
        assert not staged.endswith(".yaml"), (
            "netplan reads /etc/netplan/*.yaml, so it would read a staging "
            "file that is still being written"
        )
        for mv in ("/usr/bin/mv", "/bin/mv"):
            assert f"{mv} -f {staged} {target}" in rule, mv
        assert f"tee {staged}" in rule
        assert f"chmod 600 {staged}" in rule


def test_the_rename_can_only_ever_move_our_own_staging_file(ran):
    # Both ends named in full. A wildcard on either would turn "save the wifi
    # password" into "move any file over any other file, as root".
    rule = servicectl.sudoers_rule("retro")
    for line in rule.splitlines():
        if "/mv " not in line or line.lstrip().startswith("#"):
            continue
        assert "*" not in line, line


def test_a_timezone_the_box_does_not_know_is_refused(ran):
    with pytest.raises(servicectl.ServiceError):
        servicectl.set_timezone("Mars/Olympus", allowed=["Europe/London"])
    assert ran == []


@pytest.mark.parametrize(
    "zone",
    ["", "../../etc/passwd", "Europe/London; rm -rf /", "-", "x" * 300, None,
     "Europe/London\x00"],
)
def test_a_timezone_that_is_not_a_timezone_never_reaches_sudo(ran, zone):
    with pytest.raises(servicectl.ServiceError):
        servicectl.set_timezone(zone, allowed=["Europe/London"])
    assert ran == []


def test_a_known_timezone_is_set(ran):
    servicectl.set_timezone("Europe/London", allowed=["Europe/London", "UTC"])
    assert ran == [["sudo", "-n", "timedatectl", "set-timezone", "Europe/London"]]


def test_the_sudoers_rule_refuses_a_username_it_cannot_vouch_for():
    # It is written into a file that grants privilege. A name with a space or
    # a comma in it changes what that file means.
    for bad in ("", "root ALL=(ALL) NOPASSWD: ALL", "a b", "a,b", "a\nb", "x" * 200):
        with pytest.raises(ValueError):
            servicectl.sudoers_rule(bad)


def test_the_sudoers_rule_covers_both_places_systemctl_lives():
    # /usr/bin on modern distros, /bin on older ones. sudo matches the literal
    # path, so a rule with only one of them silently fails on the other.
    rule = servicectl.sudoers_rule("retro")
    assert "/usr/bin/systemctl" in rule and "/bin/systemctl" in rule


# ==========================================================================
# Does the permission on THIS box cover what THIS code runs?
#
# The question is deliberately not "does /etc/sudoers.d/retrobox-system
# exist". That check passes on a box where half the dashboard is dead: the
# file can be there and be a version behind, and the file is 0440 root:root
# inside a directory an ordinary user cannot even look into, so the dashboard
# could not read it to compare it anyway. The only honest source for "will
# sudo run this without a password" is sudo, so the check asks sudo - with
# `-l`, which lists a command and never runs it.
# ==========================================================================
PROBE = ["sudo", "-n", "-l", "--"]


class FakeSudo:
    """Stands in for ``sudo -n -l``, answering from a rule we choose.

    Fails the test loudly if the check ever tries to *run* one of the
    commands it is asking about, which on a real box would reboot it.
    """

    def __init__(self, allow=lambda spec: True,
                 refusal="sudo: a password is required"):
        self.calls = []
        self._allow = allow
        self.refusal = refusal

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        assert cmd[:4] == PROBE, f"the check ran {cmd!r} instead of listing it"
        spec = " ".join(cmd[4:])
        if self._allow(spec):
            return 0, spec
        return 1, self.refusal

    @property
    def specs(self):
        return [" ".join(call[4:]) for call in self.calls]


@pytest.fixture
def sudo(monkeypatch, tmp_path):
    """A box where every privileged program exists and sudo is a stub."""
    monkeypatch.setattr(servicectl, "_first_existing", lambda paths: paths[0])
    monkeypatch.setattr(
        servicectl, "SUDOERS_PATH", str(tmp_path / "retrobox-system")
    )

    def install(allow=lambda spec: True,
                refusal="sudo: a password is required"):
        fake = FakeSudo(allow, refusal)
        monkeypatch.setattr(servicectl, "_run", fake)
        return fake

    return install


def test_a_box_that_may_run_everything_the_code_runs_reports_nothing_wrong(sudo):
    sudo()
    check = servicectl.check_privileges()
    assert check.state == servicectl.PRIVILEGES_OK
    assert check.ok is True
    assert check.affected == ()
    assert check.refused == ()


def test_a_healthy_box_is_not_told_off_for_a_file_the_dashboard_cannot_see(sudo):
    # /etc/sudoers.d is root-only, so the dashboard cannot stat what is in it
    # and cannot read the fragment even if it could. A banner that appears on
    # a healthy box teaches customers to ignore banners, so the filesystem
    # never raises the alarm on its own: sudo's own answer does.
    sudo()
    assert servicectl.check_privileges().ok is True


def test_a_fragment_that_someone_reformatted_is_not_mistaken_for_damage(sudo, tmp_path):
    # A trailing newline, a comment, a different username, a reordered line:
    # none of that changes what sudo will do, so none of it is a fault.
    path = tmp_path / "retrobox-system"
    path.write_text("# somebody added a note\n\n\n")
    sudo()
    assert servicectl.check_privileges().ok is True


def test_a_box_that_was_never_granted_anything_says_the_permission_is_missing(sudo):
    sudo(allow=lambda spec: False)
    check = servicectl.check_privileges()
    assert check.state == servicectl.PRIVILEGES_MISSING
    assert check.ok is False


def test_a_box_granted_an_older_smaller_set_of_commands_is_called_out_as_stale(sudo, tmp_path):
    # This is the one that hits every box in the field the first time COMMANDS
    # grows: the file is there, it was generated from a version behind, and so
    # some buttons work and some do not.
    (tmp_path / "retrobox-system").write_text("something older\n")
    sudo(allow=lambda spec: "systemctl poweroff" in spec)
    check = servicectl.check_privileges()
    assert check.state == servicectl.PRIVILEGES_STALE
    assert check.ok is False
    assert any("systemctl reboot" in spec for spec in check.refused)


def test_the_box_this_bug_was_found_on_is_called_stale_and_not_missing(sudo):
    # The real unit: the legacy /etc/sudoers.d/retrobox-poweroff was there and
    # granted exactly one command, so Shut Down worked while Restart, Reboot
    # and the whole wifi flow did not, and /etc/sudoers.d/retrobox-system had
    # never been written at all. So the file genuinely is absent here - and the
    # honest description is still "some of it works and some of it does not",
    # because that is what the owner is looking at. A partial grant is a
    # partial grant no matter which file it came out of, and the filesystem
    # does not get a vote: on a real box /etc/sudoers.d is root-only, the
    # dashboard's look inside it is refused rather than answered, and a state
    # that depended on that would change with who was asking.
    sudo(allow=lambda spec: "systemctl poweroff" in spec)
    check = servicectl.check_privileges()
    assert check.state == servicectl.PRIVILEGES_STALE
    assert any("systemctl reboot" in spec for spec in check.refused)


def test_a_fragment_that_is_there_but_grants_nothing_is_still_missing(sudo, tmp_path):
    # And the same rule read the other way round. A file that exists but that
    # sudo will not act on buys nothing, so its being there must not soften
    # what the customer is told.
    (tmp_path / "retrobox-system").write_text("# something that grants nothing\n")
    sudo(allow=lambda spec: False)
    assert servicectl.check_privileges().state == servicectl.PRIVILEGES_MISSING


def test_a_command_added_to_the_table_is_something_the_check_asks_about(sudo, monkeypatch):
    # The check follows COMMANDS. Nothing anywhere keeps a second list that
    # could be forgotten when a fifth button is added.
    grown = dict(servicectl.COMMANDS)
    grown["restart-share"] = [
        "sudo", "-n", "systemctl", "restart", "retrobox-share.service",
    ]
    monkeypatch.setattr(servicectl, "COMMANDS", grown)
    fake = sudo(allow=lambda spec: "retrobox-share.service" not in spec)
    check = servicectl.check_privileges()
    assert any("retrobox-share.service" in spec for spec in fake.specs), (
        "the new command was never asked about"
    )
    assert check.state == servicectl.PRIVILEGES_STALE


def test_the_check_asks_about_every_command_the_code_can_run(sudo):
    from retrobox import netconf

    fake = sudo()
    servicectl.check_privileges()
    asked = " | ".join(fake.specs)
    for argv in servicectl.COMMANDS.values():
        assert " ".join(argv[3:]) in asked, argv
    for target in (netconf.WIRED_FILE, netconf.WIFI_FILE):
        # The staging file is where a stale rule bit a real box before.
        assert netconf.staging_for(target) in asked, target
    assert "set-timezone" in asked
    assert "scan" in asked


def test_a_box_where_sudo_itself_cannot_become_root_is_a_different_fault(sudo):
    # NoNewPrivileges= in a unit file makes the kernel ignore sudo's setuid
    # bit. The permission file can be perfect and every button still fails,
    # and re-generating the file would fix nothing - so it gets its own state.
    fake = sudo(
        allow=lambda spec: False,
        refusal='sudo: The "no new privileges" flag is set, which prevents '
                "sudo from running as root.",
    )
    check = servicectl.check_privileges()
    assert check.state == servicectl.PRIVILEGES_BLOCKED
    assert len(fake.calls) == 1, "it kept asking after sudo said it cannot work"


@pytest.mark.parametrize(
    "refusal",
    ['sudo: The "no new privileges" flag is set, which prevents sudo from '
     "running as root.",
     "sudo: effective uid is not 0, is /usr/bin/sudo on a file system with "
     "the 'nosuid' option set or an NFS file system without root privileges?",
     "sudo: /usr/bin/sudo must be owned by uid 0 and have the setuid bit set"],
)
def test_every_way_sudo_says_it_cannot_become_root_is_read_the_same_way(sudo, refusal):
    sudo(allow=lambda spec: False, refusal=refusal)
    assert servicectl.check_privileges().state == servicectl.PRIVILEGES_BLOCKED


def test_only_the_programs_this_box_actually_has_are_asked_about(monkeypatch, tmp_path):
    # A box with no wifi hardware may have no `iw`. Asking sudo about a
    # command that is not installed gets "command not found", which is not a
    # permission problem and must not raise a banner.
    monkeypatch.setattr(
        servicectl, "SUDOERS_PATH", str(tmp_path / "retrobox-system")
    )
    monkeypatch.setattr(
        servicectl, "_first_existing",
        lambda paths: None if "iw" in paths[0] else paths[0],
    )
    fake = FakeSudo()
    monkeypatch.setattr(servicectl, "_run", fake)
    check = servicectl.check_privileges()
    assert check.ok is True
    assert not any(" scan" in spec for spec in fake.specs)


def test_which_parts_of_the_dashboard_are_affected_is_said_in_words_a_customer_knows(sudo, tmp_path):
    (tmp_path / "retrobox-system").write_text("older\n")
    sudo(allow=lambda spec: "netplan" not in spec and "/tee" not in spec
         and "/chmod" not in spec and "/mv" not in spec and "/cat" not in spec)
    check = servicectl.check_privileges()
    assert check.affected, "it did not say what stopped working"
    said = " ".join(check.affected).lower()
    assert "network" in said
    assert "netplan" not in said and "/usr/bin" not in said


@pytest.mark.parametrize("broken", ["missing", "stale", "blocked", "ok"])
def test_nothing_a_customer_is_shown_is_written_in_jargon(sudo, tmp_path, broken):
    (tmp_path / "retrobox-system").write_text("older\n")
    if broken == "ok":
        sudo()
    elif broken == "missing":
        sudo(allow=lambda spec: False)
    elif broken == "stale":
        sudo(allow=lambda spec: "systemctl poweroff" in spec)
    else:
        sudo(allow=lambda spec: False,
             refusal='sudo: The "no new privileges" flag is set')
    check = servicectl.check_privileges()
    shown = (check.headline + " " + check.message).lower()
    for jargon in ("sudo", "nopasswd", "systemctl", "netplan", "visudo",
                   "uid", "setuid", "fragment", "sudoers"):
        assert jargon not in shown, (jargon, shown)


def test_a_check_that_finds_trouble_hands_back_the_command_that_fixes_it(sudo):
    sudo(allow=lambda spec: False)
    check = servicectl.check_privileges()
    assert check.command == servicectl.FIX_COMMAND
    assert "install-service.sh" in check.command
    assert check.command in check.message


# ==========================================================================
# Putting it back
# ==========================================================================
def test_the_dashboard_cannot_grant_itself_permission_and_says_so_plainly(monkeypatch, tmp_path):
    # The Repair button has no authentication in front of it - anyone on the
    # LAN can press it. A dashboard that could write /etc/sudoers.d would be
    # handing root to the whole street, so it may not, it never tries, and it
    # says what a person has to run instead.
    target = tmp_path / "retrobox-system"
    monkeypatch.setattr(servicectl, "SUDOERS_PATH", str(target))
    monkeypatch.setattr(servicectl, "_am_root", lambda: False)
    ran = []
    monkeypatch.setattr(servicectl, "_run", lambda cmd, **kw: ran.append(cmd) or (0, ""))

    result = servicectl.repair("retro")
    assert result.applied is False
    assert result.command == servicectl.FIX_COMMAND
    assert not target.exists(), "it wrote a privilege file as an ordinary user"
    assert ran == [], "it tried to run something to get around not being root"
    assert "sudo" not in result.message.lower()


def test_repair_installs_the_current_rule_when_it_is_run_as_root(monkeypatch, tmp_path):
    target = tmp_path / "retrobox-system"
    monkeypatch.setattr(servicectl, "SUDOERS_PATH", str(target))
    monkeypatch.setattr(servicectl, "_am_root", lambda: True)
    monkeypatch.setattr(servicectl, "_first_existing", lambda paths: paths[0])
    checked = []

    def visudo(cmd, **kw):
        checked.append(list(cmd))
        return 0, ""

    monkeypatch.setattr(servicectl, "_run", visudo)

    result = servicectl.repair("retro")
    assert result.applied is True
    assert target.read_text() == servicectl.sudoers_rule("retro")
    assert checked and "-c" in checked[0], "it installed without checking first"
    assert list(tmp_path.iterdir()) == [target], "it left a half-written file behind"


def test_a_rule_that_does_not_check_out_is_never_installed(monkeypatch, tmp_path):
    # A bad file in /etc/sudoers.d breaks sudo for everything, on a box whose
    # only recovery story is sudo. Better the old file than a broken one.
    target = tmp_path / "retrobox-system"
    target.write_text("the rule that is already there\n")
    monkeypatch.setattr(servicectl, "SUDOERS_PATH", str(target))
    monkeypatch.setattr(servicectl, "_am_root", lambda: True)
    monkeypatch.setattr(servicectl, "_first_existing", lambda paths: paths[0])
    monkeypatch.setattr(servicectl, "_run", lambda cmd, **kw: (1, ">>> syntax error"))

    result = servicectl.repair("retro")
    assert result.applied is False
    assert target.read_text() == "the rule that is already there\n"
    assert list(tmp_path.iterdir()) == [target], "it left a half-written file behind"


def test_repair_will_not_write_a_rule_it_cannot_check(monkeypatch, tmp_path):
    target = tmp_path / "retrobox-system"
    monkeypatch.setattr(servicectl, "SUDOERS_PATH", str(target))
    monkeypatch.setattr(servicectl, "_am_root", lambda: True)
    monkeypatch.setattr(servicectl, "_first_existing", lambda paths: None)
    monkeypatch.setattr(servicectl, "_run", lambda cmd, **kw: (0, ""))

    result = servicectl.repair("retro")
    assert result.applied is False
    assert not target.exists()


def test_repair_refuses_a_user_name_it_cannot_vouch_for(monkeypatch, tmp_path):
    target = tmp_path / "retrobox-system"
    monkeypatch.setattr(servicectl, "SUDOERS_PATH", str(target))
    monkeypatch.setattr(servicectl, "_am_root", lambda: True)
    monkeypatch.setattr(servicectl, "_first_existing", lambda paths: paths[0])
    monkeypatch.setattr(servicectl, "_run", lambda cmd, **kw: (0, ""))

    result = servicectl.repair("root ALL=(ALL) NOPASSWD: ALL")
    assert result.applied is False
    assert not target.exists()


def test_no_rule_this_box_grants_lets_anything_write_the_permission_files():
    # The way out of this bug must not be a rule that lets an unauthenticated
    # web page edit sudo's own configuration. Repair is allowed to say what is
    # wrong and what to type; it is not allowed to hand itself root.
    for spec in specs_in(servicectl.sudoers_rule("retro")):
        for word in ("sudoers", "visudo", "/etc/sudoers", "install", "cp ",
                     "bash", "/sh", "sudo"):
            assert word not in spec, spec


# ==========================================================================
# What a customer is told when a button does not work
# ==========================================================================
@pytest.mark.parametrize(
    "raw",
    ["sudo: a password is required",
     "sudo: interactive authentication is required",
     "Interactive authentication required.",
     "sudo: no tty present and no askpass program specified",
     "Sorry, user retro is not allowed to execute '/bin/systemctl reboot' as "
     "root on retrobox.",
     "retro is not in the sudoers file.  This incident will be reported.",
     'sudo: The "no new privileges" flag is set, which prevents sudo from '
     "running as root.",
     "could not reboot: sudo: a password is required"],
)
def test_every_way_sudo_says_no_becomes_the_same_plain_sentence(raw):
    said = servicectl.explain_failure(raw)
    assert said == servicectl.explain_failure("sudo: a password is required")
    assert "permission" in said.lower()
    assert "install-service.sh" in said
    for leaked in ("sudo", "askpass", "tty", "uid", "NOPASSWD"):
        assert leaked not in said, leaked


def test_a_failure_that_is_not_about_permission_keeps_its_own_words():
    said = servicectl.explain_failure(
        "Failed to restart retrobox.service: Unit retrobox.service not found."
    )
    assert "Unit retrobox.service not found" in said


def test_sudo_grumbling_about_the_hostname_is_not_read_as_a_refusal():
    # sudo writes this on EVERY invocation once the hostname no longer matches
    # /etc/hosts, which the dashboard's own hostname button causes. Reading it
    # as a refusal would put a repair banner on a perfectly healthy box.
    noise = "sudo: unable to resolve host retrobox: Name or service not known"
    assert servicectl.is_permission_problem(noise) is False
    said = servicectl.explain_failure(noise + "\nJob failed. See journal.")
    assert "Job failed" in said
    assert "unable to resolve host" not in said


def test_a_refused_action_carries_the_plain_english_with_it(monkeypatch):
    monkeypatch.setattr(
        servicectl, "_run",
        lambda cmd, **kw: (1, "sudo: interactive authentication is required"),
    )
    with pytest.raises(servicectl.ServiceError) as caught:
        servicectl.run("reboot")
    assert caught.value.plain == servicectl.PERMISSION_MESSAGE
    assert "sudo" not in caught.value.plain.lower()


def test_a_timezone_that_sudo_refuses_is_explained_the_same_way(monkeypatch):
    monkeypatch.setattr(
        servicectl, "_run", lambda cmd, **kw: (1, "sudo: a password is required")
    )
    with pytest.raises(servicectl.ServiceError) as caught:
        servicectl.set_timezone("Europe/London", allowed=["Europe/London"])
    assert "install-service.sh" in caught.value.plain
    assert "sudo" not in caught.value.plain.lower()


# ==========================================================================
# The invariants, guarded
# ==========================================================================
def specs_in(rule):
    """Every command spelled out in a rule, one per entry."""
    found = []
    for line in rule.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "NOPASSWD:" in line:
            line = line.split("NOPASSWD:", 1)[1]
        for part in line.rstrip("\\").split(","):
            part = part.strip()
            if part.startswith("/"):
                found.append(part)
    return found


def test_every_command_in_the_table_is_in_the_rule_and_nothing_else_is():
    granted = {s for s in specs_in(servicectl.sudoers_rule("retro"))
               if "systemctl" in s}
    wanted = set()
    for argv in servicectl.COMMANDS.values():
        for path in ("/usr/bin/systemctl", "/bin/systemctl"):
            wanted.add(f"{path} {' '.join(argv[3:])}".rstrip())
    assert granted == wanted


def test_a_command_added_to_the_table_appears_in_the_rule_on_its_own(monkeypatch):
    grown = dict(servicectl.COMMANDS)
    grown["restart-share"] = [
        "sudo", "-n", "systemctl", "restart", "retrobox-share.service",
    ]
    monkeypatch.setattr(servicectl, "COMMANDS", grown)
    assert "/usr/bin/systemctl restart retrobox-share.service" in specs_in(
        servicectl.sudoers_rule("retro")
    )


def test_every_command_in_the_table_is_one_the_rule_knows_how_to_spell():
    """The rule spells everything in COMMANDS as systemctl. Keep it that way.

    ``required_privileges`` takes each entry in the table, drops "sudo -n", and
    grants what is left under /usr/bin/systemctl and /bin/systemctl. That is
    true of all four buttons and reads as harmless - until somebody adds a
    command for a different program. Then the rule grants
    "systemctl <that program's arguments>", which is not the command the code
    runs, so sudo refuses the real one and the new button fails on every box in
    the field with nothing in the dashboard to explain it. Exactly the drift
    this whole module exists to stop, so it is caught here instead: a fifth
    command for a different program means required_privileges has to learn
    where that program lives first.
    """
    for action, argv in servicectl.COMMANDS.items():
        assert argv[:3] == ["sudo", "-n", "systemctl"], (
            f"{action} does not run systemctl, so the generated rule would "
            f"grant the wrong program: {argv!r}"
        )


def test_a_command_taken_out_of_the_table_stops_being_granted(monkeypatch):
    shrunk = {k: v for k, v in servicectl.COMMANDS.items() if k != "reboot"}
    monkeypatch.setattr(servicectl, "COMMANDS", shrunk)
    assert "/usr/bin/systemctl reboot" not in specs_in(
        servicectl.sudoers_rule("retro")
    )


def test_the_rule_names_no_program_the_code_does_not_run():
    allowed_programs = {
        "systemctl", "timedatectl", "netplan", "tee", "chmod", "mv", "cat",
        "iw", "hostnamectl",
    }
    for spec in specs_in(servicectl.sudoers_rule("retro")):
        program = spec.split(" ", 1)[0].rsplit("/", 1)[-1]
        assert program in allowed_programs, spec


@pytest.mark.parametrize(
    "bad",
    ["retro pi", "retro,pi", "retro\npi", "retro\tpi", "retro\rpi",
     "retro ALL=(ALL) NOPASSWD: ALL", "retro\n%wheel ALL=(ALL) NOPASSWD: ALL",
     "retro\x00", "retro ", " retro", "%wheel", "ALL", "Retro", "0retro",
     "retro/pi", "retro:pi", "x" * 33, "", None, 42],
)
def test_a_user_name_that_could_change_what_the_file_means_is_refused(bad):
    # The name is written straight into a file that grants root. Anything that
    # could close one rule and open another has to be refused, not escaped.
    with pytest.raises(ValueError):
        servicectl.sudoers_rule(bad)
