"""The clock: is it right, does it stay right, and does it know where it is.

On most products the clock is plumbing. Here it changes what the television
*shows*. :mod:`retrobox.daypart` swaps a channel's name and its folder by the
time of day - cartoons in the morning, sitcoms at night - and it does that
without ever consulting anything but the local clock. So a box whose clock is
wrong does not error, does not warn and does not look broken. It just plays
the wrong thing, and the owner concludes the feature does not work.

Two ways that happens in the field, and this module covers both.

**The box does not know where it is.** Ubuntu ships systemd-timesyncd, so a
networked box almost certainly has correct UTC. What it does not have is the
right *timezone*, because nothing ever told it: it sits on the installer's
Etc/UTC and every daypart fires at the wrong hour. So on a box that has never
been told, and only then, this asks a public service what timezone its public
address is in and sets it - once.

**The battery on the motherboard is flat.** A CR2032 coin cell is one of the
most common faults on a ten-year-old office mini PC, and these boxes are
switched off at the wall. A box with a flat cell comes up with an absurd date
every single time it loses power. With internet, timesyncd fixes it within a
minute and nobody ever finds out; without internet, it stays wrong forever.
Either way the owner cannot guess at a two-dollar part unless the box says so,
so this detects an implausible clock and says what it *means*.

Three rules hold the rest of it up.

* **Nothing here may delay the picture.** Every outbound call is on a daemon
  thread with a short timeout, everything is best-effort, and no function in
  this module raises into a caller that is trying to start a television.
* **One privileged path.** The zone is set by :func:`servicectl.set_timezone`
  and nothing else. That function checks the zone against what this machine's
  own ``timedatectl list-timezones`` returned - a whitelist, not a sanitiser -
  which is what stops a hostile or corrupted lookup response reaching a
  command that runs as root. This module adds a *second, narrower* gate in
  front of it (see :func:`is_named_zone`) and no way around it.
* **Nothing about this box is sent anywhere.** The lookup is the one outbound
  call this feature adds. It carries the request and nothing else: no serial,
  no version, no identifier, no query parameters, and it can be turned off.

What is deliberately *not* here: a second time sync. systemd-timesyncd is
already on the box and already correct; a second implementation would be one
more thing to get wrong and one more thing to disagree with.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone as _utc
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .servicectl import set_timezone

log = logging.getLogger(__name__)


# ==========================================================================
# Where the record lives, and why it is a file of its own
# ==========================================================================
#: The record sits beside config.yaml, the same as the network and update
#: records do. It is deliberately NOT in config.yaml itself: it is bookkeeping
#: - who last chose the zone, what network we were on, whether the clock was
#: wrong at boot - not a setting anybody would edit, and writing config.yaml
#: from a background thread during start-up is a risk with no upside. Keeping
#: it separate also gets the factory-reset behaviour for free: wiping the box's
#: settings wipes this, and detection is then free to run again, which is
#: exactly what should happen to a box that has been handed to somebody else.
STATE_NAME = ".retrobox-time.json"

#: Who decided the timezone this box is running. The difference matters more
#: than anything else in the file: detection is for a box that has never been
#: told where it is, never for one that knows better than its owner.
SOURCE_DETECTED = "detected"        # this module set it
SOURCE_MANUAL = "manual"            # a person picked it in the dashboard
SOURCE_PREEXISTING = "pre-existing" # already a real zone before we ever looked
SOURCE_UNKNOWN = "unknown"          # no record, and nothing chosen yet


# ==========================================================================
# A named zone, and never an offset
# ==========================================================================
#: The ten areas the tz database uses for real places. A zone must begin with
#: one of them.
#:
#: This is what makes an offset *unrepresentable* rather than merely
#: discouraged. A named zone like ``Australia/Melbourne`` carries the whole
#: history and future of that place's daylight saving, so it is right forever.
#: A fixed offset is wrong for half of every year, and offsets have a nasty
#: habit of arriving dressed as names: ``UTC+10``, ``GMT+10``, and worst of
#: all ``Etc/GMT-10``, which is a genuine entry in the tz database, appears in
#: ``timedatectl list-timezones``, and would sail straight through any
#: whitelist built from that list. Requiring a real geographic area refuses
#: every one of them.
#:
#: The cost is the legacy single-word aliases - ``Japan``, ``Israel``,
#: ``US/Eastern`` - which are refused too. Every one of them has a modern
#: ``Area/Place`` spelling that geo-IP services return anyway, and a refusal
#: here changes nothing on the box, so the cost is nil.
GEOGRAPHIC_AREAS = frozenset({
    "Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic",
    "Australia", "Europe", "Indian", "Pacific",
})

#: ``Area/Place`` or ``Area/Region/Place`` (America/Argentina/Buenos_Aires).
_ZONE_SHAPE = re.compile(r"\A[A-Za-z]+(?:/[A-Za-z0-9_+-]+){1,2}\Z")

#: The loosest thing this module will write into its own record as a zone
#: somebody chose by hand. It is deliberately wider than
#: :func:`is_named_zone`, because an owner is allowed to pick UTC on purpose
#: and the whole point of the record is to remember that they did. It has
#: already been through servicectl's whitelist by the time it gets here; this
#: only stops obvious rubbish being written to a file.
_RECORDABLE = re.compile(r"\A[A-Za-z][A-Za-z0-9+_-]*(?:/[A-Za-z0-9+._-]+){0,2}\Z")


def is_named_zone(value: Any) -> bool:
    """True only for a named zone belonging to a real place on Earth.

    Everything this module is about to do with a zone - set it as the system
    timezone, record it, offer it - goes through here first. See
    :data:`GEOGRAPHIC_AREAS` for why the bar is geography rather than shape.
    """
    if not isinstance(value, str):
        return False
    if len(value) > 64 or not _ZONE_SHAPE.match(value):
        return False
    return value.split("/", 1)[0] in GEOGRAPHIC_AREAS


# ==========================================================================
# The lookup
# ==========================================================================
#: How long one provider gets. Short on purpose: this runs on a box that is
#: starting up, and a lookup that takes ten seconds on a flaky connection is a
#: lookup that is holding a thread open for no reason. Nothing waits for it,
#: but nothing should hang around either.
LOOKUP_TIMEOUT = 4.0

#: Everything a sane answer to "what timezone is this address in" fits inside
#: many times over. Enforced on the socket as well as on what comes back, so a
#: provider that has been replaced by something that streams forever cannot
#: fill this box's memory.
MAX_RESPONSE_BYTES = 8192

#: What the request says about itself, which is as close to nothing as an HTTP
#: request gets. Not the product name, not the version, not a serial: the same
#: string from every box on Earth, so it cannot tell one customer from
#: another. It is set explicitly rather than left to urllib because urllib's
#: default announces the exact Python version, and there is no reason to hand
#: anybody even that.
USER_AGENT = "Python-urllib"


def _zone_at(data: Any, *keys: str) -> Optional[str]:
    """Walk a path of keys through parsed JSON, or give up."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data if isinstance(data, str) else None


