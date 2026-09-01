# AGENTS.md

Instructions for AI agents operating this repository. Humans should read
[README.md](README.md) instead — this file assumes you can run commands and
want to know what to run, how to read the output, and what will bite you.

## What this is

A validation suite that runs **on an Omarchy/Arch desktop** and answers two
questions after a kernel deploy or system change: does the hardware still work,
and is the machine still responsive. It writes a JSON report per kernel so two
kernels can be diffed.

Pure Python standard library. No install step, no dependencies. Clone and run.

## Invocation

```bash
./omarchy-vitals.py --tier 0,1                    # health + hardware   ~4 min
./omarchy-vitals.py --tier 2 --minutes 5          # latency/jitter     ~12 min
./omarchy-vitals.py --tier 5                      # kernel throughput   ~4 min
./omarchy-vitals.py --tier 3,4                    # stress + suspend   ~25 min
./omarchy-vitals.py --list                        # enumerate checks
./omarchy-vitals.py --only gpu_driver,audio_sinks # run specific checks
./omarchy-vitals.py compare A.json B.json         # diff two reports
```

Exit code is `0` when nothing failed, `1` when any check FAILed. Reports land in
`--out` (default `./reports`) named `<kernel-release>.json`.

## Reading the output

Five statuses, and the distinction between two of them matters more than the rest:

| Status | Means | Act on it? |
|---|---|---|
| `PASS` | working | no |
| `FAIL` | **this machine has the hardware and it is broken** | yes |
| `SKIP` | **not applicable here** — no such hardware, or tool missing | no |
| `WARN` | degraded or suspicious, not broken | investigate |
| `INFO` | recorded for context, not a judgement | no |

Do not report `SKIP` as a problem. A four-NIC box with no radio skipping wifi
checks is correct behaviour, not missing coverage.

## Running it remotely

Checks are written to work over SSH, including ones that query the Wayland
session (`hyprctl`, `wpctl`) — the context borrows the compositor's environment
automatically. You do not need to be logged in graphically.

```bash
ssh user@host 'cd ~/omarchy-vitals && python3 omarchy-vitals.py --tier 0,1'
scp user@host:'~/omarchy-vitals/reports/*.json' ./
```

## Comparing kernels — the main workflow

A single run says whether something is broken. A comparison says whether
something got *worse*, which is usually the actual question.

1. Run `--tier 0,1,2` on kernel A, keep the JSON.
2. Reboot into kernel B (bootloader entry, not a rebuild).
3. Run the **same tiers with the same `--minutes`** — mismatched durations make
   latency numbers incomparable.
4. `./omarchy-vitals.py compare A.json B.json`

The comparison is direction-aware: it knows latency should fall and IOPS should
rise, so it prints `REGRESSION` rather than a bare delta, and flags checks that
flipped `PASS → FAIL`. Anything inside ±10 % is reported as `~same` (tune with
`--tolerance`).

**Always check the `sysbench_cpu_eps` control row first.** It is deliberately
*not* kernel-sensitive. If it moved more than ~1 % between runs, the machine was
in a different thermal or clock state and every other number in that comparison
is suspect. Say so rather than reporting the deltas as findings.

## Gotchas that will produce wrong conclusions

- **Check `kernel_current` before reading anything else.** It compares the
  running release against the installed module trees. If it WARNs, a newer
  kernel is installed and the machine has not rebooted - the report is named
  after the kernel that produced it, which is not the one you meant to test.
- **`secure_boot` never fails, by design.** Most machines run without it.
  It records a metric instead, so `compare` reports a 1 -> 0 flip as a
  regression on the machines where it was on, and says nothing on the rest.
- **Disk numbers before this change measured tmpfs.** `disk_io` used to
  run in /tmp, which is RAM on a systemd machine, so `fio_randread_iops`
  in any older report is a memcpy figure. Do not compare it against a
  new one.
- **`perf` is version-locked to its kernel.** Checks SKIP on mismatch by design.
  If you see four `perf_*` SKIPs, that is the guard working, not missing data —
  do not "fix" it by forcing them to run.
- **BORE is a runtime toggle** (`kernel.sched_bore`). The same binary behaves
  differently with it on or off, so the report records `sched_bore` as a metric.
  Check it matches expectations before attributing a difference to the kernel.
- **`cyclictest` measures RT-priority timer wakeups**, which schedulers like BORE
  do not govern. It being flat does not mean the scheduler is doing nothing —
  look at `ctxsw_usecs_op` and `hackbench_sec` for ordinary task scheduling.
- **`stress-ng` bogo-ops are not a benchmark** (stress-ng's own documentation
  says so). Only compare them within one stress-ng version on one machine.
- **One run per kernel cannot resolve a few percent.** If a difference matters,
  repeat it and report the spread. Several tiers take minutes; budget for it.

## Disruptive checks

Tiers 3 and 4 and the `audio_playback` check will affect a machine someone may
be using: heavy sustained load, an audible tone, and actual S3 suspend cycles.

