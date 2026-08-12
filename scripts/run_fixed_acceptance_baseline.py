"""运行固定旅行规划场景，可调用本地服务录制，也可离线回放录制状态。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent_runtime import AgentState
from app.evaluation import (
    FIXED_ACCEPTANCE_SCENARIOS,
    build_fixed_acceptance_baseline,
    create_acceptance_recording,
    load_acceptance_recording_suite,
    write_acceptance_recording_suite,
)


def _http_json(url: str, *, method: str = "GET", payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    request = Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=240) as response:
        return json.loads(response.read().decode("utf-8"))


def _load_recorded_states(
    directory: Path,
    *,
    require_manifest: bool,
    allowed_sources: set[str] | None,
) -> list[AgentState]:
    return load_acceptance_recording_suite(
        directory,
        FIXED_ACCEPTANCE_SCENARIOS,
        require_manifest=require_manifest,
        allowed_sources=allowed_sources,
        allow_legacy=not require_manifest,
    )


def _execute_and_record(
    base_url: str,
    record_dir: Path | None,
) -> list[AgentState]:
    states: list[AgentState] = []
    recordings = []

    for index, scenario in enumerate(FIXED_ACCEPTANCE_SCENARIOS, start=1):
        print(f"[{index:02}/{len(FIXED_ACCEPTANCE_SCENARIOS)}] {scenario.case_id}")
        try:
            response = _http_json(
                f"{base_url}/api/trip/plan",
                method="POST",
                payload=scenario.request.model_dump(),
            )
            session_id = response.get("session_id")
            if not session_id:
                print("  未返回 session_id，跳过录制", file=sys.stderr)
                continue
            state_payload = _http_json(f"{base_url}/api/trip/sessions/{session_id}")
            state = AgentState.model_validate(state_payload)
            states.append(state)
            if record_dir is not None:
                recordings.append(
                    create_acceptance_recording(scenario, state, source="live")
                )
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            print(f"  执行失败: {exc}", file=sys.stderr)

    if record_dir is not None:
        write_acceptance_recording_suite(record_dir, recordings)
    return states


def _print_scenarios() -> None:
    for scenario in FIXED_ACCEPTANCE_SCENARIOS:
        request = scenario.request
        print(
            f"{scenario.case_id}: {request.city} {request.travel_days}日 "
            f"{request.transportation} {request.start_date}~{request.end_date}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="只列出固定场景")
    parser.add_argument("--execute", action="store_true", help="调用正在运行的本地服务")
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8000", help="旅行服务地址"
    )
    parser.add_argument("--record-dir", type=Path, help="保存版本化且脱敏的录制文件")
    parser.add_argument("--replay-dir", type=Path, help="离线读取已录制 AgentState")
    parser.add_argument("--output", type=Path, help="把验收报告写入 JSON 文件")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Only print coverage and pass/fail summary for CI logs",
    )
    parser.add_argument(
        "--require-manifest",
        action="store_true",
        help="要求录制目录包含版本化 manifest，并拒绝旧裸状态文件",
    )
    parser.add_argument(
        "--allowed-source",
        action="append",
        choices=["live", "synthetic", "legacy"],
        help="限制回放样本来源；可重复传入",
    )
    args = parser.parse_args()

    if args.list or (not args.execute and args.replay_dir is None):
        _print_scenarios()
        return 0

    states: list[AgentState] = []
    try:
        if args.replay_dir is not None:
            states.extend(
                _load_recorded_states(
                    args.replay_dir,
                    require_manifest=args.require_manifest,
                    allowed_sources=(
                        set(args.allowed_source) if args.allowed_source else None
                    ),
                )
            )
        if args.execute:
            states.extend(
                _execute_and_record(args.base_url.rstrip("/"), args.record_dir)
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"固定验收样本加载失败: {exc}", file=sys.stderr)
        return 2

    report = build_fixed_acceptance_baseline(
        states,
        requested_limit=max(1, len(states)),
        sampled_session_count=len(states),
    )
    rendered = report.model_dump_json(indent=2)
    if args.summary_only:
        print(
            json.dumps(
                {
                    "suite_name": report.suite_name,
                    "total_case_count": report.total_case_count,
                    "covered_case_count": report.covered_case_count,
                    "passed_case_count": report.passed_case_count,
                    "failed_case_count": report.failed_case_count,
                    "missing_case_count": report.missing_case_count,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")

    # 自动化流水线只有在 15 个场景全部覆盖且全部通过时返回 0。
    return 0 if report.missing_case_count == 0 and report.failed_case_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