@dataclass(frozen=True)
class Provider:
    """One public service that will say what timezone an address is in."""

    name: str
    url: str
    #: Pulls (zone, public address) out of parsed JSON. Either may be None.
    read: Callable[[Any], Tuple[Optional[str], Optional[str]]]


#: The services this box may ask, in order, until one gives a usable answer.
#:
#: **Why these two.** Both are HTTPS, both need no account and no API key -
#: which matters on a product sold to other people, because a key would have
#: to be either baked into the image (and so published) or asked of a customer
#: who should never have to know what an API key is. Both answer with a *named
#: zone*, not an offset. Neither is asked for anything but the timezone.
#:
#: **HTTPS is not optional here.** The popular keyless service ip-api.com is
#: plaintext HTTP on its free tier, which would let anyone between this box
#: and the internet choose its timezone. The whitelists downstream mean the
#: worst they could achieve is a wrong-but-real zone rather than anything
#: privileged, but "a stranger can decide what your television shows at
#: breakfast" is still not a thing to ship.
#:
#: **The day one of them disappears.** Nothing happens. A dead host, a moved
#: URL, an HTML error page, a quota message, a rebranded API that answers with
#: something else entirely - all of it fails the same three gates below, the
#: box changes nothing, keeps the zone it has, writes a line in the journal
#: and tries again the next time it looks like it moved. Detection going away
#: leaves the box exactly where it was before this module existed: the owner
#: picks the zone on the System page. The second provider exists so that one
#: service disappearing is not even that much of an event.
PROVIDERS: Tuple[Provider, ...] = (
    Provider(
        name="ipapi.co",
        url="https://ipapi.co/json/",
        read=lambda d: (_zone_at(d, "timezone"), _zone_at(d, "ip")),
    ),
    Provider(
        name="ipwho.is",
        url="https://ipwho.is/",
        read=lambda d: (_zone_at(d, "timezone", "id"), _zone_at(d, "ip")),
    ),
)


def _urlopen(url: str, timeout: float) -> bytes:
    """The one outbound call in this feature. Sends the request and nothing.

    No cookies, no query string, no body, no identifying header. The read is
    bounded at the socket so nothing can be streamed at this box.
    """
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_RESPONSE_BYTES + 1)


