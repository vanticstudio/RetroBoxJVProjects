"""Deciding whether there is a newer release, and what it says.

Two things matter here and nothing else does. The comparison has to be a real
version comparison, because 1.0.10 beats 1.0.9 and string ordering says
otherwise. And the whole thing has to fail silently: a box with no internet is
a television, and a television that stalls at start-up because GitHub is
unreachable is a broken product.
"""

import io
import json
import pathlib
import re
import urllib.error

import pytest

from retrobox import updates


def release(tag, notes="", published="2026-01-01T00:00:00Z", draft=False, prerelease=False):
    return {
        "tag_name": tag,
        "name": tag,
        "body": notes,
        "published_at": published,
        "draft": draft,
        "prerelease": prerelease,
    }


@pytest.fixture
def github(monkeypatch):
    """Answer the releases API without touching the network."""
    def fetch(url, *, timeout):
        fetch.calls.append((url, timeout))
        if isinstance(fetch.answer, Exception):
            raise fetch.answer
        return fetch.answer

    fetch.calls = []
    fetch.answer = "[]"
    monkeypatch.setattr(updates, "_fetch", fetch)
    return fetch


# ==========================================================================
# Version comparison
# ==========================================================================
@pytest.mark.parametrize(
    "older,newer",
    [
        ("1.0.0", "1.0.1"),
        ("1.0.9", "1.0.10"),          # the one string comparison gets wrong
        ("1.9.0", "1.10.0"),
        ("1.0.0", "2.0.0"),
        ("0.9.9", "1.0.0"),
        ("1.0.0", "1.1.0"),
        ("1.0.0-rc1", "1.0.0"),       # a release beats its own candidate
        ("2.0.0-rc2", "2.0.0-rc10"),
    ],
)
def test_newer_versions_sort_after_older_ones(older, newer):
    assert updates.parse_version(older) < updates.parse_version(newer)
    assert updates.is_newer(newer, than=older) is True
    assert updates.is_newer(older, than=newer) is False


def test_the_same_version_is_not_newer():
    assert updates.is_newer("1.2.3", than="1.2.3") is False


def test_a_leading_v_is_the_same_version():
    assert updates.is_newer("v1.0.1", than="1.0.0") is True
    assert updates.is_newer("1.0.1", than="v1.0.1") is False


@pytest.mark.parametrize("junk", ["", "latest", "banana", None, "v", "..", "1.0.0.0.0.x"])
def test_a_tag_that_is_not_a_version_is_never_newer(junk):
    # An odd tag on the repo must not make every box in the field update to it.
    assert updates.is_newer(junk, than="1.0.0") is False


# ==========================================================================
# Checking
# ==========================================================================
def test_a_newer_release_is_found(github):
    github.answer = json.dumps([release("v1.1.0", "## New\n- stuff")])
    result = updates.check(current="1.0.3")

    assert result.available is True
    assert result.latest == "1.1.0"
    assert result.error is None


def test_the_same_or_an_older_release_is_not_an_update(github):
    github.answer = json.dumps([release("v1.0.3"), release("v1.0.2")])
    assert updates.check(current="1.0.3").available is False
    assert updates.check(current="1.2.0").available is False


def test_drafts_and_prereleases_are_ignored(github):
    # A draft is something you are still writing. It must not go to a fleet.
    github.answer = json.dumps([
        release("v2.0.0", draft=True),
        release("v1.9.0", prerelease=True),
        release("v1.1.0"),
    ])
    result = updates.check(current="1.0.3")
    assert result.latest == "1.1.0"


def test_every_version_being_skipped_is_reported_newest_first(github):
    # Somebody who has not switched the box on for six months should see what
    # the six months contained, not just the last line of it.
    github.answer = json.dumps([
        release("v1.0.4", "### Added\n- four"),
        release("v1.1.0", "### Added\n- one one oh"),
        release("v1.0.5", "### Fixed\n- five"),
        release("v1.0.2", "### Added\n- already have this"),
    ])
    result = updates.check(current="1.0.3")
    assert [r["version"] for r in result.releases] == ["1.1.0", "1.0.5", "1.0.4"]


def test_the_release_date_comes_through(github):
    github.answer = json.dumps([release("v1.1.0", published="2026-03-04T09:00:00Z")])
    assert updates.check(current="1.0.0").releases[0]["published"] == "2026-03-04"


