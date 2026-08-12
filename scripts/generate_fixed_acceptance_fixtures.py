"""生成不依赖外部 API 的固定验收录制格式契约样本。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.evaluation import (
    FIXED_ACCEPTANCE_SCENARIOS,
    create_acceptance_recording,
    write_acceptance_recording_suite,
)
from app.evaluation.sample_factory import build_synthetic_acceptance_state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "tests" / "fixtures" / "fixed_acceptance" / "v1",
    )
    args = parser.parse_args()

    fixture_time = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    recordings = [
        create_acceptance_recording(
            scenario,
            build_synthetic_acceptance_state(scenario),
            source="synthetic",
            recorded_at=fixture_time + timedelta(seconds=index),
        )
        for index, scenario in enumerate(FIXED_ACCEPTANCE_SCENARIOS)
    ]
    manifest = write_acceptance_recording_suite(
        args.output_dir, recordings, generated_at=fixture_time
    )
    print(
        f"generated {manifest.total_case_count} synthetic contract recordings at "
        f"{args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
