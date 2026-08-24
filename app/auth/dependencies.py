"""FastAPI Bearer Token 认证依赖。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.models import User
from app.auth.security import InvalidAccessTokenError
from app.auth.service import AuthService


_bearer = HTTPBearer(auto_error=False)


def build_current_user_dependency(auth_service: AuthService | None) -> Callable[..., User]:
    def get_current_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> User:
        if auth_service is None:
            raise HTTPException(status_code=503, detail="用户认证尚未正确配置")
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise _unauthorized()
        try:
            return auth_service.resolve_token(credentials.credentials)
        except InvalidAccessTokenError as exc:
            raise _unauthorized() from exc

    return get_current_user


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效或已过期的访问令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )
