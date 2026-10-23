"""Additive SQLite compatibility hooks for extension-owned schema changes."""

from __future__ import annotations


async def ensure_compatible_schema(connection) -> None:
    """Add nullable/defaulted extension columns missing from earlier local drafts.

    New databases receive the complete schema from SQLAlchemy. Older extension
    tables are extended in place; unverifiable legacy media remains unusable
    until an owner explicitly registers it again.
    """
    await _add_columns(
        connection,
        "extension_room_item",
        {
            "content_text": "TEXT",
            "source_person_id": "VARCHAR(128)",
            "role_owned": "BOOLEAN NOT NULL DEFAULT 0",
        },
    )
    await _add_columns(
        connection,
        "extension_voice_profile_version",
        {"engine_config_json": "TEXT"},
    )
    await _add_columns(
        connection,
        "extension_managed_media",
        {
            "sha256": "VARCHAR(64)",
            "mime_type": "VARCHAR(128)",
            "byte_length": "INTEGER",
            "source_refs": "JSON NOT NULL DEFAULT '[]'",
            "source_scope": "VARCHAR(32) NOT NULL DEFAULT 'unknown'",
            "source_person_id": "VARCHAR(128)",
            "role_use_approved": "BOOLEAN NOT NULL DEFAULT 0",
            "share_allowed": "BOOLEAN NOT NULL DEFAULT 0",
            "registered_by": "VARCHAR(128) NOT NULL DEFAULT ''",
            "version": "INTEGER NOT NULL DEFAULT 1",
            "asset_class": "VARCHAR(32) NOT NULL DEFAULT 'room'",
            "generation_request_id": "VARCHAR(36)",
            "profile_id": "VARCHAR(128)",
            "profile_version": "INTEGER",
            "profile_settings_hash": "VARCHAR(64)",
            "approval_ref": "VARCHAR(36)",
            "artifact_purpose": "VARCHAR(32)",
            "artifact_state": "VARCHAR(24)",
            "duration_ms": "INTEGER",
            "conversation_id": "VARCHAR(36)",
            "account_id": "VARCHAR(128)",
            "recipient_id": "VARCHAR(128)",
            "batch_id": "VARCHAR(36)",
            "batch_version": "INTEGER",
            "source_batch_id": "VARCHAR(36)",
            "source_message_id": "VARCHAR(36)",
            "source_revision": "INTEGER",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        },
    )
    await _copy_legacy_media_fields(connection)
    await _ensure_voice_asset_indexes(connection)


async def _add_columns(connection, table: str, columns: dict[str, str]) -> None:
    result = await connection.exec_driver_sql(f'PRAGMA table_info("{table}")')
    existing_rows = result.fetchall()
    if not existing_rows:
        return
    existing = {row[1] for row in existing_rows}
    for name, declaration in columns.items():
        if name not in existing:
            await connection.exec_driver_sql(
                f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}'
            )


async def _copy_legacy_media_fields(connection) -> None:
    result = await connection.exec_driver_sql('PRAGMA table_info("extension_managed_media")')
    columns = {row[1] for row in result.fetchall()}
    if not columns:
        return
    if "media_type" in columns:
        await connection.exec_driver_sql(
            'UPDATE "extension_managed_media" SET "mime_type" = "media_type" '
            'WHERE "mime_type" IS NULL'
        )
    if "size_bytes" in columns:
        await connection.exec_driver_sql(
            'UPDATE "extension_managed_media" SET "byte_length" = "size_bytes" '
            'WHERE "byte_length" IS NULL'
        )


async def _ensure_voice_asset_indexes(connection) -> None:
    result = await connection.exec_driver_sql('PRAGMA table_info("extension_managed_media")')
    columns = {row[1] for row in result.fetchall()}
    if {"generation_request_id"} <= columns:
        await connection.exec_driver_sql(
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_extension_media_generation_request" '
            'ON "extension_managed_media" ("generation_request_id")'
        )
