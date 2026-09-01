"""Tests for report building and the direction-aware comparison."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import cp  # noqa: E402,F401

from vitals import report as rp  # noqa: E402
from vitals.core import Check, Fail, Ok, Skip, Status, Warn  # noqa: E402


def result_row(name, result, tier=0, dur=0.5):
    return (Check(fn=lambda c: result, name=name, tier=tier, desc="d"),
            result, dur)


class TestFormatting(unittest.TestCase):
    def test_line_contains_status_and_text(self):
        out = rp.line(Status.PASS, "gpu", "all good")
        self.assertIn("PASS", out)
        self.assertIn("gpu", out)
        self.assertIn("all good", out)

    def test_every_status_has_a_colour(self):
        for s in Status:
            self.assertIn(s, rp._COLOR)
            self.assertIn(s.value, rp.line(s, "n", "m"))

    def test_header(self):
        self.assertIn("TITLE", rp.header("TITLE"))


class TestSummarise(unittest.TestCase):
    def test_counts_every_status(self):
        results = [result_row("a", Ok("x")), result_row("b", Ok("y")),
                   result_row("c", Fail("z")), result_row("d", Warn("w")),
                   result_row("e", Skip("s"))]
        counts = rp.summarise(results)
        self.assertEqual(counts[Status.PASS], 2)
        self.assertEqual(counts[Status.FAIL], 1)
        self.assertEqual(counts[Status.WARN], 1)
        self.assertEqual(counts[Status.SKIP], 1)
        self.assertEqual(counts[Status.INFO], 0)

    def test_empty(self):
        self.assertEqual(rp.summarise([])[Status.PASS], 0)


class TestBuildReport(unittest.TestCase):
    def test_collects_metrics_and_checks(self):
        results = [result_row("a", Ok("fine", x=1)),
                   result_row("b", Fail("bad", y=2.5), tier=1)]
        r = rp.build_report(results)
        self.assertEqual(r["metrics"], {"x": 1, "y": 2.5})
        self.assertEqual(len(r["checks"]), 2)
        self.assertEqual(r["checks"][0]["status"], "PASS")
        self.assertEqual(r["checks"][1]["tier"], 1)
        self.assertIn("kernel", r)
        self.assertIn("date", r)

    def test_extra_fields_merged(self):
        r = rp.build_report([], extra={"note": "hi"})
        self.assertEqual(r["note"], "hi")

    def test_duration_rounded(self):
        r = rp.build_report([result_row("a", Ok("x"), dur=1.23456)])
        self.assertEqual(r["checks"][0]["duration_s"], 1.23)


class TestWriteReport(unittest.TestCase):
    def test_writes_json_named_after_kernel(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "nested" / "dir"
            report = rp.build_report([result_row("a", Ok("x", m=1))])
            path = rp.write_report(report, out)
            self.assertTrue(path.exists())
            self.assertEqual(path.name, f"{report['kernel']}.json")
            self.assertEqual(json.loads(path.read_text())["metrics"], {"m": 1})


class TestDirection(unittest.TestCase):
    def test_known_directions(self):
        self.assertEqual(rp._direction("cyclictest_idle_max_us"), -1)
        self.assertEqual(rp._direction("fio_randread_iops"), 1)
        self.assertEqual(rp._direction("unknown_metric"), 0)

    def test_verdict_neutral_metric_has_no_label(self):
        self.assertEqual(rp._verdict("unknown_metric", 1, 2), ("", ""))

    def test_verdict_lower_is_better(self):
        label, _ = rp._verdict("hackbench_sec", 100, 50)     # halved
        self.assertEqual(label, "better")
        label, _ = rp._verdict("hackbench_sec", 50, 100)     # doubled
        self.assertEqual(label, "REGRESSION")

    def test_verdict_higher_is_better(self):
        label, _ = rp._verdict("fio_randread_iops", 100, 200)
        self.assertEqual(label, "better")
        label, _ = rp._verdict("fio_randread_iops", 200, 100)
        self.assertEqual(label, "REGRESSION")

    def test_within_tolerance_is_same(self):
        label, _ = rp._verdict("hackbench_sec", 100, 105)    # +5%
        self.assertEqual(label, "~same")

    def test_tolerance_is_configurable(self):
        self.assertEqual(rp._verdict("hackbench_sec", 100, 105, tolerance=1)[0],
                         "REGRESSION")

    def test_zero_baseline_handled(self):
        self.assertEqual(rp._verdict("oops_count", 0, 0)[0], "same")
        self.assertEqual(rp._verdict("oops_count", 0, 5)[0], "worse")
        self.assertEqual(rp._verdict("fio_randread_iops", 0, 5)[0], "better")


class TestCompare(unittest.TestCase):
    def _write(self, d, name, metrics, checks=None):
        p = Path(d) / f"{name}.json"
        p.write_text(json.dumps({
            "kernel": name, "date": "2026-01-01T00:00:00+0000",
            "metrics": metrics, "checks": checks or [],
        }))
        return p

    def test_detects_regression_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._write(d, "A", {"hackbench_sec": 3.0})
            b = self._write(d, "B", {"hackbench_sec": 6.0})
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rp.compare(a, b)
            out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("REGRESSION", out)
        self.assertIn("hackbench_sec", out)

    def test_no_regression_returns_zero(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._write(d, "A", {"hackbench_sec": 3.0})
            b = self._write(d, "B", {"hackbench_sec": 2.0})
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rp.compare(a, b)
        self.assertEqual(rc, 0)
        self.assertIn("No regressions", buf.getvalue())

    def test_check_flip_to_fail_counts_as_regression(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._write(d, "A", {}, [{"name": "gpu", "status": "PASS"}])
            b = self._write(d, "B", {}, [{"name": "gpu", "status": "FAIL"}])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rp.compare(a, b)
            out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("PASS -> FAIL", out)

    def test_check_flip_to_pass_is_not_a_regression(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._write(d, "A", {}, [{"name": "gpu", "status": "FAIL"}])
            b = self._write(d, "B", {}, [{"name": "gpu", "status": "PASS"}])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rp.compare(a, b)
        self.assertEqual(rc, 0)

    def test_missing_metric_on_one_side_is_shown_not_judged(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._write(d, "A", {"only_in_a": 5})
            b = self._write(d, "B", {"only_in_b": 7})
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rp.compare(a, b)
            out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("only_in_a", out)
        self.assertIn("only_in_b", out)

    def test_non_numeric_metrics_do_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._write(d, "A", {"note": "text"})
            b = self._write(d, "B", {"note": "other"})
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rp.compare(a, b)
        self.assertEqual(rc, 0)

    def test_units_are_rendered(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._write(d, "A", {"cyclictest_idle_max_us": 100})
            b = self._write(d, "B", {"cyclictest_idle_max_us": 101})
            buf = io.StringIO()
            with redirect_stdout(buf):
                rp.compare(a, b)
        self.assertIn("us", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
