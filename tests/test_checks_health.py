"""Tier 0 health checks."""
from __future__ import annotations

import json
import unittest

from fakefs import fake_fs  # noqa: E402
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


class TestCpuVulnerabilities(unittest.TestCase):
    BASE = "/sys/devices/system/cpu/vulnerabilities"

    def ctx(self, cmdline="root=UUID=deadbeef rw quiet", **states):
        files = {f"{self.BASE}/{k}": v for k, v in states.items()}
        files["/proc/cmdline"] = cmdline
        return FakeContext(files=files)

    def tree(self, *names):
        return {self.BASE: list(names)}

    def test_absent_directory_skips(self):
        with fake_fs({}):
            self.assertIs(health.cpu_vulnerabilities(FakeContext()).status,
                          Status.SKIP)

    def test_unreadable_entries_skip(self):
        with fake_fs(self.tree("spectre_v2")):
            self.assertIs(health.cpu_vulnerabilities(FakeContext()).status,
                          Status.SKIP)

    def test_all_mitigated_passes(self):
        ctx = self.ctx(spectre_v2="Mitigation: Enhanced IBRS",
                       meltdown="Not affected")
        with fake_fs(self.tree("spectre_v2", "meltdown")):
            r = health.cpu_vulnerabilities(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["cpu_vulnerable"], 0)

    def test_unmitigated_warns(self):
        ctx = self.ctx(spectre_v2="Vulnerable: IBPB not enabled",
                       meltdown="Not affected")
        with fake_fs(self.tree("spectre_v2", "meltdown")):
            r = health.cpu_vulnerabilities(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["cpu_vulnerable"], 1)
        self.assertIn("spectre_v2", r.message)

    def test_mitigations_off_is_a_choice_not_a_warning(self):
        """A deliberate boot option must not read as a fault."""
        ctx = self.ctx(cmdline="root=UUID=deadbeef mitigations=off",
                       spectre_v2="Vulnerable", mds="Vulnerable")
        with fake_fs(self.tree("spectre_v2", "mds")):
            r = health.cpu_vulnerabilities(ctx)
        self.assertIs(r.status, Status.INFO)
        self.assertEqual(r.metrics["cpu_vulnerable"], 2)

    def test_command_line_is_never_quoted_back(self):
        ctx = self.ctx(spectre_v2="Vulnerable")
        with fake_fs(self.tree("spectre_v2")):
            r = health.cpu_vulnerabilities(ctx)
        self.assertNotIn("UUID", r.message)


if __name__ == "__main__":
    unittest.main()


def journal_json(*entries):
    """One JSON object per line, the way `journalctl -o json` emits them."""
    return "".join(json.dumps(e) + "\n" for e in entries)


class TestJournalErrors(unittest.TestCase):
    def run_check(self, stdout="", rc=0):
        return health.journal_errors(
            FakeContext(commands={"journalctl": cp(stdout, rc)}))

    def test_unreadable_journal_skips(self):
        self.assertIs(self.run_check(rc=1).status, Status.SKIP)

    def test_clean_boot_passes(self):
        r = self.run_check("")
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["journal_errors"], 0)

    def test_counts_by_source(self):
        out = journal_json(
            {"SYSLOG_IDENTIFIER": "kernel", "MESSAGE": "SGX disabled by BIOS"},
            {"SYSLOG_IDENTIFIER": "kernel", "MESSAGE": "TDX not supported"},
            {"SYSLOG_IDENTIFIER": "sddm-helper", "MESSAGE": "keyring locked"})
        r = self.run_check(out)
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["journal_errors"], 3)
        self.assertIn("kernel:2", r.message)
        # the message text itself never reaches the report
        self.assertNotIn("keyring", r.message)

    def test_ignores_the_suites_own_probe_crash(self):
        # stack_protector aborts a probe on purpose; systemd-coredump logs it.
        out = journal_json(
            {"SYSLOG_IDENTIFIER": "systemd-coredump",
             "MESSAGE": "Process 5848 (vitals-probe) terminated abnormally"})
        r = self.run_check(out)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["journal_errors"], 0)

    def test_malformed_and_binary_entries(self):
        out = ("not json\n" + journal_json(
            {"MESSAGE": [72, 105]},                    # binary message, no id
            {"SYSLOG_IDENTIFIER": "foo", "MESSAGE": None}))
        r = self.run_check(out)
        self.assertEqual(r.metrics["journal_errors"], 2)
        self.assertIn("unknown:1", r.message)
