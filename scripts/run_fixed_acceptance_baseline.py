"""执行、录制或离线回放固定端到端验收场景。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# 允许从项目根目录直接运行：python scripts/run_fixed_acceptance_baseline.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent_runtime import AgentState
from app.evaluation import FIXED_ACCEPTANCE_SCENARIOS, build_fixed_acceptance_baseline


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


def _load_recorded_states(directory: Path) -> list[AgentState]:
    states: list[AgentState] = []
    for scenario in FIXED_ACCEPTANCE_SCENARIOS:
        path = directory / f"{scenario.case_id}.json"
        if not path.exists():
            continue
        states.append(AgentState.model_validate_json(path.read_text(encoding="utf-8")))
    return states


def _execute_and_record(base_url: str, record_dir: Path | None) -> list[AgentState]:
    states: list[AgentState] = []
    if record_dir is not None:
        record_dir.mkdir(parents=True, exist_ok=True)

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
            state_payload = _http_json(
                f"{base_url}/api/trip/sessions/{session_id}"
            )
            state = AgentState.model_validate(state_payload)
            states.append(state)
            if record_dir is not None:
                (record_dir / f"{scenario.case_id}.json").write_text(
                    state.model_dump_json(indent=2), encoding="utf-8"
                )
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            print(f"  执行失败: {exc}", file=sys.stderr)
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
    parser.add_argument("--record-dir", type=Path, help="保存无密钥 AgentState 录制文件")
    parser.add_argument("--replay-dir", type=Path, help="离线读取已录制 AgentState")
    parser.add_argument("--output", type=Path, help="把验收报告写入 JSON 文件")
    args = parser.parse_args()

    if args.list or (not args.execute and args.replay_dir is None):
        _print_scenarios()
        return 0

    states: list[AgentState] = []
    if args.replay_dir is not None:
        states.extend(_load_recorded_states(args.replay_dir))
    if args.execute:
        states.extend(_execute_and_record(args.base_url.rstrip("/"), args.record_dir))

    report = build_fixed_acceptance_baseline(
        states,
        requested_limit=max(1, len(states)),
        sampled_session_count=len(states),
    )
    rendered = report.model_dump_json(indent=2)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")

    # 自动化流水线只有在 15 个场景全部覆盖且全部通过时返回 0。
    return 0 if report.missing_case_count == 0 and report.failed_case_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