- Pass `--skip-disruptive` when the machine is in use or is remote-only.
- **Never run tier 4 on a remote machine you cannot physically reach** unless
  suspend/resume is already known to work there. A failed resume needs a power
  button.
- Some checks need passwordless `sudo` (cyclictest, bpftrace, rtcwake,
  `efi_signatures`, `luks_tpm`, and `module_load`, which modprobes an inert
  module and unloads it again). They degrade to SKIP or WARN without it
  rather than failing.

## Optional tools

Checks skip cleanly when a tool is absent; coverage improves with:

```bash
sudo pacman -S rt-tests stress-ng fio bpftrace usbutils smartmontools \
               mesa-utils libva-utils sysbench perf iperf3 sbctl
```

`rt-tests` is required for tier 2 (the jitter tier). `perf` and `sysbench` are
required for tier 5.

## Tests — run them before and after any change

401 unit tests, **100 % line coverage**, no network or hardware access. They run
in under a second on any machine, including one that is not the target.

```bash
./run-tests.sh                 # everything, with the 100 % gate
./run-tests.sh gpu             # filter by test name (method or class)
./run-tests.sh --setup         # create .venv with coverage, one-off
./run-tests.sh --no-coverage   # plain unittest, no dependencies at all
```

To add a check, scaffold it rather than writing boilerplate — the generated
tests already cover every branch, so the coverage gate stays green:

```bash
./new-check.py --module graphics --name my_check --desc "..." --write
```

**Coverage is enforced at 100 %** by `run-tests.sh` and CI. If you add a check,
add tests for every branch of it — including the failure and skip paths, which
are the ones that run when something is already wrong.

Nothing in the suite touches real hardware. Two helpers make that possible:

- `tests/helpers.py` — `FakeContext` stands in for `Context`. Commands are
  matched by substring against the joined argv, so a test only names the
  distinctive part of a command:

  ```python
  ctx = FakeContext(
      commands={"lspci": cp("00:02.0 VGA ... \n\tKernel driver in use: i915")},
      tools=["glxinfo"],                 # what ctx.have() reports
      journal_kernel="GPU HANG: ecode",  # cached journal contents
      files={"/proc/sys/kernel/tainted": "0"},
      kconfig="CONFIG_PREEMPT=y\n",
  )
  assert graphics.gpu_driver(ctx).status is Status.PASS
  ```

- `tests/fakefs.py` — `fake_fs({...})` simulates /sys and /proc for checks that
  walk the filesystem. Map a path to a string for a file, or a list for a
  directory; nested glob patterns like `card*-*/status` work:

  ```python
  with fake_fs({"/sys/class/drm": ["card0-HDMI-A-1"],
                "/sys/class/drm/card0-HDMI-A-1/status": "connected"}):
      assert graphics.displays(ctx).metrics["displays_connected"] == 1
  ```

When a test fails, check whether the *test* is wrong before changing the code —
two failures during development here were incorrect assertions, not bugs. A
third was a genuine bug the tests caught: `snapshot()` in `stress.py` raised
instead of degrading when a sysfs directory was missing, which is exactly the
situation it exists to report on.


## Adding a check

One decorated function in `vitals/checks/<area>.py`. Keyword arguments become
metrics in the report and are diffed by `compare`.

```python
from ..core import check, Ok, Fail, Skip

@check(tier=1, name="my_check", desc="what it proves", requires=["some-tool"])
def my_check(ctx):
    if not ctx.path_exists("/sys/class/thing"):
        return Skip("hardware not present")          # not applicable
    n = ctx.count_matches(ctx.journal_kernel, r"thing.*error")
    if n:
        return Fail(f"{n} errors", thing_errors=n)   # present but broken
    return Ok("thing healthy", thing_errors=0)
```

`ctx` provides: `run`, `sudo`, `run_in_session` (compositor env attached),
`have`, `read`, `path_exists`, `count_matches`, `config_is_set`, and cached
`journal_kernel` / `journal_all` / `kconfig`. The journal is read once per run —
do not shell out to `journalctl` yourself.

Rules for new checks:

- Add tests covering every branch — `./run-tests.sh` enforces 100 % coverage.

- Absent hardware returns `Skip`, never `Fail`.
- If a metric has a direction, add it to `LOWER_IS_BETTER` or
  `HIGHER_IS_BETTER` in `vitals/core.py`, or comparisons cannot judge it.
- Mark anything that loads the machine, makes noise or suspends it as
  `disruptive=True`.
- Never record identifying data — no IP addresses, MACs or hostnames. Reports
  get committed and shared. Record that the gateway answered, not its address.
- Import the module in `omarchy-vitals.py` or its checks will not register.

## Reporting results back to a human

State the control first if it moved, then failures, then regressions. Do not
present `SKIP` counts as gaps, and do not attribute a difference to a cause you
have not isolated — this suite exists partly because two plausible-sounding
explanations for one result were tested here and both turned out wrong.
