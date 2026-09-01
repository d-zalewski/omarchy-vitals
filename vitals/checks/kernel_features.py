"""Tier 1 - kernel features userspace quietly assumes are there.

A config that drops one of these produces no error anywhere. The kernel boots,
the desktop comes up, and then an application fails days later for reasons that
look nothing like a kernel problem:

  * user_namespaces / overlayfs - every flatpak, distrobox, rootless container
    and Chromium's own sandbox. The failure reads as "the app will not start".
  * seccomp    - the same set of software, plus systemd's service hardening.
  * cgroups    - without the memory controller, systemd-oomd and every
    container memory limit silently do nothing at all.
  * kvm        - virtual machines, noticed a week later.
  * io_uring   - modern I/O paths, and a common hardening target.
  * crypto_accel - LUKS throughput. Correct either way, just far slower.

These probe the running kernel rather than reading its config, because the
config is not the thing userspace depends on.
"""
from __future__ import annotations

import fcntl
import os
import re
import tempfile
from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check, compile_run

# _IO(KVMIO, 0x00) - asks the KVM device for its API version.
KVM_GET_API_VERSION = 0xAE00

# Registers a filter that allows every syscall, so the process survives to
# report success. Proving the filter is *installed* is the point; enforcing
# anything would just kill the probe.
C_SECCOMP = r"""
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/prctl.h>
#include <stdio.h>
int main(void){
  struct sock_filter f[] = {{0x06, 0, 0, 0x7fff0000u}};   /* RET|K ALLOW */
  struct sock_fprog p = {.len = 1, .filter = f};
  if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) return 1;
  if (prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &p)) return 2;
  puts("ok"); return 0;
}
"""

# Controllers whose absence changes behaviour rather than just limiting it.
NEEDED_CONTROLLERS = ("cpu", "memory", "io", "pids")

# Driver-name markers for hardware AES across x86 and arm.
AES_ACCEL = re.compile(r"aesni|aes[-_]ce|vaes|aes[-_]x86|neon", re.I)


@check(tier=1, name="user_namespaces", desc="unprivileged user namespaces work",
       requires=["unshare"])
def user_namespaces(ctx):
    r = ctx.run(["unshare", "--user", "--map-root-user", "--mount", "--pid",
                 "--fork", "true"], timeout=30)
    if r.returncode == 0:
        return Ok("unprivileged user namespaces work", user_ns=1)
    limit = ctx.read("/proc/sys/user/max_user_namespaces", "").strip()
    if limit == "0":
        return Fail("disabled by user.max_user_namespaces=0 - flatpak, "
                    "distrobox and Chromium's sandbox will not start", user_ns=0)
    return Fail(f"unshare failed: {(r.stderr or r.stdout).strip()[:60]} - "
                f"containers and app sandboxes will not start", user_ns=0)


@check(tier=1, name="overlayfs", desc="overlayfs mounts in a user namespace",
       requires=["unshare"])
def overlayfs(ctx):
    """Mount one for real.

    /proc/filesystems is not evidence either way: overlay is a module that
    autoloads on first use, so it is legitimately absent from that list on a
    machine where overlayfs works perfectly.
    """
    with tempfile.TemporaryDirectory() as d:
        script = (f"cd {d} && mkdir -p l u w m && mount -t overlay overlay "
                  f"-o lowerdir=l,upperdir=u,workdir=w m")
        r = ctx.run(["unshare", "--user", "--map-root-user", "--mount",
                     "sh", "-c", script], timeout=30)
    if r.returncode == 0:
        return Ok("overlayfs mounts unprivileged", overlayfs=1)
    # Distinguish "this kernel has no overlayfs" from "it has one but will not
    # mount it unprivileged", which is a much smaller problem.
    present = ".ko" in ctx.run(["modinfo", "-F", "filename", "overlay"],
                               timeout=30).stdout or "overlay" in ctx.read(
        "/proc/filesystems", "")
    if present:
        return Warn("overlayfs exists but will not mount unprivileged - "
                    "rootless containers will not work", overlayfs=0)
    return Fail("no overlayfs - containers, flatpak and distrobox cannot work",
                overlayfs=0)


@check(tier=1, name="seccomp", desc="seccomp filters can be installed")
def seccomp(ctx):
    if not ctx.have("gcc"):
        # Without a compiler, the /proc field at least proves CONFIG_SECCOMP.
        if "Seccomp:" in ctx.read("/proc/self/status", ""):
            return Info("seccomp compiled in (install gcc to prove filters load)")
        return Fail("no Seccomp field in /proc/self/status - CONFIG_SECCOMP is "
                    "off; flatpak, Chromium and systemd hardening all need it")
    built, rc, out = compile_run(ctx, C_SECCOMP, ["-O2"])
    if not built:
        return Skip("could not compile the seccomp probe")
    if rc == 0 and "ok" in out:
        return Ok("seccomp BPF filter installed and enforced", seccomp=1)
    if rc == 1:
        return Fail("PR_SET_NO_NEW_PRIVS rejected", seccomp=0)
    return Fail(f"seccomp filter rejected (rc={rc}) - sandboxed applications "
                f"will not start", seccomp=0)


