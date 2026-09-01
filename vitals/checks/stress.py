"""Tiers 3 and 4 - stress, thermal, and suspend/resume.

Both tiers are disruptive: tier 3 makes the desktop sluggish for its duration,
tier 4 actually suspends the machine. Suspend/resume is the single most common
place a new kernel breaks desktop hardware - the GPU or a NIC does not come
back - so it earns its own tier despite the inconvenience.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check


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


@check(tier=3, name="disk_io", desc="sustained random I/O", requires=["fio"],
       disruptive=True, est_seconds=90)
def disk_io(ctx):
    r = ctx.run(["fio", "--name=kt", "--directory=/tmp", "--size=256M",
                 "--rw=randread", "--bs=4k", "--numjobs=2", "--runtime=60",
                 "--time_based", "--group_reporting", "--output-format=terse"],
                timeout=180)
    for f in Path("/tmp").glob("ov.*.0"):
        try:
            f.unlink()
        except Exception:                              # noqa: BLE001
            pass
    if r.returncode != 0:
        return Warn(f"fio failed: {(r.stderr or r.stdout).strip()[:70]}")
    fields = r.stdout.strip().split(";")
    try:
        iops = int(float(fields[7]))
    except (IndexError, ValueError):
        return Info("fio ran but IOPS not parseable")
    return Ok(f"random-read {iops} IOPS", fio_randread_iops=iops)


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
        r = ctx.sudo(["rtcwake", "-m", "mem", "-s", "20"], timeout=180)
        if r.returncode != 0:
            return Fail(f"suspend cycle {i} failed: "
                        f"{(r.stderr or r.stdout).strip()[:70]}")
        time.sleep(12)  # let devices re-probe before inspecting

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
