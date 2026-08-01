"""Finding out whether there is a newer Retro Box, and what it changed.

Two rules shape everything in this module.

**Where it looks is compiled in.** :data:`REPOSITORY` is a constant and nothing
in this file - or in the routes that call it - takes a URL, a repository or a
ref from a caller. The dashboard has no authentication by design, which is an
accepted trade for letting a neighbour change the channel; it is emphatically
not an accepted trade for letting them nominate an address to download and
execute code from. That is a different risk class, and the way to not have it
is to make the address unspellable.

**A box with no internet is still a television.** Every failure in here - no
DNS, no route, a timeout, GitHub having a bad afternoon, a reply that is not
JSON - comes back as "could not check", logged and forgotten. Nothing raises,
nothing retries in a tight loop, and nothing runs before the picture is up.

Only tagged releases count. Following a branch would put every unpushed
thought on customer hardware.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

#: Where updates come from. A constant, deliberately: see the module docstring.
REPOSITORY = "vanticstudio/RetroBoxJVProjects"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=30"

#: Sent so GitHub can tell who is asking, and so a fleet is identifiable in
#: their logs if it ever needs to be.
USER_AGENT = "RetroBox/JV-Projects"

DEFAULT_TIMEOUT = 15.0

#: How much to smear the check across, as a fraction of the interval. A fleet
#: that all checks at 03:00 is a fleet that all hits GitHub in one second.
JITTER_FRACTION = 0.25

_VERSION = re.compile(r"\A(\d+)\.(\d+)\.(\d+)(?:[-.]?([0-9A-Za-z.-]+))?\Z")


@dataclass
class UpdateCheck:
    """What one look at the releases API found."""

    current: str
    latest: Optional[str] = None
    available: bool = False
    releases: List[Dict[str, Any]] = field(default_factory=list)
    checked_at: Optional[float] = None
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "available": self.available,
            "releases": self.releases,
            "checked_at": self.checked_at,
            "error": self.error,
        }


# ==========================================================================
# Comparing versions
# ==========================================================================
def parse_version(raw: Any) -> Optional[Tuple[int, int, int, int, str]]:
    """``"v1.0.10"`` into something that sorts correctly, or ``None``.

    The last two components encode the pre-release: ``(1, "")`` for a real
    release and ``(0, "rc2")`` for a candidate, so 1.0.0 sorts after 1.0.0-rc1
    while two candidates still compare sensibly against each other.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text[:1].lower() == "v":
        text = text[1:]
    match = _VERSION.match(text)
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    if pre is None:
        return (int(major), int(minor), int(patch), 1, "")
    return (int(major), int(minor), int(patch), 0, _pad_numbers(pre))


def _pad_numbers(text: str) -> str:
    """Zero-pad digit runs so rc2 sorts before rc10 as a plain string."""
    return re.sub(r"\d+", lambda m: m.group().zfill(6), text)


def is_newer(candidate: Any, *, than: Any) -> bool:
    """Is ``candidate`` a later release than ``than``? Unparseable is never."""
    left, right = parse_version(candidate), parse_version(than)
    if left is None or right is None:
        return False
    return left > right


