#!/usr/bin/env python3
"""Run the test suite sharded across processes, one module per shard.

The suite has no parallelism of its own and takes a quarter of an hour on
one core; most of that is subprocess-heavy modules waiting on each other.
Each test module is hermetic (its own temporary root), so modules can run
side by side. This runs them across N worker processes and prints one
summary, exiting non-zero if any module failed. Use it locally; CI runs
the plain discover so a failure there is easy to read.

    python3 tools/run_tests.py            # all modules, one process per core
    python3 tools/run_tests.py -j 4       # four at a time
    python3 tools/run_tests.py tests/test_gates.py tests/test_review.py
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUMMARY = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.M)
VERDICT = re.compile(r"^(OK|FAILED)(?: \((.*)\))?$", re.M)


def run_module(path: Path) -> tuple[Path, int, str]:
    name = "tests." + path.stem
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "unittest", name],
        cwd=ROOT, capture_output=True, text=True,
    )
    output = result.stdout + result.stderr
    return path, result.returncode, output + f"\n[{time.monotonic() - started:.1f}s]"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("modules", nargs="*", help="test files; default: every tests/test_*.py")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args(argv)
    modules = [Path(m) for m in args.modules] or sorted((ROOT / "tests").glob("test_*.py"))
    started = time.monotonic()
    failed: list[tuple[Path, str]] = []
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(run_module, path): path for path in modules}
        for future in as_completed(futures):
            path, code, output = future.result()
            ran = SUMMARY.search(output)
            verdict = VERDICT.search(output)
            counts["tests"] += int(ran.group(1)) if ran else 0
            detail = (verdict.group(2) or "") if verdict else ""
            for key in ("failures", "errors", "skipped"):
                found = re.search(rf"{key}=(\d+)", detail)
                counts[key] += int(found.group(1)) if found else 0
            status = "ok" if code == 0 else "FAILED"
            elapsed = output.rsplit("[", 1)[-1].rstrip("]\n")
            print(f"{status:6s} {path.name:40s} {ran.group(1) if ran else '?':>4s} tests  {elapsed}")
            if code != 0:
                failed.append((path, output))
    print(
        f"\n{counts['tests']} tests across {len(modules)} modules in {time.monotonic() - started:.0f}s: "
        f"{counts['failures']} failure(s), {counts['errors']} error(s), {counts['skipped']} skipped"
    )
    for path, output in failed:
        print(f"\n===== {path.name} =====")
        print("\n".join(line for line in output.splitlines() if not line.startswith("Recorded ")).rstrip()[-6000:])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
