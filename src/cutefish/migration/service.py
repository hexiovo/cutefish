"""Conservative, offline migration snapshot and staging-import pipeline.

The export path never edits the live database. SQLite's Online Backup API
copies a consistent view into memory; sanitization and VACUUM happen only on
that isolated copy before a database file is written to the bundle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from cutefish import __version__
from cutefish.core.db import CURRENT_SCHEMA_VERSION, Database
from cutefish.core.models import Base
from cutefish.core.paths import HostPathResolver, PathPolicyError
from cutefish.extensions.media_manifest import hash_stream, validate_relative_path

from .contracts import (
    BundleResult,
    EXCLUDED_CATEGORIES,
    EXCLUDED_TABLES,
    ImportResult,
    ManifestDatabase,
    ManifestFile,
    ManifestLogicalRoot,
    MigrationManifest,
    ManifestSource,
    Preflight,
    RestorePolicy,
    ROOT_KEYS,
    TargetStagingPolicy,
    TRANSFER_ROOT_KEYS,
    ValidationResult,
)


MIGRATION_RUNTIME_KEY = "migration_runtime"
MIGRATION_POLICY_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
MANIFEST_HASH_FILENAME = "manifest.sha256"
DATABASE_FILENAME = "cutefish.sqlite3"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_DATABASE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEDIA_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEDIA_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_MEDIA_FILES = 256
_ALLOWED_INTERNAL_TABLES = frozenset({"sqlite_sequence", "sqlite_stat1", "sqlite_stat4"})
_SENSITIVE_WORDS = re.compile(r"(^|_)(password|passwd|secret|credential|cookie|token|private_key|api_key)(_|$)", re.I)
_JSON_SECRET_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "passwd",
        "api_key",
        "access_token",
        "refresh_token",
        "auth_token",
        "token",
        "cookie",
        "cookies",
        "secret",
        "secret_value",
        "encrypted_value",
        "private_key",
    }
)
_JSON_REFERENCE_KEYS = frozenset(
    {"credential_ref", "credential_reference", "credential_id", "secret_ref", "secret_name", "secret_id"}
)
_JSON_PATH_KEYS = frozenset(
    {
        "path",
        "root_path",
        "directory",
        "dir",
        "file_path",
        "source_path",
        "base_dir",
        "backup_path",
        "temp_path",
        "data_path",
        "gallery_path",
        "room_path",
        "model_path",
        "cache_path",
    }
)
_TOKEN_USAGE_INTEGER_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "total_tokens",
        "dropped_history_messages",
        "prompt_utf8_bytes",
    }
)
_TOKEN_USAGE_NULLABLE_INTEGER_FIELDS = frozenset({"cache_hit_tokens", "cache_miss_tokens"})
_TOKEN_USAGE_TEXT_FIELDS = {
    "cost_basis": frozenset({"official_peak_rates"}),
    "currency": frozenset({"CNY"}),
    "budget_state": frozenset({"settled", "unknown"}),
    "turn_state": frozenset({"current", "stale"}),
}
_TOKEN_USAGE_METADATA_FIELDS = frozenset(
    {*_TOKEN_USAGE_INTEGER_FIELDS, "actual_cost", "reservation_id", *_TOKEN_USAGE_TEXT_FIELDS}
)


class MigrationError(Exception):
    """A migration operation refused unsafe or unsupported input."""

    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class MigrationPolicy:
    """Caller-supplied host bindings and bounded resource limits.

    `paths` is the host-local resolver. It is deliberately not serialized into
    the bundle; only root names and relative file locations cross hosts.
    """

    paths: HostPathResolver | None = None
    source_host_id: str | None = None
    app_version: str = __version__
    expected_schema_version: int = CURRENT_SCHEMA_VERSION
    max_database_bytes: int = MAX_DATABASE_BYTES
    max_media_file_bytes: int = MAX_MEDIA_FILE_BYTES
    max_media_total_bytes: int = MAX_MEDIA_TOTAL_BYTES
    max_media_files: int = MAX_MEDIA_FILES


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _effective_profile(ctx: Any) -> str:
    return str(getattr(ctx, "deployment_profile", getattr(ctx.settings, "mode", "local_trial")))


def _resolver(policy: MigrationPolicy | None) -> HostPathResolver:
    if policy is None or policy.paths is None:
        raise MigrationError("host_roots_required", "迁移操作必须使用已登记的逻辑根目录。", 422)
    return policy.paths


def _metadata_tables() -> dict[str, set[str]]:
    Database._register_module_models()
    return {name: set(table.columns.keys()) for name, table in Base.metadata.tables.items()}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    if not database_path.is_file() or _is_link_or_junction(database_path):
        raise MigrationError("source_database_unavailable", "源数据库不存在或不是普通文件。", 404)
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _schema_errors(connection: sqlite3.Connection, expected_version: int) -> tuple[list[str], int | None, int]:
    errors: list[str] = []
    expected_tables = _metadata_tables()
    rows = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    user_tables = {
        str(row["name"])
        for row in rows
        if row["type"] == "table"
        and not str(row["name"]).startswith("sqlite_")
        and row["name"] not in _ALLOWED_INTERNAL_TABLES
    }
    missing = sorted(set(expected_tables) - user_tables)
    unexpected = sorted(user_tables - set(expected_tables))
    if missing:
        errors.append("数据库缺少当前版本要求的表。")
    if unexpected:
        errors.append("数据库包含未识别的表。")
    if any(row["type"] in {"view", "trigger"} for row in rows):
        errors.append("数据库包含未识别的视图或触发器。")
    for row in rows:
        name = str(row["name"])
        if not name.startswith("sqlite_"):
            continue
        if row["type"] == "table" and name in _ALLOWED_INTERNAL_TABLES:
            continue
        if row["type"] == "index" and name.startswith("sqlite_autoindex_") and row["tbl_name"] in expected_tables:
            continue
        if name.startswith("sqlite_"):
            errors.append("数据库包含未允许的 SQLite 内部对象。")

    for table_name, expected_columns in expected_tables.items():
        if table_name not in user_tables:
            continue
        info = connection.execute(f"PRAGMA table_xinfo({_quote(table_name)})").fetchall()
        actual_columns = {str(item["name"]) for item in info if int(item["hidden"]) == 0}
        if actual_columns != expected_columns:
            errors.append("数据库表结构与当前 CuteFish schema 不一致。")
            continue
        for column in actual_columns:
            if _SENSITIVE_WORDS.search(column) and not (
                (table_name == "chat_message" and column == "token_usage_json")
                or table_name in EXCLUDED_TABLES
                or (table_name == "qq_account" and column in {"secret_name", "webhook_secret_name"})
            ):
                errors.append("发现未识别的敏感字段，拒绝迁移。")
                break

        expected_indexes = {
            index.name for index in Base.metadata.tables[table_name].indexes if index.name
        }
        for index_row in connection.execute(f"PRAGMA index_list({_quote(table_name)})").fetchall():
            index_name = str(index_row["name"])
            if not index_name.startswith("sqlite_autoindex_") and index_name not in expected_indexes:
                errors.append("数据库包含未识别的索引结构。")
                break

    schema_version: int | None = None
    if "schema_version" in user_tables:
        try:
            version_row = connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
            if version_row is not None:
                schema_version = int(version_row["version"])
        except (sqlite3.Error, TypeError, ValueError):
            errors.append("数据库 schema 版本记录无效。")
    if schema_version != expected_version:
        errors.append("数据库 schema 版本与当前应用不匹配。")
    return errors, schema_version, len(user_tables)


def _count(connection: sqlite3.Connection, table: str, where: str = "") -> int:
    if table not in _metadata_tables():
        return 0
    sql = f"SELECT COUNT(*) AS count FROM {_quote(table)}"
    if where:
        sql += f" WHERE {where}"
    return int(connection.execute(sql).fetchone()["count"])


def _inspect_source_sync(ctx: Any) -> Preflight:
    database_path = Path(ctx.settings.database_path)
    if not database_path.is_file():
        return Preflight(
            available=False,
            schema_version=None,
            schema_supported=False,
            table_count=0,
            registered_media_count=0,
            active_batch_count=0,
            pending_outbox_count=0,
            sending_outbox_count=0,
            errors=("源数据库不存在。",),
        )
    try:
        connection = _read_only_connection(database_path)
    except MigrationError as exc:
        return Preflight(
            available=False,
            schema_version=None,
            schema_supported=False,
            table_count=0,
            registered_media_count=0,
            active_batch_count=0,
            pending_outbox_count=0,
            sending_outbox_count=0,
            errors=(exc.message,),
        )
    try:
        errors, version, table_count = _schema_errors(connection, CURRENT_SCHEMA_VERSION)
        tables = set(_metadata_tables())
        media_count = _count(connection, "extension_managed_media") if "extension_managed_media" in tables else 0
        active_batches = _count(connection, "trial_batch", "state = 'running'")
        pending = _count(connection, "outbox_item", "state IN ('prepared', 'sending', 'unknown')")
        sending = _count(connection, "outbox_item", "state = 'sending'")
        warnings = []
        if active_batches:
            warnings.append("存在运行中的试用批次；导出快照不会停止源主机。")
        if sending:
            warnings.append("存在派发中的 Outbox 项；快照会保守转为 unknown，源主机仍需另行停止。")
        if _effective_profile(ctx) == "target_staging":
            warnings.append("当前数据已处于 target_staging；不会作为源主机导出。")
            errors.append("target_staging 数据不能作为 local_trial 源主机导出。")
        return Preflight(
            available=True,
            schema_version=version,
            schema_supported=not errors,
            table_count=table_count,
            registered_media_count=media_count,
            active_batch_count=active_batches,
            pending_outbox_count=pending,
            sending_outbox_count=sending,
            errors=tuple(dict.fromkeys(errors)),
            warnings=tuple(warnings),
        )
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        return Preflight(
            available=True,
            schema_version=None,
            schema_supported=False,
            table_count=0,
            registered_media_count=0,
            active_batch_count=0,
            pending_outbox_count=0,
            sending_outbox_count=0,
            errors=("无法只读检查源数据库结构。",),
        )
    finally:
        connection.close()


async def inspect_source(ctx: Any) -> Preflight:
    """Check schema and persisted runtime state without walking host folders."""

    return await asyncio.to_thread(_inspect_source_sync, ctx)


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction and is_junction(path):
        return True
    if os.name == "nt":
        try:
            return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
        except OSError:
            return False
    return False


def _assert_no_link_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _is_link_or_junction(current):
            raise MigrationError("linked_path_rejected", "迁移路径包含符号链接或目录联接。", 422)


def _assert_under_root(path: Path, root_key: str, policy: MigrationPolicy | None) -> Path:
    resolver = _resolver(policy)
    root = resolver.root_path(root_key)
    candidate = Path(path)
    if not candidate.is_absolute():
        raise MigrationError("absolute_path_required", "内部迁移路径必须由逻辑根目录解析。", 422)
    _assert_no_link_components(root)
    _assert_no_link_components(candidate)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise MigrationError("path_outside_root", "迁移路径超出允许的逻辑根目录。", 422) from exc
    return candidate


def _safe_relative_path(raw: str) -> str:
    try:
        return validate_relative_path(raw)
    except (TypeError, ValueError) as exc:
        raise MigrationError("invalid_relative_path", "迁移清单包含越界或无效的相对路径。", 422) from exc


def _looks_absolute_path(value: str) -> bool:
    if value.startswith(("\\\\", "//")):
        return True
    win = PureWindowsPath(value)
    posix = PurePosixPath(value)
    return win.is_absolute() or bool(win.drive) or posix.is_absolute()


def _scrub_json(value: Any, *, current_key: str = "") -> Any:
    """Reject unknown secret-like data and replace known machine references."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            normalized = key.casefold()
            if normalized in _JSON_SECRET_KEYS:
                raise MigrationError("unknown_secret_field", "发现未识别的敏感 JSON 字段，拒绝导出。", 422)
            if normalized in _JSON_REFERENCE_KEYS:
                result[key] = "needs_reconfiguration"
            elif normalized in _JSON_PATH_KEYS:
                result[key] = "needs_configuration" if raw_value is not None else None
            else:
                result[key] = _scrub_json(raw_value, current_key=normalized)
        return result
    if isinstance(value, list):
        return [_scrub_json(item, current_key=current_key) for item in value]
    if isinstance(value, str) and _looks_absolute_path(value):
        if current_key in _JSON_PATH_KEYS:
            return "needs_configuration"
        raise MigrationError("source_path_in_config", "发现未映射的源机绝对路径，拒绝导出。", 422)
    return value


