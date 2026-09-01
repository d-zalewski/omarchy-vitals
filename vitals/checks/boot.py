"""Tier 0 - the running kernel is the deployed one, and it is complete.

A deploy installs a package, regenerates an initramfs and reboots. Each of
those can half-succeed in a way nothing else in this suite notices, because
everything already running keeps working:

  * kernel_current - the package was upgraded under a running kernel, so its
    module tree is gone and nothing new can be modprobed until reboot. It also
    quietly invalidates a comparison: the report is named after the running
    kernel, which may not be the one that was just installed.
  * module_tree    - depmod never ran, or the headers are absent, so modprobe
    and every DKMS build fail while loaded modules carry on.
  * initramfs      - mkinitcpio failed after the package installed. Nothing is
    wrong until the *next* boot, which is the worst time to find out.
  * microcode      - the ucode image dropped out of the initramfs.
"""
from __future__ import annotations

import platform
import re
from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check

MODULES_ROOT = "/usr/lib/modules"

# An initramfs smaller than this never contains a working userspace. A failed
# mkinitcpio characteristically leaves a truncated image rather than none.
MIN_INITRAMFS_BYTES = 1_000_000


def _release_key(release: str) -> tuple:
    """Sortable key for a kernel release string.

    Compares "7.2.3-1-omarchy-bore" against "7.2.2-5-omarchy-bore" numerically
    rather than lexically, where "10" would sort before "9".
    """
    return tuple(int(n) for n in re.findall(r"\d+", release))


def _module_trees(ctx) -> dict:
    """Installed module trees, mapped release -> pkgbase.

    Arch kernel packages write a `pkgbase` file into their module directory.
    That ties a release string back to the package that installed it without
    having to ask pacman, which keeps this working on any distro that follows
    the same layout. Trees without the file are grouped under "".
    """
    root = Path(MODULES_ROOT)
    if not root.is_dir():
        return {}
    trees = {}
    for d in sorted(root.iterdir()):
        if d.is_dir():
            trees[d.name] = ctx.read(str(d / "pkgbase"), "").strip()
    return trees


@check(tier=0, name="kernel_current", desc="running kernel is the installed one")
def kernel_current(ctx):
    """Guards every other number in the report.

    Comparing two reports assumes each was produced by the kernel it is named
    after. If a newer package is installed and the machine has not rebooted,
    that assumption is wrong and the comparison is meaningless.
    """
    running = platform.release()
    trees = _module_trees(ctx)
    if not trees:
        return Skip(f"no {MODULES_ROOT} - not a module-based distro kernel")
    if running not in trees:
        return Fail(f"no module tree for the running kernel {running} - it was "
                    f"replaced underneath you; modprobe cannot work until reboot",
                    kernel_reboot_pending=1)
    pkgbase = trees[running]
    newer = sorted((r for r, pb in trees.items()
                    if pb == pkgbase and _release_key(r) > _release_key(running)),
                   key=_release_key)
    if newer:
        return Warn(f"running {running} but {newer[-1]} is installed - reboot "
                    f"before trusting this report", kernel_reboot_pending=1)
    return Ok(f"running the installed {pkgbase or 'kernel'} ({running})",
              kernel_reboot_pending=0)


