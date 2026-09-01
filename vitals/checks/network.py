"""Tier 1 - ethernet, wifi, bluetooth.

Written for machines with any mix of these: a box with four NICs and no radio
should not fail wifi checks, and a laptop with wifi only should not fail
ethernet ones. Absent hardware is SKIP; present-but-broken hardware is FAIL.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check

NET = Path("/sys/class/net")


def _ifaces() -> list[str]:
    if not NET.exists():
        return []
    return sorted(p.name for p in NET.iterdir()
                  if p.name != "lo" and not p.name.startswith(("veth", "docker", "br-")))


def _is_wireless(name: str) -> bool:
    return (NET / name / "wireless").exists() or (NET / name / "phy80211").exists()


def _is_virtual(name: str) -> bool:
    try:
        return "virtual" in (NET / name).resolve().as_posix()
    except Exception:                                  # noqa: BLE001
        return False


def _operstate(name: str) -> str:
    try:
        return (NET / name / "operstate").read_text().strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


@check(tier=1, name="net_drivers", desc="network drivers bound")
def net_drivers(ctx):
    physical = [i for i in _ifaces() if not _is_virtual(i)]
    if not physical:
        return Fail("no physical network interfaces found", net_ifaces=0)
    drivers = {}
    for i in physical:
        drv = (NET / i / "device" / "driver")
        try:
            drivers[i] = drv.resolve().name
        except Exception:                              # noqa: BLE001
            drivers[i] = "?"
    unbound = [i for i, d in drivers.items() if d == "?"]
    desc = ", ".join(f"{i}({d})" for i, d in drivers.items())
    if unbound:
        return Fail(f"interface(s) without driver: {', '.join(unbound)} | {desc}",
                    net_ifaces=len(physical))
    return Ok(f"{len(physical)} NIC(s): {desc}", net_ifaces=len(physical))


@check(tier=1, name="ethernet_link", desc="at least one wired link up")
def ethernet_link(ctx):
    eth = [i for i in _ifaces() if not _is_wireless(i) and not _is_virtual(i)]
    if not eth:
        return Skip("no ethernet interfaces")
    up = [i for i in eth if _operstate(i) == "up"]
    details = []
    for i in up:
        speed = ""
        try:
            s = (NET / i / "speed").read_text().strip()
            speed = f"@{s}Mb" if s and s != "-1" else ""
        except Exception:                              # noqa: BLE001
            pass
        details.append(f"{i}{speed}")
    if not up:
        # All ports down is normal on a multi-NIC box with one cable, but if
        # nothing at all is up the machine has no wired connectivity.
        return Warn(f"no wired link up (of {len(eth)}: {', '.join(eth)})",
                    ethernet_up=0, ethernet_total=len(eth))
    return Ok(f"{len(up)}/{len(eth)} up: {', '.join(details)}",
              ethernet_up=len(up), ethernet_total=len(eth))


@check(tier=1, name="net_connectivity", desc="routing and DNS work")
def net_connectivity(ctx):
    """Reports reachability without recording the address.

    Reports get shared and committed, so they should not bake in someone's LAN
    topology. The gateway address is used and then discarded; only the fact
    that it answered is recorded.
    """
    gw = ctx.run(["ip", "route", "show", "default"]).stdout.strip()
    if not gw:
        return Warn("no default route")
    parts = gw.split()
    gw_ip = parts[2] if len(parts) > 2 else None
    iface = parts[4] if len(parts) > 4 else "?"
    if gw_ip:
        r = ctx.run(["ping", "-c", "2", "-W", "3", gw_ip], timeout=20)
        if r.returncode != 0:
            return Fail(f"default gateway unreachable via {iface}")
    dns = ctx.run(["getent", "hosts", "archlinux.org"], timeout=20)
    if dns.returncode != 0:
        return Warn(f"gateway reachable via {iface} but DNS resolution failed")
    return Ok(f"gateway reachable via {iface}, DNS resolves")


@check(tier=1, name="wifi", desc="wireless adapter and association")
def wifi(ctx):
    wl = [i for i in _ifaces() if _is_wireless(i)]
    if not wl:
        return Skip("no wireless hardware on this machine")
    results = []
    associated = 0
    for i in wl:
        if not ctx.have("iw"):
            results.append(f"{i}({_operstate(i)})")
            continue
        r = ctx.run(["iw", "dev", i, "link"], timeout=20)
        if "Connected to" in r.stdout:
            ssid = re.search(r"SSID:\s*(.+)", r.stdout)
            sig = re.search(r"signal:\s*(-?\d+)", r.stdout)
            associated += 1
            results.append(f"{i}->{ssid.group(1).strip() if ssid else '?'}"
                           f"{f' {sig.group(1)}dBm' if sig else ''}")
        else:
            results.append(f"{i}(not associated)")
    if associated == 0:
        return Warn(f"wifi present but not associated: {', '.join(results)}",
                    wifi_ifaces=len(wl), wifi_associated=0)
    return Ok(f"{', '.join(results)}", wifi_ifaces=len(wl),
              wifi_associated=associated)


@check(tier=1, name="wifi_scan", desc="wireless can scan for networks")
def wifi_scan(ctx):
    wl = [i for i in _ifaces() if _is_wireless(i)]
    if not wl:
        return Skip("no wireless hardware")
    if not ctx.have("iw"):
        return Skip("iw not installed")
    r = ctx.sudo(["iw", "dev", wl[0], "scan", "ap-force"], timeout=60)
    n = len(re.findall(r"^BSS ", r.stdout, re.MULTILINE))
    if r.returncode != 0 and n == 0:
        return Fail(f"scan failed on {wl[0]} - radio or driver problem")
    return Ok(f"{wl[0]} scanned, {n} AP(s) visible", wifi_aps_seen=n)


@check(tier=1, name="bluetooth", desc="bluetooth adapter present and up")
def bluetooth(ctx):
    hci = sorted(Path("/sys/class/bluetooth").glob("hci*")) \
        if Path("/sys/class/bluetooth").exists() else []
    if not hci:
        return Skip("no bluetooth hardware on this machine")
    blocked = ""
    if ctx.have("rfkill"):
        r = ctx.run(["rfkill", "list", "bluetooth"])
        if "Soft blocked: yes" in r.stdout or "Hard blocked: yes" in r.stdout:
            blocked = " (rfkill blocked)"
    svc = ctx.run(["systemctl", "is-active", "bluetooth"]).stdout.strip()
    if svc != "active":
        return Warn(f"{len(hci)} adapter(s) but bluetooth.service is {svc}{blocked}",
                    bt_adapters=len(hci))
    return Ok(f"{len(hci)} adapter(s), service active{blocked}",
              bt_adapters=len(hci))


@check(tier=1, name="net_errors", desc="NIC errors, resets, link flaps")
def net_errors(ctx):
    n = ctx.count_matches(
        ctx.journal_kernel,
        r"(tx|rx) (timeout|hang)|Reset adapter|NETDEV WATCHDOG|link is not ready|"
        r"transmit queue \d+ timed out")
    if n == 0:
        return Ok("no NIC resets/timeouts", net_errors=0)
    return Warn(f"{n} NIC error/reset line(s) - check dmesg", net_errors=n)


@check(tier=1, name="bluetooth_scan", desc="bluetooth discovery finds devices",
       requires=["bluetoothctl"], est_seconds=15)
def bluetooth_scan(ctx):
    """The radio, rather than the adapter node.

    `bluetooth` proves an adapter exists and the daemon is up. Neither of those
    needs the radio to work; scanning does. Counts only - never an address or
    a device name, which would put the neighbours' phones in a committed file.
    """
    hci = sorted(Path("/sys/class/bluetooth").glob("hci*")) \
        if Path("/sys/class/bluetooth").exists() else []
    if not hci:
        return Skip("no bluetooth hardware on this machine")
    r = ctx.run(["bluetoothctl", "--timeout", "10", "scan", "on"], timeout=45)
    out = r.stdout + r.stderr
    if r.returncode == 124:
        return Warn("bluetoothctl did not return - scan never finished")
    if "No default controller" in out:
        return Fail("adapter present but bluetoothd has no controller - "
                    "the radio is not usable")
    if r.returncode != 0:
        # Older bluez has no --timeout; without it the command never returns,
        # so report the version difference rather than hanging the tier.
        return Skip(f"bluetoothctl scan unavailable: "
                    f"{out.strip().splitlines()[-1][:70] if out.strip() else 'no output'}")
    seen = len(set(re.findall(r"Device ([0-9A-F:]{17})", out, re.IGNORECASE)))
    if seen == 0:
        # An empty room is not a fault, and neither is a shielded one.
        return Info("scan ran, no devices in range", bt_devices_seen=0)
    return Ok(f"scan found {seen} device(s) in range", bt_devices_seen=seen)
