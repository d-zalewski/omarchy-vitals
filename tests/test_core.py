"""Tests for the check framework itself."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import cp  # noqa: E402

from vitals import core  # noqa: E402
from vitals.core import (Check, Context, Fail, Info, Ok, Result, Skip, Status,
                         Warn, check, run_check, select)


class TestResults(unittest.TestCase):
    def test_constructors_set_status_and_metrics(self):
        for fn, status in ((Ok, Status.PASS), (Fail, Status.FAIL),
                           (Warn, Status.WARN), (Skip, Status.SKIP),
                           (Info, Status.INFO)):
            r = fn("msg", a=1, b=2.5)
            self.assertIs(r.status, status)
            self.assertEqual(r.message, "msg")
            self.assertEqual(r.metrics, {"a": 1, "b": 2.5})

    def test_metrics_default_empty(self):
        self.assertEqual(Ok("x").metrics, {})

    def test_status_is_str_enum(self):
        self.assertEqual(Status.PASS.value, "PASS")
        self.assertEqual(f"{Status.FAIL.value}", "FAIL")


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self._saved = list(core.REGISTRY)
        core.REGISTRY.clear()

    def tearDown(self):
        core.REGISTRY[:] = self._saved

    def test_decorator_registers_and_returns_function(self):
        @check(tier=1, name="a", desc="d", requires=["tool"], disruptive=True,
               est_seconds=9)
        def fn(ctx):
            return Ok("ok")

        self.assertEqual(len(core.REGISTRY), 1)
        c = core.REGISTRY[0]
        self.assertEqual((c.name, c.tier, c.desc), ("a", 1, "d"))
        self.assertEqual(c.requires, ("tool",))
        self.assertTrue(c.disruptive)
        self.assertEqual(c.est_seconds, 9)
        self.assertIs(c.fn, fn)                       # decorator is transparent

    def test_check_defaults(self):
        @check(tier=0, name="b", desc="d")
        def fn(ctx):
            return Ok("ok")

        c = core.REGISTRY[0]
        self.assertEqual(c.requires, ())
        self.assertFalse(c.disruptive)
        self.assertEqual(c.est_seconds, 1)


class TestSelect(unittest.TestCase):
    def setUp(self):
        self._saved = list(core.REGISTRY)
        core.REGISTRY.clear()
        core.REGISTRY.extend([
            Check(fn=lambda c: Ok("x"), name="z", tier=0, desc=""),
            Check(fn=lambda c: Ok("x"), name="a", tier=1, desc=""),
            Check(fn=lambda c: Ok("x"), name="m", tier=1, desc="",
                  disruptive=True),
        ])

    def tearDown(self):
        core.REGISTRY[:] = self._saved

    def test_no_filters_returns_all_sorted_by_tier_then_name(self):
        self.assertEqual([c.name for c in select()], ["z", "a", "m"])

    def test_filter_by_tier(self):
        self.assertEqual({c.name for c in select(tiers=[1])}, {"a", "m"})
        self.assertEqual([c.name for c in select(tiers=[0])], ["z"])

    def test_filter_by_name(self):
        self.assertEqual([c.name for c in select(only=["a"])], ["a"])

    def test_skip_disruptive(self):
        names = [c.name for c in select(skip_disruptive=True)]
        self.assertNotIn("m", names)
        self.assertIn("a", names)

    def test_filters_combine(self):
        self.assertEqual(
            [c.name for c in select(tiers=[1], skip_disruptive=True)], ["a"])

    def test_unknown_name_yields_nothing(self):
        self.assertEqual(select(only=["nope"]), [])


class TestRunCheck(unittest.TestCase):
    def test_returns_result_and_duration(self):
        c = Check(fn=lambda ctx: Ok("fine"), name="n", tier=0, desc="")
        res, dur = run_check(c, Context())
        self.assertIs(res.status, Status.PASS)
        self.assertGreaterEqual(dur, 0.0)

    def test_missing_requirement_skips_without_running(self):
        ran = []

        def fn(ctx):
            ran.append(True)
            return Ok("should not happen")

        c = Check(fn=fn, name="n", tier=0, desc="", requires=("definitely-absent",))
        res, dur = run_check(c, Context())
        self.assertIs(res.status, Status.SKIP)
        self.assertIn("definitely-absent", res.message)
        self.assertEqual(ran, [])
        self.assertEqual(dur, 0.0)

    def test_exception_becomes_failure_not_crash(self):
        def boom(ctx):
            raise ValueError("kaboom")

        res, _ = run_check(Check(fn=boom, name="n", tier=0, desc=""), Context())
        self.assertIs(res.status, Status.FAIL)
        self.assertIn("ValueError", res.message)
        self.assertIn("kaboom", res.message)

    def test_none_return_becomes_info(self):
        c = Check(fn=lambda ctx: None, name="n", tier=0, desc="")
        res, _ = run_check(c, Context())
        self.assertIs(res.status, Status.INFO)


class TestContextProcess(unittest.TestCase):
    def test_run_list_command(self):
        r = Context().run(["echo", "hello"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("hello", r.stdout)

    def test_run_shell_string(self):
        r = Context().run("echo shellmode")
        self.assertIn("shellmode", r.stdout)

    def test_run_timeout_returns_124(self):
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("c", 1)):
            r = Context().run(["sleep", "10"], timeout=1)
        self.assertEqual(r.returncode, 124)
        self.assertEqual(r.stderr, "timeout")

    def test_run_generic_exception_is_caught(self):
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            r = Context().run(["whatever"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("nope", r.stderr)

    def test_sudo_prefixes_list_and_string(self):
        ctx = Context()
        with mock.patch.object(Context, "run", return_value=cp()) as m:
            ctx.sudo(["id"])
            self.assertEqual(m.call_args[0][0], ["sudo", "-n", "id"])
        with mock.patch.object(Context, "run", return_value=cp()) as m:
            ctx.sudo("id")
            self.assertIn("sudo -n id", m.call_args[0][0])

    def test_have(self):
        ctx = Context()
        self.assertTrue(ctx.have("sh"))
        self.assertFalse(ctx.have("definitely-not-a-real-binary-xyz"))

    def test_read_and_path_exists(self):
        ctx = Context()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "f"
            f.write_text("  value  \n")
            self.assertTrue(ctx.path_exists(str(f)))
            self.assertEqual(ctx.read(str(f)), "value")
        self.assertFalse(ctx.path_exists("/nonexistent/xyz"))
        self.assertEqual(ctx.read("/nonexistent/xyz", "fallback"), "fallback")

    def test_count_matches_is_case_insensitive_multiline(self):
        ctx = Context()
        self.assertEqual(ctx.count_matches("Foo\nfoo\nFOO", r"^foo$"), 3)
        self.assertEqual(ctx.count_matches("", r"x"), 0)


class TestContextCaching(unittest.TestCase):
    def test_journal_kernel_read_once(self):
        ctx = Context()
        with mock.patch.object(Context, "run", return_value=cp("kernel log")) as m:
            self.assertEqual(ctx.journal_kernel, "kernel log")
            self.assertEqual(ctx.journal_kernel, "kernel log")
            self.assertEqual(m.call_count, 1)          # cached, not re-read

    def test_journal_all_read_once(self):
        ctx = Context()
        with mock.patch.object(Context, "run", return_value=cp("all log")) as m:
            self.assertEqual(ctx.journal_all, "all log")
            self.assertEqual(ctx.journal_all, "all log")
            self.assertEqual(m.call_count, 1)

    def test_kconfig_success_and_failure(self):
        ctx = Context()
        with mock.patch.object(Context, "sudo",
                               return_value=cp("CONFIG_X=y\n")) as m:
            self.assertIn("CONFIG_X=y", ctx.kconfig)
            _ = ctx.kconfig
            self.assertEqual(m.call_count, 1)
        failed = Context()
        with mock.patch.object(Context, "sudo", return_value=cp("", 1)):
            self.assertEqual(failed.kconfig, "")

    def test_config_is_set(self):
        ctx = Context()
        ctx._config = "CONFIG_A=y\nCONFIG_B=m\n# CONFIG_C is not set\n"
        self.assertTrue(ctx.config_is_set("CONFIG_A"))
        self.assertTrue(ctx.config_is_set("CONFIG_B"))
        self.assertFalse(ctx.config_is_set("CONFIG_C"))


class TestSessionEnv(unittest.TestCase):
    ENV = ("WAYLAND_DISPLAY=wayland-1\0XDG_RUNTIME_DIR=/run/user/1000\0"
           "IRRELEVANT=drop-me\0")

    def test_no_compositor_returns_empty(self):
        ctx = Context()
        with mock.patch.object(Context, "run", return_value=cp("", 1)):
            self.assertEqual(ctx.session_env(), {})

    def test_reads_compositor_environ_and_filters(self):
        ctx = Context()
        with mock.patch.object(Context, "run", return_value=cp("4242\n")), \
             mock.patch.object(Path, "read_bytes",
                               return_value=self.ENV.encode()), \
             mock.patch.object(Path, "is_dir", return_value=False):
            env = ctx.session_env()
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")
        self.assertEqual(env["_compositor"], "Hyprland")
        self.assertNotIn("IRRELEVANT", env)

    def test_result_is_cached(self):
        ctx = Context()
        ctx._session_env = {"_compositor": "sway"}
        with mock.patch.object(Context, "run",
                               side_effect=AssertionError("should not run")):
            self.assertEqual(ctx.session_env()["_compositor"], "sway")

    def test_hyprland_signature_recovered_from_runtime_dir(self):
        """Hyprland exports the signature to children but not to itself."""
        ctx = Context()
        with tempfile.TemporaryDirectory() as d:
            inst = Path(d) / "hypr" / "abc123"
            inst.mkdir(parents=True)
            env = f"XDG_RUNTIME_DIR={d}\0"
            with mock.patch.object(Context, "run", return_value=cp("1\n")), \
                 mock.patch.object(Path, "read_bytes", return_value=env.encode()):
                out = ctx.session_env()
        self.assertEqual(out["HYPRLAND_INSTANCE_SIGNATURE"], "abc123")

    def test_wayland_display_recovered_from_runtime_dir(self):
        """A compositor creates its socket after exec, so its environ lacks it.

        grim connects to wayland-0 without this and fails on a machine whose
        socket is wayland-1 - which is every Hyprland session seen so far.
        """
        ctx = Context()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "wayland-1").touch()
            (Path(d) / "wayland-1.lock").touch()
            env = f"XDG_RUNTIME_DIR={d}\0"
            with mock.patch.object(Context, "run", return_value=cp("1\n")), \
                 mock.patch.object(Path, "read_bytes", return_value=env.encode()):
                out = ctx.session_env()
        self.assertEqual(out["WAYLAND_DISPLAY"], "wayland-1")

    def test_wayland_display_in_environ_is_left_alone(self):
        ctx = Context()
        with mock.patch.object(Context, "run", return_value=cp("1\n")), \
             mock.patch.object(Path, "read_bytes", return_value=self.ENV.encode()), \
             mock.patch.object(Path, "is_dir", return_value=False), \
             mock.patch.object(Path, "glob",
                               side_effect=AssertionError("should not look")):
            self.assertEqual(ctx.session_env()["WAYLAND_DISPLAY"], "wayland-1")

    def test_no_socket_leaves_wayland_display_unset(self):
        ctx = Context()
        with tempfile.TemporaryDirectory() as d:
            env = f"XDG_RUNTIME_DIR={d}\0"
            with mock.patch.object(Context, "run", return_value=cp("1\n")), \
                 mock.patch.object(Path, "read_bytes", return_value=env.encode()):
                out = ctx.session_env()
        self.assertNotIn("WAYLAND_DISPLAY", out)

    def test_unreadable_environ_is_skipped(self):
        ctx = Context()
        with mock.patch.object(Context, "run", return_value=cp("1\n")), \
             mock.patch.object(Path, "read_bytes", side_effect=OSError("denied")):
            self.assertEqual(ctx.session_env(), {})

    def test_run_in_session_merges_env(self):
        ctx = Context()
        ctx._session_env = {"WAYLAND_DISPLAY": "wayland-9", "_compositor": "Hyprland"}
        with mock.patch("subprocess.run", return_value=cp("out")) as m:
            ctx.run_in_session(["true"])
        passed = m.call_args.kwargs["env"]
        self.assertEqual(passed["WAYLAND_DISPLAY"], "wayland-9")
        self.assertNotIn("_compositor", passed)        # internal key not exported

    def test_run_in_session_timeout_and_error(self):
        ctx = Context()
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("c", 1)):
            self.assertEqual(ctx.run_in_session(["x"]).returncode, 124)
        with mock.patch("subprocess.run", side_effect=OSError("bad")):
            r = ctx.run_in_session(["x"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("bad", r.stderr)


class TestMetricMetadata(unittest.TestCase):
    def test_direction_sets_are_disjoint(self):
        self.assertFalse(core.LOWER_IS_BETTER & core.HIGHER_IS_BETTER)

    def test_latency_metrics_are_lower_is_better(self):
        for k in ("cyclictest_idle_max_us", "ctxsw_usecs_op", "hackbench_sec"):
            self.assertIn(k, core.LOWER_IS_BETTER)

    def test_throughput_metrics_are_higher_is_better(self):
        for k in ("fio_randread_iops", "ctxsw_ops_sec", "loopback_gbit_s"):
            self.assertIn(k, core.HIGHER_IS_BETTER)


if __name__ == "__main__":
    unittest.main()