@check(tier=0, name="module_tree", desc="running kernel's modules are on disk and indexed")
def module_tree(ctx):
    """Proves modprobe has something to work with.

    tier 0's `modules` check counts what is already loaded, which says nothing
    about whether another module could be loaded now.
    """
    root = Path(MODULES_ROOT)
    if not root.is_dir():
        return Skip(f"no {MODULES_ROOT} - not a module-based distro kernel")
    base = root / platform.release()
    if not base.is_dir():
        return Fail(f"{base} is missing - no module can be loaded")
    dep = ctx.read(str(base / "modules.dep"), "")
    entries = [l.split(":", 1)[0] for l in dep.splitlines() if l.strip()]
    if not entries:
        return Fail("modules.dep is empty or absent - depmod never indexed this "
                    "tree; modprobe will not find anything")

    # Spot-check that the index and the files agree. An interrupted install
    # leaves the previous kernel's index in place, which modprobe believes
    # right up until it tries to open a file that is not there.
    sample = entries[:: max(1, len(entries) // 20)][:20]
    missing = [e for e in sample if not (base / e).exists()]
    if missing:
        return Fail(f"{len(missing)} of {len(sample)} sampled modules are indexed "
                    f"but absent from disk - the tree is inconsistent",
                    modules_indexed=len(entries))
    if not (base / "build" / "Makefile").exists():
        return Warn(f"{len(entries)} modules indexed but no build/Makefile - "
                    f"DKMS cannot build anything for this kernel",
                    modules_indexed=len(entries))
    return Ok(f"{len(entries)} modules indexed, headers present",
              modules_indexed=len(entries))


def _boot_images(ctx):
    """(size, path) for every kernel image under /boot, or None if unreadable.

    /boot is frequently the ESP mounted root-only, where a plain find returns
    nothing at all rather than failing in a way the caller can see. Retry under
    sudo, which several checks in this suite already depend on.
    """
    args = ["find", "/boot", "-maxdepth", "3", "(",
            "-name", "vmlinuz-*", "-o", "-name", "initramfs-*",
            "-o", "-name", "initrd.img-*", "-o", "-name", "*.efi", ")",
            "-printf", "%s\\t%p\\n"]
    r = ctx.run(args, timeout=30)
    if not r.stdout.strip() and "Permission denied" in (r.stderr or ""):
        r = ctx.sudo(args, timeout=30)
        if not r.stdout.strip():
            return None
    images = []
    for line in r.stdout.splitlines():
        size, _, path = line.partition("\t")
        if size.strip().isdigit() and path:
            images.append((int(size), path))
    return images


@check(tier=0, name="initramfs", desc="every installed kernel has a bootable image")
def initramfs(ctx):
    """Catches a failed image build before the reboot that would expose it.

    Two layouts, because a UKI setup has no separate initramfs to look for:
    the initramfs is linked into the .efi, so the image itself is the artifact
    that has to exist for each installed kernel.
    """
    images = _boot_images(ctx)
    if images is None:
        return Skip("/boot is not readable, even with sudo")
    by_name = {Path(p).name: size for size, p in images}
    releases = sorted(n[len("vmlinuz-"):] for n in by_name if n.startswith("vmlinuz-"))
    # Only /EFI/Linux holds unified kernel images; the bootloader's own .efi
    # binaries live elsewhere in /boot and are much smaller.
    ukis = {Path(p).stem: size for size, p in images if "/efi/linux/" in p.lower()}

    if releases:
        found = {r: next((by_name[c] for c in (f"initramfs-{r}.img",
                                               f"initramfs-{r}",
                                               f"initrd.img-{r}")
                          if c in by_name), None) for r in releases}
        missing = [r for r, size in found.items() if size is None]
        if missing:
            return Fail(f"no initramfs for {', '.join(missing)} - that boot "
                        f"entry will not come up")
        tiny = [r for r, size in found.items() if size < MIN_INITRAMFS_BYTES]
        if tiny:
            return Fail(f"initramfs for {', '.join(tiny)} is truncated - the "
                        f"image build did not finish")
        return Ok(f"{len(releases)} kernel(s), each with an initramfs")

    if not ukis:
        return Skip("no kernel images found in /boot")

    # A UKI is named after its pkgbase, not its version, so this proves an
    # image exists for each installed kernel - kernel_current is what proves
    # the running one is current.
    pkgbases = {pb for pb in _module_trees(ctx).values() if pb}
    small = sorted(n for n, size in ukis.items() if size < MIN_INITRAMFS_BYTES)
    if small:
        return Fail(f"unified kernel image {', '.join(small)} is truncated - "
                    f"the image build did not finish")
    if not pkgbases:
        return Info(f"{len(ukis)} unified kernel image(s)")
    absent = sorted(pb for pb in pkgbases
                    if not any(stem.endswith(pb) for stem in ukis))
    if absent:
        return Fail(f"{', '.join(absent)} is installed but has no unified "
                    f"kernel image - it cannot be booted")
    return Ok(f"{len(ukis)} unified kernel image(s), one per installed kernel")


@check(tier=0, name="microcode", desc="CPU microcode loaded early from the initramfs")
def microcode(ctx):
    """Microcode is applied from the initramfs before the CPU is trusted.

    Regenerating an initramfs without the ucode image is silent: the machine
    boots and runs on whatever revision the BIOS supplied, which is how you end
    up debugging an erratum that was fixed years ago.
    """
    rev = ""
    for line in ctx.read("/proc/cpuinfo", "").splitlines():
        if line.startswith("microcode"):
            rev = line.split(":", 1)[1].strip()
            break
    if not rev:
        return Skip("CPU reports no microcode revision (VM or unsupported)")
    # "patch_level"/"Current revision" lines are printed whether or not an
    # update was applied, so only an explicit "updated early" counts.
    if ctx.count_matches(ctx.journal_kernel, r"microcode:.*updated early"):
        return Ok(f"revision {rev}, updated early from the initramfs")
    if ctx.count_matches(ctx.journal_kernel, r"microcode:"):
        return Info(f"revision {rev}, no early update logged - BIOS is current "
                    f"or the CPU takes no update")
    return Warn(f"revision {rev} but the microcode driver logged nothing - the "
                f"ucode image may be missing from the initramfs")
