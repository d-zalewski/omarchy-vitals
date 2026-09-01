# Roadmap

Checks that have been written but not yet run on real hardware, the ones
deliberately rejected, and the conventions that any new check is expected to
follow.

## Where the suite stands

97 checks. 478 tests at 100 % line coverage, passing on Python 3.11 through
3.14. Everything here has been run against real hardware except the three items
in the next section.

## Still unproven on hardware

The nine checks added most recently were run on the Omarchy mini PC
(7.2.2-5-omarchy-bore, Intel UHD 600, Hyprland 0.56.2). Six pass there,
`screenshot` was fixed twice against it, and these three could not be
exercised:

| Gap | Why | What would settle it |
|---|---|---|
| `bluetooth_scan` | That machine has no bluetooth hardware, so the check skips before doing anything | A box with an adapter: confirm `bluetoothctl --timeout 10 scan on` exists in the installed bluez, and that "No default controller" is the wording when the radio is unusable |
| `resume_functional` | Tier 4, not run - it suspends the machine, and a failed resume needs a power button | Run it from a keyboard you can reach; confirm all three probes answer *before* the cycle, or the check skips itself |
| `clock_after_resume` | Same | Confirm `CLOCK_BOOTTIME` minus `CLOCK_MONOTONIC` really is the sleep length, and that NTP does not step the clock often enough to make the warning routine |

## Proposed, not yet written

Nothing outstanding. The standing wish is the one in
[CONTRIBUTING.md](CONTRIBUTING.md): checks for hardware that isn't here to test
against - NVIDIA and AMD graphics, wifi, laptop suspend, batteries, external
displays.

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
rest. `cpu_vulnerabilities` is the same shape for a different reason - plenty of
desktops boot `mitigations=off` on purpose. A check that cries wolf is one
people learn to skim.

**Reports carry no identifying data.** They get committed. That rules out
hostnames, TPM PCR digests, LUKS UUIDs, journal message text and module signer
names - all of which a check has wanted to include at some point. Report counts
and states. The one identifier a report carries is `machine_id`, a digest of
`/etc/machine-id` that `compare` uses to notice two reports from different
machines; it reveals neither the id nor the name, and the committed examples
carry an obvious placeholder rather than a real one.

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
- Hyprland's own `/proc/<pid>/environ` carries no `WAYLAND_DISPLAY`: it creates
  the socket after exec and exports it only to children. hyprctl and wpctl
  never noticed, because they find the session by signature and runtime
  directory - but grim connected to `wayland-0` and died with "failed to create
  display". `session_env` now recovers the socket name the same way it already
  recovered the Hyprland instance signature.
- grim blocks forever on an output that is not rendering; wlr-screencopy simply
  never delivers a frame. Capturing the whole layout therefore hangs on any
  machine with a second monitor asleep, so `screenshot` names one output.
- `dpmsStatus: false` does not mean "cannot be captured". The VNC output on the
  test machine reports asleep and still returns a frame instantly, so DPMS is
  used to *explain* a blank frame rather than to refuse to take one.
- The blank-frame threshold was guessed at 3 KB and measured at 6,121 bytes for
  a blank 1920x1080 output. The same output awake is 98 KB, a 4K output awake
  is 287 KB, and the whole two-monitor layout is 699 KB - so 16 KB separates
  them by an order of magnitude in both directions.
- The unsuffixed `glmark2` binary is the GLX build and dies with "Could not
  initialize canvas" in a Wayland session, however healthy the GPU is.
  `glmark2-wayland` scores 603-624 on the same machine and the same GPU.
- The `libinput` CLI is packaged as `libinput-tools`; the `libinput` package
  is the library, is already installed everywhere, and ships no binary.

**Prefer a functional probe to a config read.** `modules_signed` reads the
config; `module_sig` asks `modinfo` about a real module. `gpu_accel` reads a
renderer string; `gl_render` submits frames. `suspend_resume` compares device
names; `resume_functional` re-runs the probes. The config, and the name, is not
the thing userspace depends on.

**Keep the coverage gate platform-independent.** Bind Linux-only functions once
at import (`FADVISE = getattr(os, "posix_fadvise", None)`,
`CLOCK_BOOTTIME = getattr(time, "CLOCK_BOOTTIME", None)`) rather than probing
with `hasattr` at call time, so the same lines are exercised wherever the tests
run.

## Verifying a change on real hardware

Unit tests do not catch any of the failures listed above. Copy the working tree
to the target and run the new checks there before committing:

```bash
COPYFILE_DISABLE=1 tar czf - --exclude .git --exclude __pycache__ . \
  | ssh user@host 'rm -rf /tmp/ov && mkdir -p /tmp/ov && tar xzf - -C /tmp/ov \
                   && cd /tmp/ov && python3 omarchy-vitals.py --only <names> \
                      --tier all --no-report'
```

The nine above, in one go - tier 4 last, and only from a keyboard you can
reach:

```bash
--only screenshot,screencast_portal,gl_render,libinput_devices,cpuidle,\
bluetooth_scan,cpu_vulnerabilities
--only resume_functional,clock_after_resume --tier 4
```

Then confirm nothing was left behind - loaded modules, temporary files, kernel
taint - and remove `/tmp/ov`.
