"""Tier 0 health checks."""
from __future__ import annotations

import unittest

from helpers import FakeContext, cp  # noqa: E402

from vitals.checks import health  # noqa: E402
from vitals.core import Status  # noqa: E402


class TestTaint(unittest.TestCase):
    def test_untainted(self):
        r = health.taint(FakeContext(files={"/proc/sys/kernel/tainted": "0"}))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["taint"], 0)

    def test_fault_bits_fail(self):
        # bit 7 (D) = kernel died from an oops/BUG
        r = health.taint(FakeContext(files={"/proc/sys/kernel/tainted": "128"}))
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("kernel died", r.message)

    def test_machine_check_bit_fails(self):
        # bit 4 (M) = machine check exception
        r = health.taint(FakeContext(files={"/proc/sys/kernel/tainted": "16"}))
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("machine check", r.message)

    def test_benign_bits_only_warn(self):
        # bit 12 (O) = out-of-tree module, expected with DKMS
        r = health.taint(FakeContext(files={"/proc/sys/kernel/tainted": "4096"}))
        self.assertIs(r.status, Status.WARN)
        self.assertIn("out-of-tree", r.message)

    def test_unknown_bit_still_warns(self):
        r = health.taint(FakeContext(files={"/proc/sys/kernel/tainted": "1024"}))
        self.assertIs(r.status, Status.WARN)

    def test_missing_file_treated_as_zero(self):
        self.assertIs(health.taint(FakeContext()).status, Status.PASS)


class TestJournalScans(unittest.TestCase):
    def test_oops_clean_and_dirty(self):
        self.assertIs(health.oops(FakeContext(journal_kernel="all fine")).status,
                      Status.PASS)
        r = health.oops(FakeContext(journal_kernel="BUG: unable to handle\nOops: 0002"))
        self.assertIs(r.status, Status.FAIL)
        self.assertEqual(r.metrics["oops_count"], 2)

    def test_warnings(self):
        self.assertIs(health.warnings(FakeContext(journal_kernel="")).status,
                      Status.PASS)
        r = health.warnings(FakeContext(journal_kernel="WARNING: CPU: 0 PID: 1"))
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["warn_count"], 1)

    def test_mce(self):
        self.assertIs(health.mce(FakeContext(journal_kernel="")).status, Status.PASS)
        r = health.mce(FakeContext(journal_kernel="mce: [Hardware Error]"))
        self.assertIs(r.status, Status.FAIL)

    def test_probe_failures(self):
        self.assertIs(health.probe_failures(FakeContext(journal_kernel="")).status,
                      Status.PASS)
        r = health.probe_failures(
            FakeContext(journal_kernel="probe with driver foo failed"))
        self.assertIs(r.status, Status.FAIL)

    def test_missing_firmware_is_info_not_failure(self):
        self.assertIs(health.firmware(FakeContext(journal_kernel="")).status,
                      Status.PASS)
        r = health.firmware(
            FakeContext(journal_kernel="Possibly missing firmware for module: x"))
        self.assertIs(r.status, Status.INFO)
        self.assertEqual(r.metrics["missing_firmware"], 1)


class TestSystemd(unittest.TestCase):
    def test_no_failed_units(self):
        r = health.failed_units(FakeContext(commands={"systemctl": cp("")}))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["failed_units"], 0)

    def test_failed_units_named(self):
        out = "foo.service loaded failed failed Foo\nbar.timer loaded failed failed Bar"
        r = health.failed_units(FakeContext(commands={"systemctl": cp(out)}))
        self.assertIs(r.status, Status.FAIL)
        self.assertEqual(r.metrics["failed_units"], 2)
        self.assertIn("foo.service", r.message)


class TestModulesAndBoot(unittest.TestCase):
    def test_module_count_excludes_header(self):
        listing = "Module Size Used\na 1 0\nb 2 0\n"
        r = health.modules(FakeContext(commands={"lsmod": cp(listing)}))
        self.assertIs(r.status, Status.INFO)
        self.assertEqual(r.metrics["modules_loaded"], 2)

    def test_module_count_empty_output(self):
        r = health.modules(FakeContext(commands={"lsmod": cp("")}))
        self.assertEqual(r.metrics["modules_loaded"], 0)

    def test_boot_time_parsed(self):
        out = ("Startup finished in 1.5s (kernel) + 2.25s (initrd) + "
               "3.0s (userspace) = 6.75s")
        r = health.boot_time(FakeContext(commands={"systemd-analyze": cp(out)}))
        self.assertEqual(r.metrics["boot_kernel_ms"], 1500)
        self.assertEqual(r.metrics["boot_initrd_ms"], 2250)
        self.assertEqual(r.metrics["boot_userspace_ms"], 3000)

    def test_boot_time_unavailable(self):
        r = health.boot_time(FakeContext(commands={"systemd-analyze": cp("")}))
        self.assertIs(r.status, Status.INFO)
        self.assertNotIn("boot_kernel_ms", r.metrics)


if __name__ == "__main__":
    unittest.main()