Opener = Callable[[str, float], bytes]


def _ask(provider: Provider, opener: Opener,
         timeout: float) -> Tuple[Optional[str], Optional[str]]:
    """Ask one provider. Returns (zone, public address), or (None, None).

    Never raises. Every way a response can fail to be an answer - no network,
    a timeout, an HTML error page, truncated JSON, a null, a nested object
    where a string was expected, something enormous - lands in the same place,
    which is "we did not find out".
    """
    try:
        body = opener(provider.url, timeout)
    except AssertionError:
        # The test suite's guard against a call that would reach the real
        # internet. It must stay loud rather than becoming a log line.
        raise
    except Exception:  # noqa: BLE001 - no internet is an ordinary outcome
        log.debug("timezone lookup: %s did not answer", provider.name,
                  exc_info=True)
        return None, None

    if not isinstance(body, (bytes, bytearray)):
        return None, None
    if len(body) > MAX_RESPONSE_BYTES:
        log.warning("timezone lookup: %s answered with %d bytes, which is not "
                    "an answer to this question", provider.name, len(body))
        return None, None

    try:
        data = json.loads(body.decode("utf-8", errors="strict"))
        zone, address = provider.read(data)
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 - rubbish is an ordinary outcome too
        log.debug("timezone lookup: could not read what %s said",
                  provider.name, exc_info=True)
        return None, None

    if zone is not None and not isinstance(zone, str):
        zone = None
    if address is not None and (not isinstance(address, str) or len(address) > 64):
        address = None
    return zone, address


def look_up(*, opener: Optional[Opener] = None,
            timeout: float = LOOKUP_TIMEOUT,
            providers: Sequence[Provider] = PROVIDERS) -> Dict[str, Any]:
    """What timezone does the internet think this box's address is in.

    ``zone`` is either a named geographic zone or None - nothing else ever
    comes out of here, because :func:`is_named_zone` is applied before the
    answer leaves this function. ``address`` is a hash, never the address
    itself; see :func:`_fingerprint`. ``refused`` holds anything that came
    back and was thrown away, which is what lets the caller tell "nobody
    answered" apart from "somebody answered with rubbish" - two different
    things to a person reading the journal.
    """
    opener = opener or _urlopen
    refused: list = []
    for provider in providers:
        zone, address = _ask(provider, opener, timeout)
        if zone is None and address is None:
            continue           # that provider is not answering; try the next
        if not is_named_zone(zone):
            if zone is not None:
                log.warning("timezone lookup: %s said %r, which is not a named "
                            "zone for a real place - ignoring it",
                            provider.name, zone[:64])
                refused.append(zone[:64])
            continue
        log.info("timezone lookup: %s says this box is in %s", provider.name, zone)
        return {"zone": zone, "address": _fingerprint(address), "refused": refused}
    return {"zone": None, "address": None, "refused": refused}


def _fingerprint(value: Optional[str]) -> Optional[str]:
    """A public IP address, remembered without being kept.

    The only question ever asked of the previous address is "is this the same
    one", so there is no reason to hold a durable record of where a customer
    lives. A hash answers that question exactly as well.
    """
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


# ==========================================================================
# Has this box moved?
# ==========================================================================
def network_fingerprint(*, runner: Optional[Callable[[], str]] = None) -> Optional[str]:
    """Something cheap and local that changes when the box changes network.

    The gate on re-detection has to be answerable *without* making a request,
    or "only look when the box moved" would mean looking on every boot to find
    out whether it had. The default route - which gateway, on which interface -
    is free, needs no privilege, and changes when the box is plugged into a
    different network. It does not change when a VPN comes up, which is the
    case that would otherwise have a customer's box hopping timezones.

    None when it cannot be worked out, and None compares equal to None, so a
    box that cannot answer this question stops asking the internet rather than
    asking it forever. That is the conservative direction: the failure is a
    box that does not notice it moved, not a box that phones out every boot.
    """
    try:
        text = runner() if runner else _run(["ip", "route", "show", "default"])
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001
        return None
    match = re.search(r"\bvia\s+(\S+)\s+dev\s+(\S+)", text or "")
    if not match:
        return None
    return f"{match.group(1)} {match.group(2)}"


def _run(argv: Sequence[str], *, timeout: float = 5.0) -> str:
    """Run a command for a reading. "" for every way it can go wrong."""
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True,
                                timeout=timeout, check=False)
        return result.stdout or ""
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 - a missing binary is an ordinary outcome
        log.debug("could not run %s", argv, exc_info=True)
        return ""


