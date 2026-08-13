"""运行固定旅行规划场景，可调用本地服务增量录制，也可离线回放录制状态。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent_runtime import AgentState
from app.evaluation import (
    AcceptanceRecordingManifest,
    FIXED_ACCEPTANCE_SCENARIOS,
    RECORDING_SUITE_NAME,
    build_fixed_acceptance_baseline,
    create_acceptance_recording,
    load_acceptance_recording_suite,
    sanitize_recording_payload,
    write_acceptance_recording_suite,
)


class HttpJsonError(RuntimeError):
    """保留供应商代理返回的 HTTP 状态码和 JSON 错误体。"""

    def __init__(
        self,
        *,
        endpoint: str,
        status_code: int,
        detail: Any,
        response_body: Any,
    ):
        self.endpoint = endpoint
        self.status_code = status_code
        self.detail = detail
        self.response_body = response_body
        super().__init__(f"HTTP {status_code}: {detail}")


@dataclass(slots=True)
class RecordingFailure:
    """单个 Live 场景的结构化录制失败。"""

    case_id: str
    stage: str
    error_type: str
    message: str
    recorded_at: str
    endpoint: str | None = None
    status_code: int | None = None
    detail: Any = None
    response_body: Any = None
    redacted_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LiveExecutionResult:
    """一次增量 Live 录制的状态、失败和跳过信息。"""

    states: list[AgentState] = field(default_factory=list)
    failures: list[RecordingFailure] = field(default_factory=list)
    attempted_case_ids: list[str] = field(default_factory=list)
    skipped_case_ids: list[str] = field(default_factory=list)


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_http_body(raw_body: bytes) -> Any:
    text = raw_body.decode("utf-8", errors="replace")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text[:4000]


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout_seconds: float = 240,
):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    request = Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        response_body = _decode_http_body(exc.read())
        detail = (
            response_body.get("detail")
            if isinstance(response_body, dict) and "detail" in response_body
            else response_body or exc.reason
        )
        raise HttpJsonError(
            endpoint=url,
            status_code=exc.code,
            detail=detail,
            response_body=response_body,
        ) from exc


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


def _load_existing_case_ids(directory: Path | None) -> set[str]:
    if directory is None:
        return set()
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return set()
    manifest = AcceptanceRecordingManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    if manifest.suite_name != RECORDING_SUITE_NAME:
        raise ValueError(f"unexpected acceptance suite: {manifest.suite_name}")
    if manifest.total_case_count != len(manifest.records):
        raise ValueError("acceptance manifest total_case_count does not match records")
    return {entry.case_id for entry in manifest.records}


def _failure_from_exception(
    case_id: str,
    stage: str,
    exc: Exception,
    *,
    endpoint: str | None = None,
) -> RecordingFailure:
    if isinstance(exc, HttpJsonError):
        raw_payload = {
            "message": str(exc),
            "detail": exc.detail,
            "response_body": exc.response_body,
        }
        sanitized, redacted_paths = sanitize_recording_payload(
            raw_payload,
            root_path="failure",
        )
        return RecordingFailure(
            case_id=case_id,
            stage=stage,
            error_type="http_error",
            message=f"HTTP {exc.status_code}: {sanitized['detail']}",
            recorded_at=_utc_now_text(),
            endpoint=exc.endpoint,
            status_code=exc.status_code,
            detail=sanitized["detail"],
            response_body=sanitized["response_body"],
            redacted_paths=redacted_paths,
        )

    sanitized_message, redacted_paths = sanitize_recording_payload(
        str(exc),
        root_path="failure.message",
    )
    return RecordingFailure(
        case_id=case_id,
        stage=stage,
        error_type=type(exc).__name__,
        message=sanitized_message,
        recorded_at=_utc_now_text(),
        endpoint=endpoint,
        redacted_paths=redacted_paths,
    )


def _execute_and_record(
    base_url: str,
    record_dir: Path | None,
    scenarios: list,
    *,
    timeout_seconds: float,
    skipped_case_ids: list[str] | None = None,
) -> LiveExecutionResult:
    result = LiveExecutionResult(skipped_case_ids=skipped_case_ids or [])
    recordings = []

    for index, scenario in enumerate(scenarios, start=1):
        result.attempted_case_ids.append(scenario.case_id)
        print(f"[{index:02}/{len(scenarios)}] {scenario.case_id}")
        plan_endpoint = f"{base_url}/api/trip/plan"
        try:
            response = _http_json(
                plan_endpoint,
                method="POST",
                payload=scenario.request.model_dump(),
                timeout_seconds=timeout_seconds,
            )
        except (HttpJsonError, URLError, TimeoutError, ValueError) as exc:
            failure = _failure_from_exception(
                scenario.case_id,
                "trip_plan",
                exc,
                endpoint=plan_endpoint,
            )
            result.failures.append(failure)
            print(
                f"  执行失败: {failure.message}"
                + (f"；detail={failure.detail}" if failure.detail is not None else ""),
                file=sys.stderr,
            )
            continue

        session_id = response.get("session_id") if isinstance(response, dict) else None
        if not session_id:
            failure = RecordingFailure(
                case_id=scenario.case_id,
                stage="trip_plan_response",
                error_type="invalid_response",
                message="旅行规划响应未返回 session_id",
                recorded_at=_utc_now_text(),
                endpoint=plan_endpoint,
                detail=response,
            )
            result.failures.append(failure)
            print(f"  {failure.message}，跳过录制", file=sys.stderr)
            continue

        session_endpoint = f"{base_url}/api/trip/sessions/{session_id}"
        try:
            state_payload = _http_json(
                session_endpoint,
                timeout_seconds=timeout_seconds,
            )
            state = AgentState.model_validate(state_payload)
            result.states.append(state)
            if record_dir is not None:
                recordings.append(
                    create_acceptance_recording(scenario, state, source="live")
                )
        except (HttpJsonError, URLError, TimeoutError, ValueError) as exc:
            failure = _failure_from_exception(
                scenario.case_id,
                "session_recording",
                exc,
                endpoint=session_endpoint,
            )
            result.failures.append(failure)
            print(
                f"  会话录制失败: {failure.message}"
                + (f"；detail={failure.detail}" if failure.detail is not None else ""),
                file=sys.stderr,
            )

    if record_dir is not None:
        # 增量合并，避免“只重录缺失场景”时覆盖已经成功录制的 Live 样本。
        write_acceptance_recording_suite(
            record_dir,
            recordings,
            merge_existing=True,
        )
    return result


def _build_failure_report(
    result: LiveExecutionResult,
    *,
    base_url: str,
) -> dict[str, Any]:
    failures = [asdict(item) for item in result.failures]
    return {
        "format_version": 1,
        "suite_name": RECORDING_SUITE_NAME,
        "generated_at": _utc_now_text(),
        "base_url": base_url,
        "attempted_case_count": len(result.attempted_case_ids),
        "succeeded_case_count": len(result.states),
        "failed_case_count": len(failures),
        "skipped_existing_case_count": len(result.skipped_case_ids),
        "attempted_case_ids": result.attempted_case_ids,
        "skipped_existing_case_ids": result.skipped_case_ids,
        "recording_failures": failures,
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
        "--failure-output",
        type=Path,
        help="把 Live 录制 HTTP 错误和阶段信息写入结构化 JSON",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        help="只执行指定固定场景；可重复传入",
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="根据 record-dir/manifest.json 只补录尚未存在的场景",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=240,
        help="单次计划或会话 HTTP 请求超时秒数",
    )
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
    if args.missing_only and args.record_dir is None:
        parser.error("--missing-only 必须与 --record-dir 一起使用")
    if args.request_timeout <= 0:
        parser.error("--request-timeout 必须大于 0")

    states: list[AgentState] = []
    execution_result: LiveExecutionResult | None = None
    base_url = args.base_url.rstrip("/")
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
            scenario_by_id = {
                scenario.case_id: scenario for scenario in FIXED_ACCEPTANCE_SCENARIOS
            }
            requested_case_ids = list(dict.fromkeys(args.case_id or []))
            unknown_case_ids = sorted(set(requested_case_ids) - set(scenario_by_id))
            if unknown_case_ids:
                raise ValueError(
                    "unknown fixed acceptance case: " + ", ".join(unknown_case_ids)
                )
            selected = (
                [scenario_by_id[case_id] for case_id in requested_case_ids]
                if requested_case_ids
                else list(FIXED_ACCEPTANCE_SCENARIOS)
            )
            existing_case_ids = _load_existing_case_ids(args.record_dir)
            skipped_case_ids: list[str] = []
            if args.missing_only:
                skipped_case_ids = [
                    scenario.case_id
                    for scenario in selected
                    if scenario.case_id in existing_case_ids
                ]
                selected = [
                    scenario
                    for scenario in selected
                    if scenario.case_id not in existing_case_ids
                ]

            execution_result = _execute_and_record(
                base_url,
                args.record_dir,
                selected,
                timeout_seconds=args.request_timeout,
                skipped_case_ids=skipped_case_ids,
            )
            if args.record_dir is not None:
                # 合并写入后重新读取完整套件，使报告包含旧样本和本轮新增样本。
                states.extend(
                    _load_recorded_states(
                        args.record_dir,
                        require_manifest=True,
                        allowed_sources=None,
                    )
                )
            else:
                states.extend(execution_result.states)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"固定验收样本加载或执行失败: {exc}", file=sys.stderr)
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
                    "recording_failure_count": (
                        len(execution_result.failures) if execution_result else 0
                    ),
                },
                ensure_ascii=False,
            )
        )
    else:
        print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")

    if execution_result is not None:
        failure_output = args.failure_output
        if failure_output is None and args.output is not None:
            failure_output = args.output.with_name(
                f"{args.output.stem}-recording-failures{args.output.suffix or '.json'}"
            )
        if failure_output is not None:
            _write_json(
                failure_output,
                _build_failure_report(execution_result, base_url=base_url),
            )

    # 自动化流水线只有在 15 个场景全部覆盖且全部通过时返回 0。
    return 0 if report.missing_case_count == 0 and report.failed_case_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
