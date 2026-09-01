"""Kernel features userspace depends on: namespaces, seccomp, cgroups, KVM."""
from __future__ import annotations

import unittest
from unittest import mock

from fakefs import fake_fs  # noqa: E402
from helpers import FakeContext, cp  # noqa: E402

from vitals.checks import kernel_features as kf  # noqa: E402
from vitals.core import Status  # noqa: E402


class TestUserNamespaces(unittest.TestCase):
    def test_working_passes(self):
        r = kf.user_namespaces(FakeContext(commands={"unshare": cp("", 0)}))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["user_ns"], 1)

    def test_disabled_by_sysctl_says_so(self):
        ctx = FakeContext(commands={"unshare": cp("", 1, "denied")},
                          files={"/proc/sys/user/max_user_namespaces": "0"})
        r = kf.user_namespaces(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("max_user_namespaces=0", r.message)

    def test_other_failure_reports_the_error(self):
        ctx = FakeContext(commands={"unshare": cp("", 1, "Operation not permitted")})
        r = kf.user_namespaces(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("not permitted", r.message)
        self.assertEqual(r.metrics["user_ns"], 0)


class TestOverlayfs(unittest.TestCase):
    def test_mount_succeeds(self):
        r = kf.overlayfs(FakeContext(commands={"mount -t overlay": cp("", 0)}))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["overlayfs"], 1)

    def test_module_present_but_unprivileged_mount_refused_warns(self):
        # /proc/filesystems is not consulted as a negative: overlay autoloads
        # on first use, so it is absent on machines where it works.
        ctx = FakeContext(commands={"mount -t overlay": cp("", 1, "denied"),
                                    "modinfo": cp("/lib/modules/x/overlay.ko.zst")})
        r = kf.overlayfs(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertIn("will not mount unprivileged", r.message)

    def test_builtin_overlay_also_warns(self):
        ctx = FakeContext(commands={"mount -t overlay": cp("", 1, "denied"),
                                    "modinfo": cp("(builtin)")},
                          files={"/proc/filesystems": "nodev overlay\n"})
        self.assertIs(kf.overlayfs(ctx).status, Status.WARN)

    def test_absent_overlayfs_fails(self):
        ctx = FakeContext(commands={"mount -t overlay": cp("", 1, "no such device"),
                                    "modinfo": cp("")})
        r = kf.overlayfs(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertEqual(r.metrics["overlayfs"], 0)


class TestSeccomp(unittest.TestCase):
    def test_no_compiler_falls_back_to_proc(self):
        ctx = FakeContext(files={"/proc/self/status": "Seccomp:\t0\n"})
        self.assertIs(kf.seccomp(ctx).status, Status.INFO)

    def test_no_compiler_and_no_seccomp_field_fails(self):
        r = kf.seccomp(FakeContext(files={"/proc/self/status": "Name:\tbash\n"}))
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("CONFIG_SECCOMP", r.message)

    def test_compile_failure_skips(self):
        ctx = FakeContext(tools=["gcc"])
        with mock.patch.object(kf, "compile_run", return_value=(False, None, "")):
            self.assertIs(kf.seccomp(ctx).status, Status.SKIP)

    def test_filter_installs(self):
        ctx = FakeContext(tools=["gcc"])
        with mock.patch.object(kf, "compile_run", return_value=(True, 0, "ok\n")):
            r = kf.seccomp(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["seccomp"], 1)

    def test_no_new_privs_rejected_fails(self):
        ctx = FakeContext(tools=["gcc"])
        with mock.patch.object(kf, "compile_run", return_value=(True, 1, "")):
            r = kf.seccomp(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("NO_NEW_PRIVS", r.message)

    def test_filter_rejected_fails(self):
        ctx = FakeContext(tools=["gcc"])
        with mock.patch.object(kf, "compile_run", return_value=(True, 2, "")):
            self.assertIs(kf.seccomp(ctx).status, Status.FAIL)


class TestCgroups(unittest.TestCase):
    full = "cpuset cpu io memory hugetlb pids rdma misc dmem"

    def test_all_controllers_pass(self):
        ctx = FakeContext(files={"/sys/fs/cgroup/cgroup.controllers": self.full})
        r = kf.cgroups(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["cgroup_controllers"], 9)

    def test_missing_memory_controller_fails(self):
        ctx = FakeContext(files={"/sys/fs/cgroup/cgroup.controllers": "cpu io pids"})
        r = kf.cgroups(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("memory", r.message)

    def test_cgroup_v1_warns(self):
        ctx = FakeContext(files={"/proc/self/cgroup": "11:devices:/user.slice"})
        r = kf.cgroups(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertIn("cgroup v1", r.message)

    def test_v2_without_controllers_warns(self):
        ctx = FakeContext(files={"/proc/self/cgroup": "0::/user.slice"})
        self.assertIs(kf.cgroups(ctx).status, Status.WARN)


class TestKvm(unittest.TestCase):
    def test_no_device_but_capable_cpu_warns(self):
        with fake_fs({}):
            r = kf.kvm(FakeContext(files={"/proc/cpuinfo": "flags : fpu vmx sse"}))
        self.assertIs(r.status, Status.WARN)
        self.assertIn("module is missing", r.message)

    def test_no_virtualisation_support_skips(self):
        with fake_fs({}):
            r = kf.kvm(FakeContext(files={"/proc/cpuinfo": "flags : fpu sse"}))
        self.assertIs(r.status, Status.SKIP)

    def test_usable_passes(self):
        with fake_fs({"/dev/kvm": ""}), \
             mock.patch.object(kf, "_kvm_probe", return_value=("ok", 12)):
            r = kf.kvm(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertIn("version 12", r.message)
        self.assertEqual(r.metrics["kvm"], 1)

    def test_permission_denied_warns(self):
        with fake_fs({"/dev/kvm": ""}), \
             mock.patch.object(kf, "_kvm_probe", return_value=("denied", None)):
            r = kf.kvm(FakeContext())
        self.assertIs(r.status, Status.WARN)
        self.assertIn("kvm group", r.message)

    def test_driver_error_fails(self):
        with fake_fs({"/dev/kvm": ""}), \
             mock.patch.object(kf, "_kvm_probe", return_value=("error", "ENODEV")):
            self.assertIs(kf.kvm(FakeContext()).status, Status.FAIL)


class TestKvmProbe(unittest.TestCase):
    def test_permission_denied(self):
        with mock.patch("os.open", side_effect=PermissionError):
            self.assertEqual(kf._kvm_probe()[0], "denied")

    def test_open_error(self):
        with mock.patch("os.open", side_effect=OSError("ENODEV")):
            self.assertEqual(kf._kvm_probe()[0], "error")

    def test_ioctl_error_still_closes(self):
        with mock.patch("os.open", return_value=7), \
             mock.patch("os.close") as closer, \
             mock.patch("fcntl.ioctl", side_effect=OSError("bad ioctl")):
            self.assertEqual(kf._kvm_probe()[0], "error")
        closer.assert_called_once_with(7)

    def test_api_version_returned(self):
        with mock.patch("os.open", return_value=7), mock.patch("os.close"), \
             mock.patch("fcntl.ioctl", return_value=12):
            self.assertEqual(kf._kvm_probe(), ("ok", 12))


class TestIoUring(unittest.TestCase):
    def sysctl(self, value):
        return FakeContext(files={"/proc/sys/kernel/io_uring_disabled": value})

    def test_available(self):
        r = kf.io_uring(self.sysctl("0"))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["io_uring"], 1)

    def test_group_restricted_is_info(self):
        self.assertIs(kf.io_uring(self.sysctl("1")).status, Status.INFO)

    def test_disabled_by_policy_is_info(self):
        r = kf.io_uring(self.sysctl("2"))
        self.assertIs(r.status, Status.INFO)
        self.assertEqual(r.metrics["io_uring"], 0)

    def test_older_kernel_falls_back_to_config(self):
        r = kf.io_uring(FakeContext(kconfig="CONFIG_IO_URING=y\n"))
        self.assertIs(r.status, Status.PASS)
        self.assertIn("predates the sysctl", r.message)

    def test_no_sysctl_and_no_config_skips(self):
        self.assertIs(kf.io_uring(FakeContext()).status, Status.SKIP)

    def test_config_says_not_compiled_in(self):
        r = kf.io_uring(FakeContext(kconfig="# CONFIG_IO_URING is not set\n"))
        self.assertIs(r.status, Status.INFO)
        self.assertEqual(r.metrics["io_uring"], 0)


CRYPTO_ACCEL = """name         : xts(aes)
driver       : xts-aes-aesni
priority     : 401

name         : cbc(aes)
driver       : cbc-aes-aesni
priority     : 400
"""
CRYPTO_GENERIC = """name         : xts(aes)
driver       : xts(ecb(aes-generic))
priority     : 100
"""


class TestCryptoAccel(unittest.TestCase):
    def test_no_proc_crypto_skips(self):
        self.assertIs(kf.crypto_accel(FakeContext()).status, Status.SKIP)

    def test_no_aes_registered_skips(self):
        ctx = FakeContext(files={"/proc/crypto": "name : sha256\ndriver : sha256-generic\n"})
        self.assertIs(kf.crypto_accel(ctx).status, Status.SKIP)

    def test_hardware_aes_passes(self):
        ctx = FakeContext(files={"/proc/crypto": CRYPTO_ACCEL})
        r = kf.crypto_accel(ctx)
        self.assertIs(r.status, Status.PASS)
        # the xts driver is the one LUKS actually uses
        self.assertIn("xts-aes-aesni", r.message)
        self.assertEqual(r.metrics["aes_accelerated"], 1)

    def test_prefers_any_accelerated_driver_when_xts_is_generic(self):
        ctx = FakeContext(files={"/proc/crypto":
                                 "name         : cbc(aes)\ndriver       : cbc-aes-aesni\n"})
        r = kf.crypto_accel(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertIn("cbc-aes-aesni", r.message)

    def test_software_only_warns(self):
        ctx = FakeContext(files={"/proc/crypto": CRYPTO_GENERIC})
        r = kf.crypto_accel(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["aes_accelerated"], 0)


if __name__ == "__main__":
    unittest.main()
