"""固定验收录制文件的版本化、脱敏、校验与回放。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from app.agent_runtime.state import AgentState
from app.evaluation.models import AcceptanceScenario


RECORDING_FORMAT_VERSION = 1
RECORDING_SUITE_NAME = "travel-agent-fixed-e2e-v1"
_MANIFEST_FILE_NAME = "manifest.json"
_SENSITIVE_EXACT_KEYS = {"key", "token"}
_SENSITIVE_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "cookie",
)
_VALUE_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:key|token|api_key|apikey)=)[^&\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED]"),
)


class AcceptanceRecording(BaseModel):
    """单个固定场景的可移植录制包。"""

    format_version: int = RECORDING_FORMAT_VERSION
    suite_name: str = RECORDING_SUITE_NAME
    case_id: str
    source: Literal["live", "synthetic", "legacy"] = "live"
    recorded_at: datetime
    request_sha256: str = Field(min_length=64, max_length=64)
    state_sha256: str = Field(min_length=64, max_length=64)
    redacted_paths: list[str] = Field(default_factory=list)
    state: dict[str, Any]


class AcceptanceRecordingEntry(BaseModel):
    """清单中的单文件索引和完整性信息。"""

    case_id: str
    file_name: str
    source: Literal["live", "synthetic", "legacy"]
    recorded_at: datetime
    request_sha256: str
    state_sha256: str
    redacted_field_count: int = Field(default=0, ge=0)


class AcceptanceRecordingManifest(BaseModel):
    """一个完整固定验收录制目录的版本化清单。"""

    format_version: int = RECORDING_FORMAT_VERSION
    suite_name: str = RECORDING_SUITE_NAME
    generated_at: datetime
    total_case_count: int = Field(ge=0)
    records: list[AcceptanceRecordingEntry] = Field(default_factory=list)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _request_sha256(scenario: AcceptanceScenario) -> str:
    return _sha256(scenario.request.model_dump(mode="json"))


def _sanitize_string(value: str) -> str:
    sanitized = value
    for pattern, replacement in _VALUE_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def _sanitize_value(value: Any, path: str, redacted_paths: list[str]) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else str(key)
            normalized_key = str(key).lower().replace("-", "_")
            if normalized_key in _SENSITIVE_EXACT_KEYS or any(
                marker in normalized_key for marker in _SENSITIVE_KEY_MARKERS
            ):
                sanitized[key] = "[REDACTED]"
                redacted_paths.append(item_path)
            else:
                sanitized[key] = _sanitize_value(item, item_path, redacted_paths)
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_value(item, f"{path}[{index}]", redacted_paths)
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        sanitized = _sanitize_string(value)
        if sanitized != value:
            redacted_paths.append(path)
        return sanitized
    return value


def sanitize_recording_payload(
    value: Any,
    *,
    root_path: str = "payload",
) -> tuple[Any, list[str]]:
    """递归清理任意录制载荷，供状态样本和 HTTP 失败报告共同使用。"""

    redacted_paths: list[str] = []
    sanitized_payload = _sanitize_value(value, root_path, redacted_paths)
    return sanitized_payload, sorted(set(redacted_paths))


def sanitize_agent_state(state: AgentState) -> tuple[AgentState, list[str]]:
    """递归移除录制状态中的密钥、令牌和认证信息，并重新执行模型校验。"""

    raw_state = state.model_dump(mode="json")
    sanitized_payload, redacted_paths = sanitize_recording_payload(
        raw_state,
        root_path="state",
    )
    return AgentState.model_validate(sanitized_payload), redacted_paths


def create_acceptance_recording(
    scenario: AcceptanceScenario,
    state: AgentState,
    *,
    source: Literal["live", "synthetic", "legacy"] = "live",
    recorded_at: datetime | None = None,
) -> AcceptanceRecording:
    """校验场景匹配关系后，创建带摘要的脱敏录制包。"""

    if state.request.model_dump(mode="json") != scenario.request.model_dump(mode="json"):
        raise ValueError(f"recorded state does not match scenario: {scenario.case_id}")
    sanitized_state, redacted_paths = sanitize_agent_state(state)
    state_payload = sanitized_state.model_dump(mode="json")
    return AcceptanceRecording(
        case_id=scenario.case_id,
        source=source,
        recorded_at=recorded_at or datetime.now(timezone.utc),
        request_sha256=_request_sha256(scenario),
        state_sha256=_sha256(state_payload),
        redacted_paths=redacted_paths,
        state=state_payload,
    )


def verify_acceptance_recording(
    recording: AcceptanceRecording,
    scenario: AcceptanceScenario | None = None,
) -> AgentState:
    """验证版本、场景摘要和状态摘要，成功后恢复 AgentState。"""

    if recording.format_version != RECORDING_FORMAT_VERSION:
        raise ValueError(
            f"unsupported acceptance recording version: {recording.format_version}"
        )
    if recording.suite_name != RECORDING_SUITE_NAME:
        raise ValueError(f"unexpected acceptance suite: {recording.suite_name}")
    if _sha256(recording.state) != recording.state_sha256:
        raise ValueError(f"acceptance recording checksum mismatch: {recording.case_id}")
    state = AgentState.model_validate(recording.state)
    if scenario is not None:
        if recording.case_id != scenario.case_id:
            raise ValueError(
                f"acceptance recording case mismatch: {recording.case_id} != {scenario.case_id}"
            )
        if recording.request_sha256 != _request_sha256(scenario):
            raise ValueError(f"acceptance request checksum mismatch: {scenario.case_id}")
        if state.request.model_dump(mode="json") != scenario.request.model_dump(mode="json"):
            raise ValueError(f"acceptance state request mismatch: {scenario.case_id}")
    return state


def write_acceptance_recording(path: str | Path, recording: AcceptanceRecording) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(recording.model_dump_json(indent=2), encoding="utf-8")


def _load_manifest_recordings(
    directory: Path,
) -> dict[str, AcceptanceRecording]:
    """读取并校验已有清单，供增量录制合并使用。"""

    manifest_path = directory / _MANIFEST_FILE_NAME
    if not manifest_path.exists():
        return {}
    manifest = AcceptanceRecordingManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    if manifest.format_version != RECORDING_FORMAT_VERSION:
        raise ValueError(
            f"unsupported acceptance manifest version: {manifest.format_version}"
        )
    if manifest.suite_name != RECORDING_SUITE_NAME:
        raise ValueError(f"unexpected acceptance suite: {manifest.suite_name}")
    if manifest.total_case_count != len(manifest.records):
        raise ValueError("acceptance manifest total_case_count does not match records")

    recordings: dict[str, AcceptanceRecording] = {}
    for entry in manifest.records:
        if entry.case_id in recordings:
            raise ValueError(f"duplicate acceptance case in manifest: {entry.case_id}")
        path = _resolve_recording_path(directory, entry.file_name)
        if not path.exists():
            raise ValueError(f"acceptance recording file is missing: {entry.file_name}")
        recording = AcceptanceRecording.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        verify_acceptance_recording(recording)
        if recording.case_id != entry.case_id:
            raise ValueError(f"manifest case mismatch: {entry.case_id}")
        if recording.source != entry.source:
            raise ValueError(f"manifest source mismatch: {entry.case_id}")
        if recording.request_sha256 != entry.request_sha256:
            raise ValueError(f"manifest request checksum mismatch: {entry.case_id}")
        if recording.state_sha256 != entry.state_sha256:
            raise ValueError(f"manifest checksum mismatch: {entry.case_id}")
        recordings[entry.case_id] = recording
    return recordings


def write_acceptance_recording_suite(
    directory: str | Path,
    recordings: Iterable[AcceptanceRecording],
    *,
    generated_at: datetime | None = None,
    merge_existing: bool = False,
) -> AcceptanceRecordingManifest:
    """写入录制文件和 CI 清单；增量模式会保留未被本轮覆盖的已有样本。"""

    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    recording_by_case = (
        _load_manifest_recordings(target_dir) if merge_existing else {}
    )
    for recording in recordings:
        recording_by_case[recording.case_id] = recording

    entries: list[AcceptanceRecordingEntry] = []
    for recording in sorted(recording_by_case.values(), key=lambda item: item.case_id):
        file_name = f"{recording.case_id}.json"
        write_acceptance_recording(target_dir / file_name, recording)
        entries.append(
            AcceptanceRecordingEntry(
                case_id=recording.case_id,
                file_name=file_name,
                source=recording.source,
                recorded_at=recording.recorded_at,
                request_sha256=recording.request_sha256,
                state_sha256=recording.state_sha256,
                redacted_field_count=len(recording.redacted_paths),
            )
        )
    manifest = AcceptanceRecordingManifest(
        generated_at=generated_at or datetime.now(timezone.utc),
        total_case_count=len(entries),
        records=entries,
    )
    (target_dir / _MANIFEST_FILE_NAME).write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )
    return manifest


def load_acceptance_recording(
    path: str | Path,
    *,
    scenario: AcceptanceScenario | None = None,
    allow_legacy: bool = True,
) -> tuple[AgentState, Literal["live", "synthetic", "legacy"]]:
    """加载新版录制包；必要时兼容旧版裸 AgentState 文件。"""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "format_version" in payload and "state" in payload:
        recording = AcceptanceRecording.model_validate(payload)
        return verify_acceptance_recording(recording, scenario), recording.source
    if not allow_legacy:
        raise ValueError(f"legacy acceptance recording is not allowed: {path}")
    state = AgentState.model_validate(payload)
    if scenario is not None and state.request.model_dump(mode="json") != scenario.request.model_dump(mode="json"):
        raise ValueError(f"legacy acceptance state request mismatch: {scenario.case_id}")
    return state, "legacy"


def _resolve_recording_path(source_dir: Path, file_name: str) -> Path:
    """将清单文件名限制在录制目录内，阻止绝对路径和目录穿越。"""

    base = source_dir.resolve()
    candidate = (source_dir / file_name).resolve()
    if candidate == base or base not in candidate.parents:
        raise ValueError(f"acceptance recording path escapes suite directory: {file_name}")
    return candidate


def load_acceptance_recording_suite(
    directory: str | Path,
    scenarios: Iterable[AcceptanceScenario],
    *,
    require_manifest: bool = False,
    allowed_sources: set[str] | None = None,
    allow_legacy: bool = True,
) -> list[AgentState]:
    """按固定场景顺序加载录制目录，并校验可选清单及来源策略。"""

    source_dir = Path(directory)
    manifest_path = source_dir / _MANIFEST_FILE_NAME
    manifest: AcceptanceRecordingManifest | None = None
    if manifest_path.exists():
        manifest = AcceptanceRecordingManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        if manifest.format_version != RECORDING_FORMAT_VERSION:
            raise ValueError(
                f"unsupported acceptance manifest version: {manifest.format_version}"
            )
        if manifest.suite_name != RECORDING_SUITE_NAME:
            raise ValueError(f"unexpected acceptance manifest suite: {manifest.suite_name}")
    elif require_manifest:
        raise ValueError(f"acceptance manifest is required: {manifest_path}")

    manifest_records = manifest.records if manifest is not None else []
    manifest_case_ids = [entry.case_id for entry in manifest_records]
    if len(manifest_case_ids) != len(set(manifest_case_ids)):
        raise ValueError("acceptance manifest contains duplicate case ids")
    manifest_file_names = [entry.file_name for entry in manifest_records]
    if len(manifest_file_names) != len(set(manifest_file_names)):
        raise ValueError("acceptance manifest contains duplicate file names")
    manifest_by_case = {entry.case_id: entry for entry in manifest_records}
    states: list[AgentState] = []
    for scenario in scenarios:
        entry = manifest_by_case.get(scenario.case_id)
        # 清单存在时以清单为唯一索引，避免读取未登记或意外残留的同名文件。
        if manifest is not None and entry is None:
            continue
        file_name = entry.file_name if entry is not None else f"{scenario.case_id}.json"
        path = _resolve_recording_path(source_dir, file_name)
        if not path.exists():
            continue
        state, source = load_acceptance_recording(
            path,
            scenario=scenario,
            allow_legacy=allow_legacy,
        )
        if entry is not None:
            payload = AcceptanceRecording.model_validate_json(path.read_text(encoding="utf-8"))
            if payload.state_sha256 != entry.state_sha256:
                raise ValueError(f"manifest checksum mismatch: {scenario.case_id}")
            if payload.request_sha256 != entry.request_sha256:
                raise ValueError(f"manifest request checksum mismatch: {scenario.case_id}")
            if payload.source != entry.source:
                raise ValueError(f"manifest source mismatch: {scenario.case_id}")
        if allowed_sources is not None and source not in allowed_sources:
            raise ValueError(
                f"acceptance recording source is not allowed: {scenario.case_id}={source}"
            )
        states.append(state)

    if manifest is not None:
        expected_case_ids = {scenario.case_id for scenario in scenarios}
        recorded_case_ids = {entry.case_id for entry in manifest.records}
        unknown = sorted(recorded_case_ids - expected_case_ids)
        if unknown:
            raise ValueError(f"manifest contains unknown acceptance cases: {', '.join(unknown)}")
        if manifest.total_case_count != len(manifest.records):
            raise ValueError("acceptance manifest total_case_count does not match records")
    return states