@check(tier=1, name="cgroups", desc="cgroup v2 controllers are delegated")
def cgroups(ctx):
    controllers = ctx.read("/sys/fs/cgroup/cgroup.controllers", "").split()
    if not controllers:
        if not ctx.read("/proc/self/cgroup", "").startswith("0::"):
            return Warn("cgroup v1 - systemd-oomd and modern container runtimes "
                        "expect the unified hierarchy")
        return Warn("cgroup v2 mounted but no controllers available")
    missing = [c for c in NEEDED_CONTROLLERS if c not in controllers]
    if missing:
        return Fail(f"missing controller(s): {', '.join(missing)} - resource "
                    f"limits using them are silently ignored",
                    cgroup_controllers=len(controllers))
    return Ok(f"{len(controllers)} controllers, all of "
              f"{'/'.join(NEEDED_CONTROLLERS)} present",
              cgroup_controllers=len(controllers))


def _kvm_probe(path="/dev/kvm"):
    """Open the KVM device and ask its API version.

    Opening proves the module is loaded and the driver answers, which is more
    than checking that a device node exists.
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except PermissionError:
        return "denied", None
    except OSError as exc:                             # noqa: BLE001
        return "error", str(exc)
    try:
        return "ok", fcntl.ioctl(fd, KVM_GET_API_VERSION)
    except OSError as exc:                             # noqa: BLE001
        return "error", str(exc)
    finally:
        os.close(fd)


@check(tier=1, name="kvm", desc="KVM is usable for virtual machines")
def kvm(ctx):
    if not Path("/dev/kvm").exists():
        if re.search(r"\b(vmx|svm)\b", ctx.read("/proc/cpuinfo", "")):
            return Warn("CPU supports virtualization but /dev/kvm is absent - "
                        "the KVM module is missing or not loaded", kvm=0)
        return Skip("CPU has no hardware virtualization")
    state, value = _kvm_probe()
    if state == "denied":
        return Warn("/dev/kvm exists but this user cannot open it - add the "
                    "user to the kvm group", kvm=0)
    if state == "error":
        return Fail(f"/dev/kvm did not answer: {str(value)[:60]}", kvm=0)
    return Ok(f"KVM usable, API version {value}", kvm=1)


@check(tier=1, name="io_uring", desc="io_uring is compiled in and permitted")
def io_uring(ctx):
    """The sysctl is registered by io_uring itself.

    Its presence is therefore a runtime fact about the kernel, not a guess
    from the config - though it only exists on 6.6 and newer.
    """
    policy = ctx.read("/proc/sys/kernel/io_uring_disabled", "").strip()
    if policy == "0":
        return Ok("io_uring available", io_uring=1)
    if policy == "1":
        return Info("io_uring restricted to the io_uring group", io_uring=1)
    if policy == "2":
        return Info("io_uring disabled by policy", io_uring=0)
    if ctx.config_is_set("CONFIG_IO_URING"):
        return Ok("io_uring compiled in (kernel predates the sysctl)", io_uring=1)
    if not ctx.kconfig:
        return Skip("no io_uring sysctl and /proc/config.gz is unavailable")
    return Info("io_uring not compiled in", io_uring=0)


@check(tier=1, name="crypto_accel", desc="hardware AES is registered for LUKS")
def crypto_accel(ctx):
    """LUKS uses xts(aes); without an accelerated driver every read burns CPU."""
    raw = ctx.read("/proc/crypto", "")
    if not raw:
        return Skip("/proc/crypto unavailable")
    accelerated, generic = [], []
    for block in raw.split("\n\n"):
        fields = dict(re.findall(r"^(\w+)\s*:\s*(.+)$", block, re.M))
        name, driver = fields.get("name", ""), fields.get("driver", "")
        if "aes" not in name:
            continue
        (accelerated if AES_ACCEL.search(driver) else generic).append((name, driver))
    if not accelerated and not generic:
        return Skip("no AES implementations registered")
    if not accelerated:
        return Warn(f"only software AES ({generic[0][1]}) - LUKS throughput "
                    f"will be CPU-bound", aes_accelerated=0)
    xts = next((d for n, d in accelerated if n.startswith("xts")), None)
    return Ok(f"hardware AES: {xts or accelerated[0][1]}", aes_accelerated=1)
