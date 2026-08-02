#!/usr/bin/env python3
"""Render the real autoinstall answer file from the committed template.

Called by installer/make-autoinstall.sh. The password arrives in the
environment, never on the command line, so it does not show up in `ps` or in
your shell history.

Substitution is done in Python rather than with sed because the values are
full of characters that break sed: '$' and '/' in the crypt hash, '+' and '/'
in the SSH key, and anything at all in a wifi passphrase.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sha512crypt import make_salt, sha512_crypt  # noqa: E402

# subiquity enforces these at config-load time (and since 26.04 it fails early
# rather than deep into the install), so check them here where the error is
# cheap and obvious.
USERNAME_RE = re.compile(r"\A[a-z_][a-z0-9_-]*\Z")
USERNAME_MAXLEN = 32
RESERVED = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "nobody",
    "systemd-network", "admin",
}

WIFI_BLOCK = """    wifis:
      wireless:
        match:
          name: "wl*"
        dhcp4: true
        optional: true
        # A higher route metric than the wired interfaces, so a box with both
        # plugged in prefers the cable and falls back to wifi automatically.
        dhcp4-overrides:
          route-metric: 600
        access-points:
          {ssid}:
            password: {password}
"""


def fail(msg):
    sys.stderr.write("make-autoinstall: %s\n" % msg)
    raise SystemExit(1)


def yaml_str(value):
    """Quote a value safely for YAML.

    A JSON string is always a valid YAML double-quoted scalar, so json.dumps
    gives correct escaping for free - including for passphrases containing
    quotes, backslashes or a leading '#'.
    """
    return json.dumps(value)


def main(argv):
    if len(argv) != 3:
        fail("usage: render_autoinstall.py <template> <output>")
    template_path, out_path = argv[1], argv[2]

    password = os.environ.get("RETROBOX_PASSWORD")
    if not password:
        fail("RETROBOX_PASSWORD is not set")

    username = os.environ.get("RETROBOX_USERNAME", "retrobox")
    hostname = os.environ.get("RETROBOX_HOSTNAME", "retrobox")
    key_path = os.environ.get("RETROBOX_SSH_KEY", "")
    wifi_ssid = os.environ.get("RETROBOX_WIFI_SSID", "")
    wifi_password = os.environ.get("RETROBOX_WIFI_PASSWORD", "")

    # --- validate --------------------------------------------------------
    if not USERNAME_RE.match(username) or len(username) > USERNAME_MAXLEN:
        fail("username %r is not valid on Ubuntu (must match [a-z_][a-z0-9_-]* "
             "and be at most %d characters)" % (username, USERNAME_MAXLEN))
    if username in RESERVED:
        fail("username %r is reserved; the install would abort" % username)
    if not re.match(r"\A[a-z0-9][a-z0-9-]*\Z", hostname):
        fail("hostname %r is not a valid DNS label" % hostname)
    if len(password) < 8:
        fail("that password is under 8 characters. It is the console password "
             "for every unit you ship - pick a longer one.")

    if not key_path:
        fail("no SSH public key given (--ssh-key). Remote access is key-only, "
             "so a box built without one can never be reached over the network.")
    try:
        with open(key_path, "r") as fh:
            key = fh.read().strip()
    except OSError as exc:
        fail("could not read the SSH public key: %s" % exc)

    if not key or not re.match(r"\A(ssh-|ecdsa-|sk-ssh-|sk-ecdsa-)", key):
        fail("%s does not look like an SSH PUBLIC key. Point --ssh-key at the "
             ".pub file, not the private key." % key_path)
    if "PRIVATE KEY" in key:
        fail("%s is a PRIVATE key. Use the matching .pub file." % key_path)
    if "\n" in key:
        fail("%s holds more than one key; give a file with exactly one."
             % key_path)

    if wifi_password and not wifi_ssid:
        fail("--wifi-password given without --wifi-ssid")
    if wifi_ssid and len(wifi_password) < 8:
        fail("a WPA2 passphrase must be at least 8 characters")

    # --- render ----------------------------------------------------------
    hashed = sha512_crypt(password, make_salt())
    # Cheap proof we did not silently produce a DES hash, which is what the
    # platform crypt() would have handed back on macOS.
    if not hashed.startswith("$6$") or len(hashed) < 90:
        fail("internal error: produced a hash that is not SHA-512 crypt")

    if wifi_ssid:
        wifi = WIFI_BLOCK.format(
            ssid=yaml_str(wifi_ssid), password=yaml_str(wifi_password)
        )
    else:
        wifi = ("    # No wireless configured. Re-run make-autoinstall.sh with\n"
                "    # --wifi-ssid/--wifi-password to add it.\n")

    try:
        with open(template_path, "r") as fh:
            doc = fh.read()
    except OSError as exc:
        fail("could not read the template: %s" % exc)

    # The wifi placeholder stands for a multi-line indented netplan block, so
    # unlike the scalar placeholders it may appear exactly once. Mentioning its
    # name a second time - in a comment, say - substitutes a whole `wifis:`
    # block into the middle of that sentence and the answer file quietly stops
    # being YAML. That has happened; this is cheaper than finding out from
    # subiquity.
    if doc.count("__WIFI_BLOCK__") != 1:
        fail("the template mentions __WIFI_BLOCK__ %d times; it stands for a "
             "whole netplan block and may appear exactly once (not even in a "
             "comment)" % doc.count("__WIFI_BLOCK__"))

    # The hash and the key are pasted into a document that is later PARSED as
    # YAML, so - like the wifi SSID/password below - they go through
    # yaml_str() rather than being trusted bare or hand-quoted in the
    # template. The hash happens to contain no character that breaks a
    # hand-typed single quote, which is luck, not a reason to skip this: an
    # SSH key's comment field routinely holds an apostrophe.
    for placeholder, value in (
        ("__HOSTNAME__", hostname),
        ("__USERNAME__", username),
        ("__PASSWORD_HASH__", yaml_str(hashed)),
        ("__SSH_AUTHORIZED_KEY__", yaml_str(key)),
        ("__WIFI_BLOCK__", wifi),
    ):
        if placeholder not in doc:
            fail("the template is missing the %s placeholder" % placeholder)
        doc = doc.replace(placeholder, value)

    # Strip the "this is a template" preamble so the shipped answer file does
    # not describe itself as a template, and replace it with a banner saying
    # what it actually is: a credential-bearing file that must not be committed.
    doc = re.sub(
        r"^# >>> TEMPLATE-ONLY >>>.*?^# <<< TEMPLATE-ONLY <<<\n",
        "# GENERATED by installer/make-autoinstall.sh - DO NOT COMMIT.\n"
        "# Contains a password hash and an SSH public key. Gitignored on purpose.\n"
        "# Edit installer/autoinstall.yaml.template and regenerate instead.\n",
        doc,
        flags=re.S | re.M,
    )

    left = re.findall(r"__[A-Z_]+__", doc)
    if left:
        fail("unsubstituted placeholders remain: %s" % ", ".join(sorted(set(left))))

    # 0600: this file contains the password hash and is the reason the real
    # answer file is gitignored.
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(doc)

    sys.stderr.write(
        "make-autoinstall: wrote %s (mode 0600)\n"
        "  hostname      : %s\n"
        "  username      : %s\n"
        "  password hash : $6$ SHA-512 crypt, %d chars\n"
        "  ssh key       : %s\n"
        "  wireless      : %s\n"
        % (out_path, hostname, username, len(hashed), key.split()[-1] if len(key.split()) > 2 else key[:24] + "...",
           wifi_ssid if wifi_ssid else "not configured")
    )


if __name__ == "__main__":
    main(sys.argv)
