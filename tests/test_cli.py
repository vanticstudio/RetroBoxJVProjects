import pytest

from retrobox.__main__ import _cmd_check, main
from retrobox.config import config_from_dict
from tests.helpers import make_show


def _config(tmp_path, **extra):
    make_show(tmp_path, "adultswim", 3)
    data = {
        "channels": [
            {"number": 2, "name": "Adult Swim", "path": str(tmp_path / "adultswim")}
        ]
    }
    data.update(extra)
    return config_from_dict(data)


def _write_config_file(tmp_path, body):
    make_show(tmp_path, "adultswim", 3)
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


# -- --check ---------------------------------------------------------------
def test_check_lists_channels_and_succeeds(tmp_path, capsys):
    assert _cmd_check(_config(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "configuration OK" in out
    assert "CH   2  Adult Swim" in out
    assert "3 episodes" in out
    assert "total episodes: 3" in out


def test_check_fails_when_nothing_would_play(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "Dead Air", "path": str(tmp_path / "empty")}]}
    )
    assert _cmd_check(config) == 1
    assert "NO EPISODES FOUND" in capsys.readouterr().out


def test_check_reports_dayparts(tmp_path, capsys):
    make_show(tmp_path, "afterdark", 2)
    config = _config(
        tmp_path,
        channels=[
            {
                "number": 2,
                "name": "Adult Swim",
                "path": str(tmp_path / "adultswim"),
                "dayparts": [
                    {"from": "22:00", "to": "02:00", "name": "AFTER DARK",
                     "path": str(tmp_path / "afterdark")},
                    {"from": "02:00", "to": "06:00", "off_air": True},
                ],
            }
        ],
    )
    assert _cmd_check(config) == 0
    out = capsys.readouterr().out
    assert "22:00-02:00" in out and "AFTER DARK" in out
    assert "02:00-06:00" in out and "off air" in out
    assert "total episodes: 5" in out   # 3 base + 2 in the daypart folder


def test_check_reports_bumpers(tmp_path, capsys):
    make_show(tmp_path, "bumps", 4)
    assert _cmd_check(_config(tmp_path, bumpers=str(tmp_path / "bumps"))) == 0
    assert "station bumpers: 4 clips" in capsys.readouterr().out


def test_check_warns_about_an_empty_bumper_folder(tmp_path, capsys):
    (tmp_path / "bumps").mkdir()
    _cmd_check(_config(tmp_path, bumpers=str(tmp_path / "bumps")))
    assert "NO CLIPS FOUND" in capsys.readouterr().out


def test_check_reports_the_sleep_ladder(tmp_path, capsys):
    _cmd_check(_config(tmp_path))
    assert "sleep timer: 30m -> 60m -> 90m -> off" in capsys.readouterr().out

    _cmd_check(_config(tmp_path, sleep_timer=False))
    assert "sleep timer: disabled" in capsys.readouterr().out


def test_check_rejects_a_bad_key_override(tmp_path, capsys):
    config = _config(tmp_path, input={"key_overrides": {"KEY_F1": "teleport"}})
    assert _cmd_check(config) == 2
    assert "unknown action" in capsys.readouterr().out


def test_check_counts_key_overrides(tmp_path, capsys):
    config = _config(tmp_path, input={"key_overrides": {"KEY_F1": "guide"}})
    assert _cmd_check(config) == 0
    assert "key overrides: 1 configured" in capsys.readouterr().out


# -- main() ----------------------------------------------------------------
def test_main_check_reads_the_config_file(tmp_path, capsys):
    path = _write_config_file(
        tmp_path,
        f"channels:\n  - number: 2\n    name: Adult Swim\n"
        f"    path: {tmp_path / 'adultswim'}\n",
    )
    assert main(["--check", "--config", str(path)]) == 0
    assert "configuration OK" in capsys.readouterr().out


def test_main_reports_a_missing_config(tmp_path, capsys):
    assert main(["--check", "--config", str(tmp_path / "nope.yaml")]) == 2


def test_main_reports_an_invalid_config(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("tune_in: sideways\nmedia_root: /nowhere\n")
    assert main(["--check", "--config", str(path)]) == 2


def test_main_version_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "retrobox" in capsys.readouterr().out


# -- --setup wiring --------------------------------------------------------
# setup_wizard.main() builds its own argparse over sys.argv, so the CLI must
# hand it a private argv. These pin that down: passing our own flags through
# would make the wizard die on "unrecognized arguments".
def _capture_wizard(monkeypatch, exit_code=None):
    import sys as _sys

    from retrobox import setup_wizard

    seen = {}

    def fake_main():
        seen["argv"] = list(_sys.argv)
        if exit_code is not None:
            raise SystemExit(exit_code)

    monkeypatch.setattr(setup_wizard, "main", fake_main)
    return seen


def test_setup_hides_our_own_flags_from_the_wizard(monkeypatch):
    seen = _capture_wizard(monkeypatch)
    assert main(["--setup"]) == 0
    assert "--setup" not in seen["argv"]
    assert "--log-level" not in seen["argv"]


def test_setup_forwards_config_as_the_wizard_output(monkeypatch):
    seen = _capture_wizard(monkeypatch)
    assert main(["--setup", "--config", "/tmp/somewhere.yaml"]) == 0
    assert seen["argv"][1:] == ["--output", "/tmp/somewhere.yaml"]


def test_setup_restores_argv_afterwards(monkeypatch):
    import sys as _sys

    before = list(_sys.argv)
    _capture_wizard(monkeypatch)
    main(["--setup"])
    assert _sys.argv == before


def test_setup_propagates_the_wizards_exit_code(monkeypatch):
    _capture_wizard(monkeypatch, exit_code=1)
    assert main(["--setup"]) == 1


def test_setup_restores_argv_even_when_the_wizard_exits(monkeypatch):
    import sys as _sys

    before = list(_sys.argv)
    _capture_wizard(monkeypatch, exit_code=1)
    main(["--setup"])
    assert _sys.argv == before
