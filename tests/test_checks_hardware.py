"""Tier 1 hardware checks: graphics, audio, network, peripherals.

The distinction these must get right is absent hardware (SKIP) versus present
but broken hardware (FAIL), so most modules are tested for both.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from helpers import FakeContext, cp  # noqa: E402

from vitals.checks import audio, graphics, network, peripherals  # noqa: E402
from vitals.core import Status  # noqa: E402

LSPCI_GPU = """00:02.0 VGA compatible controller: Intel Corporation UHD Graphics
\tDeviceName: Onboard
\tKernel driver in use: i915
00:0e.0 Audio device: Intel Corporation HD Audio
\tKernel driver in use: snd_hda_intel
"""


class TestGraphics(unittest.TestCase):
    def test_gpu_driver_bound(self):
        r = graphics.gpu_driver(FakeContext(commands={"lspci": cp(LSPCI_GPU)}))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["gpu_count"], 1)
        self.assertIn("i915", r.message)

    def test_gpu_present_but_unbound_fails(self):
        no_driver = "00:02.0 VGA compatible controller: Some GPU\n"
        ctx = FakeContext(commands={"lspci -k": cp(no_driver),
                                    "lspci": cp(no_driver)})
        self.assertIs(graphics.gpu_driver(ctx).status, Status.FAIL)

    def test_no_gpu_skips(self):
        ctx = FakeContext(commands={"lspci": cp("00:00.0 Host bridge: x\n")})
        self.assertIs(graphics.gpu_driver(ctx).status, Status.SKIP)

    def test_render_node_missing_fails(self):
        with mock.patch.object(Path, "exists", return_value=False):
            self.assertIs(graphics.drm_render_node(FakeContext()).status,
                          Status.FAIL)

    def test_gpu_errors_clean_warn_and_fail(self):
        self.assertIs(graphics.gpu_errors(FakeContext(journal_kernel="")).status,
                      Status.PASS)
        r = graphics.gpu_errors(FakeContext(journal_kernel="GPU HANG: ecode"))
        self.assertIs(r.status, Status.FAIL)
        r = graphics.gpu_errors(
            FakeContext(journal_kernel="[drm:foo] *ERROR* something"))
        self.assertIs(r.status, Status.WARN)

    def test_compositor_absent_skips(self):
        self.assertIs(graphics.compositor(FakeContext(session={})).status,
                      Status.SKIP)

    def test_compositor_present_but_unresponsive_fails(self):
        ctx = FakeContext(session={"_compositor": "Hyprland"},
                          commands={"hyprctl": cp("", 1, "no instance")})
        self.assertIs(graphics.compositor(ctx).status, Status.FAIL)

    def test_compositor_responsive(self):
        ctx = FakeContext(session={"_compositor": "Hyprland"},
                          commands={"hyprctl": cp("Hyprland 0.56.2\n")})
        self.assertIs(graphics.compositor(ctx).status, Status.PASS)

    def test_compositor_other_wm(self):
        ctx = FakeContext(session={"_compositor": "sway"})
        self.assertIs(graphics.compositor(ctx).status, Status.PASS)

    def test_compositor_outputs_parsed(self):
        ctx = FakeContext(
            session={"_compositor": "Hyprland"},
            commands={"hyprctl": cp('[{"name":"HDMI-A-1","refreshRate":60.0}]')})
        r = graphics.compositor_outputs(ctx)
        self.assertEqual(r.metrics["compositor_monitors"], 1)

    def test_compositor_outputs_bad_json_warns(self):
        ctx = FakeContext(session={"_compositor": "Hyprland"},
                          commands={"hyprctl": cp("not json")})
        self.assertIs(graphics.compositor_outputs(ctx).status, Status.WARN)

    def test_software_rendering_is_a_failure(self):
        ctx = FakeContext(tools=["glxinfo"], commands={
            "glxinfo": cp("OpenGL renderer string: llvmpipe (LLVM 17)")})
        r = graphics.gpu_accel(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("software rendering", r.message)

    def test_hardware_rendering_passes(self):
        ctx = FakeContext(tools=["glxinfo"], commands={
            "glxinfo": cp("OpenGL renderer string: Mesa Intel(R) UHD Graphics")})
        self.assertIs(graphics.gpu_accel(ctx).status, Status.PASS)

    def test_gpu_accel_no_tools_skips(self):
        self.assertIs(graphics.gpu_accel(FakeContext()).status, Status.SKIP)

    def test_video_decode_absent_tool_skips(self):
        self.assertIs(graphics.video_decode(FakeContext()).status, Status.SKIP)

    def test_video_decode_working(self):
        ctx = FakeContext(tools=["vainfo"],
                          commands={"vainfo": cp("VAProfileH264Main : VAEntrypoint")})
        self.assertIs(graphics.video_decode(ctx).status, Status.PASS)


class TestAudio(unittest.TestCase):
    def test_no_asound_fails(self):
        with mock.patch.object(Path, "exists", return_value=False):
            self.assertIs(audio.sound_card(FakeContext()).status, Status.FAIL)

    def test_driver_bound(self):
        r = audio.sound_driver(FakeContext(commands={"lspci": cp(LSPCI_GPU)}))
        self.assertIs(r.status, Status.PASS)
        self.assertIn("snd_hda_intel", r.message)

    def test_usb_audio_fallback(self):
        ctx = FakeContext(commands={"lspci": cp("00:00.0 Host bridge: x\n"),
                                    "lsmod": cp("snd_usb_audio 1 0\n")})
        self.assertIs(audio.sound_driver(ctx).status, Status.PASS)

    def test_no_audio_driver_warns(self):
        ctx = FakeContext(commands={"lspci": cp("00:00.0 Host bridge: x\n"),
                                    "lsmod": cp("")})
        self.assertIs(audio.sound_driver(ctx).status, Status.WARN)

    def test_pipewire_running(self):
        ctx = FakeContext(commands={"pgrep -x pipewire": cp("1\n"),
                                    "pgrep -x wireplumber": cp("2\n"),
                                    "pgrep -x pipewire-pulse": cp("3\n")})
        self.assertIs(audio.pipewire(ctx).status, Status.PASS)

    def test_wireplumber_missing_warns(self):
        ctx = FakeContext(commands={"pgrep -x pipewire": cp("1\n")},
                          default=cp("", 1))
        self.assertIs(audio.pipewire(ctx).status, Status.WARN)

    def test_no_audio_server_warns(self):
        self.assertIs(audio.pipewire(FakeContext()).status, Status.WARN)

    def test_pulseaudio_fallback(self):
        ctx = FakeContext(commands={"pgrep -x pulseaudio": cp("1\n")})
        self.assertIs(audio.pipewire(ctx).status, Status.PASS)

    def test_sinks_counted(self):
        status = ("Audio\n Sinks:\n  │  *   49. Built-in Audio  [vol: 1.00]\n"
                  " Sources:\n  │      50. Mic  [vol: 1.00]\n")
        ctx = FakeContext(tools=["wpctl"], commands={"wpctl status": cp(status)})
        r = audio.audio_sinks(ctx)
        self.assertEqual(r.metrics["audio_sinks"], 1)

    def test_no_sinks_fails(self):
        ctx = FakeContext(tools=["wpctl"], commands={"wpctl status": cp("Audio\n")})
        self.assertIs(audio.audio_sinks(ctx).status, Status.FAIL)

    def test_sinks_tool_missing_skips(self):
        self.assertIs(audio.audio_sinks(FakeContext()).status, Status.SKIP)

    def test_default_sink_muted_is_info(self):
        ctx = FakeContext(tools=["wpctl"],
                          commands={"get-volume": cp("Volume: 0.50 [MUTED]")})
        self.assertIs(audio.audio_default_sink(ctx).status, Status.INFO)

    def test_no_default_sink_warns(self):
        ctx = FakeContext(tools=["wpctl"], commands={"get-volume": cp("", 1)})
        self.assertIs(audio.audio_default_sink(ctx).status, Status.WARN)

    def test_xruns(self):
        self.assertIs(audio.audio_xruns(FakeContext(journal_all="")).status,
                      Status.PASS)
        r = audio.audio_xruns(FakeContext(journal_all="xrun detected\nxrun again"))
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["audio_xruns"], 2)

    def test_playback(self):
        ctx = FakeContext(commands={"speaker-test": cp("Playback device is default")})
        self.assertIs(audio.audio_playback(ctx).status, Status.PASS)
        bad = FakeContext(commands={"speaker-test": cp("", 1, "no device")})
        self.assertIs(audio.audio_playback(bad).status, Status.FAIL)


class TestNetwork(unittest.TestCase):
    def _net(self, ifaces, wireless=(), virtual=(), oper=None):
        oper = oper or {}
        patches = [
            mock.patch.object(network, "_ifaces", return_value=list(ifaces)),
            mock.patch.object(network, "_is_wireless",
                              side_effect=lambda n: n in wireless),
            mock.patch.object(network, "_is_virtual",
                              side_effect=lambda n: n in virtual),
            mock.patch.object(network, "_operstate",
                              side_effect=lambda n: oper.get(n, "down")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_no_interfaces_fails(self):
        self._net([])
        self.assertIs(network.net_drivers(FakeContext()).status, Status.FAIL)

    def test_ethernet_link_up(self):
        self._net(["eth0", "eth1"], oper={"eth0": "up"})
        with mock.patch.object(Path, "read_text", return_value="1000"):
            r = network.ethernet_link(FakeContext())
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["ethernet_up"], 1)
        self.assertEqual(r.metrics["ethernet_total"], 2)

    def test_no_link_warns(self):
        self._net(["eth0"], oper={})
        self.assertIs(network.ethernet_link(FakeContext()).status, Status.WARN)

    def test_no_ethernet_skips(self):
        self._net(["wlan0"], wireless=["wlan0"])
        self.assertIs(network.ethernet_link(FakeContext()).status, Status.SKIP)

    def test_absent_wifi_skips_not_fails(self):
        self._net(["eth0"])
        self.assertIs(network.wifi(FakeContext()).status, Status.SKIP)
        self.assertIs(network.wifi_scan(FakeContext()).status, Status.SKIP)

    def test_wifi_present_but_unassociated_warns(self):
        self._net(["wlan0"], wireless=["wlan0"])
        ctx = FakeContext(tools=["iw"], commands={"iw dev": cp("Not connected.")})
        self.assertIs(network.wifi(ctx).status, Status.WARN)

    def test_wifi_associated(self):
        self._net(["wlan0"], wireless=["wlan0"])
        out = "Connected to aa:bb\n\tSSID: MyNet\n\tsignal: -45 dBm"
        ctx = FakeContext(tools=["iw"], commands={"iw dev": cp(out)})
        r = network.wifi(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["wifi_associated"], 1)

    def test_wifi_scan_counts_aps(self):
        self._net(["wlan0"], wireless=["wlan0"])
        ctx = FakeContext(tools=["iw"],
                          commands={"scan": cp("BSS aa:bb\nBSS cc:dd\n")})
        r = network.wifi_scan(ctx)
        self.assertEqual(r.metrics["wifi_aps_seen"], 2)

    def test_connectivity_records_no_address(self):
        """Reports get shared; the gateway IP must not appear in them."""
        ctx = FakeContext(commands={
            "ip route": cp("default via 192.168.1.1 dev eth0 proto dhcp"),
            "ping": cp("2 received"),
            "getent": cp("1.2.3.4 archlinux.org")})
        r = network.net_connectivity(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertNotIn("192.168.1.1", r.message)
        self.assertIn("eth0", r.message)

    def test_connectivity_no_route(self):
        ctx = FakeContext(commands={"ip route": cp("")})
        self.assertIs(network.net_connectivity(ctx).status, Status.WARN)

    def test_connectivity_gateway_unreachable(self):
        ctx = FakeContext(commands={
            "ip route": cp("default via 192.168.1.1 dev eth0 proto dhcp"),
            "ping": cp("", 1)})
        r = network.net_connectivity(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertNotIn("192.168.1.1", r.message)

    def test_connectivity_dns_fails(self):
        ctx = FakeContext(commands={
            "ip route": cp("default via 192.168.1.1 dev eth0 proto dhcp"),
            "ping": cp("ok"), "getent": cp("", 1)})
        self.assertIs(network.net_connectivity(ctx).status, Status.WARN)

    def test_bluetooth_absent_skips(self):
        with mock.patch.object(Path, "exists", return_value=False):
            self.assertIs(network.bluetooth(FakeContext()).status, Status.SKIP)

    def test_net_errors(self):
        self.assertIs(network.net_errors(FakeContext(journal_kernel="")).status,
                      Status.PASS)
        r = network.net_errors(
            FakeContext(journal_kernel="NETDEV WATCHDOG: eth0 transmit queue"))
        self.assertIs(r.status, Status.WARN)


class TestPeripherals(unittest.TestCase):
    def test_usb_devices_counted(self):
        listing = ("Bus 001 Device 001: ID 1d6b:0002 Linux Foundation root hub\n"
                   "Bus 001 Device 002: ID 046d:c52b Logitech Receiver\n")
        ctx = FakeContext(tools=["lsusb"], commands={"lsusb": cp(listing)},
                          journal_kernel="")
        r = peripherals.usb(ctx)
        self.assertEqual(r.metrics["usb_devices"], 1)

    def test_usb_errors_warn(self):
        ctx = FakeContext(tools=["lsusb"],
                          commands={"lsusb": cp("Bus 001 Device 001: root hub\n")},
                          journal_kernel="unable to enumerate USB device")
        self.assertIs(peripherals.usb(ctx).status, Status.WARN)

    def test_usb_no_devices_fails(self):
        ctx = FakeContext(tools=["lsusb"], commands={"lsusb": cp("")})
        self.assertIs(peripherals.usb(ctx).status, Status.FAIL)

    def test_storage_io_errors_fail(self):
        ctx = FakeContext(commands={"lsblk": cp("sda 100G disk\n")},
                          journal_kernel="blk_update_request: I/O error")
        r = peripherals.storage(ctx)
        self.assertIs(r.status, Status.FAIL)

    def test_storage_clean(self):
        ctx = FakeContext(commands={"lsblk": cp("sda 100G disk\n")},
                          journal_kernel="")
        r = peripherals.storage(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["disks"], 1)

    def test_storage_no_disks_fails(self):
        ctx = FakeContext(commands={"lsblk": cp("")})
        self.assertIs(peripherals.storage(ctx).status, Status.FAIL)

    def test_thermal_hot_warns(self):
        with mock.patch.object(Path, "exists", return_value=True), \
             mock.patch.object(Path, "glob",
                               return_value=[Path("/sys/class/thermal/thermal_zone0")]), \
             mock.patch.object(Path, "read_text", side_effect=["96000", "x86_pkg"]):
            r = peripherals.thermal(FakeContext())
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["temp_max_c"], 96.0)

    def test_thermal_absent_skips(self):
        with mock.patch.object(Path, "exists", return_value=False):
            self.assertIs(peripherals.thermal(FakeContext()).status, Status.SKIP)

    def test_cpufreq(self):
        with mock.patch.object(Path, "exists", return_value=True):
            ctx = FakeContext(files={
                "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor": "schedutil",
                "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "2000000",
                "/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq": "2700000"})
            r = peripherals.cpufreq(ctx)
        self.assertEqual(r.metrics["cpu_cur_mhz"], 2000)
        self.assertEqual(r.metrics["cpu_max_mhz"], 2700)

    def test_cpufreq_absent_skips(self):
        with mock.patch.object(Path, "exists", return_value=False):
            self.assertIs(peripherals.cpufreq(FakeContext()).status, Status.SKIP)

    def test_webcam_absent_skips(self):
        with mock.patch.object(Path, "glob", return_value=[]):
            self.assertIs(peripherals.webcam(FakeContext()).status, Status.SKIP)


if __name__ == "__main__":
    unittest.main()
