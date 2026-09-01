"""Tier 1 - modules load, and every device that matters has a driver bound.

Tier 0 counts loaded modules and greps the journal for probe failures. Neither
sees the two things a custom kernel build actually gets wrong:

  * a module that cannot be loaded at all - wrong vermagic, a compression
    format the kernel was not built for, or a signature it will not accept.
    Everything already loaded keeps working, so the machine looks healthy right
    up until something needs a module it has not loaded yet.
  * a driver that was never compiled. There is no probe failure to grep for:
    the device sits on the bus with nothing bound to it, silently.

USB interfaces are deliberately not checked the same way - unbound interfaces
are common and benign there (fingerprint readers, vendor-specific endpoints),
and a check that cries wolf is a check people stop reading.
"""
from __future__ import annotations

import platform
import re
from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check

# Loaded and immediately unloaded to prove the path works. Each is inert: it
# creates no device and changes no behaviour. The first one not already loaded
# is used, so the check never unloads something that is in use.
PROBE_MODULES = ("dummy", "nbd", "cpuid", "msr")

# modprobe's own errors are terse. These are the three that a freshly built
# kernel produces, and what each one actually means.
MODPROBE_HINTS = (
    ("Key was rejected", "the kernel enforces signatures this module does not carry"),
    ("Required key not available", "lockdown or Secure Boot is rejecting the signature"),
    ("Invalid module format", "vermagic mismatch - these modules are from another build"),
    ("not found", "not in modules.dep - depmod did not index this tree"),
)

# PCI classes where nothing bound means something on this machine does not
# work. Host bridges, PCI bridges and vendor "non-essential instrumentation"
# are unbound on a perfectly healthy box, so they are not listed here.
PCI_CLASSES = {
    "0x0100": "SCSI storage", "0x0101": "IDE", "0x0104": "RAID",
    "0x0106": "SATA", "0x0107": "SAS", "0x0108": "NVMe",
    "0x0200": "ethernet", "0x0280": "wireless",
    "0x0300": "VGA", "0x0302": "3D",
    "0x0401": "audio", "0x0403": "HD audio",
    "0x0805": "SD host controller",
    "0x0c03": "USB controller", "0x0c04": "fibre channel",
}


def _sudo_refused(r) -> bool:
    """sudo -n declining is a Skip, not a failure of what we were testing."""
    text = (r.stderr or "") + (r.stdout or "")
    return "password is required" in text or "a terminal is required" in text


def _probe_candidate(ctx, loaded):
    """First inert module that is a real .ko on this kernel.

    A module compiled in (=y rather than =m) makes modprobe succeed while
    nothing appears in lsmod. That is not a failure, it is the wrong test
    subject - modinfo reports "(builtin)" for those, so they are passed over.
    """
    for m in PROBE_MODULES:
        if m in loaded:
            continue
        if ".ko" in ctx.run(["modinfo", "-F", "filename", m], timeout=30).stdout:
            return m
    return None


def _loaded_modules(ctx) -> set:
    lines = ctx.run(["lsmod"]).stdout.splitlines()[1:]
    return {l.split()[0] for l in lines if l.split()}


@check(tier=1, name="module_load", desc="a module can actually be loaded",
       requires=["modprobe", "modinfo"], est_seconds=3)
def module_load(ctx):
    """The one check that exercises the whole module path end to end.

    Covers module compression, the vermagic/ABI match against the running
    kernel, signature acceptance and the depmod index in a single operation.
    """
    victim = _probe_candidate(ctx, _loaded_modules(ctx))
    if victim is None:
        return Skip("no unloaded, loadable probe module on this kernel")

    args = ["modprobe", victim]
    if victim == "dummy":
        args.append("numdummies=0")          # do not create a dummy0 interface
    r = ctx.sudo(args, timeout=30)
    if r.returncode != 0:
        if _sudo_refused(r):
            return Skip("needs passwordless sudo")
        err = ((r.stderr or r.stdout).strip().splitlines() or [f"rc={r.returncode}"])[-1]
        hint = next((h for pat, h in MODPROBE_HINTS if pat in err), "")
        return Fail(f"modprobe {victim} failed: {err[:80]}"
                    + (f" - {hint}" if hint else ""))

    if not re.search(rf"^{victim}\b", ctx.run(["lsmod"]).stdout, re.M):
        return Fail(f"modprobe {victim} reported success but it is not loaded")

    if ctx.sudo(["modprobe", "-r", victim], timeout=30).returncode != 0:
        return Warn(f"{victim} loaded but would not unload")
    return Ok(f"{victim} loaded and unloaded - format, compression and "
              f"signatures all accepted")


