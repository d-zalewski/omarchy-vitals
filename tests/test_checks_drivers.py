"""Tier 1 driver checks: modules load, DKMS is current, devices are bound."""
from __future__ import annotations

import unittest
from unittest import mock

from fakefs import fake_fs  # noqa: E402
from helpers import FakeContext, cp  # noqa: E402

from vitals.checks import drivers  # noqa: E402
from vitals.core import Status  # noqa: E402

RUNNING = "7.2.2-5-omarchy-bore"
HEADER = "Module                  Size  Used by\n"
NOTHING_LOADED = HEADER + "kvm_amd               196608  0\n"
DUMMY_LOADED = NOTHING_LOADED + "dummy                  12288  0\n"
IS_A_MODULE = cp("/usr/lib/modules/7.2.2/kernel/drivers/net/dummy.ko.zst\n")


def lsmod_sequence(*outputs):
    """lsmod is called before and after modprobe; give each call its own answer."""
    seq = iter(outputs)
    last = [outputs[-1]]

    def next_result():
        try:
            last[0] = next(seq)
        except StopIteration:
            pass
        return cp(last[0])
    return next_result


class TestModuleLoad(unittest.TestCase):
    def test_all_probe_modules_already_loaded_skips(self):
        loaded = HEADER + "".join(f"{m} 1 0\n" for m in drivers.PROBE_MODULES)
        r = drivers.module_load(FakeContext(commands={"lsmod": cp(loaded)}))
        self.assertIs(r.status, Status.SKIP)

    def test_builtin_probe_module_is_not_a_failure(self):
        # dummy compiled in rather than built as a module: modprobe would
        # succeed and lsmod would show nothing, which is not a fault.
        ctx = FakeContext(commands={"lsmod": cp(NOTHING_LOADED),
                                    "modinfo -F filename": cp("(builtin)\n")})
        self.assertIs(drivers.module_load(ctx).status, Status.SKIP)

    def test_sudo_refusal_skips(self):
        ctx = FakeContext(commands={
            "modinfo -F filename": IS_A_MODULE,
            "lsmod": cp(NOTHING_LOADED),
            "modprobe": cp("", 1, "sudo: a password is required")})
        self.assertIs(drivers.module_load(ctx).status, Status.SKIP)

    def test_rejected_signature_explains_itself(self):
        ctx = FakeContext(commands={
            "modinfo -F filename": IS_A_MODULE,
            "lsmod": cp(NOTHING_LOADED),
            "modprobe": cp("", 1, "modprobe: ERROR: could not insert 'dummy': "
                                  "Key was rejected by service")})
        r = drivers.module_load(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("enforces signatures", r.message)

    def test_vermagic_mismatch_explains_itself(self):
        ctx = FakeContext(commands={
            "modinfo -F filename": IS_A_MODULE,
            "lsmod": cp(NOTHING_LOADED),
            "modprobe": cp("", 1, "modprobe: ERROR: could not insert 'dummy': "
                                  "Invalid module format")})
        r = drivers.module_load(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("vermagic", r.message)

    def test_silent_failure_reports_return_code(self):
        ctx = FakeContext(commands={"modinfo -F filename": IS_A_MODULE,
                                    "lsmod": cp(NOTHING_LOADED),
                                    "modprobe": cp("", 1)})
        r = drivers.module_load(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("rc=1", r.message)

    def test_load_reported_but_module_absent_fails(self):
        ctx = FakeContext(commands={"modinfo -F filename": IS_A_MODULE,
                                    "lsmod": cp(NOTHING_LOADED),
                                    "modprobe": cp("", 0)})
        r = drivers.module_load(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("not loaded", r.message)

    def test_stuck_module_warns(self):
        ctx = FakeContext(commands={
            "modinfo -F filename": IS_A_MODULE,
            "modprobe -r": cp("", 1),
            "modprobe": cp("", 0),
            "lsmod": lsmod_sequence(NOTHING_LOADED, DUMMY_LOADED)})
        r = drivers.module_load(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertIn("would not unload", r.message)

    def test_round_trip_passes(self):
        ctx = FakeContext(commands={
            "modinfo -F filename": IS_A_MODULE,
            "modprobe -r": cp("", 0),
            "modprobe": cp("", 0),
            "lsmod": lsmod_sequence(NOTHING_LOADED, DUMMY_LOADED)})
        r = drivers.module_load(ctx)
        self.assertIs(r.status, Status.PASS)
        # numdummies=0 keeps the load from creating a network interface.
        self.assertIn("modprobe dummy numdummies=0", ctx.calls)


class TestModuleSig(unittest.TestCase):
    def test_no_modules_loaded_skips(self):
        ctx = FakeContext(commands={"lsmod": cp(HEADER)})
        self.assertIs(drivers.module_sig(ctx).status, Status.SKIP)

    def test_signed_and_enforced_passes(self):
        ctx = FakeContext(
            commands={"lsmod": cp(NOTHING_LOADED),
                      "sig_hashalgo": cp("sha512\n")},
            files={"/sys/module/module/parameters/sig_enforce": "Y"})
        r = drivers.module_sig(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertIn("enforcement on", r.message)
        self.assertEqual(r.metrics["module_signed"], 1)

    def test_signed_without_enforcement_passes(self):
        ctx = FakeContext(commands={"lsmod": cp(NOTHING_LOADED),
                                    "sig_hashalgo": cp("sha256\n")})
        r = drivers.module_sig(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertIn("enforcement off", r.message)

    def test_unsigned_while_enforcing_fails(self):
        ctx = FakeContext(
            commands={"lsmod": cp(NOTHING_LOADED),
                      "sig_hashalgo": cp("(none)\n")},
            files={"/sys/module/module/parameters/sig_enforce": "Y"})
        r = drivers.module_sig(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertEqual(r.metrics["module_signed"], 0)

    def test_unsigned_without_enforcement_is_info(self):
        ctx = FakeContext(commands={"lsmod": cp(NOTHING_LOADED),
                                    "sig_hashalgo": cp("")})
        self.assertIs(drivers.module_sig(ctx).status, Status.INFO)


class TestDkms(unittest.TestCase):
    def run_check(self, output):
        ctx = FakeContext(commands={"dkms status": cp(output)})
        with mock.patch("platform.release", return_value=RUNNING):
            return drivers.dkms(ctx)

    def test_nothing_registered_skips(self):
        self.assertIs(self.run_check("").status, Status.SKIP)

    def test_built_for_running_kernel_passes(self):
        r = self.run_check(f"nvidia/550.107.02, {RUNNING}, x86_64: installed\n")
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["dkms_missing"], 0)

    def test_built_only_for_the_previous_kernel_fails(self):
        r = self.run_check("nvidia/550.107.02, 7.1.11-arch1-1, x86_64: installed\n")
        self.assertIs(r.status, Status.FAIL)
        self.assertEqual(r.metrics["dkms_missing"], 1)
        self.assertIn("this driver is", r.message)

    def test_several_missing_reads_as_plural(self):
        r = self.run_check(
            "nvidia/550.107.02, 7.1.11-arch1-1, x86_64: installed\n"
            "v4l2loopback/0.13.2, 7.1.11-arch1-1, x86_64: installed\n")
        self.assertIs(r.status, Status.FAIL)
        self.assertEqual(r.metrics["dkms_missing"], 2)
        self.assertIn("these drivers are", r.message)

    def test_added_but_not_installed_counts_as_missing(self):
        r = self.run_check(f"v4l2loopback/0.13.2, {RUNNING}, x86_64: added\n")
        self.assertIs(r.status, Status.FAIL)


class TestPciDrivers(unittest.TestCase):
    root = "/sys/bus/pci/devices"

    def device(self, addr, cls, bound=True, vendor="0x8086", device="0x2725"):
        tree = {f"{self.root}/{addr}": []}
        if bound:
            tree[f"{self.root}/{addr}/driver"] = []
        files = {f"{self.root}/{addr}/class": cls,
                 f"{self.root}/{addr}/vendor": vendor,
                 f"{self.root}/{addr}/device": device}
        return tree, files

    def run_check(self, *devices):
        tree, files = {self.root: [a for a, _, _ in devices]}, {}
        for addr, cls, bound in devices:
            t, f = self.device(addr, cls, bound)
            tree.update(t)
            files.update(f)
        with fake_fs(tree):
            return drivers.pci_drivers(FakeContext(files=files))

    def test_no_pci_bus_skips(self):
        with fake_fs({}):
            self.assertIs(drivers.pci_drivers(FakeContext()).status, Status.SKIP)

    def test_only_uninteresting_classes_skips(self):
        # A host bridge with nothing bound is normal, not a finding.
        self.assertIs(self.run_check(("0000:00:00.0", "0x060000", False)).status,
                      Status.SKIP)

    def test_all_bound_passes(self):
        r = self.run_check(("0000:01:00.0", "0x010802", True),
                           ("0000:00:1f.3", "0x040300", True))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["pci_unbound"], 0)

    def test_unbound_wireless_warns(self):
        r = self.run_check(("0000:01:00.0", "0x010802", True),
                           ("0000:02:00.0", "0x028000", False))
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["pci_unbound"], 1)
        self.assertIn("wireless [8086:2725]", r.message)


if __name__ == "__main__":
    unittest.main()
