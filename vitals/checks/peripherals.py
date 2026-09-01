"""Tier 1 - USB, input devices, storage, webcam, thermal/power.

The things you notice within a minute of logging in if a kernel broke them:
the keyboard is dead, the disk is gone, the fan is screaming.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check


@check(tier=1, name="usb", desc="USB controllers and devices enumerate")
def usb(ctx):
    if not ctx.have("lsusb"):
        buses = list(Path("/sys/bus/usb/devices").glob("usb*")) \
            if Path("/sys/bus/usb/devices").exists() else []
        if not buses:
            return Warn("no USB buses found")
        return Ok(f"{len(buses)} USB bus(es) (install usbutils for detail)")
    out = ctx.run(["lsusb"]).stdout.strip().splitlines()
    if not out:
        return Fail("no USB devices enumerated - controller problem")
    roots = sum(1 for l in out if "root hub" in l.lower())
    devs = len(out) - roots
    errs = ctx.count_matches(ctx.journal_kernel,
                             r"usb .*: device descriptor read|"
                             r"unable to enumerate USB device|usb .*disconnect, reason")
    if errs:
        return Warn(f"{devs} device(s) on {roots} hub(s), but {errs} USB error(s)",
                    usb_devices=devs, usb_errors=errs)
    return Ok(f"{devs} device(s) on {roots} root hub(s)",
              usb_devices=devs, usb_errors=0)


@check(tier=1, name="input_devices", desc="keyboard / mouse / touchpad present")
def input_devices(ctx):
    p = Path("/proc/bus/input/devices")
    if not p.exists():
        return Skip("no /proc/bus/input/devices")
    text = p.read_text()
    kb = len(re.findall(r"EV=.*\n.*KEY=.*[1-9a-f]", text)) or text.count("kbd")
    mice = len(list(Path("/dev/input").glob("mouse*"))) if Path("/dev/input").exists() else 0
    touch = "Touchpad" in text or "touchpad" in text
    names = re.findall(r'N: Name="([^"]+)"', text)
    if not names:
        return Fail("no input devices - keyboard/mouse would be dead")
    bits = [f"{len(names)} device(s)"]
    if kb:
        bits.append("keyboard")
    if mice:
        bits.append(f"{mice} mouse node(s)")
    if touch:
        bits.append("touchpad")
    return Ok(", ".join(bits), input_devices=len(names))


@check(tier=1, name="storage", desc="block devices and filesystem mounted")
def storage(ctx):
    out = ctx.run(["lsblk", "-dno", "NAME,SIZE,TYPE"]).stdout.strip().splitlines()
    disks = [l for l in out if l.strip().endswith("disk")]
    if not disks:
        return Fail("no block devices - storage driver problem", disks=0)
    ioerr = ctx.count_matches(
        ctx.journal_kernel,
        r"I/O error|blk_update_request|ata\d+\.\d+: failed|nvme.*: I/O|"
        r"critical medium error")
    names = ", ".join(re.sub(r"\s+", " ", d).strip() for d in disks[:4])
    if ioerr:
        return Fail(f"{len(disks)} disk(s) but {ioerr} I/O error line(s): {names}",
                    disks=len(disks), io_errors=ioerr)
    return Ok(f"{len(disks)} disk(s): {names}", disks=len(disks), io_errors=0)


@check(tier=1, name="smart", desc="drive SMART health", requires=["smartctl"])
def smart(ctx):
    out = ctx.run(["lsblk", "-dno", "PATH,TYPE"]).stdout
    devs = [l.split()[0] for l in out.splitlines() if l.strip().endswith("disk")]
    if not devs:
        return Skip("no disks to query")
    bad = []
    checked = 0
    for d in devs:
        r = ctx.sudo(["smartctl", "-H", d], timeout=30)
        if "PASSED" in r.stdout or "OK" in r.stdout:
            checked += 1
        elif "FAILED" in r.stdout:
            bad.append(d)
    if bad:
        return Fail(f"SMART health FAILED on: {', '.join(bad)}")
    if checked == 0:
        return Skip("SMART not reported by any device")
    return Ok(f"SMART healthy on {checked} device(s)")


@check(tier=1, name="webcam", desc="video capture devices")
def webcam(ctx):
    devs = sorted(Path("/dev").glob("video*"))
    if not devs:
        return Skip("no /dev/video* - no capture hardware")
    if ctx.have("v4l2-ctl"):
        working = []
        for d in devs:
            r = ctx.run(["v4l2-ctl", "-d", str(d), "--info"], timeout=15)
            if r.returncode == 0 and "Card type" in r.stdout:
                m = re.search(r"Card type\s*:\s*(.+)", r.stdout)
                working.append(m.group(1).strip()[:30] if m else d.name)
        if working:
            return Ok(f"{len(working)} capture device(s): {', '.join(working[:2])}",
                      video_devices=len(working))
    return Ok(f"{len(devs)} /dev/video* node(s)", video_devices=len(devs))


@check(tier=1, name="thermal", desc="temperature sensors and current temps")
def thermal(ctx):
    zones = sorted(Path("/sys/class/thermal").glob("thermal_zone*")) \
        if Path("/sys/class/thermal").exists() else []
    if not zones:
        return Skip("no thermal zones exposed")
    temps = []
    for z in zones:
        try:
            t = int((z / "temp").read_text().strip()) / 1000.0
            typ = (z / "type").read_text().strip()
            temps.append((typ, t))
        except Exception:                              # noqa: BLE001
            continue
    if not temps:
        return Warn("thermal zones present but unreadable")
    hottest = max(temps, key=lambda x: x[1])
    desc = ", ".join(f"{t:.0f}C" for _n, t in temps)
    metrics = {"temp_max_c": round(hottest[1], 1)}
    if hottest[1] >= 95:
        return Warn(f"running hot: {hottest[0]} {hottest[1]:.0f}C ({desc})", **metrics)
    return Ok(f"{len(temps)} zone(s): {desc}", **metrics)


@check(tier=1, name="cpufreq", desc="CPU frequency scaling works")
def cpufreq(ctx):
    base = Path("/sys/devices/system/cpu/cpu0/cpufreq")
    if not base.exists():
        return Skip("no cpufreq interface (BIOS/ACPI managed?)")
    gov = ctx.read(str(base / "scaling_governor"), "?")
    cur = ctx.read(str(base / "scaling_cur_freq"), "0")
    mx = ctx.read(str(base / "scaling_max_freq"), "0")
    try:
        cur_mhz, max_mhz = int(cur) // 1000, int(mx) // 1000
    except ValueError:
        return Warn(f"governor {gov}, frequencies unreadable")
    return Ok(f"governor={gov}, {cur_mhz}/{max_mhz} MHz",
              cpu_cur_mhz=cur_mhz, cpu_max_mhz=max_mhz)


@check(tier=1, name="battery", desc="battery / AC power reporting")
def battery(ctx):
    ps = Path("/sys/class/power_supply")
    if not ps.exists() or not any(ps.iterdir()):
        return Skip("no power supply devices (desktop/mini PC)")
    bats = [p for p in ps.iterdir() if (p / "capacity").exists()]
    if not bats:
        acs = [p.name for p in ps.iterdir()]
        return Info(f"AC only: {', '.join(acs)}")
    b = bats[0]
    cap = ctx.read(str(b / "capacity"), "?")
    status = ctx.read(str(b / "status"), "?")
    return Ok(f"{b.name}: {cap}% ({status})", battery_pct=int(cap) if cap.isdigit() else 0)
