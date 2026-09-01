# omarchy-vitals

Health and performance checks for Omarchy desktops. Run it after a kernel deploy
or system update to confirm the hardware still works and the machine still feels
responsive — then diff two kernels to see what changed.

Python standard library only. Clone and run.

```bash
./omarchy-vitals.py --tier 0,1     # health + desktop hardware      ~4 min
./omarchy-vitals.py --tier 2       # scheduling latency / jitter   ~12 min
./omarchy-vitals.py --tier 5       # kernel throughput              ~4 min
./omarchy-vitals.py --list         # every check, by tier
```

## Tiers

| Tier | Covers | Time |
|---|---|---|
| 0 | kernel faults: taint, oops, MCE, failed units, boot timing | 30s |
| 1 | hardware: GPU, audio, network, USB, input, storage, thermal | 4m |
| 2 | latency: `cyclictest` idle and under load, `hackbench` | 12m |
| 3 | stress: sustained load, thermal peak, disk I/O | 20m |
| 4 | suspend/resume: S3 cycles, devices verified afterwards | 3m |
| 5 | throughput: context switch, syscall, contention, memcpy | 4m |

Tiers 3–4 load or suspend the machine; `--skip-disruptive` omits them.

## Comparing kernels

```bash
./omarchy-vitals.py --tier 0,1,2              # on kernel A
# reboot into kernel B, run again, then:
./omarchy-vitals.py compare A.json B.json
```

Reports are JSON named after the running kernel. The comparison knows which
direction each metric should move, so it reports `REGRESSION` rather than a bare
delta, and flags checks that flipped `PASS → FAIL`. Changes inside ±10 % are
called `~same`.

An [example comparison report](report/kernel-comparison.html) is included.

## Extending it

Scaffold a check and its tests in one command — the generated tests cover every
branch, so the suite passes immediately and stays at 100 %:

```bash
./new-check.py --module network --name wifi_regdomain \
               --desc "regulatory domain is set" --requires iw --write
```

[CONTRIBUTING.md](CONTRIBUTING.md) walks through a complete example. The rest of
this section is the shape it generates.

A check is one decorated function. Keyword arguments become metrics that
`compare` can diff. Drop it in `vitals/checks/<area>.py` — modules are imported
in `omarchy-vitals.py`.

**Reading a sysfs value:**

```python
from ..core import check, Ok, Warn, Skip

@check(tier=1, name="fan", desc="fan is spinning")
def fan(ctx):
    if not ctx.path_exists("/sys/class/hwmon/hwmon0/fan1_input"):
        return Skip("no fan sensor")                    # absent -> Skip
    rpm = int(ctx.read("/sys/class/hwmon/hwmon0/fan1_input", "0"))
    if rpm == 0:
        return Warn("fan reporting 0 RPM", fan_rpm=0)
    return Ok(f"{rpm} RPM", fan_rpm=rpm)
```

**Grepping the kernel log** (cached, read once per run):

```python
@check(tier=0, name="thermal_throttle", desc="CPU thermal throttling")
def thermal_throttle(ctx):
    n = ctx.count_matches(ctx.journal_kernel, r"package temperature above threshold")
    if n:
        return Warn(f"{n} throttle events", throttle_events=n)
    return Ok("no throttling", throttle_events=0)
```

**Running a tool that may be missing** — `requires` makes it Skip cleanly:

```python
@check(tier=1, name="nvme_temp", desc="NVMe drive temperature",
       requires=["nvme"])
def nvme_temp(ctx):
    r = ctx.sudo(["nvme", "smart-log", "/dev/nvme0"])
    if r.returncode != 0:
        return Skip("no NVMe device")
    ...
```

**Querying the desktop session** — works over SSH, the compositor's environment
is attached automatically:

```python
@check(tier=1, name="workspaces", desc="compositor responds")
def workspaces(ctx):
    r = ctx.run_in_session(["hyprctl", "-j", "workspaces"])
    if r.returncode != 0:
        return Skip("no Hyprland session")
    return Ok(f"{len(json.loads(r.stdout))} workspaces")
```

**A benchmark** — add the metric to `LOWER_IS_BETTER` or `HIGHER_IS_BETTER` in
`vitals/core.py` so comparisons can judge direction, and mark it `disruptive`
if it loads the machine:

```python
@check(tier=5, name="fs_create", desc="file creation rate",
       disruptive=True, est_seconds=30)
def fs_create(ctx):
    ...
    return Ok(f"{rate:.0f} files/sec", fs_create_rate=round(rate))
```

`ctx` provides `run`, `sudo`, `run_in_session`, `have`, `read`, `path_exists`,
`count_matches`, `config_is_set`, and cached `journal_kernel` / `journal_all` /
`kconfig`.

Two rules worth following: **absent hardware returns `Skip`, never `Fail`** — a
machine without wifi failing wifi checks makes a suite people ignore — and
**reports should carry no identifying data**, since they get shared and
committed.

## Tests

258 tests, 100 % coverage, no hardware or network access — they run anywhere in
under a second.

```bash
./run-tests.sh              # everything, with the 100 % coverage gate
./run-tests.sh gpu          # just tests matching "gpu"
./run-tests.sh --setup      # create .venv with coverage (optional, one-off)
```

## Optional tools

Checks skip cleanly when absent; coverage improves with:

```bash
sudo pacman -S rt-tests stress-ng fio bpftrace usbutils smartmontools \
               mesa-utils libva-utils sysbench perf iperf3
```

`rt-tests` is required for tier 2, `perf`/`sysbench` for tier 5.

## Notes

Latency thresholds follow perception rather than round numbers: under 1 ms is
smooth, 1–10 ms an occasional hitch, beyond that visible stutter. Worst case is
reported beside the average, because one 12 ms outlier is a dropped frame you see
while the average stays flat.

Results so far come from a single x86 desktop. Contributions welcome, especially
checks for hardware not covered yet — NVIDIA and AMD graphics, wifi, bluetooth,
laptop suspend and battery.

## License

MIT. See [LICENSE](LICENSE).

Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) · Agent usage notes: [AGENTS.md](AGENTS.md).
