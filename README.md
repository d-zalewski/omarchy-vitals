# omarchy-vitals

Health and performance checks for **Omarchy desktop machines** — run after a
kernel deploy, a system update, or any change you want to prove didn't break
the machine you actually use.

It answers the two questions a desktop user cares about:

1. **Does the hardware still work?** GPU and displays, sound, ethernet/wifi/
   bluetooth, USB, input devices, storage, thermals.
2. **Does it still feel smooth?** Scheduling latency measured idle *and* under
   load — the thing you notice as stutter, dropped frames, or audio crackle.

Runs on the target machine. Python standard library only, no pip dependencies.

```bash
./omarchy-vitals.py --tier 0,1          # health + desktop hardware   (~4 min)
./omarchy-vitals.py --tier 2            # latency / jitter            (~12 min)
./omarchy-vitals.py --tier 3,4          # stress + suspend/resume     (~25 min)
./omarchy-vitals.py --tier 5            # kernel throughput benchmarks (~4 min)
./omarchy-vitals.py --list              # every check, by tier
```

## What it found

Pointed at Omarchy's own BORE kernel, on an Intel Celeron J4125 mini PC. Three
configurations, with a deliberately kernel-insensitive control benchmark landing
within **0.03 %** across all of them — so the machine was in an identical state
and the differences below are the kernel.

| | Arch stock 7.2.2 | custom, BORE off | custom, BORE on |
|---|---|---|---|
| context switch | 6.75 µs | 7.73 µs | **5.23 µs** |
| hackbench | 3.95 s | 4.76 s | **3.38 s** |
| mutex contention | **29,237 ev** | 24,945 ev | 23,038 ev |
| cyclictest loaded | 971 µs | 995 µs | 1036 µs |

**BORE is what makes task switching fast.** On the identical binary, toggling
`kernel.sched_bore` moves context-switch cost 32 % and `hackbench` 29 %. It costs
about 8 % under sustained mutex contention — a database workload, not a desktop.

**Timer latency is flat across all three.** `cyclictest` measures RT-priority timer
wakeups, which BORE does not govern. A suite that only ran `cyclictest` would have
concluded the scheduler does nothing — which is why both measurements are in here.

**Two theories tested and discarded along the way.** The contention gap was first
blamed on BORE (it is ~8 % of it, not all), then on `RCU_NOCB_CPU_DEFAULT_ALL`, the
only scheduler-relevant config difference from Arch. Booting with `rcu_nocbs=0` showed
that one backwards: contention barely moved, but context-switch latency got **69 %
worse**, because RCU softirq work then runs on the CPUs doing the actual work. RCU
offloading is a latency benefit. The remaining gap is still unexplained, and the tool
says so rather than offering a third guess.


## Tiers

| Tier | Covers | Time |
|---|---|---|
| 0 | kernel faults: taint, oops, MCE, failed units, probe failures, boot timing | 30s |
| 1 | **desktop hardware**: GPU/display, audio, network, USB, input, storage, thermal, toolchain | 4m |
| 2 | **latency/jitter**: `cyclictest` idle + under load, `hackbench`, scheduler state | 12m |
| 3 | stress: sustained CPU/VM/IO load, thermal peak, disk I/O | 20m |
| 4 | suspend/resume: S3 cycles, verifies GPU/NIC/audio come back | 3m |
| 5 | **throughput**: context switch, syscall, scheduler contention, memcpy, loopback | 4m |

Tiers 3–4 are disruptive (heavy load, suspends the machine). `--skip-disruptive`
omits those and the audible speaker test.

## Comparing two kernels

A single run tells you whether anything is broken. Comparing two runs tells you
whether anything got *worse* — usually the real question.

```bash
./omarchy-vitals.py --tier 0,1,2                     # on kernel A
# reboot into kernel B, run again, then:
./omarchy-vitals.py compare reports/A.json reports/B.json
```

Reports are JSON named after the running kernel. The comparison is
**direction-aware** — it knows latency should fall and IOPS should rise — so it
reports `REGRESSION` rather than a bare delta, and flags checks that flipped
`PASS → FAIL`. Changes under ±10% are treated as noise (`--tolerance` to adjust).

## Design notes

**Absent hardware skips; broken hardware fails.** A four-NIC mini PC with no
radio must not fail wifi checks, and a laptop with no ethernet port must not
fail wired ones. `SKIP` means "not applicable here"; `FAIL` means "this machine
has it and it's broken". Getting this wrong produces a suite people learn to
ignore.

