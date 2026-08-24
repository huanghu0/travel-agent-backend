"""认证 HTTP 请求与响应模型。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.auth.models import User


_USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$")


class AuthCredentials(BaseModel):
    username: str = Field(description="3～32 位字母、数字或下划线，不区分大小写")
    password: str = Field(min_length=8, max_length=72, description="8～72 位密码")

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("用户名必须是字符串")
        normalized = value.strip().lower()
        if not _USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("用户名必须为 3～32 位字母、数字或下划线")
        return normalized


class AuthTokenResponse(BaseModel):
    access_token: str = Field(description="7 天有效的 JWT")
    token_type: Literal["bearer"] = Field(default="bearer", description="固定为 bearer")
    expires_in: int = Field(description="Token 有效秒数")
    user: User
