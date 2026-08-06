"""Structured models produced by deterministic trip-plan validation."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(BaseModel):
    """One actionable semantic problem found in a generated TripPlan."""

    code: str
    severity: ValidationSeverity
    path: str
    message: str
    repair_hint: str
    repairable: bool = True
    expected: Any = None
    actual: Any = None


class TripValidationResult(BaseModel):
    """Complete result of one deterministic validation pass."""

    valid: bool
    repairable: bool
    error_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    issues: list[ValidationIssue] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def from_issues(cls, issues: list[ValidationIssue]) -> "TripValidationResult":
        errors = [item for item in issues if item.severity is ValidationSeverity.ERROR]
        warnings = [item for item in issues if item.severity is ValidationSeverity.WARNING]
        return cls(
            valid=not errors,
            repairable=bool(errors) and all(item.repairable for item in errors),
            error_count=len(errors),
            warning_count=len(warnings),
            issues=issues,
        )

    def error_summary(self, *, limit: int = 8) -> str:
        errors = [item for item in self.issues if item.severity is ValidationSeverity.ERROR]
        if not errors:
            return "行程校验通过"
        shown = errors[:limit]
        detail = "; ".join(f"{item.path}: {item.message}" for item in shown)
        remaining = len(errors) - len(shown)
        if remaining > 0:
            detail += f"; 另有 {remaining} 个错误"
        return detail