@check(tier=1, name="module_sig", desc="shipped modules carry a usable signature",
       requires=["modinfo"])
def module_sig(ctx):
    """Functional counterpart to `modules_signed`, which only reads the config.

    Signing with an ephemeral key and enforcing signatures is a combination
    that boots fine and then refuses every module it did not start with.

    The signer's name is deliberately not recorded - reports get committed, and
    the hash algorithm is the part that matters.
    """
    enforced = ctx.read("/sys/module/module/parameters/sig_enforce", "").strip() == "Y"
    loaded = sorted(_loaded_modules(ctx))
    if not loaded:
        return Skip("no loaded modules to inspect")
    name = loaded[0]
    algo = ctx.run(["modinfo", "-F", "sig_hashalgo", name], timeout=30).stdout.strip()
    if algo and algo != "(none)":
        return Ok(f"modules signed ({algo})"
                  + (", enforcement on" if enforced else ", enforcement off"),
                  module_signed=1)
    if enforced:
        return Fail("signature enforcement is on but shipped modules are "
                    "unsigned - nothing new will load", module_signed=0)
    return Info("modules are unsigned (enforcement off)", module_signed=0)


@check(tier=1, name="dkms", desc="out-of-tree modules are built for this kernel",
       requires=["dkms"])
def dkms(ctx):
    """The most common breakage after a kernel swap.

    A DKMS module built for the previous kernel is not a warning about the
    future - the driver is missing right now, on the kernel being tested.
    """
    r = ctx.run(["dkms", "status"], timeout=60)
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if not lines:
        return Skip("no DKMS modules registered")
    running = platform.release()
    # dkms 3.x prints "nvidia/550.107.02, 6.12.4-arch1-1, x86_64: installed";
    # 2.x separates name and version with a comma. Taking the field before the
    # first comma or slash gets the module name out of both.
    names = sorted({l.split(",")[0].split("/")[0].strip() for l in lines})
    built = {l.split(",")[0].split("/")[0].strip() for l in lines
             if running in l and "installed" in l.rsplit(":", 1)[-1]}
    missing = [n for n in names if n not in built]
    if missing:
        return Fail(f"not built for {running}: {', '.join(missing)} - "
                    f"{'these drivers are' if len(missing) > 1 else 'this driver is'} "
                    f"missing on this boot", dkms_missing=len(missing))
    return Ok(f"{len(built)} DKMS module(s) installed for {running}",
              dkms_missing=0)


@check(tier=1, name="pci_drivers", desc="storage/net/display/audio devices have drivers")
def pci_drivers(ctx):
    """Finds drivers that were never compiled, which log nothing at all.

    `probe_failures` greps for drivers that tried and failed. A driver missing
    from the config does not try, so the only evidence is a device on the bus
    with an empty driver link.
    """
    root = Path("/sys/bus/pci/devices")
    if not root.is_dir():
        return Skip("no PCI bus")
    unbound, total = [], 0
    for dev in sorted(root.iterdir()):
        label = PCI_CLASSES.get(ctx.read(str(dev / "class"), "")[:6])
        if label is None:
            continue
        total += 1
        if (dev / "driver").exists():
            continue
        vendor = ctx.read(str(dev / "vendor"), "?").replace("0x", "")
        device = ctx.read(str(dev / "device"), "?").replace("0x", "")
        unbound.append(f"{label} [{vendor}:{device}]")
    if total == 0:
        return Skip("no PCI devices in the classes this checks")
    if unbound:
        # Warn, not Fail: a second GPU nobody uses is a choice, not a fault.
        return Warn(f"{len(unbound)} of {total} device(s) with no driver bound: "
                    f"{', '.join(unbound[:4])}", pci_unbound=len(unbound))
    return Ok(f"all {total} storage/network/display/audio/USB devices bound",
              pci_unbound=0)
