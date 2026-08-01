"""Reading the log without opening a terminal.

The single most common reason to SSH into anything is to read a log, so this
is the endpoint that removes the most trips. It is also the easiest one to
make dangerous: a journal is unbounded, and streaming one into a browser is a
good way to take the box down while trying to diagnose it.
"""

import json

import pytest

from retrobox import journal


def entries(*records):
    """What `journalctl -o json` actually emits: one JSON object per line."""
    return "\n".join(json.dumps(r) for r in records) + "\n"


def record(message, unit="retrobox.service", priority="6", stamp=1_700_000_000_000_000):
    return {
        "__REALTIME_TIMESTAMP": str(stamp),
        "__CURSOR": f"s=abc;i={stamp}",
        "PRIORITY": priority,
        "MESSAGE": message,
        "_SYSTEMD_UNIT": unit,
        "SYSLOG_IDENTIFIER": unit.split(".")[0],
    }


@pytest.fixture
def fake_journalctl(monkeypatch):
    """Capture the argv and hand back canned journal output."""
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return runner.output

    runner.output = ""
    runner.calls = calls
    monkeypatch.setattr(journal, "_run", runner)
    return runner


# ==========================================================================
# Reading
# ==========================================================================
def test_lines_are_parsed_into_something_a_page_can_render(fake_journalctl):
    fake_journalctl.output = entries(
        record("starting up"), record("tuned to channel 2"),
    )
    page = journal.read(unit="retrobox.service")

    assert [e["message"] for e in page["entries"]] == [
        "starting up", "tuned to channel 2",
    ]
    assert page["entries"][0]["level"] == "info"
    assert page["entries"][0]["unit"] == "retrobox.service"
    assert page["entries"][0]["time"].startswith("20")


def test_priorities_become_words(fake_journalctl):
    fake_journalctl.output = entries(
        record("bad", priority="3"), record("odd", priority="4"),
        record("chatty", priority="7"),
    )
    assert [e["level"] for e in journal.read()["entries"]] == [
        "error", "warning", "debug",
    ]


def test_a_message_split_into_bytes_is_still_readable(fake_journalctl):
    # journalctl emits MESSAGE as an array of byte values when it is not
    # valid UTF-8. Rendering that as "[104, 105]" helps nobody.
    fake_journalctl.output = entries({**record("x"), "MESSAGE": [104, 105]})
    assert journal.read()["entries"][0]["message"] == "hi"


def test_a_line_that_is_not_json_is_skipped_not_fatal(fake_journalctl):
    fake_journalctl.output = "not json at all\n" + entries(record("fine"))
    assert [e["message"] for e in journal.read()["entries"]] == ["fine"]


def test_an_empty_journal_is_an_empty_page_not_an_error(fake_journalctl):
    fake_journalctl.output = ""
    page = journal.read()
    assert page["entries"] == [] and page["cursor"] is None


# ==========================================================================
# Bounded, always
# ==========================================================================
def test_the_request_is_capped_however_much_is_asked_for(fake_journalctl):
    fake_journalctl.output = ""
    journal.read(lines=10_000_000)
    argv = fake_journalctl.calls[0]
    assert f"--lines={journal.MAX_LINES}" in argv


def test_a_sensible_request_is_passed_through(fake_journalctl):
    fake_journalctl.output = ""
    journal.read(lines=50)
    assert "--lines=50" in fake_journalctl.calls[0]


def test_a_silly_line_count_does_not_become_zero_or_negative(fake_journalctl):
    fake_journalctl.output = ""
    for asked in (0, -1, -999):
        fake_journalctl.calls.clear()
        journal.read(lines=asked)
        assert f"--lines={journal.DEFAULT_LINES}" in fake_journalctl.calls[0]