def _is_sensitive_json_key(key: str) -> bool:
    if key.casefold() in _JSON_REFERENCE_KEYS:
        return False
    return bool(re.search(r"password|passwd|secret|credential|cookie|token|private.?key|api.?key", key, re.I))


def _validate_token_usage_json(raw: Any) -> Any:
    """Validate known accounting metadata and fail closed on nested credentials."""

    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise MigrationError("invalid_json_field", "数据库中的用量 JSON 无法安全识别。", 422) from exc
    if not isinstance(value, dict):
        raise MigrationError("invalid_json_field", "数据库中的用量 JSON 结构无效。", 422)

    def validate_sensitive_keys(item: Any, *, root: bool = False) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or (
                    _is_sensitive_json_key(key)
                    and not (root and key in _TOKEN_USAGE_METADATA_FIELDS)
                ):
                    raise MigrationError("unknown_secret_field", "发现未识别的敏感 JSON 字段，拒绝导出。", 422)
                validate_sensitive_keys(child)
        elif isinstance(item, list):
            for child in item:
                validate_sensitive_keys(child)

    validate_sensitive_keys(value, root=True)
    for key, field_value in value.items():
        if key in _TOKEN_USAGE_INTEGER_FIELDS:
            nullable = key in _TOKEN_USAGE_NULLABLE_INTEGER_FIELDS
            if (field_value is None and nullable) or (type(field_value) is int and field_value >= 0):
                continue
            raise MigrationError("invalid_usage_metadata", "数据库中的用量数值字段无效。", 422)
        if key == "actual_cost":
            try:
                amount = Decimal(field_value) if isinstance(field_value, str) else Decimal("NaN")
            except (InvalidOperation, ValueError):
                amount = Decimal("NaN")
            if not amount.is_finite() or amount < 0 or amount > Decimal("99999999.999999"):
                raise MigrationError("invalid_usage_metadata", "数据库中的用量金额字段无效。", 422)
            continue
        if key == "reservation_id":
            if not isinstance(field_value, str) or not field_value or len(field_value) > 128:
                raise MigrationError("invalid_usage_metadata", "数据库中的费用预留引用无效。", 422)
            continue
        if key in _TOKEN_USAGE_TEXT_FIELDS:
            if not isinstance(field_value, str) or field_value not in _TOKEN_USAGE_TEXT_FIELDS[key]:
                raise MigrationError("invalid_usage_metadata", "数据库中的用量状态字段无效。", 422)
        if key not in _TOKEN_USAGE_METADATA_FIELDS:
            # Unknown harmless metadata may be retained after generic path scrubbing;
            # nested token/secret-like keys were rejected recursively above.
            continue
    return value


def _has_sensitive_json(connection: sqlite3.Connection) -> None:
    for table, column in (("app_setting", "value"), ("module_configuration", "value"), ("feature_state", "evidence_refs"), ("extension_candidate", "context"), ("qq_inbound_event", "payload_json"), ("extension_room_item", "source_refs"), ("extension_managed_media", "source_refs"), ("chat_message", "token_usage_json")):
        if table not in _metadata_tables():
            continue
        quoted_table, quoted_column = _quote(table), _quote(column)
        for row in connection.execute(f"SELECT {quoted_column} FROM {quoted_table}").fetchall():
            raw = row[0]
            if raw is None:
                continue
            try:
                if table == "chat_message" and column == "token_usage_json":
                    parsed = _validate_token_usage_json(raw)
                else:
                    parsed = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
                _scrub_json(parsed)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
                raise MigrationError("invalid_json_field", "数据库中的结构化 JSON 字段无法安全识别。", 422) from exc