**Latency thresholds are tied to perception**, not round numbers. On a `PREEMPT`
(non-RT) desktop kernel: `<1 ms` smooth, `1–10 ms` occasional perceptible hitch,
`>10 ms` visible stutter. Worst case is reported next to average because one
12 ms outlier is a dropped frame you *see*, while the average stays flat and
reassuring.

**Software rendering counts as a failure.** If the GPU driver binds but clients
fall back to `llvmpipe`, the desktop still "works" — slowly, and hot. That
silent degradation is worse than an obvious break, so `gpu_accel` fails on it.

**Checks run over SSH still see the desktop.** A remote shell has no
`WAYLAND_DISPLAY` or `HYPRLAND_INSTANCE_SIGNATURE`, so `hyprctl` and `wpctl`
would fail misleadingly. `Context.session_env()` borrows the compositor's own
environment, recovering Hyprland's instance signature from
`$XDG_RUNTIME_DIR/hypr/` — Hyprland sets it for children but doesn't keep it in
its own `environ`.

**Some checks target custom-built kernels.** If you build your own kernels with
a self-compiled toolchain, the risks are toolchain risks:

- `stack_protector` — a cross-gcc built `--without-headers` defaults the stack
  guard to a global symbol instead of the kernel's `%gs:40`. That is *silent
  memory corruption*, not a clean failure, so the check deliberately smashes a
  stack and requires `SIGABRT`.
- `vdso32` — covers multilib / 32-bit vDSO (`IA32_EMULATION`).
- `btf` / `bpftrace` — verifies eBPF actually attaches, not merely that BTF
  files exist.

## Guards that keep a comparison honest

**`perf` is version-locked to its kernel.** Arch ships only the current one, so
running it against an older kernel can silently report nonsense. Every `perf` check
compares versions and SKIPs on mismatch — a skipped row is more useful than a wrong
one in an A/B.

**`sysbench cpu` is included as a control**, precisely because it is *not*
kernel-sensitive. If it moves between two kernels on one machine, the difference is
thermal or clock drift and every other number should be discounted accordingly.

**`stress-ng` bogo-ops are labelled, not trusted.** stress-ng documents them as
explicitly not a benchmark, so the version is recorded alongside the number and they
are only ever compared within one version on one machine.

**`sched_bore` state is recorded as a metric.** BORE is a runtime toggle, so one
binary can be measured in two scheduler configurations; a report that does not say
which cannot be compared honestly.

**Reports carry no network topology.** Connectivity checks record that the gateway
answered, not its address — reports get shared and committed.

## Adding a check

One decorated function. Keyword arguments become metrics in the report and are
diffed by `compare`:

```python
# vitals/checks/graphics.py
from ..core import check, Ok, Fail, Skip

@check(tier=1, name="my_check", desc="what it proves", requires=["some-tool"])
def my_check(ctx):
    if not ctx.path_exists("/sys/class/thing"):
        return Skip("hardware not present")            # not applicable here
    n = ctx.count_matches(ctx.journal_kernel, r"thing.*error")
    if n:
        return Fail(f"{n} errors", thing_errors=n)     # present but broken
    return Ok("thing healthy", thing_errors=0)
```

`ctx` provides `run`, `sudo`, `run_in_session`, `have`, `read`, `path_exists`,
`count_matches`, and cached `journal_kernel` / `journal_all` / `kconfig`. The
journal is read once per run, not once per check.

If a metric has a direction, add it to `LOWER_IS_BETTER` or `HIGHER_IS_BETTER`
in `vitals/core.py` so comparisons can identify regressions.

Drop a new module into `vitals/checks/` and import it in `omarchy-vitals.py`.

## Optional tools

Checks skip cleanly when a tool is missing, but coverage improves with:

```bash
sudo pacman -S rt-tests stress-ng fio bpftrace usbutils smartmontools \
               mesa-utils libva-utils
```

`rt-tests` (cyclictest, hackbench) is required for tier 2 — the jitter tier.
`mesa-utils` enables the software-rendering check.

## Status

Early but working. Verified on Omarchy running on an Intel Celeron J4125 mini PC
(i915 graphics, HDA audio, 4× Intel I225-V NICs): 38 pass, 9 skip, 0 fail.

Contributions welcome, particularly checks for hardware I can't test —
NVIDIA/AMD graphics, wifi, bluetooth, laptop suspend and battery.
