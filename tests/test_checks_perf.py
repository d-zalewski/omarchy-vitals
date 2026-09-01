"""Latency, throughput, stress and toolchain checks."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from helpers import FakeContext, cp  # noqa: E402

from vitals.checks import kernel_build, latency, stress, throughput  # noqa: E402
from vitals.core import Status  # noqa: E402

CYCLICTEST = "T: 0 (1234) P:80 I:200 C:  10000 Min:      2 Act:    3 Avg:    5 Max:     900"


class TestLatencyParsing(unittest.TestCase):
    def test_parse_takes_worst_across_threads(self):
        text = ("Avg:    5 Max:     900\n"
                "Avg:    7 Max:    1200\n")
        self.assertEqual(latency._parse_cyclictest(text), (7, 1200))

    def test_parse_empty(self):
        self.assertEqual(latency._parse_cyclictest(""), (None, None))

    def test_grade_thresholds_follow_perception(self):
        self.assertIs(latency._grade("l", 5, 900, "idle").status, Status.PASS)
        self.assertIs(latency._grade("l", 5, 5000, "idle").status, Status.WARN)
        self.assertIs(latency._grade("l", 5, 20000, "idle").status, Status.FAIL)

    def test_grade_missing_data_fails(self):
        self.assertIs(latency._grade("l", None, None, "idle").status, Status.FAIL)

    def test_grade_emits_prefixed_metrics(self):
        m = latency._grade("l", 5, 900, "loaded").metrics
        self.assertEqual(m["cyclictest_loaded_avg_us"], 5)
        self.assertEqual(m["cyclictest_loaded_max_us"], 900)


class TestLatencyChecks(unittest.TestCase):
    def test_idle_runs(self):
        ctx = FakeContext(commands={"cyclictest": cp(CYCLICTEST)})
        r = latency.cyclictest_idle(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["cyclictest_idle_max_us"], 900)

    def test_idle_failure_reported(self):
        ctx = FakeContext(commands={"cyclictest": cp("", 1, "permission denied")})
        self.assertIs(latency.cyclictest_idle(ctx).status, Status.FAIL)

    def test_loaded_starts_and_stops_load(self):
        ctx = FakeContext(commands={"cyclictest": cp(CYCLICTEST),
                                    "sleep": cp("")})
        proc = mock.MagicMock(pid=4242)
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch("subprocess.run", return_value=cp()), \
             mock.patch("os.killpg") as killpg, \
             mock.patch("os.getpgid", return_value=4242):
            r = latency.cyclictest_loaded(ctx)
        self.assertIs(r.status, Status.PASS)
        killpg.assert_called_once()                    # load always torn down

    def test_loaded_kills_load_even_when_killpg_fails(self):
        ctx = FakeContext(commands={"cyclictest": cp(CYCLICTEST)})
        proc = mock.MagicMock(pid=1)
        with mock.patch("subprocess.Popen", return_value=proc), \
             mock.patch("subprocess.run", return_value=cp()), \
             mock.patch("os.getpgid", side_effect=OSError("gone")):
            latency.cyclictest_loaded(ctx)
        proc.terminate.assert_called_once()

    def test_hackbench_parsed(self):
        ctx = FakeContext(commands={"hackbench": cp("Time: 3.382")})
        r = latency.hackbench(ctx)
        self.assertEqual(r.metrics["hackbench_sec"], 3.382)

    def test_hackbench_unparseable_skips(self):
        ctx = FakeContext(commands={"hackbench": cp("garbage")})
        self.assertIs(latency.hackbench(ctx).status, Status.SKIP)

    def test_preempt_config(self):
        ctx = FakeContext(kconfig="CONFIG_PREEMPT=y\nCONFIG_HIGH_RES_TIMERS=y\n")
        self.assertIs(latency.preempt_config(ctx).status, Status.PASS)

    def test_preempt_config_absent_skips(self):
        self.assertIs(latency.preempt_config(FakeContext()).status, Status.SKIP)

    def test_non_preemptible_warns(self):
        ctx = FakeContext(kconfig="CONFIG_HZ=250\n")
        self.assertIs(latency.preempt_config(ctx).status, Status.WARN)

    def test_sched_bore_states_recorded_as_metric(self):
        on = FakeContext(commands={"sched_bore": cp("1\n")})
        r = latency.sched_bore(on)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["sched_bore"], 1)

        off = FakeContext(commands={"sched_bore": cp("0\n")})
        r = latency.sched_bore(off)
        self.assertIs(r.status, Status.WARN)
        self.assertEqual(r.metrics["sched_bore"], 0)

        absent = FakeContext(commands={"sched_bore": cp("", 1)})
        r = latency.sched_bore(absent)
        self.assertIs(r.status, Status.INFO)
        self.assertEqual(r.metrics["sched_bore"], -1)


class TestThroughput(unittest.TestCase):
    def test_perf_version_guard_blocks_mismatch(self):
        """A skipped row is better than a wrong one in an A/B."""
        ctx = FakeContext(commands={"perf --version": cp("perf version 7.2.2\n")})
        with mock.patch("platform.release", return_value="7.1.11-arch1-1"):
            usable, why = throughput._perf_usable(ctx)
        self.assertFalse(usable)
        self.assertIn("mismatch", why)

    def test_perf_version_guard_allows_match(self):
        ctx = FakeContext(commands={"perf --version": cp("perf version 7.2.2\n")})
        with mock.patch("platform.release", return_value="7.2.2-5-omarchy-bore"):
            usable, _ = throughput._perf_usable(ctx)
        self.assertTrue(usable)

    def test_perf_version_unreadable(self):
        ctx = FakeContext(commands={"perf --version": cp("garbage")})
        usable, why = throughput._perf_usable(ctx)
        self.assertFalse(usable)
        self.assertIn("unreadable", why)

    def test_sched_pipe_parsed(self):
        out = "       5.234000 usecs/op\n         191052 ops/sec\n"
        ctx = FakeContext(commands={"perf --version": cp("perf version 7.2.2\n"),
                                    "sched pipe": cp(out)})
        with mock.patch("platform.release", return_value="7.2.2-x"):
            r = throughput.perf_sched_pipe(ctx)
        self.assertEqual(r.metrics["ctxsw_usecs_op"], 5.234)
        self.assertEqual(r.metrics["ctxsw_ops_sec"], 191052)

    def test_sched_pipe_skips_on_mismatch(self):
        ctx = FakeContext(commands={"perf --version": cp("perf version 9.9\n")})
        with mock.patch("platform.release", return_value="7.2.2-x"):
            self.assertIs(throughput.perf_sched_pipe(ctx).status, Status.SKIP)

    def test_sched_messaging(self):
        ctx = FakeContext(commands={"perf --version": cp("perf version 7.2.2\n"),
                                    "messaging": cp("Total time: 1.726 [sec]")})
        with mock.patch("platform.release", return_value="7.2.2-x"):
            r = throughput.perf_sched_messaging(ctx)
        self.assertEqual(r.metrics["sched_messaging_sec"], 1.726)

    def test_syscall_missing_subcommand_skips(self):
        ctx = FakeContext(commands={"perf --version": cp("perf version 7.2.2\n"),
                                    "syscall": cp("Unknown subcommand")})
        with mock.patch("platform.release", return_value="7.2.2-x"):
            self.assertIs(throughput.perf_syscall(ctx).status, Status.SKIP)

    def test_mem_takes_best_rate(self):
        out = "default: 3.10 GB/sec\nx86-64-movsq: 4.78 GB/sec\n"
        ctx = FakeContext(commands={"perf --version": cp("perf version 7.2.2\n"),
                                    "mem memcpy": cp(out)})
        with mock.patch("platform.release", return_value="7.2.2-x"):
            r = throughput.perf_mem(ctx)
        self.assertEqual(r.metrics["memcpy_gb_sec"], 4.78)

    def test_sysbench_threads(self):
        out = ("total number of events:              23038\n"
               "         95th percentile:                   25.74\n")
        ctx = FakeContext(commands={"sysbench threads": cp(out)})
        r = throughput.sysbench_threads(ctx)
        self.assertEqual(r.metrics["sysbench_threads_events"], 23038)
        self.assertEqual(r.metrics["sysbench_threads_p95_ms"], 25.74)

    def test_sysbench_threads_no_output_warns(self):
        ctx = FakeContext(commands={"sysbench threads": cp("")})
        self.assertIs(throughput.sysbench_threads(ctx).status, Status.WARN)

    def test_sysbench_cpu_is_labelled_a_control(self):
        ctx = FakeContext(commands={"sysbench cpu": cp("events per second: 2164.00")})
        r = throughput.sysbench_cpu(ctx)
        self.assertEqual(r.metrics["sysbench_cpu_eps"], 2164.0)
        self.assertIn("control", r.message)

    def test_sysbench_cpu_no_output_warns(self):
        ctx = FakeContext(commands={"sysbench cpu": cp("")})
        self.assertIs(throughput.sysbench_cpu(ctx).status, Status.WARN)

    def test_iperf_loopback(self):
        payload = '{"end":{"sum_received":{"bits_per_second":19130000000}}}'
        ctx = FakeContext(commands={"iperf3 -c": cp(payload)})
        with mock.patch("subprocess.Popen", return_value=mock.MagicMock()), \
             mock.patch("time.sleep"):
            r = throughput.iperf_loopback(ctx)
        self.assertEqual(r.metrics["loopback_gbit_s"], 19.13)

    def test_iperf_bad_json_warns(self):
        ctx = FakeContext(commands={"iperf3 -c": cp("not json")})
        with mock.patch("subprocess.Popen", return_value=mock.MagicMock()), \
             mock.patch("time.sleep"):
            self.assertIs(throughput.iperf_loopback(ctx).status, Status.WARN)

    def test_bogo_ops_labelled_with_version(self):
        out = "stress-ng: metrc: [1]  cpu   210284   60.00   239.00"
        ctx = FakeContext(commands={"stress-ng --version": cp("stress-ng, version 0.22.00"),
                                    "--cpu": cp(out)})
        r = throughput.stress_throughput(ctx)
        self.assertEqual(r.metrics["stress_bogo_ops"], 210284)
        self.assertIn("0.22.00", r.message)            # version pinned to number

    def test_bogo_ops_unparseable_warns(self):
        ctx = FakeContext(commands={"stress-ng --version": cp("stress-ng, version 1"),
                                    "--cpu": cp("no numbers here")})
        self.assertIs(throughput.stress_throughput(ctx).status, Status.WARN)


class TestStressAndSuspend(unittest.TestCase):
    def test_suspend_unsupported_skips(self):
        ctx = FakeContext(files={"/sys/power/state": "freeze"})
        self.assertIs(stress.suspend_resume(ctx).status, Status.SKIP)

    def test_suspend_failure_reported(self):
        ctx = FakeContext(files={"/sys/power/state": "freeze mem disk"},
                          commands={"rtcwake": cp("", 1, "rtcwake failed")})
        r = stress.suspend_resume(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertIn("cycle 1", r.message)

    def test_suspend_snapshot_tolerates_missing_paths(self):
        """After a failed resume a whole sysfs directory can be gone."""
        ctx = FakeContext(files={"/sys/power/state": "freeze mem disk"},
                          commands={"rtcwake": cp("ok"), "journalctl": cp("")})
        with mock.patch("time.sleep"), \
             mock.patch.object(Path, "is_dir", return_value=False):
            r = stress.suspend_resume(ctx)
        self.assertIs(r.status, Status.PASS)           # no crash, nothing lost

    def test_suspend_reports_pm_errors_as_warning(self):
        ctx = FakeContext(files={"/sys/power/state": "mem"},
                          commands={"rtcwake": cp("ok"),
                                    "journalctl": cp("PM: suspend failed")})
        with mock.patch("time.sleep"), \
             mock.patch.object(Path, "is_dir", return_value=False):
            r = stress.suspend_resume(ctx)
        self.assertIs(r.status, Status.WARN)
        self.assertGreater(r.metrics["resume_errors"], 0)

    def test_disk_io_parses_iops(self):
        terse = ";".join(["0"] * 7 + ["12345"] + ["0"] * 10)
        ctx = FakeContext(commands={"fio": cp(terse)})
        with mock.patch.object(Path, "glob", return_value=[]):
            r = stress.disk_io(ctx)
        self.assertEqual(r.metrics["fio_randread_iops"], 12345)

    def test_disk_io_failure_warns(self):
        ctx = FakeContext(commands={"fio": cp("", 1, "no space")})
        with mock.patch.object(Path, "glob", return_value=[]):
            self.assertIs(stress.disk_io(ctx).status, Status.WARN)

    def test_disk_io_unparseable(self):
        ctx = FakeContext(commands={"fio": cp("garbage")})
        with mock.patch.object(Path, "glob", return_value=[]):
            self.assertIs(stress.disk_io(ctx).status, Status.INFO)


class TestKernelBuild(unittest.TestCase):
    def test_compiler_handles_nested_parentheses(self):
        """The cross-toolchain string nests brackets; a naive regex stops early."""
        ver = ("Linux version 7.2.2 (builder@host) (x86_64-pc-linux-gnu-gcc "
               "(marchy cross / Arch 16.2.1+r23) 16.2.1 20260810, GNU ld 2.47) #1")
        r = kernel_build.compiler(FakeContext(files={"/proc/version": ver}))
        self.assertIn("gcc", r.message)
        self.assertIn("16.2.1", r.message)

    def test_compiler_unparseable(self):
        r = kernel_build.compiler(FakeContext(files={"/proc/version": "nothing"}))
        self.assertIn("not parseable", r.message)

    def test_btf_missing_fails(self):
        with mock.patch.object(Path, "exists", return_value=False):
            self.assertIs(kernel_build.btf(FakeContext()).status, Status.FAIL)

    def test_bpftrace_attach_success_and_failure(self):
        ok = FakeContext(commands={"bpftrace": cp("Attached 2 probes")})
        self.assertIs(kernel_build.bpftrace_attach(ok).status, Status.PASS)
        bad = FakeContext(commands={"bpftrace": cp("", 1, "ERROR: need root")})
        self.assertIs(kernel_build.bpftrace_attach(bad).status, Status.FAIL)

    def test_vdso64_pass_and_fail(self):
        with mock.patch.object(kernel_build, "_compile_run",
                               return_value=(True, 0, "ok")):
            self.assertIs(kernel_build.vdso64(FakeContext()).status, Status.PASS)
        with mock.patch.object(kernel_build, "_compile_run",
                               return_value=(True, 1, "")):
            self.assertIs(kernel_build.vdso64(FakeContext()).status, Status.FAIL)
        with mock.patch.object(kernel_build, "_compile_run",
                               return_value=(False, None, "err")):
            self.assertIs(kernel_build.vdso64(FakeContext()).status, Status.SKIP)

    def test_vdso32_absent_libc_skips(self):
        with mock.patch.object(kernel_build, "_compile_run",
                               return_value=(False, None, "no -m32")):
            self.assertIs(kernel_build.vdso32(FakeContext()).status, Status.SKIP)

    def test_stack_protector_must_abort(self):
        """A wrong guard is silent corruption, so the canary must actually fire."""
        with mock.patch.object(kernel_build, "_compile_run",
                               return_value=(True, 134, "")):
            self.assertIs(kernel_build.stack_protector(FakeContext()).status,
                          Status.PASS)
        with mock.patch.object(kernel_build, "_compile_run",
                               return_value=(True, 0, "")):
            self.assertIs(kernel_build.stack_protector(FakeContext()).status,
                          Status.WARN)

    def test_compile_run_helper(self):
        ctx = FakeContext(commands={"gcc": cp("", 1, "compile error")})
        built, rc, err = kernel_build._compile_run(ctx, "int main(){}", [])
        self.assertFalse(built)

    def test_modules_signed(self):
        ctx = FakeContext(kconfig="CONFIG_MODULE_SIG=y\nCONFIG_MODULE_SIG_FORCE=y\n")
        self.assertIn("enforced", kernel_build.modules_signed(ctx).message)
        ctx2 = FakeContext(kconfig="CONFIG_OTHER=y\n")
        self.assertIn("not enabled", kernel_build.modules_signed(ctx2).message)
        self.assertIs(kernel_build.modules_signed(FakeContext()).status, Status.SKIP)


if __name__ == "__main__":
    unittest.main()
