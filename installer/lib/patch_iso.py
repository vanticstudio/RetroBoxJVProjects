#!/usr/bin/env python3
"""Add the `autoinstall` kernel argument to an Ubuntu Server ISO, in place.

Why in place rather than a normal remaster:

The Ubuntu 26.04 live-server ISO is a hybrid image - GRUB boot code in the MBR,
a protective 0xEE partition, a GPT, an El Torito catalog with both a BIOS and a
UEFI entry, and a Canonical-signed EFI System Partition APPENDED as a real
partition rather than stored as a file in the ISO9660 tree. Rebuilding that
needs xorriso with --grub2-mbr, -partition_offset 16 and -append_partition.
macOS has no such tool: hdiutil makehybrid supports one El Torito entry and
cannot emit a GPT at all, and it cannot even mount an ISO9660 filesystem.

But we do not need to rebuild anything. /boot/grub/grub.cfg is a single
2048-byte sector, and on the stock 26.04 ISO only 394 of those bytes are used -
the remaining 1654 are zero padding. Both the BIOS path (El Torito ->
boot/grub/i386-pc/eltorito.img) and the UEFI path (signed shim -> signed grub ->
`search --file /.disk/info` -> $prefix/grub.cfg) read that same file. So
rewriting those bytes on a COPY of the ISO changes the boot menu for both
firmware modes while leaving the MBR, GPT, El Torito catalog and the signed ESP
byte-for-byte identical.

That last point is what makes this safe under Secure Boot. The signature chain
covers shim and grubx64.efi, which we never touch; grub.cfg is ordinary data on
the ISO9660 filesystem and is not signed or hashed by the boot chain. (md5sum.txt
does list it, but nothing verifies that during a normal boot - there is no
integrity-check menu entry on this ISO.)

The new config is written to EXACTLY the original byte length, padded with
newlines, so the ISO9660 directory records stay correct and untouched. This ISO
carries four copies of those records - a primary and a Joliet tree, each
duplicated because it was built with -partition_offset 16 - so keeping the
length fixed avoids having to patch all four in lockstep.

Requires nothing but the python3 that ships with macOS.
"""

import os
import shutil
import sys

SECTOR = 2048


# --- ISO9660 -----------------------------------------------------------------
# Just enough of the format to find one file. Deliberately parsed rather than
# hardcoded to a byte offset, so this keeps working when Canonical respins the
# ISO and grub.cfg lands on a different sector.

def _u32le(b, off):
    return int.from_bytes(b[off:off + 4], "little")


