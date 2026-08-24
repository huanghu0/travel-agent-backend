"""认证领域模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class User(BaseModel):
    """可安全返回给客户端的用户信息。"""

    user_id: str
    username: str
    created_at: datetime


class UserRecord(User):
    """仅在认证内部使用的用户记录。"""

    password_hash: str
