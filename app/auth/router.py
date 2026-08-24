"""注册、登录与当前用户接口。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.models import User
from app.auth.schemas import AuthCredentials, AuthTokenResponse
from app.auth.service import AuthService, InvalidCredentialsError, UsernameAlreadyExistsError


def build_auth_router(
    auth_service: AuthService | None,
    current_user_dependency: Callable[..., User],
) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["认证"])

    @router.post(
        "/register",
        response_model=AuthTokenResponse,
        status_code=status.HTTP_201_CREATED,
        summary="注册用户",
    )
    def register(credentials: AuthCredentials):
        if auth_service is None:
            raise HTTPException(status_code=503, detail="用户认证尚未正确配置")
        try:
            return auth_service.register(credentials)
        except UsernameAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/login", response_model=AuthTokenResponse, summary="用户登录")
    def login(credentials: AuthCredentials):
        if auth_service is None:
            raise HTTPException(status_code=503, detail="用户认证尚未正确配置")
        try:
            return auth_service.login(credentials)
        except InvalidCredentialsError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    @router.get("/me", response_model=User, summary="查询当前用户")
    def me(current_user: User = Depends(current_user_dependency)):
        return current_user

    return router