def _safe_manifest_roots(paths: HostPathResolver) -> tuple[ManifestLogicalRoot, ...]:
    statuses = {str(item["root_key"]): item for item in paths.status()}
    return tuple(
        ManifestLogicalRoot(root_key=key, mapped=bool(statuses.get(key, {}).get("configured", False)))
        for key in ROOT_KEYS
    )


def _runtime_marker() -> dict[str, Any]:
    return {
        "profile": "target_staging",
        "policy_version": MIGRATION_POLICY_VERSION,
        "external_actions_enabled": False,
        "outbox_dispatch_enabled": False,
        "paid_provider_calls_enabled": False,
        "scheduling_enabled": False,
        "auto_start_enabled": False,
        "task_auto_resume": False,
        "credentials_state": "reconfigure_on_target",
        "requires_host_bindings": True,
    }


def _set_app_setting(connection: sqlite3.Connection, key: str, value: Mapping[str, Any], now: str) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row = connection.execute("SELECT version FROM app_setting WHERE key = ?", (key,)).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO app_setting (key, value, version, updated_at) VALUES (?, ?, ?, ?)",
            (key, encoded, 1, now),
        )
    else:
        connection.execute(
            "UPDATE app_setting SET value = ?, version = ?, updated_at = ? WHERE key = ?",
            (encoded, int(row["version"]) + 1, now, key),
        )


def _hold_snapshot(connection: sqlite3.Connection) -> None:
    """Persist staging and invalidate all one-run grants in the copy."""

    now = _utc_now_text()
    for table in (
        "secret_record",
        "auth_session",
        "admin_account",
        "action_authorization",
        "tool_action_grant",
    ):
        connection.execute(f"DELETE FROM {_quote(table)}")

    connection.execute(
        "UPDATE trial_batch SET state = 'stopped', stopped_at = ?, stop_reason = 'migration_hold', version = version + 1 "
        "WHERE state IN ('draft', 'running')",
        (now,),
    )
    connection.execute(
        "UPDATE outbox_item SET state = 'cancelled', result_code = 'migration_hold_prepared', finished_at = ? "
        "WHERE state = 'prepared'",
        (now,),
    )
    connection.execute(
        "UPDATE outbox_item SET state = 'unknown', result_code = 'migration_hold_sending', finished_at = ? "
        "WHERE state = 'sending'",
        (now,),
    )
    connection.execute(
        "UPDATE tool_action_outbox SET state = 'cancelled', result_code = 'migration_hold_prepared', finished_at = ? "
        "WHERE state = 'prepared'",
        (now,),
    )
    connection.execute(
        "UPDATE tool_action_outbox SET state = 'unknown', result_code = 'migration_hold_sending', finished_at = ? "
        "WHERE state = 'sending'",
        (now,),
    )
    connection.execute(
        "UPDATE tool_action_preview SET state = 'expired' WHERE state = 'preview_only'"
    )
    connection.execute("UPDATE budget_entry SET state = 'unknown' WHERE state = 'dispatched'")
    connection.execute(
        "UPDATE chat_request SET state = 'unknown', error_code = 'migration_hold_unknown', updated_at = ? "
        "WHERE state IN ('queued', 'dispatching')",
        (now,),
    )
    connection.execute(
        "UPDATE conversation SET state = 'needs_reconciliation', stop_requested = 1, revision = revision + 1, updated_at = ? "
        "WHERE id IN (SELECT conversation_id FROM chat_request WHERE state = 'unknown')",
        (now,),
    )
    connection.execute(
        "UPDATE qq_inbound_event SET state = 'failed', reason = 'migration_hold', updated_at = ? "
        "WHERE state IN ('received', 'processing', 'queued')",
        (now,),
    )
    connection.execute(
        "UPDATE extension_task SET status = 'paused', explicit_pause = 1, pause_reason = 'migration_hold', "
        "effect_state = CASE WHEN effect_state IN ('not_dispatched', 'pending') THEN 'cancelled' "
        "WHEN effect_state = 'sending' THEN 'unknown' "
        "ELSE effect_state END, updated_at = ? WHERE status IN ('open', 'active')",
        (now,),
    )
    connection.execute(
        "UPDATE extension_candidate SET defer_reason = 'migration_hold', revision = revision + 1, updated_at = ? "
        "WHERE state IN ('proposed', 'draft', 'prepared')",
        (now,),
    )
    connection.execute(
        "UPDATE extension_room_authorization SET revoked_at = ? WHERE revoked_at IS NULL",
        (now,),
    )
    connection.execute(
        "UPDATE qq_account SET service_url = NULL, secret_name = 'credential_unconfigured_' || substr(hex(randomblob(16)), 1, 24), "
        "webhook_secret_name = 'webhook_unconfigured_' || substr(hex(randomblob(16)), 1, 24), enabled = 0, "
        "group_chat_enabled = 0, state = 'needs_credentials', updated_at = ?",
        (now,),
    )
    connection.execute("UPDATE module_configuration SET enabled = 0, version = version + 1, updated_at = ?", (now,))

    voice_state = {row[1] for row in connection.execute('PRAGMA table_info("extension_voice_profile_state")').fetchall()}
    if {"runtime_enabled", "voice_outbox_enabled"}.issubset(voice_state):
        updates = ['"runtime_enabled" = 0', '"voice_outbox_enabled" = 0']
        params: tuple[Any, ...] = ()
        if "revision" in voice_state:
            updates.append('"revision" = "revision" + 1')
        if "updated_at" in voice_state:
            updates.append('"updated_at" = ?')
            params = (now,)
        connection.execute(f"UPDATE extension_voice_profile_state SET {', '.join(updates)}", params)

    # Additional active features are disabled using explicit, known schema
    # columns only. This keeps new functionality fail-closed without a generic
    # data-driven SQL dispatcher.
    known_tables = set(_metadata_tables())
    if "proactivity_policy" in known_tables:
        columns = {row[1] for row in connection.execute('PRAGMA table_info("proactivity_policy")').fetchall()}
        updates: list[str] = []
        if "enabled" in columns:
            updates.append('"enabled" = 0')
        if "updated_at" in columns:
            updates.append('"updated_at" = ?')
        if "mode" in columns:
            updates.append('"mode" = ?')
        if "explicit_pause" in columns:
            updates.append('"explicit_pause" = 1')
        if updates:
            params = (now,) if "updated_at" in columns else ()
            if "mode" in columns:
                params = (*params, "manual") if "updated_at" in columns else ("manual",)
            connection.execute(f"UPDATE proactivity_policy SET {', '.join(updates)}", params)
    _set_app_setting(connection, MIGRATION_RUNTIME_KEY, _runtime_marker(), now)


def _assert_relative_db_paths(connection: sqlite3.Connection) -> None:
    for table, column in (("extension_room_item", "relative_path"), ("extension_managed_media", "relative_path"), ("extension_room_authorization", "relative_prefix")):
        for row in connection.execute(f"SELECT {_quote(column)} FROM {_quote(table)} WHERE {_quote(column)} IS NOT NULL").fetchall():
            if table == "extension_room_authorization" and column == "relative_prefix" and str(row[0]) == "":
                # Empty prefix means a grant scoped to the logical root itself.
                continue
            try:
                _safe_relative_path(str(row[0]))
            except MigrationError as exc:
                raise MigrationError("unsafe_database_path", "数据库记录了不安全的主机路径，拒绝迁移。", 422) from exc


