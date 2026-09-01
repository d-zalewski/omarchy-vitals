"""Remaining branches: error handling, fallbacks and rarely-hit paths.

Mostly defensive code. It is worth covering because these paths run precisely
when something is already wrong, and a check that crashes while reporting a
fault is worse than no check at all.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from fakefs import fake_fs  # noqa: E402
from helpers import FakeContext, cp  # noqa: E402

from vitals.checks import graphics, network, stress, throughput  # noqa: E402
from vitals.core import Status  # noqa: E402


class TestGraphicsFallbacks(unittest.TestCase):
    def test_no_connectors_skips(self):
        with fake_fs({"/sys/class/drm": []}):
            self.assertIs(graphics.displays(FakeContext()).status, Status.SKIP)

    def test_drm_modes_without_sysfs_skips(self):
        with fake_fs({}):
            self.assertIs(graphics.drm_modes(FakeContext()).status, Status.SKIP)

    def test_compositor_outputs_not_hyprland_skips(self):
        ctx = FakeContext(session={"_compositor": "sway"})
        self.assertIs(graphics.compositor_outputs(ctx).status, Status.SKIP)

    def test_compositor_outputs_command_fails_skips(self):
        ctx = FakeContext(session={"_compositor": "Hyprland"},
                          commands={"hyprctl": cp("", 1)})
        self.assertIs(graphics.compositor_outputs(ctx).status, Status.SKIP)

    def test_compositor_reports_no_monitors_warns(self):
        ctx = FakeContext(session={"_compositor": "Hyprland"},
                          commands={"hyprctl": cp("[]")})
        r = graphics.compositor_outputs(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["compositor_monitors"], 0)

    def test_gpu_accel_skips_tool_without_renderer_line(self):
        ctx = FakeContext(tools=["glxinfo"], commands={"glxinfo": cp("no match here")})
        self.assertIs(graphics.gpu_accel(ctx).status, Status.SKIP)

    def test_gpu_accel_falls_back_to_vulkan(self):
        ctx = FakeContext(tools=["vulkaninfo"],
                          commands={"vulkaninfo": cp("deviceName = Intel UHD 600")})
        r = graphics.gpu_accel(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertIn("Intel UHD 600", r.message)

    def test_video_decode_unusable_warns(self):
        ctx = FakeContext(tools=["vainfo"],
                          commands={"vainfo": cp("", 1, "libva error: no driver")})
        self.assertIs(graphics.video_decode(ctx).status, Status.WARN)

    def test_video_decode_empty_output_warns(self):
        ctx = FakeContext(tools=["vainfo"], commands={"vainfo": cp("")})
        self.assertIs(graphics.video_decode(ctx).status, Status.WARN)


class TestNetworkFallbacks(unittest.TestCase):
    def test_is_virtual_handles_resolve_error(self):
        with mock.patch.object(Path, "resolve", side_effect=OSError("boom")):
            self.assertFalse(network._is_virtual("eth0"))

    def test_driver_symlink_unreadable_marks_unknown_and_fails(self):
        with mock.patch.object(network, "_ifaces", return_value=["eth0"]), \
             mock.patch.object(network, "_is_virtual", return_value=False), \
             mock.patch.object(Path, "resolve", side_effect=OSError("no link")):
            r = network.net_drivers(FakeContext())
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("without driver", r.message)

    def test_ethernet_speed_unreadable_is_tolerated(self):
        with mock.patch.object(network, "_ifaces", return_value=["eth0"]), \
             mock.patch.object(network, "_is_wireless", return_value=False), \
             mock.patch.object(network, "_is_virtual", return_value=False), \
             mock.patch.object(network, "_operstate", return_value="up"), \
             mock.patch.object(Path, "read_text", side_effect=OSError("nope")):
            r = network.ethernet_link(FakeContext())
        self.assertIs(r.status, Status.PASS)           # no speed, still up

    def test_wifi_without_iw_reports_operstate(self):
        with mock.patch.object(network, "_ifaces", return_value=["wlan0"]), \
             mock.patch.object(network, "_is_wireless", return_value=True), \
             mock.patch.object(network, "_operstate", return_value="down"):
            r = network.wifi(FakeContext())            # iw not in tools
        self.assertIs(r.status, Status.WARN)
        self.assertIn("wlan0", r.message)

    def test_wifi_scan_without_iw_skips(self):
        with mock.patch.object(network, "_ifaces", return_value=["wlan0"]), \
             mock.patch.object(network, "_is_wireless", return_value=True):
            self.assertIs(network.wifi_scan(FakeContext()).status, Status.SKIP)

    def test_wifi_scan_failure_fails(self):
        with mock.patch.object(network, "_ifaces", return_value=["wlan0"]), \
             mock.patch.object(network, "_is_wireless", return_value=True):
            ctx = FakeContext(tools=["iw"], commands={"scan": cp("", 1, "busy")})
            self.assertIs(network.wifi_scan(ctx).status, Status.FAIL)


class TestStressFallbacks(unittest.TestCase):
    def _proc(self, rc=0):
        p = mock.MagicMock()
        p.poll.side_effect = [None, rc]
        p.stdout.read.return_value = ""
        p.returncode = rc
        return p

    def test_unreadable_thermal_zone_is_ignored(self):
        ctx = FakeContext(journal_kernel="", commands={"journalctl": cp("")})
        tree = {"/sys/class/thermal": ["thermal_zone0"]}   # temp file absent
        with mock.patch("subprocess.Popen", return_value=self._proc()), \
             mock.patch("time.sleep"), fake_fs(tree):
            r = stress.stress_stability(ctx)
        self.assertIs(r.status, Status.PASS)

    def test_peak_temperature_recorded_when_readable(self):
        ctx = FakeContext(journal_kernel="", commands={"journalctl": cp("")})
        tree = {"/sys/class/thermal": ["thermal_zone0"],
                "/sys/class/thermal/thermal_zone0/temp": "96000"}
        with mock.patch("subprocess.Popen", return_value=self._proc()), \
             mock.patch("time.sleep"), fake_fs(tree):
            r = stress.stress_stability(ctx)
        self.assertEqual(r.metrics["stress_peak_temp_c"], 96.0)
        self.assertIn("throttling", r.message)

    def test_disk_io_cleanup_error_is_tolerated(self):
        out = json.dumps({"jobs": [{"read": {"iops": 999.0}}]})
        ctx = FakeContext(commands={"findmnt": cp("btrfs\n"), "fio": cp(out)})
        leftover = mock.MagicMock()
        leftover.unlink.side_effect = OSError("busy")
        with mock.patch.object(Path, "glob", return_value=[leftover]):
            r = stress.disk_io(ctx)
        self.assertEqual(r.metrics["fio_randread_iops"], 999)

    def test_snapshot_handles_unreadable_directory(self):
        ctx = FakeContext(files={"/sys/power/state": "mem"},
                          commands={"rtcwake": cp("ok"), "journalctl": cp("")})
        with mock.patch("time.sleep"), \
             mock.patch.object(Path, "is_dir", return_value=True), \
             mock.patch.object(Path, "iterdir", side_effect=OSError("denied")), \
             mock.patch.object(Path, "glob", side_effect=OSError("denied")):
            r = stress.suspend_resume(ctx)
        self.assertIs(r.status, Status.PASS)           # degrades, does not crash

    def test_device_missing_after_resume_fails(self):
        ctx = FakeContext(files={"/sys/power/state": "mem"},
                          commands={"rtcwake": cp("ok"), "journalctl": cp("")})
        states = [["eth0"], ["eth0"], []]              # before, mid, after
        with mock.patch("time.sleep"), \
             mock.patch.object(Path, "is_dir", return_value=True), \
             mock.patch.object(Path, "glob", return_value=[]), \
             mock.patch.object(Path, "iterdir",
                               side_effect=[[Path("/sys/class/net/eth0")], []]):
            r = stress.suspend_resume(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("gone after resume", r.message)


class TestThroughputFallbacks(unittest.TestCase):
    def test_iperf_server_killed_when_client_hangs(self):
        srv = mock.MagicMock()
        srv.wait.side_effect = Exception("won't die")
        ctx = FakeContext(commands={"iperf3 -c": cp("not json")})
        with mock.patch("subprocess.Popen", return_value=srv), \
             mock.patch("time.sleep"):
            throughput.iperf_loopback(ctx)
        srv.terminate.assert_called_once()
        srv.kill.assert_called_once()                  # escalates when needed


if __name__ == "__main__":
    unittest.main()
