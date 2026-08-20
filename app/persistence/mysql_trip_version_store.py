"""使用 MySQL 持久化行程草稿和不可变版本历史。"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.persistence.exceptions import DraftNotFoundError, VersionNotFoundError
from app.persistence.interfaces import TripVersionStore
from app.persistence.mysql_base import MySQLStoreBase, mysql_utc, utc_now
from app.persistence.sqlalchemy_models import TripDraftRow, TripPlanVersionRow
from app.schemas.trip_draft_schema import TripDraft, TripPlanVersion


class MySQLTripVersionStore(MySQLStoreBase, TripVersionStore):
    """草稿可覆盖保存，正式版本按编号保留并支持原子确认。"""

    version_table = TripPlanVersionRow.__table__
    draft_table = TripDraftRow.__table__

    def save_version(self, version: TripPlanVersion) -> TripPlanVersion:
        statement = mysql_insert(self.version_table).values(
            version_id=version.version_id,
            session_id=version.session_id,
            version_number=version.version_number,
            status=version.status,
            source=version.source,
            source_draft_id=version.source_draft_id,
            version_json=version.model_dump_json(),
            created_at=mysql_utc(version.created_at),
            confirmed_at=mysql_utc(version.confirmed_at),
        )
        with self.engine.begin() as connection:
            connection.execute(
                statement.on_duplicate_key_update(
                    session_id=statement.inserted.session_id,
                    version_number=statement.inserted.version_number,
                    status=statement.inserted.status,
                    source=statement.inserted.source,
                    source_draft_id=statement.inserted.source_draft_id,
                    version_json=statement.inserted.version_json,
                    confirmed_at=statement.inserted.confirmed_at,
                )
            )
        return version

    def get_version(self, session_id: str, version_number: int) -> TripPlanVersion:
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(self.version_table.c.version_json).where(
                    self.version_table.c.session_id == session_id,
                    self.version_table.c.version_number == version_number,
                )
            ).scalar_one_or_none()
        if payload is None:
            raise VersionNotFoundError(f"行程版本不存在：{session_id} v{version_number}")
        return TripPlanVersion.model_validate_json(payload)

    def get_version_by_id(self, version_id: str) -> TripPlanVersion:
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(self.version_table.c.version_json).where(
                    self.version_table.c.version_id == version_id
                )
            ).scalar_one_or_none()
        if payload is None:
            raise VersionNotFoundError(f"行程版本不存在：{version_id}")
        return TripPlanVersion.model_validate_json(payload)

    def get_confirmed_version(self, session_id: str) -> TripPlanVersion | None:
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(self.version_table.c.version_json)
                .where(
                    self.version_table.c.session_id == session_id,
                    self.version_table.c.status == "confirmed",
                )
                .order_by(self.version_table.c.version_number.desc())
                .limit(1)
            ).scalar_one_or_none()
        return TripPlanVersion.model_validate_json(payload) if payload else None

    def list_versions(self, session_id: str) -> list[TripPlanVersion]:
        with self.engine.connect() as connection:
            payloads = connection.execute(
                select(self.version_table.c.version_json)
                .where(self.version_table.c.session_id == session_id)
                .order_by(self.version_table.c.version_number.desc())
            ).scalars().all()
        return [TripPlanVersion.model_validate_json(payload) for payload in payloads]

    def next_version_number(self, session_id: str) -> int:
        with self.engine.connect() as connection:
            value = connection.execute(
                select(func.coalesce(func.max(self.version_table.c.version_number), 0)).where(
                    self.version_table.c.session_id == session_id
                )
            ).scalar_one()
        return int(value) + 1

    def save_draft(self, draft: TripDraft) -> TripDraft:
        statement = mysql_insert(self.draft_table).values(
            draft_id=draft.draft_id,
            session_id=draft.session_id,
            base_version=draft.base_version,
            status=draft.status,
            draft_json=draft.model_dump_json(),
            candidate_version_id=draft.candidate_version_id,
            created_at=mysql_utc(draft.created_at),
            updated_at=mysql_utc(draft.updated_at),
        )
        with self.engine.begin() as connection:
            connection.execute(
                statement.on_duplicate_key_update(
                    status=statement.inserted.status,
                    draft_json=statement.inserted.draft_json,
                    candidate_version_id=statement.inserted.candidate_version_id,
                    updated_at=statement.inserted.updated_at,
                )
            )
        return draft

    def get_draft(self, session_id: str, draft_id: str) -> TripDraft:
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(self.draft_table.c.draft_json).where(
                    self.draft_table.c.session_id == session_id,
                    self.draft_table.c.draft_id == draft_id,
                )
            ).scalar_one_or_none()
        if payload is None:
            raise DraftNotFoundError(f"行程草稿不存在：{draft_id}")
        return TripDraft.model_validate_json(payload)

    def supersede_candidate(self, version_id: str | None) -> None:
        if not version_id:
            return
        with self.engine.begin() as connection:
            payload = connection.execute(
                select(self.version_table.c.version_json)
                .where(self.version_table.c.version_id == version_id)
                .with_for_update()
            ).scalar_one_or_none()
            if payload is None:
                raise VersionNotFoundError(f"行程版本不存在：{version_id}")
            version = TripPlanVersion.model_validate_json(payload)
            if version.status != "candidate":
                return
            version.status = "superseded"
            connection.execute(
                update(self.version_table)
                .where(self.version_table.c.version_id == version_id)
                .values(status="superseded", version_json=version.model_dump_json())
            )

    def confirm_version(self, version: TripPlanVersion) -> TripPlanVersion:
        """锁定同一会话的版本集合，原子撤销旧确认版本并确认目标版本。"""

        now = utc_now()
        with self.engine.begin() as connection:
            rows = connection.execute(
                select(self.version_table.c.version_id, self.version_table.c.version_json)
                .where(self.version_table.c.session_id == version.session_id)
                .with_for_update()
            ).mappings().all()
            if not any(row["version_id"] == version.version_id for row in rows):
                raise VersionNotFoundError(f"行程版本不存在：{version.version_id}")

            for row in rows:
                old = TripPlanVersion.model_validate_json(row["version_json"])
                if old.status != "confirmed" or old.version_id == version.version_id:
                    continue
                old.status = "superseded"
                connection.execute(
                    update(self.version_table)
                    .where(self.version_table.c.version_id == old.version_id)
                    .values(status="superseded", version_json=old.model_dump_json())
                )

            version.status = "confirmed"
            version.confirmed_at = now
            connection.execute(
                update(self.version_table)
                .where(self.version_table.c.version_id == version.version_id)
                .values(
                    status="confirmed",
                    version_json=version.model_dump_json(),
                    confirmed_at=mysql_utc(now),
                )
            )
        return version
