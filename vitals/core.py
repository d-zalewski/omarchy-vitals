"""Core framework: checks, results, metrics, execution context.

Adding a new check is one decorated function:

    from vitals.core import check, Ok, Fail

    @check(tier=1, name="my_thing", desc="what it proves")
    def my_thing(ctx):
        if not ctx.path_exists("/sys/kernel/something"):
            return Fail("missing /sys/kernel/something")
        return Ok("present", some_metric=42)

Return Ok/Fail/Warn/Skip/Info. Keyword arguments become metrics recorded in
the JSON report and diffed by `omarchy-vitals compare`.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"
    INFO = "INFO"


# Direction metadata so A/B comparison can say "regression" rather than just
# printing a delta. Keys are metric names; unknown metrics are treated neutral.
LOWER_IS_BETTER = {
    "cyclictest_idle_avg_us", "cyclictest_idle_max_us",
    "cyclictest_loaded_avg_us", "cyclictest_loaded_max_us",
    "hackbench_sec", "boot_userspace_ms", "boot_initrd_ms", "boot_kernel_ms",
    "oops_count", "warn_count", "mce_count", "failed_units", "probe_failures",
    "gpu_hangs", "audio_xruns", "taint", "oom_events", "stress_new_oops",
    "resume_errors", "btrfs_scrub_errors", "stress_peak_temp_c",
    "dkms_missing", "pci_unbound", "kernel_reboot_pending", "efi_unsigned",
    "journal_errors", "fio_randwrite_lat_us", "btrfs_io_errors",
    "cpu_vulnerable", "resume_broken", "clock_resume_skew_ms",
    # tier 5: time-per-operation and latency percentiles
    "ctxsw_usecs_op", "syscall_usecs_op", "sched_messaging_sec",
    "sysbench_threads_p95_ms",
}
HIGHER_IS_BETTER = {
    "fio_randread_iops", "fio_randwrite_iops", "btf_modules",
    "stress_bogo_ops",
    # tier 5: operations-per-second and bandwidth
    "ctxsw_ops_sec", "syscall_ops_sec", "sysbench_threads_events",
    "sysbench_cpu_eps", "memcpy_gb_sec", "loopback_gbit_s",
    "secure_boot", "luks_tpm_token",
    "user_ns", "overlayfs", "seccomp", "kvm", "io_uring",
    "aes_accelerated", "cgroup_controllers", "discard_reaches_drive",
    "screencast_portal", "glmark2_score", "cpuidle_states_used",
}
# The C probes compile_run() builds. stack_protector deliberately aborts
# one, which systemd-coredump logs at error priority, so journal_errors
# needs to recognise the suite's own noise. Kept under 15 characters, the
# width of a comm field.
PROBE_NAME = "vitals-probe"

UNITS = {
    "cyclictest_idle_avg_us": "us", "cyclictest_idle_max_us": "us",
    "cyclictest_loaded_avg_us": "us", "cyclictest_loaded_max_us": "us",
    "hackbench_sec": "s", "stress_peak_temp_c": "C",
    "btf_vmlinux_bytes": "B", "swap_kb": "kB",
    "boot_userspace_ms": "ms", "boot_initrd_ms": "ms", "boot_kernel_ms": "ms",
    "fio_randread_iops": "IOPS", "fio_randwrite_iops": "IOPS",
    "fio_randwrite_lat_us": "us",
    "ctxsw_usecs_op": "us", "syscall_usecs_op": "us",
    "sched_messaging_sec": "s", "sysbench_threads_p95_ms": "ms",
    "memcpy_gb_sec": "GB/s", "loopback_gbit_s": "Gb/s",
    "clock_resume_skew_ms": "ms",
}


@dataclass
class Result:
    status: Status
    message: str
    metrics: dict = field(default_factory=dict)


def Ok(message: str, **metrics) -> Result:
    return Result(Status.PASS, message, metrics)


def Fail(message: str, **metrics) -> Result:
    return Result(Status.FAIL, message, metrics)


def Warn(message: str, **metrics) -> Result:
    return Result(Status.WARN, message, metrics)


def Skip(message: str, **metrics) -> Result:
    return Result(Status.SKIP, message, metrics)


def Info(message: str, **metrics) -> Result:
    return Result(Status.INFO, message, metrics)


def compile_run(ctx, src: str, flags: list[str]):
    """Compile a C probe and run it, returning (built, returncode, stdout).

    Some kernel features can only be confirmed by asking the kernel for them
    from C. Callers should require "gcc" or degrade when built is False.
    """
    with tempfile.TemporaryDirectory() as d:
        c = Path(d) / f"{PROBE_NAME}.c"
        exe = Path(d) / PROBE_NAME
        c.write_text(src)
        cp = ctx.run(["gcc", *flags, "-o", str(exe), str(c)], timeout=90)
        if cp.returncode != 0:
            return False, None, cp.stderr
        try:
            # ulimit -c 0 keeps a deliberate abort from writing a core dump
            r = subprocess.run(f"ulimit -c 0; {exe}", shell=True,
                               capture_output=True, text=True, timeout=30)
            return True, r.returncode, r.stdout
        except Exception as exc:                       # noqa: BLE001
            return True, None, str(exc)


# tmpfs is RAM. An I/O test run there measures memcpy and reports it as disk
# throughput, which is worse than not testing at all.
NOT_A_DISK = ("tmpfs", "ramfs", "devtmpfs", "overlay")


def disk_dir(ctx):
    """A directory backed by real storage, with the filesystem it sits on.

    /var/tmp is the standard on-disk temporary directory - unlike /tmp, which
    is tmpfs on most systemd installations, this one included.
    """
    for path in ("/var/tmp", os.path.expanduser("~")):
        fstype = ctx.run(["findmnt", "-no", "FSTYPE", "--target", path],
                         timeout=30).stdout.strip()
        if fstype and fstype not in NOT_A_DISK:
            return path, fstype
    return None, None


def sudo_refused(result) -> bool:
    """True when `sudo -n` declined, rather than the command itself failing.

    Checks that need root degrade to SKIP in that case, so a machine without
    passwordless sudo does not read as broken.
    """
    text = (result.stderr or "") + (result.stdout or "")
    return "password is required" in text or "a terminal is required" in text


@dataclass
class Check:
    fn: Callable
    name: str
    tier: int
    desc: str
    requires: tuple = ()        # binaries that must exist, else SKIP
    disruptive: bool = False    # heavy load / suspends the machine
    est_seconds: int = 1


REGISTRY: list[Check] = []


def check(*, tier: int, name: str, desc: str, requires: Iterable[str] = (),
          disruptive: bool = False, est_seconds: int = 1):
    def deco(fn):
        REGISTRY.append(Check(fn=fn, name=name, tier=tier, desc=desc,
                              requires=tuple(requires), disruptive=disruptive,
                              est_seconds=est_seconds))
        return fn
    return deco


class Context:
    """Helpers shared by checks, with caching for expensive lookups.

    The journal is read once per run rather than per check - the shell version
    of this suite shelled out to journalctl a dozen times, which on a low-power
    box was a measurable chunk of the runtime.
    """

    def __init__(self, *, minutes: int = 5, stress_minutes: int = 20,
                 assume_yes: bool = False):
        self.minutes = minutes
        self.stress_minutes = stress_minutes
        self.assume_yes = assume_yes
        self._journal_k: str | None = None
        self._journal_all: str | None = None
        self._config: str | None = None
        self._session_env: dict | None = None

    # -- process helpers ---------------------------------------------------
    def run(self, cmd: list[str] | str, timeout: int = 60, check_rc: bool = False):
        shell = isinstance(cmd, str)
        try:
            return subprocess.run(cmd, shell=shell, capture_output=True,
                                  text=True, timeout=timeout, check=check_rc)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, "", "timeout")
        except Exception as exc:                      # noqa: BLE001
            return subprocess.CompletedProcess(cmd, 1, "", str(exc))

    def sudo(self, cmd: list[str] | str, timeout: int = 60):
        if isinstance(cmd, str):
            return self.run(f"sudo -n {cmd}", timeout=timeout)
        return self.run(["sudo", "-n", *cmd], timeout=timeout)

    def have(self, binary: str) -> bool:
        return shutil.which(binary) is not None

    def path_exists(self, p: str) -> bool:
        return Path(p).exists()

    def read(self, p: str, default: str = "") -> str:
        try:
            return Path(p).read_text().strip()
        except Exception:                             # noqa: BLE001
            return default

    # -- cached sources ----------------------------------------------------
    @property
    def journal_kernel(self) -> str:
        if self._journal_k is None:
            self._journal_k = self.run(
                ["journalctl", "-b", "0", "-k", "--no-pager"], timeout=120).stdout
        return self._journal_k

    @property
    def journal_all(self) -> str:
        if self._journal_all is None:
            self._journal_all = self.run(
                ["journalctl", "-b", "0", "--no-pager"], timeout=120).stdout
        return self._journal_all

    @property
    def kconfig(self) -> str:
        """Running kernel's config, from /proc/config.gz if available."""
        if self._config is None:
            r = self.sudo("zcat /proc/config.gz", timeout=30)
            self._config = r.stdout if r.returncode == 0 else ""
        return self._config

    def config_is_set(self, opt: str) -> bool:
        return f"{opt}=y" in self.kconfig or f"{opt}=m" in self.kconfig

    def count_matches(self, text: str, pattern: str) -> int:
        import re
        return len(re.findall(pattern, text, re.IGNORECASE | re.MULTILINE))

    # -- graphical session -------------------------------------------------
    def session_env(self) -> dict:
        """Environment of the running Wayland compositor.

        Checks are usually run over SSH, which has no WAYLAND_DISPLAY or
        HYPRLAND_INSTANCE_SIGNATURE, so tools like hyprctl and wpctl would fail
        with a misleading error. Borrow the compositor's own environment
        instead. Returns {} when no session is found.
        """
        if self._session_env is not None:
            return self._session_env
        env: dict = {}
        for comp in ("Hyprland", "sway", "gnome-shell", "kwin_wayland"):
            r = self.run(["pgrep", "-x", comp])
            if r.returncode != 0 or not r.stdout.strip():
                continue
            pid = r.stdout.split()[0]
            try:
                raw = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace")
            except Exception:                          # noqa: BLE001
                continue
            for item in raw.split("\0"):
                if "=" not in item:
                    continue
                k, v = item.split("=", 1)
                if k in ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DISPLAY",
                         "HYPRLAND_INSTANCE_SIGNATURE", "XDG_CURRENT_DESKTOP",
                         "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE"):
                    env[k] = v
            env["_compositor"] = comp
            break

        # Hyprland sets HYPRLAND_INSTANCE_SIGNATURE for its children but does
        # not necessarily carry it in its own environ, so recover it from the
        # runtime directory it creates (newest instance wins).
        if env.get("_compositor") == "Hyprland" and "HYPRLAND_INSTANCE_SIGNATURE" not in env:
            rt = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
            hypr = Path(rt) / "hypr"
            if hypr.is_dir():
                inst = sorted((p for p in hypr.iterdir() if p.is_dir()),
                              key=lambda p: p.stat().st_mtime, reverse=True)
                if inst:
                    env["HYPRLAND_INSTANCE_SIGNATURE"] = inst[0].name
                    env.setdefault("XDG_RUNTIME_DIR", rt)

        # WAYLAND_DISPLAY needs the same treatment, for the same reason: the
        # compositor creates its socket after exec, so its own environ never
        # names it. hyprctl and wpctl do not care - they find the session by
        # signature and by runtime directory - but anything speaking the
        # Wayland protocol itself (grim) falls back to wayland-0 and fails on
        # a machine whose socket is wayland-1. Newest socket wins.
        if env and "WAYLAND_DISPLAY" not in env:
            rt = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
            socks = sorted((p for p in Path(rt).glob("wayland-*")
                            if p.suffix != ".lock"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if socks:
                env["WAYLAND_DISPLAY"] = socks[0].name
                env.setdefault("XDG_RUNTIME_DIR", rt)
        self._session_env = env
        return env

    def run_in_session(self, cmd: list[str], timeout: int = 30):
        """Run a command with the compositor's environment attached."""
        import os
        env = {**os.environ, **{k: v for k, v in self.session_env().items()
                                if not k.startswith("_")}}
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, "", "timeout")
        except Exception as exc:                       # noqa: BLE001
            return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def select(tiers: Iterable[int] | None = None,
           only: Iterable[str] | None = None,
           skip_disruptive: bool = False) -> list[Check]:
    picked = list(REGISTRY)
    if tiers is not None:
        tiers = set(tiers)
        picked = [c for c in picked if c.tier in tiers]
    if only:
        want = set(only)
        picked = [c for c in picked if c.name in want]
    if skip_disruptive:
        picked = [c for c in picked if not c.disruptive]
    return sorted(picked, key=lambda c: (c.tier, c.name))


def run_check(c: Check, ctx: Context) -> tuple[Result, float]:
    missing = [b for b in c.requires if not ctx.have(b)]
    if missing:
        return Skip(f"missing tool(s): {', '.join(missing)}"), 0.0
    start = time.monotonic()
    try:
        res = c.fn(ctx)
        if res is None:
            res = Info("check returned no result")
    except Exception as exc:                          # noqa: BLE001
        res = Fail(f"check raised {type(exc).__name__}: {exc}")
    return res, time.monotonic() - start
