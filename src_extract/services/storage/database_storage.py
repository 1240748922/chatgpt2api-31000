from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Mapping, Sequence

from sqlalchemy import Column, Integer, String, Text, select, text, true
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import sessionmaker

from services.application_database import (
    DatabaseBase,
    display_database_url,
    initialize_application_database,
)
from services.storage.base import (
    StorageBackend,
    StorageCapabilities,
    StorageCollection,
    StorageMutation,
    StorageMutationResult,
    StorageRevisionConflictError,
    StorageSnapshot,
)
from services.storage.mutation import item_key, normalize_items, normalize_mutation


_account_commit_hook = ContextVar("account_commit_hook", default=None)


@contextmanager
def account_commit_hook(callback, backend):
    """Attach a durable import checkpoint to this worker's account transaction.

    Context-local, never shared with other request/maintenance threads. A lost
    job lease raises before writing, rolling back both rows and checkpoint.
    """
    token = _account_commit_hook.set((backend, callback))
    try:
        yield
    finally:
        _account_commit_hook.reset(token)


class AccountModel(DatabaseBase):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(String(2048), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)


class AuthKeyModel(DatabaseBase):
    __tablename__ = "auth_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_id = Column(String(255), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)


class StorageRevisionModel(DatabaseBase):
    __tablename__ = "storage_revisions"

    collection = Column(String(32), primary_key=True)
    version = Column(Integer, nullable=False, default=0)


