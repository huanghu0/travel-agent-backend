"""Argon2id 密码安全和 HS256 JWT 编解码。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class InvalidAccessTokenError(ValueError):
    """Access Token 无法验证。"""


class PasswordSecurity:
    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher()

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False


class JwtCodec:
    def __init__(self, *, secret_key: str, algorithm: str, expire_days: int) -> None:
        secret = secret_key.strip()
        if len(secret) < 32:
            raise ValueError("JWT 密钥长度至少为 32 个字符")
        if algorithm.upper() != "HS256":
            raise ValueError("JWT 当前只支持 HS256")
        if expire_days <= 0:
            raise ValueError("JWT 有效天数必须为正整数")
        self.secret_key = secret
        self.algorithm = "HS256"
        self.expire_days = expire_days

    @property
    def expires_in_seconds(self) -> int:
        return self.expire_days * 24 * 60 * 60

    def encode(self, user_id: str, *, now: datetime | None = None) -> str:
        issued_at = now or datetime.now(timezone.utc)
        payload = {
            "sub": user_id,
            "typ": "access",
            "iat": issued_at,
            "exp": issued_at + timedelta(days=self.expire_days),
        }
        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)

    def decode_subject(self, token: str) -> str:
        try:
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm],
                options={"require": ["sub", "typ", "iat", "exp"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidAccessTokenError("无效或已过期的访问令牌") from exc
        subject = payload.get("sub")
        if payload.get("typ") != "access" or not isinstance(subject, str) or not subject:
            raise InvalidAccessTokenError("无效或已过期的访问令牌")
        return subject
