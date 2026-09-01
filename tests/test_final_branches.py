"""The last few branches: tool failures, teardown errors and section resets."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from fakefs import fake_fs  # noqa: E402
from helpers import FakeContext, cp  # noqa: E402

from vitals.checks import audio, kernel_build, latency, peripherals, stress  # noqa: E402
from vitals.core import Status  # noqa: E402


class TestAudioBranches(unittest.TestCase):
    def test_wpctl_failure_warns(self):
        ctx = FakeContext(tools=["wpctl"],
                          commands={"wpctl status": cp("", 1, "connection refused")})
        r = audio.audio_sinks(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertIn("wpctl failed", r.message)

    def test_section_resets_between_blocks(self):
        """A trailing block must not have its entries counted as sinks."""
        status = (" Sinks:\n  │  *   49. Speakers  [vol: 1.00]\n"
                  " Settings:\n"
                  "  │      99. Not a sink\n")
        ctx = FakeContext(tools=["wpctl"], commands={"wpctl status": cp(status)})
        r = audio.audio_sinks(ctx)
        self.assertEqual(r.metrics["audio_sinks"], 1)   # 99 not counted

    def test_default_sink_without_wpctl_skips(self):
        self.assertIs(audio.audio_default_sink(FakeContext()).status, Status.SKIP)

    def test_default_sink_unmuted_passes(self):
        ctx = FakeContext(tools=["wpctl"],
                          commands={"get-volume": cp("Volume: 0.74")})
        r = audio.audio_default_sink(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertIn("0.74", r.message)


class TestKernelBuildBranches(unittest.TestCase):
    def test_vdso32_success(self):
        with mock.patch.object(kernel_build, "compile_run",
                               return_value=(True, 0, "ok")):
            r = kernel_build.vdso32(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertIn("32-bit", r.message)

    def test_vdso32_runs_but_fails(self):
        with mock.patch.object(kernel_build, "compile_run",
                               return_value=(True, 1, "")):
            self.assertIs(kernel_build.vdso32(FakeContext()).status, Status.FAIL)

    def test_stack_protector_compile_failure_skips(self):
        with mock.patch.object(kernel_build, "compile_run",
                               return_value=(False, None, "gcc missing")):
            self.assertIs(kernel_build.stack_protector(FakeContext()).status,
                          Status.SKIP)


class TestLatencyBranches(unittest.TestCase):
    def test_loaded_tolerates_load_process_that_will_not_wait(self):
        ctx = FakeContext(commands={"cyclictest": cp(
            "Avg:    7 Max:    1000")})
        proc = mock.MagicMock(pid=99)
        proc.wait.side_effect = Exception("never exits")
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch("subprocess.run", return_value=cp()), \
             mock.patch("os.killpg"), mock.patch("os.getpgid", return_value=99):
            r = latency.cyclictest_loaded(ctx)
        self.assertIs(r.status, Status.WARN)           # 1000us = borderline

    def test_loaded_reports_cyclictest_failure(self):
        ctx = FakeContext(commands={"cyclictest": cp("", 1, "must be root")})
        proc = mock.MagicMock(pid=1)
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch("subprocess.run", return_value=cp()), \
             mock.patch("os.killpg"), mock.patch("os.getpgid", return_value=1):
            r = latency.cyclictest_loaded(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("must be root", r.message)


class TestPeripheralsBranches(unittest.TestCase):
    def test_mouse_nodes_reported(self):
        content = 'N: Name="Logitech Mouse"\nE: EV=17\n'
        tree = {"/proc/bus/input/devices": content, "/dev/input": ["mouse0", "mouse1"]}
        with fake_fs(tree):
            r = peripherals.input_devices(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertIn("mouse node", r.message)


class TestStressBranches(unittest.TestCase):
    def test_thermal_read_error_during_stress_is_ignored(self):
        ctx = FakeContext(journal_kernel="", commands={"journalctl": cp("")})
        proc = mock.MagicMock()
        proc.poll.side_effect = [None, 0]
        proc.stdout.read.return_value = ""
        proc.returncode = 0
        zone = mock.MagicMock()
        zone.read_text.side_effect = OSError("sensor gone")
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch("time.sleep"), \
             mock.patch.object(Path, "glob", return_value=[zone]):
            r = stress.stress_stability(ctx)
        self.assertIs(r.status, Status.PASS)           # unreadable sensor tolerated


if __name__ == "__main__":
    unittest.main()