# ==========================================================================
# The record
# ==========================================================================
def read_state(path: Any) -> Dict[str, Any]:
    """The record, or an empty one. Never raises, never half-reads."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(path: Any, data: Dict[str, Any]) -> bool:
    """Write the record down. Says whether it landed; never raises.

    A box that cannot write this is a box that will look again next boot,
    which is a wasted request rather than a wrong television, so this is not
    worth failing anything over.
    """
    from .configwrite import atomic_write_text

    try:
        atomic_write_text(Path(path), json.dumps(data, indent=1, sort_keys=True))
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001
        log.warning("could not write down what this box knows about its clock",
                    exc_info=True)
        return False
    return True


def record_manual_timezone(path: Any, zone: str, *,
                           clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """Remember that a person chose this zone, so detection never undoes it.

    Called by the dashboard straight after ``servicectl.set_timezone``
    succeeds. This is the whole of the difference between "nobody has ever
    told this box where it is" and "somebody told it, and they meant it" - and
    it deliberately accepts zones :func:`is_named_zone` would refuse, because
    an owner is allowed to pick UTC on purpose and this must remember that
    they did.
    """
    if not isinstance(zone, str) or not _RECORDABLE.match(zone) or len(zone) > 64:
        raise ValueError(f"{zone!r} is not a timezone name")
    state = read_state(path)
    state.update({
        "source": SOURCE_MANUAL,
        "zone": zone,
        "chosen_at": float(clock()),
    })
    write_state(path, state)
    log.info("timezone %s was chosen by hand; detection will not change it", zone)
    return state


def _owner_chose(state: Dict[str, Any], current: Optional[str]) -> Optional[str]:
    """Which flavour of "leave it alone" applies, or None to go ahead.

    Two ways a box can already know better than we do. The obvious one is the
    record saying a person picked it. The other has no record at all: a box
    sitting on a real geographic zone that this module did not set was put
    there by somebody - the installer, a person at a keyboard - long before
    detection existed, and overriding that would be the same mistake.
    """
    source = state.get("source")
    if source in (SOURCE_MANUAL, SOURCE_PREEXISTING):
        return source
    if source == SOURCE_DETECTED:
        # Belt and braces for the one way this could go badly wrong in the
        # field. The record is written by the dashboard when somebody uses the
        # picker; if that call is ever missed - a wiring mistake, an older
        # dashboard, a zone changed by some other route entirely - the record
        # would still say "detected" and this would feel entitled to change it
        # back. So the *live* zone is the tie-breaker: if the box is not on the
        # zone we set, somebody moved it, and that outranks the record.
        if is_named_zone(current) and current != state.get("zone"):
            return SOURCE_MANUAL
        return None
    return SOURCE_PREEXISTING if is_named_zone(current) else None


# ==========================================================================
# Detection: the whole decision, in one place
# ==========================================================================
#: The single privileged path. Bound here by name rather than wrapped, so
#: there is one function that can set this box's timezone and this module
#: cannot come to have a second one without that being obvious.
DEFAULT_SETTER = set_timezone


def _default_zones() -> Sequence[str]:
    from .sysinfo import timezones

    return timezones()


def _default_current_zone() -> Optional[str]:
    from .sysinfo import timezone as clock_info

    return clock_info().get("timezone")


def detect_once(
    *,
    state_path: Any,
    enabled: bool = True,
    opener: Optional[Opener] = None,
    setter: Optional[Callable[..., str]] = None,
    zones: Optional[Callable[[], Sequence[str]]] = None,
    current_zone: Optional[Callable[[], Optional[str]]] = None,
    fingerprint: Optional[Callable[[], Optional[str]]] = None,
    clock: Callable[[], float] = time.time,
    timeout: float = LOOKUP_TIMEOUT,
    providers: Sequence[Provider] = PROVIDERS,
) -> Dict[str, Any]:
    """Work out this box's timezone from its address, once, if it should.

    Never raises. The answer is a report the dashboard can show, whose
    ``action`` is one of:

    ``disabled``    the owner turned detection off; nothing was sent.
    ``unchanged``   nothing to do - same network, or the same public address
                    as last time, so this box has not moved.
    ``manual``      somebody chose the zone; we report what we think and stop.
    ``set``         the box had never been told, and now it has.
    ``no-answer``   nobody answered, or nothing usable came back.
    ``refused``     something came back and it was not a named zone for a real
                    place, so it went no further.
    ``failed``      the zone could not be set (see the note).
    """
    result: Dict[str, Any] = {
        "action": "failed", "zone": None, "suggested": None,
        "source": None, "note": "", "at": None,
    }
    try:
        return _detect_once(
            result,
            state_path=state_path, enabled=enabled, opener=opener,
            setter=setter or DEFAULT_SETTER,
            zones=zones or _default_zones,
            current_zone=current_zone or _default_current_zone,
            fingerprint=fingerprint or network_fingerprint,
            clock=clock, timeout=timeout, providers=providers,
        )
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 - the television comes first, always
        log.warning("working out this box's timezone did not finish", exc_info=True)
        result["note"] = "this box could not work out where it is"
        return result


def _detect_once(result, *, state_path, enabled, opener, setter, zones,
                 current_zone, fingerprint, clock, timeout, providers):
    if not enabled:
        # The off switch is here, above everything, so that "off" means no
        # request was made rather than one that was thrown away.
        result.update(action="disabled",
                      note="looking up this box's timezone is turned off")
        return result

    state = read_state(state_path)
    here = fingerprint()

    # Same network as last time we looked? Then this box has not moved, and
    # there is nothing to ask anybody. This is the ordinary case on every boot
    # after the first, and it is why a customer on a VPN does not find their
    # box wandering between timezones.
    if "network" in state and state.get("network") == here:
        result.update(action="unchanged", zone=state.get("zone"),
                      source=state.get("source"),
                      note="this box is on the same network as last time")
        return result

    found = look_up(opener=opener, timeout=timeout, providers=providers)
    zone, address = found["zone"], found["address"]
    if zone is None:
        # Deliberately records nothing. A box with no internet, or one whose
        # lookup service has gone or turned to rubbish, must come back and try
        # again rather than deciding it has already looked.
        if found["refused"]:
            result.update(
                action="refused",
                note=("this box was told it is somewhere that is not a real "
                      "place, so it changed nothing"),
            )
        else:
            result.update(action="no-answer",
                          note="this box could not find out where it is")
        return result
    result["suggested"] = zone

    current = current_zone()
    chose = _owner_chose(state, current)
    if chose:
        # Recording the new network here, without changing anything, is what
        # stops a box whose owner chose the zone asking again on every boot.
        state.update({"source": chose, "zone": current, "network": here,
                      "address": address, "checked_at": float(clock()),
                      "suggested": zone})
        write_state(state_path, state)
        result.update(action="manual", zone=current, source=chose,
                      note=(f"this box thinks it is in {zone}, but somebody "
                            f"has already set its timezone to {current}, so "
                            f"nothing was changed"))
        log.info("timezone detection thinks %s; leaving the chosen %s alone",
                 zone, current)
        return result

    # Same public address as last time on a different local network - a new
    # router, a re-addressed LAN, the same house. The box did not move.
    if address is not None and state.get("address") == address:
        state.update({"network": here, "checked_at": float(clock())})
        write_state(state_path, state)
        result.update(action="unchanged", zone=state.get("zone"),
                      source=state.get("source"),
                      note="this box is at the same public address as last time")
        return result

    try:
        allowed = list(zones())
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001
        log.warning("could not ask this box which timezones it knows",
                    exc_info=True)
        result.update(action="refused",
                      note="this box could not list the timezones it knows")
        return result

    if zone not in set(allowed):
        # servicectl would refuse this too. Refusing it here as well means the
        # journal says which zone and why, instead of a bare failure.
        result.update(action="refused",
                      note=f"this box has no timezone called {zone}")
        log.warning("timezone detection: this box does not know a zone called %r",
                    zone)
        return result

    try:
        setter(zone, allowed=allowed)
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("could not set the detected timezone %s: %s", zone, exc)
        result.update(action="failed",
                      note="this box could not set the timezone it worked out")
        return result

    now = float(clock())
    state.update({"source": SOURCE_DETECTED, "zone": zone, "network": here,
                  "address": address, "detected_at": now, "checked_at": now,
                  "suggested": zone})
    write_state(state_path, state)
    result.update(action="set", zone=zone, source=SOURCE_DETECTED, at=now,
                  note=f"this box set its timezone to {zone} from its location")
    log.info("timezone set to %s from this box's location", zone)
    return result


# ==========================================================================
# Is anything keeping the clock honest?
# ==========================================================================
#: systemd-timesyncd touches this file every time it successfully syncs - that
#: is its own documented mechanism for remembering how far the clock had got,
#: not a trick. Its modification time is therefore the honest answer to "when
#: did this box last actually set its clock from the internet", which
#: ``timedatectl`` itself does not report.
TIMESYNC_CLOCK = Path("/var/lib/systemd/timesync/clock")

#: The service Ubuntu ships. Named rather than assumed: a box running chrony
#: or ntpd instead is a box where the answer is "cannot tell", not "never".
SYNC_SERVICE = "systemd-timesyncd"

#: Said when the box's own network time is switched off.
#:
#: **Why this box does not simply turn it back on.** It cannot, honestly.
#: ``timedatectl set-ntp true`` needs root, and every privileged command this
#: product runs is in one closed list in servicectl.py, from which the sudoers
#: rule is generated so that the rule and the code cannot drift apart. Running
#: a privileged command that is not on that list would not work on any box in
#: the field - sudo would refuse it - and would recreate exactly the drift that
#: file exists to prevent. So this reports the state and names the fix instead
#: of pretending to apply one. The command is spelled out because the person
#: helping needs it and there is no way to guess it.
SYNC_OFF_FIX = (
    "This box is not setting its own clock from the internet, so the time will "
    "slowly drift and channels that change through the day will start playing "
    "the wrong thing at the wrong hour. Switching it back on has to be done on "
    "the box itself, with: timedatectl set-ntp true"
)


def _timedatectl_show() -> str:
    return _run(["timedatectl", "show"])


def _ago(seconds: float) -> str:
    """A gap in the words a person uses for it."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        n = max(1, int(round(seconds)))
        return f"{n} second{'s' if n != 1 else ''} ago"
    minutes = seconds / 60.0
    if minutes < 90:
        n = int(round(minutes))
        return f"{n} minute{'s' if n != 1 else ''} ago"
    hours = minutes / 60.0
    if hours < 36:
        n = int(round(hours))
        return f"{n} hour{'s' if n != 1 else ''} ago"
    n = int(round(hours / 24.0))
    return f"{n} day{'s' if n != 1 else ''} ago"


