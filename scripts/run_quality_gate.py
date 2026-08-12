"""本地和 CI 共用的确定性质量门。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "fixed_acceptance" / "v1"


def _run(command: list[str]) -> int:
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, cwd=PROJECT_ROOT, check=False).returncode


def main() -> int:
    commands = [
        [sys.executable, "-m", "compileall", "-q", "app", "tests", "scripts", "main.py"],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        [
            sys.executable,
            "scripts/run_fixed_acceptance_baseline.py",
            "--replay-dir",
            str(FIXTURE_DIR),
            "--require-manifest",
            "--allowed-source",
            "synthetic",
            "--summary-only",
        ],
    ]
    for command in commands:
        exit_code = _run(command)
        if exit_code != 0:
            return exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
