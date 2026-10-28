"""Reject new mypy errors while keeping the inherited debt explicit."""

import collections
import json
import re
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "qobuz_proxy/", "--no-pretty"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        print(result.stdout + result.stderr)
        return 1
    errors = collections.Counter(
        re.sub(r":\d+(?::\d+)?: error:", ": error:", line)
        for line in result.stdout.splitlines()
        if ": error:" in line
    )
    if result.returncode and not errors:
        print(result.stdout + result.stderr)
        return 1
    baseline = collections.Counter(json.loads((root / "tools/mypy-baseline.json").read_text()))
    added = errors - baseline
    if added:
        for message, count in sorted(added.items()):
            print(f"{message} (new occurrences: {count})")
        return 1
    print(f"No new type errors ({sum(errors.values())} inherited errors remain).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