# ==========================================================================
# A box with no internet is still a television
# ==========================================================================
@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("Name or service not known"),
        urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b"")),
        TimeoutError("timed out"),
        OSError("network is unreachable"),
        ValueError("something odd"),
    ],
)
def test_no_internet_is_reported_and_never_raised(github, failure):
    github.answer = failure
    result = updates.check(current="1.0.3")
    assert result.available is False
    assert result.error is not None
    assert result.latest is None


@pytest.mark.parametrize(
    "body", ["", "not json", "{}", "null", '{"message": "rate limited"}', "[[]]", "[1,2]"]
)
def test_a_reply_that_makes_no_sense_is_not_an_update(github, body):
    github.answer = body
    result = updates.check(current="1.0.3")
    assert result.available is False


def test_a_release_with_no_tag_is_skipped(github):
    github.answer = json.dumps([{"body": "x"}, release("v1.1.0")])
    assert updates.check(current="1.0.0").latest == "1.1.0"


def test_the_check_is_given_a_timeout(github):
    github.answer = "[]"
    updates.check(current="1.0.0", timeout=7.5)
    assert github.calls[0][1] == 7.5


# ==========================================================================
# Where it looks is not negotiable
# ==========================================================================
def test_the_repository_is_compiled_in(github):
    github.answer = "[]"
    updates.check(current="1.0.0")
    url = github.calls[0][0]
    assert url.startswith("https://api.github.com/repos/")
    assert updates.REPOSITORY in url


def test_no_public_entry_point_accepts_a_source():
    # The dashboard has no authentication. An update path that accepted a URL
    # would let anyone on the LAN run their own code as this user. _fetch takes
    # a url because something has to, but nothing public hands it one - it is
    # only ever called with the module constant.
    import inspect

    public = [updates.check, updates.UpdateChecker.__init__,
              updates.UpdateChecker.check_now]
    for function in public:
        for parameter in inspect.signature(function).parameters:
            assert parameter not in ("url", "repo", "repository", "ref", "source"), (
                f"{function.__qualname__}() accepts {parameter}"
            )


def test_fetch_is_only_ever_called_with_the_constant():
    import ast

    tree = ast.parse(pathlib.Path(updates.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_fetch":
            first = node.args[0]
            assert isinstance(first, ast.Name) and first.id == "RELEASES_URL", (
                "_fetch() is called with something other than the constant"
            )


def test_the_url_is_built_from_the_constant_not_from_input():
    assert updates.RELEASES_URL == (
        f"https://api.github.com/repos/{updates.REPOSITORY}/releases?per_page=30"
    )


# ==========================================================================
# Release notes, rendered rather than injected
# ==========================================================================
def test_headings_lists_and_emphasis_survive():
    html = updates.render_notes("### Added\n\n- a *thing*\n- and `code`\n")
    assert "<h3>Added</h3>" in html
    assert "<li>a <em>thing</em></li>" in html
    assert "<code>code</code>" in html


ALLOWED_TAGS = {
    "p", "ul", "li", "code", "strong", "em", "h3", "h4", "h5", "h6",
}


@pytest.mark.parametrize(
    "attack",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "[link](javascript:alert(1))",
        "<iframe src='http://evil'></iframe>",
        "regular & <b>bold</b> text",
        "<a href=\"javascript:alert(1)\">click</a>",
        "<style>body{display:none}</style>",
        "<!-- <script>alert(1)</script> -->",
    ],
)
def test_nothing_arriving_as_markdown_becomes_live_html(attack):
    # The property is not "the word onerror is absent" - escaped text may say
    # anything at all. It is that the renderer emits ONLY its own small set of
    # tags, so nothing from the network can become an element on the page.
    html = updates.render_notes(attack)
    emitted = {t.lower() for t in re.findall(r"<\s*/?\s*([A-Za-z][A-Za-z0-9]*)", html)}
    assert emitted <= ALLOWED_TAGS, f"{attack!r} produced {emitted - ALLOWED_TAGS}"


def test_the_dangerous_text_is_still_shown_just_not_executed():
    # Escaping is not deleting. The words are still readable.
    html = updates.render_notes("<script>alert(1)</script>")
    assert "&lt;script&gt;" in html


def test_an_empty_release_body_renders_as_nothing_not_a_crash():
    assert updates.render_notes("") == ""
    assert updates.render_notes(None) == ""