def _copy_snapshot_to_memory(database_path: Path, expected_schema_version: int) -> tuple[sqlite3.Connection, list[str], int, int]:
    source = _read_only_connection(database_path)
    snapshot = sqlite3.connect(":memory:", timeout=10)
    snapshot.row_factory = sqlite3.Row
    snapshot.execute("PRAGMA foreign_keys=ON")
    try:
        source_errors, _version, _count_tables = _schema_errors(source, expected_schema_version)
        if source_errors:
            raise MigrationError("unsupported_schema", source_errors[0], 409)
        _has_sensitive_json(source)
        source.backup(snapshot, pages=1024, sleep=0.01)
        snapshot_errors, _snapshot_version, table_count = _schema_errors(snapshot, expected_schema_version)
        if snapshot_errors:
            raise MigrationError("unsupported_snapshot_schema", snapshot_errors[0], 409)
        _assert_relative_db_paths(snapshot)
        _has_sensitive_json(snapshot)
        return snapshot, [], table_count, 0
    except BaseException:
        snapshot.close()
        raise
    finally:
        source.close()


def _validate_media_selection(
    connection: sqlite3.Connection,
    media_ids: Sequence[str],
    policy: MigrationPolicy,
    *,
    candidate_media: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    if len(media_ids) > policy.max_media_files or len(set(media_ids)) != len(media_ids):
        raise MigrationError("invalid_media_selection", "媒体 ID 数量超限或存在重复项。", 422)
    if any(not isinstance(item, str) or not item or len(item) > 128 for item in media_ids):
        raise MigrationError("invalid_media_selection", "媒体 ID 无效。", 422)
    if not media_ids:
        return []
    placeholders = ",".join("?" for _ in media_ids)
    approval_preview_ids = _voice_approval_preview_ids(connection)
    rows = connection.execute(
        f"SELECT id, root_key, relative_path, sha256, mime_type, byte_length, source_scope, role_use_approved, version, source_refs, asset_class, artifact_purpose "
        f"FROM extension_managed_media WHERE id IN ({placeholders})",
        tuple(media_ids),
    ).fetchall()
    by_id = {str(row["id"]): row for row in rows}
    if len(by_id) != len(media_ids):
        raise MigrationError("media_not_registered", "至少一个媒体 ID 未在数据库中登记。", 404)
    items: list[dict[str, Any]] = []
    candidate_media = candidate_media or {}
    for media_id in media_ids:
        row = by_id[media_id]
        root_key = str(row["root_key"])
        source_scope = str(row["source_scope"])
        relative_path = _safe_relative_path(str(row["relative_path"]))
        if root_key not in TRANSFER_ROOT_KEYS:
            raise MigrationError("media_root_not_transferable", "媒体逻辑根不在迁移允许范围。", 403)
        is_approved_voice_audition = (
            media_id in approval_preview_ids
            and row["asset_class"] == "generated_voice"
            and row["artifact_purpose"] == "approval_preview"
        )
        candidate_id = candidate_media.get(media_id)
        is_candidate_reference = (
            candidate_id is not None
            and root_key == "data_root"
            and relative_path == f"voice-candidates/{candidate_id}.wav"
            and str(row["mime_type"] or "").casefold() == "audio/wav"
            and row["asset_class"] == "voice_candidate"
            and row["artifact_purpose"] == "candidate_reference"
            and source_scope == "calibration"
            and not bool(row["role_use_approved"])
        )
        if source_scope not in {"production", "calibration"} or not (
            bool(row["role_use_approved"]) or is_approved_voice_audition or is_candidate_reference
        ):
            raise MigrationError("media_scope_not_approved", "媒体用途范围未获迁移所需批准。", 403)
        try:
            source_refs = json.loads(row["source_refs"] or "[]")
        except (json.JSONDecodeError, TypeError) as exc:
            raise MigrationError("media_source_refs_invalid", "媒体来源引用无法核验。", 422) from exc
        if not isinstance(source_refs, list) or not source_refs:
            raise MigrationError("media_source_refs_invalid", "媒体缺少已登记来源引用。", 422)
        if not row["sha256"] or len(str(row["sha256"])) != 64:
            raise MigrationError("media_hash_missing", "媒体记录缺少可验证的 SHA256。", 409)
        size = int(row["byte_length"])
        if size < 0 or size > policy.max_media_file_bytes:
            raise MigrationError("media_size_limit", "媒体文件超过单文件迁移大小限制。", 413)
        items.append(
            {
                "stable_id": media_id,
                "kind": str(row["mime_type"] or "application/octet-stream"),
                "root_key": root_key,
                "relative_path": relative_path,
                "sha256": str(row["sha256"]).lower(),
                "byte_length": size,
                "source_scope": source_scope,
                "version": int(row["version"]),
                "role_use_approved": bool(row["role_use_approved"]),
                "reason": (
                    "voice_candidate_reference"
                    if is_candidate_reference
                    else "approved_voice_preview"
                    if is_approved_voice_audition and not bool(row["role_use_approved"])
                    else "selected_media"
                ),
                "candidate_id": candidate_id if is_candidate_reference else None,
            }
        )
    return items


def _voice_candidate_references(
    connection: sqlite3.Connection,
    policy: MigrationPolicy,
) -> tuple[dict[str, str], dict[str, tuple[Any, ...]]]:
    """Validate the whole-DB candidate dependency closure for a snapshot."""
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "local_voice_candidate" not in tables:
        return {}, {}
    if "extension_managed_media" not in tables:
        raise MigrationError("voice_candidate_reference_missing", "声线候选缺少受管媒体表。", 409)
    rows = connection.execute(
        "SELECT c.id AS candidate_id, c.origin, c.status, c.managed_media_id, "
        "c.reference_relative_path, c.reference_sha256, "
        "m.id AS media_id, m.root_key, m.relative_path, m.sha256, m.mime_type, m.byte_length, "
        "m.source_scope, m.role_use_approved, m.asset_class, m.artifact_purpose, m.artifact_state "
        "FROM local_voice_candidate AS c "
        "LEFT JOIN extension_managed_media AS m ON m.id = c.managed_media_id "
        "ORDER BY c.id"
    ).fetchall()
    by_media: dict[str, str] = {}
    signatures: dict[str, tuple[Any, ...]] = {}
    for row in rows:
        candidate_id = str(row["candidate_id"] or "")
        media_id = str(row["managed_media_id"] or "")
        relative_path = str(row["reference_relative_path"] or "")
        reference_hash = str(row["reference_sha256"] or "").lower()
        if (
            not candidate_id
            or len(candidate_id) > 128
            or not media_id
            or row["origin"] != "qwen_voice_design"
            or row["status"] != "unapproved"
            or row["media_id"] != media_id
            or row["root_key"] != "data_root"
            or relative_path != f"voice-candidates/{candidate_id}.wav"
            or row["relative_path"] != relative_path
            or not _safe_relative_path(relative_path)
            or not _SHA256_HEX.fullmatch(reference_hash)
            or str(row["sha256"] or "").lower() != reference_hash
            or str(row["mime_type"] or "").casefold() != "audio/wav"
            or row["asset_class"] != "voice_candidate"
            or row["artifact_purpose"] != "candidate_reference"
            or row["artifact_state"] != "unapproved"
            or row["source_scope"] != "calibration"
            or bool(row["role_use_approved"])
        ):
            raise MigrationError(
                "voice_candidate_reference_invalid",
                "声线候选与其受管参考 WAV 登记不一致，拒绝导出不完整快照。",
                409,
            )
        try:
            size = int(row["byte_length"])
        except (TypeError, ValueError) as exc:
            raise MigrationError("voice_candidate_reference_invalid", "声线候选 WAV 长度登记无效。", 409) from exc
        if size <= 0 or size > policy.max_media_file_bytes:
            raise MigrationError("media_size_limit", "声线候选 WAV 超过迁移单文件大小限制。", 413)
        if media_id in by_media or candidate_id in signatures:
            raise MigrationError("voice_candidate_reference_duplicate", "声线候选引用重复，拒绝导出。", 409)
        by_media[media_id] = candidate_id
        signatures[candidate_id] = (
            row["origin"], row["status"], media_id, relative_path, reference_hash,
            row["root_key"], row["sha256"], row["byte_length"], row["mime_type"],
            row["source_scope"], row["role_use_approved"], row["asset_class"],
            row["artifact_purpose"], row["artifact_state"],
        )
    if len(by_media) > policy.max_media_files:
        raise MigrationError("media_count_limit", "声线候选参考 WAV 数量超过迁移媒体数量限制。", 413)
    return by_media, signatures


_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")


def _current_voice_candidate_signature(
    database_path: Path,
    candidate_ids: Sequence[str],
) -> dict[str, tuple[Any, ...]]:
    if not candidate_ids:
        return {}
    connection = _read_only_connection(database_path)
    try:
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = connection.execute(
            "SELECT c.id AS candidate_id, c.origin, c.status, c.managed_media_id, "
            "c.reference_relative_path, c.reference_sha256, "
            "m.root_key, m.sha256, m.byte_length, m.mime_type, m.source_scope, m.role_use_approved, "
            "m.asset_class, m.artifact_purpose, m.artifact_state "
            "FROM local_voice_candidate AS c "
            "LEFT JOIN extension_managed_media AS m ON m.id = c.managed_media_id "
            f"WHERE c.id IN ({placeholders})",
            tuple(candidate_ids),
        ).fetchall()
        result: dict[str, tuple[Any, ...]] = {}
        for row in rows:
            result[str(row["candidate_id"])] = (
                row["origin"], row["status"], row["managed_media_id"], row["reference_relative_path"],
                str(row["reference_sha256"] or "").lower(), row["root_key"], row["sha256"],
                row["byte_length"], row["mime_type"], row["source_scope"], row["role_use_approved"],
                row["asset_class"], row["artifact_purpose"], row["artifact_state"],
            )
        return result
    finally:
        connection.close()


def _voice_approval_preview_ids(connection: sqlite3.Connection) -> set[str]:
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "extension_voice_approval" not in tables:
        return set()
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT preview_artifact_id FROM extension_voice_approval WHERE preview_artifact_id IS NOT NULL"
        ).fetchall()
    }


