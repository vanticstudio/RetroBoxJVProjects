"""The read-modify-write cycle behind every change the dashboard makes.

The rule this file exists to enforce: the box must never end up holding a
config it cannot boot from. Every path that writes goes through a validation
round trip first, and anything that fails it leaves the old file in place.
"""

import threading
import time

import pytest
import yaml

from retrobox.config import ConfigError, load_config
from retrobox.configstore import ConfigStore
from tests.helpers import make_show


def _media(tmp_path, *shows):
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    for name in shows:
        make_show(root, name, 2)
    return root


def _store(tmp_path, body):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return ConfigStore(path), path


def _basic(tmp_path):
    root = _media(tmp_path, "sitcoms", "movies")
    return _store(
        tmp_path,
        f"""# hand written, with a comment
media_root: {root}
tune_in: random

channels:
  - number: 2
    name: "Sitcoms"
    path: {root / 'sitcoms'}
  - number: 3
    name: "Movies"
    path: {root / 'movies'}
""",
    )


# -- the round trip --------------------------------------------------------
def test_a_change_survives_being_written_and_read_back(tmp_path):
    store, path = _basic(tmp_path)

    def rename(data):
        data["channels"][0]["name"] = "Classic Sitcoms"

    store.update(rename)
    assert [c.name for c in load_config(path).channels] == ["Classic Sitcoms", "Movies"]


def test_paths_come_back_as_plain_yaml_strings(tmp_path):
    # A Path object dumped by PyYAML becomes a !!python/object tag that
    # safe_load then refuses to read - a config that writes but never loads.
    store, path = _basic(tmp_path)
    store.update(lambda data: data["channels"].append(
        {"number": 4, "name": "Extra", "path": tmp_path / "media" / "sitcoms"}
    ))
    assert "!!python" not in path.read_text()
    assert load_config(path).channels[2].name == "Extra"


def test_unrelated_settings_and_per_channel_options_survive(tmp_path):
    root = _media(tmp_path, "sitcoms")
    store, path = _store(
        tmp_path,
        f"""media_root: {root}
tune_in: broadcast
sleep_timer: [15, 45]
channels:
  - number: 2
    name: "Sitcoms"
    path: {root / 'sitcoms'}
    shuffle: false
    exclude: ["*live aid*"]
""",
    )
    store.update(lambda data: data["channels"][0].update({"name": "Renamed"}))

    reloaded = load_config(path)
    assert reloaded.tune_in == "broadcast"
    assert reloaded.sleep_steps == (15, 45)
    assert reloaded.channels[0].shuffle is False
    assert reloaded.channels[0].exclude == ("*live aid*",)


# -- never write a config the box cannot boot from -------------------------
def test_a_change_that_would_not_load_is_never_written(tmp_path):
    store, path = _basic(tmp_path)
    before = path.read_text()

    def collide(data):
        data["channels"][1]["number"] = 2  # duplicate of channel 2

    with pytest.raises(ConfigError):
        store.update(collide)

    assert path.read_text() == before, "a broken config reached the disk"


def test_the_validation_is_of_the_text_that_would_actually_be_written(tmp_path):
    # Validating the in-memory dict is not enough: it is the YAML text that the
    # box boots from, so that is what has to be proved loadable.
    store, path = _basic(tmp_path)
    before = path.read_text()

    def wreck(data):
        data["channels"] = "not a list at all"

    with pytest.raises(ConfigError):
        store.update(wreck)
    assert path.read_text() == before


def test_a_mutator_that_raises_leaves_the_file_alone(tmp_path):
    store, path = _basic(tmp_path)
    before = path.read_text()

    def explode(data):
        data["channels"][0]["name"] = "half done"
        raise ValueError("mutator blew up")

    with pytest.raises(ValueError):
        store.update(explode)
    assert path.read_text() == before


