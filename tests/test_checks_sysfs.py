"""Branches that walk /sys and /proc, driven through a fake filesystem."""
from __future__ import annotations

import unittest
from unittest import mock

from fakefs import fake_fs  # noqa: E402
from helpers import FakeContext, cp  # noqa: E402

from vitals.checks import (audio, graphics, kernel_build, network,  # noqa: E402
                           peripherals, stress, throughput)
from vitals.core import Status  # noqa: E402


class TestGraphicsSysfs(unittest.TestCase):
    def test_render_node_present(self):
        with fake_fs({"/dev/dri": ["card0", "renderD128"]}):
            r = graphics.drm_render_node(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["drm_render_nodes"], 1)

    def test_card_without_render_node_fails(self):
        with fake_fs({"/dev/dri": ["card0"]}):
            r = graphics.drm_render_node(FakeContext())
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("no GPU acceleration", r.message)

    def test_displays_connected_and_disconnected(self):
        tree = {"/sys/class/drm": ["card0-HDMI-A-1", "card0-DP-1"],
                "/sys/class/drm/card0-HDMI-A-1/status": "connected",
                "/sys/class/drm/card0-DP-1/status": "disconnected"}
        with fake_fs(tree):
            r = graphics.displays(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["displays_connected"], 1)

    def test_no_connected_displays_warns(self):
        tree = {"/sys/class/drm": ["card0-DP-1"],
                "/sys/class/drm/card0-DP-1/status": "disconnected"}
        with fake_fs(tree):
            self.assertIs(graphics.displays(FakeContext()).status, Status.WARN)

    def test_displays_absent_sysfs_skips(self):
        with fake_fs({}):
            self.assertIs(graphics.displays(FakeContext()).status, Status.SKIP)

    def test_drm_modes_readable(self):
        tree = {"/sys/class/drm": ["card0-HDMI-A-1"],
                "/sys/class/drm/card0-HDMI-A-1/status": "connected",
                "/sys/class/drm/card0-HDMI-A-1/modes": "3840x2160\n1920x1080"}
        with fake_fs(tree):
            r = graphics.drm_modes(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertIn("3840x2160", r.message)

    def test_drm_modes_edid_failure_warns(self):
        tree = {"/sys/class/drm": ["card0-HDMI-A-1"],
                "/sys/class/drm/card0-HDMI-A-1/status": "connected"}
        with fake_fs(tree):
            self.assertIs(graphics.drm_modes(FakeContext()).status, Status.WARN)

    def test_drm_modes_no_connected_skips(self):
        tree = {"/sys/class/drm": ["card0-DP-1"],
                "/sys/class/drm/card0-DP-1/status": "disconnected"}
        with fake_fs(tree):
            self.assertIs(graphics.drm_modes(FakeContext()).status, Status.SKIP)


class TestAudioSysfs(unittest.TestCase):
    def test_cards_listed(self):
        tree = {"/proc/asound": ["cards"],
                "/proc/asound/cards": " 0 [PCH  ]: HDA-Intel - HDA Intel PCH\n"}
        with fake_fs(tree):
            r = audio.sound_card(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["sound_cards"], 1)

    def test_no_cards_fails(self):
        with fake_fs({"/proc/asound": ["cards"], "/proc/asound/cards": "\n"}):
            self.assertIs(audio.sound_card(FakeContext()).status, Status.FAIL)


class TestNetworkSysfs(unittest.TestCase):
    def test_iface_helpers(self):
        tree = {"/sys/class/net": ["lo", "eth0", "wlan0", "docker0", "veth123"],
                "/sys/class/net/wlan0/wireless": "",
                "/sys/class/net/eth0/operstate": "up"}
        with fake_fs(tree):
            self.assertEqual(network._ifaces(), ["eth0", "wlan0"])
            self.assertTrue(network._is_wireless("wlan0"))
            self.assertFalse(network._is_wireless("eth0"))
            self.assertEqual(network._operstate("eth0"), "up")
            self.assertEqual(network._operstate("missing"), "unknown")

    def test_ifaces_no_sysfs(self):
        with fake_fs({}):
            self.assertEqual(network._ifaces(), [])

    def test_is_virtual(self):
        with fake_fs({"/sys/class/net": ["eth0"]}):
            self.assertFalse(network._is_virtual("eth0"))

    def test_net_drivers_resolves_driver_names(self):
        tree = {"/sys/class/net": ["eth0"],
                "/sys/class/net/eth0/device/driver": ""}
        with fake_fs(tree), \
             mock.patch.object(network, "_is_virtual", return_value=False):
            r = network.net_drivers(FakeContext())
        self.assertIn("eth0", r.message)

    def test_bluetooth_present_service_inactive_warns(self):
        with fake_fs({"/sys/class/bluetooth": ["hci0"]}):
            ctx = FakeContext(commands={"is-active": cp("inactive\n")})
            r = network.bluetooth(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["bt_adapters"], 1)

    def test_bluetooth_active(self):
        with fake_fs({"/sys/class/bluetooth": ["hci0"]}):
            ctx = FakeContext(commands={"is-active": cp("active\n")})
            r = network.bluetooth(ctx)
        self.assertIs(r.status, Status.PASS)

    def test_bluetooth_rfkill_blocked_noted(self):
        with fake_fs({"/sys/class/bluetooth": ["hci0"]}):
            ctx = FakeContext(tools=["rfkill"],
                              commands={"is-active": cp("active\n"),
                                        "rfkill": cp("Soft blocked: yes")})
            r = network.bluetooth(ctx)
        self.assertIn("blocked", r.message)


class TestPeripheralsSysfs(unittest.TestCase):
    def test_usb_without_lsusb_counts_buses(self):
        with fake_fs({"/sys/bus/usb/devices": ["usb1", "usb2", "1-1"]}):
            r = peripherals.usb(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertIn("2 USB bus", r.message)

    def test_usb_no_buses_warns(self):
        with fake_fs({}):
            self.assertIs(peripherals.usb(FakeContext()).status, Status.WARN)

    def test_input_devices_parsed(self):
        content = ('N: Name="AT Translated Set 2 keyboard"\n'
                   'I: Bus=0011\nE: EV=120013\nB: KEY=402000000 3803078f800d001\n'
                   'N: Name="SynPS/2 Synaptics Touchpad"\n')
        with fake_fs({"/proc/bus/input/devices": content}):
            r = peripherals.input_devices(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["input_devices"], 2)

    def test_input_devices_none_fails(self):
        with fake_fs({"/proc/bus/input/devices": "nothing here"}):
            self.assertIs(peripherals.input_devices(FakeContext()).status,
                          Status.FAIL)

    def test_input_devices_absent_skips(self):
        with fake_fs({}):
            self.assertIs(peripherals.input_devices(FakeContext()).status,
                          Status.SKIP)

    def test_smart_healthy_and_failed(self):
        ctx = FakeContext(commands={"lsblk": cp("/dev/sda disk\n"),
                                    "smartctl": cp("SMART overall-health: PASSED")})
        self.assertIs(peripherals.smart(ctx).status, Status.PASS)
        bad = FakeContext(commands={"lsblk": cp("/dev/sda disk\n"),
                                    "smartctl": cp("SMART overall-health: FAILED")})
        self.assertIs(peripherals.smart(bad).status, Status.FAIL)

    def test_smart_no_disks_skips(self):
        self.assertIs(peripherals.smart(FakeContext(commands={"lsblk": cp("")})).status,
                      Status.SKIP)

    def test_smart_unsupported_skips(self):
        ctx = FakeContext(commands={"lsblk": cp("/dev/sda disk\n"),
                                    "smartctl": cp("Unavailable")})
        self.assertIs(peripherals.smart(ctx).status, Status.SKIP)

    def test_webcam_with_v4l2(self):
        with fake_fs({"/dev": ["video0"]}):
            ctx = FakeContext(tools=["v4l2-ctl"],
                              commands={"v4l2-ctl": cp("Card type     : Integrated Cam")})
            r = peripherals.webcam(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["video_devices"], 1)

    def test_webcam_nodes_only(self):
        with fake_fs({"/dev": ["video0"]}):
            r = peripherals.webcam(FakeContext())
        self.assertIs(r.status, Status.PASS)

    def test_thermal_normal(self):
        tree = {"/sys/class/thermal": ["thermal_zone0"],
                "/sys/class/thermal/thermal_zone0/temp": "50000",
                "/sys/class/thermal/thermal_zone0/type": "x86_pkg_temp"}
        with fake_fs(tree):
            r = peripherals.thermal(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["temp_max_c"], 50.0)

    def test_thermal_unreadable_warns(self):
        tree = {"/sys/class/thermal": ["thermal_zone0"]}
        with fake_fs(tree):
            self.assertIs(peripherals.thermal(FakeContext()).status, Status.WARN)

    def test_cpufreq_unreadable_warns(self):
        with fake_fs({"/sys/devices/system/cpu/cpu0/cpufreq": []}):
            ctx = FakeContext(files={
                "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor": "performance",
                "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "notanumber"})
            self.assertIs(peripherals.cpufreq(ctx).status, Status.WARN)

    def test_battery_present(self):
        tree = {"/sys/class/power_supply": ["BAT0"],
                "/sys/class/power_supply/BAT0/capacity": "85"}
        with fake_fs(tree):
            ctx = FakeContext(files={
                "/sys/class/power_supply/BAT0/capacity": "85",
                "/sys/class/power_supply/BAT0/status": "Discharging"})
            r = peripherals.battery(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["battery_pct"], 85)

    def test_battery_ac_only_is_info(self):
        with fake_fs({"/sys/class/power_supply": ["AC"]}):
            self.assertIs(peripherals.battery(FakeContext()).status, Status.INFO)

    def test_battery_absent_skips(self):
        with fake_fs({}):
            self.assertIs(peripherals.battery(FakeContext()).status, Status.SKIP)

    def test_storage_lists_names(self):
        ctx = FakeContext(commands={"lsblk": cp("sda  111.8G disk\nzram0 7.6G disk\n")},
                          journal_kernel="")
        r = peripherals.storage(ctx)
        self.assertEqual(r.metrics["disks"], 2)


class TestStressPaths(unittest.TestCase):
    def test_stress_stability_reports_faults(self):
        ctx = FakeContext(journal_kernel="", commands={"journalctl": cp("BUG: x")})
        proc = mock.MagicMock()
        proc.poll.side_effect = [None, 0]
        proc.stdout.read.return_value = ""
        proc.returncode = 0
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch("time.sleep"), \
             fake_fs({"/sys/class/thermal": []}):
            r = stress.stress_stability(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertGreater(r.metrics["stress_new_oops"], 0)

    def test_stress_stability_clean(self):
        ctx = FakeContext(journal_kernel="", commands={"journalctl": cp("")})
        proc = mock.MagicMock()
        proc.poll.side_effect = [None, 0]
        proc.stdout.read.return_value = ""
        proc.returncode = 0
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch("time.sleep"), \
             fake_fs({"/sys/class/thermal": []}):
            r = stress.stress_stability(ctx)
        self.assertIs(r.status, Status.PASS)

    def test_stress_stability_nonzero_exit_warns(self):
        ctx = FakeContext(journal_kernel="", commands={"journalctl": cp("")})
        proc = mock.MagicMock()
        proc.poll.side_effect = [None, 1]
        proc.stdout.read.return_value = ""
        proc.returncode = 1
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch("time.sleep"), \
             fake_fs({"/sys/class/thermal": []}):
            self.assertIs(stress.stress_stability(ctx).status, Status.WARN)


class TestKernelBuildPaths(unittest.TestCase):
    def test_btf_present_with_modules(self):
        tree = {"/sys/kernel/btf": ["vmlinux", "nfs", "btrfs"],
                "/sys/kernel/btf/vmlinux": "x" * 2048}
        with fake_fs(tree):
            r = kernel_build.btf(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["btf_modules"], 2)

    def test_btf_without_module_btf_warns(self):
        tree = {"/sys/kernel/btf": ["vmlinux"], "/sys/kernel/btf/vmlinux": "x"}
        with fake_fs(tree):
            r = kernel_build.btf(FakeContext())
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["btf_modules"], 0)

    def test_compile_run_executes(self):
        ctx = FakeContext(commands={"gcc": cp("")})
        with mock.patch("subprocess.run", return_value=cp("ok", 0)):
            built, rc, out = kernel_build._compile_run(ctx, "int main(){}", ["-O2"])
        self.assertTrue(built)
        self.assertEqual(rc, 0)

    def test_compile_run_execution_error(self):
        ctx = FakeContext(commands={"gcc": cp("")})
        with mock.patch("subprocess.run", side_effect=OSError("boom")):
            built, rc, out = kernel_build._compile_run(ctx, "int main(){}", [])
        self.assertTrue(built)
        self.assertIsNone(rc)


class TestThroughputEdges(unittest.TestCase):
    def test_sched_pipe_no_result_warns(self):
        ctx = FakeContext(commands={"perf --version": cp("perf version 7.2.2\n"),
                                    "sched pipe": cp("nothing")})
        with mock.patch("platform.release", return_value="7.2.2-x"):
            self.assertIs(throughput.perf_sched_pipe(ctx).status, Status.WARN)

    def test_messaging_no_result_warns(self):
        ctx = FakeContext(commands={"perf --version": cp("perf version 7.2.2\n"),
                                    "messaging": cp("nothing")})
        with mock.patch("platform.release", return_value="7.2.2-x"):
            self.assertIs(throughput.perf_sched_messaging(ctx).status, Status.WARN)

    def test_mem_no_rate_skips(self):
        ctx = FakeContext(commands={"perf --version": cp("perf version 7.2.2\n"),
                                    "mem memcpy": cp("nothing")})
        with mock.patch("platform.release", return_value="7.2.2-x"):
            self.assertIs(throughput.perf_mem(ctx).status, Status.SKIP)

    def test_all_perf_checks_skip_together_on_mismatch(self):
        ctx = FakeContext(commands={"perf --version": cp("perf version 1.0\n")})
        with mock.patch("platform.release", return_value="7.2.2-x"):
            for fn in (throughput.perf_sched_messaging, throughput.perf_syscall,
                       throughput.perf_mem):
                self.assertIs(fn(ctx).status, Status.SKIP)

    def test_syscall_parsed(self):
        out = "       0.290800 usecs/op\n        3439298 ops/sec\n"
        ctx = FakeContext(commands={"perf --version": cp("perf version 7.2.2\n"),
                                    "syscall": cp(out)})
        with mock.patch("platform.release", return_value="7.2.2-x"):
            r = throughput.perf_syscall(ctx)
        self.assertEqual(r.metrics["syscall_usecs_op"], 0.2908)


if __name__ == "__main__":
    unittest.main()
