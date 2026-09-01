"""Boot-chain checks: Secure Boot, signed EFI binaries, TPM, LUKS unlock."""
from __future__ import annotations

import unittest

from fakefs import fake_fs  # noqa: E402
from helpers import FakeContext, cp  # noqa: E402

from vitals.checks import secureboot as sb  # noqa: E402
from vitals.core import Status  # noqa: E402

EFI = "/sys/firmware/efi/efivars"


def efivar(value: int) -> str:
    """Four attribute bytes then the value, the way efivarfs presents it."""
    return "\x06\x00\x00\x00" + chr(value)


def efi_tree(secure=None, setup=None, extra=None):
    tree = {"/sys/firmware/efi": ["efivars"]}
    for name, value in (("SecureBoot", secure), ("SetupMode", setup)):
        if value is not None:
            tree[f"{EFI}/{name}-{sb.EFI_GLOBAL}"] = efivar(value)
    tree.update(extra or {})
    return tree


class TestSecureBoot(unittest.TestCase):
    def run_check(self, tree):
        with fake_fs(tree):
            return sb.secure_boot(FakeContext())

    def test_legacy_bios_skips(self):
        self.assertIs(self.run_check({}).status, Status.SKIP)

    def test_variable_not_exposed_skips(self):
        self.assertIs(self.run_check(efi_tree()).status, Status.SKIP)

    def test_truncated_variable_skips(self):
        tree = efi_tree(extra={f"{EFI}/SecureBoot-{sb.EFI_GLOBAL}": "\x06\x00"})
        self.assertIs(self.run_check(tree).status, Status.SKIP)

    def test_enabled_passes(self):
        r = self.run_check(efi_tree(secure=1, setup=0))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["secure_boot"], 1)

    def test_enabled_but_in_setup_mode_warns(self):
        r = self.run_check(efi_tree(secure=1, setup=1))
        self.assertIs(r.status, Status.WARN)
        self.assertIn("Setup Mode", r.message)

    def test_setup_mode_mid_enrollment_warns(self):
        r = self.run_check(efi_tree(secure=0, setup=1))
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["secure_boot"], 0)

    def test_supported_but_off_is_info_not_a_failure(self):
        # Most machines are here and are fine; compare() is what turns a
        # 1 -> 0 flip into a regression.
        r = self.run_check(efi_tree(secure=0, setup=0))
        self.assertIs(r.status, Status.INFO)
        self.assertEqual(r.metrics["secure_boot"], 0)


class TestEfiSignatures(unittest.TestCase):
    signed = ("Verifying file database and EFI images in /boot...\n"
              "✓ /boot/EFI/BOOT/BOOTX64.EFI is signed\n"
              "✓ /boot/EFI/Linux/omarchy_linux.efi is signed\n")

    def run_check(self, out, rc=0, err=""):
        return sb.efi_signatures(FakeContext(commands={"sbctl verify": cp(out, rc, err)}))

    def test_sudo_refusal_skips(self):
        r = self.run_check("", 1, "sudo: a password is required")
        self.assertIs(r.status, Status.SKIP)

    def test_no_binaries_skips(self):
        self.assertIs(self.run_check("Verifying file database...\n").status,
                      Status.SKIP)

    def test_all_signed_passes(self):
        r = self.run_check(self.signed)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["efi_unsigned"], 0)

    def test_unsigned_binary_fails(self):
        # what limine-update leaves behind
        r = self.run_check(self.signed +
                           "✗ /boot/EFI/BOOT/BOOTX64.EFI is not signed\n")
        self.assertIs(r.status, Status.FAIL)
        self.assertEqual(r.metrics["efi_unsigned"], 1)
        self.assertIn("BOOTX64.EFI", r.message)
        self.assertIn("sbctl sign-all", r.message)


class TestTpm(unittest.TestCase):
    root = "/sys/class/tpm"

    def run_check(self, tree, files=None):
        with fake_fs(tree):
            return sb.tpm(FakeContext(files=files or {}))

    def test_no_tpm_skips(self):
        self.assertIs(self.run_check({}).status, Status.SKIP)

    def test_empty_class_dir_skips(self):
        self.assertIs(self.run_check({self.root: []}).status, Status.SKIP)

    def test_tpm_1_2_is_info(self):
        r = self.run_check({self.root: ["tpm0"]},
                           files={f"{self.root}/tpm0/tpm_version_major": "1"})
        self.assertIs(r.status, Status.INFO)

    def test_no_pcr_interface_is_info(self):
        r = self.run_check({self.root: ["tpm0"]},
                           files={f"{self.root}/tpm0/tpm_version_major": "2"})
        self.assertIs(r.status, Status.INFO)
        self.assertIn("does not expose PCRs", r.message)

    def test_readable_pcr_passes(self):
        r = self.run_check(
            {self.root: ["tpm0"]},
            files={f"{self.root}/tpm0/tpm_version_major": "2",
                   f"{self.root}/tpm0/pcr-sha256/7": "4949DA80B59C86AB"})
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["tpm_version"], 2)
        # the digest fingerprints the machine, so it must not be reported
        self.assertNotIn("4949DA80", r.message)


LSBLK = ("/dev/sda      \n"
         "/dev/sda1     vfat\n"
         "/dev/sda2     crypto_LUKS\n")


def luks_dump(keyslots=2, token="systemd-tpm2"):
    out = ["LUKS header information", "Version:        2", "Data segments:",
           "  0: crypt", "        offset: 16777216 [bytes]", "Keyslots:"]
    for i in range(keyslots):
        out += [f"  {i}: luks2", "        Key:        512 bits"]
    out.append("Tokens:")
    if token:
        out.append(f"  0: {token}")
    out += ["Digests:", "  0: pbkdf2", "        Hash:       sha512"]
    return "\n".join(out) + "\n"


class TestLuksTpm(unittest.TestCase):
    def run_check(self, lsblk=LSBLK, dump=None, rc=0):
        cmds = {"lsblk": cp(lsblk),
                "luksDump": cp(dump if dump is not None else luks_dump(), rc)}
        return sb.luks_tpm(FakeContext(commands=cmds))

    def test_no_luks_skips(self):
        self.assertIs(self.run_check(lsblk="/dev/sda1  vfat\n").status, Status.SKIP)

    def test_unreadable_header_skips(self):
        self.assertIs(self.run_check(rc=1).status, Status.SKIP)

    def test_no_tpm_token_is_info(self):
        r = self.run_check(dump=luks_dump(token=None))
        self.assertIs(r.status, Status.INFO)
        self.assertEqual(r.metrics["luks_tpm_token"], 0)

    def test_tpm_without_fallback_warns(self):
        r = self.run_check(dump=luks_dump(keyslots=1))
        self.assertIs(r.status, Status.WARN)
        self.assertIn("only way in", r.message)
        self.assertEqual(r.metrics["luks_tpm_token"], 1)

    def test_tpm_with_passphrase_fallback_passes(self):
        r = self.run_check()
        self.assertIs(r.status, Status.PASS)
        self.assertIn("sda2", r.message)
        self.assertEqual(r.metrics["luks_tpm_token"], 1)


if __name__ == "__main__":
    unittest.main()
