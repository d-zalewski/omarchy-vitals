"""Tier 0 deploy-integrity checks: is the running kernel the installed one."""
from __future__ import annotations

import unittest
from unittest import mock

from fakefs import fake_fs  # noqa: E402
from helpers import FakeContext, cp  # noqa: E402

from vitals.checks import boot  # noqa: E402
from vitals.core import Status  # noqa: E402

RUNNING = "7.2.2-5-omarchy-bore"
NEWER = "7.2.3-1-omarchy-bore"
OLDER = "7.1.11-arch1-1"
PKGBASE = "linux-omarchy-bore"


def trees(*releases, extra=None):
    """A /usr/lib/modules containing one directory per release."""
    tree = {"/usr/lib/modules": list(releases) + list(extra or [])}
    for r in releases:
        tree[f"/usr/lib/modules/{r}"] = []
    return tree


def pkgbase_files(*releases, name=PKGBASE):
    return {f"/usr/lib/modules/{r}/pkgbase": f"{name}\n" for r in releases}


class TestKernelCurrent(unittest.TestCase):
    def run_check(self, tree, files=None, running=RUNNING):
        with fake_fs(tree), mock.patch("platform.release", return_value=running):
            return boot.kernel_current(FakeContext(files=files or {}))

    def test_no_modules_root_skips(self):
        self.assertIs(self.run_check({}).status, Status.SKIP)

    def test_running_tree_gone_fails(self):
        r = self.run_check(trees(NEWER), pkgbase_files(NEWER))
        self.assertIs(r.status, Status.FAIL)
        self.assertEqual(r.metrics["kernel_reboot_pending"], 1)
        self.assertIn("replaced underneath you", r.message)

    def test_newer_installed_warns(self):
        r = self.run_check(trees(RUNNING, NEWER), pkgbase_files(RUNNING, NEWER))
        self.assertIs(r.status, Status.WARN)
        self.assertIn(NEWER, r.message)
        self.assertEqual(r.metrics["kernel_reboot_pending"], 1)

    def test_older_sibling_is_not_a_warning(self):
        # 7.1.11 sorts below 7.2.2 numerically, not lexically.
        r = self.run_check(trees(RUNNING, OLDER),
                           pkgbase_files(RUNNING, OLDER))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["kernel_reboot_pending"], 0)

    def test_newer_tree_from_another_package_is_ignored(self):
        # A stock Arch kernel installed alongside is not a pending reboot of
        # the kernel under test.
        files = {**pkgbase_files(RUNNING),
                 f"/usr/lib/modules/{NEWER}/pkgbase": "linux\n"}
        r = self.run_check(trees(RUNNING, NEWER), files)
        self.assertIs(r.status, Status.PASS)
        self.assertIn(PKGBASE, r.message)

    def test_stray_file_in_modules_root_ignored(self):
        tree = trees(RUNNING, extra=["stray"])
        tree["/usr/lib/modules/stray"] = "not a directory"
        r = self.run_check(tree, pkgbase_files(RUNNING))
        self.assertIs(r.status, Status.PASS)

    def test_missing_pkgbase_still_passes(self):
        r = self.run_check(trees(RUNNING))
        self.assertIs(r.status, Status.PASS)
        self.assertIn("kernel", r.message)


class TestModuleTree(unittest.TestCase):
    base = f"/usr/lib/modules/{RUNNING}"
    ko = "kernel/fs/btrfs/btrfs.ko.zst"

    def run_check(self, tree, dep=None):
        if tree:
            tree = {"/usr/lib/modules": [RUNNING], **tree}
        files = {} if dep is None else {f"{self.base}/modules.dep": dep}
        with fake_fs(tree), mock.patch("platform.release", return_value=RUNNING):
            return boot.module_tree(FakeContext(files=files))

    def test_no_modules_root_skips(self):
        # A kernel with no module tree at all is not a fault, it is a
        # different kind of system.
        self.assertIs(self.run_check({}).status, Status.SKIP)

    def test_missing_tree_for_running_kernel_fails(self):
        r = self.run_check({"/usr/lib/modules": []})
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("no module can be loaded", r.message)

    def test_empty_dep_index_fails(self):
        r = self.run_check({self.base: []})
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("depmod", r.message)

    def test_indexed_module_absent_from_disk_fails(self):
        r = self.run_check({self.base: []}, dep=f"{self.ko}:\n")
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("absent from disk", r.message)
        self.assertEqual(r.metrics["modules_indexed"], 1)

    def test_no_headers_warns(self):
        tree = {self.base: [], f"{self.base}/{self.ko}": ""}
        r = self.run_check(tree, dep=f"{self.ko}:\n")
        self.assertIs(r.status, Status.WARN)
        self.assertIn("DKMS", r.message)

    def test_complete_tree_passes(self):
        second = "kernel/net/dummy.ko.zst"
        tree = {self.base: [], f"{self.base}/{self.ko}": "",
                f"{self.base}/{second}": "", f"{self.base}/build/Makefile": ""}
        r = self.run_check(tree, dep=f"{self.ko}:\n{second}:\n")
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["modules_indexed"], 2)


def find_output(*pairs):
    """What `find -printf '%s\\t%p\\n'` prints."""
    return "".join(f"{size}\t{path}\n" for size, path in pairs)


def responses(*results):
    """Successive answers for one command pattern (plain run, then sudo)."""
    seq = iter(results)
    last = [results[-1]]

    def next_result():
        try:
            last[0] = next(seq)
        except StopIteration:
            pass
        return last[0]
    return next_result


UKI = f"/boot/EFI/Linux/omarchy_{PKGBASE}.efi"
DENIED = cp("", 1, "find: '/boot': Permission denied")