class DatabaseStorageBackend(StorageBackend):
    """Transactional database adapter with collection-level CAS revisions."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = initialize_application_database(database_url)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._ensure_revision_rows()

    @staticmethod
    def _spec(collection: StorageCollection) -> tuple[type[Any], str]:
        if collection == "accounts":
            return AccountModel, "access_token"
        return AuthKeyModel, "key_id"

    @staticmethod
    def _serialize(item: dict[str, Any]) -> str:
        return json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _deserialize(value: str) -> dict[str, Any] | None:
        try:
            item = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
        return item if isinstance(item, dict) else None

    def _ensure_revision_rows(self) -> None:
        for collection in ("accounts", "auth_keys"):
            session = self.Session()
            try:
                if session.get(StorageRevisionModel, collection) is None:
                    session.add(StorageRevisionModel(collection=collection, version=0))
                    session.commit()
            except IntegrityError:
                # Another process initialized the same row first.
                session.rollback()
            finally:
                session.close()

    def _begin_write(self, session: Any) -> None:
        if self.engine.dialect.name == "sqlite":
            # SQLite ignores SELECT FOR UPDATE. BEGIN IMMEDIATE serializes writers
            # before the revision is checked, preserving CAS semantics.
            session.execute(text("BEGIN IMMEDIATE"))

    @staticmethod
    def _check_revision(
        collection: StorageCollection,
        expected_revision: str | None,
        actual_revision: str,
    ) -> None:
        if expected_revision is not None and expected_revision != actual_revision:
            raise StorageRevisionConflictError(
                collection,
                expected_revision,
                actual_revision,
            )

    @staticmethod
    def _revision_value(collection: StorageCollection, version: int) -> str:
        return f"{collection}:{version}"

    def _locked_revision(self, session: Any, collection: StorageCollection) -> Any:
        return (
            session.query(StorageRevisionModel)
            .filter(StorageRevisionModel.collection == collection)
            .with_for_update()
            .one()
        )

    def _load_snapshot(self, collection: StorageCollection) -> StorageSnapshot:
        model, _ = self._spec(collection)
        # PostgreSQL READ COMMITTED gives each statement a new snapshot, not
        # each Session. Separate revision/rows/revision reads can therefore
        # starve under continuous quota writes or imports. One SELECT observes
        # the rows and their CAS revision together on PostgreSQL and SQLite,
        # without locking writers or requiring a quiet interval. The left join
        # preserves the revision even when the collection is empty.
        statement = (
            select(StorageRevisionModel.version, model.data)
            .select_from(StorageRevisionModel)
            .outerjoin(model, true())
            .where(StorageRevisionModel.collection == collection)
            .order_by(model.id.asc())
        )
        with self.Session() as session:
            rows = session.execute(statement).all()
        if not rows:
            raise NoResultFound(f"missing {collection} storage revision")
        # Return the connection before decoding a potentially large collection.
        # Never attach a subsequently fetched revision to these older payloads.
        return StorageSnapshot(
            items=[
                item
                for _version, data in rows
                if data is not None and (item := self._deserialize(data)) is not None
            ],
            revision=self._revision_value(collection, rows[0][0]),
        )

    def load_accounts_snapshot(self) -> StorageSnapshot:
        return self._load_snapshot("accounts")

    def get_collection_revision(self, collection: StorageCollection) -> str:
        """Read a collection revision without loading and decoding its rows."""
        session = self.Session()
        try:
            version = session.execute(
                select(StorageRevisionModel.version).where(
                    StorageRevisionModel.collection == collection
                )
            ).scalar_one()
            return self._revision_value(collection, version)
        finally:
            session.close()

    def load_auth_keys_snapshot(self) -> StorageSnapshot:
        return self._load_snapshot("auth_keys")

    def _replace(
        self,
        collection: StorageCollection,
        items: Sequence[dict[str, Any]],
        *,
        expected_revision: str | None,
    ) -> StorageMutationResult:
        desired_items = normalize_items(collection, items)
        desired = {item_key(collection, item): item for item in desired_items}
        model, model_key = self._spec(collection)

        session = self.Session()
        try:
            self._begin_write(session)
            revision_row = self._locked_revision(session, collection)
            current_revision = self._revision_value(collection, revision_row.version)
            self._check_revision(collection, expected_revision, current_revision)

            rows = session.query(model).order_by(model.id.asc()).all()
            existing = {str(getattr(row, model_key)): row for row in rows}
            inserted = 0
            updated = 0
            deleted = 0

            for key in existing.keys() - desired.keys():
                session.delete(existing[key])
                deleted += 1

            for key, item in desired.items():
                serialized = self._serialize(item)
                row = existing.get(key)
                if row is None:
                    session.add(model(**{model_key: key}, data=serialized))
                    inserted += 1
                elif self._deserialize(row.data) != item:
                    row.data = serialized
                    updated += 1

            if inserted or updated or deleted:
                revision_row.version += 1
            session.commit()
            return StorageMutationResult(
                revision=self._revision_value(collection, revision_row.version),
                inserted=inserted,
                updated=updated,
                deleted=deleted,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def replace_accounts(
        self,
        accounts: Sequence[dict[str, Any]],
        *,
        expected_revision: str | None = None,
    ) -> StorageMutationResult:
        return self._replace(
            "accounts",
            accounts,
            expected_revision=expected_revision,
        )

    def replace_auth_keys(
        self,
        auth_keys: Sequence[dict[str, Any]],
        *,
        expected_revision: str | None = None,
    ) -> StorageMutationResult:
        return self._replace(
            "auth_keys",
            auth_keys,
            expected_revision=expected_revision,
        )

    def _mutate(
        self,
        collection: StorageCollection,
        mutation: StorageMutation,
        *,
        expected_items: Mapping[str, dict[str, Any] | None] | None = None,
    ) -> StorageMutationResult:
        upserts, delete_keys = normalize_mutation(collection, mutation)
        model, model_key = self._spec(collection)
        key_column = getattr(model, model_key)
        target_keys = {
            *(item_key(collection, item) for item in upserts),
            *delete_keys,
        }
        if expected_items is not None:
            if set(expected_items) != target_keys:
                raise ValueError("checked mutation requires a baseline for every affected key")
            for key, item in expected_items.items():
                if item is not None and item_key(collection, item) != key:
                    raise ValueError("checked mutation baseline identity mismatch")

        session = self.Session()
        try:
            self._begin_write(session)
            revision_row = self._locked_revision(session, collection)
            current_revision = self._revision_value(collection, revision_row.version)
            if expected_items is None:
                self._check_revision(
                    collection,
                    mutation.expected_revision,
                    current_revision,
                )
            hook_context = _account_commit_hook.get() if collection == "accounts" else None
            hook = hook_context[1] if hook_context and hook_context[0] is self else None
            if hook is not None:
                hook(session, "before", mutation, None)
            rows = (
                session.query(model).filter(key_column.in_(target_keys)).all()
                if target_keys
                else []
            )
            existing = {str(getattr(row, model_key)): row for row in rows}
            if expected_items is not None:
                # Guard the actual rows being changed, under the same writer
                # transaction/lock as every other mutation. An unrelated quota
                # update/import must not invalidate a credential save. Missing
                # keys are explicit expectations too, protecting token rotation
                # from clobbering an existing destination or resurrecting a
                # remotely deleted account.
                for key, expected in expected_items.items():
                    row = existing.get(key)
                    matches = (
                        row is None if expected is None
                        else row is not None and self._deserialize(row.data) == expected
                    )
                    if not matches:
                        raise StorageRevisionConflictError(
                            collection, mutation.expected_revision or "checked rows", current_revision,
                        )
            inserted_keys = []
            inserted = 0
            updated = 0
            deleted = 0

            for key in delete_keys:
                row = existing.get(key)
                if row is not None:
                    session.delete(row)
                    deleted += 1

            insert_rows = []
            for item in upserts:
                key = item_key(collection, item)
                serialized = self._serialize(item)
                row = existing.get(key)
                if row is None:
                    insert_rows.append({model_key: key, "data": serialized})
                    inserted += 1
                    inserted_keys.append(key)
                elif self._deserialize(row.data) != item:
                    row.data = serialized
                    updated += 1

            # No generated ORM identity is needed here. Use executemany instead
            # of one INSERT ... RETURNING per account, within the same CAS lock
            # and transaction as the import checkpoint.
            if insert_rows:
                session.execute(model.__table__.insert(), insert_rows)

            if inserted or updated or deleted:
                revision_row.version += 1
            if hook is not None:
                hook(session, "after", mutation, {"inserted": inserted, "updated": updated, "deleted": deleted, "inserted_keys": inserted_keys})
            session.commit()
            return StorageMutationResult(
                revision=self._revision_value(collection, revision_row.version),
                inserted=inserted,
                updated=updated,
                deleted=deleted,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mutate_accounts(self, mutation: StorageMutation) -> StorageMutationResult:
        return self._mutate("accounts", mutation)

    def mutate_accounts_checked(
        self,
        mutation: StorageMutation,
        *,
        expected_items: Mapping[str, dict[str, Any] | None],
    ) -> StorageMutationResult:
        """CAS on affected account rows instead of the whole collection revision.

        The returned revision is a write receipt, NOT a full account snapshot.
        Callers must not advance a cached collection revision from this result.
        Unconditional writes and full-collection CAS retain their old semantics.
        """
        return self._mutate("accounts", mutation, expected_items=expected_items)

    def mutate_auth_keys(self, mutation: StorageMutation) -> StorageMutationResult:
        return self._mutate("auth_keys", mutation)

    def _db_type(self) -> str:
        dialect = self.engine.dialect.name
        return "postgresql" if dialect == "postgres" else dialect

    def get_capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            atomic_mutations=True,
            compare_and_swap=True,
            cross_process_safe=True,
            distributed_safe=self.engine.dialect.name != "sqlite",
            transactional=True,
        )

    def health_check(self) -> dict[str, Any]:
        try:
            session = self.Session()
            try:
                session.execute(text("SELECT 1"))
                count = session.query(AccountModel).count()
                auth_key_count = session.query(AuthKeyModel).count()
                return {
                    "status": "healthy",
                    "backend": "database",
                    "database_url": display_database_url(self.database_url),
                    "account_count": count,
                    "auth_key_count": auth_key_count,
                }
            finally:
                session.close()
        except Exception as exc:
            return {
                "status": "unhealthy",
                "backend": "database",
                "error": str(exc),
            }

    def get_backend_info(self) -> dict[str, Any]:
        db_type = self._db_type()
        return {
            "type": "database",
            "db_type": db_type,
            "description": f"应用数据库（{db_type}）",
            "database_url": display_database_url(self.database_url),
        }