def sync_status(
    *,
    reader: Optional[Callable[[], str]] = None,
    clock_file: Any = None,
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Whether anything is keeping this clock honest, and when it last did.

    There are **three** answers to "is this clock synchronised", not two, and
    the third is not a polite version of either of the others:

    * ``True``  - the box says so.
    * ``False`` - the box says it is not.
    * ``None``  - we cannot tell. No timedatectl, a container, a box running
      chrony instead. Reporting that as "never synchronised" would send
      somebody to buy a battery for a box that is perfectly fine.

    ``last_sync_state`` splits the same way: ``known`` with a time, ``never``
    when the sync service is the one we understand and it has plainly never
    succeeded, and ``unknown`` when we are not in a position to say.

    Both readings come from one ``timedatectl show``. ``NTP=yes`` is systemd's
    answer to "is network time switched on and in use", which is the whole of
    "enabled and running" for the service Ubuntu ships - there is deliberately
    no separate ``systemctl is-active`` here, because the answer would either
    agree or be about a service this box is not using. ``NTP=no`` is the one
    state a person has to act on, and :data:`SYNC_OFF_FIX` says how.
    """
    reader = reader or _timedatectl_show
    clock_file = TIMESYNC_CLOCK if clock_file is None else Path(clock_file)

    try:
        text = reader() or ""
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 - no timedatectl is an ordinary outcome
        log.debug("could not read the time sync status", exc_info=True)
        text = ""

    enabled: Optional[bool] = None
    synchronised: Optional[bool] = None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "NTP":
            enabled = value.strip() == "yes"
        elif key.strip() == "NTPSynchronized":
            synchronised = value.strip() == "yes"

    last_sync: Optional[float] = None
    last_sync_state = "unknown"
    if synchronised is None:
        # We could not read the box's own opinion, so we are in no position to
        # say anything about when it last synced either.
        last_sync_state = "unknown"
    else:
        try:
            last_sync = clock_file.stat().st_mtime
            last_sync_state = "known"
        except OSError:
            # timedatectl answered, so this is a systemd box, so the absence
            # of timesyncd's own record means it has genuinely never synced.
            last_sync_state = "never"

    if last_sync_state == "known" and last_sync is not None:
        summary = f"Synchronised {_ago(clock() - last_sync)}"
    elif last_sync_state == "never":
        summary = "Never synchronised"
    else:
        summary = ("This box cannot say whether its clock has ever been set "
                   "from the internet")

    return {
        "service": SYNC_SERVICE if synchronised is not None else None,
        "enabled": enabled,
        "synchronised": synchronised,
        "last_sync": last_sync,
        "last_sync_state": last_sync_state,
        "summary": summary,
        "fix": SYNC_OFF_FIX if enabled is False else None,
    }


# ==========================================================================
# The flat CMOS battery
# ==========================================================================
#: Nothing this software runs on can honestly be reading a date before the
#: software existed. Version 1.0.0 was tagged in July 2026; this sits safely
#: before that, so the test never fires on a box that is merely a bit behind -
#: it fires on a box whose clock has been reset to a manufacturing default,
#: which is what a flat coin cell produces.
SOFTWARE_WRITTEN = datetime(2026, 1, 1, tzinfo=_utc.utc)
IMPLAUSIBLE_BEFORE = SOFTWARE_WRITTEN.timestamp()

#: The part. Naming it is the whole point: an owner can act on "a two-dollar
#: coin battery" and cannot act on "the RTC is not being maintained".
CMOS_PART = "CR2032"

CMOS_HEADLINE = "This box's clock is wrong."

#: Says what it MEANS, in order: the consequence the owner is actually seeing,
#: the fix that costs nothing, then the fix that costs two dollars.
CMOS_DETAIL = (
    "It thinks it is a different day, so any channel that changes through the "
    "day - cartoons in the morning, sitcoms at night - is working from the "
    "wrong time and will play the wrong thing until this is put right. "
    "Connect this box to the internet and it will set its own clock within a "
    "minute. If the clock is wrong again every time the box is switched off "
    "at the wall, the coin-sized battery on the motherboard is flat: it is a "
    f"{CMOS_PART}, it costs about two dollars, and any computer shop will fit "
    "one."
)

#: The quiet version. The clock is right now because the network fixed it, and
#: without this line nobody would ever learn that it will be wrong again.
CMOS_CORRECTED = (
    "This box started up with the wrong date and the internet has since put "
    "it right, so the picture and the schedule are fine. It will come back "
    "wrong after the next power cut: that is the coin-sized battery on the "
    f"motherboard going flat. It is a {CMOS_PART}, about a two-dollar part."
)


def clock_is_plausible(*, now: Optional[float] = None) -> bool:
    """False when the clock reads a date from before this software existed."""
    try:
        reading = float(time.time() if now is None else now)
    except (TypeError, ValueError):
        return False
    return reading >= IMPLAUSIBLE_BEFORE


def clock_report(
    *,
    now: Optional[float] = None,
    sync: Optional[Dict[str, Any]] = None,
    boot_clock_wrong: bool = False,
) -> Dict[str, Any]:
    """What to say about this clock, and how loudly.

    The alarm is raised only when the clock is wrong *and* nothing is going to
    fix it by itself, because that is the only state a person has to act on.
    A clock the network already corrected still gets a line - a quieter one -
    because it will be wrong again at the next power cut and nobody would ever
    find that out otherwise.
    """
    reading = float(time.time() if now is None else now)
    plausible = clock_is_plausible(now=reading)
    sync = sync if sync is not None else sync_status()

    headline: Optional[str] = None
    detail: Optional[str] = None
    alarm = False
    if not plausible:
        headline, detail, alarm = CMOS_HEADLINE, CMOS_DETAIL, True
    elif boot_clock_wrong:
        headline = "This box's hardware clock is not keeping time."
        detail = CMOS_CORRECTED

    return {
        "now": reading,
        "local_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reading)),
        "plausible": plausible,
        "boot_clock_wrong": bool(boot_clock_wrong),
        "synchronised": sync.get("synchronised"),
        "last_sync": sync.get("last_sync"),
        "last_sync_state": sync.get("last_sync_state"),
        "summary": sync.get("summary"),
        "alarm": alarm,
        "headline": headline,
        "detail": detail,
    }


# ==========================================================================
# What the dashboard reads, and what start-up calls
# ==========================================================================
#: The plain words a customer needs about the one outbound call this adds.
#: The dashboard shows this next to the switch that turns it off.
WHAT_IS_SENT = (
    "When this box has never been told where it is, it asks a public internet "
    "service which timezone its internet address is in, so the clock is right "
    "without anybody having to set it. It sends the request and nothing else: "
    "no name, no serial number, no version, and nothing about you or what you "
    "watch. It asks once, and again only if the box is moved to a different "
    "connection. Turn this off and it never asks."
)


def report(
    *,
    state_path: Any,
    now: Optional[float] = None,
    sync: Optional[Dict[str, Any]] = None,
    current_zone: Optional[Callable[[], Optional[str]]] = None,
    detect_enabled: bool = True,
) -> Dict[str, Any]:
    """Everything a page needs to say about this box's clock. Never raises."""
    state = read_state(state_path)
    sync = sync if sync is not None else sync_status()
    # Either this start-up was wrong, or some earlier one was. The second half
    # is what keeps a flat coin cell on the page after the network has quietly
    # corrected the clock and after the dashboard has been restarted.
    ever_wrong = bool(state.get("boot_clock_wrong")) or bool(
        state.get("wrong_clock_boots"))
    clock = clock_report(now=now, sync=sync, boot_clock_wrong=ever_wrong)

    try:
        live = (current_zone or _default_current_zone)()
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001
        live = None

    source = state.get("source")
    if source not in (SOURCE_DETECTED, SOURCE_MANUAL, SOURCE_PREEXISTING):
        source = SOURCE_PREEXISTING if is_named_zone(live) else SOURCE_UNKNOWN

    return {
        "clock": clock,
        "sync": sync,
        "timezone": {
            "zone": live or state.get("zone"),
            "source": source,
            "detected_at": state.get("detected_at"),
            "checked_at": state.get("checked_at"),
            "suggested": state.get("suggested"),
        },
        "detection": {
            "enabled": bool(detect_enabled),
            "what_is_sent": WHAT_IS_SENT,
            "providers": [p.url for p in PROVIDERS],
        },
        "alarm": clock["alarm"],
        "headline": clock["headline"],
        "detail": clock["detail"],
    }


def note_boot_clock(state_path: Any, *,
                    clock: Callable[[], float] = time.time) -> bool:
    """Write down whether the clock was absurd when this box started up.

    It has to be written down at start-up, because the fix erases the
    evidence: a box with a flat coin cell and a working internet connection is
    wrong for about forty seconds and then perfectly normal, and by the time
    anybody looks at the dashboard there is nothing left to see. Without this,
    the single most common hardware fault on these machines is invisible.
    """
    reading = float(clock())
    wrong = not clock_is_plausible(now=reading)
    if wrong:
        log.warning(
            "this box started up believing it was %s, which is before this "
            "software existed - its hardware clock is not keeping time. That "
            "is a flat %s coin cell on the motherboard, and it will happen "
            "again at the next power cut.",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reading)),
            CMOS_PART,
        )
    state = read_state(state_path)
    state["boot_clock_wrong"] = wrong
    state["booted_at"] = reading
    if wrong:
        # A count that only ever goes up, because the boolean above describes
        # *this* start-up and the dashboard restarting would otherwise wipe the
        # fault off the page. It is also the honest evidence for a flat cell
        # rather than a one-off: a battery that is going is one that does this
        # every single time the box is switched off at the wall.
        try:
            state["wrong_clock_boots"] = int(state.get("wrong_clock_boots", 0)) + 1
        except (TypeError, ValueError):
            state["wrong_clock_boots"] = 1
    write_state(state_path, state)
    return wrong


