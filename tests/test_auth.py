from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.auth.dependencies import (
    build_current_user_dependency,
    build_optional_current_user_dependency,
)
from app.auth.models import UserRecord
from app.auth.router import build_auth_router
from app.auth.schemas import AuthCredentials
from app.auth.security import InvalidAccessTokenError, JwtCodec, PasswordSecurity
from app.auth.service import AuthService, UserStore, UsernameAlreadyExistsError
from app.core.config import Settings


SECRET = "test-jwt-secret-key-with-at-least-32-characters"


class MemoryUserStore(UserStore):
    def __init__(self) -> None:
        self.by_id: dict[str, UserRecord] = {}
        self.by_username: dict[str, UserRecord] = {}

    def create(self, user: UserRecord) -> UserRecord:
        if user.username in self.by_username:
            raise UsernameAlreadyExistsError("用户名已存在")
        self.by_id[user.user_id] = user
        self.by_username[user.username] = user
        return user

    def get_by_username(self, username: str) -> UserRecord | None:
        return self.by_username.get(username)

    def get_by_id(self, user_id: str) -> UserRecord | None:
        return self.by_id.get(user_id)


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryUserStore()
        self.security = PasswordSecurity()
        self.codec = JwtCodec(secret_key=SECRET, algorithm="HS256", expire_days=7)
        self.service = AuthService(
            user_store=self.store,
            password_security=self.security,
            jwt_codec=self.codec,
        )
        dependency = build_current_user_dependency(self.service)
        app = FastAPI()
        app.include_router(build_auth_router(self.service, dependency))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_credentials_normalize_username_and_validate_bounds(self):
        credentials = AuthCredentials(username="  Demo_User  ", password="12345678")
        self.assertEqual("demo_user", credentials.username)
        with self.assertRaises(ValidationError):
            AuthCredentials(username="中文用户", password="12345678")
        with self.assertRaises(ValidationError):
            AuthCredentials(username=None, password="12345678")
        with self.assertRaises(ValidationError):
            AuthCredentials(username="demo", password="short")

    def test_register_returns_token_and_never_stores_plaintext_password(self):
        response = self.client.post(
            "/api/auth/register",
            json={"username": "Demo_User", "password": "correct-password"},
        )
        self.assertEqual(201, response.status_code)
        payload = response.json()
        self.assertEqual("bearer", payload["token_type"])
        self.assertEqual(604800, payload["expires_in"])
        self.assertEqual("demo_user", payload["user"]["username"])
        stored = self.store.get_by_username("demo_user")
        self.assertIsNotNone(stored)
        self.assertNotEqual("correct-password", stored.password_hash)
        self.assertNotIn("password_hash", payload["user"])

    def test_duplicate_register_and_bad_login_have_safe_errors(self):
        body = {"username": "demo", "password": "correct-password"}
        self.assertEqual(201, self.client.post("/api/auth/register", json=body).status_code)
        duplicate = self.client.post("/api/auth/register", json=body)
        self.assertEqual(409, duplicate.status_code)
        bad_login = self.client.post(
            "/api/auth/login",
            json={"username": "missing", "password": "wrong-password"},
        )
        self.assertEqual(401, bad_login.status_code)
        self.assertEqual("用户名或密码错误", bad_login.json()["detail"])

    def test_login_and_me_require_valid_bearer_token(self):
        body = {"username": "demo", "password": "correct-password"}
        self.client.post("/api/auth/register", json=body)
        login = self.client.post("/api/auth/login", json=body)
        token = login.json()["access_token"]
        me = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(200, me.status_code)
        self.assertEqual("demo", me.json()["username"])
        missing = self.client.get("/api/auth/me")
        self.assertEqual(401, missing.status_code)
        self.assertEqual("Bearer", missing.headers["www-authenticate"])

    def test_optional_current_user_accepts_anonymous_requests_and_valid_bearer(self):
        app = FastAPI()
        optional_user = build_optional_current_user_dependency(self.service)

        @app.get("/optional")
        def optional_me(user=Depends(optional_user)):
            return {"username": user.username if user else None}

        client = TestClient(app)
        try:
            self.assertEqual({"username": None}, client.get("/optional").json())
            token = self.service.register(
                AuthCredentials(username="viewer", password="correct-password")
            ).access_token
            response = client.get(
                "/optional", headers={"Authorization": f"Bearer {token}"}
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual({"username": "viewer"}, response.json())
        finally:
            client.close()

    def test_optional_current_user_rejects_supplied_bad_credentials_and_degrades_only_when_supplied(self):
        app = FastAPI()
        optional_user = build_optional_current_user_dependency(self.service)

        @app.get("/optional")
        def optional_me(user=Depends(optional_user)):
            return {"username": user.username if user else None}

        client = TestClient(app)
        try:
            for authorization in ("Basic abc", "Bearer", "Bearer invalid"):
                with self.subTest(authorization=authorization):
                    response = client.get(
                        "/optional", headers={"Authorization": authorization}
                    )
                    self.assertEqual(401, response.status_code)
                    self.assertEqual("Bearer", response.headers["www-authenticate"])
        finally:
            client.close()

        unavailable_app = FastAPI()
        unavailable_user = build_optional_current_user_dependency(None)

        @unavailable_app.get("/optional")
        def unavailable_optional_me(user=Depends(unavailable_user)):
            return {"username": user.username if user else None}

        unavailable_client = TestClient(unavailable_app)
        try:
            self.assertEqual(
                {"username": None}, unavailable_client.get("/optional").json()
            )
            self.assertEqual(
                503,
                unavailable_client.get(
                    "/optional", headers={"Authorization": "Bearer token"}
                ).status_code,
            )
        finally:
            unavailable_client.close()

    def test_expired_and_wrong_type_tokens_are_rejected(self):
        old = datetime.now(timezone.utc) - timedelta(days=8)
        expired = self.codec.encode("user-id", now=old)
        with self.assertRaises(InvalidAccessTokenError):
            self.codec.decode_subject(expired)
        now = datetime.now(timezone.utc)
        wrong_type = jwt.encode(
            {
                "sub": "user-id",
                "typ": "refresh",
                "iat": now,
                "exp": now + timedelta(days=1),
            },
            SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(InvalidAccessTokenError):
            self.codec.decode_subject(wrong_type)

    def test_runtime_requires_mysql_and_a_strong_jwt_secret(self):
        runtime = Settings()
        runtime.DATABASE_BACKEND = "sqlite"
        runtime.JWT_SECRET_KEY = SECRET
        with self.assertRaises(RuntimeError):
            runtime.validate_auth_runtime()

        runtime.DATABASE_BACKEND = "mysql"
        runtime.JWT_SECRET_KEY = "too-short"
        with self.assertRaises(RuntimeError):
            runtime.validate_auth_runtime()

        runtime.JWT_SECRET_KEY = SECRET
        runtime.validate_auth_runtime()


if __name__ == "__main__":
    unittest.main()