# ==========================================================================
# Asking GitHub
# ==========================================================================
def _fetch(url: str, *, timeout: float) -> str:
    """One GET, standard library only. The URL is ours, never a caller's."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def check(*, current: str, timeout: float = DEFAULT_TIMEOUT) -> UpdateCheck:
    """Look for a newer tagged release. Never raises.

    Takes no source: see the module docstring. ``current`` is this box's own
    version and is compared against, never sent.
    """
    import time

    result = UpdateCheck(current=current, checked_at=time.time())
    try:
        raw = _fetch(RELEASES_URL, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - no internet is an ordinary state
        log.info("could not check for updates: %s", exc)
        result.error = _explain(exc)
        return result

    try:
        payload = json.loads(raw)
    except ValueError:
        log.info("the releases API replied with something that is not JSON")
        result.error = "the update server replied with something unexpected"
        return result

    if not isinstance(payload, list):
        # A dict here is GitHub's error shape ({"message": "rate limited"}).
        message = payload.get("message") if isinstance(payload, dict) else None
        result.error = message or "the update server replied with something unexpected"
        return result

    newer: List[Dict[str, Any]] = []
    best: Optional[str] = None
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("draft") or item.get("prerelease"):
            # A draft is something still being written; a prerelease is not
            # for a fleet of televisions in people's living rooms.
            continue
        version = parse_version(item.get("tag_name"))
        if version is None:
            continue
        tag = str(item.get("tag_name", "")).lstrip("vV")
        if not is_newer(tag, than=current):
            continue
        newer.append({
            "version": tag,
            "name": str(item.get("name") or tag),
            "published": str(item.get("published_at") or "")[:10],
            "notes": str(item.get("body") or ""),
            "notes_html": render_notes(item.get("body")),
        })
        if best is None or is_newer(tag, than=best):
            best = tag

    newer.sort(key=lambda r: parse_version(r["version"]) or (0, 0, 0, 0, ""), reverse=True)
    result.releases = newer
    result.latest = best
    result.available = best is not None
    return result


def _explain(exc: Exception) -> str:
    """Turn a network failure into something worth showing a person."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"the update server answered {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "could not reach the update server - is this box online?"
    if isinstance(exc, TimeoutError):
        return "the update server took too long to answer"
    return "could not check for updates"


def next_check_delay(interval_seconds: float, *, rng: Optional[random.Random] = None) -> float:
    """The interval, smeared, so a whole fleet does not arrive together."""
    generator = rng or random
    spread = interval_seconds * JITTER_FRACTION
    return max(60.0, interval_seconds + generator.uniform(-spread, spread))


# ==========================================================================
# Release notes
# ==========================================================================
_INLINE = (
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)"), r"<em>\1</em>"),
)


def render_notes(markdown: Any) -> str:
    """Render the small bit of markdown a release body actually uses.

    Everything is escaped first and only then are the handful of patterns we
    understand turned into tags. This is not a markdown renderer and is not
    trying to be one: it is a way of showing text that arrived over the
    network without letting it become live HTML on a page that has no
    authentication in front of it.
    """
    if not isinstance(markdown, str) or not markdown.strip():
        return ""

    out: List[str] = []
    in_list = False
    for line in markdown.splitlines():
        stripped = line.strip()
        safe = html.escape(stripped)
        for pattern, replacement in _INLINE:
            safe = pattern.sub(replacement, safe)

        heading = re.match(r"\A(#{1,6})\s+(.*)\Z", safe)
        bullet = re.match(r"\A[-*+]\s+(.*)\Z", safe)

        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{bullet.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False

        if heading:
            # Clamped to h3 and below: the page's own headings are h1/h2, and
            # text from the network must not outrank them in the outline.
            level = min(max(len(heading.group(1)), 3), 6)
            out.append(f"<h{level}>{heading.group(2)}</h{level}>")
        elif stripped:
            out.append(f"<p>{safe}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


# ==========================================================================
# Checking in the background
# ==========================================================================
class UpdateChecker:
    """Looks for updates on a timer, off the main thread, quietly.

    Started after the box is already playing. It holds the last result in
    memory and nothing waits on it: if the check has never completed, the
    dashboard simply says it has not checked yet.
    """

    def __init__(
        self,
        *,
        current: str,
        interval_seconds: float,
        enabled: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.current = current
        self.interval = interval_seconds
        self.enabled = enabled
        self.timeout = timeout
        self.last: Optional[UpdateCheck] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="update-check", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def check_now(self) -> UpdateCheck:
        """Check on this thread. Used by the "check now" button."""
        if not self.enabled:
            return UpdateCheck(current=self.current, error="update checking is turned off")
        self.last = check(current=self.current, timeout=self.timeout)
        return self.last

    def _loop(self) -> None:
        # A short jittered wait before the first check as well, so a box that
        # has just booted is not competing with its own start-up.
        delay = next_check_delay(min(self.interval, 300.0))
        while not self._stop.wait(delay):
            try:
                self.last = check(current=self.current, timeout=self.timeout)
            except Exception:  # noqa: BLE001 - belt and braces; check() eats its own
                log.debug("update check failed", exc_info=True)
            delay = next_check_delay(self.interval)


__all__ = [
    "DEFAULT_TIMEOUT",
    "JITTER_FRACTION",
    "RELEASES_URL",
    "REPOSITORY",
    "UpdateCheck",
    "UpdateChecker",
    "check",
    "is_newer",
    "next_check_delay",
    "parse_version",
    "render_notes",
]