def _media_signature(connection: sqlite3.Connection, items: Sequence[Mapping[str, Any]]) -> dict[str, tuple[Any, ...]]:
    if not items:
        return {}
    ids = [str(item["stable_id"]) for item in items]
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT id, root_key, relative_path, sha256, mime_type, byte_length, source_scope, role_use_approved, version "
        f"FROM extension_managed_media WHERE id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    return {
        str(row["id"]): (
            row["root_key"], row["relative_path"], row["sha256"], row["mime_type"],
            row["byte_length"], row["source_scope"], row["role_use_approved"], row["version"],
        )
        for row in rows
    }


def _current_media_signature(database_path: Path, items: Sequence[Mapping[str, Any]]) -> dict[str, tuple[Any, ...]]:
    connection = _read_only_connection(database_path)
    try:
        return _media_signature(connection, items)
    finally:
        connection.close()


def _expected_media_signature(items: Sequence[Mapping[str, Any]]) -> dict[str, tuple[Any, ...]]:
    return {
        str(item["stable_id"]): (
            item["root_key"], item["relative_path"], item["sha256"], item["kind"],
            item["byte_length"], item["source_scope"], bool(item.get("role_use_approved", True)), item["version"],
        )
        for item in items
    }


def _hash_file(path: Path, *, max_bytes: int | None = None) -> tuple[str, int]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or _is_link_or_junction(path) or getattr(info, "st_nlink", 1) > 1:
        raise MigrationError("unsafe_file", "迁移文件不是独立普通文件。", 422)
    if max_bytes is not None and info.st_size > max_bytes:
        raise MigrationError("file_size_limit", "迁移文件超过允许的大小限制。", 413)
    with path.open("rb") as stream:
        return hash_stream(stream)


