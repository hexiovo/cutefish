"""Async SQLite database setup with explicit durability and schema versioning."""

from __future__ import annotations

import importlib
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .models import Base, utc_now


CURRENT_SCHEMA_VERSION = 4
_SUPPORTED_LEGACY_SCHEMA_VERSIONS = frozenset({1, 2, 3})
_DRAFT_V2_COLUMNS = {
    # S02 is still being assembled. These nullable/defaulted additions let a
    # local database created by an earlier S02 draft move forward in place.
    "budget_entry": (
        ("provider_request_id", "VARCHAR(256)"),
        ("max_input_tokens", "INTEGER"),
        ("max_output_tokens", "INTEGER"),
        ("input_price_per_million", "NUMERIC(14, 6)"),
        ("output_price_per_million", "NUMERIC(14, 6)"),
        ("settlement_evidence_ref", "VARCHAR(512)"),
    ),
    "action_authorization": (("delivery_mode", "VARCHAR(32)"), ("batch_version", "INTEGER")),
    "outbox_item": (
        ("delivery_mode", "VARCHAR(32) NOT NULL DEFAULT 'qq'"),
        ("batch_version", "INTEGER NOT NULL DEFAULT 1"),
        ("group_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
        ("attempt_id", "VARCHAR(36)"),
        ("dispatch_evidence_ref", "VARCHAR(512)"),
        ("message_type", "VARCHAR(16) NOT NULL DEFAULT 'text'"),
        ("record_ref", "JSON"),
    ),
    "chat_request": (("source_revision", "INTEGER NOT NULL DEFAULT 0"),),
    "qq_account": (("webhook_secret_name", "VARCHAR(128) NOT NULL DEFAULT 'legacy_unconfigured'"),),
    # Earlier S02 databases can already contain approval rows created before
    # same-version interview evidence was recorded. Keep the additive columns
    # nullable for those rows; the approval service validates them for every
    # new approval.
    "persona_approval": (
        ("inbound_message_id", "VARCHAR(36)"),
        ("reply_message_id", "VARCHAR(36)"),
        ("config_fingerprint", "VARCHAR(64)"),
    ),
}
MODEL_MODULES = (
    "cutefish.chat.models",
    "cutefish.memory.models",
    "cutefish.qq.models",
    "cutefish.extensions.models",
    "cutefish.proactivity.models",
)


class Database:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        url = URL.create("sqlite+aiosqlite", database=str(database_path))
        self.engine: AsyncEngine = create_async_engine(url, connect_args={"timeout": 15})
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        event.listen(self.engine.sync_engine, "connect", self._configure_connection)

    @staticmethod
    def _configure_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    @staticmethod
    def _register_module_models() -> None:
        for module_name in MODEL_MODULES:
            try:
                importlib.import_module(module_name)
            except ModuleNotFoundError as exc:
                if exc.name == module_name or module_name.startswith(f"{exc.name}."):
                    continue
                raise

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._register_module_models()
        async with self.immediate_transaction() as connection:
            tables = {
                row[0]
                for row in (
                    await connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                ).all()
            }
            version = None
            if "schema_version" in tables:
                result = await connection.exec_driver_sql(
                    "SELECT version FROM schema_version WHERE id = 1"
                )
                version_row = result.first()
                if version_row is not None:
                    version = int(version_row[0])
            elif tables:
                raise RuntimeError("数据库没有schema版本记录；拒绝猜测旧结构并继续初始化")

            if version not in {None, *_SUPPORTED_LEGACY_SCHEMA_VERSIONS, CURRENT_SCHEMA_VERSION}:
                raise RuntimeError(
                    f"数据库版本为{version}，应用需要版本{CURRENT_SCHEMA_VERSION}；需要显式迁移"
                )
            if version is None and tables:
                raise RuntimeError("数据库版本记录缺失；拒绝将已有数据误标为新结构")

            # Version validation, additive table creation, draft-v2 column
            # compatibility, and the version stamp share one BEGIN IMMEDIATE.
            # A v1 stamp is written only after every additive operation succeeds.
            await connection.run_sync(Base.metadata.create_all)
            await self._ensure_draft_v2_columns(connection)
            await self._ensure_qq_webhook_secret_names(connection)
            from cutefish.extensions import ensure_compatible_schema

            await ensure_compatible_schema(connection)

            now = utc_now().isoformat()
            if version is None:
                await connection.exec_driver_sql(
                    "INSERT INTO schema_version (id, version, updated_at) VALUES (?, ?, ?)",
                    (1, CURRENT_SCHEMA_VERSION, now),
                )
            elif version in _SUPPORTED_LEGACY_SCHEMA_VERSIONS:
                await connection.exec_driver_sql(
                    "UPDATE schema_version SET version = ?, updated_at = ? WHERE id = 1",
                    (CURRENT_SCHEMA_VERSION, now),
                )

    @staticmethod
    async def _ensure_draft_v2_columns(connection) -> None:
        for table_name, required_columns in _DRAFT_V2_COLUMNS.items():
            result = await connection.exec_driver_sql(f'PRAGMA table_info("{table_name}")')
            existing = {row[1] for row in result.fetchall()}
            for column_name, definition in required_columns:
                if column_name not in existing:
                    await connection.exec_driver_sql(
                        f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}'
                    )
                    existing.add(column_name)
            if table_name == "outbox_item" and "group_id" in existing:
                await connection.exec_driver_sql(
                    'UPDATE "outbox_item" SET "group_id" = "id" WHERE "group_id" = \'\''
                )
                await connection.exec_driver_sql(
                    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_outbox_group_segment_slot" '
                    'ON "outbox_item" ("conversation_id", "batch_id", "group_id", "segment_index")'
                )

    @staticmethod
    async def _ensure_qq_webhook_secret_names(connection) -> None:
        result = await connection.exec_driver_sql(
            'SELECT "account_id" FROM "qq_account" '
            'WHERE "webhook_secret_name" IS NULL OR "webhook_secret_name" = ?',
            ("legacy_unconfigured",),
        )
        for (account_id,) in result.fetchall():
            suffix = uuid.uuid5(uuid.NAMESPACE_URL, f"cutefish-qq-webhook:{account_id}").hex
            await connection.exec_driver_sql(
                'UPDATE "qq_account" SET "webhook_secret_name" = ? WHERE "account_id" = ?',
                (f"qq_webhook_legacy_{suffix}", account_id),
            )

        index_result = await connection.exec_driver_sql('PRAGMA index_list("qq_account")')
        for index_row in index_result.fetchall():
            if not bool(index_row[2]):
                continue
            index_name = str(index_row[1]).replace('"', '""')
            info = await connection.exec_driver_sql(f'PRAGMA index_info("{index_name}")')
            if [row[2] for row in info.fetchall()] == ["webhook_secret_name"]:
                return
        await connection.exec_driver_sql(
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_qq_account_webhook_secret_name" '
            'ON "qq_account" ("webhook_secret_name")'
        )

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def immediate_transaction(self):
        """Hold SQLite's write reservation only for the caller's short DB work."""
        connection = await self.engine.connect()
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.close()

