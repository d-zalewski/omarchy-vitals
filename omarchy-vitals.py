#!/usr/bin/env python3
"""omarchy-vitals - desktop kernel validation suite.

Run on the target machine after deploying a kernel. Emits a JSON report named
after the running kernel so two kernels can be compared directly:

    ./omarchy-vitals.py --tier 0,1              # health + desktop hardware  (~5 min)
    ./omarchy-vitals.py --tier 2                # latency / jitter          (~12 min)
    # reboot into the other kernel, repeat, then:
    ./omarchy-vitals.py compare reports/A.json reports/B.json
"""
from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vitals import report as rp                                    # noqa: E402
from vitals.core import Context, Status, run_check, select         # noqa: E402
from vitals.checks import (audio, boot, drivers, filesystem,  # noqa: E402,F401
                          graphics, health, kernel_build, kernel_features,
                          latency, network, peripherals, secureboot,
                          stress, throughput)

TIER_NAMES = {
    0: "health           (kernel faults, taint, failed units, deploy integrity, boot chain)",
    1: "desktop hardware (gpu, audio, network, peripherals, drivers, kernel features)",
    2: "latency/jitter   (cyclictest idle + under load)",
    3: "stress           (sustained load, thermal, disk I/O)",
    4: "suspend/resume   (S3 cycles, hardware returns)",
    5: "throughput       (context switch, syscall, scheduler contention)",
}


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="omarchy-vitals", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    cmp_p = sub.add_parser("compare", help="compare two JSON reports")
    cmp_p.add_argument("a", type=Path)
    cmp_p.add_argument("b", type=Path)
    cmp_p.add_argument("--tolerance", type=float, default=10.0,
                       help="percent change before calling it a regression")

    ap.add_argument("--tier", default="0,1",
                    help="comma-separated tiers (default 0,1)")
    ap.add_argument("--only", default=None,
                    help="comma-separated check names to run")
    ap.add_argument("--list", action="store_true", help="list checks and exit")
    ap.add_argument("--minutes", type=int, default=5,
                    help="minutes per cyclictest run (default 5)")
    ap.add_argument("--stress-minutes", type=int, default=20,
                    help="minutes for the stress tier (default 20)")
    ap.add_argument("--skip-disruptive", action="store_true",
                    help="skip checks that load, suspend, or make noise")
    ap.add_argument("-o", "--out", type=Path, default=Path("reports"),
                    help="report directory (default ./reports)")
    ap.add_argument("--no-report", action="store_true",
                    help="console only, do not write JSON")
    args = ap.parse_args()

    if args.cmd == "compare":
        return rp.compare(args.a, args.b, args.tolerance)

    tiers = None
    if args.tier and args.tier != "all":
        try:
            tiers = [int(t) for t in args.tier.split(",") if t.strip() != ""]
        except ValueError:
            print(f"bad --tier value: {args.tier}", file=sys.stderr)
            return 2
    only = [s.strip() for s in args.only.split(",")] if args.only else None

    checks = select(tiers=tiers, only=only, skip_disruptive=args.skip_disruptive)

    if args.list:
        print(f"{rp.BOLD}Available checks{rp.RESET}")
        cur = None
        for c in sorted(select(), key=lambda c: (c.tier, c.name)):
            if c.tier != cur:
                cur = c.tier
                print(f"\n  {rp.BOLD}tier {cur}{rp.RESET} - {TIER_NAMES.get(cur,'')}")
            flag = f" {rp.YELLOW}[disruptive]{rp.RESET}" if c.disruptive else ""
            print(f"    {c.name:<22} {c.desc}{flag}")
        return 0

    if not checks:
        print("no checks selected", file=sys.stderr)
        return 2

    est = sum(c.est_seconds for c in checks)
    print(f"{rp.BOLD}omarchy-vitals{rp.RESET}  kernel {rp.CYAN}{platform.release()}{rp.RESET}"
          f"  tiers {args.tier}  ({len(checks)} checks, ~{max(1, est // 60)} min)")

    ctx = Context(minutes=args.minutes, stress_minutes=args.stress_minutes)
    results = []
    cur_tier = None
    for c in checks:
        if c.tier != cur_tier:
            cur_tier = c.tier
            print(rp.header(f"TIER {cur_tier} - {TIER_NAMES.get(cur_tier, '')}"))
        res, dur = run_check(c, ctx)
        print(rp.line(res.status, c.name, res.message))
        results.append((c, res, dur))

    counts = rp.summarise(results)
    print(rp.header("SUMMARY"))
    print(f"  {rp.GREEN}pass {counts[Status.PASS]}{rp.RESET}   "
          f"{rp.YELLOW}warn {counts[Status.WARN]}{rp.RESET}   "
          f"{rp.RED}fail {counts[Status.FAIL]}{rp.RESET}   "
          f"{rp.GREY}skip {counts[Status.SKIP]}{rp.RESET}   "
          f"{rp.CYAN}info {counts[Status.INFO]}{rp.RESET}")

    failures = [(c, r) for c, r, _ in results if r.status is Status.FAIL]
    if failures:
        print(f"\n  {rp.RED}{rp.BOLD}Failures:{rp.RESET}")
        for c, r in failures:
            print(f"    - {c.name}: {r.message}")

    if not args.no_report:
        path = rp.write_report(rp.build_report(results), args.out)
        print(f"\n  report: {path}")
        print(f"  compare: {sys.argv[0]} compare <other>.json {path}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
