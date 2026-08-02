"""Changing the box's network, from a page served over that network.

This is the only thing in Retro Box that can permanently cut you off from the
machine you are configuring. A bad wifi password, a static address on the
wrong subnet, a typo'd gateway - none of those produce an error message. They
produce a box that has silently vanished from the LAN and can only be
recovered with a keyboard and a monitor plugged into it, which is the exact
situation this product exists to avoid.

Three rules follow from that, and they are not negotiable.

**Nothing is applied permanently in one step.** Every change goes on probation
through ``netplan try``, which reverts by itself if nobody confirms it. See
:mod:`retrobox.netprobation`.

**Configuration is built as data and serialised, never formatted into a
string.** A home network can legitimately be called ``my"net`` or
``net$(id)``; quoting that correctly is a solved problem and the solution is
``yaml.safe_dump``, not care.

**Nothing typed by a person becomes a command-line argument.** The SSID and the
password only ever exist inside a document handed to a fixed command on stdin.
There is no shell anywhere in this module and no argv position that a field
can reach.

**A netplan file is never half-written.** ``tee`` truncates before it writes,
and this box is switched off at the wall, so a document is built in a staging
file beside its target and renamed over it in one step. A truncated file in
``/etc/netplan`` is not a broken interface, it is ``netplan generate`` failing
for the whole directory - a box with no network on anything, which nobody can
reach to fix.

Files are written alongside whatever the installer and the distro already put
in ``/etc/netplan``, never replacing them, and numbered so they merge last.
Wired and wireless are separate files so that changing one cannot disturb the
other - which matters, because a box with both up is the safest place to be
making changes from.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

#: Ours, additive, and numbered to merge after the distro's own (Ubuntu ships
#: 50-cloud-init.yaml). Separate files so wired and wireless are independent.
WIRED_FILE = "/etc/netplan/90-retrobox-wired.yaml"
WIFI_FILE = "/etc/netplan/91-retrobox-wifi.yaml"

#: The wifi file holds the customer's home network password, so it is 0600
#: from the instant it exists - see :func:`_install_privileged`, which makes
#: the file private *before* the password goes into it rather than after.
CREDENTIAL_MODE = 0o600
PLAN_MODE = 0o600

#: A netplan document that says nothing. Used to bring one of our files into
#: existence so it can be narrowed to 0600 before a password is written into
#: it; an empty document contributes nothing to netplan's merge, so a box left
#: holding one is a box running on whatever the distro's own files say.
EMPTY_DOCUMENT = "network: {}\n"

#: What a new document is called while it is being written, before it becomes
#: the file netplan reads.
#:
#: Two things about this name are load-bearing. It is in the same directory as
#: its target, because a rename is only atomic within one filesystem - across
#: two it is a copy, which is the very thing being avoided. And it does not
#: end in ``.yaml``, because netplan reads ``/etc/netplan/*.yaml`` and a
#: half-written document it can see is no better than a half-written one it
#: was given.
STAGING_SUFFIX = ".retrobox-new"


def staging_for(path: str) -> str:
    """Where a new version of one of our netplan files is built.

    A constant derived from a constant, so the sudoers rule can name it in
    full - see :func:`retrobox.servicectl.sudoers_rule`, which builds its
    entry from this same function.
    """
    return path + STAGING_SUFFIX


#: Interface names as the kernel makes them. Not a sanitiser - a whitelist.
_INTERFACE = re.compile(r"\A[A-Za-z][A-Za-z0-9]{0,14}\Z")

#: 802.11 caps an SSID at 32 octets and WPA at 63 characters.
MAX_SSID = 32
MIN_PASSWORD = 8
MAX_PASSWORD = 63

COMMAND_TIMEOUT = 20.0

#: ``netplan apply`` re-runs generate and then reconfigures every interface,
#: so it takes seconds rather than milliseconds. It is bounded because the
#: start-up recovery path waits for it before the dashboard serves its first
#: page, and the dashboard is the only thing somebody with a sick box can
#: still reach. The file on the disk is already right by the time this runs,
#: so the worst a timeout costs is that the box has to be restarted to pick
#: the configuration up - which is a great deal better than never coming up.
APPLY_TIMEOUT = 30.0


class NetworkError(Exception):
    """A network change we refuse, with something worth showing a person."""


def _run(cmd: Sequence[str], *, timeout: float = COMMAND_TIMEOUT,
         stdin: Optional[str] = None) -> Tuple[int, str, str]:
    """Run one command and keep its two streams apart: (code, printed, warned).

    They are different things and confusing them has cost this box its network
    before. A command can succeed and still write to stderr - sudo does it on
    *every* invocation once the hostname no longer matches ``/etc/hosts``,
    which is a thing the dashboard's own hostname button causes:

        sudo: unable to resolve host retrobox: Name or service not known

    Glue that onto stdout and :func:`read_plan` hands back a netplan file with
    a warning on the end of it. That string is what gets written back when a
    change is not confirmed, netplan rejects it at the next boot, and the box
    comes up with no network on a customer's shelf. So stdout is the answer,
    stderr is the grumbling, and callers say which one they meant.

    stdout comes back exactly as the command printed it, not stripped: for a
    file being read out for a rollback, the bytes are the point.
    """
    try:
        result = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout,
            check=False, input=stdin,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Nothing was printed because nothing ran; the reason is the grumbling.
        return 1, "", str(exc)
    return result.returncode, result.stdout or "", result.stderr or ""


#: Long enough to say what went wrong, short enough that a command which
#: printed a novel cannot fill the browser or the journal with it.
MAX_REASON = 300


def _reason(*streams: str) -> str:
    """The first thing a failed command said that is worth showing a person.

    Callers pass the streams they are willing to repeat, most useful first.
    A stream carrying a wifi password - ``tee`` echoes its own input - is
    simply not passed.

    Capped here rather than at the end, so that anything a caller adds after
    this - the sentence below, say - cannot be the part that gets cut off.
    """
    for text in streams:
        said = (text or "").strip()
        if said:
            return said[:MAX_REASON]
    return ""


#: Added to every step of a write that a stale ``sudoers`` rule refuses.
#:
#: A box installed before the staging file existed has a rule naming the live
#: netplan file and nothing else, so sudo refuses every command that mentions
#: ``<file>.retrobox-new`` - starting with the very first one, long before the
#: rename. And what sudo says about a command it has no rule for is "sudo: a
#: password is required", which sends the owner of a box that has no password
#: to type off looking for one. This sentence is the only place the thing that
#: actually fixes it is ever said, so it goes on every step a stale rule can
#: refuse rather than only on the last of them.
NEEDS_THE_INSTALLER_AGAIN = (
    " - this box may need scripts/install-service.sh run again so that sudo "
    "allows a network file to be written and replaced"
)


# ==========================================================================
# Validating what somebody typed
# ==========================================================================
def _interface(name: Any) -> str:
    if not isinstance(name, str) or not _INTERFACE.match(name):
        raise NetworkError(f"{name!r} is not an interface name")
    return name


def _address(raw: Any, *, field: str) -> ipaddress.IPv4Address:
    if not isinstance(raw, str) or not raw.strip():
        raise NetworkError(f"{field} is required")
    try:
        address = ipaddress.IPv4Address(raw.strip())
    except ValueError:
        raise NetworkError(f"{raw!r} is not an IPv4 address") from None
    if address.is_unspecified:
        raise NetworkError(f"{field} cannot be 0.0.0.0")
    return address


def _prefix(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise NetworkError("the netmask must be a number of bits, like 24")
    # /31 and /32 leave no room for a gateway or any other host, so a box set
    # to one is a box on its own with nothing to talk to.
    if not 1 <= raw <= 30:
        raise NetworkError("the netmask must be between 1 and 30 bits")
    return raw


def _check_static(
    address: str, prefix: int, gateway: Optional[str], dns: Sequence[str]
) -> Tuple[ipaddress.IPv4Address, int, Optional[ipaddress.IPv4Address], List[str]]:
    host = _address(address, field="the address")
    bits = _prefix(prefix)
    network = ipaddress.IPv4Network(f"{host}/{bits}", strict=False)

    if host == network.network_address:
        raise NetworkError(f"{host} is the network address, not a usable one")
    if host == network.broadcast_address:
        raise NetworkError(f"{host} is the broadcast address, not a usable one")

    router = None
    if gateway not in (None, ""):
        router = _address(gateway, field="the gateway")
        if router == host:
            raise NetworkError("the gateway cannot be this box's own address")
        if router not in network:
            # The classic way to lose a box: it applies cleanly and then
            # nothing can reach it.
            raise NetworkError(
                f"the gateway {router} is not on the same network as {host}/{bits} - "
                f"nothing would be able to reach this box"
            )

    servers = []
    for item in dns or []:
        servers.append(str(_address(item, field="a DNS server")))
    return host, bits, router, servers


def _ssid(raw: Any) -> str:
    if not isinstance(raw, str):
        raise NetworkError("the network name must be text")
    name = raw.strip("\r\n")
    if not name.strip():
        raise NetworkError("the network name is required")
    if len(name.encode("utf-8")) > MAX_SSID:
        raise NetworkError(f"a network name cannot be longer than {MAX_SSID} characters")
    return name


def _password(raw: Any) -> Optional[str]:
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise NetworkError("the password must be text")
    if not MIN_PASSWORD <= len(raw) <= MAX_PASSWORD:
        raise NetworkError(
            f"a wifi password is between {MIN_PASSWORD} and {MAX_PASSWORD} characters"
        )
    return raw


# ==========================================================================
# Building the configuration
# ==========================================================================
def _dump(plan: Dict[str, Any]) -> str:
    """Serialise, never format.

    Every quoting question an SSID could raise - quotes, colons, newlines,
    leading dashes, braces - is the serialiser's problem, and it has the
    right answer. A f-string here would be the bug.
    """
    import yaml

    return yaml.safe_dump(plan, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)


def _addressing(
    address: Optional[str], prefix: Optional[int],
    gateway: Optional[str], dns: Optional[Sequence[str]],
) -> Dict[str, Any]:
    if address in (None, ""):
        return {"dhcp4": True}
    host, bits, router, servers = _check_static(address, prefix, gateway, dns or [])
    block: Dict[str, Any] = {"dhcp4": False, "addresses": [f"{host}/{bits}"]}
    if router is not None:
        # `gateway4` is deprecated in netplan; a default route is the current
        # spelling and works on every version that ships with netplan today.
        block["routes"] = [{"to": "default", "via": str(router)}]
    if servers:
        block["nameservers"] = {"addresses": servers}
    return block


def dhcp_plan(*, interface: str) -> str:
    """Wired, address from the router. The ordinary case."""
    name = _interface(interface)
    return _dump({"network": {"version": 2, "ethernets": {name: {"dhcp4": True}}}})


def static_plan(
    *, interface: str, address: Any, prefix: Any,
    gateway: Optional[str] = None, dns: Optional[Sequence[str]] = None,
) -> str:
    """Wired, with an address somebody typed. Validated before it exists.

    An address is required here. Falling back to DHCP when the field is blank
    would mean somebody filling in the static form and missing one box gets
    DHCP instead, with nothing said - and then wonders why the address they
    typed is not the address the box has.
    """
    name = _interface(interface)
    if address in (None, ""):
        raise NetworkError("a static configuration needs an address")
    block = _addressing(address, prefix, gateway, dns)
    return _dump({"network": {"version": 2, "ethernets": {name: block}}})


def wifi_plan(
    *, interface: str, ssid: Any, password: Any,
    address: Optional[str] = None, prefix: Optional[int] = None,
    gateway: Optional[str] = None, dns: Optional[Sequence[str]] = None,
) -> str:
    """Join a wireless network, on DHCP unless an address is given."""
    name = _interface(interface)
    network = _ssid(ssid)
    secret = _password(password)

    access_point: Dict[str, Any] = {}
    if secret is not None:
        access_point["password"] = secret

    block = _addressing(address, prefix, gateway, dns)
    block["access-points"] = {network: access_point}
    return _dump({"network": {"version": 2, "wifis": {name: block}}})


# ==========================================================================
# Writing it, without any of it becoming an argument
# ==========================================================================
def _install_privileged(path: str, content: str, mode: int) -> Tuple[int, str]:
    """Put ``content`` at ``path`` as root, readable by nobody else, ever, and
    without ``path`` ever being a half-written file.

    The content goes on **stdin**. The command line is fixed and contains only
    constants from this module - no SSID, no password, no address, ever. There
    is no shell: ``subprocess`` is given a list, so metacharacters are just
    characters.

    Nothing is ever written into the live file. ``tee`` truncates before it
    writes, and this box is switched off at the wall - so a cut inside that
    window would leave a truncated document in /etc/netplan, and netplan
    generate then fails for the *whole directory*: no network on any
    interface, on hardware nobody can log in to. So the document is built in a
    staging file beside it and renamed over it, which is one atomic step. The
    live file goes from being one complete document to being the other, and a
    power cut anywhere in here leaves the box exactly as it was. This is the
    same dance every other write in this codebase does - see
    :mod:`retrobox.configwrite`.

    The order of the first three steps is the other half of the point.
    ``tee`` creates a file under root's umask, which is 0644, so writing first
    and narrowing afterwards leaves the customer's wifi password readable by
    every account on the box for as long as the second command takes - and
    *permanently* if that second command ever fails. So the staging file is
    made root-only **before** any secret goes into it, and if it cannot be
    made root-only, nothing is written at all. The rename then carries that
    0600 across with it, because a rename moves the inode: the live file is
    private from the instant it exists, with no window at all.

    That works because of one POSIX guarantee: opening a file that already
    exists ignores the mode argument. ``tee`` truncates and rewrites, but it
    cannot widen a file that is already 0600.
    """
    staged = staging_for(path)
    narrow = ["sudo", "-n", "chmod", oct(mode)[2:], staged]
    write = ["sudo", "-n", "tee", staged]
    # -f so it cannot stop to ask about the file it is replacing. Both ends
    # are constants from this module; sudo is given the same two, spelled the
    # same way, by the rule servicectl generates.
    rename = ["sudo", "-n", "mv", "-f", staged, path]

    code, _printed, warned = _run(narrow)
    if code != 0:
        # There is usually no staging file: it exists only for the moment a
        # write takes. Bring it into existence holding a document with no
        # secret in it, and narrow that before the real content goes near the
        # disk. Nothing here can destroy anything - the staging file is ours,
        # and the live configuration has not been touched yet.
        code, _printed, warned = _run(write, stdin=EMPTY_DOCUMENT)
        if code != 0:
            # This is where an upgraded box that never re-ran the installer
            # actually stops. Its sudoers rule does not name the staging file,
            # so the chmod above was refused and so is this - the write never
            # gets near the rename, which is why the sentence about what to do
            # has to be here as well as there.
            return code, (
                _reason(warned, "the command failed") + NEEDS_THE_INSTALLER_AGAIN
            )
        code, _printed, warned = _run(narrow)
        if code != 0:
            # An empty document is left in the staging file. It is not a
            # secret, and netplan does not read it.
            return code, (
                _reason(warned, "could not make the file private")
                + NEEDS_THE_INSTALLER_AGAIN
            )

    # The staging file exists and is root-only, so this cannot expose anything.
    code, _printed, warned = _run(write, stdin=content)
    # _printed is tee's copy of its own input - the password. It never becomes
    # part of a message that goes to the browser or the journal.
    if code != 0:
        return code, _reason(warned, "the command failed")

    code, _printed, warned = _run(rename)
    if code != 0:
        # The live file has not been touched, so the box keeps the network it
        # already had. A stale sudoers rule would have stopped this write long
        # before here, so on this step the likeliest causes are a full disk or
        # a read-only root - but the sentence costs nothing and a rule missing
        # only the rename is still a rule the installer would put right.
        return code, (
            _reason(warned, "the command failed") + NEEDS_THE_INSTALLER_AGAIN
        )
    return 0, ""


def write_plan(path: str, content: str) -> None:
    """Write one of our netplan files, or say why not."""
    if path not in (WIRED_FILE, WIFI_FILE):
        # The path is a constant from this module and nothing else.
        raise NetworkError("that is not a file this box writes")
    code, reason = _install_privileged(path, content, PLAN_MODE)
    if code != 0:
        # Not truncated here: what a command said is already capped at
        # MAX_REASON, and the sentence telling somebody what to do about it is
        # on the end. Cutting the whole thing to a length would cut that off.
        raise NetworkError(f"could not write the network configuration: {reason}")


def apply_plan() -> None:
    """Make what is in /etc/netplan the configuration the box is running.

    Writing the file is not the same as changing the network. netplan reads
    /etc/netplan at boot and when it is told to, and at no other time - so a
    box that has had its old, working configuration put back for it goes on
    running the one nobody wanted for the rest of the session. If that
    configuration is why the box cannot be reached, nobody can open the
    dashboard to ask again, and the whole undo achieved nothing but a tidy
    file on a machine still sitting silently on somebody's shelf.

    This is disruptive by nature - it reconfigures every interface, so a
    working connection goes down and comes back - which is why it belongs only
    at the moment a configuration has actually changed underneath the running
    system, and never as a routine check.
    """
    code, _printed, warned = _run(["sudo", "-n", "netplan", "apply"],
                                  timeout=APPLY_TIMEOUT)
    if code != 0:
        raise NetworkError(
            f"could not put the network configuration into effect: "
            f"{_reason(warned, 'the command failed')}"
        )


def read_plan(path: str) -> Optional[str]:
    """What is in one of our files now, for the rollback target.

    Only what ``cat`` printed, and all of it. This string is written straight
    back into ``/etc/netplan`` when a change is not confirmed, so a warning
    from sudo appended to it is a warning appended to the file, and a file
    netplan refuses at the next boot is a box with no network.
    """
    if path not in (WIRED_FILE, WIFI_FILE):
        raise NetworkError("that is not a file this box writes")
    code, printed, _warned = _run(["sudo", "-n", "cat", path])
    return printed if code == 0 else None


# ==========================================================================
# Reading what the box has now
# ==========================================================================
def interfaces() -> List[Dict[str, Any]]:
    """Every configurable interface, with its state and addresses."""
    # Only what ip printed. Anything on stderr - sudo's host warning reaches
    # this on a box whose hostname was changed - is not JSON, and taking it as
    # part of the answer means a working box showing no adapters at all.
    code, printed, _warned = _run(["ip", "-details", "-json", "addr", "show"])
    if code != 0:
        return []
    try:
        payload = json.loads(printed)
    except ValueError:
        return []
    if not isinstance(payload, list):
        return []

    found = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("ifname")
        if not isinstance(name, str) or name == "lo":
            continue
        if item.get("link_type") == "loopback":
            continue
        addresses = [
            f"{a.get('local')}/{a.get('prefixlen')}"
            for a in item.get("addr_info") or []
            if isinstance(a, dict) and a.get("family") == "inet"
        ]
        found.append({
            "name": name,
            "up": str(item.get("operstate", "")).upper() == "UP",
            "state": str(item.get("operstate", "unknown")).lower(),
            "wireless": bool(item.get("wireless")) or name.startswith(("wl", "wlan")),
            "addresses": addresses,
            "mac": item.get("address", ""),
        })
    return found


def wireless_interfaces() -> List[str]:
    return [i["name"] for i in interfaces() if i["wireless"]]


def nameservers() -> List[str]:
    """Whatever is actually resolving right now."""
    servers: List[str] = []
    code, printed, _warned = _run(["resolvectl", "dns"])
    if code == 0 and printed:
        for line in printed.splitlines():
            _, _, rest = line.partition(":")
            servers.extend(p for p in rest.split() if _looks_like_address(p))
    if servers:
        return sorted(set(servers))
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) > 1 and _looks_like_address(parts[1]):
                        servers.append(parts[1])
    except OSError:
        pass
    return sorted(set(servers))


def _looks_like_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


# ==========================================================================
# Scanning
# ==========================================================================
def scan(interface: str) -> List[Dict[str, Any]]:
    """Wireless networks in range, strongest first.

    The interface is checked against the whitelist pattern before it is used;
    it is the only part of this that is not a constant.
    """
    name = _interface(interface)
    code, printed, warned = _run(["sudo", "-n", "iw", "dev", name, "scan"],
                                 timeout=30.0)
    if code != 0:
        log.info("wifi scan on %s failed: %s", name, _reason(warned, printed)[:200])
        return []

    # Only what the radio found. A warning on stderr must not be able to put a
    # network in this list for somebody to try to join.
    networks: Dict[str, Dict[str, Any]] = {}
    current: Optional[Dict[str, Any]] = None
    for line in printed.splitlines():
        stripped = line.strip()
        if line.startswith("BSS "):
            current = {"ssid": "", "signal": -100.0, "secured": False}
        elif current is None:
            continue
        elif stripped.startswith("signal:"):
            try:
                current["signal"] = float(stripped.split()[1])
            except (IndexError, ValueError):
                pass
        elif stripped.startswith("SSID:"):
            current["ssid"] = stripped[5:].strip()
            if current["ssid"]:
                # Keep the strongest sighting of each network; a house with
                # two access points shows the same name twice otherwise.
                seen = networks.get(current["ssid"])
                if seen is None or current["signal"] > seen["signal"]:
                    networks[current["ssid"]] = current
        elif stripped.startswith(("RSN:", "WPA:")):
            current["secured"] = True
            if current["ssid"] and current["ssid"] in networks:
                networks[current["ssid"]]["secured"] = True

    return sorted(networks.values(), key=lambda n: n["signal"], reverse=True)


# ==========================================================================
# Three separate answers
# ==========================================================================
def _has_link() -> bool:
    return any(i["up"] and i["addresses"] for i in interfaces())


def _dns_resolves() -> bool:
    try:
        socket.setdefaulttimeout(5.0)
        socket.getaddrinfo("api.github.com", 443)
        return True
    except (OSError, socket.gaierror):
        return False
    finally:
        socket.setdefaulttimeout(None)


def _reaches_internet() -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=5.0):
            return True
    except OSError:
        return False


def connectivity() -> Dict[str, Any]:
    """Link, DNS and internet as three separate answers.

    Lumping them into "no internet" helps nobody: a box with a link but no DNS
    has a different problem, with a different fix, from one with no cable in.
    """
    link = _has_link()
    dns = _dns_resolves() if link else False
    internet = _reaches_internet() if link else False

    if not link:
        summary = "No network at all - check the cable, or join a wifi network below."
    elif not internet:
        summary = (
            "This box is on your network but cannot reach the internet. "
            "That is usually the router rather than the box."
        )
    elif not dns:
        summary = (
            "This box can reach the internet but DNS is not resolving, so "
            "names do not work. Check the DNS servers below."
        )
    else:
        summary = "Everything is working."

    return {
        "link": link, "dns": dns, "internet": internet,
        "ok": link and dns and internet, "summary": summary,
    }


__all__ = [
    "APPLY_TIMEOUT",
    "CREDENTIAL_MODE",
    "MAX_REASON",
    "MAX_SSID",
    "NEEDS_THE_INSTALLER_AGAIN",
    "NetworkError",
    "PLAN_MODE",
    "STAGING_SUFFIX",
    "WIFI_FILE",
    "WIRED_FILE",
    "apply_plan",
    "connectivity",
    "dhcp_plan",
    "interfaces",
    "nameservers",
    "read_plan",
    "scan",
    "staging_for",
    "static_plan",
    "wifi_plan",
    "wireless_interfaces",
    "write_plan",
]
