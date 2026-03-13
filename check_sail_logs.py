#!/usr/bin/env python3
"""Check all Sail signature logs in work/ for SUCCESS/FAIL, and report skipped tests."""

import sys
from pathlib import Path

WORKDIR = Path(__file__).parent / "work"
TESTDIR = Path(__file__).parent / "tests"

# Tests skipped for known architectural reasons (not a bug)
EXPECTED_SKIP_PREFIXES = (
    "D-", "Zcd-",           # Double FP — no D extension
    "E-",                   # RV32E base — not applicable
    "Zaamo-", "Zalrsc-",    # Atomics — no A extension
    "pmpsm_", "pmpzca_",    # PMP — 0 PMP entries
    "sv32_", "sv39_", "sv48_", "sv57_",  # Virtual memory — no MMU
    "mprv_vm_", "vm_mstatus_",           # VM tests — no MMU
)

# RV64-only instruction names (W-suffix ops, 64-bit loads/stores, 64-bit FP conversions)
EXPECTED_SKIP_SUFFIXES = (
    "w-00", "w-01",   # *w instructions (addw, subw, etc.)
)

EXPECTED_SKIP_EXACT = {
    "I-ld-00", "I-lwu-00", "I-sd-00",         # RV64-only load/store
    "F-fcvt.l.s-00", "F-fcvt.lu.s-00",        # RV64-only FP conversions
    "F-fcvt.s.l-00", "F-fcvt.s.lu-00",
    "Zca-c.ld-00", "Zca-c.ldsp-00",           # RV64-only compressed
    "Zca-c.sd-00", "Zca-c.sdsp-00",
}


def main():
    logs = sorted(WORKDIR.rglob("*.sig.log"))
    if not logs:
        print("No .sig.log files found in work/")
        sys.exit(1)

    failures = []
    passes = []

    for log in logs:
        text = log.read_text(errors="replace")
        if "SUCCESS" in text:
            passes.append(log)
        else:
            failures.append(log)

    total = len(logs)

    # Print failures first and prominently
    if failures:
        print(f"\n{'='*60}")
        print(f"  FAILURES ({len(failures)}/{total})")
        print(f"{'='*60}")
        for log in failures:
            rel = log.relative_to(WORKDIR)
            parts = rel.parts
            test = log.stem.replace(".sig", "")
            ext = parts[-2] if len(parts) >= 2 else "?"
            config = parts[0]
            text = log.read_text(errors="replace")
            print(f"  FAIL  [{config}]  {ext}/{test}")
            lines = [l for l in text.splitlines() if l.strip()]
            for line in lines[-3:]:
                print(f"        {line}")
            print()
    else:
        print(f"\n  All {total} tests: SUCCESS")

    # Summary table by extension
    print(f"\n{'='*60}")
    print(f"  SUMMARY  (pass {len(passes)}/{total})")
    print(f"{'='*60}")

    ext_stats: dict[str, dict[str, int]] = {}
    for log in logs:
        rel = log.relative_to(WORKDIR)
        parts = rel.parts
        ext = parts[-2] if len(parts) >= 2 else "unknown"
        if ext not in ext_stats:
            ext_stats[ext] = {"pass": 0, "fail": 0}
        if log in failures:
            ext_stats[ext]["fail"] += 1
        else:
            ext_stats[ext]["pass"] += 1

    print(f"  {'Extension':<20} {'Pass':>6} {'Fail':>6} {'Total':>6}")
    print(f"  {'-'*20} {'-'*6} {'-'*6} {'-'*6}")
    for ext, stats in sorted(ext_stats.items()):
        flag = " <-- FAIL" if stats["fail"] else ""
        print(f"  {ext:<20} {stats['pass']:>6} {stats['fail']:>6} {stats['pass']+stats['fail']:>6}{flag}")

    # Coverage check: which available tests were not run?
    ran = {p.stem.replace(".sig", "") for p in logs}
    available = {p.stem for p in TESTDIR.rglob("*.S")}
    not_run = available - ran

    def is_expected_skip(t):
        return (
            any(t.startswith(p) for p in EXPECTED_SKIP_PREFIXES)
            or any(t.endswith(s) for s in EXPECTED_SKIP_SUFFIXES)
            or t in EXPECTED_SKIP_EXACT
        )

    unexpected_skips = [t for t in sorted(not_run) if not is_expected_skip(t)]
    expected_skips = [t for t in sorted(not_run) if is_expected_skip(t)]

    print(f"\n{'='*60}")
    print(f"  COVERAGE  (ran {len(ran)}/{len(available)} available tests)")
    print(f"  Expected skips (unsupported extensions): {len(expected_skips)}")
    print(f"  Unexpected skips (should investigate):   {len(unexpected_skips)}")
    print(f"{'='*60}")

    if unexpected_skips:
        print("  UNEXPECTED SKIPS:")
        for t in unexpected_skips:
            print(f"    {t}")
    else:
        print("  No unexpected skips.")
    print()

    sys.exit(1 if failures or unexpected_skips else 0)


if __name__ == "__main__":
    main()
