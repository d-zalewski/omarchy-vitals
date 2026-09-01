"""Tier 1 - GPU and display.

On a desktop this is the most visible thing a kernel can break: a driver that
fails to bind, a render node that never appears, or a compositor that falls
back to software rendering. The last one is the nasty case, because the desktop
still "works" - just slowly and hot - so it is checked explicitly.
"""
from __future__ import annotations

import json
import re
import struct
import tempfile
from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check

DRM = Path("/sys/class/drm")
DRI = Path("/dev/dri")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# A PNG of a blank screen is a few KB at any resolution, because uniform rows
# filter to zeros and deflate to nothing. Measured on an Intel UHD 600 desktop:
# a blank 1920x1080 frame is 6.1KB, the same output awake is 96KB, and a 4K
# output awake is 287KB. 16KB sits an order of magnitude from either side.
BLANK_FRAME_BYTES = 16 * 1024
# grim speaks wlr-screencopy, which the GNOME and KDE compositors do not
# implement. On those a failed capture says nothing about the desktop.
WLROOTS = ("Hyprland", "sway")
PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SOFTWARE_RENDERER = re.compile(r"llvmpipe|softpipe|swrast", re.I)
# Wayland builds first: the unsuffixed binary is the GLX one, which cannot
# open a canvas in a Wayland session however healthy the GPU is.
GLMARK2_BINARIES = ("glmark2-wayland", "glmark2-es2-wayland", "glmark2")


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
        if SOFTWARE_RENDERER.search(renderer):
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


def _preferred_output(ctx):
    """(output to capture, whether the compositor calls it awake).

    grim blocks until a frame arrives, and an output that is not rendering
    never sends one, so asking for the whole layout hangs on any machine with
    a second monitor asleep. Prefer an awake output and name it explicitly.

    dpmsStatus is a preference, not a veto: the VNC output this was tested
    against reports asleep and still delivers a frame instantly. It is used
    again afterwards, to tell a blank frame that is expected from one that is
    a finding. (None, None) means the compositor could not say, and grim gets
    the whole layout as before.
    """
    if ctx.session_env().get("_compositor") != "Hyprland":
        return None, None
    r = ctx.run_in_session(["hyprctl", "-j", "monitors"])
    if r.returncode != 0:
        return None, None
    try:
        monitors = json.loads(r.stdout)
    except ValueError:
        return None, None
    awake = [m.get("name") for m in monitors if m.get("dpmsStatus")]
    if awake:
        return awake[0], True
    names = [m.get("name") for m in monitors]
    return (names[0], False) if names else (None, None)


@check(tier=1, name="screenshot", desc="compositor produces a real frame",
       requires=["grim"], est_seconds=5)
def screenshot(ctx):
    """Ask for the pixels instead of inferring them.

    Every other check here reasons about the desktop from one layer down - a
    driver bound, a render node present, a compositor answering. This one takes
    a frame, which is the only thing that proves all of them at once, and the
    only one that would notice a session that is alive and displaying nothing.
    Works over SSH: grim talks to the compositor, not to a login.

    The frame lands in a temporary directory and is deleted with it. Its size
    and dimensions are the only things that leave this function - the contents
    of somebody's screen are not report material.
    """
    env = ctx.session_env()
    if not env:
        return Skip("no Wayland compositor running (headless or TTY only)")
    comp = env.get("_compositor", "?")
    if comp not in WLROOTS:
        return Skip(f"{comp} does not implement wlr-screencopy - grim cannot "
                    f"capture from it")
    output, awake = _preferred_output(ctx)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "frame.png"
        grim = ["grim", *(["-o", output] if output else []), str(out)]
        r = ctx.run_in_session(grim, timeout=20)
        if r.returncode == 124:
            # Blocked waiting for a frame that never came. On a working desktop
            # that means an output stopped rendering, not a broken compositor.
            return Skip(f"no frame from {output or 'the layout'} within 20s - "
                        f"the output is asleep or not rendering")
        if r.returncode != 0 or not out.exists():
            detail = (r.stderr or r.stdout).strip().splitlines()
            return Fail(f"grim captured nothing: "
                        f"{detail[-1][:70] if detail else 'no output file'}")
        data = out.read_bytes()
    if len(data) < 24 or not data.startswith(PNG_MAGIC):
        return Fail(f"grim wrote {len(data)} byte(s) that are not a PNG")
    width, height = struct.unpack(">II", data[16:24])   # IHDR, after the magic
    kb = len(data) // 1024
    if not width or not height:
        return Fail(f"captured frame has no dimensions ({width}x{height})")
    if len(data) < BLANK_FRAME_BYTES:
        if awake is False:
            # A blank frame from a screen the compositor has already turned off
            # is the expected answer, not a finding.
            return Skip(f"{width}x{height} frame is blank and {output} is "
                        f"asleep - nothing is being rendered to capture")
        return Warn(f"{width}x{height} frame is only {kb}KB - screen blanked, "
                    f"or the compositor is rendering nothing",
                    screenshot_pixels=width * height)
    return Ok(f"{width}x{height} frame captured{f' from {output}' if output else ''}, "
              f"{kb}KB compressed", screenshot_pixels=width * height)


