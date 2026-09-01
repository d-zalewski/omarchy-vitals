"""Storage stack: round-trip, btrfs counters, swap/zram, discard."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from helpers import FakeContext, cp  # noqa: E402

from vitals.checks import filesystem as fs  # noqa: E402
from vitals.core import Status  # noqa: E402

BTRFS = cp("btrfs\n")


class TestFsRoundtrip(unittest.TestCase):
    def run_check(self, ctx=None):
        return fs.fs_roundtrip(ctx or FakeContext(commands={"findmnt": BTRFS}))

    def test_no_real_storage_skips(self):
        ctx = FakeContext(commands={"findmnt": cp("tmpfs\n")})
        self.assertIs(self.run_check(ctx).status, Status.SKIP)

    def test_data_survives(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(fs, "disk_dir", return_value=(d, "btrfs")):
            r = self.run_check()
        self.assertIs(r.status, Status.PASS)
        self.assertIn("btrfs", r.message)

    def test_write_error_fails(self):
        with mock.patch.object(fs, "disk_dir", return_value=("/nonexistent", "ext4")):
            r = self.run_check()
        self.assertIs(r.status, Status.FAIL)

    def test_corrupted_read_back_fails(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(fs, "disk_dir", return_value=(d, "btrfs")), \
             mock.patch("os.read", return_value=b"wrong"):
            r = self.run_check()
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("corruption", r.message)

    def test_evicts_the_page_cache_before_reading_back(self):
        evict = mock.MagicMock()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(fs, "disk_dir", return_value=(d, "btrfs")), \
             mock.patch.object(fs, "FADVISE", evict):
            r = self.run_check()
        self.assertIs(r.status, Status.PASS)
        evict.assert_called_once()

    def test_runs_where_posix_fadvise_is_absent(self):
        # Not Linux; the round trip is still worth verifying.
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(fs, "disk_dir", return_value=(d, "xfs")), \
             mock.patch.object(fs, "FADVISE", None):
            r = self.run_check()
        self.assertIs(r.status, Status.PASS)


STATS_CLEAN = """[/dev/mapper/root].write_io_errs    0
[/dev/mapper/root].read_io_errs     0
[/dev/mapper/root].corruption_errs  0
"""
STATS_BAD = STATS_CLEAN.replace("corruption_errs  0", "corruption_errs  4")
SCRUB_CLEAN = "Status:           finished\nError summary:    no errors found\n"
SCRUB_ERRORS = "Status:           finished\nError summary:    csum=3\n"


class TestBtrfsHealth(unittest.TestCase):
    def run_check(self, stats=STATS_CLEAN, stats_rc=0, scrub=SCRUB_CLEAN,
                  scrub_rc=0, tools=("btrfs",)):
        return fs.btrfs_health(FakeContext(
            tools=tools,
            commands={"scrub status": cp(scrub, scrub_rc),
                      "device stats": cp(stats, stats_rc)}))

    def test_no_btrfs_progs_skips(self):
        self.assertIs(self.run_check(tools=()).status, Status.SKIP)

    def test_not_btrfs_skips(self):
        self.assertIs(self.run_check(stats_rc=1).status, Status.SKIP)

    def test_no_counters_skips(self):
        self.assertIs(self.run_check(stats="unexpected output").status, Status.SKIP)

    def test_clean_passes(self):
        r = self.run_check()
        self.assertIs(r.status, Status.PASS)
        self.assertIn("last scrub clean", r.message)
        self.assertEqual(r.metrics["btrfs_scrub_errors"], 0)

    def test_corruption_counter_fails(self):
        r = self.run_check(stats=STATS_BAD)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("corruption_errs=4", r.message)
        self.assertEqual(r.metrics["btrfs_io_errors"], 4)

    def test_scrub_errors_warn_even_with_clean_counters(self):
        r = self.run_check(scrub=SCRUB_ERRORS)
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["btrfs_scrub_errors"], 1)

    def test_never_scrubbed_is_noted(self):
        r = self.run_check(scrub="never started\n")
        self.assertIs(r.status, Status.PASS)
        self.assertIn("never scrubbed", r.message)

    def test_scrub_needs_root(self):
        r = self.run_check(scrub="", scrub_rc=1)
        self.assertIs(r.status, Status.PASS)
        self.assertNotIn("scrub", r.message)


class TestSwapZram(unittest.TestCase):
    def run_check(self, swapon, mm_stat=None):
        files = {"/sys/block/zram0/mm_stat": mm_stat} if mm_stat else {}
        return fs.swap_zram(FakeContext(commands={"swapon": cp(swapon)}, files=files))

    def test_no_swap_is_info(self):
        r = self.run_check("")
        self.assertIs(r.status, Status.INFO)
        self.assertEqual(r.metrics["swap_kb"], 0)

    def test_plain_swapfile(self):
        r = self.run_check("/swap/swapfile file 8195604480 0\n")
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["swap_kb"], 8195604480 // 1024)

    def test_zram_without_stats_still_passes(self):
        r = self.run_check("/dev/zram0 partition 8195604480 0\n")
        self.assertIs(r.status, Status.PASS)

    def test_zram_nearly_idle_reports_no_ratio(self):
        # One page in, compressed to 64 bytes, is not "64x compression".
        r = self.run_check("/dev/zram0 partition 8195604480 0\n",
                           mm_stat="4096 64 20480 0 20480 0 0 0 0")
        self.assertIs(r.status, Status.PASS)
        self.assertIn("holding 4 KiB", r.message)
        self.assertNotIn("x", r.message.split("holding")[1])

    def test_zram_compressing_well(self):
        r = self.run_check("/dev/zram0 partition 8195604480 0\n",
                           mm_stat="41943040 13981013 20480 0 20480 0 0 0 0")
        self.assertIs(r.status, Status.PASS)
        self.assertIn("3.0x", r.message)

    def test_zram_barely_compressing_warns(self):
        # 4 MiB in, barely smaller out: zram is costing RAM for nothing.
        r = self.run_check("/dev/zram0 partition 8195604480 0\n",
                           mm_stat="4194304 4100000 4200000 0 4200000 0 0 0 0")
        self.assertIs(r.status, Status.WARN)


LSBLK_BLOCKED = "sda disk 2147483648\nsda2 part 2147483648\nroot crypt 0\n"
LSBLK_OPEN = "sda disk 2147483648\nsda2 part 2147483648\nroot crypt 2147483648\n"
LSBLK_NO_TRIM = "sda disk 0\nsda1 part 0\n"


class TestDiscard(unittest.TestCase):
    def run_check(self, lsblk, timer="enabled"):
        return fs.discard(FakeContext(commands={"lsblk": cp(lsblk),
                                                "is-enabled": cp(timer + "\n")}))

    def test_no_lsblk_output_skips(self):
        self.assertIs(self.run_check("").status, Status.SKIP)

    def test_drive_without_trim_skips(self):
        self.assertIs(self.run_check(LSBLK_NO_TRIM).status, Status.SKIP)

    def test_dm_crypt_blocking_is_info_not_a_warning(self):
        # Passing discards through dm-crypt leaks which blocks are in use, so
        # refusing them is a defensible default.
        r = self.run_check(LSBLK_BLOCKED)
        self.assertIs(r.status, Status.INFO)
        self.assertIn("crypt", r.message)
        self.assertEqual(r.metrics["discard_reaches_drive"], 0)

    def test_reaches_drive_but_no_timer_warns(self):
        r = self.run_check(LSBLK_OPEN, timer="disabled")
        self.assertIs(r.status, Status.WARN)
        self.assertIn("fstrim.timer", r.message)

    def test_fully_working_passes(self):
        r = self.run_check(LSBLK_OPEN)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["discard_reaches_drive"], 1)


if __name__ == "__main__":
    unittest.main()
