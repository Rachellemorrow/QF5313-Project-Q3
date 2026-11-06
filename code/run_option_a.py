"""Run the complete Option A pipeline in order.

    uv run python code/run_option_a.py --seed 42

This is a convenience driver only: every step is the same script documented in
AGENTS.md and can be run individually.  It stops at the first failing step.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [
    "code/approach1_data.py",
    "code/approach1_gdp_nowcast.py",
    "code/approach1_regime.py",
    "code/approach1_cluster.py",
    "code/approach1_validate.py",
    "code/compare.py",
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the complete Option A pipeline")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    for script in SCRIPTS:
        cmd = [sys.executable, str(ROOT / script), "--seed", str(args.seed)]
        print(f"\n=== {script} (seed={args.seed}) ===", flush=True)
        result = subprocess.run(cmd, cwd=ROOT)
        if result.returncode != 0:
            print(f"FAILED: {script} (exit {result.returncode})", file=sys.stderr)
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
