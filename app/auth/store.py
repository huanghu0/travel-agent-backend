"""MySQL 用户 Store。"""

from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError

from app.auth.models import UserRecord
from app.auth.service import UserStore, UsernameAlreadyExistsError
from app.persistence.mysql_base import as_utc, mysql_utc
from app.persistence.sqlalchemy_models import UserRow


class MySQLUserStore(UserStore):
    table = UserRow.__table__

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create(self, user: UserRecord) -> UserRecord:
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    self.table.insert().values(
                        user_id=user.user_id,
                        username=user.username,
                        password_hash=user.password_hash,
                        created_at=mysql_utc(user.created_at),
                    )
                )
        except IntegrityError as exc:
            raise UsernameAlreadyExistsError("用户名已存在") from exc
        return user

    def get_by_username(self, username: str) -> UserRecord | None:
        return self._get(self.table.c.username == username.strip().lower())

    def get_by_id(self, user_id: str) -> UserRecord | None:
        return self._get(self.table.c.user_id == user_id)

    def _get(self, condition) -> UserRecord | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(self.table).where(condition)).mappings().one_or_none()
        if row is None:
            return None
        payload = dict(row)
        payload["created_at"] = as_utc(payload["created_at"])
        return UserRecord.model_validate(payload)
