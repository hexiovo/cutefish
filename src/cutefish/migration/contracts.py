"""Versioned, strict DTOs for the on-disk migration bundle and local API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MigrationPolicyVersion = Literal[1]

ROOT_KEYS = (
    "data_root",
    "gallery_root",
    "room_root",
    "backup_root",
    "cache_root",
    "model_root",
    "temp_root",
)
TRANSFER_ROOT_KEYS = ("data_root", "gallery_root", "room_root")
EXCLUDED_TABLES = ("secret_record", "auth_session", "admin_account")
EXCLUDED_CATEGORIES = (
    "secrets",
    "auth_sessions",
    "admin_credentials",
    "cookies",
    "browser_profiles",
    "machine_credentials",
    "model_weights",
    "venv",
    "node_modules",
    "temporary_cache",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ManifestSource(StrictModel):
    host_id: str = Field(min_length=1, max_length=128)
    app_version: str = Field(min_length=1, max_length=64)
    schema_version: int = Field(ge=1)
    deployment_profile: Literal["local_trial"]


class ManifestDatabase(StrictModel):
    filename: Literal["cutefish.sqlite3"]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    schema_version: int = Field(ge=1)
    sqlite_integrity_check: Literal["ok"]
    foreign_key_check: Literal["ok"]
    sanitized: Literal[True]
    excluded_tables: tuple[Literal["secret_record", "auth_session", "admin_account"], ...]


class ManifestFile(StrictModel):
    stable_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    root_key: Literal["data_root", "gallery_root", "room_root"]
    relative_path: str = Field(min_length=1, max_length=1024)
    bundle_path: str = Field(min_length=1, max_length=1200)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_length: int = Field(ge=0)
    source_scope: Literal["production", "calibration"]
    version: int = Field(ge=1)
    reason: Literal["selected_media", "approved_voice_preview", "voice_candidate_reference"] = "selected_media"
    candidate_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def candidate_reference_has_identity(self) -> "ManifestFile":
        if self.reason == "voice_candidate_reference" and self.candidate_id is None:
            raise ValueError("voice candidate reference must identify its candidate")
        if self.reason != "voice_candidate_reference" and self.candidate_id is not None:
            raise ValueError("candidate_id is only allowed for a voice candidate reference")
        return self


class ManifestLogicalRoot(StrictModel):
    root_key: Literal[
        "data_root", "gallery_root", "room_root", "backup_root", "cache_root", "model_root", "temp_root"
    ]
    mapped: bool


class RestorePolicy(StrictModel):
    deployment_profile: Literal["target_staging"]
    external_actions_enabled: Literal[False]
    outbox_dispatch_enabled: Literal[False]
    paid_provider_calls_enabled: Literal[False]
    scheduling_enabled: Literal[False]
    auto_start_enabled: Literal[False]
    task_auto_resume: Literal[False]
    credentials_state: Literal["reconfigure_on_target"]
    requires_host_bindings: Literal[True]


class MigrationManifest(StrictModel):
    format: Literal["cutefish-migration"]
    manifest_version: Literal[1]
    export_id: str = Field(min_length=1, max_length=64)
    snapshot_batch_id: str = Field(min_length=1, max_length=64)
    created_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
    source: ManifestSource
    database: ManifestDatabase
    files: tuple[ManifestFile, ...]
    logical_roots: tuple[ManifestLogicalRoot, ...]
    excluded_categories: tuple[
        Literal[
            "secrets",
            "auth_sessions",
            "admin_credentials",
            "cookies",
            "browser_profiles",
            "machine_credentials",
            "model_weights",
            "venv",
            "node_modules",
            "temporary_cache",
        ],
        ...,
    ]
    restore_policy: RestorePolicy


class RootStatus(StrictModel):
    root_key: str
    configured: bool
    available: bool


class TargetStagingPolicy(StrictModel):
    external_actions_enabled: Literal[False] = False
    outbox_dispatch_enabled: Literal[False] = False
    paid_provider_calls_enabled: Literal[False] = False
    scheduling_enabled: Literal[False] = False
    auto_start_enabled: Literal[False] = False
    task_auto_resume: Literal[False] = False


class MigrationStatus(StrictModel):
    deployment_profile: Literal["local_trial", "target_staging"]
    target_staging: bool
    restore_policy: TargetStagingPolicy
    logical_roots: tuple[RootStatus, ...]


class Preflight(StrictModel):
    available: bool
    schema_version: int | None
    schema_supported: bool
    table_count: int
    registered_media_count: int
    active_batch_count: int
    pending_outbox_count: int
    sending_outbox_count: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class BundleResult(StrictModel):
    export_id: str
    bundle_root_key: Literal["backup_root"]
    bundle_relative_path: str
    database_sha256: str
    manifest_sha256: str
    media_count: int
    sanitized: Literal[True]


class ValidationResult(StrictModel):
    valid: bool
    export_id: str | None = None
    database_sha256: str | None = None
    media_count: int = 0
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ImportResult(StrictModel):
    imported: Literal[True]
    deployment_profile: Literal["target_staging"]
    target_relative_path: str
    bundle_export_id: str
    requires_restart: Literal[True]
    restore_policy: TargetStagingPolicy


class ExportRequest(StrictModel):
    destination_relative_path: str = Field(min_length=1, max_length=1024)
    media_ids: tuple[str, ...] = Field(max_length=256)


class BundlePathRequest(StrictModel):
    bundle_relative_path: str = Field(min_length=1, max_length=1024)


class ImportRequest(StrictModel):
    bundle_relative_path: str = Field(min_length=1, max_length=1024)
    target_relative_path: str = Field(min_length=1, max_length=1024)
