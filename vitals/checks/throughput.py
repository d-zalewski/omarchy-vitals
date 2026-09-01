"""Tier 5 - kernel throughput benchmarks.

Deliberately narrow: these measure *kernel* code paths, not hardware. A general
CPU benchmark (Geekbench and friends) mostly exercises userspace SIMD and
compression, barely touches the scheduler, and on a passively-cooled box its
thermal variance swamps any kernel-attributable difference - so it tells you
almost nothing when comparing two kernels on one machine.

What does move between kernels: context-switch cost, syscall entry/exit,
scheduling under contention, and the network stack. Those are what this tier
measures.

A caveat that matters for A/B work: `perf` is version-tied to the kernel it
ships with. Running Arch's perf against a different kernel usually works but
can silently report nonsense, so every perf check records the version pairing
and degrades to SKIP rather than contributing a bogus number to a comparison.
"""
from __future__ import annotations

import os
import platform
import re

from ..core import Fail, Info, Ok, Skip, Warn, check


def _perf_version(ctx) -> str | None:
    r = ctx.run(["perf", "--version"], timeout=30)
    m = re.search(r"perf version (\S+)", r.stdout)
    return m.group(1) if m else None


def _perf_usable(ctx) -> tuple[bool, str]:
    """perf's major.minor should match the running kernel."""
    pv = _perf_version(ctx)
    if not pv:
        return False, "perf version unreadable"
    krel = platform.release()
    pmm = ".".join(pv.split(".")[:2])
    kmm = ".".join(krel.split(".")[:2])
    if pmm != kmm:
        return False, f"perf {pv} vs kernel {krel} - version mismatch, skipping"
    return True, pv


def _num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


@check(tier=5, name="perf_sched_pipe", desc="context-switch latency",
       requires=["perf"], est_seconds=20)
def perf_sched_pipe(ctx):
    """Two processes ping-ponging through a pipe: the purest context-switch cost."""
    usable, why = _perf_usable(ctx)
    if not usable:
        return Skip(why)
    r = ctx.run(["perf", "bench", "sched", "pipe", "-l", "50000"], timeout=300)
    out = r.stdout + r.stderr
    usecs = _num(r"([\d.]+)\s+usecs/op", out)
    ops = _num(r"(\d+)\s+ops/sec", out)
    if usecs is None:
        return Warn(f"perf bench produced no result: {out.strip()[:70]}")
    m = {"ctxsw_usecs_op": round(usecs, 3)}
    if ops:
        m["ctxsw_ops_sec"] = int(ops)
    return Ok(f"{usecs:.2f} us/switch, {int(ops or 0):,} ops/sec", **m)


@check(tier=5, name="perf_sched_messaging", desc="multi-process scheduling",
       requires=["perf"], est_seconds=30)
def perf_sched_messaging(ctx):
    """Many task groups passing messages - scheduler under fan-out."""
    usable, why = _perf_usable(ctx)
    if not usable:
        return Skip(why)
    r = ctx.run(["perf", "bench", "sched", "messaging", "-g", "20", "-l", "200"],
                timeout=300)
    out = r.stdout + r.stderr
    secs = _num(r"Total time:\s*([\d.]+)", out)
    if secs is None:
        return Warn(f"no result: {out.strip()[:70]}")
    return Ok(f"{secs:.3f}s for 20 groups", sched_messaging_sec=round(secs, 3))


@check(tier=5, name="perf_syscall", desc="syscall entry/exit cost",
       requires=["perf"], est_seconds=20)
def perf_syscall(ctx):
    """Syscall overhead - moves with CPU mitigations and entry-path changes."""
    usable, why = _perf_usable(ctx)
    if not usable:
        return Skip(why)
    r = ctx.run(["perf", "bench", "syscall", "basic", "-l", "1000000"], timeout=300)
    out = r.stdout + r.stderr
    usecs = _num(r"([\d.]+)\s+usecs/op", out)
    ops = _num(r"(\d+)\s+ops/sec", out)
    if usecs is None:
        # 'syscall' subsystem is newer; older perf builds lack it.
        return Skip("perf bench syscall unavailable in this build")
    m = {"syscall_usecs_op": round(usecs, 4)}
    if ops:
        m["syscall_ops_sec"] = int(ops)
    return Ok(f"{usecs:.4f} us/syscall, {int(ops or 0):,} ops/sec", **m)


@check(tier=5, name="perf_mem", desc="memory bandwidth (kernel memcpy paths)",
       requires=["perf"], est_seconds=25)