def _records(data):
    """Yield (name, extent_lba, length) for each record in a directory extent."""
    pos = 0
    while pos < len(data):
        rec_len = data[pos]
        if rec_len == 0:
            # Records do not straddle sector boundaries; skip to the next one.
            pos = (pos // SECTOR + 1) * SECTOR
            if pos >= len(data):
                return
            continue
        rec = data[pos:pos + rec_len]
        if len(rec) < 33:
            return
        extent = _u32le(rec, 2)
        length = _u32le(rec, 10)
        name_len = rec[32]
        name = rec[33:33 + name_len]
        if name_len == 1 and name in (b"\x00", b"\x01"):
            label = "." if name == b"\x00" else ".."
        else:
            label = name.decode("latin-1").split(";")[0].upper()
        yield label, extent, length
        pos += rec_len


def find_file(fh, path):
    """Locate `path` (e.g. 'boot/grub/grub.cfg') in the primary ISO9660 tree.

    Returns (absolute_byte_offset, length_in_bytes).
    """
    fh.seek(16 * SECTOR)
    pvd = fh.read(SECTOR)
    if pvd[1:6] != b"CD001":
        raise SystemExit("not an ISO9660 image (no CD001 at sector 16)")
    if pvd[0] != 1:
        raise SystemExit("sector 16 is not a Primary Volume Descriptor")

    root = pvd[156:156 + 34]
    extent = _u32le(root, 2)
    length = _u32le(root, 10)

    for part in path.upper().split("/"):
        fh.seek(extent * SECTOR)
        data = fh.read(length)
        for name, child_extent, child_len in _records(data):
            if name == part:
                extent, length = child_extent, child_len
                break
        else:
            raise SystemExit("could not find %r in the ISO" % path)
    return extent * SECTOR, length


# --- the edit ----------------------------------------------------------------

def patch_config(original, kernel_args, budget):
    """Insert `kernel_args` into a GRUB config, trimmed to fit `budget` bytes.

    Returns (new_bytes, notes) or raises SystemExit if it cannot be made to fit.
    """
    text = original.decode("utf-8")
    notes = []

    if " %s " % kernel_args in text:
        notes.append("already contains %r" % kernel_args)
        return original, notes

    # The stock config leaves a double space before '---' as an injection point.
    # '---' separates live-session arguments from ones copied to the installed
    # system's bootloader, so our token has to go BEFORE it or casper and
    # subiquity never see it.
    if "/casper/vmlinuz  ---" in text:
        text = text.replace("/casper/vmlinuz  ---",
                            "/casper/vmlinuz  %s ---" % kernel_args)
    elif " ---" in text:
        text = text.replace(" ---", " %s ---" % kernel_args, 1)
    else:
        raise SystemExit("no '---' marker on the kernel line; refusing to guess")
    notes.append("added %r before ---" % kernel_args)

    # Boot straight in. The menu is pointless on a walk-away build, and this
    # also buys back a byte.
    if "set timeout=30" in text:
        text = text.replace("set timeout=30", "set timeout=0")
        notes.append("timeout 30 -> 0")

    # If it still does not fit, drop cosmetics in increasing order of regret.
    # None of these affect whether or how the box boots.
    trims = [
        ("loadfont unicode\n\n", "dropped 'loadfont unicode'"),
        ("set menu_color_normal=white/black\n", "dropped menu_color_normal"),
        ("set menu_color_highlight=black/light-gray\n", "dropped menu_color_highlight"),
        ("    set gfxpayload=keep\n", "dropped gfxpayload=keep"),
    ]
    for needle, note in trims:
        if len(text.encode("utf-8")) <= budget:
            break
        if needle in text:
            text = text.replace(needle, "", 1)
            notes.append(note)

    blob = text.encode("utf-8")
    if len(blob) > budget:
        raise SystemExit(
            "patched config is %d bytes but only %d are available, and there is "
            "nothing left to trim" % (len(blob), budget)
        )

    # Pad to exactly the original length with newlines, which GRUB ignores.
    # Keeping the length identical is what lets us leave every ISO9660
    # directory record alone.
    blob += b"\n" * (budget - len(blob))
    return blob, notes


def main(argv):
    if len(argv) < 3:
        raise SystemExit(
            "usage: patch_iso.py <source.iso> <output.iso> [kernel-args]")
    src, dst = argv[1], argv[2]
    kernel_args = argv[3] if len(argv) > 3 else "autoinstall"

    if os.path.abspath(src) == os.path.abspath(dst):
        raise SystemExit("refusing to patch the source ISO in place; "
                         "give a different output path")
    if not os.path.exists(src):
        raise SystemExit("no such file: %s" % src)

    src_size = os.path.getsize(src)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(dst))).free
    if free < src_size + (64 << 20):
        raise SystemExit(
            "not enough free space: need ~%.1f GB, have %.1f GB"
            % (src_size / 1e9, free / 1e9))

    print("==> Copying %s -> %s (%.2f GB)" % (src, dst, src_size / 1e9))
    shutil.copyfile(src, dst)

    changed = 0
    with open(dst, "r+b") as fh:
        for path in ("boot/grub/grub.cfg", "boot/grub/loopback.cfg"):
            try:
                offset, length = find_file(fh, path)
            except SystemExit as exc:
                print("    %s: %s (skipping)" % (path, exc))
                continue

            fh.seek(offset)
            original = fh.read(length)

            # Confirm the rest of the sector really is padding before relying
            # on it. If Canonical ever packs another file in behind this one,
            # stop rather than corrupt it.
            tail_len = (SECTOR - (length % SECTOR)) % SECTOR
            fh.seek(offset + length)
            tail = fh.read(tail_len)
            if tail.strip(b"\x00"):
                print("    %s: sector tail is not padding; skipping" % path)
                continue

            try:
                new, notes = patch_config(original, kernel_args, length)
            except SystemExit as exc:
                print("    %s: %s (skipping)" % (path, exc))
                continue

            print("--> %s at byte %d (%d bytes)" % (path, offset, length))
            for note in notes:
                print("      - %s" % note)

            if new == original:
                print("      already patched, nothing written")
                continue

            fh.seek(offset)
            fh.write(new)
            changed += 1

        fh.flush()
        os.fsync(fh.fileno())

    if not changed:
        raise SystemExit("nothing was patched - the ISO is unchanged")

    # Read it back and prove the token is really on the kernel line.
    with open(dst, "rb") as fh:
        offset, length = find_file(fh, "boot/grub/grub.cfg")
        fh.seek(offset)
        final = fh.read(length).decode("utf-8")
    for line in final.splitlines():
        if "/casper/vmlinuz" in line:
            print("==> Kernel line is now:%s" % line.rstrip())
            if kernel_args.split()[0] not in line.split():
                raise SystemExit("verification FAILED: %r is not a bare token "
                                 "on the kernel line" % kernel_args)
    print("==> Patched %d file(s). Boot records, GPT and the signed ESP are "
          "untouched." % changed)


if __name__ == "__main__":
    main(sys.argv)
