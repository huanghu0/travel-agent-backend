"""查询和展示持久化智能体会话使用的公共模型。"""

from datetime import datetime

from pydantic import BaseModel

from app.agent_runtime.state import AgentStatus


class AgentSessionSummary(BaseModel):
    session_id: str
    status: AgentStatus
    city: str
    current_step: int
    max_steps: int
    action_count: int
    created_at: datetime
    updated_at: datetime