@check(tier=1, name="screencast_portal", desc="screen sharing portal answers",
       requires=["busctl"])
def screencast_portal(ctx):
    """The path every screen share takes, and the one nothing else covers.

    It breaks quietly - the portal service fails to start, or pipewire is not
    where it expects - and the first anyone knows is a black window in a call.
    """
    if not ctx.session_env():
        return Skip("no desktop session to ask")
    r = ctx.run_in_session(
        ["busctl", "--user", "call", PORTAL_BUS, PORTAL_PATH,
         "org.freedesktop.DBus.Properties", "Get", "ss",
         "org.freedesktop.portal.ScreenCast", "version"], timeout=30)
    out = (r.stdout + r.stderr).strip()
    last = out.splitlines()[-1][:70] if out else "no output"
    if r.returncode != 0:
        # An uninstalled portal is absent hardware, not a broken one.
        if "not provided by any .service" in out or "ServiceUnknown" in out:
            return Skip("xdg-desktop-portal not installed")
        return Warn(f"ScreenCast portal did not answer: {last}",
                    screencast_portal=0)
    version = re.search(r"\bu\s+(\d+)", r.stdout)      # reply is: v u <n>
    if not version:
        return Warn(f"portal answered but the reply did not parse: {last}",
                    screencast_portal=0)
    return Ok(f"ScreenCast portal answers, interface version {version.group(1)}",
              screencast_portal=1)


@check(tier=1, name="gl_render", desc="GPU renders frames offscreen",
       est_seconds=30)
def gl_render(ctx):
    """Submit frames rather than read a renderer string.

    gpu_accel asks the driver what it is. This asks it to draw, offscreen, so
    it disturbs nothing on screen. A stack that names a hardware renderer and
    then cannot render is the failure the string cannot see.

    The binary matters: plain `glmark2` is the GLX build and dies with "Could
    not initialize canvas" on a Wayland session, so the wayland builds are
    preferred and the X11 one is the fallback.
    """
    if not (DRI.exists() and list(DRI.glob("renderD*"))):
        return Skip("no render node to draw with")
    binary = next((b for b in GLMARK2_BINARIES if ctx.have(b)), None)
    if binary is None:
        return Skip("no glmark2 build installed - cannot submit frames")
    r = ctx.run_in_session([binary, "--off-screen", "-b", "build:duration=2"],
                           timeout=120)
    out = r.stdout + r.stderr
    renderer = re.search(r"GL_RENDERER:\s*(.+)", out)
    name = renderer.group(1).strip() if renderer else ""
    if name and SOFTWARE_RENDERER.search(name):
        return Fail(f"frames rendered in software: {name[:60]}", glmark2_score=0)
    score = re.search(r"glmark2 Score:\s*(\d+)", out)
    if not score:
        # Offscreen GL has its own packaging quirks, so a run that never got
        # going is reported as suspicious rather than as a broken GPU - the
        # renderer string above is what fails outright.
        detail = out.strip().splitlines()
        return Warn(f"glmark2 produced no score: "
                    f"{detail[-1][:70] if detail else 'no output'}")
    n = int(score.group(1))
    if n == 0:
        return Fail("glmark2 scored 0 - no frames were rendered", glmark2_score=0)
    return Ok(f"offscreen GL rendered, {binary} score {n}"
              f"{f' on {name[:40]}' if name else ''}", glmark2_score=n)