class TestInitramfs(unittest.TestCase):
    def check(self, ctx, minimum=10):
        with mock.patch.object(boot, "MIN_INITRAMFS_BYTES", minimum):
            return boot.initramfs(ctx)

    def check_uki(self, ctx, minimum=10, releases=(RUNNING,)):
        with fake_fs(trees(*releases)):
            return self.check(ctx, minimum)

    def ctx_for(self, stdout, files=None):
        return FakeContext(commands={"find /boot": cp(stdout)}, files=files or {})

    # -- reading /boot ----------------------------------------------------
    def test_root_only_boot_is_read_with_sudo(self):
        # The ESP is commonly mounted root-only, where a plain find prints
        # nothing at all.
        ctx = FakeContext(
            commands={"find /boot": responses(DENIED, cp(find_output((5000, UKI))))},
            files={f"/usr/lib/modules/{RUNNING}/pkgbase": f"{PKGBASE}\n"})
        r = self.check_uki(ctx)
        self.assertIs(r.status, Status.PASS)

    def test_unreadable_even_with_sudo_skips(self):
        ctx = FakeContext(commands={"find /boot": responses(DENIED, cp(""))})
        self.assertIs(self.check(ctx).status, Status.SKIP)

    def test_garbage_lines_ignored(self):
        ctx = self.ctx_for("not a size line\n" + find_output((5000, UKI)))
        self.assertIs(self.check_uki(ctx).status, Status.INFO)

    def test_no_images_skips(self):
        self.assertIs(self.check(self.ctx_for("")).status, Status.SKIP)

    # -- vmlinuz + initramfs layout ---------------------------------------
    def test_missing_initramfs_fails(self):
        ctx = self.ctx_for(find_output((900, f"/boot/vmlinuz-{RUNNING}")))
        r = self.check(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("will not come up", r.message)

    def test_truncated_initramfs_fails(self):
        ctx = self.ctx_for(find_output((900, f"/boot/vmlinuz-{RUNNING}"),
                                       (4, f"/boot/initramfs-{RUNNING}.img")))
        r = self.check(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("truncated", r.message)

    def test_complete_initramfs_passes(self):
        ctx = self.ctx_for(find_output((900, f"/boot/vmlinuz-{RUNNING}"),
                                       (5000, f"/boot/initramfs-{RUNNING}.img")))
        self.assertIs(self.check(ctx).status, Status.PASS)

    def test_debian_style_name_also_recognised(self):
        ctx = self.ctx_for(find_output((900, f"/boot/vmlinuz-{RUNNING}"),
                                       (5000, f"/boot/initrd.img-{RUNNING}")))
        self.assertIs(self.check(ctx).status, Status.PASS)

    # -- unified kernel image layout --------------------------------------
    def test_bootloader_binaries_are_not_mistaken_for_ukis(self):
        # limine_x64.efi is a few hundred KB and lives outside /EFI/Linux.
        ctx = self.ctx_for(find_output((370744, "/boot/EFI/limine/limine_x64.efi"),
                                       (5000, UKI)),
                           files={f"/usr/lib/modules/{RUNNING}/pkgbase": PKGBASE})
        self.assertIs(self.check_uki(ctx).status, Status.PASS)

    def test_truncated_uki_fails(self):
        ctx = self.ctx_for(find_output((4, UKI)))
        r = self.check_uki(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("truncated", r.message)

    def test_uki_without_known_pkgbases_is_info(self):
        self.assertIs(self.check_uki(self.ctx_for(find_output((5000, UKI)))).status,
                      Status.INFO)

    def test_installed_kernel_with_no_uki_fails(self):
        # The stock kernel is installed but only the bore UKI was generated.
        ctx = self.ctx_for(find_output((5000, UKI)),
                           files={f"/usr/lib/modules/{RUNNING}/pkgbase": PKGBASE,
                                  f"/usr/lib/modules/{OLDER}/pkgbase": "linux"})
        r = self.check_uki(ctx, releases=(RUNNING, OLDER))
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("no unified kernel image", r.message)

    def test_one_uki_per_installed_kernel_passes(self):
        ctx = self.ctx_for(find_output((5000, UKI),
                                       (5000, "/boot/EFI/Linux/omarchy_linux.efi")),
                           files={f"/usr/lib/modules/{RUNNING}/pkgbase": PKGBASE,
                                  f"/usr/lib/modules/{OLDER}/pkgbase": "linux"})
        r = self.check_uki(ctx, releases=(RUNNING, OLDER))
        self.assertIs(r.status, Status.PASS)


class TestMicrocode(unittest.TestCase):
    cpuinfo = "processor\t: 0\nmicrocode\t: 0xa50000d\n"

    def test_no_revision_skips(self):
        r = boot.microcode(FakeContext(files={"/proc/cpuinfo": "processor\t: 0\n"}))
        self.assertIs(r.status, Status.SKIP)

    def test_early_update_passes(self):
        r = boot.microcode(FakeContext(
            files={"/proc/cpuinfo": self.cpuinfo},
            journal_kernel="microcode: Updated early from: 0x0a50000c"))
        self.assertIs(r.status, Status.PASS)
        self.assertIn("0xa50000d", r.message)

    def test_revision_reported_without_update_is_info(self):
        # AMD prints the current patch level whether or not it updated.
        r = boot.microcode(FakeContext(
            files={"/proc/cpuinfo": self.cpuinfo},
            journal_kernel="microcode: Current revision: 0x0a50000d"))
        self.assertIs(r.status, Status.INFO)

    def test_driver_silent_warns(self):
        r = boot.microcode(FakeContext(files={"/proc/cpuinfo": self.cpuinfo},
                                       journal_kernel="nothing relevant"))
        self.assertIs(r.status, Status.WARN)
        self.assertIn("initramfs", r.message)


if __name__ == "__main__":
    unittest.main()
