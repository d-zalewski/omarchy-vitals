"""Tier 1 - the storage stack actually stores things.

`storage` in tier 1 counts block devices and greps for I/O errors. That says
nothing about whether a write survives the trip, whether the filesystem has
noticed corruption, or whether the parts of the stack that only matter over
months - scrub results, TRIM reaching the drive - are working at all.

  * fs_roundtrip - write, fsync, drop the page cache, read back, compare.
  * btrfs_health - the error counters btrfs keeps and the last scrub result.
  * swap_zram    - swap exists, and zram is compressing rather than merely
    configured.
  * discard      - whether TRIM survives every layer between the filesystem
    and the drive. Recorded, not judged: modern controllers cope without it.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check, disk_dir, sudo_refused

ROUNDTRIP_BYTES = 1 << 20

# posix_fadvise is Linux-only. Bound once here rather than probed with
# hasattr at call time, so the tests cover the same lines on any platform
# the suite is developed on.
FADVISE = getattr(os, "posix_fadvise", None)
FADV_DONTNEED = getattr(os, "POSIX_FADV_DONTNEED", 0)

# /sys/block/<dev>/mm_stat, in order. Only the first two are needed here.
ZRAM_ORIG, ZRAM_COMPRESSED = 0, 1

# Below this, the compression ratio is arithmetic on noise: an idle zram
# holding one page reports 64x, which means nothing and would churn in
# a comparison.
ZRAM_RATIO_FLOOR = 1 << 20


@check(tier=1, name="fs_roundtrip", desc="a file survives write, fsync and read back")
def fs_roundtrip(ctx):
    """Push a megabyte through the whole stack and read it back off the device.

    posix_fadvise(DONTNEED) after the fsync evicts the page cache for the file,
    so the comparison reads from the device rather than from memory. That needs
    no root, unlike dropping the whole cache.
    """
    directory, fstype = disk_dir(ctx)
    if directory is None:
        return Skip("no writable directory on real storage")
    payload = os.urandom(ROUNDTRIP_BYTES)
    digest = hashlib.sha256(payload).hexdigest()
    path = Path(directory) / "vitals-roundtrip.tmp"
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        fd = os.open(str(path), os.O_RDONLY)
        try:
            if FADVISE:
                # Evict just this file from the page cache, so the comparison
                # below reads from the device instead of from memory.
                FADVISE(fd, 0, ROUNDTRIP_BYTES, FADV_DONTNEED)
            read_back = os.read(fd, ROUNDTRIP_BYTES)
        finally:
            os.close(fd)
    except OSError as exc:
        return Fail(f"{fstype} write/read failed: {exc.strerror or exc}")
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if hashlib.sha256(read_back).hexdigest() != digest:
        return Fail(f"data read back from {fstype} does not match what was "
                    f"written - filesystem or device corruption")
    return Ok(f"1 MiB round-tripped through {fstype} intact")


@check(tier=1, name="btrfs_health", desc="btrfs error counters and last scrub")
def btrfs_health(ctx):
    """The counters btrfs keeps for itself, which nothing surfaces otherwise.

    corruption_errs is the one that matters: btrfs checksums everything it
    reads, so a non-zero count is data that came back wrong.
    """
    if not ctx.have("btrfs"):
        return Skip("btrfs-progs not installed")
    mount = "/"
    r = ctx.run(["btrfs", "device", "stats", mount], timeout=30)
    if r.returncode != 0:
        return Skip(f"{mount} is not btrfs")
    counters = {k: int(v) for k, v in
                re.findall(r"\.(\w+)\s+(\d+)", r.stdout)}
    if not counters:
        return Skip("btrfs reported no device statistics")
    bad = {k: v for k, v in counters.items() if v}
    io_errors = sum(counters.values())

    # Scrub state needs root; without it the counters above still stand.
    scrub = ctx.sudo(["btrfs", "scrub", "status", mount], timeout=30)
    scrub_errors = 0
    scrub_note = ""
    if scrub.returncode == 0 and not sudo_refused(scrub):
        if "no errors found" in scrub.stdout:
            scrub_note = ", last scrub clean"
        else:
            found = re.search(r"Error summary:\s*(.+)", scrub.stdout)
            if found:
                scrub_errors = 1
                scrub_note = f", last scrub: {found.group(1).strip()[:40]}"
            elif "never" in scrub.stdout.lower():
                scrub_note = ", never scrubbed"
    if bad:
        return Fail(f"btrfs error counters non-zero: "
                    f"{', '.join(f'{k}={v}' for k, v in bad.items())}{scrub_note}",
                    btrfs_io_errors=io_errors, btrfs_scrub_errors=scrub_errors)
    if scrub_errors:
        return Warn(f"counters clean but{scrub_note}",
                    btrfs_io_errors=0, btrfs_scrub_errors=scrub_errors)
    return Ok(f"all error counters zero{scrub_note}",
              btrfs_io_errors=0, btrfs_scrub_errors=0)


@check(tier=1, name="swap_zram", desc="swap is configured and zram compresses")
def swap_zram(ctx):
    r = ctx.run(["swapon", "--show=NAME,TYPE,SIZE,USED", "--bytes", "--noheadings"],
                timeout=30)
    devices = [l.split() for l in r.stdout.splitlines() if l.strip()]
    if not devices:
        return Info("no swap configured - the OOM killer is the only backstop",
                    swap_kb=0)
    total_kb = sum(int(d[2]) for d in devices if len(d) > 2 and d[2].isdigit()) // 1024
    names = ", ".join(Path(d[0]).name for d in devices)
    zram = [d for d in devices if "zram" in d[0]]
    if not zram:
        return Ok(f"{total_kb // 1024} MiB swap on {names}", swap_kb=total_kb)

    stat = ctx.read(f"/sys/block/{Path(zram[0][0]).name}/mm_stat", "").split()
    if len(stat) <= ZRAM_COMPRESSED:
        return Ok(f"{total_kb // 1024} MiB swap including zram", swap_kb=total_kb)
    original, compressed = int(stat[ZRAM_ORIG]), int(stat[ZRAM_COMPRESSED])
    if original < ZRAM_RATIO_FLOOR:
        return Ok(f"{total_kb // 1024} MiB swap, zram configured and holding "
                  f"{original // 1024} KiB", swap_kb=total_kb)
    ratio = original / max(compressed, 1)
    if ratio < 1.2:
        return Warn(f"zram compression ratio only {ratio:.1f}x - it is costing "
                    f"RAM for little gain", swap_kb=total_kb)
    return Ok(f"{total_kb // 1024} MiB swap, zram compressing {ratio:.1f}x",
              swap_kb=total_kb)


@check(tier=1, name="discard", desc="TRIM reaches the drive through every layer")
def discard(ctx):
    """Records the state of the discard path without calling any of it a fault.

    Over-provisioning means a modern controller garbage-collects perfectly well
    without ever being told which blocks are free, so a missing TRIM path is
    not a problem to fix on a drive with room to spare. It only becomes
    measurable on one kept near-full under a write-heavy workload.

    Worth recording anyway: the metric turns a change into a regression for
    anyone who set the path up deliberately, and dm-crypt silently dropping
    discards is not otherwise visible anywhere.
    """
    r = ctx.run(["lsblk", "-Dnrbo", "NAME,TYPE,DISC-MAX"], timeout=30)
    rows = [l.split() for l in r.stdout.splitlines() if len(l.split()) >= 3]
    if not rows:
        return Skip("lsblk reported no discard information")
    disks = [int(c) for _, t, c in rows if t == "disk" and c.isdigit()]
    if not any(disks):
        return Skip("no drive here supports discard")
    blockers = sorted({t for _, t, c in rows
                       if t in ("crypt", "lvm", "raid0", "raid1")
                       and c.isdigit() and int(c) == 0})
    timer = ctx.run(["systemctl", "is-enabled", "fstrim.timer"],
                    timeout=30).stdout.strip()
    if blockers:
        # Passing discards through dm-crypt reveals which blocks are in use,
        # so refusing them is a defensible default, not a misconfiguration.
        return Info(f"TRIM stops at {'/'.join(blockers)} - harmless with free "
                    f"space to spare, and passing it through would leak which "
                    f"blocks are in use", discard_reaches_drive=0)
    if timer != "enabled":
        return Info(f"TRIM reaches the drive but fstrim.timer is "
                    f"{timer or 'absent'}, so nothing issues it",
                    discard_reaches_drive=1)
    return Ok("TRIM reaches the drive, fstrim.timer enabled",
              discard_reaches_drive=1)
