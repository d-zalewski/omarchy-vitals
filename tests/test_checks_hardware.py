"""Tier 1 hardware checks: graphics, audio, network, peripherals.

The distinction these must get right is absent hardware (SKIP) versus present
but broken hardware (FAIL), so most modules are tested for both.
"""
from __future__ import annotations

import struct
import unittest
from pathlib import Path
from unittest import mock

from fakefs import fake_fs  # noqa: E402
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


class TestScreenshot(unittest.TestCase):
    """The only check that looks at pixels, so every way that can go wrong."""

    SESSION = {"_compositor": "Hyprland", "WAYLAND_DISPLAY": "wayland-1"}

    def png(self, width=2560, height=1440, size=200_000):
        head = graphics.PNG_MAGIC + b"\x00" * 8 + struct.pack(">II", width, height)
        return head + b"\x00" * max(0, size - len(head))

    MONITORS = '[{"name": "HDMI-A-1", "dpmsStatus": true}]'
    ASLEEP = '[{"name": "HDMI-A-1", "dpmsStatus": false}]'

    def capture(self, ctx, data=None, exists=True):
        with mock.patch.object(Path, "exists", return_value=exists), \
             mock.patch.object(Path, "read_bytes",
                               return_value=data if data is not None else self.png()):
            return graphics.screenshot(ctx)

    def test_no_session_skips(self):
        self.assertIs(graphics.screenshot(FakeContext(session={})).status,
                      Status.SKIP)

    def test_non_wlroots_compositor_skips(self):
        """grim cannot capture under GNOME, which says nothing about GNOME."""
        ctx = FakeContext(session={"_compositor": "gnome-shell"})
        r = graphics.screenshot(ctx)
        self.assertIs(r.status, Status.SKIP)
        self.assertIn("gnome-shell", r.message)

    def test_sleeping_output_is_still_tried(self):
        """dpmsStatus is a preference: a VNC output reports asleep and still
        delivers frames instantly."""
        ctx = FakeContext(session=self.SESSION,
                          commands={"hyprctl": cp(self.ASLEEP), "grim": cp("")})
        r = self.capture(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertIn("grim -o HDMI-A-1", " ".join(ctx.calls))

    def test_blank_frame_from_a_sleeping_output_skips(self):
        """A screen the compositor already turned off owes nobody a picture."""
        ctx = FakeContext(session=self.SESSION,
                          commands={"hyprctl": cp(self.ASLEEP), "grim": cp("")})
        r = self.capture(ctx, data=self.png(size=6_121))
        self.assertIs(r.status, Status.SKIP)
        self.assertIn("asleep", r.message)

    def test_awake_output_is_captured_by_name(self):
        ctx = FakeContext(session=self.SESSION,
                          commands={"hyprctl": cp(self.MONITORS), "grim": cp("")})
        r = self.capture(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertIn("HDMI-A-1", r.message)
        self.assertIn("grim -o HDMI-A-1", " ".join(ctx.calls))

    def test_sway_captures_everything(self):
        """Only Hyprland can be asked which output is awake; sway still works."""
        ctx = FakeContext(session={"_compositor": "sway"}, commands={"grim": cp("")})
        r = self.capture(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertNotIn("-o", " ".join(ctx.calls))

    def test_unparseable_monitor_list_captures_everything(self):
        ctx = FakeContext(session=self.SESSION,
                          commands={"hyprctl": cp("not json"), "grim": cp("")})
        self.assertIs(self.capture(ctx).status, Status.PASS)

    def test_blocked_capture_skips_rather_than_fails(self):
        ctx = FakeContext(session=self.SESSION,
                          commands={"grim": cp("", 124, "timeout")})
        r = self.capture(ctx)
        self.assertIs(r.status, Status.SKIP)
        self.assertIn("no frame", r.message)

    def test_grim_failure_fails(self):
        ctx = FakeContext(session=self.SESSION,
                          commands={"grim": cp("", 1, "compositor does not support"
                                                      " wlr-screencopy")})
        r = self.capture(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("wlr-screencopy", r.message)

    def test_missing_output_file_fails(self):
        ctx = FakeContext(session=self.SESSION, commands={"grim": cp("")})
        r = self.capture(ctx, exists=False)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("no output file", r.message)

    def test_not_a_png_fails(self):
        ctx = FakeContext(session=self.SESSION, commands={"grim": cp("")})
        r = self.capture(ctx, data=b"not a png at all, but long enough")
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("not a PNG", r.message)

    def test_zero_dimensions_fail(self):
        ctx = FakeContext(session=self.SESSION, commands={"grim": cp("")})
        r = self.capture(ctx, data=self.png(width=0, height=0))
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("no dimensions", r.message)

    def test_blank_frame_from_an_awake_output_warns(self):
        """A screen that renders nothing compresses to almost nothing.

        6,121 bytes is what a blank 1920x1080 frame measured on real hardware.
        """
        ctx = FakeContext(session=self.SESSION,
                          commands={"hyprctl": cp(self.MONITORS), "grim": cp("")})
        r = self.capture(ctx, data=self.png(size=6_121))
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["screenshot_pixels"], 2560 * 1440)

    def test_real_frame_passes(self):
        ctx = FakeContext(session=self.SESSION, commands={"grim": cp("")})
        r = self.capture(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertIn("2560x1440", r.message)
        self.assertEqual(r.metrics["screenshot_pixels"], 2560 * 1440)


class TestScreencastPortal(unittest.TestCase):
    SESSION = {"_compositor": "Hyprland"}

    def test_no_session_skips(self):
        self.assertIs(graphics.screencast_portal(FakeContext(session={})).status,
                      Status.SKIP)

    def test_portal_not_installed_skips(self):
        ctx = FakeContext(session=self.SESSION, commands={"busctl": cp(
            "", 1, "Failed to call method: The name org.freedesktop.portal.Desktop"
                   " was not provided by any .service files")})
        self.assertIs(graphics.screencast_portal(ctx).status, Status.SKIP)

    def test_portal_error_warns(self):
        ctx = FakeContext(session=self.SESSION,
                          commands={"busctl": cp("", 1, "Connection timed out")})
        r = graphics.screencast_portal(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["screencast_portal"], 0)

    def test_unparseable_reply_warns(self):
        ctx = FakeContext(session=self.SESSION,
                          commands={"busctl": cp("something else entirely")})
        self.assertIs(graphics.screencast_portal(ctx).status, Status.WARN)

    def test_version_reply_passes(self):
        ctx = FakeContext(session=self.SESSION, commands={"busctl": cp("v u 5\n")})
        r = graphics.screencast_portal(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertIn("version 5", r.message)
        self.assertEqual(r.metrics["screencast_portal"], 1)


GLMARK2 = """=====================================
    glmark2 2023.01
=====================================
    OpenGL Information
    GL_VENDOR:     Intel
    GL_RENDERER:   Mesa Intel(R) UHD Graphics 770
    GL_VERSION:    4.6 (Compatibility Profile) Mesa 25.2.0
=====================================
[build] use-vbo=false: FPS: 1836 FrameTime: 0.545 ms
=====================================
                                  glmark2 Score: 1836
=====================================
"""


class TestGlRender(unittest.TestCase):
    DRI = {"/dev/dri": ["renderD128", "card0"]}
    TOOLS = ["glmark2-wayland"]

    def ctx(self, out, tools=None):
        return FakeContext(commands={"glmark2": out},
                           tools=self.TOOLS if tools is None else tools)

    def test_no_render_node_skips(self):
        with fake_fs({}):
            self.assertIs(graphics.gl_render(FakeContext()).status, Status.SKIP)

    def test_no_glmark2_installed_skips(self):
        with fake_fs(self.DRI):
            self.assertIs(graphics.gl_render(self.ctx(cp(""), tools=[])).status,
                          Status.SKIP)

    def test_wayland_build_is_preferred_over_the_glx_one(self):
        """Plain glmark2 is the GLX build and cannot open a Wayland canvas."""
        with fake_fs(self.DRI):
            ctx = self.ctx(cp(GLMARK2), tools=["glmark2", "glmark2-wayland"])
            r = graphics.gl_render(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertTrue(any(c.startswith("glmark2-wayland") for c in ctx.calls))

    def test_software_rendering_fails(self):
        soft = GLMARK2.replace("Mesa Intel(R) UHD Graphics 770", "llvmpipe (LLVM 20)")
        with fake_fs(self.DRI):
            r = graphics.gl_render(self.ctx(cp(soft)))
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("software", r.message)
        self.assertEqual(r.metrics["glmark2_score"], 0)

    def test_no_score_warns(self):
        """Offscreen GL has packaging quirks; a run that never started is not
        the same finding as a GPU that renders in software."""
        with fake_fs(self.DRI):
            r = graphics.gl_render(self.ctx(
                cp("", 1, "Error: main: Could not initialize canvas")))
        self.assertIs(r.status, Status.WARN)
        self.assertIn("Could not initialize", r.message)

    def test_no_output_at_all_warns(self):
        with fake_fs(self.DRI):
            r = graphics.gl_render(self.ctx(cp("")))
        self.assertIs(r.status, Status.WARN)
        self.assertIn("no output", r.message)

    def test_zero_score_fails(self):
        with fake_fs(self.DRI):
            r = graphics.gl_render(self.ctx(
                cp(GLMARK2.replace("Score: 1836", "Score: 0"))))
        self.assertIs(r.status, Status.FAIL)
        self.assertEqual(r.metrics["glmark2_score"], 0)

    def test_score_passes_and_names_the_renderer(self):
        with fake_fs(self.DRI):
            r = graphics.gl_render(self.ctx(cp(GLMARK2)))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["glmark2_score"], 1836)
        self.assertIn("Mesa Intel", r.message)


LIBINPUT = """Device:           AT Translated Set 2 keyboard
Kernel:           /dev/input/event3
Capabilities:     keyboard
Tap-to-click:     n/a

Device:           Logitech Wireless Mouse
Kernel:           /dev/input/event5
Capabilities:     pointer
Tap-to-click:     n/a
"""


class TestLibinputDevices(unittest.TestCase):
    def test_without_sudo_skips(self):
        ctx = FakeContext(commands={"libinput": cp(
            "", 1, "sudo: a password is required")})
        self.assertIs(peripherals.libinput_devices(ctx).status, Status.SKIP)

    def test_other_failure_warns(self):
        ctx = FakeContext(commands={"libinput": cp("", 1, "failed to open /dev/input")})
        r = peripherals.libinput_devices(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertIn("/dev/input", r.message)

    def test_no_devices_skips(self):
        """input_devices already fails on a machine with no input at all."""
        ctx = FakeContext(commands={"libinput": cp("")})
        self.assertIs(peripherals.libinput_devices(ctx).status, Status.SKIP)

    def test_nothing_usable_warns(self):
        listing = LIBINPUT.replace("keyboard", "switch").replace("pointer", "switch")
        ctx = FakeContext(commands={"libinput": cp(listing)})
        r = peripherals.libinput_devices(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["libinput_devices"], 2)
        self.assertEqual(r.metrics["libinput_keyboards"], 0)

    def test_keyboard_and_pointer_pass(self):
        r = peripherals.libinput_devices(FakeContext(commands={"libinput": cp(LIBINPUT)}))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["libinput_devices"], 2)
        self.assertEqual(r.metrics["libinput_keyboards"], 1)
        self.assertEqual(r.metrics["libinput_pointers"], 1)

    def test_device_names_are_never_recorded(self):
        r = peripherals.libinput_devices(FakeContext(commands={"libinput": cp(LIBINPUT)}))
        self.assertNotIn("Logitech", r.message)


class TestCpuidle(unittest.TestCase):
    BASE = "/sys/devices/system/cpu/cpu0/cpuidle"

    def tree(self, states):
        return {self.BASE: list(states)}

    def files(self, **kw):
        return {f"{self.BASE}/{k}": v for k, v in kw.items()}

    def test_no_cpuidle_skips(self):
        with fake_fs({}):
            self.assertIs(peripherals.cpuidle(FakeContext()).status, Status.SKIP)

    def test_no_states_skips(self):
        with fake_fs(self.tree([])):
            self.assertIs(peripherals.cpuidle(FakeContext()).status, Status.SKIP)

    def test_never_idling_warns(self):
        ctx = FakeContext(files=self.files(**{"state0/name": "POLL",
                                              "state0/usage": "0",
                                              "state1/name": "C6",
                                              "state1/usage": "0"}))
        with fake_fs(self.tree(["state0", "state1"])):
            r = peripherals.cpuidle(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["cpuidle_states_used"], 0)

    def test_deepest_state_unused_warns(self):
        ctx = FakeContext(files=self.files(**{"state0/name": "POLL",
                                              "state0/usage": "500",
                                              "state1/name": "C6",
                                              "state1/usage": "0"}))
        with fake_fs(self.tree(["state0", "state1"])):
            r = peripherals.cpuidle(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertIn("C6", r.message)
        self.assertEqual(r.metrics["cpuidle_states_used"], 1)

    def test_deep_states_used_passes(self):
        ctx = FakeContext(files=self.files(**{"state0/name": "POLL",
                                              "state0/usage": "500",
                                              "state1/name": "C6",
                                              "state1/usage": "9000"}))
        with fake_fs(self.tree(["state0", "state1"])):
            r = peripherals.cpuidle(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["cpuidle_states"], 2)
        self.assertEqual(r.metrics["cpuidle_states_used"], 2)

    def test_states_are_ordered_numerically_not_lexically(self):
        """state10 is deeper than state2, and sorts before it as text."""
        ctx = FakeContext(files=self.files(**{"state2/name": "C1",
                                              "state2/usage": "5",
                                              "state10/name": "C10",
                                              "state10/usage": "7",
                                              "odd/name": "?", "odd/usage": "1"}))
        with fake_fs(self.tree(["state2", "state10", "odd"])):
            r = peripherals.cpuidle(ctx)
        self.assertIn("deepest C10", r.message)

    def test_unreadable_usage_counts_as_unused(self):
        ctx = FakeContext(files=self.files(**{"state0/name": "C1",
                                              "state0/usage": "<error>"}))
        with fake_fs(self.tree(["state0"])):
            self.assertIs(peripherals.cpuidle(ctx).status, Status.WARN)


class TestBluetoothScan(unittest.TestCase):
    HCI = {"/sys/class/bluetooth": ["hci0"]}

    def test_no_adapter_skips(self):
        with fake_fs({}):
            self.assertIs(network.bluetooth_scan(FakeContext()).status, Status.SKIP)

    def test_timeout_warns(self):
        with fake_fs(self.HCI):
            r = network.bluetooth_scan(
                FakeContext(commands={"bluetoothctl": cp("", 124, "timeout")}))
        self.assertIs(r.status, Status.WARN)

    def test_no_controller_fails(self):
        """The adapter is there and the daemon cannot use it - a real break."""
        with fake_fs(self.HCI):
            r = network.bluetooth_scan(FakeContext(
                commands={"bluetoothctl": cp("No default controller available\n", 1)}))
        self.assertIs(r.status, Status.FAIL)

    def test_old_bluetoothctl_skips(self):
        with fake_fs(self.HCI):
            r = network.bluetooth_scan(FakeContext(
                commands={"bluetoothctl": cp("", 1, "Invalid argument --timeout")}))
        self.assertIs(r.status, Status.SKIP)

    def test_failure_without_output_skips(self):
        with fake_fs(self.HCI):
            r = network.bluetooth_scan(FakeContext(
                commands={"bluetoothctl": cp("", 1)}))
        self.assertIs(r.status, Status.SKIP)
        self.assertIn("no output", r.message)

    def test_empty_room_is_not_a_fault(self):
        with fake_fs(self.HCI):
            r = network.bluetooth_scan(FakeContext(
                commands={"bluetoothctl": cp("Discovery started\n")}))
        self.assertIs(r.status, Status.INFO)
        self.assertEqual(r.metrics["bt_devices_seen"], 0)

    def test_devices_are_counted_once_and_never_recorded(self):
        out = ("Discovery started\n"
               "[NEW] Device AA:BB:CC:DD:EE:FF Someone's Phone\n"
               "[CHG] Device AA:BB:CC:DD:EE:FF RSSI: -70\n"
               "[NEW] Device 11:22:33:44:55:66 Headphones\n")
        with fake_fs(self.HCI):
            r = network.bluetooth_scan(FakeContext(commands={"bluetoothctl": cp(out)}))
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["bt_devices_seen"], 2)
        self.assertNotIn("AA:BB", r.message)
        self.assertNotIn("Phone", r.message)


if __name__ == "__main__":
    unittest.main()
