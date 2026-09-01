"""Console output, JSON reports, and direction-aware A/B comparison."""
from __future__ import annotations

import json
import platform
import time
from pathlib import Path

from .core import HIGHER_IS_BETTER, LOWER_IS_BETTER, UNITS, Status

BOLD = "\033[1m"; DIM = "\033[2m"; RESET = "\033[0m"
GREEN = "\033[32m"; RED = "\033[31m"; YELLOW = "\033[33m"
CYAN = "\033[36m"; GREY = "\033[90m"

_COLOR = {
    Status.PASS: GREEN, Status.FAIL: RED, Status.WARN: YELLOW,
    Status.SKIP: GREY, Status.INFO: CYAN,
}


def line(status: Status, name: str, message: str) -> str:
    return f"  {_COLOR[status]}{status.value:<4}{RESET}  {name:<26} {message}"


def header(text: str) -> str:
    return f"\n{BOLD}== {text} =={RESET}"


def summarise(results: list[tuple]) -> dict:
    counts = {s: 0 for s in Status}
    for _c, r, _d in results:
        counts[r.status] += 1
    return counts


def build_report(results: list[tuple], extra: dict | None = None) -> dict:
    metrics: dict = {}
    checks = []
    for c, r, dur in results:
        checks.append({
            "name": c.name, "tier": c.tier, "desc": c.desc,
            "status": r.status.value, "message": r.message,
            "duration_s": round(dur, 2),
        })
        for k, v in r.metrics.items():
            metrics[k] = v
    return {
        "kernel": platform.release(),
        "machine": platform.node(),
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "metrics": metrics,
        "checks": checks,
        **(extra or {}),
    }


def write_report(report: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report['kernel']}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True))
    return path


# ------------------------------------------------------------------ compare
def _direction(key: str) -> int:
    """-1 = lower is better, +1 = higher is better, 0 = neutral."""
    if key in LOWER_IS_BETTER:
        return -1
    if key in HIGHER_IS_BETTER:
        return 1
    return 0


def _verdict(key: str, a: float, b: float, tolerance: float = 10.0) -> tuple[str, str]:
    """Return (label, colour) describing B relative to A."""
    d = _direction(key)
    if d == 0:
        return "", ""
    if a == 0:
        if b == 0:
            return "same", GREY
        return ("worse", RED) if d < 0 else ("better", GREEN)
    pct = (b - a) / abs(a) * 100.0
    if abs(pct) < tolerance:
        return "~same", GREY
    improved = (pct < 0) if d < 0 else (pct > 0)
    return ("better", GREEN) if improved else ("REGRESSION", RED)


def compare(path_a: Path, path_b: Path, tolerance: float = 10.0) -> int:
    a = json.loads(Path(path_a).read_text())
    b = json.loads(Path(path_b).read_text())
    ma, mb = a.get("metrics", {}), b.get("metrics", {})

    print(f"{BOLD}Kernel A/B comparison{RESET}")
    print(f"  A: {CYAN}{a['kernel']}{RESET}   ({a.get('date','?')})")
    print(f"  B: {CYAN}{b['kernel']}{RESET}   ({b.get('date','?')})")
    print(f"  {DIM}tolerance: +/-{tolerance:.0f}% before calling a change{RESET}\n")

    keys = sorted(set(ma) | set(mb))
    print(f"  {'METRIC':<30} {'A':>12} {'B':>12} {'DELTA':>10}  VERDICT")
    print(f"  {'-'*30} {'-'*12} {'-'*12} {'-'*10}  {'-'*11}")

    regressions = []
    for k in keys:
        va, vb = ma.get(k), mb.get(k)
        unit = UNITS.get(k, "")
        sa = "-" if va is None else f"{va}{unit}"
        sb = "-" if vb is None else f"{vb}{unit}"
        delta, label, colour = "-", "", ""
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            delta = f"{vb-va:+.0f}" if abs(vb - va) >= 1 or (vb - va) == 0 else f"{vb-va:+.2f}"
            if va != 0:
                delta = f"{(vb-va)/abs(va)*100:+.0f}%"
            label, colour = _verdict(k, va, vb, tolerance)
            if label == "REGRESSION":
                regressions.append((k, va, vb))
        print(f"  {k:<30} {sa:>12} {sb:>12} {delta:>10}  {colour}{label}{RESET}")

    # Check-level differences matter as much as metrics: a check that passed on
    # A and fails on B is a regression even without a number attached.
    ca = {c["name"]: c["status"] for c in a.get("checks", [])}
    cb = {c["name"]: c["status"] for c in b.get("checks", [])}
    changed = [(n, ca.get(n, "-"), cb.get(n, "-"))
               for n in sorted(set(ca) | set(cb)) if ca.get(n) != cb.get(n)]
    if changed:
        print(f"\n  {BOLD}Check status changes{RESET}")
        for n, sa_, sb_ in changed:
            worse = sb_ == "FAIL" and sa_ in ("PASS", "WARN")
            col = RED if worse else YELLOW
            print(f"    {col}{n:<28} {sa_} -> {sb_}{RESET}")
            if worse:
                regressions.append((n, sa_, sb_))

    print()
    if regressions:
        print(f"  {RED}{BOLD}{len(regressions)} regression(s) in B vs A{RESET}")
        for k, va, vb in regressions:
            print(f"    - {k}: {va} -> {vb}")
    else:
        print(f"  {GREEN}{BOLD}No regressions detected{RESET}")

    print(f"\n  {DIM}Note: worst-case latency (*_max_us) matters more than average for")
    print(f"  perceived smoothness - a single 12ms outlier is a dropped frame.{RESET}")
    return 1 if regressions else 0
