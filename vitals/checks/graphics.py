"""Tier 1 - GPU and display.

On a desktop this is the most visible thing a kernel can break: a driver that
fails to bind, a render node that never appears, or a compositor that falls
back to software rendering. The last one is the nasty case, because the desktop
still "works" - just slowly and hot - so it is checked explicitly.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check

DRM = Path("/sys/class/drm")


def _gpus(ctx) -> list[tuple[str, str]]:
    """[(pci_id, driver)] for VGA/3D controllers."""
    out = ctx.run(["lspci", "-k"]).stdout
    gpus, cur = [], None
    for line in out.splitlines():
        if re.search(r"(VGA compatible|3D) controller", line):
            cur = line.split()[0]
        elif cur and "Kernel driver in use:" in line:
            gpus.append((cur, line.split(":", 1)[1].strip()))
            cur = None
        elif line and not line.startswith("\t"):
            cur = None
    return gpus


@check(tier=1, name="gpu_driver", desc="GPU driver bound")
def gpu_driver(ctx):
    gpus = _gpus(ctx)
    if not gpus:
        # No driver line means the GPU is present but nothing claimed it.
        if "controller" in ctx.run(["lspci"]).stdout.lower():
            return Fail("GPU present but no kernel driver bound")
        return Skip("no GPU found")
    desc = ", ".join(f"{pci} -> {drv}" for pci, drv in gpus)
    return Ok(f"{len(gpus)} GPU(s): {desc}", gpu_count=len(gpus))


@check(tier=1, name="drm_render_node", desc="DRM render node for acceleration")
def drm_render_node(ctx):
    nodes = sorted(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").exists() else []
    cards = sorted(Path("/dev/dri").glob("card*")) if Path("/dev/dri").exists() else []
    if not cards:
        return Fail("no /dev/dri/card* - KMS not working")
    if not nodes:
        # Without a render node, GPU-accelerated clients fall back to software.
        return Fail("no /dev/dri/renderD* - no GPU acceleration available")
    return Ok(f"{len(cards)} card node(s), {len(nodes)} render node(s)",
              drm_cards=len(cards), drm_render_nodes=len(nodes))


@check(tier=1, name="displays", desc="connected display outputs")
def displays(ctx):
    if not DRM.exists():
        return Skip("no /sys/class/drm")
    connected, total = [], 0
    for st in DRM.glob("card*-*/status"):
        total += 1
        if st.read_text().strip() == "connected":
            connected.append(st.parent.name.split("-", 1)[1])
    if total == 0:
        return Skip("no display connectors exposed")
    if not connected:
        return Warn(f"no connected displays (of {total} connectors) - headless?",
                    displays_connected=0)
    return Ok(f"{len(connected)}/{total} connected: {', '.join(connected)}",
              displays_connected=len(connected))


@check(tier=1, name="drm_modes", desc="display mode/EDID readable")
def drm_modes(ctx):
    if not DRM.exists():
        return Skip("no /sys/class/drm")
    for st in DRM.glob("card*-*/status"):
        if st.read_text().strip() != "connected":
            continue
        modes = st.parent / "modes"
        if modes.exists() and modes.read_text().strip():
            first = modes.read_text().strip().splitlines()[0]
            return Ok(f"{st.parent.name.split('-',1)[1]} advertises modes (top: {first})")
        return Warn(f"{st.parent.name} connected but no modes - EDID read failed")
    return Skip("no connected outputs to read modes from")


@check(tier=1, name="gpu_errors", desc="GPU hangs, resets, DRM errors")
def gpu_errors(ctx):
    hangs = ctx.count_matches(ctx.journal_kernel,
                              r"GPU HANG|gpu hung|reset .*(ring|engine)|GPU crash")
    drm_err = ctx.count_matches(ctx.journal_kernel,
                                r"\[drm:.*\*ERROR\*|drm.*failed to (init|probe)")
    if hangs:
        return Fail(f"{hangs} GPU hang/reset event(s)", gpu_hangs=hangs,
                    drm_errors=drm_err)
    if drm_err:
        return Warn(f"{drm_err} DRM error line(s), no hangs", gpu_hangs=0,
                    drm_errors=drm_err)
    return Ok("no GPU hangs or DRM errors", gpu_hangs=0, drm_errors=0)


@check(tier=1, name="compositor", desc="Wayland compositor alive")
def compositor(ctx):
    env = ctx.session_env()
    if not env:
        return Skip("no Wayland compositor running (headless or TTY only)")
    comp = env.get("_compositor", "?")
    if comp == "Hyprland":
        r = ctx.run_in_session(["hyprctl", "version"])
        if r.returncode != 0:
            return Fail(f"Hyprland running but hyprctl failed: "
                        f"{(r.stderr or r.stdout).strip()[:70]}")
        ver = r.stdout.strip().splitlines()[0] if r.stdout.strip() else "?"
        return Ok(f"{comp} responsive ({ver[:50]})")
    return Ok(f"{comp} running")


@check(tier=1, name="compositor_outputs", desc="compositor sees its monitors")
def compositor_outputs(ctx):
    env = ctx.session_env()
    if env.get("_compositor") != "Hyprland":
        return Skip("not a Hyprland session")
    r = ctx.run_in_session(["hyprctl", "-j", "monitors"])
    if r.returncode != 0:
        return Skip("hyprctl monitors unavailable")
    try:
        mons = json.loads(r.stdout)
    except Exception:                                  # noqa: BLE001
        return Warn("could not parse hyprctl monitors output")
    if not mons:
        return Warn("compositor reports no monitors", compositor_monitors=0)
    desc = ", ".join(f"{m.get('name')}@{m.get('refreshRate', 0):.0f}Hz" for m in mons)
    return Ok(f"{len(mons)} monitor(s): {desc}", compositor_monitors=len(mons))


@check(tier=1, name="gpu_accel", desc="hardware 3D acceleration in use")
def gpu_accel(ctx):
    # Software fallback is the dangerous failure: everything works, but slowly.
    for tool, args, pat in (
        ("glxinfo", ["-B"], r"OpenGL renderer string:\s*(.+)"),
        ("eglinfo", [], r"OpenGL renderer string:\s*(.+)"),
    ):
        if not ctx.have(tool):
            continue
        r = ctx.run_in_session([tool, *args], timeout=60)
        m = re.search(pat, r.stdout)
        if not m:
            continue
        renderer = m.group(1).strip()
        if re.search(r"llvmpipe|softpipe|swrast", renderer, re.I):
            return Fail(f"software rendering in use: {renderer[:60]}")
        return Ok(f"hardware renderer: {renderer[:60]}")
    if ctx.have("vulkaninfo"):
        r = ctx.run_in_session(["vulkaninfo", "--summary"], timeout=60)
        if "deviceName" in r.stdout:
            name = re.search(r"deviceName\s*=\s*(.+)", r.stdout)
            return Ok(f"Vulkan device: {name.group(1).strip()[:60] if name else 'present'}")
    return Skip("no glxinfo/eglinfo/vulkaninfo - install mesa-utils to check accel")


@check(tier=1, name="video_decode", desc="hardware video decode (VA-API)")
def video_decode(ctx):
    if not ctx.have("vainfo"):
        return Skip("vainfo not installed (libva-utils) - cannot verify VA-API")
    r = ctx.run_in_session(["vainfo"], timeout=60)
    out = r.stdout + r.stderr
    if "VAProfile" not in out:
        return Warn(f"VA-API not usable: {out.strip().splitlines()[-1][:70] if out.strip() else 'no output'}")
    profiles = len(re.findall(r"VAProfile\w+", out))
    return Ok(f"VA-API working ({profiles} profile entries)", vaapi_profiles=profiles)
