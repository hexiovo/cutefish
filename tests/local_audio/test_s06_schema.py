from __future__ import annotations

import sqlite3

import pytest

from cutefish.core.db import CURRENT_SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_v4_database_additively_upgrades_to_local_voice_candidate_v5(tmp_path):
    database_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_version (id, version, updated_at) VALUES (1, 4, '2026-01-01T00:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(database_path)
    await database.initialize()
    try:
        async with database.engine.connect() as connection:
            version = await connection.exec_driver_sql("SELECT version FROM schema_version WHERE id = 1")
            assert version.scalar_one() == CURRENT_SCHEMA_VERSION == 5
            candidate_columns = await connection.exec_driver_sql('PRAGMA table_info("local_voice_candidate")')
            assert {row[1] for row in candidate_columns.fetchall()} >= {
                "id",
                "managed_media_id",
                "reference_relative_path",
                "reference_sha256",
                "ref_text",
                "style_prompt",
                "seed",
                "design_revision",
                "base_revision",
                "design_resource_fingerprint",
                "base_resource_fingerprint",
                "status",
            }
            foreign_keys = await connection.exec_driver_sql('PRAGMA foreign_key_list("local_voice_candidate")')
            assert any(row[2] == "extension_managed_media" for row in foreign_keys.fetchall())
    finally:
        await database.close()
