#!/usr/bin/env python3
"""Scaffold a new check and its tests.

Writing a check is easy; remembering where the test goes, which helper to use
and which branches need covering is the friction. This writes both stubs with
the right imports and a test for every branch, so `./run-tests.sh` passes
immediately and stays at 100 % while you fill them in.

    ./new-check.py --module graphics --name backlight --tier 1 \
                   --desc "screen backlight is controllable"

    ./new-check.py --module graphics --name backlight --write   # append to files
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHECKS = ROOT / "vitals" / "checks"

MODULES = {
    "health": "tier 0 — kernel faults",
    "graphics": "tier 1 — GPU, displays, compositor",
    "audio": "tier 1 — sound",
    "network": "tier 1 — ethernet, wifi, bluetooth",
    "peripherals": "tier 1 — USB, input, storage, thermal",
    "kernel_build": "tier 1 — toolchain / kernel build",
    "latency": "tier 2 — scheduling latency",
    "stress": "tier 3/4 — stress and suspend",
    "throughput": "tier 5 — kernel benchmarks",
}

TEST_FILE = {
    "health": "test_checks_health.py",
    "graphics": "test_checks_hardware.py",
    "audio": "test_checks_hardware.py",
    "network": "test_checks_hardware.py",
    "peripherals": "test_checks_hardware.py",
    "kernel_build": "test_checks_perf.py",
    "latency": "test_checks_perf.py",
    "stress": "test_checks_perf.py",
    "throughput": "test_checks_perf.py",
}

CHECK_TMPL = '''

@check(tier={tier}, name="{name}", desc="{desc}"{extras})
def {name}(ctx):
    """{desc}.

    TODO: describe what a failure here would mean for the user.
    """
    # Absent hardware is Skip, never Fail - a machine without this device
    # failing the check makes a suite people learn to ignore.
    if not ctx.path_exists("/sys/class/CHANGE_ME"):
        return Skip("hardware not present")

    value = ctx.read("/sys/class/CHANGE_ME/value", "0")
    if not value.isdigit():
        return Warn("value unreadable")

    n = int(value)
    if n == 0:
        return Fail("CHANGE_ME reports zero", {name}_value=n)
    return Ok(f"CHANGE_ME at {{n}}", {name}_value=n)
'''

TEST_TMPL = '''

class Test{cls}(unittest.TestCase):
    """Every branch of {module}.{name} - the gate requires 100 % coverage."""

    def test_absent_hardware_skips(self):
        with fake_fs({{}}):
            self.assertIs({module}.{name}(FakeContext()).status, Status.SKIP)

    def test_unreadable_value_warns(self):
        with fake_fs({{"/sys/class/CHANGE_ME": []}}):
            ctx = FakeContext(files={{"/sys/class/CHANGE_ME/value": "not-a-number"}})
            self.assertIs({module}.{name}(ctx).status, Status.WARN)

    def test_zero_fails(self):
        with fake_fs({{"/sys/class/CHANGE_ME": []}}):
            ctx = FakeContext(files={{"/sys/class/CHANGE_ME/value": "0"}})
            r = {module}.{name}(ctx)
        self.assertIs(r.status, Status.FAIL)
        self.assertEqual(r.metrics["{name}_value"], 0)

    def test_healthy_passes(self):
        with fake_fs({{"/sys/class/CHANGE_ME": []}}):
            ctx = FakeContext(files={{"/sys/class/CHANGE_ME/value": "42"}})
            r = {module}.{name}(ctx)
        self.assertIs(r.status, Status.PASS)
        self.assertEqual(r.metrics["{name}_value"], 42)
'''


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--module", required=True, choices=sorted(MODULES),
                    help="which check module to add to")
    ap.add_argument("--name", required=True,
                    help="check name (snake_case, becomes the function name)")
    ap.add_argument("--tier", type=int, default=1, choices=range(6))
    ap.add_argument("--desc", default="what this check proves")
    ap.add_argument("--requires", default="",
                    help="comma-separated tools that must exist, else Skip")
    ap.add_argument("--disruptive", action="store_true",
                    help="loads the machine, makes noise or suspends it")
    ap.add_argument("--write", action="store_true",
                    help="append to the module and test file instead of printing")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.name):
        print(f"error: --name must be snake_case, got {args.name!r}", file=sys.stderr)
        return 2

    mod_path = CHECKS / f"{args.module}.py"
    test_path = ROOT / "tests" / TEST_FILE[args.module]

    if re.search(rf'name="{args.name}"', mod_path.read_text()):
        print(f"error: a check named {args.name!r} already exists in "
              f"{mod_path.name}", file=sys.stderr)
        return 2

    extras = ""
    if args.requires:
        tools = ", ".join(f'"{t.strip()}"' for t in args.requires.split(",") if t.strip())
        extras += f",\n       requires=[{tools}]"
    if args.disruptive:
        extras += ",\n       disruptive=True, est_seconds=30"

    check_src = CHECK_TMPL.format(tier=args.tier, name=args.name,
                                  desc=args.desc, extras=extras)
    cls = "".join(p.capitalize() for p in args.name.split("_"))
    test_src = TEST_TMPL.format(cls=cls, module=args.module, name=args.name)

    if not args.write:
        print(f"# ---- append to vitals/checks/{args.module}.py ----")
        print(check_src.rstrip())
        print(f"\n# ---- append to tests/{TEST_FILE[args.module]} ----")
        print(test_src.rstrip())
        print("\n# Re-run with --write to append these automatically.")
        return 0

    with mod_path.open("a") as f:
        f.write(check_src)
    with test_path.open("a") as f:
        f.write(test_src)

    print(f"added check   -> vitals/checks/{args.module}.py")
    print(f"added tests   -> tests/{TEST_FILE[args.module]}")
    print()
    print("Next:")
    print(f"  1. Replace CHANGE_ME in both files with what you are checking.")
    print(f"  2. If the metric has a direction, add {args.name}_value to")
    print( "     LOWER_IS_BETTER or HIGHER_IS_BETTER in vitals/core.py so")
    print( "     comparisons can call it a regression.")
    print(f"  3. ./run-tests.sh {args.module}")
    print(f"  4. ./omarchy-vitals.py --only {args.name}      # on a real machine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