def perf_mem(ctx):
    usable, why = _perf_usable(ctx)
    if not usable:
        return Skip(why)
    r = ctx.run(["perf", "bench", "mem", "memcpy", "-s", "64MB", "-l", "5"],
                timeout=300)
    out = r.stdout + r.stderr
    # Output lists several implementations; take the best GB/sec seen.
    rates = [float(x) for x in re.findall(r"([\d.]+)\s*GB/sec", out)]
    if not rates:
        return Skip("perf bench mem produced no rate")
    best = max(rates)
    return Ok(f"{best:.2f} GB/sec peak memcpy", memcpy_gb_sec=round(best, 2))


@check(tier=5, name="sysbench_threads", desc="scheduling under lock contention",
       requires=["sysbench"], est_seconds=40)
def sysbench_threads(ctx):
    """Threads fighting over mutexes - where scheduler policy shows up most."""
    ncpu = os.cpu_count() or 1
    r = ctx.run(["sysbench", "threads", f"--threads={ncpu * 2}",
                 "--thread-locks=4", "--time=20", "run"], timeout=180)
    out = r.stdout
    events = _num(r"total number of events:\s*(\d+)", out)
    lat95 = _num(r"95th percentile:\s*([\d.]+)", out)
    if events is None:
        return Warn(f"sysbench produced no result: {out.strip()[:70]}")
    m = {"sysbench_threads_events": int(events)}
    if lat95 is not None:
        m["sysbench_threads_p95_ms"] = round(lat95, 2)
    tail = f", p95 {lat95:.2f}ms" if lat95 is not None else ""
    return Ok(f"{int(events):,} events in 20s{tail}", **m)


@check(tier=5, name="sysbench_cpu", desc="CPU compute reference point",
       requires=["sysbench"], est_seconds=25)
def sysbench_cpu(ctx):
    """Not kernel-sensitive - included as a control.

    If this moves between two kernels on the same machine, the difference is
    thermal or clock variance, not the kernel, and the other tier 5 numbers
    should be read with that in mind.
    """
    ncpu = os.cpu_count() or 1
    r = ctx.run(["sysbench", "cpu", f"--threads={ncpu}", "--cpu-max-prime=20000",
                 "--time=15", "run"], timeout=180)
    eps = _num(r"events per second:\s*([\d.]+)", r.stdout)
    if eps is None:
        return Warn("sysbench cpu produced no result")
    return Ok(f"{eps:,.0f} events/sec (control - should not vary by kernel)",
              sysbench_cpu_eps=round(eps, 1))


@check(tier=5, name="iperf_loopback", desc="network stack throughput",
       requires=["iperf3"], est_seconds=30)
def iperf_loopback(ctx):
    """Loopback keeps the NIC out of it, isolating the kernel network path."""
    import subprocess
    import time
    srv = subprocess.Popen(["iperf3", "-s", "-1", "-p", "5299"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1)
        r = ctx.run(["iperf3", "-c", "127.0.0.1", "-p", "5299", "-t", "10",
                     "-J"], timeout=60)
        import json
        try:
            data = json.loads(r.stdout)
            bps = data["end"]["sum_received"]["bits_per_second"]
        except Exception:                              # noqa: BLE001
            return Warn("iperf3 output not parseable")
        gbps = bps / 1e9
        return Ok(f"{gbps:.2f} Gbit/s loopback", loopback_gbit_s=round(gbps, 2))
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:                              # noqa: BLE001
            srv.kill()


@check(tier=5, name="stress_throughput", desc="stress-ng aggregate bogo-ops",
       requires=["stress-ng"], disruptive=True, est_seconds=70)
def stress_throughput(ctx):
    """stress-ng's own throughput counters.

    stress-ng documents bogo-ops as explicitly *not* a benchmark: they are not
    comparable across stress-ng versions or between stressor mixes. Within one
    version on one machine they still carry relative signal, which is the only
    way they are used here - so the version is recorded alongside the number.
    """
    ncpu = os.cpu_count() or 1
    ver = ctx.run(["stress-ng", "--version"]).stdout.strip()
    vm = re.search(r"version ([\d.]+)", ver)
    r = ctx.run(["stress-ng", "--cpu", str(ncpu), "--timeout", "60s",
                 "--metrics-brief"], timeout=200)
    out = r.stdout + r.stderr
    # Final column layout varies by version; take the cpu stressor's bogo-ops.
    m = re.search(r"^\s*\S*\s*cpu\s+(\d+)", out, re.MULTILINE)
    if not m:
        m = re.search(r"cpu\s+(\d+)\s+\d+\.\d+", out)
    if not m:
        return Warn("bogo-ops not parseable from stress-ng output")
    bogo = int(m.group(1))
    return Ok(f"{bogo:,} bogo-ops/60s (stress-ng {vm.group(1) if vm else '?'};"
              f" comparable only within this version)",
              stress_bogo_ops=bogo)
