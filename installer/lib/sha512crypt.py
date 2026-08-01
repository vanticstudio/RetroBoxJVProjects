#!/usr/bin/env python3
"""SHA-512 crypt ($6$), implemented on hashlib alone.

Why this exists instead of `crypt.crypt`, `mkpasswd` or `openssl passwd -6`:

  * macOS's libcrypt does not implement $6$. On a Mac,
    `crypt.crypt(pw, "$6$somesalt")` returns a THIRTEEN CHARACTER DES hash -
    it reads "$6" as a two-character salt and silently ignores the rest.
    That hash looks plausible, drops into the answer file without complaint,
    and ships a unit whose password is truncated to eight significant
    characters. Perl's crypt() does exactly the same thing.
  * The system `openssl` on macOS is LibreSSL, whose `passwd` has no -6.
  * `mkpasswd` (from the whois package) does not ship on macOS.
  * Python removed the `crypt` module entirely in 3.13.

So the algorithm is implemented here from Ulrich Drepper's specification, and
checked against that specification's own published test vectors. Run this file
directly to verify:  python3 sha512crypt.py

Python 3.6+. No third-party dependencies, so it works on the python3 that
ships with macOS.
"""

import hashlib

# crypt(3)'s base64 alphabet, which is NOT the RFC 4648 one.
B64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

DEFAULT_ROUNDS = 5000
MIN_ROUNDS = 1000
MAX_ROUNDS = 999999999


def _b64_from_24bit(b2, b1, b0, n):
    """Emit n crypt-base64 characters from a 24-bit group."""
    w = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(n):
        out.append(B64[w & 0x3F])
        w >>= 6
    return "".join(out)


def sha512_crypt(password, salt, rounds=None):
    """Return the $6$ hash of `password` with `salt`.

    `salt` is the raw salt only - no "$6$" prefix and no rounds= part. It is
    truncated to 16 characters, as the specification requires.
    """
    if isinstance(password, str):
        password = password.encode("utf-8")
    salt = salt[:16]
    salt_b = salt.encode("utf-8")

    explicit_rounds = rounds is not None
    if rounds is None:
        rounds = DEFAULT_ROUNDS
    rounds = max(MIN_ROUNDS, min(MAX_ROUNDS, int(rounds)))

    # Digest B: password + salt + password
    b = hashlib.sha512(password + salt_b + password).digest()

    # Digest A: password + salt, then |password| bytes of B, then one of
    # B/password per bit of |password|, high bit to low.
    a_ctx = hashlib.sha512()
    a_ctx.update(password + salt_b)
    plen = len(password)
    a_ctx.update(b * (plen // 64))
    if plen % 64:
        a_ctx.update(b[: plen % 64])
    n = plen
    while n:
        a_ctx.update(b if n & 1 else password)
        n >>= 1
    a = a_ctx.digest()

    # Sequence P: digest of the password repeated |password| times.
    dp = hashlib.sha512(password * plen).digest()
    p = dp * (plen // 64) + dp[: plen % 64]

    # Sequence S: digest of the salt repeated 16 + A[0] times.
    ds = hashlib.sha512(salt_b * (16 + a[0])).digest()
    slen = len(salt_b)
    s = ds * (slen // 64) + ds[: slen % 64]

    # The stretching loop.
    c = a
    for i in range(rounds):
        ctx = hashlib.sha512()
        ctx.update(p if i & 1 else c)
        if i % 3:
            ctx.update(s)
        if i % 7:
            ctx.update(p)
        ctx.update(c if i & 1 else p)
        c = ctx.digest()

    # The specification's permuted base64 of the final digest.
    order = [
        (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4),
        (47, 5, 26), (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51),
        (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13), (56, 14, 35),
        (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19),
        (62, 20, 41),
    ]
    out = []
    for x, y, z in order:
        out.append(_b64_from_24bit(c[x], c[y], c[z], 4))
    out.append(_b64_from_24bit(0, 0, c[63], 2))

    prefix = "$6$"
    if explicit_rounds:
        prefix += "rounds=%d$" % rounds
    return prefix + salt + "$" + "".join(out)


def make_salt(length=16):
    """A fresh random salt drawn from crypt's base64 alphabet."""
    import secrets

    return "".join(secrets.choice(B64) for _ in range(length))


# Test vectors from Drepper's SHA-crypt specification.
VECTORS = [
    ("Hello world!", "saltstring", None,
     "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJu"
     "esI68u4OTLiBFdcbYEdFCoEOfaS35inz1"),
    ("Hello world!", "saltstringsaltstring", 10000,
     "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sb"
     "HbbMCVNSnCM/UrjmM0Dp8vOuZeHBy/YTBmSK6H9qs/y3RnOaw5v."),
    ("This is just a test", "toolongsaltstring", 5000,
     "$6$rounds=5000$toolongsaltstrin$lQ8jolhgVRVhY4b5pZKaysCLi0QBxGoNeKQ"
     "zQ3glMhwllF7oGDZxUhx1yxdYcz/e1JSbq3y6JMxxl8audkUEm0"),
    ("a very much longer text to encrypt.  This one even stretches over more"
     "than one line.", "anotherlongsaltstring", 1400,
     "$6$rounds=1400$anotherlongsalts$POfYwTEok97VWcjxIiSOjiykti.o/pQs.wP"
     "vMxQ6Fm7I6IoYN3CmLs66x9t0oSwbtEW7o7UmJEiDwGqd8p4ur1"),
    ("we have a short salt string but not a short password", "short", 77777,
     "$6$rounds=77777$short$WuQyW2YR.hBNpjjRhpYD/ifIw05xdfeEyQoMxIXbkvr0g"
     "ge1a1x3yRULJ5CCaUeOxFmtlcGZelFl5CxtgfiAc0"),
]


def self_test():
    """Check the implementation against the specification's vectors."""
    ok = True
    for pw, salt, rnds, expect in VECTORS:
        got = sha512_crypt(pw, salt, rnds)
        if got == expect:
            print("PASS  %s..." % expect[:44])
        else:
            ok = False
            print("FAIL\n  expected %s\n  got      %s" % (expect, got))
    print("ALL %d VECTORS PASS" % len(VECTORS) if ok else "FAILURES PRESENT")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if self_test() else 1)