def start(
    *,
    state_path: Any,
    config: Any = None,
    clock: Callable[[], float] = time.time,
    **kwargs: Any,
) -> Optional[threading.Thread]:
    """Everything this module does at start-up, off the critical path.

    Returns straight away, always. The thread is a daemon and nothing joins
    it, so a lookup that never answers cannot delay start-up, cannot delay
    playback and cannot hold up a shutdown. Returns the thread so a test can
    wait for it; nothing in the product should.
    """
    enabled = True
    if config is not None:
        try:
            enabled = bool(getattr(config, "time").detect_timezone)
        except AttributeError:
            enabled = True

    def work() -> None:
        try:
            note_boot_clock(state_path, clock=clock)
        except AssertionError:
            raise
        except Exception:  # noqa: BLE001 - the television comes first
            log.warning("could not check this box's clock at start-up",
                        exc_info=True)
        detect_once(state_path=state_path, enabled=enabled, clock=clock, **kwargs)

    thread = threading.Thread(target=work, name="retrobox-timekeeping",
                              daemon=True)
    thread.start()
    return thread


__all__ = [
    "CMOS_CORRECTED",
    "CMOS_DETAIL",
    "CMOS_HEADLINE",
    "CMOS_PART",
    "DEFAULT_SETTER",
    "GEOGRAPHIC_AREAS",
    "IMPLAUSIBLE_BEFORE",
    "LOOKUP_TIMEOUT",
    "MAX_RESPONSE_BYTES",
    "PROVIDERS",
    "SOURCE_DETECTED",
    "SOURCE_MANUAL",
    "SOURCE_PREEXISTING",
    "SOURCE_UNKNOWN",
    "STATE_NAME",
    "SYNC_SERVICE",
    "WHAT_IS_SENT",
    "clock_is_plausible",
    "clock_report",
    "detect_once",
    "is_named_zone",
    "look_up",
    "network_fingerprint",
    "note_boot_clock",
    "read_state",
    "record_manual_timezone",
    "report",
    "start",
    "sync_status",
    "write_state",
]