def _copy_verified_media(
    paths: HostPathResolver,
    items: Sequence[Mapping[str, Any]],
    bundle_temp: Path,
    policy: MigrationPolicy,
) -> tuple[ManifestFile, ...]:
    result: list[ManifestFile] = []
    total = 0
    for item in items:
        try:
            source = paths.resolve(str(item["root_key"]), str(item["relative_path"]), must_exist=True)
            _assert_no_link_components(source)
            digest, size = _hash_file(source, max_bytes=policy.max_media_file_bytes)
        except (PathPolicyError, FileNotFoundError, OSError, ValueError) as exc:
            if item.get("reason") == "voice_candidate_reference":
                raise MigrationError(
                    "voice_candidate_reference_missing",
                    "声线候选参考 WAV 缺失或不在受控目录中，拒绝生成不完整迁移包。",
                    409,
                ) from exc
            raise MigrationError("media_unavailable", "登记媒体当前缺失或不在受控目录中。", 409) from exc
        if digest != item["sha256"] or size != item["byte_length"]:
            if item.get("reason") == "voice_candidate_reference":
                raise MigrationError(
                    "voice_candidate_reference_mismatch",
                    "声线候选参考 WAV 与登记 hash 或长度不一致，拒绝生成不完整迁移包。",
                    409,
                )
            raise MigrationError("media_source_changed", "媒体文件内容与登记的版本或哈希不一致。", 409)
        total += size
        if total > policy.max_media_total_bytes:
            raise MigrationError("media_total_size_limit", "媒体文件总量超过迁移限制。", 413)
        bundle_path = f"media/{item['root_key']}/{item['relative_path']}"
        target = bundle_temp.joinpath(*PurePosixPath(bundle_path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as source_stream, target.open("xb") as target_stream:
            shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
            target_stream.flush()
            os.fsync(target_stream.fileno())
        copied_hash, copied_size = _hash_file(target, max_bytes=policy.max_media_file_bytes)
        if copied_hash != digest or copied_size != size:
            raise MigrationError("media_copy_mismatch", "复制后的媒体内容校验失败。", 409)
        result.append(
            ManifestFile(
                stable_id=str(item["stable_id"]),
                kind=str(item["kind"]),
                root_key=str(item["root_key"]),
                relative_path=str(item["relative_path"]),
                bundle_path=bundle_path,
                sha256=digest,
                byte_length=size,
                source_scope=str(item["source_scope"]),
                version=int(item["version"]),
                reason=str(item.get("reason") or "selected_media"),
                candidate_id=item.get("candidate_id"),
            )
        )
    return tuple(result)


def _sanitize_snapshot(snapshot: sqlite3.Connection) -> None:
    snapshot.execute("PRAGMA foreign_keys=OFF")
    snapshot.execute("PRAGMA secure_delete=ON")
    snapshot.execute("BEGIN IMMEDIATE")
    try:
        _hold_snapshot(snapshot)
        # Config JSON is deliberately examined before rewriting it. Unknown
        # sensitive fields or source paths stop the whole export.
        for table, column in (("app_setting", "value"), ("module_configuration", "value")):
            for row in snapshot.execute(f"SELECT rowid, {_quote(column)} FROM {_quote(table)}").fetchall():
                value = json.loads(row[column])
                sanitized = _scrub_json(value)
                encoded = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                snapshot.execute(
                    f"UPDATE {_quote(table)} SET {_quote(column)} = ? WHERE rowid = ?",
                    (encoded, row["rowid"]),
                )
        _assert_relative_db_paths(snapshot)
        snapshot.commit()
    except BaseException:
        snapshot.rollback()
        raise
    snapshot.execute("PRAGMA secure_delete=ON")
    snapshot.execute("VACUUM")
    snapshot.execute("PRAGMA foreign_keys=ON")
    integrity = [str(row[0]) for row in snapshot.execute("PRAGMA integrity_check").fetchall()]
    if integrity != ["ok"]:
        raise MigrationError("sanitized_integrity_failed", "净库 SQLite integrity_check 未通过。", 409)
    foreign = snapshot.execute("PRAGMA foreign_key_check").fetchall()
    if foreign:
        raise MigrationError("sanitized_foreign_keys_failed", "净库外键校验未通过。", 409)


def _write_database(snapshot: sqlite3.Connection, destination: Path) -> tuple[str, int]:
    target = sqlite3.connect(str(destination), timeout=10)
    try:
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("PRAGMA synchronous=FULL")
        snapshot.backup(target, pages=1024, sleep=0.01)
        target.commit()
    finally:
        target.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(destination) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    if Path(str(destination) + "-wal").exists() or Path(str(destination) + "-shm").exists():
        raise MigrationError("database_sidecar_present", "净库关闭后仍有 SQLite WAL/SHM 文件。", 409)
    return _hash_file(destination, max_bytes=MAX_DATABASE_BYTES)


def _validate_database(path: Path, expected_schema_version: int) -> tuple[list[str], int | None]:
    errors: list[str] = []
    if Path(str(path) + "-wal").exists() or Path(str(path) + "-shm").exists():
        return ["迁移数据库旁存在 WAL/SHM 文件。"], None
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        schema_errors, version, _table_count = _schema_errors(connection, expected_schema_version)
        errors.extend(schema_errors)
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            errors.append("迁移数据库 integrity_check 未通过。")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            errors.append("迁移数据库 foreign_key_check 未通过。")
        for table in EXCLUDED_TABLES:
            if _count(connection, table):
                errors.append("迁移数据库仍含管理员或主机秘密记录。")
        if _count(connection, "action_authorization"):
            errors.append("迁移数据库仍含短期运行授权。")
        if _count(connection, "tool_action_grant"):
            errors.append("迁移数据库仍含短期工具动作授权。")
        if _count(connection, "outbox_item", "state IN ('prepared', 'sending')"):
            errors.append("迁移数据库仍有可恢复或正在派发的 Outbox 项。")
        if _count(connection, "tool_action_outbox", "state IN ('prepared', 'sending')"):
            errors.append("迁移数据库仍有可恢复或正在派发的工具动作。")
        if _count(connection, "tool_action_preview", "state = 'preview_only'"):
            errors.append("迁移数据库仍含可继续使用的动作预览。")
        if _count(connection, "trial_batch", "state IN ('draft', 'running')"):
            errors.append("迁移数据库仍有可运行试用批次。")
        for row in connection.execute("SELECT enabled, state, service_url, secret_name, webhook_secret_name FROM qq_account"):
            if row["enabled"] or row["state"] != "needs_credentials" or row["service_url"] is not None:
                errors.append("迁移数据库仍含启用的源主机 QQ 连接。")
                break
        marker = connection.execute("SELECT value FROM app_setting WHERE key = ?", (MIGRATION_RUNTIME_KEY,)).fetchone()
        try:
            marker_data = json.loads(marker[0]) if marker else {}
        except (json.JSONDecodeError, TypeError):
            marker_data = {}
        if marker_data != _runtime_marker():
            errors.append("迁移数据库缺少持久 target_staging 安全策略。")
        return list(dict.fromkeys(errors)), version
    except sqlite3.Error:
        return ["无法读取迁移数据库。"], None
    finally:
        connection.close()


def _validate_candidate_manifest_closure(
    database_path: Path,
    manifest_files: Sequence[ManifestFile],
    policy: MigrationPolicy,
) -> None:
    """Reject bundles whose database has candidates without their required WAV."""
    connection = _read_only_connection(database_path)
    try:
        candidate_media, signatures = _voice_candidate_references(connection, policy)
    finally:
        connection.close()
    by_media = {item.stable_id: item for item in manifest_files}
    for media_id, candidate_id in candidate_media.items():
        item = by_media.get(media_id)
        if (
            item is None
            or item.reason != "voice_candidate_reference"
            or item.candidate_id != candidate_id
            or item.root_key != "data_root"
            or item.relative_path != f"voice-candidates/{candidate_id}.wav"
            or item.sha256 != signatures[candidate_id][4]
            or item.byte_length != int(signatures[candidate_id][7])
            or item.kind != signatures[candidate_id][8]
            or item.source_scope != signatures[candidate_id][9]
        ):
            raise MigrationError(
                "voice_candidate_reference_missing",
                "迁移清单没有包含数据库中全部声线候选所需的参考 WAV。",
                422,
            )
    for item in manifest_files:
        expected_candidate = candidate_media.get(item.stable_id)
        if item.reason == "voice_candidate_reference":
            if expected_candidate is None or item.candidate_id != expected_candidate:
                raise MigrationError(
                    "voice_candidate_reference_invalid",
                    "迁移清单包含无对应候选记录的声线参考 WAV。",
                    422,
                )
        elif expected_candidate is not None:
            raise MigrationError(
                "voice_candidate_reference_missing",
                "迁移清单没有正确标注声线候选参考 WAV。",
                422,
            )


def _walk_bundle(bundle_path: Path, manifest: MigrationManifest) -> list[str]:
    expected_files = {MANIFEST_FILENAME, MANIFEST_HASH_FILENAME, DATABASE_FILENAME}
    expected_dirs: set[str] = set()
    for item in manifest.files:
        relative = _safe_relative_path(item.bundle_path)
        if relative != item.bundle_path or not relative.startswith("media/"):
            raise MigrationError("invalid_bundle_path", "迁移清单中的媒体包路径无效。", 422)
        if item.bundle_path in expected_files:
            raise MigrationError("duplicate_bundle_path", "迁移清单中的媒体路径重复。", 422)
        expected_files.add(item.bundle_path)
        parts = PurePosixPath(item.bundle_path).parts
        for idx in range(1, len(parts)):
            expected_dirs.add("/".join(parts[:idx]))
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for current_root, dirs, filenames in os.walk(bundle_path, followlinks=False):
        current = Path(current_root)
        _assert_no_link_components(current)
        relative_root = current.relative_to(bundle_path).as_posix()
        if relative_root != ".":
            actual_dirs.add(relative_root)
        for name in list(dirs):
            path = current / name
            if _is_link_or_junction(path):
                raise MigrationError("linked_bundle_entry", "迁移包包含符号链接或目录联接。", 422)
        for name in filenames:
            path = current / name
            if _is_link_or_junction(path):
                raise MigrationError("linked_bundle_entry", "迁移包包含符号链接或目录联接。", 422)
            actual_files.add((path.relative_to(bundle_path)).as_posix())
    extra_files = actual_files - expected_files
    missing_files = expected_files - actual_files
    if extra_files or missing_files:
        raise MigrationError("bundle_contents_mismatch", "迁移包包含未清单登记或缺失的文件。", 422)
    if actual_dirs - expected_dirs:
        raise MigrationError("bundle_contents_mismatch", "迁移包包含未登记的目录。", 422)
    return sorted(actual_files)


def _validate_bundle_sync(bundle_path: Path, policy: MigrationPolicy) -> ValidationResult:
    try:
        bundle_path = _assert_under_root(bundle_path, "backup_root", policy)
        if not bundle_path.is_dir() or _is_link_or_junction(bundle_path):
            raise MigrationError("bundle_not_found", "迁移包目录不存在或不可用。", 404)
        manifest_path = bundle_path / MANIFEST_FILENAME
        if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise MigrationError("manifest_invalid", "迁移清单不存在或超过大小限制。", 422)
        raw = manifest_path.read_bytes()
        hash_path = bundle_path / MANIFEST_HASH_FILENAME
        if not hash_path.is_file() or hash_path.stat().st_size > 128:
            raise MigrationError("manifest_checksum_missing", "迁移清单旁车校验文件不存在。", 422)
        declared_manifest_hash = hash_path.read_text(encoding="ascii").strip().lower()
        manifest_hash = hashlib.sha256(raw).hexdigest()
        if declared_manifest_hash != manifest_hash:
            raise MigrationError("manifest_hash_mismatch", "迁移清单 SHA256 不匹配。", 422)
        manifest = MigrationManifest.model_validate_json(raw)
        if manifest.database.excluded_tables != EXCLUDED_TABLES:
            raise MigrationError("manifest_exclusion_mismatch", "迁移清单排除项与当前策略不匹配。", 422)
        if manifest.excluded_categories != EXCLUDED_CATEGORIES:
            raise MigrationError("manifest_exclusion_mismatch", "迁移清单排除类别与当前策略不匹配。", 422)
        root_keys = [item.root_key for item in manifest.logical_roots]
        if len(root_keys) != len(ROOT_KEYS) or set(root_keys) != set(ROOT_KEYS):
            raise MigrationError("manifest_roots_invalid", "迁移清单逻辑根列表不完整或重复。", 422)
        file_ids = [item.stable_id for item in manifest.files]
        bundle_paths = [item.bundle_path for item in manifest.files]
        if len(file_ids) != len(set(file_ids)) or len(bundle_paths) != len(set(bundle_paths)):
            raise MigrationError("manifest_media_duplicate", "迁移清单包含重复的媒体 ID 或路径。", 422)
        for item in manifest.files:
            if _safe_relative_path(item.relative_path) != item.relative_path:
                raise MigrationError("manifest_media_path_invalid", "迁移清单包含不安全的媒体相对路径。", 422)
            expected_bundle_path = f"media/{item.root_key}/{item.relative_path}"
            if item.bundle_path != expected_bundle_path:
                raise MigrationError("manifest_media_path_invalid", "迁移包路径与逻辑根/相对路径不匹配。", 422)
        files = _walk_bundle(bundle_path, manifest)
        db_path = bundle_path / DATABASE_FILENAME
        db_hash, db_size = _hash_file(db_path, max_bytes=policy.max_database_bytes)
        if db_hash != manifest.database.sha256 or db_size != manifest.database.size_bytes:
            raise MigrationError("database_hash_mismatch", "迁移数据库哈希或大小不匹配。", 422)
        db_errors, version = _validate_database(db_path, policy.expected_schema_version)
        if db_errors:
            raise MigrationError("database_validation_failed", db_errors[0], 422)
        _validate_candidate_manifest_closure(db_path, manifest.files, policy)
        for item in manifest.files:
            file_path = bundle_path.joinpath(*PurePosixPath(item.bundle_path).parts)
            digest, size = _hash_file(file_path, max_bytes=policy.max_media_file_bytes)
            if digest != item.sha256 or size != item.byte_length:
                raise MigrationError("media_hash_mismatch", "迁移包媒体文件哈希或大小不匹配。", 422)
        total_bytes = sum(item.byte_length for item in manifest.files)
        if len(manifest.files) > policy.max_media_files or total_bytes > policy.max_media_total_bytes:
            raise MigrationError("media_total_size_limit", "迁移包媒体数量或总大小超过限制。", 413)
        if version != manifest.database.schema_version or version != manifest.source.schema_version:
            raise MigrationError("manifest_schema_mismatch", "迁移清单和净库 schema 版本不一致。", 422)
        return ValidationResult(
            valid=True,
            export_id=manifest.export_id,
            database_sha256=db_hash,
            media_count=len(manifest.files),
        )
    except (MigrationError, ValidationError, OSError, UnicodeError, json.JSONDecodeError, sqlite3.Error) as exc:
        if isinstance(exc, MigrationError):
            message = exc.message
        elif isinstance(exc, ValidationError):
            message = "迁移清单格式或字段不符合当前版本。"
        else:
            message = "迁移包无法安全读取或验证。"
        return ValidationResult(valid=False, errors=(message,))


async def validate_bundle(bundle_path: Path, policy: MigrationPolicy | None = None) -> ValidationResult:
    resolved_policy = policy or MigrationPolicy()
    return await asyncio.to_thread(_validate_bundle_sync, Path(bundle_path), resolved_policy)


def _materialize_candidate_references(staging: Path, manifest: MigrationManifest, policy: MigrationPolicy) -> None:
    """Place candidate WAVs under the target's controlled data_root for the normal reader."""
    target_paths = HostPathResolver({"data_root": staging})
    for item in manifest.files:
        if item.reason != "voice_candidate_reference":
            continue
        expected_relative = f"voice-candidates/{item.candidate_id}.wav"
        if (
            item.root_key != "data_root"
            or not item.candidate_id
            or item.relative_path != expected_relative
        ):
            raise MigrationError(
                "voice_candidate_reference_invalid",
                "声线候选参考 WAV 的目标路径无效。",
                422,
            )
        relative = _safe_relative_path(expected_relative)
        try:
            source_relative = _safe_relative_path(item.bundle_path)
            source = target_paths.resolve("data_root", source_relative, must_exist=True)
            destination = target_paths.resolve("data_root", relative, for_write=True)
            _assert_no_link_components(source)
            _assert_no_link_components(destination.parent)
            if not source.is_file() or _is_link_or_junction(source) or destination.exists():
                raise ValueError("candidate reference source or destination is unavailable")
            destination.parent.mkdir(parents=True, exist_ok=True)
            _assert_no_link_components(destination.parent)
            destination = target_paths.resolve("data_root", relative, for_write=True)
            if destination.exists():
                raise ValueError("candidate reference destination already exists")
            with source.open("rb") as source_stream, destination.open("xb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            digest, size = _hash_file(destination, max_bytes=policy.max_media_file_bytes)
            if digest != item.sha256 or size != item.byte_length:
                raise MigrationError(
                    "voice_candidate_reference_mismatch",
                    "导入后的声线候选 WAV 与清单校验值不一致。",
                    409,
                )
        except MigrationError:
            raise
        except (PathPolicyError, FileNotFoundError, OSError, ValueError) as exc:
            raise MigrationError(
                "voice_candidate_reference_materialization_failed",
                "声线候选参考 WAV 无法安全放入目标数据目录。",
                409,
            ) from exc


def _export_bundle_sync(
    ctx: Any,
    output_dir: Path,
    media_ids: Sequence[str],
    policy: MigrationPolicy | None,
) -> BundleResult:
    settings = ctx.settings
    active_profile = _effective_profile(ctx)
    if active_profile != "local_trial":
        raise MigrationError("source_mode_unsupported", "仅 local_trial 数据可作为迁移源导出。", 409)
    root_policy = policy or MigrationPolicy(paths=ctx.paths, source_host_id=settings.host_id)
    paths = root_policy.paths or ctx.paths
    root_policy = MigrationPolicy(
        paths=paths,
        source_host_id=root_policy.source_host_id or str(settings.host_id),
        app_version=root_policy.app_version or __version__,
        expected_schema_version=root_policy.expected_schema_version,
        max_database_bytes=root_policy.max_database_bytes,
        max_media_file_bytes=root_policy.max_media_file_bytes,
        max_media_total_bytes=root_policy.max_media_total_bytes,
        max_media_files=root_policy.max_media_files,
    )
    output_dir = _assert_under_root(Path(output_dir), "backup_root", root_policy)
    database_path = Path(settings.database_path)
    _assert_no_link_components(database_path)
    if output_dir.exists():
        raise MigrationError("destination_exists", "迁移导出目标必须是一个新的目录。", 409)
    if not database_path.is_file():
        raise MigrationError("source_database_unavailable", "源数据库不存在。", 404)
    source_size = database_path.stat().st_size
    if source_size > root_policy.max_database_bytes:
        raise MigrationError("database_size_limit", "源数据库超过迁移大小限制。", 413)

    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    _assert_no_link_components(parent)
    temporary_dir = parent / f".{output_dir.name}.migration-{uuid.uuid4().hex}.tmp"
    temporary_dir.mkdir(mode=0o700)
    snapshot: sqlite3.Connection | None = None
    try:
        snapshot, _warnings, _table_count, _unused = _copy_snapshot_to_memory(
            database_path, root_policy.expected_schema_version
        )
        candidate_media, candidate_signatures = _voice_candidate_references(snapshot, root_policy)
        selected_ids = list(media_ids)
        selected_id_set = set(selected_ids)
        for candidate_media_id in sorted(candidate_media):
            if candidate_media_id not in selected_id_set:
                selected_ids.append(candidate_media_id)
                selected_id_set.add(candidate_media_id)
        for preview_id in sorted(_voice_approval_preview_ids(snapshot)):
            if preview_id not in selected_id_set:
                selected_ids.append(preview_id)
                selected_id_set.add(preview_id)
        if len(selected_ids) > root_policy.max_media_files:
            raise MigrationError("media_count_limit", "声线候选、获批试听和所选媒体超过迁移媒体数量限制。", 413)
        selected_media = _validate_media_selection(
            snapshot,
            selected_ids,
            root_policy,
            candidate_media=candidate_media,
        )
        if len(selected_media) > root_policy.max_media_files:
            raise MigrationError("media_count_limit", "媒体数量超过迁移限制。", 413)
        _sanitize_snapshot(snapshot)
        db_path = temporary_dir / DATABASE_FILENAME
        db_hash, db_size = _write_database(snapshot, db_path)
        snapshot.close()
        snapshot = None

        media_manifest = _copy_verified_media(paths, selected_media, temporary_dir, root_policy)
        current_signature = _current_media_signature(database_path, selected_media)
        if current_signature != _expected_media_signature(selected_media):
            raise MigrationError("media_batch_changed", "登记媒体在导出批次中发生变化；拒绝生成不一致迁移包。", 409)
        current_candidates = _current_voice_candidate_signature(database_path, tuple(candidate_signatures))
        if current_candidates != candidate_signatures:
            raise MigrationError("media_batch_changed", "声线候选登记在导出批次中发生变化；拒绝生成不一致迁移包。", 409)

        export_id = str(uuid.uuid4())
        manifest = MigrationManifest(
            format="cutefish-migration",
            manifest_version=1,
            export_id=export_id,
            snapshot_batch_id=str(uuid.uuid4()),
            created_at=_utc_now_text(),
            source=ManifestSource(
                host_id=str(root_policy.source_host_id),
                app_version=root_policy.app_version,
                schema_version=root_policy.expected_schema_version,
                deployment_profile="local_trial",
            ),
            database=ManifestDatabase(
                filename=DATABASE_FILENAME,
                sha256=db_hash,
                size_bytes=db_size,
                schema_version=root_policy.expected_schema_version,
                sqlite_integrity_check="ok",
                foreign_key_check="ok",
                sanitized=True,
                excluded_tables=EXCLUDED_TABLES,
            ),
            files=media_manifest,
            logical_roots=_safe_manifest_roots(paths),
            excluded_categories=EXCLUDED_CATEGORIES,
            restore_policy=RestorePolicy(
                deployment_profile="target_staging",
                external_actions_enabled=False,
                outbox_dispatch_enabled=False,
                paid_provider_calls_enabled=False,
                scheduling_enabled=False,
                auto_start_enabled=False,
                task_auto_resume=False,
                credentials_state="reconfigure_on_target",
                requires_host_bindings=True,
            ),
        )
        manifest_bytes = manifest.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_path = temporary_dir / MANIFEST_FILENAME
        with manifest_path.open("wb") as stream:
            stream.write(manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        (temporary_dir / MANIFEST_HASH_FILENAME).write_text(manifest_hash + "\n", encoding="ascii")
        if (temporary_dir / (DATABASE_FILENAME + "-wal")).exists() or (temporary_dir / (DATABASE_FILENAME + "-shm")).exists():
            raise MigrationError("database_sidecar_present", "迁移包生成时出现 SQLite WAL/SHM 文件。", 409)
        os.rename(temporary_dir, output_dir)
        relative = output_dir.resolve(strict=False).relative_to(paths.root_path("backup_root").resolve(strict=False)).as_posix()
        return BundleResult(
            export_id=export_id,
            bundle_root_key="backup_root",
            bundle_relative_path=relative,
            database_sha256=db_hash,
            manifest_sha256=manifest_hash,
            media_count=len(media_manifest),
            sanitized=True,
        )
    except BaseException:
        if snapshot is not None:
            snapshot.close()
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


async def export_bundle(
    ctx: Any,
    output_dir: Path,
    media_ids: Sequence[str],
    policy: MigrationPolicy | None = None,
) -> BundleResult:
    """Create a sanitized, content-checked bundle under the registered backup root."""

    return await asyncio.to_thread(_export_bundle_sync, ctx, Path(output_dir), tuple(media_ids), policy)


def _import_staging_sync(
    bundle_path: Path,
    empty_target_dir: Path,
    policy: MigrationPolicy,
) -> ImportResult:
    paths = _resolver(policy)
    bundle_path = _assert_under_root(Path(bundle_path), "backup_root", policy)
    target = _assert_under_root(Path(empty_target_dir), "data_root", policy)
    if target.resolve(strict=False) == paths.root_path("data_root").resolve(strict=False):
        raise MigrationError("target_must_be_new_child", "导入目标必须是 data_root 下的新子目录。", 422)
    validation = _validate_bundle_sync(bundle_path, policy)
    if not validation.valid:
        raise MigrationError("bundle_validation_failed", validation.errors[0], 422)
    if target.resolve(strict=False) == bundle_path.resolve(strict=False):
        raise MigrationError("target_bundle_collision", "导入目标不能覆盖迁移包。", 422)
    try:
        target.resolve(strict=False).relative_to(bundle_path.resolve(strict=False))
        raise MigrationError("target_bundle_collision", "导入目标不能位于迁移包内部。", 422)
    except ValueError:
        pass
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    _assert_no_link_components(parent)
    if target.exists():
        if _is_link_or_junction(target) or not target.is_dir() or any(target.iterdir()):
            raise MigrationError("target_not_empty", "导入目标必须是新的空目录。", 409)
    staging = parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        manifest = MigrationManifest.model_validate_json((bundle_path / MANIFEST_FILENAME).read_bytes())
        bundle_files = [MANIFEST_FILENAME, MANIFEST_HASH_FILENAME, DATABASE_FILENAME]
        bundle_files.extend(item.bundle_path for item in manifest.files)
        for relative in bundle_files:
            normalized = _safe_relative_path(relative)
            source = bundle_path.joinpath(*PurePosixPath(normalized).parts)
            destination = staging.joinpath(*PurePosixPath(normalized).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _assert_no_link_components(source)
            with source.open("rb") as source_stream, destination.open("xb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                target_stream.flush()
                os.fsync(target_stream.fileno())
        second_validation = _validate_bundle_sync(staging, MigrationPolicy(
            paths=HostPathResolver({"backup_root": str(staging)}),
            expected_schema_version=policy.expected_schema_version,
            max_database_bytes=policy.max_database_bytes,
            max_media_file_bytes=policy.max_media_file_bytes,
            max_media_total_bytes=policy.max_media_total_bytes,
            max_media_files=policy.max_media_files,
        ))
        if not second_validation.valid:
            raise MigrationError("staging_validation_failed", "导入临时区复核未通过。", 422)
        _materialize_candidate_references(staging, manifest, policy)
        had_empty_target = target.exists()
        if had_empty_target:
            target.rmdir()
        try:
            os.rename(staging, target)
        except OSError:
            if had_empty_target and not target.exists():
                target.mkdir()
            raise
        relative = target.resolve(strict=False).relative_to(paths.root_path("data_root").resolve(strict=False)).as_posix()
        return ImportResult(
            imported=True,
            deployment_profile="target_staging",
            target_relative_path=relative,
            bundle_export_id=manifest.export_id,
            requires_restart=True,
            restore_policy=TargetStagingPolicy(),
        )
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


async def import_staging(
    bundle_path: Path,
    empty_target_dir: Path,
    policy: MigrationPolicy | None = None,
) -> ImportResult:
    """Copy a validated bundle through same-volume staging and atomically publish it."""

    resolved_policy = policy or MigrationPolicy()
    return await asyncio.to_thread(_import_staging_sync, Path(bundle_path), Path(empty_target_dir), resolved_policy)


def migration_status(ctx: Any) -> dict[str, Any]:
    """Return target-staging controls without disclosing host-local paths."""

    profile = _effective_profile(ctx)
    if profile not in {"local_trial", "target_staging"}:
        profile = "local_trial"
    status = {str(item["root_key"]): item for item in ctx.paths.status()}
    return {
        "deployment_profile": profile,
        "target_staging": profile == "target_staging",
        "restore_policy": TargetStagingPolicy().model_dump(),
        "logical_roots": [
            {
                "root_key": key,
                "configured": bool(status.get(key, {}).get("configured", False)),
                "available": bool(status.get(key, {}).get("available", False)),
            }
            for key in ROOT_KEYS
        ],
    }
