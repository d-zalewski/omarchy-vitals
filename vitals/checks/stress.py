"""Tiers 3 and 4 - stress, thermal, and suspend/resume.

Both tiers are disruptive: tier 3 makes the desktop sluggish for its duration,
tier 4 actually suspends the machine. Suspend/resume is the single most common
place a new kernel breaks desktop hardware - the GPU or a NIC does not come
back - so it earns its own tier despite the inconvenience.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from ..core import (Fail, Info, Ok, Skip, Warn, check, disk_dir,
                    sudo_refused)

# Bound once at import rather than probed at call time, so the same lines run
# on a machine that has no CLOCK_BOOTTIME and the coverage gate stays
# platform-independent.
CLOCK_BOOTTIME = getattr(time, "CLOCK_BOOTTIME", None)


def _suspend(ctx, seconds: int) -> tuple[bool, str, bool]:
    """One rtcwake cycle: (suspended, error, sudo refused). Never raises.

    The refusal is reported separately because it is not a finding about the
    machine. Without passwordless sudo the cycle never happened, so every
    check built on this one has nothing to say rather than something bad.

    The sleep afterwards is not padding: devices re-probe asynchronously on
    resume, and inspecting them immediately reports a NIC as missing that is
    two seconds from coming back.
    """
    r = ctx.sudo(["rtcwake", "-m", "mem", "-s", str(seconds)], timeout=180)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:70], sudo_refused(r)
    time.sleep(12)
    return True, "", False


@check(tier=3, name="stress_stability", desc="no faults under CPU/VM/IO load",
       requires=["stress-ng"], disruptive=True, est_seconds=1200)
def stress_stability(ctx):
    ncpu = os.cpu_count() or 1
    mins = ctx.stress_minutes
    before = ctx.count_matches(ctx.journal_kernel, r"Oops|BUG:|Call Trace")

    zones = sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
    peak = 0.0
    proc = subprocess.Popen(
        ["stress-ng", "--cpu", str(ncpu), "--vm", "2", "--vm-bytes", "512M",
         "--io", "2", "--matrix", str(ncpu), "--timeout", f"{mins}m",
         "--metrics-brief"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    while proc.poll() is None:
        for z in zones:
            try:
                peak = max(peak, int(z.read_text().strip()) / 1000.0)
            except Exception:                          # noqa: BLE001
                pass
        time.sleep(10)
    out = proc.stdout.read() if proc.stdout else ""

    # Re-read the journal: the cached copy predates the stress run.
    fresh = ctx.run(["journalctl", "-b", "0", "-k", "--no-pager"], timeout=120).stdout
    after = ctx.count_matches(fresh, r"Oops|BUG:|Call Trace")
    new_faults = after - before
    oom = ctx.count_matches(fresh, r"Out of memory")

    metrics = {"stress_new_oops": new_faults, "oom_events": oom}
    if peak:
        metrics["stress_peak_temp_c"] = round(peak, 1)

    if new_faults > 0:
        return Fail(f"{new_faults} new kernel fault(s) under load "
                    f"(peak {peak:.0f}C)", **metrics)
    if proc.returncode != 0:
        return Warn(f"stress-ng exited rc={proc.returncode} (peak {peak:.0f}C)",
                    **metrics)
    hot = " - thermal throttling likely" if peak >= 95 else ""
    return Ok(f"{mins}min load clean, peak {peak:.0f}C{hot}", **metrics)


def _fio(ctx, directory, name, rw):
    """Run one fio job and return its parsed result, or (None, error).

    JSON rather than the terse format: terse means counting semicolons to
    field 48 for write IOPS, which silently returns the wrong number when the
    format version changes.
    """
    r = ctx.run(["fio", f"--name={name}", f"--directory={directory}",
                 "--size=256M", f"--rw={rw}", "--bs=4k", "--numjobs=2",
                 "--runtime=60", "--time_based", "--group_reporting",
                 "--direct=1", "--output-format=json"], timeout=180)
    # fio names its files <job>.<jobnum>.<filenum>; the previous glob here did
    # not match what it creates, so the files were never cleaned up.
    for leftover in Path(directory).glob(f"{name}.*.*"):
        try:
            leftover.unlink()
        except OSError:
            pass
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()[:70]
    try:
        return json.loads(r.stdout)["jobs"][0], None
    except (ValueError, KeyError, IndexError) as exc:
        return None, f"could not parse fio output ({type(exc).__name__})"


@check(tier=3, name="disk_io", desc="sustained random read from real storage",
       requires=["fio"], disruptive=True, est_seconds=90)
def disk_io(ctx):
    directory, fstype = disk_dir(ctx)
    if directory is None:
        return Skip("no writable directory on real storage")
    job, err = _fio(ctx, directory, "vitals-read", "randread")
    if err:
        return Warn(f"fio failed: {err}")
    iops = int(job["read"]["iops"])
    return Ok(f"random-read {iops} IOPS on {fstype}", fio_randread_iops=iops)


@check(tier=3, name="disk_write", desc="sustained random write from real storage",
       requires=["fio"], disruptive=True, est_seconds=90)
def disk_write(ctx):
    """The write path fails differently from the read path.

    Reads can be served from cache; writes go through the filesystem, dm-crypt
    and the device's own write path, which is where a regression in any of
    them shows up.
    """
    directory, fstype = disk_dir(ctx)
    if directory is None:
        return Skip("no writable directory on real storage")
    job, err = _fio(ctx, directory, "vitals-write", "randwrite")
    if err:
        return Warn(f"fio failed: {err}")
    write = job["write"]
    iops = int(write["iops"])
    lat_us = round(write["lat_ns"]["mean"] / 1000, 1)
    return Ok(f"random-write {iops} IOPS, {lat_us:.0f}us mean latency on {fstype}",
              fio_randwrite_iops=iops, fio_randwrite_lat_us=lat_us)


@check(tier=4, name="suspend_resume", desc="S3 suspend/resume, hardware returns",
       requires=["rtcwake"], disruptive=True, est_seconds=180)
def suspend_resume(ctx):
    states = ctx.read("/sys/power/state", "")
    if "mem" not in states:
        return Skip(f"S3 not supported (states: {states or 'none'})")

    def snapshot():
        """Device inventory, tolerant of any of these paths being absent.

        Called again after resume, when a subsystem that failed to come back
        may have taken its whole sysfs directory with it - so this must not
        raise, or the check reports a crash instead of the missing device.
        """
        def names(path, pattern=None):
            p = Path(path)
            if not p.is_dir():
                return []
            try:
                entries = p.glob(pattern) if pattern else p.iterdir()
                return sorted(e.name for e in entries if e.name != "lo")
            except OSError:
                return []
        return names("/sys/class/net"), names("/dev/dri", "card*"), \
            names("/proc/asound", "card*")

    before = snapshot()
    cycles = 2
    for i in range(1, cycles + 1):
        ok, err, refused = _suspend(ctx, 20)
        if refused:
            return Skip("rtcwake needs passwordless sudo to suspend")
        if not ok:
            return Fail(f"suspend cycle {i} failed: {err}")

    after = snapshot()
    lost = []
    for label, b, a in zip(("NIC", "DRM card", "sound card"), before, after):
        missing = set(b) - set(a)
        if missing:
            lost.append(f"{label}(s) gone after resume: {', '.join(sorted(missing))}")

    fresh = ctx.run(["journalctl", "-b", "0", "-k", "--no-pager"], timeout=120).stdout
    errs = ctx.count_matches(
        fresh, r"PM: .*(fail|error)|suspend.*(abort|fail)|resume.*fail|"
               r"Failed to suspend")

    if lost:
        return Fail(f"{cycles} cycle(s): " + "; ".join(lost), resume_errors=errs)
    if errs:
        return Warn(f"{cycles} cycle(s) survived but {errs} PM error line(s)",
                    resume_errors=errs)
    return Ok(f"{cycles} suspend/resume cycle(s) clean, all devices returned",
              resume_errors=0)


def _functional_probes(ctx):
    """Probes that answer the same question before and after a suspend.

    Each is a yes/no that costs a second or two and covers a subsystem that
    comes back wrong rather than not at all. Nothing they touch is recorded -
    that DNS resolved, not what it resolved to.
    """
    def dns():
        return ctx.run(["getent", "hosts", "archlinux.org"],
                       timeout=20).returncode == 0

    def render_node():
        # Opened, not counted: a node that is still listed after a resume and
        # will not open is precisely the case a name comparison misses.
        dri = Path("/dev/dri")
        for node in (sorted(dri.glob("renderD*")) if dri.is_dir() else []):
            try:
                fd = os.open(str(node), os.O_RDWR)
            except OSError:
                continue
            os.close(fd)
            return True
        return False

    def audio_server():
        return ctx.run_in_session(["wpctl", "status"], timeout=20).returncode == 0

    return (("DNS", dns), ("GPU render node", render_node),
            ("audio server", audio_server))


@check(tier=4, name="resume_functional", desc="subsystems still work after resume",
       requires=["rtcwake"], disruptive=True, est_seconds=90)
def resume_functional(ctx):
    """suspend_resume compares device names; this re-runs the probes.

    A NIC that comes back as a node but not as a working link passes a name
    comparison, and so does a GPU whose render node exists and whose driver is
    wedged. Only probes that worked *before* the cycle are judged afterwards,
    so a machine with no network does not fail for something that was never up.
    """
    states = ctx.read("/sys/power/state", "")
    if "mem" not in states:
        return Skip(f"S3 not supported (states: {states or 'none'})")
    probes = _functional_probes(ctx)
    before = {name: fn() for name, fn in probes}
    working = [n for n, ok in before.items() if ok]
    if not working:
        return Skip("none of the probes worked before suspending - "
                    "nothing to prove")
    ok, err, _refused = _suspend(ctx, 20)
    if not ok:
        return Skip(f"suspend did not complete: {err} - see suspend_resume")
    broken = [name for name, fn in probes if before[name] and not fn()]
    if broken:
        return Fail(f"worked before the cycle, not after: {', '.join(broken)}",
                    resume_broken=len(broken))
    return Ok(f"{len(working)} probe(s) still working after resume: "
              f"{', '.join(working)}", resume_broken=0)


@check(tier=4, name="clock_after_resume", desc="timekeeping survives suspend",
       requires=["rtcwake"], disruptive=True, est_seconds=60)
def clock_after_resume(ctx):
    """Two clocks, and the gap between them.

    CLOCK_BOOTTIME counts time spent suspended and CLOCK_MONOTONIC does not,
    so their difference is what the kernel believes it slept for. If that gap
    goes missing, every timer and timeout that survived the suspend is wrong by
    the length of it - a regression nothing else here would see, because the
    machine comes back looking perfectly healthy.
    """
    if CLOCK_BOOTTIME is None:
        return Skip("CLOCK_BOOTTIME not available on this platform")
    states = ctx.read("/sys/power/state", "")
    if "mem" not in states:
        return Skip(f"S3 not supported (states: {states or 'none'})")
    requested = 20
    boot0 = time.clock_gettime(CLOCK_BOOTTIME)
    mono0, wall0 = time.monotonic(), time.time()
    ok, err, _refused = _suspend(ctx, requested)
    if not ok:
        return Skip(f"suspend did not complete: {err} - see suspend_resume")
    boot_d = time.clock_gettime(CLOCK_BOOTTIME) - boot0
    mono_d, wall_d = time.monotonic() - mono0, time.time() - wall0
    slept = boot_d - mono_d
    skew_ms = round(abs(wall_d - boot_d) * 1000)
    metrics = {"clock_resume_skew_ms": skew_ms}
    if slept < requested / 2:
        return Fail(f"CLOCK_BOOTTIME gained only {slept:.0f}s across a "
                    f"{requested}s suspend - suspended time is not being "
                    f"accounted for", **metrics)
    if skew_ms > 2000:
        # A machine whose RTC was wrong gets stepped by NTP moments after
        # resume, which is indistinguishable from here and is a fix rather
        # than a fault - hence a warning.
        return Warn(f"wall clock and boot clock disagree by {skew_ms / 1000:.1f}s "
                    f"across the suspend - an NTP step, or the RTC did not "
                    f"restore", **metrics)
    return Ok(f"slept {slept:.0f}s of {requested}s requested, clocks agree "
              f"within {skew_ms}ms", **metrics)
