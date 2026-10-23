"""Read-only loading of non-secret, host-local application settings."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


def _default_data_dir(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve(strict=False)
    configured = os.environ.get("CUTEFISH_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "CuteFish").resolve(strict=False)
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return (Path(state_home) / "cutefish").expanduser().resolve(strict=False)
    return (Path.home() / ".local" / "state" / "cutefish").resolve(strict=False)


def project_root() -> Path:
    # settings.py -> core -> cutefish -> src -> repository root
    return Path(__file__).resolve().parents[3]


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_dir: Path
    data_dir: Path
    database_path: Path
    host_config_path: Path
    feature_status_path: Path
    frontend_dist: Path
    host_id: str
    mode: str = "local_trial"
    timezone: str = "Asia/Shanghai"
    roots: dict[str, str]
    allowed_origins: tuple[str, ...]
    confirmed_design_import_root: Path | None = None
    host_config_status: str = "loaded"


def load_settings(
    *,
    data_dir: Path | None = None,
    host_config_path: Path | None = None,
    feature_status_path: Path | None = None,
    frontend_dist: Path | None = None,
) -> Settings:
    repo_root = project_root()
    resolved_data_dir = _default_data_dir(data_dir)
    config_override = os.environ.get("CUTEFISH_HOST_CONFIG")
    host_config = (
        Path(host_config_path).expanduser().resolve(strict=False)
        if host_config_path is not None
        else Path(config_override).expanduser().resolve(strict=False)
        if config_override
        else (repo_root.parent / "manage" / "host.local.json").resolve(strict=False)
    )

    roots: dict[str, str] = {
        "data_root": str(resolved_data_dir),
        "cache_root": str(resolved_data_dir / "cache"),
        "temp_root": str(resolved_data_dir / "tmp"),
        "backup_root": str(resolved_data_dir / "backup"),
        "gallery_root": str(resolved_data_dir / "gallery"),
    }
    config_status = "missing"
    configured_feature_status = os.environ.get("CUTEFISH_FEATURE_STATUS")
    confirmed_design_import_root: Path | None = None
    host_id_value: str | None = None
    if host_config.is_file():
        try:
            payload: Any = json.loads(host_config.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                config_status = "unsupported_schema"
            else:
                if configured_feature_status is None:
                    configured_feature_status = payload.get("feature_status_path")
                room_root = payload.get("room_root")
                if isinstance(room_root, str) and room_root.strip():
                    room_path = Path(room_root).expanduser()
                    if room_path.is_absolute():
                        # Preserve the configured path itself so HostPathResolver can
                        # reject a linked/junction root before resolving descendants.
                        roots["room_root"] = str(room_path)
                        config_status = "loaded"
                    else:
                        config_status = "invalid_room_root"
                else:
                    config_status = "room_root_missing"
                raw_host_id = payload.get("host_id")
                if isinstance(raw_host_id, str) and raw_host_id.strip():
                    host_id_value = raw_host_id.strip()[:128]
                raw_import_root = payload.get("confirmed_design_import_root")
                if isinstance(raw_import_root, str) and raw_import_root.strip():
                    candidate = Path(raw_import_root.strip()).expanduser()
                    if candidate.is_absolute():
                        # Keep the lexical path so the importer can still reject
                        # symlink/junction components when the owner requests import.
                        candidate = Path(os.path.abspath(candidate))
                        private_root = Path(os.path.abspath(repo_root.parent / "manage" / "private"))
                        try:
                            relative = candidate.relative_to(private_root)
                        except ValueError:
                            relative = None
                        if relative is not None and relative.parts:
                            confirmed_design_import_root = candidate
        except (OSError, UnicodeError, json.JSONDecodeError):
            config_status = "invalid_json"

    stable_material = os.path.normcase(str(resolved_data_dir)).casefold().encode("utf-8")
    stable_host_id = "local-" + hashlib.sha256(stable_material).hexdigest()[:16]
    host_id = host_id_value or stable_host_id

    if feature_status_path is not None:
        resolved_feature_status = Path(feature_status_path).expanduser().resolve(strict=False)
    elif isinstance(configured_feature_status, str) and configured_feature_status.strip():
        candidate = Path(configured_feature_status.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = host_config.parent / candidate
        resolved_feature_status = candidate.resolve(strict=False)
    else:
        resolved_feature_status = (repo_root.parent / "manage" / "feature-status.json").resolve(strict=False)

    origins = [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]
    custom_origins = os.environ.get("CUTEFISH_ALLOWED_ORIGINS")
    if custom_origins:
        origins.extend(item.strip() for item in custom_origins.split(",") if item.strip())

    return Settings(
        project_dir=repo_root,
        data_dir=resolved_data_dir,
        database_path=resolved_data_dir / "cutefish.sqlite3",
        host_config_path=host_config,
        feature_status_path=resolved_feature_status,
        frontend_dist=(
            Path(frontend_dist).expanduser().resolve(strict=False)
            if frontend_dist is not None
            else Path(os.environ.get("CUTEFISH_FRONTEND_DIST", str(repo_root / "frontend" / "dist")))
            .expanduser()
            .resolve(strict=False)
        ),
        host_id=host_id,
        roots=roots,
        allowed_origins=tuple(dict.fromkeys(origins)),
        confirmed_design_import_root=confirmed_design_import_root,
        host_config_status=config_status,
    )