def test_output_is_truncated_even_if_journalctl_ignores_the_cap(fake_journalctl):
    # Belt and braces: the cap is asked for AND enforced on what comes back,
    # so a journalctl that misbehaves cannot flood the browser.
    fake_journalctl.output = entries(*[record(f"line {i}") for i in range(journal.MAX_LINES + 50)])
    page = journal.read(lines=journal.MAX_LINES)
    assert len(page["entries"]) == journal.MAX_LINES
    assert page["truncated"] is True


def test_a_reasonable_page_is_not_marked_truncated(fake_journalctl):
    fake_journalctl.output = entries(record("a"), record("b"))
    assert journal.read()["truncated"] is False


# ==========================================================================
# Choosing what to look at
# ==========================================================================
def test_a_unit_can_be_picked(fake_journalctl):
    fake_journalctl.output = ""
    journal.read(unit="retrobox-web.service")
    assert "--unit=retrobox-web.service" in fake_journalctl.calls[0]


@pytest.mark.parametrize(
    "unit", ["sshd.service", "../../etc", "retrobox.service; rm -rf /", "", "x" * 300]
)
def test_only_this_products_units_may_be_read(fake_journalctl, unit):
    # The unit name arrives from the network. Nothing on this box needs the
    # dashboard to be a general-purpose journal reader.
    with pytest.raises(ValueError):
        journal.read(unit=unit)


def test_both_units_together_is_the_default(fake_journalctl):
    fake_journalctl.output = ""
    journal.read()
    argv = fake_journalctl.calls[0]
    assert "--unit=retrobox.service" in argv
    assert "--unit=retrobox-web.service" in argv


def test_a_minimum_level_can_be_asked_for(fake_journalctl):
    fake_journalctl.output = ""
    journal.read(level="warning")
    assert "--priority=4" in fake_journalctl.calls[0]


def test_a_level_that_is_not_a_level_is_refused(fake_journalctl):
    with pytest.raises(ValueError):
        journal.read(level="loud")


def test_searching_filters_what_comes_back(fake_journalctl):
    fake_journalctl.output = entries(
        record("tuned to channel 2"), record("volume up"), record("tuned to channel 5"),
    )
    page = journal.read(search="tuned")
    assert [e["message"] for e in page["entries"]] == [
        "tuned to channel 2", "tuned to channel 5",
    ]


def test_searching_is_case_insensitive(fake_journalctl):
    fake_journalctl.output = entries(record("Tuned To Channel 2"))
    assert len(journal.read(search="tuned to")["entries"]) == 1


def test_the_search_is_plain_text_not_a_pattern(fake_journalctl):
    # A search box that quietly accepts regular expressions is a search box
    # that hangs the box on a bad one.
    fake_journalctl.output = entries(record("a.b"), record("axb"))
    assert [e["message"] for e in journal.read(search="a.b")["entries"]] == ["a.b"]


# ==========================================================================
# Paging back
# ==========================================================================
def test_a_cursor_comes_back_so_the_page_can_ask_for_more(fake_journalctl):
    fake_journalctl.output = entries(record("one"), record("two", stamp=1_700_000_001))
    assert journal.read()["cursor"] == "s=abc;i=1700000001"


def test_paging_back_uses_the_cursor(fake_journalctl):
    fake_journalctl.output = ""
    journal.read(after="s=abc;i=42")
    argv = fake_journalctl.calls[0]
    assert "--after-cursor=s=abc;i=42" in argv


@pytest.mark.parametrize("cursor", ["--boot", "; rm -rf /", "\x00", "x" * 5000])
def test_a_hostile_cursor_is_refused(fake_journalctl, cursor):
    with pytest.raises(ValueError):
        journal.read(after=cursor)


# ==========================================================================
# When there is no journal at all
# ==========================================================================
def test_no_journalctl_on_this_machine_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(journal, "_run", lambda cmd, **kw: None)
    page = journal.read()
    assert page["entries"] == []
    assert page["available"] is False
    assert "journal" in page["note"].lower()


def test_a_working_journal_says_it_is_available(fake_journalctl):
    fake_journalctl.output = entries(record("hello"))
    assert journal.read()["available"] is True
