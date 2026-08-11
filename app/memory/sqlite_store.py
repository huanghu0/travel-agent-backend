"""SQLite 会话记忆：保存完整 AgentState 检查点，并提供轻量会话摘要查询。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.agent_runtime.state import (
    CURRENT_AGENT_STATE_VERSION,
    AgentState,
    AgentStatus,
)
from app.memory.execution_analytics import build_execution_baseline
from app.memory.models import AgentSessionSummary, ExecutionBaselineReport


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
                    session_id,
                    status,
                    city,
                    current_step,
                    max_steps,
                    action_count,
                    state_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    status = excluded.status,
                    city = excluded.city,
                    current_step = excluded.current_step,
                    max_steps = excluded.max_steps,
                    action_count = excluded.action_count,
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