# -- the trap: a config with no explicit channel list ----------------------
def test_editing_a_media_root_only_config_keeps_every_channel(tmp_path):
    # With no `channels:` key the loader discovers them from the folders. Write
    # back only the one being edited and the rest of the lineup disappears.
    root = _media(tmp_path, "sitcoms", "movies", "cartoons")
    store, path = _store(tmp_path, f"media_root: {root}\n")
    assert [c.name for c in store.load().channels] == ["Cartoons", "Movies", "Sitcoms"]

    def rename_first(data):
        data["channels"][0]["name"] = "Saturday Cartoons"

    store.update(rename_first)
    names = [c.name for c in load_config(path).channels]
    assert names == ["Saturday Cartoons", "Movies", "Sitcoms"], (
        "editing one channel silently deleted the others"
    )


def test_channels_written_by_hand_without_numbers_become_addressable(tmp_path):
    # The loader defaults a missing `number` to its position. The dashboard
    # addresses channels BY number, so they have to be written down explicitly
    # before an edit can refer to one.
    root = _media(tmp_path, "sitcoms", "movies")
    store, path = _store(
        tmp_path,
        f"media_root: {root}\nchannels:\n"
        f"  - path: {root / 'sitcoms'}\n"
        f"  - path: {root / 'movies'}\n",
    )
    store.update(lambda data: None)
    written = yaml.safe_load(path.read_text())
    assert [c["number"] for c in written["channels"]] == [2, 3]
    assert [c["name"] for c in written["channels"]] == ["Sitcoms", "Movies"]


# -- the documented cost ---------------------------------------------------
def test_comments_are_lost_but_the_backup_still_has_them(tmp_path):
    # The tradeoff, pinned: a PyYAML round trip drops comments, which is why
    # the once-only backup matters so much.
    store, path = _basic(tmp_path)
    store.update(lambda data: data["channels"][0].update({"name": "Renamed"}))

    assert "# hand written, with a comment" not in path.read_text()
    backup = tmp_path / "config.yaml.bak"
    assert "# hand written, with a comment" in backup.read_text()


def test_the_backup_is_the_config_from_before_the_first_edit(tmp_path):
    store, path = _basic(tmp_path)
    original = path.read_text()
    store.update(lambda data: data["channels"][0].update({"name": "One"}))
    store.update(lambda data: data["channels"][0].update({"name": "Two"}))

    assert (tmp_path / "config.yaml.bak").read_text() == original
    assert load_config(path).channels[0].name == "Two"


# -- two browser tabs at once ----------------------------------------------
def test_simultaneous_edits_do_not_lose_each_other(tmp_path):
    # Flask serves with threads. Two people on two phones, or one person
    # double-tapping, must not end up with one edit overwriting the other.
    root = _media(tmp_path, "sitcoms")
    store, path = _store(
        tmp_path,
        f"media_root: {root}\nchannels:\n  - number: 2\n    name: A\n"
        f"    path: {root / 'sitcoms'}\n",
    )

    def add(number):
        def mutate(data):
            time.sleep(0.01)  # widen the window an unlocked version would lose
            data["channels"].append(
                {"number": number, "name": f"Ch{number}", "path": str(root / "sitcoms")}
            )
        store.update(mutate)

    threads = [threading.Thread(target=add, args=(n,)) for n in range(10, 18)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    numbers = sorted(c.number for c in load_config(path).channels)
    assert numbers == [2, 10, 11, 12, 13, 14, 15, 16, 17], "an edit was lost"


@pytest.mark.parametrize(
    "name", ["No", "Yes", "On", "Off", "Null", "22:00", "1.0", "~", "- Movies"]
)
def test_a_channel_name_yaml_would_misread_survives_as_text(tmp_path, name):
    # Unquoted, YAML 1.1 reads "No" as false and "22:00" as a sexagesimal
    # number. A channel called "No" that came back as False would be a config
    # the box cannot load - written by the box itself, on a user's say-so.
    store, path = _basic(tmp_path)
    store.update(lambda data: data["channels"][0].update({"name": name}))

    assert load_config(path).channels[0].name == name
    assert yaml.safe_load(path.read_text())["channels"][0]["name"] == name
