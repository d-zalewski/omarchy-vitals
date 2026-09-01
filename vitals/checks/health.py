"""Tier 0 - health scan. Fast, non-disruptive, catches real faults."""
from __future__ import annotations

import json

from ..core import PROBE_NAME, Fail, Info, Ok, Skip, Warn, check

# Bit meanings from Documentation/admin-guide/tainted-kernels.rst. Only some
# indicate a fault; an out-of-tree module (e.g. a DKMS driver) is expected.
TAINT_BITS = {
    0: ("G/P", "proprietary module", False),
    1: ("F", "module force-loaded", False),
    2: ("S", "SMP with unsupported CPU", True),
    3: ("R", "module force-unloaded", False),
    4: ("M", "machine check exception", True),
    5: ("B", "bad page detected", True),
    6: ("U", "user-requested taint", False),
    7: ("D", "kernel died (oops/BUG)", True),
    9: ("W", "warning issued", True),
    12: ("O", "out-of-tree module", False),
    13: ("E", "unsigned module", False),
}


@check(tier=0, name="taint", desc="kernel taint flags")
def taint(ctx):
    raw = ctx.read("/proc/sys/kernel/tainted", "0")
    val = int(raw or 0)
    if val == 0:
        return Ok("kernel not tainted", taint=0)
    set_bits = [(l, d, bad) for b, (l, d, bad) in TAINT_BITS.items() if val & (1 << b)]
    desc = ", ".join(f"{l}={d}" for l, d, _ in set_bits) or f"raw {val}"
    if any(bad for _, _, bad in set_bits):
        return Fail(f"tainted ({desc}) - indicates a real fault", taint=val)
    return Warn(f"tainted ({desc}) - benign", taint=val)


@check(tier=0, name="oops", desc="oops / BUG / panic / call traces")
def oops(ctx):
    n = ctx.count_matches(
        ctx.journal_kernel,
        r"Oops|BUG:|kernel panic|Call Trace|general protection fault")
    if n == 0:
        return Ok("no oops/BUG/panic/call-trace", oops_count=0)
    return Fail(f"{n} fault line(s) - see: journalctl -b 0 -k", oops_count=n)


@check(tier=0, name="warnings", desc="kernel WARN_ON / WARNING")
def warnings(ctx):
    n = ctx.count_matches(ctx.journal_kernel, r"WARNING: CPU|WARN_ON")
    if n == 0:
        return Ok("no kernel WARNINGs", warn_count=0)
    return Warn(f"{n} kernel WARNING(s) - inspect", warn_count=n)


@check(tier=0, name="mce", desc="machine check exceptions / hardware errors")
def mce(ctx):
    n = ctx.count_matches(ctx.journal_kernel, r"machine check|mce:|Hardware Error")
    if n == 0:
        return Ok("no machine-check exceptions", mce_count=0)
    return Fail(f"{n} MCE/hardware-error line(s) - suspect hardware", mce_count=n)


@check(tier=0, name="failed_units", desc="failed systemd units")
def failed_units(ctx):
    out = ctx.run(["systemctl", "--failed", "--no-legend"]).stdout.strip()
    names = [l.split()[0] for l in out.splitlines() if l.strip()]
    if not names:
        return Ok("no failed systemd units", failed_units=0)
    return Fail(f"{len(names)} failed: {' '.join(names)}", failed_units=len(names))


@check(tier=0, name="probe_failures", desc="driver probe failures")
def probe_failures(ctx):
    n = ctx.count_matches(
        ctx.journal_kernel,
        r"probe with driver .* failed|failed to initialize|driver failed to")
    if n == 0:
        return Ok("no driver probe failures", probe_failures=0)
    return Fail(f"{n} driver probe failure(s)", probe_failures=n)


@check(tier=0, name="firmware", desc="missing firmware notices")
def firmware(ctx):
    n = ctx.count_matches(ctx.journal_kernel, r"Possibly missing firmware")
    if n == 0:
        return Ok("no missing-firmware notices", missing_firmware=0)
    # Common and usually harmless: firmware for hardware this box lacks.
    return Info(f"{n} missing-firmware notice(s) - usually harmless",
                missing_firmware=n)


@check(tier=0, name="modules", desc="loaded module count")
def modules(ctx):
    out = ctx.run(["lsmod"]).stdout.splitlines()
    n = max(0, len(out) - 1)
    return Info(f"{n} modules loaded", modules_loaded=n)


@check(tier=0, name="boot_time", desc="boot timing breakdown")
def boot_time(ctx):
    out = ctx.run(["systemd-analyze"], timeout=30).stdout
    if "=" not in out:
        return Info("systemd-analyze unavailable")
    import re
    m = {}
    for part, key in (("kernel", "boot_kernel_ms"), ("initrd", "boot_initrd_ms"),
                      ("userspace", "boot_userspace_ms")):
        mt = re.search(rf"([\d.]+)s\s*\({part}\)", out)
        if mt:
            m[key] = round(float(mt.group(1)) * 1000)
    # A long initrd usually means it waited for a passphrase rather than
    # unlocking from the TPM - useful signal on an encrypted box.
    msg = out.strip().splitlines()[-1] if out.strip() else "n/a"
    return Info(msg[:90], **m)


@check(tier=0, name="journal_errors", desc="error-priority log lines this boot")
def journal_errors(ctx):
    """The userspace half of the log, which the kernel greps above cannot see.

    `failed_units` catches a unit that gave up; this catches one erroring
    loudly while still running. Counts are reported by source rather than by
    message, because journal text carries usernames and paths and these
    reports get committed.

    WARN, never FAIL: a couple of error-priority lines is normal on a healthy
    machine - firmware capability notices ("SGX disabled by BIOS") are logged
    at that level and mean nothing is wrong.
    """
    r = ctx.run(["journalctl", "-b", "0", "-p", "err", "--no-pager", "-q",
                 "-o", "json", "--output-fields=SYSLOG_IDENTIFIER,MESSAGE"],
                timeout=120)
    if r.returncode != 0:
        return Skip("journalctl could not read the journal")
    sources: dict = {}
    for line in r.stdout.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("MESSAGE") or ""
        # stack_protector aborts a probe on purpose and systemd-coredump logs
        # it. The suite should not report its own noise as a machine fault.
        if isinstance(message, str) and PROBE_NAME in message:
            continue
        ident = entry.get("SYSLOG_IDENTIFIER") or "unknown"
        sources[ident] = sources.get(ident, 0) + 1
    total = sum(sources.values())
    if total == 0:
        return Ok("no error-priority log lines", journal_errors=0)
    top = ", ".join(f"{k}:{n}" for k, n in
                    sorted(sources.items(), key=lambda kv: -kv[1])[:4])
    return Warn(f"{total} error-priority line(s) - {top}", journal_errors=total)
