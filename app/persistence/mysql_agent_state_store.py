"""使用 MySQL 持久化完整 AgentState 检查点和质量基线。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, select, update

from app.agent_runtime.state import CURRENT_AGENT_STATE_VERSION, AgentState, AgentStatus
from app.evaluation import FixedAcceptanceBaselineReport, build_fixed_acceptance_baseline
from app.memory.execution_analytics import build_execution_baseline
from app.memory.models import AgentSessionSummary, ExecutionBaselineReport, QualityBaselineReport
from app.memory.quality_analytics import (
    build_quality_baseline,
    infer_completion_mode,
    state_issue_codes,
    state_quality_level,
)
from app.persistence.exceptions import SessionNotFoundError
from app.persistence.interfaces import AgentStateStore
from app.persistence.mysql_base import MySQLStoreBase, as_utc, mysql_utc
from app.persistence.sqlalchemy_models import (
    AgentSessionRow,
    TripDraftRow,
    TripPlanVersionRow,
    TripPlanningTaskRow,
    TripTaskEventRow,
)


class MySQLAgentStateStore(MySQLStoreBase, AgentStateStore):
    """通过 MySQL 保存会话检查点，并复用现有纯函数生成统计基线。"""

    table = AgentSessionRow.__table__

    @staticmethod
    def _quality_values(state: AgentState) -> dict[str, Any]:
        report = state.acceptance_report
        return {
            "travel_days": state.request.travel_days,
            "transportation": state.request.transportation,
            "completion_mode": infer_completion_mode(state),
            "quality_level": state_quality_level(state),
            "quality_score": report.quality_score if report is not None else None,
            "warning_count": len(state.completion_warnings or (report.warnings if report else [])),
            "issue_codes_json": json.dumps(state_issue_codes(state), ensure_ascii=False),
            "tool_call_count": state.tool_call_count,
            "llm_call_count": state.llm_call_count,
            "total_duration_ms": state.total_duration_ms,
        }

    def save_state(self, state: AgentState) -> None:
        """保存最新检查点；完整 JSON 与高频查询冗余列在同一事务更新。"""

        values = self._state_values(state)
        update_values = {
            key: value
            for key, value in values.items()
            if key not in {"session_id", "created_at", "user_id"}
        }
        with self.engine.begin() as connection:
            if not state.checkpoint_persisted:
                connection.execute(self.table.insert().values(**values))
            else:
                result = connection.execute(
                    update(self.table)
                    .where(
                        self.table.c.session_id == state.session_id,
                        self.table.c.user_id == state.user_id,
                    )
                    .values(**update_values)
                )
                if result.rowcount == 0:
                    exists = connection.execute(
                        select(self.table.c.session_id).where(
                            self.table.c.session_id == state.session_id,
                            self.table.c.user_id == state.user_id,
                        )
                    ).scalar_one_or_none()
                    if exists is None:
                        raise SessionNotFoundError(f"会话不存在: {state.session_id}")
        state.mark_checkpoint_persisted()

    def get_state(self, session_id: str, *, user_id: str | None = None) -> AgentState:
        filters = [self.table.c.session_id == session_id]
        if user_id is not None:
            filters.append(self.table.c.user_id == user_id)
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(self.table.c.state_json).where(*filters)
            ).scalar_one_or_none()
        if payload is None:
            raise SessionNotFoundError(f"会话不存在: {session_id}")
        state = AgentState.model_validate_json(payload)
        state.mark_checkpoint_persisted()
        return state

    def list_sessions(
        self,
        *,
        limit: int = 50,
        status: AgentStatus | None = None,
        user_id: str | None = None,
    ) -> list[AgentSessionSummary]:
        safe_limit = max(1, min(limit, 200))
        columns = (
            self.table.c.session_id,
            self.table.c.status,
            self.table.c.city,
            self.table.c.current_step,
            self.table.c.max_steps,
            self.table.c.action_count,
            self.table.c.created_at,
            self.table.c.updated_at,
        )
        statement = select(*columns)
        if user_id is not None:
            statement = statement.where(self.table.c.user_id == user_id)
        if status is not None:
            statement = statement.where(self.table.c.status == status)
        statement = statement.order_by(self.table.c.updated_at.desc()).limit(safe_limit)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        summaries: list[AgentSessionSummary] = []
        for row in rows:
            payload = dict(row)
            payload["created_at"] = as_utc(payload["created_at"])
            payload["updated_at"] = as_utc(payload["updated_at"])
            summaries.append(AgentSessionSummary.model_validate(payload))
        return summaries

    def _query_state_payloads(
        self,
        *,
        limit: int,
        filters: list[Any],
    ) -> tuple[int, list[str]]:
        count_statement = select(func.count()).select_from(self.table)
        rows_statement = select(self.table.c.state_json)
        if filters:
            count_statement = count_statement.where(*filters)
            rows_statement = rows_statement.where(*filters)
        rows_statement = rows_statement.order_by(self.table.c.updated_at.desc()).limit(limit)
        with self.engine.connect() as connection:
            matching_count = int(connection.execute(count_statement).scalar_one())
            payloads = list(connection.execute(rows_statement).scalars().all())
        return matching_count, payloads

    @staticmethod
    def _restore_states(payloads: list[str]) -> tuple[list[AgentState], int]:
        states: list[AgentState] = []
        invalid_count = 0
        for payload in payloads:
            try:
                states.append(AgentState.model_validate_json(payload))
            except (TypeError, ValueError):
                invalid_count += 1
        return states, invalid_count

    def get_execution_baseline(
        self,
        *,
        limit: int = 1000,
        status: AgentStatus | None = None,
        city: str | None = None,
        top_n: int = 20,
        max_cycle_span: int = 12,
        user_id: str | None = None,
    ) -> ExecutionBaselineReport:
        safe_limit = max(1, min(limit, 5000))
        safe_top_n = max(1, min(top_n, 100))
        safe_cycle_span = max(1, min(max_cycle_span, 50))
        normalized_city = city.strip() if city and city.strip() else None
        filters: list[Any] = []
        if user_id is not None:
            filters.append(self.table.c.user_id == user_id)
        if status is not None:
            filters.append(self.table.c.status == status)
        if normalized_city is not None:
            filters.append(self.table.c.city == normalized_city)
        matching_count, payloads = self._query_state_payloads(limit=safe_limit, filters=filters)
        states, invalid_count = self._restore_states(payloads)
        return build_execution_baseline(
            states,
            requested_limit=safe_limit,
            matching_session_count=matching_count,
            sampled_row_count=len(payloads),
            invalid_session_count=invalid_count,
            status_filter=status,
            city_filter=normalized_city,
            top_n=safe_top_n,
            max_cycle_span=safe_cycle_span,
        )

    def get_quality_baseline(
        self,
        *,
        limit: int = 1000,
        status: AgentStatus | None = None,
        city: str | None = None,
        travel_days: int | None = None,
        transportation: str | None = None,
        completion_mode: str | None = None,
        quality_level: str | None = None,
        top_n: int = 20,
        user_id: str | None = None,
    ) -> QualityBaselineReport:
        safe_limit = max(1, min(limit, 5000))
        safe_top_n = max(1, min(top_n, 100))
        normalized_city = city.strip() if city and city.strip() else None
        normalized_transportation = transportation.strip() if transportation and transportation.strip() else None
        filters: list[Any] = []
        if user_id is not None:
            filters.append(self.table.c.user_id == user_id)
        for column, value in (
            (self.table.c.status, status),
            (self.table.c.city, normalized_city),
            (self.table.c.travel_days, travel_days),
            (self.table.c.transportation, normalized_transportation),
            (self.table.c.completion_mode, completion_mode),
            (self.table.c.quality_level, quality_level),
        ):
            if value is not None:
                filters.append(column == value)
        matching_count, payloads = self._query_state_payloads(limit=safe_limit, filters=filters)
        states, invalid_count = self._restore_states(payloads)
        return build_quality_baseline(
            states,
            requested_limit=safe_limit,
            matching_session_count=matching_count,
            sampled_row_count=len(payloads),
            invalid_session_count=invalid_count,
            status_filter=status,
            city_filter=normalized_city,
            travel_days_filter=travel_days,
            transportation_filter=normalized_transportation,
            completion_mode_filter=completion_mode,
            quality_level_filter=quality_level,
            top_n=safe_top_n,
        )

    def get_fixed_acceptance_baseline(
        self, *, limit: int = 5000, user_id: str | None = None
    ) -> FixedAcceptanceBaselineReport:
        safe_limit = max(1, min(limit, 10000))
        filters = [self.table.c.user_id == user_id] if user_id is not None else []
        _, payloads = self._query_state_payloads(limit=safe_limit, filters=filters)
        states, invalid_count = self._restore_states(payloads)
        return build_fixed_acceptance_baseline(
            states,
            requested_limit=safe_limit,
            sampled_session_count=len(payloads),
            invalid_session_count=invalid_count,
        )

    def delete_session(self, session_id: str, *, user_id: str) -> list[str]:
        """在同一事务中级联删除当前用户的旅行会话聚合。"""

        task_table = TripPlanningTaskRow.__table__
        event_table = TripTaskEventRow.__table__
        draft_table = TripDraftRow.__table__
        version_table = TripPlanVersionRow.__table__
        with self.engine.begin() as connection:
            exists = connection.execute(
                select(self.table.c.session_id)
                .where(
                    self.table.c.session_id == session_id,
                    self.table.c.user_id == user_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if exists is None:
                raise SessionNotFoundError(f"会话不存在: {session_id}")
            task_id_statement = select(task_table.c.task_id).where(
                task_table.c.session_id == session_id,
                task_table.c.user_id == user_id,
            )
            task_ids = list(connection.execute(task_id_statement).scalars().all())
            if task_ids:
                connection.execute(delete(event_table).where(event_table.c.task_id.in_(task_ids)))
            connection.execute(
                delete(task_table).where(
                    task_table.c.session_id == session_id,
                    task_table.c.user_id == user_id,
                )
            )
            connection.execute(delete(draft_table).where(draft_table.c.session_id == session_id))
            connection.execute(
                delete(version_table).where(version_table.c.session_id == session_id)
            )
            connection.execute(
                delete(self.table).where(
                    self.table.c.session_id == session_id,
                    self.table.c.user_id == user_id,
                )
            )
        return task_ids
    def _state_values(self, state: AgentState) -> dict[str, Any]:
        state.state_version = CURRENT_AGENT_STATE_VERSION
        state.touch()
        return {
            "session_id": state.session_id,
            "user_id": state.user_id,
            "status": state.status,
            "city": state.request.city,
            "current_step": state.current_step,
            "max_steps": state.max_steps,
            "action_count": len(state.action_history),
            **self._quality_values(state),
            "state_json": state.model_dump_json(),
            "created_at": mysql_utc(state.created_at),
            "updated_at": mysql_utc(state.updated_at),
        }

    def create_state(self, state: AgentState) -> None:
        """首次创建会话；重复 session_id 由主键约束拒绝。"""

        with self.engine.begin() as connection:
            connection.execute(self.table.insert().values(**self._state_values(state)))
        state.mark_checkpoint_persisted()
