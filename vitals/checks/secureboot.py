"""Tier 0/1 - the boot chain: Secure Boot, signed binaries, TPM, LUKS.

A kernel or bootloader update can quietly undo the chain that lets a headless
machine come back on its own, and none of it shows up until the reboot:

  * secure_boot    - firmware state, read straight from the EFI variables.
    Reported as a metric rather than a failure, so a machine that never
    enabled Secure Boot stays quiet while one that had it and lost it turns
    up as a regression in `compare`.
  * efi_signatures - bootloader updates characteristically replace a signed
    binary with an unsigned copy; limine-update does this to
    /boot/EFI/BOOT/BOOTX64.EFI. Firmware then refuses it at the next boot.
  * tpm            - the device an unattended unlock depends on, and whether
    the kernel can read a PCR at all.
  * luks_tpm       - a TPM2 token *and* a second keyslot. TPM-only means the
    next firmware update is an unbootable machine.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check, sudo_refused

# The vendor GUID every UEFI implementation uses for the global variables.
EFI_GLOBAL = "8be4df61-93ca-11d2-aa0d-00e098032b8c"


def _efivar_flag(name: str):
    """Value of a one-byte EFI variable, or None if it is not exposed.

    Each efivars file is four attribute bytes followed by the value, so the
    fifth byte is the one that means anything.
    """
    try:
        raw = Path(f"/sys/firmware/efi/efivars/{name}-{EFI_GLOBAL}").read_bytes()
    except Exception:                                  # noqa: BLE001
        return None
    return raw[4] if len(raw) > 4 else None


@check(tier=0, name="secure_boot", desc="Secure Boot state as the firmware reports it")
def secure_boot(ctx):
    """Deliberately never fails.

    Most machines run with Secure Boot off and are fine. Recording it as a
    metric means `compare` calls a 1 -> 0 flip a regression on the machines
    where it matters, without nagging the ones where it does not.
    """
    if not Path("/sys/firmware/efi").exists():
        return Skip("booted through legacy BIOS, no Secure Boot")
    enabled = _efivar_flag("SecureBoot")
    if enabled is None:
        return Skip("firmware does not expose the SecureBoot variable")
    setup = _efivar_flag("SetupMode")
    if enabled:
        if setup:
            return Warn("Secure Boot on but firmware is in Setup Mode - "
                        "the key database can be rewritten", secure_boot=1)
        return Ok("enabled, Setup Mode off", secure_boot=1)
    if setup:
        return Warn("Setup Mode - platform keys are cleared, enrollment is "
                    "half finished", secure_boot=0)
    return Info("supported but not enabled", secure_boot=0)


@check(tier=0, name="efi_signatures", desc="every EFI binary in /boot is signed",
       requires=["sbctl"], est_seconds=3)
def efi_signatures(ctx):
    """Catches the unsigned copy a bootloader update leaves behind.

    Only meaningful with Secure Boot on, but it is cheap and an unsigned
    binary is worth knowing about before enabling it, so it runs either way.
    """
    r = ctx.sudo(["sbctl", "verify"], timeout=60)
    out = r.stdout + r.stderr
    if sudo_refused(r):
        return Skip("needs passwordless sudo to read /boot")
    unsigned = [l.split()[1] for l in out.splitlines()
                if "is not signed" in l and len(l.split()) > 1]
    signed = [l for l in out.splitlines() if "is signed" in l]
    if not signed and not unsigned:
        return Skip("sbctl reported no EFI binaries")
    if unsigned:
        names = ", ".join(Path(p).name for p in unsigned[:3])
        return Fail(f"{len(unsigned)} unsigned EFI binary(s): {names} - "
                    f"Secure Boot will refuse this at the next boot; "
                    f"run sbctl sign-all", efi_unsigned=len(unsigned))
    return Ok(f"all {len(signed)} EFI binaries signed", efi_unsigned=0)


@check(tier=1, name="tpm", desc="TPM present and the kernel can read a PCR")
def tpm(ctx):
    """A PCR read proves the driver works, not just that the device enumerated.

    The digest itself is never recorded: it is a stable per-machine value and
    these reports get committed.
    """
    devices = sorted(p.name for p in Path("/sys/class/tpm").glob("tpm*")) \
        if Path("/sys/class/tpm").is_dir() else []
    if not devices:
        return Skip("no TPM device")
    dev = Path("/sys/class/tpm") / devices[0]
    version = ctx.read(str(dev / "tpm_version_major"), "").strip()
    if version != "2":
        return Info(f"{devices[0]} present, TPM version {version or 'unknown'}")
    pcr = ctx.read(str(dev / "pcr-sha256" / "7"), "").strip()
    if not pcr:
        return Info(f"{devices[0]} is TPM 2.0, kernel does not expose PCRs")
    return Ok(f"{devices[0]} is TPM 2.0, PCR 7 readable", tpm_version=2)


@check(tier=1, name="luks_tpm", desc="LUKS unlocks unattended and still has a fallback")
def luks_tpm(ctx):
    """Both halves matter.

    A TPM2 token is what makes a headless box reboot without a console. A
    second keyslot is what saves it when a firmware update changes the PCRs
    the token is bound to.
    """
    out = ctx.run(["lsblk", "-no", "PATH,FSTYPE"], timeout=30).stdout
    devices = [l.split()[0] for l in out.splitlines() if "crypto_LUKS" in l]
    if not devices:
        return Skip("no LUKS volumes")

    tpm_backed, slots, examined = [], {}, 0
    for dev in devices:
        r = ctx.sudo(["cryptsetup", "luksDump", dev], timeout=30)
        if r.returncode != 0:
            continue
        examined += 1
        section, keyslots, tokens = None, 0, []
        for line in r.stdout.splitlines():
            if line and not line[0].isspace():
                section = line.split(":")[0].strip()
                continue
            m = re.match(r"\s+\d+:\s*(\S+)", line)
            if not m:
                continue
            if section == "Keyslots" and m.group(1).startswith("luks"):
                keyslots += 1
            elif section == "Tokens":
                tokens.append(m.group(1))
        name = Path(dev).name
        slots[name] = keyslots
        if any("tpm2" in t for t in tokens):
            tpm_backed.append(name)

    if examined == 0:
        return Skip("cannot read LUKS headers (needs passwordless sudo)")
    if not tpm_backed:
        return Info(f"{examined} LUKS volume(s), no TPM2 token - a passphrase "
                    f"is needed at every boot", luks_tpm_token=0)
    stranded = [n for n in tpm_backed if slots.get(n, 0) < 2]
    if stranded:
        return Warn(f"{', '.join(stranded)}: TPM2 is the only way in - a "
                    f"firmware update would leave this machine unbootable",
                    luks_tpm_token=1)
    return Ok(f"{', '.join(tpm_backed)}: TPM2 enrolled with a passphrase "
              f"fallback", luks_tpm_token=1)
