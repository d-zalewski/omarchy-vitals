"""Tier 1 - checks specific to how marchy builds kernels.

These exist because marchy cross-compiles with a self-built Arch GCC rather
than using Arch's binary toolchain, so the risks are toolchain risks:

  * stack_protector - the cross-gcc needs --with-glibc-version to emit the
    kernel's %gs:40 guard rather than a global __stack_chk_guard symbol.
    Getting it wrong is silent memory corruption, not a clean failure.
  * vdso32 - the cross toolchain enables multilib for the 32-bit vDSO.
  * btf - validates keeping CONFIG_DEBUG_INFO_BTF_MODULES in the stock config.
"""
from __future__ import annotations

from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check, compile_run

C_VDSO = r"""
#include <time.h>
#include <stdio.h>
int main(void){struct timespec t;
 if(clock_gettime(CLOCK_MONOTONIC,&t)) return 1;
 if(clock_gettime(CLOCK_REALTIME,&t)) return 1;
 puts("ok"); return 0;}
"""

C_SSP = r"""
#include <string.h>
int main(void){char b[16]; memset(b,'A',64); return 0;}
"""


@check(tier=1, name="compiler", desc="compiler the kernel was built with")
def compiler(ctx):
    import re
    ver = ctx.read("/proc/version", "")
    # The compiler field nests parentheses, e.g.
    #   (x86_64-pc-linux-gnu-gcc (marchy cross / Arch 16.2.1+r23+g...) 16.2.1 ...)
    # so a naive [^)]* stops too early. Match name, optional bracketed vendor
    # string, then the version number.
    m = re.search(r"(\S*(?:gcc|clang)\s*(?:\([^)]*\)\s*)?[\d.]+)", ver, re.I)
    if not m:
        return Info("compiler string not parseable")
    return Info(m.group(1).strip()[:78])


@check(tier=1, name="btf", desc="BTF present for eBPF CO-RE tooling")
def btf(ctx):
    p = Path("/sys/kernel/btf/vmlinux")
    if not p.exists():
        return Fail("no /sys/kernel/btf/vmlinux - eBPF CO-RE will not work")
    size = p.stat().st_size
    mods = len([f for f in Path("/sys/kernel/btf").iterdir() if f.name != "vmlinux"])
    if mods == 0:
        return Warn(f"vmlinux BTF ({size//1024} KB) but no per-module BTF",
                    btf_vmlinux_bytes=size, btf_modules=0)
    return Ok(f"vmlinux BTF {size//1024} KB, {mods} modules",
              btf_vmlinux_bytes=size, btf_modules=mods)


@check(tier=1, name="bpftrace", desc="eBPF attaches in practice", requires=["bpftrace"])
def bpftrace_attach(ctx):
    # Needs root: eBPF attach wants CAP_BPF/CAP_DAC_READ_SEARCH, and Arch ships
    # kernel.unprivileged_bpf_disabled=2.
    prog = ('tracepoint:sched:sched_process_exec { @[comm] = count(); } '
            'interval:s:2 { exit(); }')
    r = ctx.sudo(["timeout", "30", "bpftrace", "-e", prog], timeout=45)
    if "Attached" in (r.stdout + r.stderr):
        return Ok("bpftrace attached and ran - BTF usable")
    return Fail(f"bpftrace failed: {(r.stderr or r.stdout).strip()[:70]}")


@check(tier=1, name="vdso64", desc="64-bit vDSO clock_gettime", requires=["gcc"])
def vdso64(ctx):
    built, rc, out = compile_run(ctx, C_VDSO, ["-O2"])
    if not built:
        return Skip("could not compile test program")
    return Ok("vDSO clock_gettime works (64-bit)") if rc == 0 and "ok" in out \
        else Fail(f"64-bit vDSO failed (rc={rc})")


@check(tier=1, name="vdso32", desc="32-bit vDSO / IA32 emulation", requires=["gcc"])
def vdso32(ctx):
    built, rc, out = compile_run(ctx, C_VDSO, ["-m32", "-O2"])
    if not built:
        return Skip("no 32-bit libc/headers installed")
    return Ok("vDSO works (32-bit compat / IA32_EMULATION)") if rc == 0 and "ok" in out \
        else Fail(f"32-bit vDSO failed (rc={rc}) - IA32 emulation broken")


@check(tier=1, name="stack_protector", desc="stack canary actually fires", requires=["gcc"])
def stack_protector(ctx):
    built, rc, _ = compile_run(ctx, C_SSP, ["-O0", "-fstack-protector-strong"])
    if not built:
        return Skip("could not compile test program")
    if rc in (134, -6):                # SIGABRT from __stack_chk_fail
        return Ok("stack protector fires correctly (SIGABRT)")
    return Warn(f"deliberate stack smash did not abort (rc={rc})")


@check(tier=1, name="modules_signed", desc="module signature enforcement state")
def modules_signed(ctx):
    if not ctx.kconfig:
        return Skip("/proc/config.gz unavailable")
    sig = ctx.config_is_set("CONFIG_MODULE_SIG")
    force = "CONFIG_MODULE_SIG_FORCE=y" in ctx.kconfig
    if not sig:
        return Info("module signing not enabled in this kernel")
    return Ok(f"module signing enabled{' (enforced)' if force else ' (not enforced)'}")
