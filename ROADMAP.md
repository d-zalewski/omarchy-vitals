# Roadmap

Checks that have been proposed and not yet written, the ones deliberately
rejected, and the conventions that any new check is expected to follow.

## Where the suite stands

88 checks. 401 tests at 100 % line coverage, passing on Python 3.11 through
3.14. Everything currently in the suite has been run against real hardware,
not only against its unit tests.

## Proposed, not yet written

**Desktop, proven rather than enumerated.** Two are worth doing on any machine
with a session:

| Check | Proves | Needs |
|---|---|---|
| `screenshot` | A frame is captured at monitor resolution and its compressed size is far above a blank frame's. The strongest "the desktop actually renders" test available, and it works over SSH through `run_in_session`. | `grim` |
| `screencast_portal` | `org.freedesktop.portal.ScreenCast` answers over the session bus. Screen sharing breaks quietly and is noticed during a call. | `busctl` |

Three more only pay off on hardware that has the device, and skip cleanly
otherwise: `gl_render` (a few frames of real offscreen GL, needing `glmark2`),
`libinput_devices` (the input stack reporting capabilities rather than
`/proc/bus/input/devices` text), and `bluetooth_scan` (discovery finding
devices; count only, never addresses).

**Post-resume correctness.** Tier 4 today compares device *names* before and
after suspend, so a NIC that returns as a node but not as a working link
passes. `resume_functional` would re-run a handful of probes after the cycles -
DNS resolves, the GPU render node works, audio still opens. `clock_after_resume`
would confirm `CLOCK_BOOTTIME` advanced consistently across the sleep;
timekeeping regressions across suspend are a real bug class nothing here sees.

Both need care: never run tier 4 on a machine you cannot physically reach.

**Smaller ones.** `cpu_vulnerabilities` (anything reporting `Vulnerable` in
`/sys/devices/system/cpu/vulnerabilities/`, since a config trim can quietly
drop a mitigation) and `cpuidle` (deep C-state usage counters non-zero - a
machine that never idles benchmarks fine and just runs hot).

## Rejected

**Microphone capture.** A check that records a second of audio to verify the
capture path is not acceptable, on privacy grounds. Recording captures whatever
is happening around the machine even when the samples are only inspected and
discarded. Verify capture hardware by enumeration instead. Emitting sound is
fine - `audio_playback` already does.

## Conventions a new check has to follow

Most of these were learned by getting them wrong first.

**Absent hardware returns `Skip`, never `Fail`.** A machine without wifi
failing wifi checks makes a suite people mute.

**Never overstate a finding.** `secure_boot` and `discard` never fail: most
machines run without Secure Boot, and modern SSD controllers cope without TRIM.
Both record a metric instead, so `compare` reports a change as a regression on
the machines where the thing was set up deliberately, and says nothing on the
rest. A check that cries wolf is one people learn to skim.

**Reports carry no identifying data.** They get committed. That rules out
hostnames, TPM PCR digests, LUKS UUIDs, journal message text and module signer
names - all of which a check has wanted to include at some point. Report counts
and states.

**Probe the target before writing the parser.** Every check here that was
written from a plausible assumption about output format had the assumption
turn out wrong:

- `overlay` is absent from `/proc/filesystems` on a machine where overlayfs
  works, because the module autoloads on first use. Treating that file as
  evidence of absence would have failed a working system.
- `/boot` had no `vmlinuz-*` at all (unified kernel images) and was root-only,
  so the first `initramfs` check silently skipped on its primary target.
- `/tmp` is tmpfs, so the disk I/O check was benchmarking RAM and reporting it
  as disk throughput.
- The suite's own `stack_protector` probe aborts on purpose, and
  systemd-coredump logs that at error priority - so `journal_errors` was
  reporting a fault the suite itself caused.

**Prefer a functional probe to a config read.** `modules_signed` reads the
config; `module_sig` asks `modinfo` about a real module. `gpu_accel` reads a
renderer string; `gl_render` would submit frames. The config is not the thing
userspace depends on.

**Keep the coverage gate platform-independent.** Bind Linux-only functions once
at import (`FADVISE = getattr(os, "posix_fadvise", None)`) rather than probing
with `hasattr` at call time, so the same lines are exercised wherever the tests
run.

## Known defects, unfixed

- **Reports leak the hostname.** `build_report` writes `platform.node()`, while
  the README states reports carry no identifying data. Both committed examples
  say `example-host`, so they were scrubbed by hand. Drop the field or hash it.
- **`.coverage` is tracked in git**, so every local test run shows a spurious
  diff. It wants `.gitignore` and a `git rm --cached`.
- **`run-tests.sh` fails on bash 3.2** (`ARGS[@]: unbound variable` under
  `set -u` with an empty array). Harmless on the Arch target, awkward when
  developing on macOS.

## Verifying a change on real hardware

Unit tests do not catch any of the failures listed above. Copy the working tree
to the target and run the new checks there before committing:

```bash
COPYFILE_DISABLE=1 tar czf - --exclude .git --exclude __pycache__ . \
  | ssh user@host 'rm -rf /tmp/ov && mkdir -p /tmp/ov && tar xzf - -C /tmp/ov \
                   && cd /tmp/ov && python3 omarchy-vitals.py --only <names> \
                      --tier all --no-report'
```

Then confirm nothing was left behind - loaded modules, temporary files, kernel
taint - and remove `/tmp/ov`.
