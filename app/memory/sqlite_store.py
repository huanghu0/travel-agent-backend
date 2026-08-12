"""SQLite 会话记忆：保存完整 AgentState 检查点，并提供轻量会话摘要查询。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.agent_runtime.state import (
    CURRENT_AGENT_STATE_VERSION,
    AgentState,
    AgentStatus,
)
from app.evaluation import FixedAcceptanceBaselineReport, build_fixed_acceptance_baseline
from app.memory.execution_analytics import build_execution_baseline
from app.memory.quality_analytics import (
    build_quality_baseline,
    infer_completion_mode,
    state_issue_codes,
    state_quality_level,
)
from app.memory.models import (
    AgentSessionSummary,
    ExecutionBaselineReport,
    QualityBaselineReport,
)


class SessionNotFoundError(LookupError):
    """找不到指定持久化会话时抛出。"""


class SQLiteAgentStateStore:
    """使用 SQLite UPSERT 保存完整 AgentState 检查点。"""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        # 步骤 1：启用 WAL，提升读写并发；随后创建会话表和查询索引。
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    session_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    city TEXT NOT NULL,
                    current_step INTEGER NOT NULL,
                    max_steps INTEGER NOT NULL,
                    action_count INTEGER NOT NULL,
                    travel_days INTEGER,
                    transportation TEXT,
                    completion_mode TEXT,
                    quality_level TEXT,
                    quality_score REAL,
                    warning_count INTEGER NOT NULL DEFAULT 0,
                    issue_codes_json TEXT NOT NULL DEFAULT '[]',
                    tool_call_count INTEGER NOT NULL DEFAULT 0,
                    llm_call_count INTEGER NOT NULL DEFAULT 0,
                    total_duration_ms INTEGER NOT NULL DEFAULT 0,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_updated_at
                ON agent_sessions(updated_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_status
                ON agent_sessions(status)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_city
                ON agent_sessions(city)
                """
            )
            # 步骤 2：对旧数据库执行幂等列迁移，并为常用质量筛选建立索引。
            self._ensure_quality_columns(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_completion_mode
                ON agent_sessions(completion_mode)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_quality_level
                ON agent_sessions(quality_level)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_request_dimensions
                ON agent_sessions(city, travel_days, transportation)
                """
            )
            # 步骤 3：旧检查点没有冗余质量列时，从 state_json 一次性回填。
            self._backfill_quality_columns(connection)

    @staticmethod
    def _ensure_quality_columns(connection: sqlite3.Connection) -> None:
        """兼容已有 SQLite 文件，逐列补齐质量可观测性字段。"""

        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(agent_sessions)")
        }
        definitions = {
            "travel_days": "INTEGER",
            "transportation": "TEXT",
            "completion_mode": "TEXT",
            "quality_level": "TEXT",
            "quality_score": "REAL",
            "warning_count": "INTEGER NOT NULL DEFAULT 0",
            "issue_codes_json": "TEXT NOT NULL DEFAULT '[]'",
            "tool_call_count": "INTEGER NOT NULL DEFAULT 0",
            "llm_call_count": "INTEGER NOT NULL DEFAULT 0",
            "total_duration_ms": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in definitions.items():
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE agent_sessions ADD COLUMN {column} {definition}"
                )

    @staticmethod
    def _quality_values(state: AgentState) -> tuple[object, ...]:
        """把大状态对象投影成 SQLite 可直接筛选和聚合的小字段。"""

        report = state.acceptance_report
        return (
            state.request.travel_days,
            state.request.transportation,
            infer_completion_mode(state),
            state_quality_level(state),
            report.quality_score if report is not None else None,
            len(state.completion_warnings or (report.warnings if report else [])),
            json.dumps(state_issue_codes(state), ensure_ascii=False),
            state.tool_call_count,
            state.llm_call_count,
            state.total_duration_ms,
        )

    def _backfill_quality_columns(self, connection: sqlite3.Connection) -> None:
        """为升级前的会话补齐质量列；坏数据只跳过，不阻断服务启动。"""

        rows = connection.execute(
            """
            SELECT session_id, state_json
            FROM agent_sessions
            WHERE travel_days IS NULL OR transportation IS NULL
            """
        ).fetchall()
        for row in rows:
            try:
                state = AgentState.model_validate_json(row["state_json"])
            except (TypeError, ValueError):
                continue
            connection.execute(
                """
                UPDATE agent_sessions SET
                    travel_days = ?, transportation = ?, completion_mode = ?,
                    quality_level = ?, quality_score = ?, warning_count = ?,
                    issue_codes_json = ?, tool_call_count = ?, llm_call_count = ?,
                    total_duration_ms = ?
                WHERE session_id = ?
                """,
                (*self._quality_values(state), state.session_id),
            )

    def save_state(self, state: AgentState) -> None:
        """创建或覆盖指定会话的最新检查点。"""

        # 步骤 1：更新时间和版本，并把完整状态序列化为 JSON。
        state.state_version = CURRENT_AGENT_STATE_VERSION
        state.touch()
        payload = state.model_dump_json()
        # 步骤 2：使用 UPSERT 覆盖同一会话的最新检查点，session_id 保持不变。
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO agent_sessions (
                    session_id, status, city, current_step, max_steps, action_count,
                    travel_days, transportation, completion_mode, quality_level,
                    quality_score, warning_count, issue_codes_json, tool_call_count,
                    llm_call_count, total_duration_ms, state_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    status = excluded.status,
                    city = excluded.city,
                    current_step = excluded.current_step,
                    max_steps = excluded.max_steps,
                    action_count = excluded.action_count,
                    travel_days = excluded.travel_days,
                    transportation = excluded.transportation,
                    completion_mode = excluded.completion_mode,
                    quality_level = excluded.quality_level,
                    quality_score = excluded.quality_score,
                    warning_count = excluded.warning_count,
                    issue_codes_json = excluded.issue_codes_json,
                    tool_call_count = excluded.tool_call_count,
                    llm_call_count = excluded.llm_call_count,
                    total_duration_ms = excluded.total_duration_ms,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.session_id,
                    state.status,
                    state.request.city,
                    state.current_step,
                    state.max_steps,
                    len(state.action_history),
                    *self._quality_values(state),
                    payload,
                    state.created_at.isoformat(),
                    state.updated_at.isoformat(),
                ),
            )

    def get_state(self, session_id: str) -> AgentState:
        """加载并校验指定会话的最新检查点。"""

        # 步骤 1：按 session_id 读取最近一次完整 JSON 检查点。
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state_json FROM agent_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"会话不存在: {session_id}")
        # 步骤 2：通过 Pydantic 恢复类型、枚举和嵌套模型，避免直接使用不可信 JSON。
        return AgentState.model_validate_json(row["state_json"])

    def list_sessions(
        self,
        *,
        limit: int = 50,
        status: AgentStatus | None = None,
    ) -> list[AgentSessionSummary]:
        """不加载完整大状态对象，只查询最近会话摘要。"""

        # 只查询摘要列，不加载可能很大的 state_json；同时限制单次最多 200 条。
        safe_limit = max(1, min(limit, 200))
        query = """
            SELECT session_id, status, city, current_step, max_steps,
                   action_count, created_at, updated_at
            FROM agent_sessions
        """
        params: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(safe_limit)

        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [AgentSessionSummary.model_validate(dict(row)) for row in rows]

    def get_execution_baseline(
        self,
        *,
        limit: int = 1000,
        status: AgentStatus | None = None,
        city: str | None = None,
        top_n: int = 20,
        max_cycle_span: int = 12,
    ) -> ExecutionBaselineReport:
        """从最近会话检查点统计完成率、动作跳转和常见循环。"""

        # 步骤 1：限制分析样本和排行榜规模，避免一次读取过多大型 state_json。
        safe_limit = max(1, min(limit, 5000))
        safe_top_n = max(1, min(top_n, 100))
        safe_cycle_span = max(1, min(max_cycle_span, 50))
        normalized_city = city.strip() if city and city.strip() else None

        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if normalized_city is not None:
            clauses.append("city = ?")
            params.append(normalized_city)
        where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        # 步骤 2：先得到完整匹配数量，再按更新时间读取最近样本。
        with self._connection() as connection:
            matching_session_count = connection.execute(
                f"SELECT COUNT(*) FROM agent_sessions{where_clause}",
                params,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT state_json
                FROM agent_sessions
                {where_clause}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                [*params, safe_limit],
            ).fetchall()

        # 步骤 3：逐条恢复 AgentState，单条旧数据损坏不会阻断整份基线报告。
        states: list[AgentState] = []
        invalid_session_count = 0
        for row in rows:
            try:
                states.append(AgentState.model_validate_json(row["state_json"]))
            except (TypeError, ValueError):
                invalid_session_count += 1

        # 步骤 4：在内存中聚合动作、跳转、循环以及城市完成率。
        return build_execution_baseline(
            states,
            requested_limit=safe_limit,
            matching_session_count=matching_session_count,
            sampled_row_count=len(rows),
            invalid_session_count=invalid_session_count,
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
    ) -> QualityBaselineReport:
        """从 SQLite 最近会话统计交付质量、警告和资源消耗基线。"""

        safe_limit = max(1, min(limit, 5000))
        safe_top_n = max(1, min(top_n, 100))
        normalized_city = city.strip() if city and city.strip() else None
        normalized_transportation = (
            transportation.strip()
            if transportation and transportation.strip()
            else None
        )

        # 步骤 1：筛选字段均来自冗余质量列，不需要反序列化无关会话。
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (
            ("status", status),
            ("city", normalized_city),
            ("travel_days", travel_days),
            ("transportation", normalized_transportation),
            ("completion_mode", completion_mode),
            ("quality_level", quality_level),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        # 步骤 2：只加载最近 limit 个完整状态；匹配总数单独保留用于判断是否截断。
        with self._connection() as connection:
            matching_session_count = connection.execute(
                f"SELECT COUNT(*) FROM agent_sessions{where_clause}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT state_json
                FROM agent_sessions
                {where_clause}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                [*params, safe_limit],
            ).fetchall()

        states: list[AgentState] = []
        invalid_session_count = 0
        for row in rows:
            try:
                states.append(AgentState.model_validate_json(row["state_json"]))
            except (TypeError, ValueError):
                invalid_session_count += 1

        # 步骤 3：统一在纯函数中聚合，便于单元测试和未来离线批处理复用。
        return build_quality_baseline(
            states,
            requested_limit=safe_limit,
            matching_session_count=matching_session_count,
            sampled_row_count=len(rows),
            invalid_session_count=invalid_session_count,
            status_filter=status,
            city_filter=normalized_city,
            travel_days_filter=travel_days,
            transportation_filter=normalized_transportation,
            completion_mode_filter=completion_mode,
            quality_level_filter=quality_level,
            top_n=safe_top_n,
        )

    def get_fixed_acceptance_baseline(
        self,
        *,
        limit: int = 5000,
    ) -> FixedAcceptanceBaselineReport:
        """用 SQLite 最近会话覆盖固定 15 场景，并返回确定性验收报告。"""

        safe_limit = max(1, min(limit, 10000))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT state_json
                FROM agent_sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

        states: list[AgentState] = []
        invalid_session_count = 0
        for row in rows:
            try:
                states.append(AgentState.model_validate_json(row["state_json"]))
            except (TypeError, ValueError):
                invalid_session_count += 1

        return build_fixed_acceptance_baseline(
            states,
            requested_limit=safe_limit,
            sampled_session_count=len(rows),
            invalid_session_count=invalid_session_count,
        )
