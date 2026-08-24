"""注册、登录和 Token 身份解析。"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from app.auth.models import User, UserRecord
from app.auth.schemas import AuthCredentials, AuthTokenResponse
from app.auth.security import InvalidAccessTokenError, JwtCodec, PasswordSecurity
from app.task_runtime.models import utc_now


class UsernameAlreadyExistsError(ValueError):
    pass


class InvalidCredentialsError(ValueError):
    pass


class UserStore(Protocol):
    """认证服务依赖的最小用户持久化接口。"""

    def create(self, user: UserRecord) -> UserRecord: ...

    def get_by_username(self, username: str) -> UserRecord | None: ...

    def get_by_id(self, user_id: str) -> UserRecord | None: ...


class AuthService:
    def __init__(
        self,
        *,
        user_store: UserStore,
        password_security: PasswordSecurity,
        jwt_codec: JwtCodec,
    ) -> None:
        self.user_store = user_store
        self.password_security = password_security
        self.jwt_codec = jwt_codec

    def register(self, credentials: AuthCredentials) -> AuthTokenResponse:
        if self.user_store.get_by_username(credentials.username) is not None:
            raise UsernameAlreadyExistsError("用户名已存在")
        record = UserRecord(
            user_id=str(uuid4()),
            username=credentials.username,
            password_hash=self.password_security.hash(credentials.password),
            created_at=utc_now(),
        )
        created = self.user_store.create(record)
        return self._token_response(created)

    def login(self, credentials: AuthCredentials) -> AuthTokenResponse:
        record = self.user_store.get_by_username(credentials.username)
        if record is None or not self.password_security.verify(
            credentials.password, record.password_hash
        ):
            raise InvalidCredentialsError("用户名或密码错误")
        return self._token_response(record)

    def resolve_token(self, token: str) -> User:
        try:
            user_id = self.jwt_codec.decode_subject(token)
        except InvalidAccessTokenError:
            raise
        record = self.user_store.get_by_id(user_id)
        if record is None:
            raise InvalidAccessTokenError("无效或已过期的访问令牌")
        return User.model_validate(record.model_dump(exclude={"password_hash"}))

    def _token_response(self, record: UserRecord) -> AuthTokenResponse:
        return AuthTokenResponse(
            access_token=self.jwt_codec.encode(record.user_id),
            expires_in=self.jwt_codec.expires_in_seconds,
            user=User.model_validate(record.model_dump(exclude={"password_hash"})),
        )
