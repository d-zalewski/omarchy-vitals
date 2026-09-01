"""Tier 2 - scheduling latency and jitter.

cyclictest measures how late a timer wakeup actually is versus when it was due,
which is precisely what "jitter" means on a desktop. The idle run is easy and
proves little; the run under load is the one that matters, because that is when
a scheduler either holds interactive tasks together or does not.

Thresholds are tied to human perception on a PREEMPT (non-RT) desktop kernel:
    < 1 ms   smooth
    1-10 ms  occasional perceptible hitch
    > 10 ms  visible stutter / dropped frames

Worst case matters more than average: one 12 ms outlier is a dropped frame you
see, while the average stays flat and reassuring.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess

from ..core import Fail, Info, Ok, Skip, Warn, check

SMOOTH_US = 1_000
HITCH_US = 10_000


def _parse_cyclictest(text: str) -> tuple[int | None, int | None]:
    """Return (worst avg across threads, worst max across threads) in us."""
    avgs = [int(m) for m in re.findall(r"Avg:\s*(\d+)", text)]
    maxs = [int(m) for m in re.findall(r"Max:\s*(\d+)", text)]
    return (max(avgs) if avgs else None, max(maxs) if maxs else None)


def _cyclictest(ctx, seconds: int) -> tuple[int | None, int | None, str]:
    ncpu = os.cpu_count() or 1
    cmd = ["cyclictest", "--mlockall", "--priority=80", "--interval=200",
           "--distance=0", f"--threads={ncpu}", f"--duration={seconds}", "--quiet"]
    r = ctx.sudo(cmd, timeout=seconds + 120)
    avg, mx = _parse_cyclictest(r.stdout)
    return avg, mx, (r.stderr or "")[:120]


def _grade(label: str, avg: int | None, mx: int | None, prefix: str):
    if avg is None or mx is None:
        return Fail(f"{label}: cyclictest produced no parseable result")
    metrics = {f"cyclictest_{prefix}_avg_us": avg, f"cyclictest_{prefix}_max_us": mx}
    msg = f"{label}: avg {avg}us, max {mx}us"
    if mx < SMOOTH_US:
        return Ok(f"{msg} - smooth", **metrics)
    if mx < HITCH_US:
        return Warn(f"{msg} - occasional perceptible hitch", **metrics)
    return Fail(f"{msg} - visible stutter territory", **metrics)


@check(tier=2, name="cyclictest_idle", desc="wakeup latency, idle system",
       requires=["cyclictest"], est_seconds=300)
def cyclictest_idle(ctx):
    secs = ctx.minutes * 60
    avg, mx, err = _cyclictest(ctx, secs)
    if avg is None and err:
        return Fail(f"cyclictest failed: {err}")
    return _grade("idle", avg, mx, "idle")


@check(tier=2, name="cyclictest_loaded", desc="wakeup latency under CPU/IO/VM load",
       requires=["cyclictest", "stress-ng"], disruptive=True, est_seconds=340)
def cyclictest_loaded(ctx):
    ncpu = os.cpu_count() or 1
    secs = ctx.minutes * 60
    load = subprocess.Popen(
        ["stress-ng", "--cpu", str(ncpu), "--io", "2", "--vm", "1",
         "--vm-bytes", "256M", "--timeout", f"{secs + 60}s"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        subprocess.run(["sleep", "5"], check=False)
        avg, mx, err = _cyclictest(ctx, secs)
    finally:
        try:
            os.killpg(os.getpgid(load.pid), signal.SIGTERM)
        except Exception:                              # noqa: BLE001
            load.terminate()
        try:
            load.wait(timeout=20)
        except Exception:                              # noqa: BLE001
            pass
    if avg is None and err:
        return Fail(f"cyclictest failed: {err}")
    return _grade("loaded", avg, mx, "loaded")


@check(tier=2, name="hackbench", desc="scheduler throughput, many short tasks",
       requires=["hackbench"], est_seconds=60)
def hackbench(ctx):
    r = ctx.run(["hackbench", "-pTl", "2000"], timeout=300)
    m = re.search(r"Time:\s*([\d.]+)", r.stdout)
    if not m:
        return Skip("hackbench produced no timing")
    t = float(m.group(1))
    # BORE optimises interactivity; a modest throughput cost here can be a fair
    # trade, so this is informational rather than pass/fail.
    return Info(f"hackbench {t}s (lower = more throughput)", hackbench_sec=t)


@check(tier=2, name="preempt_config", desc="preemption / tick configuration")
def preempt_config(ctx):
    if not ctx.kconfig:
        return Skip("/proc/config.gz unavailable")
    bits = []
    for opt, label in (("CONFIG_PREEMPT", "PREEMPT"),
                       ("CONFIG_PREEMPT_DYNAMIC", "PREEMPT_DYNAMIC"),
                       ("CONFIG_HIGH_RES_TIMERS", "HIGH_RES_TIMERS"),
                       ("CONFIG_NO_HZ_FULL", "NO_HZ_FULL"),
                       ("CONFIG_RCU_NOCB_CPU", "RCU_NOCB")):
        if ctx.config_is_set(opt):
            bits.append(label)
    mode = ctx.read("/sys/kernel/debug/sched/preempt", "")
    if not ctx.config_is_set("CONFIG_PREEMPT") and \
       not ctx.config_is_set("CONFIG_PREEMPT_DYNAMIC"):
        return Warn("kernel is not fully preemptible - higher desktop latency")
    return Ok(f"{', '.join(bits)}{(' | runtime: ' + mode) if mode else ''}")


@check(tier=2, name="sched_bore", desc="BORE scheduler active")
def sched_bore(ctx):
    """Record BORE state as a metric, not merely as a status.

    BORE is a runtime toggle, so one kernel can be measured in two scheduler
    configurations. A report that does not record which one it ran under cannot
    be compared honestly against another - and the difference is real: measured
    on this hardware, BORE costs roughly 7% throughput and 7% p95 under heavy
    mutex contention, targeting interactive responsiveness instead.
    """
    r = ctx.run(["sysctl", "-n", "kernel.sched_bore"])
    if r.returncode != 0:
        return Info("kernel.sched_bore not present (not a BORE kernel)",
                    sched_bore=-1)
    val = r.stdout.strip()
    if val == "0":
        return Warn("BORE compiled in but disabled (kernel.sched_bore=0)",
                    sched_bore=0)
    return Ok(f"BORE scheduler enabled (kernel.sched_bore={val})",
              sched_bore=int(val) if val.isdigit() else 1)
